from backend.events import Event, aemit, emit
from dorian.types import Payload, RequestId, Ts

from backend.envs import aioredis

from dorian.data.science.operators import Operators
from dorian.data.science.tasks import Tasks
from dorian.ranking.objectives import Objectives
from dorian.evaluation.procedures import EvaluationProcedures
from dorian.event.helpers.lifecycle import ensure_ranking_objectives
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
from dorian.pipeline.recommendation import suggest_with_status
from dorian.event.handlers.recommendations import _serialize_suggestions
from dorian.ui.tooltips import TOOLTIPS
from dorian.experiment.kdtree import is_partial_profile

import asyncio
import json
import os
import traceback
import time


async def _safe(coro):
    """Wrapper for fire-and-forget tasks that catches and logs exceptions."""
    try:
        await coro
    except Exception as exc:
        await aemit(Event("BackgroundTaskFailed", data={"source": "handlers.session._safe", "error": str(exc), "trace": traceback.format_exc()}))


# =========================================================================
# Catalog cache — session-independent KB data computed once, reused by all.
#
# Invalidation contract:
#   Every code path that mutates Neo4j MUST emit a KBChanged event.
#   The handler (handle_kb_changed) invalidates LRU caches, rebuilds
#   the catalog cache, and pushes fresh catalogs to every connected WS
#   client via Redis XADD to their active streams.
#
# Mutation points (exhaustive):
#   1. dorian/knowledge/base.py        — initial seed (FORCE_SEED=1)
#   2. dorian/mcp/mitigation_tools.py  — mitigation_commit()
#   3. dorian/data/science/operators.py — Operators.add()
#   4. dorian/evaluation/procedures.py  — EvaluationProcedures.add()
#   5. POST /api/kb/invalidate-cache   — Go gateway admin endpoint
# =========================================================================

class _CatalogCache:
    """Cache for session-independent KB catalog messages.

    The catalogs (operators, tasks, objectives, evals, operator-params,
    operator type lookup) are identical for every session — they come from
    the Neo4j knowledge base which only changes on re-seed.  Computing and
    serializing them per-session wastes 2-4 seconds of Neo4j queries.

    This cache builds them once (on first seed_session or at startup via
    warm_start()) and reuses the pre-serialized Redis XADD message dicts
    for all subsequent sessions.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._ready = False
        self._built_at: float = 0

        # Raw catalog objects (needed for ensure_ranking_objectives).
        self.objectives = None

        # Pre-serialized XADD messages for Phase 2.
        self.phase2_msgs: list[dict[str, str]] = []

        # Pre-serialized operator-params message for Phase 3.
        self.operator_params_msg: dict[str, str] | None = None

        # Pre-serialized tooltips message.
        self.tooltips_msg: dict[str, str] | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def ensure_built(self):
        """Build the cache if not already built.  Thread-safe via asyncio.Lock."""
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return  # another coroutine built it while we waited
            await self._build()

    async def rebuild(self):
        """Force-rebuild the cache (called after KB mutation)."""
        async with self._lock:
            self._ready = False
            await self._build()

    async def _build(self):
        t0 = time.monotonic()
        try:
            # Fetch all catalogs in parallel.
            operators, tasks, objectives, evaluation_procedures, operator_params, op_type_lookup = (
                await asyncio.gather(
                    Operators.get(),
                    Tasks.get(),
                    Objectives.get(),
                    EvaluationProcedures.get(),
                    asyncio.to_thread(_build_operator_params),
                    asyncio.to_thread(_get_operator_type_lookup),
                )
            )

            self.objectives = objectives

            self.phase2_msgs = [
                {'event': 'state/operators', 'value': ','.join(f'{o.uuid}:{o.name}:{op_type_lookup.get(o.name, "operator")}' for o in operators), 'type': 'list'},
                {'event': 'state/tasks', 'value': ','.join(f'{t.uuid}:{t.name}' for t in tasks), 'type': 'list'},
                {'event': 'state/objectives', 'value': ','.join(f'{o.uuid}:{o.name}' for o in objectives), 'type': 'list'},
                {'event': 'state/evals', 'value': ','.join(f'{e.uuid}:{e.name}' for e in evaluation_procedures), 'type': 'list'},
            ]

            self.operator_params_msg = {
                'event': 'state/operator-params',
                'value': json.dumps(operator_params),
                'type': 'json',
            }

            self.tooltips_msg = {
                'event': 'ui/tooltips',
                'value': json.dumps(TOOLTIPS),
                'type': 'json',
            }

            elapsed = time.monotonic() - t0
            self._built_at = time.monotonic()
            self._ready = True
            await aemit(Event("CatalogCacheBuilt", {
                "source": "handlers.session._CatalogCache",
                "elapsed_ms": round(elapsed * 1000),
                "operators": len(operators),
                "tasks": len(tasks),
                "objectives": len(objectives),
                "evals": len(evaluation_procedures),
            }))
        except Exception as exc:
            await aemit(Event("CatalogCacheBuildFailed", {
                "source": "handlers.session._CatalogCache",
                "error": str(exc),
                "trace": traceback.format_exc(),
            }))
            raise


_catalog_cache = _CatalogCache()


# =========================================================================
# Public API — used by main.py startup and KB invalidation endpoint
# =========================================================================

async def warm_catalog_cache():
    """Build the catalog cache eagerly at server startup.

    Called from main.py lifespan so the first session pays zero cold-cache
    cost.  Safe to call multiple times — subsequent calls are no-ops if the
    cache is already built.
    """
    await _catalog_cache.ensure_built()


async def handle_kb_changed(event: Event):
    """React to any KB mutation: invalidate caches, rebuild, push to all clients.

    Subscribed to the ``KBChanged`` event which MUST be emitted by every
    code path that writes to Neo4j.  The handler:

    1. Clears all @lru_cache-wrapped sync KB queries in queries.py
    2. Clears the global _all_kb_params_cache
    3. Rebuilds the catalog cache from fresh Neo4j data
    4. Pushes updated catalogs to every currently connected WS client
    """
    source = (event.data or {}).get("source", "unknown")
    await aemit(Event("CatalogCacheInvalidating", {
        "source": "handlers.session.handle_kb_changed",
        "trigger": source,
    }))

    # 1. Clear sync LRU caches so threads calling get_operator_interface()
    #    etc. pick up fresh data.
    _clear_lru_caches()

    # 1b. Clear async KB caches in event handlers (risk_kb, query).
    _clear_async_kb_caches()

    # 2. Rebuild the catalog cache from Neo4j.
    await _catalog_cache.rebuild()

    # 3. Catalog broadcast moved to
    #    ``engine/backend/src/handlers/kb_changed.rs``. The rust
    #    handler reloads the on-disk snapshot into the
    #    ArcSwap-wrapped ``state.kb``, then pushes refreshed
    #    operators / tasks / objectives / evals / operator-params
    #    to every member of ``dorian:active_connections``. Calling
    #    ``_push_catalogs_to_active_sessions`` from python in
    #    addition would just double-emit on every WS stream.


def _clear_lru_caches():
    """Clear all @lru_cache-wrapped functions in dorian.knowledge.queries."""
    from dorian.knowledge.queries import (
        invalidate_all_kb_params_cache,
        get_operator_interface,
        get_library_package_map,
        get_operator_import_path,
        get_all_interface_methods,
        get_method_sequence,
        get_operators_for_task,
        get_operator_family,
        get_operators_by_interface,
        get_metrics_for_task,
        get_metric_display_name,
        get_all_operators,
        get_operator_parameters,
        get_interface_io,
        get_method_io,
        get_interface_attributes,
        get_operator_risks,
        get_model_family,
        get_sensitive_families_for_risk,
        get_risks_surfaced_by_metric,
        get_mitigation_kb_spec,
        get_all_pathways,
    )
    # Clear the single-level global cache.
    invalidate_all_kb_params_cache()
    # Clear every @lru_cache in the queries module.
    for fn in (
        get_operator_interface,
        get_library_package_map,
        get_operator_import_path,
        get_all_interface_methods,
        get_method_sequence,
        get_operators_for_task,
        get_operator_family,
        get_operators_by_interface,
        get_metrics_for_task,
        get_metric_display_name,
        get_all_operators,
        get_operator_parameters,
        get_interface_io,
        get_method_io,
        get_interface_attributes,
        get_operator_risks,
        get_model_family,
        get_sensitive_families_for_risk,
        get_risks_surfaced_by_metric,
        get_mitigation_kb_spec,
        get_all_pathways,
    ):
        fn.cache_clear()

    # ALSO clear caches outside the queries module that wrap KB data.
    # Without this, the RL scheduler's ``load_catalog`` continues to
    # return a cached result from before the reseed, and every RL batch
    # uses the stale operator catalog. This is exactly what kept the
    # ``predict.X → X_test`` KB change from taking effect until the
    # backend process restarted.
    try:
        from dorian.pipeline.generation.catalog import load_catalog
        load_catalog.cache_clear()
    except Exception:
        pass


def _clear_async_kb_caches():
    """Clear async caches in risk_kb and query modules on KB mutation.

    These caches assume KB immutability at runtime, but the MCP commit path
    and admin invalidation can mutate Neo4j/docstore while the process runs.
    """
    from dorian.event.handlers.risk_kb import (
        _mitigation_cache,
        _rewrite_rule_cache,
        _kb_risks_for_operator,
        _kb_mitigations_for_risk,
        _kb_principles_for_risk,
        _kb_checks_for_risk,
        _kb_direct_alternatives,
    )
    # Dict-based caches
    _mitigation_cache.clear()
    _rewrite_rule_cache.clear()
    # async_lru_cache-wrapped functions
    for fn in (
        _kb_risks_for_operator,
        _kb_mitigations_for_risk,
        _kb_principles_for_risk,
        _kb_checks_for_risk,
        _kb_direct_alternatives,
    ):
        if hasattr(fn, "cache_clear"):
            fn.cache_clear()


async def _push_catalogs_to_active_sessions():
    """Push fresh catalog messages to every connected WS client.

    Reads the ACTIVE_CONNECTIONS set from Redis, builds XADD commands for
    each stream, and pipelines them for efficiency.
    """
    members = await aioredis.smembers(RedisKeys.ACTIVE_CONNECTIONS)
    if not members:
        return

    msgs = _catalog_cache.phase2_msgs
    op_params_msg = _catalog_cache.operator_params_msg
    if not msgs:
        return

    pipe = aioredis.pipeline(transaction=False)
    count = 0
    for member in members:
        # member is "uid:session" — need to reconstruct stream key
        member_str = member if isinstance(member, str) else member.decode()
        parts = member_str.split(":", 1)
        stream = RedisKeys.stream(parts[0], parts[1]) if len(parts) == 2 else f"{member_str}:stream"
        for msg in msgs:
            pipe.xadd(stream, msg, maxlen=STREAM_MAXLEN, approximate=True)
        if op_params_msg:
            pipe.xadd(stream, op_params_msg, maxlen=STREAM_MAXLEN, approximate=True)
        count += 1

    if count > 0:
        await pipe.execute()
        await aemit(Event("CatalogsPushedToClients", {
            "source": "handlers.session._push_catalogs_to_active_sessions",
            "clients": count,
            "messages_per_client": len(msgs) + (1 if op_params_msg else 0),
        }))


async def _verify_dataset_profiling(uid: str, session: str, meta: dict) -> None:
    """Check dataset profiling state on session restart and take corrective action.

    Three states:
      A) No profile / partial profile  -> re-trigger full profiling (DataExists)
      B) Complete profile               -> ensure indexed (DataProfiled)

    Runs as a fire-and-forget ``create_task`` so ``seed_session`` returns
    immediately — the event bus handles everything downstream.
    """
    dataset = meta.get("dataset")
    if not dataset or not isinstance(dataset, dict):
        return

    did = dataset.get("did")
    fpath = dataset.get("fpath")
    if not did or not fpath:
        return

    # Guard: file must still exist on disk
    if not await asyncio.to_thread(os.path.isfile, fpath):
        await aemit(Event("DatasetFileMissing", data={
            "source": "handlers.session._verify_dataset_profiling",
            "uid": uid, "session": session, "did": did, "fpath": fpath,
        }))
        return

    profile = dataset.get("profile")

    if not profile or not isinstance(profile, dict) or is_partial_profile(profile):
        # Missing or incomplete profile — re-trigger full profiling chain
        await aemit(Event("DataExists", data={
            "uid": uid, "session": session, "did": did, "fpath": fpath,
        }))
        return

    # Complete profile — ensure it is indexed in the experiment store
    await aemit(Event("DataProfiled", data={
        "uid": uid, "session": session, "did": did,
    }))


# handle_websocket_disconnected was retired here — see
# engine/backend handlers/session.rs for the rust consumer that owns
# the WebsocketDisconnected → cleanup chain.


async def handle_session_renamed(event: Event, uid: str, session: str, payload: Payload, request_id: RequestId, ts: Ts):
    await aemit(Event("SessionRenamed", data={"uid": uid, "session": session, "requestId": request_id}))


def _get_operator_type_lookup() -> dict[str, str]:
    """Build operator name → type mapping (sync, runs in thread)."""
    from dorian.knowledge.queries import get_all_operators
    _TRACER_INTERFACES = {"Model Tracer", "Model Agnostic Tracer"}
    _VISUALIZER_NAMES = {"dorian.io.printout"}
    lookup = {}
    for op in get_all_operators():
        name = op["name"]
        if name in _VISUALIZER_NAMES:
            lookup[name] = "visualizer"
        elif op.get("interface") in _TRACER_INTERFACES:
            lookup[name] = "tracer"
        else:
            lookup[name] = "operator"
    return lookup


def _build_operator_params() -> dict:
    """Build the operator-params catalog (sync, runs in thread).

    Cold-start session-seed bottleneck (2026-04-20 telemetry:
    ``CatalogCacheBuilt elapsed_ms=3751 operators=3343``) came from a
    per-operator loop calling ``get_operator_interface`` +
    ``get_method_sequence`` + ``_resolve_io`` — each hitting Neo4j in
    a fresh session. With 3343 ops that was ~10k synchronous
    roundtrips.

    Fix: pre-load every per-operator lookup in four bulk Cypher
    queries, then iterate in Python against dicts. Cold seed drops
    to sub-second on the same DB.
    """
    from dorian.knowledge.queries import (
        get_all_kb_operator_params,
        get_operator_interfaces_bulk,
        get_method_sequences_bulk,
        get_operator_ios_bulk,
        get_interface_ios_bulk,
    )
    from dorian.pipeline.generation.catalog import (
        _INTERFACE_IO, _FUNCTION_IO_OVERRIDES,
    )

    # ── Pre-load everything once ──────────────────────────────────────
    op_interfaces = get_operator_interfaces_bulk()          # {op: iface}
    method_seqs = get_method_sequences_bulk()                # {iface: [methods]}
    op_ios = get_operator_ios_bulk()                         # {op: (in, out)}
    iface_ios = get_interface_ios_bulk()                     # {iface: (in, out)}

    def _io_dicts(
        kb_in: list[dict], kb_out: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        def _to_dict(p: dict, i: int) -> dict:
            d: dict = {
                "name": p["name"],
                "position": p.get("position", i),
                "type": p.get("type", "any"),
            }
            if p.get("default") is not None:
                d["default"] = p["default"]
            return d
        return (
            [_to_dict(p, i) for i, p in enumerate(kb_in)],
            [_to_dict(p, i) for i, p in enumerate(kb_out)],
        )

    def _io_for_operator(op_name: str, iface: str | None):
        """Resolve I/O port specs following the same precedence as
        ``pipeline.generation.catalog._resolve_io`` -- KB per-op >
        Python override > Python interface template > KB per-interface.
        """
        kb_op = op_ios.get(op_name)
        if kb_op and (kb_op[0] or kb_op[1]):
            return _io_dicts(*kb_op)
        override = _FUNCTION_IO_OVERRIDES.get(op_name)
        if override is not None:
            ins, outs = override
            return (
                [{"name": p.name, "position": p.position, "type": p.dtype} for p in ins],
                [{"name": p.name, "position": p.position, "type": p.dtype} for p in outs],
            )
        if iface and iface in _INTERFACE_IO:
            ins, outs = _INTERFACE_IO[iface]
            return (
                [{"name": p.name, "position": p.position, "type": p.dtype} for p in ins],
                [{"name": p.name, "position": p.position, "type": p.dtype} for p in outs],
            )
        if iface and iface in iface_ios:
            return _io_dicts(*iface_ios[iface])
        return None, None

    result: dict = {}
    for op_name, kb_params in get_all_kb_operator_params().items():
        annotated = [
            {
                "name": p["name"],
                "dtype": p.get("type", "str"),
                "default": p.get("default"),
                "method": p.get("method"),   # which method this param routes to
            }
            for p in kb_params
            if p.get("type")  # only chain-annotated params
        ]
        if annotated:
            entry: dict = {"params": annotated}
            iface = op_interfaces.get(op_name)
            if iface:
                methods = method_seqs.get(iface, [])
                if len(methods) >= 2:
                    entry["methods"] = methods
            inputs, outputs = _io_for_operator(op_name, iface)
            if inputs:
                entry["inputs"] = inputs
            if outputs:
                entry["outputs"] = outputs
            result[op_name] = entry
    emit(Event("OperatorParamsCatalogBuilt", {"source": "handlers.session._build_operator_params", "count": len(result)}))
    return result


async def seed_session(event: Event):
    """Seed a session with progressive delivery for optimal perceived latency.

    **Phase 1 + Phase 2 are now owned by the rust backend** (see
    ``engine/backend/src/handlers/session_seed.rs``). Both runtimes
    subscribe to ``InitSession``; the rust handler writes
    ``state/pipeline``, ``state/dataset``, ``state/target``,
    ``state/lastRun``, ``state/selected-task``, ``state/selected-eval``,
    ``state/custom-evals``, ``state/operators``, ``state/tasks``,
    ``state/objectives``, ``state/evals``, ``state/objectives/selected``,
    and ``state/operator-params`` directly from Redis + KbSnapshot +
    Postgres. The python handler now only fires Phase 3 — recommendations
    + ``state/queries`` resolution + dataset profiling verification +
    notification flush — paths that still depend on python-only modules
    (KDTree similarity, recommendation engine, partial-profile checks).

    Why the split: every reconnect to the SPA triggers ``InitSession``,
    so Phase 1+2 was the load-bearing dependency on the python event-bus.
    The python local-dispatch worker pool has a recurring silent-stall
    pattern (``project_python_eventbus_workers_degrade.md``); when the
    workers stop processing, the sidebar stays empty even though
    everything else is healthy. Moving the on-the-wire SPA seed to rust
    breaks that dependency — the sidebar populates as long as the
    rust-backend's redis subscriber is running, which is a single
    XREADGROUP loop with no asyncio worker pool to wedge.

    Phase 3 stays python-side because:
    - ``_deferred_cached_payloads`` still emits the python KB-derived
      tooltips + the operator-params hot path the user-facing flow
      benefits from caching.
    - ``flush_pending`` reads python-emitted in-app notifications.
    - ``_verify_dataset_profiling`` calls ``is_partial_profile`` whose
      KDTree backing isn't ported to rust yet.
    - ``_deferred_recommendations`` uses ``suggest_with_status`` which
      pulls the similarity-search recommendation engine — a substantial
      port of its own.
    """
    uid, session = event.data.get("uid"), event.data.get("session")
    try:
        raw = await aioredis.get(RedisKeys.session_meta(session))
        if not raw:
            await aemit(Event("SessionNotFound", data={"source": "handlers.session.seed_session", "uid": uid, "session": session}))
            return

        meta = json.loads(raw)
        await aemit(Event("SessionInitStarted", data={"source": "handlers.session.seed_session", "uid": uid, "session": session}))

        stream = RedisKeys.stream(uid, session)

        # =====================================================================
        # PHASE 1 + PHASE 2: owned by rust (engine/backend/src/handlers/
        # session_seed.rs). The python side used to xadd state/pipeline,
        # state/dataset, state/operators, state/tasks, state/objectives,
        # state/evals, state/operator-params, state/objectives/selected
        # and state/{target,lastRun,selected-task,selected-eval,custom-evals};
        # those all land via the rust handler now and the python xadds
        # were causing duplicate messages on the WS stream.
        #
        # We still need ``meta`` + ``has_pipeline`` for the Phase 3
        # branches below, which is why this function still parses the
        # session_meta JSON.
        # =====================================================================
        has_pipeline = bool(meta.get("pipelineHistory"))

        # =====================================================================
        # PHASE 3: Fire-and-forget deferred payloads
        # =====================================================================
        # The catalog cache was a Phase-2 build; Phase 2 moved to rust
        # but ``_deferred_cached_payloads`` still uses
        # ``_catalog_cache.tooltips_msg`` for the ``ui/tooltips`` push.
        # Keep the build so Phase 3 paths still find their pre-serialised
        # messages. ``ensure_built`` is idempotent + lock-guarded and
        # only runs the Neo4j queries the first time.
        await _catalog_cache.ensure_built()

        # Operator params + tooltips — pre-serialized in cache, just XADD.
        asyncio.create_task(_safe(_deferred_cached_payloads(uid, session, stream)))

        # ``flush_pending`` ported to
        # ``engine/backend/src/handlers/session_seed.rs`` — the rust
        # handler runs the LRANGE/XADD/DEL on every InitSession, so
        # python doesn't need to repeat it.

        # Verify dataset profiling (existing fire-and-forget).
        asyncio.create_task(_safe(_verify_dataset_profiling(uid, session, meta)))

        # Recommendations — ONLY when session has context to rank against.
        # For new/empty sessions (no dataset profile), this is pure waste:
        # the recommendation engine can't score without data context, and
        # attempt_recommendations will trigger properly when the user uploads
        # data and selects a task (via DataProfiled / DataScienceTaskSelected).
        dataset = meta.get("dataset")
        has_profile = isinstance(dataset, dict) and dataset.get("profile") is not None
        if has_profile:
            asyncio.create_task(_safe(_deferred_recommendations(uid, session, stream)))

    except Exception as e:
        await aemit(
            Event(
                "SessionInitFailed",
                data={
                    "source": "handlers.session.seed_session",
                    "uid": uid,
                    "session": session,
                    "error": str(e),
                    "trace": traceback.format_exc(),
                },
            )
        )


# =========================================================================
# Deferred Phase 3 helpers
# =========================================================================

async def _deferred_cached_payloads(uid: str, session: str, stream: str):
    """Phase 3: push tooltips from cache.

    ``state/operator-params`` used to fire here too, but the rust
    ``session_seed`` handler emits it during Phase 2 so the python
    push would be a duplicate (same KbSnapshot-derived JSON).
    Keep only ``ui/tooltips`` because the tooltip catalog still
    lives in ``dorian/ui/tooltips.py`` (python-only) — porting it
    is a separate slice.
    """
    pipe = aioredis.pipeline(transaction=False)
    if _catalog_cache.tooltips_msg:
        pipe.xadd(stream, _catalog_cache.tooltips_msg, maxlen=STREAM_MAXLEN, approximate=True)
    await pipe.execute()


async def _deferred_recommendations(uid: str, session: str, stream: str):
    """Phase 3: push recommendations (only called when session has data profile)."""
    try:
        suggestions, status = await suggest_with_status(uid, session)
        pipe = aioredis.pipeline(transaction=False)
        pipe.xadd(
            stream,
            {"event": "state/pipelines/recommendation", "value": _serialize_suggestions(suggestions), "type": "json"},
            maxlen=STREAM_MAXLEN, approximate=True,
        )
        pipe.xadd(
            stream,
            {"event": "state/objectives/status", "value": json.dumps(status), "type": "json"},
            maxlen=STREAM_MAXLEN, approximate=True,
        )
        await pipe.execute()
    except Exception as e:
        await aemit(Event("RecommendationEngineFailed", data={
            "source": "handlers.session._deferred_recommendations",
            "uid": uid,
            "session": session,
            "error": str(e),
            "trace": traceback.format_exc(),
        }))
        await aioredis.xadd(
            stream,
            {"event": "state/pipelines/recommendation", "value": json.dumps([]), "type": "json"},
            maxlen=STREAM_MAXLEN, approximate=True,
        )
