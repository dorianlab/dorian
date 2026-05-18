"""
dorian/api/routes/admin.py
--------------------------
Admin-only endpoints for system management (backup, diagnostics).

Access is gated by username — the ``admin.usernames`` list in config.yaml
contains GitHub usernames (NOT user IDs) of authorized admins.
"""
from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Form

from backend.admin_auth import require_admin
from backend.config import config
from backend.envs import aioredis, expdb
from backend.events import Event, aemit

# Tables we backup/restore from Postgres.  Order matters for restore
# (parents before children — evaluations/interactions FK → pipelines,
# datasets).  Add new tables here and they flow through automatically.
_PG_BACKUP_TABLES = (
    "datasets",
    "pipelines",
    "evaluations",
    "interactions",
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _admin_usernames() -> list[str]:
    """Return the configured admin username list exactly as written.

    Comparisons are case-sensitive — ``sergred`` and ``SergRed`` are
    distinct identifiers.  The allow-list must contain the exact
    GitHub login as it appears at sign-in.
    """
    try:
        return list(config.admin.usernames)
    except (AttributeError, KeyError):
        return []


def _is_admin_username(username: str | None) -> bool:
    """Return True only for real GitHub logins that appear in the admin list.

    Rules:
      * Rejects empty / missing usernames.
      * Rejects demo sandbox identifiers (``demo``, ``demo-<uuid>``).
      * Case-sensitive exact match against the configured allow-list —
        ``SergRed`` is NOT an admin if only ``sergred`` is allow-listed.

    This is the single source of truth for admin authorisation on the
    backend.
    """
    if not username:
        return False
    stripped = username.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered == "demo" or lowered.startswith("demo-"):
        return False
    return stripped in _admin_usernames()


def _assert_admin(username: str) -> None:
    """Raise 403 if *username* is not in the admin list."""
    if not _is_admin_username(username):
        raise HTTPException(status_code=403, detail="Not an admin")


# ---------------------------------------------------------------------------
# GET /admin/check — lightweight admin check (used by frontend to show button)
# ---------------------------------------------------------------------------

@router.get("/check")
async def check_admin(username: str):
    """Return ``{admin: true}`` if the given username is an admin.

    Demo / sandbox identifiers are always rejected here — admin status is
    gated to real GitHub logins in ``config.admin.usernames``.
    """
    return {"admin": _is_admin_username(username)}


# ---------------------------------------------------------------------------
# POST /admin/kb/invalidate-cache — force KB cache rebuild + push to clients
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# RL concurrency — runtime-adjustable cap on simultaneous RL pipelines
# ---------------------------------------------------------------------------

@router.get("/rl/concurrency")
async def rl_concurrency_status():
    """Current RL pipeline concurrency limit and in-flight count.

    Read-only; intentionally unauthenticated so the welcome screen and
    observability dashboard can render a live gauge. The returned shape
    is {"limit": int, "inflight": int} — no PII, no secrets.
    """
    from backend.queue import get_rl_concurrency
    return get_rl_concurrency()


@router.post("/rl/concurrency")
async def rl_concurrency_update(
    limit: int,
    caller: str = Depends(require_admin),
):
    """Update the RL pipeline concurrency limit at runtime.

    Admin-gated via ``X-Admin-Token`` + ``X-Admin-Username`` headers —
    the plaintext ``?username=`` scheme was replaced because URLs leak
    into logs / proxy traces. See ``backend/admin_auth.py``.

    Raises take effect immediately (queued waiters unblock). Lowers are
    soft — currently-running RL pipelines complete; new admissions wait
    until in-flight drops below the new limit. ``limit=0`` halts further
    RL admissions entirely.
    """
    from backend.queue import set_rl_concurrency
    if limit < 0:
        raise HTTPException(status_code=400, detail=f"limit must be >= 0, got {limit}")
    result = await set_rl_concurrency(limit)
    await aemit(Event("RLConcurrencyChanged", {"limit": limit, "by": caller}))
    return result


# ---------------------------------------------------------------------------
# Event-bus authoritative-type toggle (phase C of the Go-bus migration)
# ---------------------------------------------------------------------------

@router.get("/eventbus/authoritative")
async def eventbus_authoritative_list():
    """Return the set of event types currently marked authoritative in Go.

    Read-only; intentionally unauthenticated so observability dashboards
    can render the current state.
    """
    try:
        from backend import eventbus_authoritative as _authz
        return {
            "types": _authz.snapshot(),
            "meta": _authz.snapshot_meta(),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"eventbus not started: {exc}")


@router.post("/eventbus/authoritative")
async def eventbus_authoritative_add(
    event: str,
    caller: str = Depends(require_admin),
):
    """Mark an event type as authoritative in Go.

    Admin-gated via ``X-Admin-Token`` + ``X-Admin-Username`` headers.
    The local cache refreshes within ~1s; clients may see a brief window
    where local + Go both dispatch the event (at-least-once semantics,
    safe for idempotent handlers which all of ours are).
    """
    if not event or len(event) > 200:
        raise HTTPException(status_code=400, detail="event name required (<=200 chars)")
    try:
        from backend import eventbus_authoritative as _authz
        await _authz.add(event)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    await aemit(Event("EventBusAuthoritativeChanged", {
        "op": "add", "event": event, "by": caller,
    }))
    return {"event": event, "authoritative": True}


@router.delete("/eventbus/authoritative")
async def eventbus_authoritative_remove(
    event: str,
    caller: str = Depends(require_admin),
):
    """Unmark an event type. Local Python dispatch resumes for it."""
    if not event:
        raise HTTPException(status_code=400, detail="event name required")
    try:
        from backend import eventbus_authoritative as _authz
        await _authz.remove(event)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    await aemit(Event("EventBusAuthoritativeChanged", {
        "op": "remove", "event": event, "by": caller,
    }))
    return {"event": event, "authoritative": False}


# ---------------------------------------------------------------------------
# Event-bus go-handled toggle (phase G of the Go-bus migration)
# ---------------------------------------------------------------------------

@router.get("/eventbus/go-handled")
async def eventbus_go_handled_list():
    """Return the set of event types whose handler lives in Go.

    Read-only; intentionally unauthenticated. When a type is in this
    set, Python skips dispatch (both emit-side and via the in-process
    subscriber) and the Go subscriber owns the handler.
    """
    try:
        from backend import eventbus_go_handled as _go
        return {"types": _go.snapshot(), "meta": _go.snapshot_meta()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"eventbus not started: {exc}")


@router.post("/eventbus/go-handled")
async def eventbus_go_handled_add(
    event: str,
    caller: str = Depends(require_admin),
):
    """Mark an event type as Go-handled.

    Admin-gated. Propagates within ~1s to both Python (skip dispatch)
    and the Go subscriber (start dispatching). Safe to flip on
    idempotent handlers; one-off race around the flip may result in
    zero or one extra invocation (at-most-one / at-least-once, same as
    the authoritative toggle).
    """
    if not event or len(event) > 200:
        raise HTTPException(status_code=400, detail="event name required (<=200 chars)")
    try:
        from backend import eventbus_go_handled as _go
        await _go.add(event)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    await aemit(Event("EventBusGoHandledChanged", {
        "op": "add", "event": event, "by": caller,
    }))
    return {"event": event, "go_handled": True}


@router.delete("/eventbus/go-handled")
async def eventbus_go_handled_remove(
    event: str,
    caller: str = Depends(require_admin),
):
    """Unmark a type — Python resumes dispatch, Go stops dispatching."""
    if not event:
        raise HTTPException(status_code=400, detail="event name required")
    try:
        from backend import eventbus_go_handled as _go
        await _go.remove(event)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    await aemit(Event("EventBusGoHandledChanged", {
        "op": "remove", "event": event, "by": caller,
    }))
    return {"event": event, "go_handled": False}


@router.post("/kb/invalidate-cache")
async def invalidate_kb_cache(caller: str = Depends(require_admin)):
    """Force-rebuild the catalog cache from fresh Neo4j data and push
    updated catalogs to all connected WebSocket clients.

    Admin-gated via ``X-Admin-Token`` + ``X-Admin-Username`` headers.
    Called by the Go gateway's ``POST /api/kb/invalidate-cache`` endpoint
    after KB re-seeding completes, or manually for debugging.
    """
    await aemit(Event("KBChanged", data={"source": "admin/kb/invalidate-cache", "by": caller}))
    return {"status": "cache invalidated and pushed to active clients"}


# ---------------------------------------------------------------------------
# POST /admin/backup — full system snapshot
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# POST /admin/user-tier — set a user's subscription tier
# ---------------------------------------------------------------------------

@router.post("/user-tier")
async def set_user_tier_endpoint(
    username: str = Form(...),
    target_uid: str = Form(...),
    tier: str = Form(...),
):
    """Set the subscription tier for a user.

    Valid tiers: ``free``, ``standard``, ``priority``, ``enterprise``.
    """
    _assert_admin(username)

    from dorian.infra.tiers import set_user_tier, TIERS
    if tier not in TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tier {tier!r}. Valid: {list(TIERS)}",
        )

    await set_user_tier(target_uid, tier)
    await aemit(Event("UserTierChanged", data={
        "uid": target_uid,
        "tier": tier,
        "changed_by": username,
    }))
    return {"uid": target_uid, "tier": tier}


@router.get("/user-tier")
async def get_user_tier_endpoint(uid: str, username: str):
    """Get a user's current tier."""
    _assert_admin(username)

    from dorian.infra.tiers import get_user_tier
    tier = await get_user_tier(uid)
    return {"uid": uid, "tier": tier}


# ---------------------------------------------------------------------------
# POST /admin/backup — full system snapshot
# ---------------------------------------------------------------------------

@router.post("/backup")
async def trigger_backup(username: str = Form(...)):
    """Dump all Redis keys and document-store collections to disk.

    The backup is written to ``{config.fs.backup}/{timestamp}/``.
    Only admin usernames (from config) are allowed to trigger this.
    """
    _assert_admin(username)
    return await run_backup(triggered_by=username)


# ---------------------------------------------------------------------------
# POST /admin/shutdown — graceful system shutdown
# ---------------------------------------------------------------------------

@router.post("/shutdown")
async def trigger_shutdown(username: str = Form(...)):
    """Trigger a graceful shutdown of the system.

    Creates a backup first, then signals the application to stop.
    Only admin usernames (from config) are allowed to trigger this.
    """
    _assert_admin(username)

    # Import the shutdown event from main — set it so the lifespan exits.
    import main as _main

    backup_result = await run_backup(triggered_by=f"{username}/shutdown")

    await aemit(Event("GracefulShutdownRequested", data={
        "triggered_by": username,
        "backup": backup_result,
    }))

    # Signal the application to shut down after a small delay so the
    # response can be sent back to the client.
    async def _delayed_shutdown():
        await asyncio.sleep(1.0)
        _main.shutdown.set()

    asyncio.create_task(_delayed_shutdown())

    return {
        "status": "shutting_down",
        "backup": backup_result,
    }


# ---------------------------------------------------------------------------
# GET /admin/stats — public aggregate platform stats (welcome screen)
# ---------------------------------------------------------------------------

# Per-sub-call budget — prevents one slow DB from hanging the whole
# endpoint. Caching is disabled (see ``platform_stats``); the
# timeout is the only fall-back the endpoint has against a wedged
# DB connection.
_STATS_CALL_TIMEOUT_S = 5.0

# Last-known-good values per counter. If a counter times out we don't
# want to overwrite "3343 operators" with 0 — that's a worse UX than
# showing a slightly stale number. Zero values from fresh-install
# deployments ARE valid, so we only suppress the zero when we've
# previously seen a positive value for that counter.
_stats_last_good: dict[str, int] = {}


@router.get("/stats")
async def platform_stats():
    """Return aggregate platform counts for the welcome screen.

    No admin auth required — all values are read-only totals.
    Each counter runs behind a per-call timeout so a single DB being
    slow/down can't make the whole endpoint hang (previous behaviour:
    one slow counter stalled ``asyncio.gather`` for the full slow
    duration, freezing the welcome screen). Failed/timed-out
    counters return 0 and the rest of the page renders.

    Result is cached in-process for 30 seconds.
    """
    # Caching disabled — the welcome screen is the lowest-traffic
    # page of the deploy (one fetch per session-less render plus a
    # 30s polling refresh), and a 30s cache pinning 0/0/0 across
    # every visitor when the first call lands on cold-start
    # connection-pool init was strictly worse for UX than just
    # paying ~tens-of-ms per call. The KB queries are already O(1)
    # snapshot lookups; the postgres counts are
    # ``estimated_document_count`` (collection-metadata read, not a
    # scan); the redis session count is a single SCAN with
    # ``count=500``. Total uncached cost on a warm deploy is
    # well under 100ms.
    degraded_counters: set[str] = set()

    async def _with_timeout(coro, label: str) -> int:
        try:
            val = await asyncio.wait_for(coro, timeout=_STATS_CALL_TIMEOUT_S)
            if isinstance(val, int) and val > 0:
                _stats_last_good[label] = val
            return val
        except asyncio.TimeoutError:
            await aemit(Event("PlatformStatsCounterSlow", {"counter": label}))
            degraded_counters.add(label)
            return _stats_last_good.get(label, 0)
        except Exception:
            degraded_counters.add(label)
            return _stats_last_good.get(label, 0)

    async def _deduped_dataset_count() -> int:
        """Distinct-dataset count matching ``/datasets`` listing dedup.

        Shadow keys:
          * contentHash: same file body, two doc envelopes.
          * id-hex: a string _id whose hex matches an ObjectId _id
            elsewhere (pre-``8c8f1fc`` upload-path collision class).
        """
        total = await expdb.datasets.count_documents({})
        # Collect docs cheaply. ``projection`` keeps the payload small
        # so a 100-doc catalogue doesn't pull in profiles / features.
        cursor = expdb.datasets.find(
            {},
            projection={
                "_id": 1, "contentHash": 1, "isPublic": 1, "createdAt": 1,
            },
        )
        docs: list[dict] = []
        async for d in cursor:
            docs.append(d)
        if not docs:
            return total
        # Group by dedup keys; prefer public > private; then older
        # createdAt. Same policy as ``list_datasets``.
        def _keys_for(d: dict) -> list[str]:
            keys: list[str] = []
            ch = d.get("contentHash")
            if isinstance(ch, str) and ch:
                keys.append(f"hash:{ch}")
            idv = d.get("_id")
            idv_str = str(idv) if idv is not None else ""
            # Normalise both ObjectId and string _ids to lowercase hex;
            # a 24-char hex string collides with an ObjectId of the
            # same hex (the pre-``8c8f1fc`` bug). Both sides must
            # register the same key or the group can't collapse.
            if (
                len(idv_str) == 24
                and all(c in "0123456789abcdef" for c in idv_str.lower())
            ):
                keys.append(f"idhex:{idv_str.lower()}")
            return keys

        best: dict[str, dict] = {}
        for d in docs:
            for k in _keys_for(d):
                cur = best.get(k)
                if cur is None:
                    best[k] = d
                    continue
                if bool(d.get("isPublic")) and not bool(cur.get("isPublic")):
                    best[k] = d
                elif bool(d.get("isPublic")) == bool(cur.get("isPublic")):
                    if str(d.get("createdAt", "")) < str(cur.get("createdAt", "")):
                        best[k] = d

        shadowed = 0
        for d in docs:
            for k in _keys_for(d):
                winner = best.get(k)
                if winner is not None and winner is not d:
                    shadowed += 1
                    break
        return max(0, len(docs) - shadowed)

    async def _doc_count(collection_name: str) -> int:
        # estimated_document_count is O(1) on an indexed collection
        # (reads from collection metadata) — way faster than
        # count_documents({}) which does a full scan.
        try:
            if collection_name == "datasets":
                # Match the dedup logic in ``/datasets``: stale
                # user-uploads that shadow a public dataset by
                # contentHash (or by id-hex colliding with an ObjectId
                # _id) are NOT distinct datasets, just leftover ghost
                # entries from the pre-dedup upload path. Counting
                # them inflates the welcome-screen stat above the real
                # dataset catalogue size.
                return await _deduped_dataset_count()
            return await expdb[collection_name].estimated_document_count()
        except Exception:
            # Fall back to count_documents for mocked test envs.
            try:
                return await expdb[collection_name].count_documents({})
            except Exception:
                return 0

    async def _redis_session_count() -> int:
        """Count distinct human users with an active session.

        System uids (RL agents, demo sandboxes, e2e test fixtures,
        'system') are filtered out so the stat reflects real-user
        footprint rather than infrastructure noise. Without this
        filter the count was 42 (~1 real + ~41 RL/demo/e2e) instead
        of the handful of humans actually connected.
        """
        uids: set[str] = set()
        cursor = 0
        while True:
            cursor, keys = await aioredis.scan(cursor, match="user:*:sessions", count=500)
            for key in keys:
                parts = key.split(":")
                if len(parts) >= 2 and not _is_system_uid(parts[1]):
                    uids.add(parts[1])
            if cursor == 0:
                break
        return len(uids)

    def _is_system_uid(uid: str) -> bool:
        """True when ``uid`` is a synthetic / system-managed identity
        that should not count against the human-user platform stat.

        Allowed synthetic categories (per the uid policy in
        (internal design note; not in public repo)):

          * ``rl-`` / ``rl:`` — RL trainer / generator sessions
          * ``flaml-`` / ``flaml:`` — FLAML seeder
          * ``demo-`` / ``demo:`` — welcome-screen demo sandbox
          * ``phase*`` / ``clean-*`` / ``e2e*`` / ``system*`` —
            integration-test fixtures (kept for back-compat; new
            tests should pick from the above three).

        Any uid not in the above list AND not matching the
        synthetic-leak patterns (``test-uid-*``, ``u-<digits>``,
        ``bench-*``, ``sweep-*``) is treated as a real human user.
        Leak patterns are still classified as synthetic so the
        welcome-screen counter doesn't inflate while the producer
        is being hunted down.
        """
        if not uid:
            return True
        lowered = uid.lower()
        # Allowed synthetic categories.
        synthetic_prefixes = (
            "demo", "rl:", "rl-", "flaml:", "flaml-",
            "phase", "clean-", "system", "e2e",
        )
        if any(lowered.startswith(p) for p in synthetic_prefixes):
            return True
        # Leak / anti-pattern uids — should not exist; flagged here so
        # the stat doesn't count them while the producer is fixed.
        leak_prefixes = ("test-uid-", "bench-", "sweep-")
        if any(lowered.startswith(p) for p in leak_prefixes):
            return True
        # ``u-<unix-timestamp>`` from ad-hoc curl traffic.
        if lowered.startswith("u-") and lowered[2:].isdigit():
            return True
        return False

    async def _kb_operator_count() -> int:
        from dorian.data.science.operators import Operators
        operators = await Operators.get()
        return len(operators)

    async def _kb_task_count() -> int:
        from dorian.data.science.tasks import Tasks
        tasks = await Tasks.get()
        return len(tasks)

    async def _kb_eval_count() -> int:
        from dorian.evaluation.procedures import EvaluationProcedures
        evals = await EvaluationProcedures.get()
        return len(evals)

    async def _kb_ranking_objective_count() -> int:
        from dorian.ranking.objectives import Objectives
        objs = await Objectives.get()
        return len(objs)

    (
        datasets,
        pipelines,
        sessions,
        ranking_objectives,
        evaluation_procedures,
        operators,
        tasks,
        contact_submissions,
    ) = await asyncio.gather(
        _with_timeout(_doc_count("datasets"), "datasets"),
        _with_timeout(_doc_count("pipelines"), "pipelines"),
        _with_timeout(_redis_session_count(), "sessions"),
        _with_timeout(_kb_ranking_objective_count(), "ranking_objectives"),
        _with_timeout(_kb_eval_count(), "evaluation_procedures"),
        _with_timeout(_kb_operator_count(), "operators"),
        _with_timeout(_kb_task_count(), "tasks"),
        _with_timeout(_doc_count("contact_submissions"), "contact_submissions"),
    )

    result = {
        "datasets": datasets,
        "pipelines": pipelines,
        "sessions": sessions,
        "ranking_objectives": ranking_objectives,
        "evaluation_procedures": evaluation_procedures,
        "operators": operators,
        "tasks": tasks,
        "contact_submissions": contact_submissions,
    }

    return result


# ---------------------------------------------------------------------------
# Redis dump — iterate all keys and persist their values
# ---------------------------------------------------------------------------

async def _dump_redis(out_dir: Path) -> dict:
    """Scan all Redis keys and write their values to a single JSON file.

    Typed entry: ``{"type": <redis_type>, "value": <value>}``.  The type
    is essential for restore — a list restored as a string loses its
    semantics.  Returns a small summary with key count and byte size.
    """
    dump: dict[str, dict] = {}
    cursor = 0
    while True:
        cursor, keys = await aioredis.scan(cursor, count=500)
        for key in keys:
            try:
                key_type = await aioredis.type(key)
                if key_type == "string":
                    value = await aioredis.get(key)
                elif key_type == "list":
                    value = await aioredis.lrange(key, 0, -1)
                elif key_type == "set":
                    value = sorted(await aioredis.smembers(key))
                elif key_type == "hash":
                    value = await aioredis.hgetall(key)
                elif key_type == "stream":
                    entries = await aioredis.xrange(key)
                    value = [
                        {"id": eid, "fields": fields}
                        for eid, fields in entries
                    ]
                elif key_type == "zset":
                    raw = await aioredis.zrange(key, 0, -1, withscores=True)
                    value = [[member, score] for member, score in raw]
                else:
                    dump[key] = {"type": "unsupported", "value": f"<{key_type}>"}
                    continue
                dump[key] = {"type": key_type, "value": value}
            except Exception as exc:
                dump[key] = {"type": "error", "value": f"<{exc}>"}
        if cursor == 0:
            break

    blob = json.dumps(dump, indent=2, default=str, ensure_ascii=False)
    await asyncio.to_thread(
        (out_dir / "dump.json").write_text, blob, encoding="utf-8"
    )
    return {"keys": len(dump), "bytes": len(blob.encode("utf-8"))}


# ---------------------------------------------------------------------------
# Document-store dump — iterate all collections
# ---------------------------------------------------------------------------

async def run_backup(triggered_by: str = "system") -> dict:
    """Run a full system backup (Redis + Postgres document store + Postgres relational + Neo4j).

    Output layout (under ``{config.fs.backup}/{timestamp}/``):
        redis/dump.json
        docstore/{collection}.json
        postgres/{table}.jsonl
        neo4j/nodes.jsonl + relationships.jsonl
        manifest.json

    The manifest records counts, timestamps, and provenance so restore
    can validate what it's reading before touching live state.
    """
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(config.fs.backup) / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    counts: dict[str, dict] = {}

    # ── Redis ────────────────────────────────────────────────────────
    redis_dir = backup_dir / "redis"
    redis_dir.mkdir(exist_ok=True)
    try:
        counts["redis"] = await _dump_redis(redis_dir)
    except Exception as exc:
        errors.append(f"Redis: {exc}")
        traceback.print_exc()

    # ── Document store ──────────────────────────────────────────────────────
    doc_dir = backup_dir / "docstore"
    doc_dir.mkdir(exist_ok=True)
    try:
        counts["docstore"] = await _dump_doc_store(doc_dir)
    except Exception as exc:
        errors.append(f"Docstore: {exc}")
        traceback.print_exc()

    # ── Postgres ─────────────────────────────────────────────────────
    pg_dir = backup_dir / "postgres"
    pg_dir.mkdir(exist_ok=True)
    try:
        counts["postgres"] = await _dump_postgres(pg_dir)
    except Exception as exc:
        errors.append(f"Postgres: {exc}")
        traceback.print_exc()

    # ── KB snapshot ──────────────────────────────────────────────────
    # Neo4j was retired; the KB lives in the rust snapshot
    # (``volumes/kb_snapshot.json``). Copy that into the backup as a
    # single artifact instead of streaming nodes + relationships.
    snap_src = Path(
        os.environ.get("DORIAN_KB_SNAPSHOT", "/app/volumes/kb_snapshot.json")
    )
    snap_dst_dir = backup_dir / "kb"
    snap_dst_dir.mkdir(exist_ok=True)
    try:
        if snap_src.is_file():
            await asyncio.to_thread(
                (snap_dst_dir / "kb_snapshot.json").write_bytes,
                snap_src.read_bytes(),
            )
            counts["kb"] = {"snapshot_bytes": snap_src.stat().st_size}
        else:
            counts["kb"] = {"snapshot_bytes": 0}
    except Exception as exc:
        errors.append(f"KB snapshot: {exc}")
        traceback.print_exc()

    # ── Manifest ─────────────────────────────────────────────────────
    manifest = {
        "version": 1,
        "timestamp": ts,
        "triggered_by": triggered_by,
        "counts": counts,
        "errors": errors,
    }
    await asyncio.to_thread(
        (backup_dir / "manifest.json").write_text,
        json.dumps(manifest, indent=2, default=str),
        encoding="utf-8",
    )

    await aemit(Event("SystemBackupCompleted", data={
        "path": str(backup_dir),
        "triggered_by": triggered_by,
        "errors": errors,
        "counts": counts,
    }))

    return {
        "path": str(backup_dir),
        "errors": errors,
        "ok": len(errors) == 0,
        "counts": counts,
    }


async def _dump_doc_store(out_dir: Path) -> dict:
    """Dump every collection in the Dorian database to a JSON file.

    Returns ``{collection_name: doc_count}`` for the manifest.
    """
    counts: dict[str, int] = {}
    collection_names = await expdb.list_collection_names()
    for name in collection_names:
        coll = expdb[name]
        docs = []
        async for doc in coll.find():
            # ObjectId is not JSON serializable — convert to string.
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
            docs.append(doc)
        await asyncio.to_thread(
            (out_dir / f"{name}.json").write_text,
            json.dumps(docs, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        counts[name] = len(docs)
    return counts


# ---------------------------------------------------------------------------
# Postgres dump — per-table JSONL dumps via asyncpg pool
# ---------------------------------------------------------------------------

async def _dump_postgres(out_dir: Path) -> dict:
    """Dump each backup-eligible table as JSONL (one record per line).

    Uses the shared asyncpg pool — no `pg_dump` binary required in the
    backend image.  Column types are preserved via asyncpg's native
    decoding; jsonb comes back as dict, arrays as lists, timestamps as
    datetime (serialized via ``default=str``).
    """
    from backend.envs import get_pg_pool

    counts: dict[str, int] = {}
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        for table in _PG_BACKUP_TABLES:
            try:
                rows = await conn.fetch(f"SELECT * FROM {table}")
            except Exception as exc:
                counts[table] = f"error: {exc}"
                continue
            path = out_dir / f"{table}.jsonl"
            lines = "".join(
                json.dumps(dict(row), default=str, ensure_ascii=False) + "\n"
                for row in rows
            )
            await asyncio.to_thread(path.write_text, lines, encoding="utf-8")
            counts[table] = len(rows)
    return counts


# Neo4j dump/restore retired with the rust KB snapshot port —
# the snapshot is now backed up as a single JSON artefact (see the
# inline ``# ── KB snapshot ──`` blocks in the backup/restore
# routes above).


# ---------------------------------------------------------------------------
# Restore — read a backup directory and re-hydrate live state
# ---------------------------------------------------------------------------

async def run_restore(backup_name: str, triggered_by: str = "system") -> dict:
    """Restore a backup directory back into Redis/docstore/Postgres/Neo4j.

    ``backup_name`` is the timestamp subdirectory under ``config.fs.backup``
    (e.g. ``20260415_072419``).  The restore is idempotent per-key/row:
    Redis overwrites, Docstore upserts by ``_id``, Postgres uses
    ``INSERT ... ON CONFLICT DO NOTHING``, Neo4j reconstructs with MERGE
    keyed on internal id (remapped to new ids via a lookup dict).
    """
    backup_dir = Path(config.fs.backup) / backup_name
    if not backup_dir.is_dir():
        raise FileNotFoundError(f"Backup directory not found: {backup_dir}")

    errors: list[str] = []
    restored: dict[str, dict] = {}

    # ── Redis ────────────────────────────────────────────────────────
    redis_dump = backup_dir / "redis" / "dump.json"
    if redis_dump.exists():
        try:
            restored["redis"] = await _restore_redis(redis_dump)
        except Exception as exc:
            errors.append(f"Redis: {exc}")
            traceback.print_exc()

    # ── Document store ──────────────────────────────────────────────────────
    doc_dir = backup_dir / "docstore"
    if doc_dir.is_dir():
        try:
            restored["docstore"] = await _restore_doc_store(doc_dir)
        except Exception as exc:
            errors.append(f"Docstore: {exc}")
            traceback.print_exc()

    # ── Postgres ─────────────────────────────────────────────────────
    pg_dir = backup_dir / "postgres"
    if pg_dir.is_dir():
        try:
            restored["postgres"] = await _restore_postgres(pg_dir)
        except Exception as exc:
            errors.append(f"Postgres: {exc}")
            traceback.print_exc()

    # ── KB snapshot ──────────────────────────────────────────────────
    snap_dir = backup_dir / "kb"
    snap_src = snap_dir / "kb_snapshot.json"
    if snap_src.is_file():
        try:
            target = Path(
                os.environ.get(
                    "DORIAN_KB_SNAPSHOT", "/app/volumes/kb_snapshot.json"
                )
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                target.write_bytes, snap_src.read_bytes()
            )
            restored["kb"] = {"snapshot_bytes": snap_src.stat().st_size}
        except Exception as exc:
            errors.append(f"KB snapshot: {exc}")
            traceback.print_exc()

    await aemit(Event("SystemRestoreCompleted", data={
        "source": str(backup_dir),
        "triggered_by": triggered_by,
        "restored": restored,
        "errors": errors,
    }))

    return {
        "source": str(backup_dir),
        "restored": restored,
        "errors": errors,
        "ok": len(errors) == 0,
    }


async def _restore_redis(dump_path: Path) -> dict:
    """Replay a Redis dump.json back into the live Redis instance.

    The live database is WIPED first — restore is authoritative for
    Redis.  Blending old + new state silently keeps stale sessions,
    Stream entries, and rate-limit keys that outlived the backup.
    """
    raw = await asyncio.to_thread(dump_path.read_text, encoding="utf-8")
    data = json.loads(raw)
    await aioredis.flushdb()
    restored = 0
    for key, entry in data.items():
        if not isinstance(entry, dict) or "type" not in entry:
            continue
        t = entry["type"]
        v = entry["value"]
        try:
            await aioredis.delete(key)
            if t == "string":
                await aioredis.set(key, v)
            elif t == "list":
                if v:
                    await aioredis.rpush(key, *v)
            elif t == "set":
                if v:
                    await aioredis.sadd(key, *v)
            elif t == "hash":
                if v:
                    await aioredis.hset(key, mapping=v)
            elif t == "zset":
                if v:
                    mapping = {member: score for member, score in v}
                    await aioredis.zadd(key, mapping)
            elif t == "stream":
                for item in v:
                    fields = item.get("fields") or {}
                    if fields:
                        await aioredis.xadd(key, fields)
            else:
                continue
            restored += 1
        except Exception:
            continue
    return {"keys": restored}


async def _restore_doc_store(doc_dir: Path) -> dict:
    """Replace every collection's contents with the dump.

    Each collection is dropped before its documents are re-inserted —
    restore is authoritative for the document store. Upserting on top of
    live state would leave orphaned documents deleted after the backup.
    """
    counts: dict[str, int] = {}
    for path in sorted(doc_dir.glob("*.json")):
        name = path.stem
        coll = expdb[name]
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            docs = json.loads(raw)
        except Exception:
            counts[name] = 0
            continue
        try:
            await coll.drop()
        except Exception:
            pass
        n = 0
        for doc in docs:
            # _id is already a string in the JSONL backup — the Postgres
            # document store keys on TEXT so no coercion is needed.
            try:
                await coll.insert_one(doc)
                n += 1
            except Exception:
                continue
        counts[name] = n
    return counts


async def _restore_postgres(pg_dir: Path) -> dict:
    """Replace every backup-eligible table's rows with the dump.

    The tables are TRUNCATE'd (children first, CASCADE) before rows are
    replayed — restore is authoritative for Postgres.  Schema additions
    since the dump are tolerated as long as new columns are nullable or
    have defaults; the dump's column set is detected per-row from JSON
    keys.
    """
    from backend.envs import get_pg_pool

    counts: dict[str, int] = {}
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        # Truncate in reverse FK order (children before parents) with
        # CASCADE to sweep any dangling FK dependents.
        for table in reversed(_PG_BACKUP_TABLES):
            try:
                await conn.execute(f"TRUNCATE TABLE {table} CASCADE")
            except Exception:
                pass

        for table in _PG_BACKUP_TABLES:
            path = pg_dir / f"{table}.jsonl"
            if not path.exists():
                counts[table] = 0
                continue
            inserted = 0
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                cols = list(row.keys())
                vals = [_pg_decode(table, c, row[c]) for c in cols]
                placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
                col_list = ", ".join(f'"{c}"' for c in cols)
                sql = (
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT DO NOTHING"
                )
                try:
                    await conn.execute(sql, *vals)
                    inserted += 1
                except Exception:
                    continue
            counts[table] = inserted
    return counts


def _pg_decode(table: str, col: str, value):
    """Decode a JSON-roundtripped value back to the shape asyncpg expects.

    Postgres jsonb columns arrive as dict/list from ``dict(row)`` but
    asyncpg's INSERT binder expects raw JSON strings for jsonb columns.
    Timestamp strings are left as-is — Postgres casts them at insert time.
    """
    # Best-effort detection: the schema uses jsonb for these columns.
    jsonb_cols = {
        ("pipelines", "dag"),
        ("datasets", "profile"),
        ("evaluations", "eval_config"),
        ("interactions", "payload"),
    }
    if (table, col) in jsonb_cols and value is not None and not isinstance(value, str):
        return json.dumps(value, default=str)
    return value


# _restore_neo4j retired — KB restore is now a JSON file copy
# (see the inline ``# ── KB snapshot ──`` block in the restore
# route above). Old backup directories that still ship Neo4j JSONL
# dumps are ignored on restore.


# ---------------------------------------------------------------------------
# Backup listing + restore endpoints
# ---------------------------------------------------------------------------

@router.get("/backups")
async def list_backups(username: str):
    """Return all backup directories with their manifest summaries."""
    _assert_admin(username)
    root = Path(config.fs.backup)
    if not root.is_dir():
        return {"backups": []}

    items = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {"error": "manifest unreadable"}
        else:
            manifest = {"legacy": True}
        items.append({
            "name": entry.name,
            "path": str(entry),
            "manifest": manifest,
        })
    return {"backups": items}


@router.post("/restore")
async def trigger_restore(
    username: str = Form(...),
    backup_name: str = Form(...),
):
    """Restore a named backup directory back into live state."""
    _assert_admin(username)
    return await run_restore(backup_name, triggered_by=username)
