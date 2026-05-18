"""
dorian/knowledge/overlay.py
---------------------------
Postgres-backed overlay for KB statements curated at runtime.

The curated KB sources in ``dorian/knowledge/sources/*.py`` are the
authoritative content from the core team — they ship with the
deployment artifact. The overlay holds *additions* contributed at
runtime: MCP-curated mitigations, end-user-proposed risks,
pipeline-specific annotations, etc. Each overlay entry carries a
validation lifecycle so the core team (and eventually a community
voting layer) can review proposals before they're absorbed into
the next snapshot.

Why not JSON-on-disk: end-user contributions need persistence,
attribution, audit trail, and concurrent multi-writer access.
JSON files don't get those for free; the document store does.

Statement form
--------------
Each overlay row stores one statement in the same DSL the curated
sources use::

    "Resampling is a Mitigation"
    "Resampling with description Balance class frequencies via …"
    "Resampling might mitigate Class Imbalance"

When the snapshot is regenerated, validated overlay statements are
merged into the parsed ontology before walking — same predicate
parser, same semantics. Promoted statements move into a curated
``sources/*.py`` file by the core team and the overlay row is
marked ``promoted``.

Status lifecycle
----------------
``proposed`` → ``validated`` (or ``rejected``) → ``promoted``

- *proposed*: just inserted via MCP / UI; not visible to runtime.
- *validated*: a reviewer (core team member or community voting
  threshold) approved it. Picked up by the next snapshot build.
- *rejected*: archived with a reason; never enters the snapshot.
- *promoted*: moved into a curated source file. The overlay row
  stays for audit but is functionally redundant.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable
from uuid import uuid4

import pendulum

_log = logging.getLogger(__name__)

_COLLECTION_NAME = "kb_overlay"


def _collection():
    """Lazy import of the document store so module load is cheap."""
    from backend.envs import expdb
    return expdb[_COLLECTION_NAME]


# ═══════════════════════════════════════════════════════════════════
# Insert
# ═══════════════════════════════════════════════════════════════════

async def add_statement(
    statement: str,
    *,
    namespace: str = "core",
    tool: str = "mcp",
    uid: str | None = None,
    session: str | None = None,
    draft_id: str | None = None,
    initial_status: str = "proposed",
) -> str:
    """Insert one statement into the overlay.

    Returns the document ID. Idempotent on ``(statement, namespace,
    source.uid)`` — re-adding the same statement from the same user
    is a no-op (returns the existing row's ID).
    """
    statement = statement.strip()
    if not statement:
        raise ValueError("empty statement")

    coll = _collection()
    existing = await coll.find_one({
        "statement": statement,
        "namespace": namespace,
        "source.uid": uid,
    })
    if existing:
        return str(existing["_id"])

    doc_id = str(uuid4())
    now = pendulum.now("UTC").isoformat()
    await coll.insert_one({
        "_id": doc_id,
        "statement": statement,
        "namespace": namespace,
        "validation": {
            "status": initial_status,
            "votes": [],
            "validated_at": None,
            "validated_by": None,
            "rejected_at": None,
            "rejected_by": None,
            "rejection_reason": None,
            "promoted_to_snapshot": None,
        },
        "source": {
            "tool": tool,
            "uid": uid,
            "session": session,
            "draft_id": draft_id,
            "ts": now,
        },
    })
    return doc_id


async def add_statements(
    statements: Iterable[str],
    *,
    namespace: str = "core",
    tool: str = "mcp",
    uid: str | None = None,
    session: str | None = None,
    draft_id: str | None = None,
    initial_status: str = "proposed",
) -> list[str]:
    """Bulk insert. Returns the inserted/existing IDs in order."""
    out: list[str] = []
    for s in statements:
        out.append(await add_statement(
            s, namespace=namespace, tool=tool, uid=uid,
            session=session, draft_id=draft_id,
            initial_status=initial_status,
        ))
    return out


# ═══════════════════════════════════════════════════════════════════
# Validation lifecycle
# ═══════════════════════════════════════════════════════════════════

async def validate(doc_id: str, *, validator_uid: str) -> bool:
    """Mark a proposed overlay statement as validated."""
    now = pendulum.now("UTC").isoformat()
    res = await _collection().update_one(
        {"_id": doc_id, "validation.status": "proposed"},
        {"$set": {
            "validation.status": "validated",
            "validation.validated_at": now,
            "validation.validated_by": validator_uid,
        }},
    )
    return res.modified_count > 0


async def reject(doc_id: str, *, validator_uid: str, reason: str) -> bool:
    """Reject a proposed statement with a reason."""
    now = pendulum.now("UTC").isoformat()
    res = await _collection().update_one(
        {"_id": doc_id, "validation.status": "proposed"},
        {"$set": {
            "validation.status": "rejected",
            "validation.rejected_at": now,
            "validation.rejected_by": validator_uid,
            "validation.rejection_reason": reason,
        }},
    )
    return res.modified_count > 0


async def vote(doc_id: str, *, uid: str, verdict: str) -> bool:
    """Record a vote (``approve`` / ``reject``) on a proposed statement.

    Voting *aggregation* (when does N approves promote to validated?)
    is left to the validation orchestrator; this function only
    persists the vote.
    """
    if verdict not in ("approve", "reject"):
        raise ValueError(f"verdict must be approve|reject, got {verdict!r}")
    now = pendulum.now("UTC").isoformat()
    coll = _collection()
    # Replace the user's prior vote if any.
    await coll.update_one(
        {"_id": doc_id},
        {"$pull": {"validation.votes": {"uid": uid}}},
    )
    res = await coll.update_one(
        {"_id": doc_id},
        {"$push": {"validation.votes": {
            "uid": uid, "verdict": verdict, "ts": now,
        }}},
    )
    return res.modified_count > 0


async def mark_promoted(doc_id: str, *, snapshot_id: str) -> bool:
    """Mark a validated statement as promoted into a curated source."""
    res = await _collection().update_one(
        {"_id": doc_id, "validation.status": "validated"},
        {"$set": {
            "validation.status": "promoted",
            "validation.promoted_to_snapshot": snapshot_id,
        }},
    )
    return res.modified_count > 0


# ═══════════════════════════════════════════════════════════════════
# Read
# ═══════════════════════════════════════════════════════════════════

async def list_validated_statements(
    *,
    namespace: str | None = None,
) -> list[str]:
    """Return raw DSL statements ready to merge into the next snapshot.

    Includes ``validated`` and ``promoted`` rows; promoted rows
    survive in the snapshot until the next curated build.
    """
    flt: dict[str, Any] = {
        "validation.status": {"$in": ["validated", "promoted"]},
    }
    if namespace is not None:
        flt["namespace"] = namespace
    docs = await _collection().find(flt).to_list(length=None)
    return [d["statement"] for d in docs]


async def list_proposed(*, namespace: str | None = None) -> list[dict]:
    """Return full proposed-row docs for review UI / community voting."""
    flt: dict[str, Any] = {"validation.status": "proposed"}
    if namespace is not None:
        flt["namespace"] = namespace
    return await _collection().find(flt).to_list(length=None)


async def find_for_subject(subject: str) -> list[dict]:
    """All overlay rows whose statement begins with ``<subject>``.

    Used by ``kb_rewrite_annotations`` to surface MCP-curated rewrite
    metadata for a given mitigation.
    """
    # Statement is "<subject> <predicate> <object>" — exact prefix match.
    docs = await _collection().find({}).to_list(length=None)
    return [d for d in docs if d.get("statement", "").startswith(f"{subject} ")]
