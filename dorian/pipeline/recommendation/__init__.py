"""Recommendation engine — fetch, score, rank pipeline candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.envs import aioredis, expdb
from backend.config import config
from backend.events import Event, aemit
from dorian.pipeline.recommendation.objectives import (
    resolve_objectives,
    check_dependencies,
    OBJECTIVE_REGISTRY,
    UserDefinedObjective,
)

# ---------------------------------------------------------------------------
# Constants — values come from config.yaml (development.recommendation.*)
# ---------------------------------------------------------------------------
_rec = config.recommendation
DEFAULT_LIMIT: int             = int(_rec.limit)
_RETRIEVAL_POOL: int           = int(_rec.retrieval_pool)
_TASK_FALLBACK_THRESHOLD: int  = int(_rec.task_fallback_threshold)


# ---------------------------------------------------------------------------
# Context — immutable snapshot for one suggest() call
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecommendationContext:
    uid: str
    session: str
    current_pipeline: Optional[Dict[str, Any]] = None
    dataset_profile: Optional[Any] = None
    upvoted: List[str] = field(default_factory=list)
    downvoted: List[str] = field(default_factory=list)
    selected: List[str] = field(default_factory=list)
    suggested: List[str] = field(default_factory=list)
    objective_names: List[str] = field(default_factory=list)
    task: Optional[str] = None
    custom_objective_defs: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def has_pipeline(self) -> bool:
        return self.current_pipeline is not None

    @property
    def has_interactions(self) -> bool:
        return bool(self.upvoted or self.downvoted or self.selected)


# ===================================================================
# Public API
# ===================================================================
async def suggest_with_status(
    uid: str, session: str, *, limit: int = DEFAULT_LIMIT
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build context, fetch candidates, score, rank.

    Returns ``(ranked_candidates, objective_status)`` where
    ``objective_status`` is a list of ``{name, status, missing}`` dicts
    for the frontend to display active/degraded indicators.
    """
    ctx = await _build_context(uid, session)

    # Anchor the candidate pool on the user's primary objective when
    # we have a quality signal for it (e.g. PPR's win-rate cache).
    # Falls back to random ``$sample`` when no anchor applies — keeps
    # cold-start sessions working without surfacing empty pools.
    primary = ctx.objective_names[0] if ctx.objective_names else None
    candidates = await _fetch_candidates(
        exclude_ids=set(ctx.downvoted),
        limit=_RETRIEVAL_POOL,
        task=ctx.task,
        primary_objective=primary,
    )

    # Error-pattern-aware filter: drop candidates whose operator set
    # overlaps with operators known to fail on this dataset. The same
    # mask the RL action selector uses (see
    # dorian/pipeline/generation/error_learning.py). If the agent has
    # learned that StandardScaler keeps crashing on this dataset's data
    # profile, we shouldn't recommend existing pipelines that include
    # StandardScaler — the user would hit the same failure.
    candidates = await _filter_candidates_by_error_patterns(
        candidates, uid=uid, session=session,
    )

    objectives = resolve_objectives(ctx.objective_names, ctx.custom_objective_defs)
    status = check_dependencies(objectives, ctx)

    # Pre-load win-rate cache if PPR objective is active
    if any(getattr(obj, "name", "") == "Pipeline Preference Ratio" for obj in objectives):
        try:
            from dorian.experiment.store import get_experiment_store
            store = await get_experiment_store()
            if store.is_initialized:
                await store.preload_win_rates()
        except Exception:
            pass  # PPR degrades gracefully to 0.0

    ranked = _rank(candidates, objectives, ctx, limit)

    # record what we suggested so PreviouslyUnseen can use it next round
    ids = [str(c.get("_id", "")) for c in ranked]
    await _append_interactions(session, "suggested", ids)

    return ranked, status


async def suggest(uid: str, session: str, *, limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Build context, fetch candidates, score, rank, return top-*limit*.

    Thin wrapper around :func:`suggest_with_status` for backward compat.
    """
    ranked, _ = await suggest_with_status(uid, session, limit=limit)
    return ranked


async def record_interaction(session: str, kind: str, pipeline_id: str) -> None:
    """Append a single interaction (upvoted / downvoted / selected)."""
    await _append_interactions(session, kind, [pipeline_id])


# ===================================================================
# Internal — context builder
# ===================================================================
async def _build_context(uid: str, session: str) -> RecommendationContext:
    interactions = await _load_interactions(session)

    raw = await aioredis.get(f"session:{session}:meta")
    meta: dict = json.loads(raw) if raw else {}

    current_pipeline = meta.get("pipeline") or None
    dataset = meta.get("dataset")
    profile = dataset.get("profile") if isinstance(dataset, dict) else None

    objective_names = [
        item["name"]
        for item in (meta.get("rankingObjectives") or [])
        if isinstance(item, dict) and item.get("name")
    ]

    selected_task = meta.get("selectedDataScienceTask")
    task_name = selected_task.get("name") if isinstance(selected_task, dict) else None

    # Load custom objective code from the docstore for names not in built-in registry
    custom_defs = await _load_custom_objective_code(session, objective_names)

    return RecommendationContext(
        uid=uid,
        session=session,
        current_pipeline=current_pipeline,
        dataset_profile=profile,
        upvoted=interactions.get("upvoted", []),
        downvoted=interactions.get("downvoted", []),
        selected=interactions.get("selected", []),
        suggested=interactions.get("suggested", []),
        objective_names=objective_names,
        task=task_name,
        custom_objective_defs=custom_defs,
    )


async def _load_custom_objective_code(
    session: str, names: List[str]
) -> List[Dict[str, Any]]:
    """Fetch custom objective code from the docstore for names not in the built-in registry."""
    custom_names = [n for n in names if n not in OBJECTIVE_REGISTRY]
    if not custom_names:
        return []

    try:
        col = expdb.ranking_objectives
        cursor = col.find(
            {"sessionId": session, "name": {"$in": custom_names}},
            {"name": 1, "code": 1, "language": 1, "_id": 0},
        )
        return await cursor.to_list(length=100)
    except Exception as exc:
        await aemit(Event("CustomObjectiveLoadFailed", {"error": str(exc), "session": session}))
        return []


# ===================================================================
# Internal — candidate retrieval (single function, replaces 2 classes)
# ===================================================================
async def _fetch_candidates(
    *,
    exclude_ids: set[str],
    limit: int,
    task: Optional[str] = None,
    primary_objective: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Pull a candidate pool from the pipelines collection.

    The primary objective (top of the user's curated list) decides
    *how* the pool is built:

    - **Pipeline Preference Ratio** — anchor on the rust win-rate
      cache (``rec_top_pipelines_by_win_rate``). Gives PPR something
      to chew on instead of random pipelines that all score 0.
    - **Faster Execution** — sort by smallest ``nodes`` count.
      Cheap (an index expression on ``data->'nodes'`` would help)
      but always available.
    - **everything else** — random ``$sample``. Mean-score and KD-tree
      anchors would each need a precomputed sort key; punted until the
      eval-aggregation pass lands.

    Task filtering is a soft hint: if it yields fewer than
    ``_TASK_FALLBACK_THRESHOLD`` results, we retry without the task
    filter so the pool stays large enough to rank.
    """
    str_excludes = [str(e) for e in (exclude_ids or set()) if e]

    anchored = await _anchored_candidates(
        primary_objective,
        exclude_ids=str_excludes,
        task=task,
        limit=limit,
    )
    if anchored is not None:
        return anchored

    return await _sample_candidates(str_excludes, task=task, limit=limit)


async def _anchored_candidates(
    primary: Optional[str],
    *,
    exclude_ids: List[str],
    task: Optional[str],
    limit: int,
) -> Optional[List[Dict[str, Any]]]:
    """Primary-objective dispatch. Returns ``None`` to fall back to
    ``$sample``; an empty list still counts as "no anchor available"
    and triggers the fallback (e.g. cold-start with no win rates yet).
    """
    if not primary:
        return None

    if primary == "Pipeline Preference Ratio":
        try:
            import dorian_native  # type: ignore
            ids = dorian_native.rec_top_pipelines_by_win_rate(limit, exclude_ids)
        except Exception:
            return None
        if not ids:
            return None
        return await _fetch_by_ids(ids)

    if primary == "Faster Execution":
        # Smallest-pipeline-first via Postgres-side jsonb_array_length.
        # Cheaper than streaming every doc to python.
        return await _fetch_smallest_pipelines(
            exclude_ids=exclude_ids,
            task=task,
            limit=limit,
        )

    return None


async def _fetch_by_ids(ids: List[str]) -> List[Dict[str, Any]]:
    cur = expdb.pipelines.find({"_id": {"$in": ids}})
    docs = await cur.to_list(length=len(ids))
    by_id = {str(d.get("_id")): d for d in docs}
    return [by_id[i] for i in ids if i in by_id]


async def _fetch_smallest_pipelines(
    *,
    exclude_ids: List[str],
    task: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Pull the ``limit`` pipelines with the fewest nodes. Sorts on
    ``jsonb_array_length(data->'nodes')`` (list-shaped nodes) and
    falls back to the keys-count when nodes is a dict — the same
    candidates show both shapes in seeded data. On any DB error we
    fall back to a random ``$sample`` so cold paths still get a pool.
    """
    from backend.db.pg_docstore import _json_path  # type: ignore

    # ``doc_pipelines`` is the per-collection table (each collection
    # has its own ``doc_<name>`` table — see backend/db/pg_docstore.py).
    where: List[str] = []
    args: List[Any] = []
    if exclude_ids:
        args.append(exclude_ids)
        where.append(f"id <> ALL(${len(args)})")
    if task:
        args.append(task)
        where.append(f"{_json_path('task')} = ${len(args)}")

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT id, data FROM doc_pipelines {where_clause} "
        "ORDER BY (CASE jsonb_typeof(data->'nodes') "
        "             WHEN 'array' THEN jsonb_array_length(data->'nodes') "
        "             WHEN 'object' THEN "
        "                (SELECT COUNT(*)::int FROM jsonb_object_keys(data->'nodes')) "
        "             ELSE 1000000 "
        "          END) ASC "
        f"LIMIT {int(limit)}"
    )
    pool = await expdb.pipelines._get_pool()  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(sql, *args)
        except Exception:
            return await _sample_candidates(exclude_ids, task=task, limit=limit)
    from backend.db.pg_docstore import _row_to_doc  # type: ignore
    return [_row_to_doc(row) for row in rows]


async def _sample_candidates(
    exclude_ids: List[str],
    *,
    task: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Random sample of pipelines, optionally task-filtered, with fallback.

    Direct SQL — the only ex-aggregation pipeline in the codebase. The legacy docstore
    ``[$match, $sample]`` translates 1:1 to ``WHERE … ORDER BY random()
    LIMIT n``, no need for the shim's ``aggregate()`` translator. When
    the task filter under-fills below ``_TASK_FALLBACK_THRESHOLD``,
    re-run without it so a cold session still has a candidate pool.
    """
    from backend.db.pg_docstore import _json_path, _row_to_doc  # type: ignore

    pool = await expdb.pipelines._get_pool()  # type: ignore[attr-defined]

    async def _query(with_task: bool) -> List[Dict[str, Any]]:
        # ``doc_pipelines`` is the per-collection table.
        where: List[str] = []
        args: List[Any] = []
        if exclude_ids:
            args.append(exclude_ids)
            where.append(f"id <> ALL(${len(args)})")
        if with_task and task:
            args.append(task)
            where.append(f"{_json_path('task')} = ${len(args)}")
        where_clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            f"SELECT id, data FROM doc_pipelines {where_clause} "
            f"ORDER BY random() LIMIT {int(limit)}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [_row_to_doc(r) for r in rows]

    results = await _query(with_task=True)
    if task and len(results) < _TASK_FALLBACK_THRESHOLD:
        results = await _query(with_task=False)
    return results


async def _filter_candidates_by_error_patterns(
    candidates: List[Dict[str, Any]],
    *,
    uid: str,
    session: str,
) -> List[Dict[str, Any]]:
    """Remove candidate pipelines whose operators have known failure
    patterns on the active dataset.

    Reads the failure corpus via ``error_learning.invalid_ops_for_dataset``.
    For each candidate, if any of its operators is in the masked set,
    the candidate is dropped and a ``RecommendationFilteredByErrors``
    observability event fires with the offending operator so downstream
    analysis can reason about which mitigations need improvement.

    Fails open: if the lookup errors (docstore unavailable, etc.) the
    original candidate list is returned unchanged — we never block
    recommendations on an observability-side failure.
    """
    try:
        from dorian.pipeline.generation.error_learning import invalid_ops_for_dataset
    except Exception:
        return candidates

    # Dataset id lives in session meta.
    try:
        raw = await aioredis.get(f"session:{session}:meta")
        meta = json.loads(raw) if raw else {}
        dataset = meta.get("dataset") or {}
        dataset_id = dataset.get("did") if isinstance(dataset, dict) else None
    except Exception:
        dataset_id = None

    if not dataset_id:
        return candidates

    # Build the set of all operator FQNs appearing in the candidate pool.
    all_ops: set[str] = set()
    for c in candidates:
        for node in (c.get("nodes") or {}).values():
            name = (node or {}).get("name") if isinstance(node, dict) else None
            if name and isinstance(name, str) and "." in name:
                all_ops.add(name)

    if not all_ops:
        return candidates

    try:
        masked, stats = await invalid_ops_for_dataset(dataset_id, all_ops)
    except Exception:
        return candidates

    if not masked:
        return candidates

    kept: List[Dict[str, Any]] = []
    dropped_by_op: dict[str, int] = {}
    for c in candidates:
        hit: str | None = None
        for node in (c.get("nodes") or {}).values():
            name = (node or {}).get("name") if isinstance(node, dict) else None
            if name in masked:
                hit = name
                break
        if hit is None:
            kept.append(c)
        else:
            dropped_by_op[hit] = dropped_by_op.get(hit, 0) + 1

    if dropped_by_op:
        try:
            await aemit(Event("RecommendationFilteredByErrors", {
                "uid": uid,
                "session": session,
                "dataset_id": dataset_id,
                "dropped_by_operator": dropped_by_op,
                "kept": len(kept),
                "considered": len(candidates),
            }))
        except Exception:
            pass  # observability must not break recommendation delivery

    return kept


# ===================================================================
# Internal — ranking (rust)
# ===================================================================

# Ranking strategy from config. Default ``nds_lex`` matches the rust
# default — Pareto fronts with lex tie-break by user objective order.
# Other accepted values: ``lexicographic``, ``nds``, ``jensen``,
# ``weighted_sum``.
_RANKING_STRATEGY: str = getattr(_rec, "strategy", "nds_lex")


def _rank(
    candidates: List[Dict[str, Any]],
    objectives,
    ctx: RecommendationContext,
    limit: int,
) -> List[Dict[str, Any]]:
    """Score and rank via ``dorian_native.rec_score_and_rank``.

    Built-in objectives are scored rust-side (parallel via rayon).
    User-defined objectives must run python — their scores are
    precomputed here and slotted into the right column so rust
    preserves the user-curated order across the mix.
    """
    if not candidates or not objectives:
        return candidates[:limit]

    import dorian_native  # imported lazily so the module loads under tests

    objective_names = [obj.name for obj in objectives]

    # Precompute python-side scores for user-defined objectives.
    user_defined_scores: Dict[str, List[float]] = {
        obj.name: [obj.score(c, ctx) for c in candidates]
        for obj in objectives
        if isinstance(obj, UserDefinedObjective)
    }

    ctx_json = json.dumps(_ctx_to_rust_payload(ctx))
    candidates_json = json.dumps(candidates, default=str)
    udf_json = json.dumps(user_defined_scores) if user_defined_scores else None

    result_json = dorian_native.rec_score_and_rank(
        ctx_json,
        candidates_json,
        objective_names,
        _RANKING_STRATEGY,
        udf_json,
    )
    ranked_meta = json.loads(result_json)

    by_id = {str(c.get("_id", "")): c for c in candidates}
    ordered: List[Dict[str, Any]] = []
    for entry in ranked_meta:
        cand = by_id.get(entry.get("id", ""))
        if cand is not None:
            ordered.append(cand)
    return ordered[:limit]


def _ctx_to_rust_payload(ctx: RecommendationContext) -> Dict[str, Any]:
    """Shape ``RecommendationContext`` for the rust deserialiser.

    Rust expects ``current_pipeline.nodes`` as a string→object map.
    Frontend payloads sometimes ship ``nodes`` as a list — coerce
    here so deserialisation never fails.
    """
    pipeline = ctx.current_pipeline
    if isinstance(pipeline, dict):
        nodes = pipeline.get("nodes")
        if isinstance(nodes, list):
            pipeline = {**pipeline, "nodes": {str(i): n for i, n in enumerate(nodes)}}

    return {
        "uid": ctx.uid,
        "session": ctx.session,
        "current_pipeline": pipeline,
        "dataset_profile": ctx.dataset_profile,
        "upvoted": list(ctx.upvoted),
        "downvoted": list(ctx.downvoted),
        "selected": list(ctx.selected),
        "suggested": list(ctx.suggested),
        "objective_names": list(ctx.objective_names),
        "task": ctx.task,
    }


# ===================================================================
# Internal — interaction store (Redis)
# ===================================================================
_INTERACTIONS_KEY = "session:{session}:recommendations:interactions"
_EMPTY: Dict[str, List[str]] = {"upvoted": [], "downvoted": [], "selected": [], "suggested": []}


async def _load_interactions(session: str) -> Dict[str, List[str]]:
    raw = await aioredis.get(_INTERACTIONS_KEY.format(session=session))
    if not raw:
        return {**_EMPTY}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {**_EMPTY}


async def _append_interactions(session: str, kind: str, ids: List[str]) -> None:
    data = await _load_interactions(session)
    data.setdefault(kind, []).extend(ids)
    key = _INTERACTIONS_KEY.format(session=session)
    await aioredis.set(key, json.dumps(data))
