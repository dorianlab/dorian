"""
dorian.mcp.session_tools — MCP tools that operate on a live user session.

Every tool takes a ``token`` as its first argument; the token is
issued by the frontend "Connect MCP" flow and resolved via
``dorian.mcp.token.resolve_token_sync``.

IMPORTANT: this module is SYNC end-to-end. The MCP server runs in a
separate process and FastMCP's tool decorator dispatches sync, so
mixing in ``aioredis`` / async expdb here deadlocks — the backend's
async clients are bound to the backend's event loop and break across
thread/loop boundaries. We open our own sync Redis + expdb clients
from the same config so both processes see the same state.

Tools:
    session_info(token)               → which extraction is active?
    session_read_extraction(token)    → full state: code, DAGs, rules
    session_read_rules(token)         → user's ordered json_specs list
    rule_persist_to_session(token,
        spec, insert_at, rationale,
        skip_compat_check)            → commit a rule to the list
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


# ────────────────────────────────────────────────────────────────────────────
# Module-local sync clients
# ────────────────────────────────────────────────────────────────────────────

def _expdb_sync():
    from dorian.mcp._backend_min import mcp_sync_expdb
    return mcp_sync_expdb()


def _sync_redis():
    from dorian.mcp._backend_min import mcp_sync_redis
    return mcp_sync_redis()


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _require_session(token: str) -> tuple[str, str]:
    from dorian.mcp.token import resolve_token_sync, McpAuthError
    resolved = resolve_token_sync(token)
    if resolved is None:
        raise McpAuthError(
            "MCP token invalid or expired. "
            "Click 'Connect MCP' in the extraction view to issue a fresh token."
        )
    return resolved


def _latest_extraction_for(uid: str, session: str) -> dict | None:
    from dorian.infra.keys import RedisKeys
    mdb = _expdb_sync()
    r = _sync_redis()
    active_id = r.get(RedisKeys.active_extraction(session))
    if active_id:
        if isinstance(active_id, bytes):
            active_id = active_id.decode()
        doc = mdb.extractions.find_one({"_id": active_id})
        if doc:
            return doc
    return mdb.extractions.find_one(
        {"uid": uid, "session": session},
        sort=[("createdAt", -1)],
    )


def _latest_rules_specs(uid: str) -> list[dict]:
    mdb = _expdb_sync()
    doc = mdb.extraction_rule_versions.find_one(
        {"uid": uid, "isValid": True, "format": "json_specs"},
        sort=[("createdAt", -1)],
    )
    if not doc or not doc.get("content"):
        return []
    try:
        specs = json.loads(doc["content"])
        return specs if isinstance(specs, list) else []
    except Exception:
        return []


def _xadd_sync(uid: str, session: str, fields: dict[str, Any]) -> None:
    """Push onto the user's WS outgoing stream (same key the async backend
    uses). Mirror of dorian/event/helpers/lifecycle.py::_xadd."""
    r = _sync_redis()
    stream = f"{uid}:{session}:stream"
    # xadd expects all values as strings; coerce.
    payload = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in fields.items()}
    r.xadd(stream, payload, maxlen=10_000, approximate=True)


# ────────────────────────────────────────────────────────────────────────────
# Read-side tools
# ────────────────────────────────────────────────────────────────────────────

def session_info(token: str) -> dict:
    uid, session = _require_session(token)
    extraction = _latest_extraction_for(uid, session)
    specs = _latest_rules_specs(uid)
    return {
        "uid": uid,
        "session": session,
        "extraction_id": extraction.get("_id") if extraction else None,
        "filename": extraction.get("filename") if extraction else None,
        "language": extraction.get("language") if extraction else None,
        "rules_version": extraction.get("rulesVersion") if extraction else None,
        "rules_count": len(specs),
        "has_auto_dag": bool(extraction and extraction.get("autoDag")),
        "has_corrected_dag": bool(extraction and extraction.get("correctedDag")),
    }


def session_read_extraction(token: str) -> dict:
    uid, session = _require_session(token)
    extraction = _latest_extraction_for(uid, session)
    if extraction is None:
        return {"error": "No extraction in this session yet",
                "uid": uid, "session": session}
    specs = _latest_rules_specs(uid)
    return {
        "uid": uid,
        "session": session,
        "extraction_id": extraction.get("_id"),
        "filename": extraction.get("filename"),
        "language": extraction.get("language"),
        "code": extraction.get("code"),
        "auto_dag": extraction.get("autoDag"),
        "corrected_dag": extraction.get("correctedDag"),
        "rules_version": extraction.get("rulesVersion"),
        "rules_snapshot": specs,
        "status": extraction.get("status"),
    }


def session_read_rules(token: str) -> dict:
    uid, _session = _require_session(token)
    specs = _latest_rules_specs(uid)
    return {
        "rules": [{"position": i, "spec": s} for i, s in enumerate(specs)],
        "count": len(specs),
    }


# ────────────────────────────────────────────────────────────────────────────
# Write-side
# ────────────────────────────────────────────────────────────────────────────

def rule_persist_to_session(
    token: str,
    spec: dict,
    insert_at: int | None = None,
    rationale: str = "",
    skip_compat_check: bool = False,
) -> dict:
    from dorian.mcp.rule_schema import validate_rule_spec
    from dorian.mcp.rule_compiler import compile_rule

    uid, session = _require_session(token)

    validated, errors = validate_rule_spec(spec)
    if errors:
        return {"status": "schema_error", "errors": errors}
    compiled, compile_errors, compile_warnings = compile_rule(validated)
    if compiled is None or compile_errors:
        return {
            "status": "compile_error",
            "errors": compile_errors,
            "warnings": compile_warnings,
        }

    existing = _latest_rules_specs(uid)
    if insert_at is None:
        insert_at = len(existing)
    insert_at = max(0, min(int(insert_at), len(existing)))
    candidate = existing[:insert_at] + [validated] + existing[insert_at:]

    # Backward-compat replay. Uses the async-only extractor under the hood
    # via asyncio.run — this is a ONE-OFF per tool call and doesn't share
    # state with any other loop, so it's safe.
    regressions: list[dict] = []
    if not skip_compat_check:
        try:
            import asyncio
            regressions = asyncio.run(_backward_compat_check(uid, candidate))
            if regressions:
                return {
                    "status": "blocked_backward_compat",
                    "regressions": regressions,
                    "hint": "Re-call with skip_compat_check=True to override "
                            "(audit-logged), or tighten the rule pattern.",
                }
        except Exception as exc:
            # Don't block a legitimate save on a flaky compat check —
            # surface the error in the result so the caller knows.
            regressions = []
            compile_warnings = (compile_warnings or []) + [f"compat check errored: {exc}"]

    content = json.dumps(candidate)
    rules_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    doc = {
        "uid": uid,
        "session": session,
        "content": content,
        "format": "json_specs",
        "rulesHash": rules_hash,
        "createdAt": datetime.now(timezone.utc),
        "isValid": True,
        "source": "mcp_client",
        "rationale": rationale or "",
        "compatOverride": bool(skip_compat_check),
    }
    mdb = _expdb_sync()
    mdb.extraction_rule_versions.insert_one(doc)

    # Emit a refresh event onto the user's outgoing WS stream so the
    # card UI reloads without a page refresh.
    _xadd_sync(uid, session, {
        "event": "extraction/rules-updated",
        "value": json.dumps({
            "source": "mcp",
            "rulesHash": rules_hash,
            "count": len(candidate),
            "insert_at": insert_at,
        }),
    })

    return {
        "status": "ok",
        "rules_version_after": rules_hash,
        "position": insert_at,
        "count": len(candidate),
        "regressions": [],
        "warnings": compile_warnings or [],
    }


async def _backward_compat_check(uid: str, candidate_specs: list[dict]) -> list[dict]:
    """Async helper — runs the extractor against the corpus.

    Scoped to a single asyncio.run() so the loop lifecycle is self-
    contained per call. Doesn't touch the backend's long-lived async
    clients.
    """
    from dorian.mcp.rule_compiler import compile_rule as _compile
    from dorian.mcp.dag_tools import semantic_dag_diff
    from dorian.code.parsing.parser import parse as parse_code
    from dorian.code.parsing.rules import get_rules as _default_rules

    compiled_all = []
    for s in candidate_specs:
        r, errs, _w = _compile(s)
        if r is not None and not errs:
            compiled_all.append(r)
    effective = list(_default_rules()) + compiled_all

    # Corpus read via the sync expdb wrapper — safe in MCP's single-call
    # subprocess pattern (one asyncio.run per operation, no loop conflict).
    mdb = _expdb_sync()
    records = list(
        mdb.extractions.find({"uid": uid}).sort("createdAt", 1).limit(500)
    )

    regressions: list[dict] = []
    for rec in records:
        accepted = rec.get("correctedDag") or rec.get("autoDag")
        if not accepted:
            continue
        try:
            dag = await parse_code(rec["code"], rec.get("language", "python"), effective)
        except Exception:
            regressions.append({
                "extraction_id": rec["_id"],
                "diff_summary": "parse failure under candidate rules",
                "missing_ops": [],
                "extra_ops": [],
            })
            continue
        replayed = dag.to_json_dict()
        diff = semantic_dag_diff(replayed, accepted)
        if diff.get("summary") and "0 differences" not in diff.get("summary", ""):
            regressions.append({
                "extraction_id": rec["_id"],
                "diff_summary": diff.get("summary"),
                "missing_ops": diff.get("missing_operators", []),
                "extra_ops": diff.get("extra_operators", []),
            })
    return regressions
