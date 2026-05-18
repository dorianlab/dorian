"""ExperimentStore — logical facade over Postgres + in-memory indices.

The Experiment Store is a **concept**, not a single database.  The docstore stores
pipeline documents, Neo4j stores the operator KB, and this module adds:

- **PostgreSQL** for relational data (interactions, evaluations, dataset profiles)
- **In-memory KD-Tree** for dataset similarity (backed by Postgres ``datasets``)
- **In-memory BK-Tree** for pipeline similarity (backed by Postgres ``pipelines``)

The store is initialized once during app lifespan and accessed as a singleton.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import numpy as np

from backend.events import Event, aemit
from dorian.experiment.schema import create_schema
from dorian.experiment.kdtree import (
    DatasetKDTree,
    get_feature_version,
    profile_to_vector,
)
from dorian.experiment.bktree import PipelineBKTree
from dorian.experiment.similarity import extract_operator_names


class ExperimentStore:
    """Singleton experiment store with in-memory similarity indices.

    Coordinates:
    - **KD-Tree** for dataset profile similarity
    - **BK-Tree** for pipeline DAG similarity
    - **Postgres** for evaluations, interactions, and durable profile/pipeline storage
    - **Docstore** (read-only from here) for full pipeline documents
    """

    def __init__(self):
        self._kd_tree: DatasetKDTree | None = None
        self._bk_tree: PipelineBKTree | None = None
        self._initialized = False
        self._bk_ready = asyncio.Event()
        self._bk_load_task: asyncio.Task | None = None
        # Upserts that arrive while the BK-Tree is still warming up are
        # queued here. ``_load_bk_tree_background`` drains the queue after
        # the initial bulk build completes and before publishing the tree.
        # Guarded by ``_bk_pending_lock`` (cheap — never held across slow
        # GED work, only across list mutations).
        self._bk_pending: list[tuple[str, dict]] = []
        self._bk_pending_lock = asyncio.Lock()
        # Watermark for ``refresh_bk_tree_from_db`` — on each call
        # only pipelines newer than this are pulled and added to
        # the tree. Bumped after each successful incremental merge
        # so subsequent calls see only newly-landed rows.
        from datetime import datetime, timezone
        self._bk_sync_watermark: datetime = datetime.fromtimestamp(0, tz=timezone.utc)

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def bk_tree_ready(self) -> bool:
        """True once the BK-Tree background load has completed."""
        return self._bk_ready.is_set()

    async def wait_bk_tree_ready(self) -> None:
        """Await BK-Tree readiness (no-op if already ready)."""
        await self._bk_ready.wait()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create schema and build in-memory indices.

        The KD-Tree (fast, O(N) profile vectorisation) loads inline so
        dataset similarity is available immediately after lifespan startup.

        The BK-Tree load is **deferred to a background task** because it
        runs ``networkx.graph_edit_distance`` (NP-hard) per-insertion ×
        O(log N) comparisons per pipeline, which scales to minutes for a
        few hundred pipelines and would otherwise block the FastAPI
        healthcheck window. Pipeline similarity queries return ``[]``
        until ``bk_tree_ready`` flips to True; downstream callers already
        treat that as "no recommendations yet" without erroring.
        """
        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        await create_schema(pool)

        self._kd_tree = DatasetKDTree()
        # NB: self._bk_tree stays None until the background load finishes.
        # Upserts during the warmup window go into self._bk_pending so
        # they can't race the bulk build or try to invoke GED on the loop.

        await self._kd_tree.load_from_db(pool)

        # Kick off BK-Tree load in the background — do NOT await.
        self._bk_load_task = asyncio.create_task(
            self._load_bk_tree_background(pool),
            name="bk-tree-background-load",
        )

        self._initialized = True
        await aemit(Event("ExperimentStoreInitialized", {
            "datasets": self._kd_tree.size,
            "pipelines": 0,  # BK-Tree still warming
            "bk_tree_warming": True,
        }))

    async def _load_bk_tree_background(self, pool) -> None:
        """Background task: load BK-Tree from Postgres, then signal ready.

        The bulk build runs inside ``load_from_db`` on a worker thread so
        it never touches the event loop. During the build, concurrent
        ``upsert_pipeline`` calls accumulate in ``self._bk_pending``.
        Once the build finishes we publish the tree and drain the backlog
        off-loop.

        Errors are emitted as events but never propagated — pipeline
        similarity stays empty if the load fails, which is the same
        behaviour as a fresh install with no pipelines yet.
        """
        try:
            tree = PipelineBKTree(use_exact_ged=True)
            await tree.load_from_db(pool)

            # Publish the built tree atomically and snapshot pending upserts
            # that arrived during the build. New upserts arriving after this
            # point see ``self._bk_tree`` set and dispatch directly via the
            # off-loop path in ``upsert_pipeline``.
            async with self._bk_pending_lock:
                pending = self._bk_pending
                self._bk_pending = []
                self._bk_tree = tree

            drain_failures: list[tuple[str, str]] = []
            if pending:
                def _drain():
                    for pid, dj in pending:
                        try:
                            tree.add(pid, dj)
                        except Exception as exc:
                            drain_failures.append((pid, repr(exc)))
                await asyncio.to_thread(_drain)
                for pid, err in drain_failures:
                    await aemit(Event("BKTreeDrainFailed", {
                        "pipeline_id": pid,
                        "error": err,
                    }))

            await aemit(Event("BKTreeReady", {
                "pipelines": tree.size,
                "drained": len(pending),
            }))
        except Exception as exc:
            await aemit(Event("BKTreeLoadFailed", {"error": repr(exc)}))
        finally:
            self._bk_ready.set()

    async def refresh_bk_tree_from_db(self) -> int:
        """Pull pipelines inserted since the last call and add them to the tree.

        Designed for processes that share the Postgres ``pipelines``
        table but not the in-memory BK-Tree — the FLAML seeder
        writes new rows while the RL trainer has the tree loaded,
        and the trainer calls this at batch boundaries so the
        priors land in warm-start distance queries without a
        restart. No-op when the BK-Tree isn't ready yet.

        Returns the number of pipelines newly added to the tree.
        """
        if self._bk_tree is None:
            return 0

        from backend.envs import get_pg_pool
        pool = await get_pg_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, dag, created_at, updated_at
                FROM pipelines
                WHERE GREATEST(created_at, updated_at) > $1
                ORDER BY GREATEST(created_at, updated_at) ASC
                """,
                self._bk_sync_watermark,
            )

        if not rows:
            return 0

        added = 0
        for row in rows:
            pid = row["id"]
            dag_raw = row["dag"]
            if isinstance(dag_raw, str):
                dag_raw = json.loads(dag_raw)
            try:
                await asyncio.to_thread(self._bk_tree.add, pid, dag_raw)
                added += 1
            except Exception as exc:
                await aemit(Event("BKTreeRefreshAddFailed", {
                    "pipeline_id": pid,
                    "error": repr(exc),
                }))
        # Advance the watermark past the most recent row we just
        # added. Using the max(created_at, updated_at) we saw
        # guarantees idempotence on subsequent polls — the next
        # call picks up only pipelines modified after this batch.
        latest = max(
            max(r["created_at"], r["updated_at"]) for r in rows
        )
        self._bk_sync_watermark = latest

        await aemit(Event("BKTreeRefreshed", {
            "added": added,
            "watermark": latest.isoformat(),
            "size": self._bk_tree.size,
        }))
        return added

    # ==================================================================
    # Dataset operations
    # ==================================================================

    async def upsert_dataset(
        self, did: str, session: str, profile: dict
    ) -> None:
        """Persist a dataset profile to Postgres and update the KD-Tree.

        Supports partial profiles — the vector is computed from whatever
        metafeatures are available and updated when more arrive.
        """
        from backend.envs import get_pg_pool

        vec = profile_to_vector(profile)
        vec_list = [float(v) if np.isfinite(v) else None for v in vec]
        version = get_feature_version()

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO datasets (id, session, profile, profile_vec, vec_version)
                VALUES ($1, $2, $3::jsonb, $4, $5)
                ON CONFLICT (id) DO UPDATE SET
                    profile = EXCLUDED.profile,
                    profile_vec = EXCLUDED.profile_vec,
                    vec_version = EXCLUDED.vec_version,
                    updated_at = NOW()
                """,
                did,
                session,
                json.dumps(profile),
                vec_list,
                version,
            )

        # Update in-memory index
        if self._kd_tree is not None:
            self._kd_tree.add(did, profile)

        await aemit(Event("DatasetUpserted", {"did": did, "session": session}))

    async def find_similar_datasets(
        self, profile: dict, k: int = 5
    ) -> list[tuple[str, float]]:
        """Find k datasets most similar to the given profile.

        Works with partial profiles — missing features are treated as neutral.

        Returns list of (dataset_id, distance) sorted by ascending distance.
        """
        if self._kd_tree is None or self._kd_tree.size == 0:
            return []
        return self._kd_tree.query(profile, k=k)

    def find_similar_datasets_sync(
        self, profile: dict, k: int = 5
    ) -> list[tuple[str, float]]:
        """Synchronous version for use in recommendation scoring (in-memory only)."""
        if self._kd_tree is None or self._kd_tree.size == 0:
            return []
        return self._kd_tree.query(profile, k=k)

    # ==================================================================
    # Pipeline operations
    # ==================================================================

    async def upsert_pipeline(
        self,
        pipeline_id: str,
        session: str,
        dag_json: dict,
        task: str | None = None,
        provenance: str = "user",
    ) -> None:
        """Persist a pipeline reference to Postgres and update the BK-Tree."""
        from backend.envs import get_pg_pool

        operators = extract_operator_names(dag_json)

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pipelines (id, session, task, dag, operators, provenance)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                ON CONFLICT (id) DO UPDATE SET
                    dag = EXCLUDED.dag,
                    operators = EXCLUDED.operators,
                    task = EXCLUDED.task
                """,
                pipeline_id,
                session,
                task,
                json.dumps(dag_json),
                operators,
                provenance,
            )

        # Update in-memory BK-Tree index. During the warm-up window the
        # tree is still being built on a worker thread — queue the upsert
        # so the background loader can drain it on publish. After publish,
        # dispatch the insert on a worker thread so the GED computation
        # never runs on the event loop. The Postgres row above is the
        # source of truth either way, so a missed in-memory insert is
        # recovered on the next restart.
        tree: PipelineBKTree | None
        async with self._bk_pending_lock:
            tree = self._bk_tree
            if tree is None:
                self._bk_pending.append((pipeline_id, dag_json))
        if tree is not None:
            await asyncio.to_thread(tree.add, pipeline_id, dag_json)

        await aemit(Event("PipelineUpserted", {"pipeline_id": pipeline_id, "session": session, "task": task}))

    async def find_similar_pipelines(
        self, dag_json: dict, max_distance: int = 5
    ) -> list[tuple[str, int]]:
        """Find pipelines within max_distance edits of the query DAG."""
        if self._bk_tree is None or self._bk_tree.size == 0:
            return []
        # GED is NP-hard — never run on the event loop.
        return await asyncio.to_thread(
            self._bk_tree.query, dag_json, max_distance
        )

    async def find_exact_match(self, dag_json: dict) -> str | None:
        """Return the ``pipeline_id`` of an existing DAG with GED 0, else None.

        This is the dedup primitive: two DAGs with graph edit distance 0 are
        structurally identical, so the incoming submission should collapse
        onto the existing row rather than creating a duplicate.

        Returns ``None`` when the BK-Tree isn't ready yet (warmup window) —
        callers must treat that as "unknown" and proceed with insertion.
        Warmup is bounded and rare; the occasional race is acceptable.
        """
        if self._bk_tree is None or self._bk_tree.size == 0:
            return None
        results = await asyncio.to_thread(
            self._bk_tree.query, dag_json, 0
        )
        if not results:
            return None
        # Query sorts by distance ascending — first hit at distance 0 is the
        # canonical match. Any further entries would also be at distance 0
        # (i.e. prior dupes that slipped in before dedup was wired), in
        # which case we still return the first as the surviving canonical.
        return results[0][0]

    # ==================================================================
    # Evaluation operations
    # ==================================================================

    async def record_evaluation(
        self,
        pipeline_id: str,
        dataset_id: str,
        run_id: str,
        metric_name: str,
        metric_value: float,
        eval_config: dict | None = None,
    ) -> None:
        """Record a pipeline execution result (metric) in Postgres."""
        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            # Only insert if both pipeline and dataset exist in Postgres
            exists = await conn.fetchval(
                """
                SELECT EXISTS(SELECT 1 FROM pipelines WHERE id = $1)
                   AND EXISTS(SELECT 1 FROM datasets WHERE id = $2)
                """,
                pipeline_id,
                dataset_id,
            )
            if not exists:
                await aemit(Event("EvaluationRecordSkipped", {
                    "pipeline_id": pipeline_id,
                    "dataset_id": dataset_id,
                    "reason": "pipeline or dataset not in Postgres",
                }))
                return

            await conn.execute(
                """
                INSERT INTO evaluations (pipeline_id, dataset_id, run_id, metric_name, metric_value, eval_config)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                pipeline_id,
                dataset_id,
                run_id,
                metric_name,
                metric_value,
                json.dumps(eval_config) if eval_config else None,
            )

        await aemit(Event("EvaluationRecorded", {
            "pipeline_id": pipeline_id,
            "dataset_id": dataset_id,
            "metric_name": metric_name,
            "metric_value": metric_value,
        }))

    async def record_evaluation_batch(
        self,
        pipeline_id: str,
        dataset_id: str,
        run_id: str,
        metrics: dict[str, float],
        eval_config: dict | None = None,
    ) -> None:
        """Record multiple metrics from a single pipeline evaluation.

        Inserts one row per metric in a single transaction.  This is the
        primary recording path — ``record_evaluation`` is kept for single-
        metric backward compatibility.
        """
        if not metrics:
            return

        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                """
                SELECT EXISTS(SELECT 1 FROM pipelines WHERE id = $1)
                   AND EXISTS(SELECT 1 FROM datasets WHERE id = $2)
                """,
                pipeline_id,
                dataset_id,
            )
            if not exists:
                await aemit(Event("EvaluationRecordSkipped", {
                    "pipeline_id": pipeline_id,
                    "dataset_id": dataset_id,
                    "reason": "pipeline or dataset not in Postgres",
                }))
                return

            config_json = json.dumps(eval_config) if eval_config else None
            async with conn.transaction():
                for metric_name, metric_value in metrics.items():
                    await conn.execute(
                        """
                        INSERT INTO evaluations
                            (pipeline_id, dataset_id, run_id, metric_name, metric_value, eval_config)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                        """,
                        pipeline_id,
                        dataset_id,
                        run_id,
                        metric_name,
                        float(metric_value),
                        config_json,
                    )

        await aemit(Event("EvaluationBatchRecorded", {
            "pipeline_id": pipeline_id,
            "dataset_id": dataset_id,
            "run_id": run_id,
            "metrics": metrics,
        }))

    async def get_evaluations_for_pipeline(
        self, pipeline_id: str
    ) -> list[dict]:
        """Get all evaluation records for a pipeline."""
        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pipeline_id, dataset_id, run_id, metric_name, metric_value, created_at
                FROM evaluations
                WHERE pipeline_id = $1
                ORDER BY created_at DESC
                """,
                pipeline_id,
            )

        return [dict(row) for row in rows]

    async def get_evaluations_for_datasets(
        self, dataset_ids: list[str]
    ) -> list[dict]:
        """Get evaluations for a set of datasets (used by SimilarDataPerformance)."""
        if not dataset_ids:
            return []

        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pipeline_id, dataset_id, metric_name, metric_value
                FROM evaluations
                WHERE dataset_id = ANY($1)
                """,
                dataset_ids,
            )

        return [dict(row) for row in rows]

    # ==================================================================
    # Interaction Table operations
    # ==================================================================

    async def record_interaction(
        self,
        dataset_id: str,
        task: str | None,
        compared_id: str,
        preferred_id: str,
        user_id: str,
        discarded_id: str | None = None,
        eval_id: int | None = None,
        performance: float | None = None,
    ) -> None:
        """Record a pairwise pipeline comparison in the Interaction Table."""
        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            # Verify foreign keys exist before inserting
            pipeline_ids = [compared_id, preferred_id]
            if discarded_id:
                pipeline_ids.append(discarded_id)

            existing = await conn.fetch(
                "SELECT id FROM pipelines WHERE id = ANY($1)",
                pipeline_ids,
            )
            existing_ids = {row["id"] for row in existing}

            # Both compared and preferred must exist
            if compared_id not in existing_ids or preferred_id not in existing_ids:
                await aemit(Event("InteractionRecordSkipped", {
                    "compared_id": compared_id,
                    "preferred_id": preferred_id,
                    "reason": "pipeline(s) not in Postgres",
                }))
                return

            dataset_exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM datasets WHERE id = $1)",
                dataset_id,
            )
            if not dataset_exists:
                await aemit(Event("InteractionRecordSkipped", {
                    "dataset_id": dataset_id,
                    "reason": "dataset not in Postgres",
                }))
                return

            await conn.execute(
                """
                INSERT INTO interactions
                    (dataset_id, task, compared_id, preferred_id, discarded_id,
                     user_id, eval_id, performance)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                dataset_id,
                task,
                compared_id,
                preferred_id,
                discarded_id if discarded_id in existing_ids else None,
                user_id,
                eval_id,
                performance,
            )

        await aemit(Event("InteractionRecorded", {
            "compared_id": compared_id,
            "preferred_id": preferred_id,
            "user_id": user_id,
        }))

    async def get_win_rate(
        self, pipeline_id: str, dataset_id: str | None = None
    ) -> float:
        """Compute the win rate of a pipeline (times preferred / times compared).

        Optionally scoped to a specific dataset.
        """
        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if dataset_id:
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM interactions WHERE (compared_id = $1 OR preferred_id = $1) AND dataset_id = $2",
                    pipeline_id, dataset_id,
                )
                wins = await conn.fetchval(
                    "SELECT COUNT(*) FROM interactions WHERE preferred_id = $1 AND dataset_id = $2",
                    pipeline_id, dataset_id,
                )
            else:
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM interactions WHERE compared_id = $1 OR preferred_id = $1",
                    pipeline_id,
                )
                wins = await conn.fetchval(
                    "SELECT COUNT(*) FROM interactions WHERE preferred_id = $1",
                    pipeline_id,
                )

        return float(wins) / float(total) if total > 0 else 0.0

    async def get_interactions_for_dataset(
        self, dataset_id: str, limit: int = 100
    ) -> list[dict]:
        """Get recent interactions for a dataset."""
        from backend.envs import get_pg_pool

        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM interactions
                WHERE dataset_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                dataset_id,
                limit,
            )

        return [dict(row) for row in rows]

    # ==================================================================
    # Win-rate cache (pre-loaded for sync scoring in objectives)
    # ==================================================================

    async def preload_win_rates(self) -> None:
        """Fetch all pipeline win rates from Postgres into an in-memory cache.

        Called before scoring loops so ``get_win_rate_sync`` can serve results
        without async I/O.  Lightweight — one aggregate query, O(N) where N is
        the number of distinct pipelines in the interactions table.
        """
        from backend.envs import get_pg_pool

        try:
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        p.id,
                        COUNT(*) FILTER (WHERE i.preferred_id = p.id) AS wins,
                        COUNT(*) AS total
                    FROM (
                        SELECT DISTINCT compared_id AS id FROM interactions
                        UNION
                        SELECT DISTINCT preferred_id FROM interactions
                    ) p
                    LEFT JOIN interactions i
                        ON i.compared_id = p.id OR i.preferred_id = p.id
                    GROUP BY p.id
                """)
            self._win_rate_cache = {
                row["id"]: float(row["wins"]) / float(row["total"]) if row["total"] > 0 else 0.0
                for row in rows
            }
        except Exception:
            self._win_rate_cache = {}

    def get_win_rate_sync(
        self, pipeline_id: str, dataset_id: str | None = None
    ) -> float:
        """Return the cached win rate for a pipeline (synchronous).

        Falls back to 0.0 if the cache hasn't been preloaded or the pipeline
        has no interactions.  The optional ``dataset_id`` parameter is accepted
        for API consistency but currently returns the global (unscoped) rate —
        dataset-scoped rates require per-dataset aggregation which can be added
        to ``preload_win_rates`` when needed.
        """
        cache = getattr(self, "_win_rate_cache", None)
        if not cache:
            return 0.0
        return cache.get(pipeline_id, 0.0)

    # ==================================================================
    # Scoring helper (used by recommendation objectives)
    # ==================================================================

    def score_by_similar_datasets(
        self,
        candidate: Dict[str, Any],
        query_profile: dict,
        k: int = 5,
    ) -> float:
        """Score a pipeline candidate by its performance on similar datasets.

        Synchronous — KD-Tree query is in-memory, no I/O.

        1. Find k most similar datasets to the query profile
        2. Weight each dataset's evaluations by 1/(1 + distance)
        3. Return the weighted average metric value for this candidate's operators

        This replaces the SimilarDataPerformance stub in objectives.py.
        """
        if self._kd_tree is None or self._kd_tree.size == 0:
            return 0.0

        similar = self._kd_tree.query(query_profile, k=k)
        if not similar:
            return 0.0

        # Look up candidate's evaluations from the stored data
        # Since this is synchronous, we use pre-cached evaluation data
        # The full async version would query Postgres
        candidate_evals = candidate.get("evaluations") or []
        if not candidate_evals:
            return 0.0

        # Weight scores by dataset similarity
        similar_dids = {did for did, _ in similar}
        distance_map = {did: dist for did, dist in similar}

        weighted_sum = 0.0
        weight_total = 0.0

        for ev in candidate_evals:
            if not isinstance(ev, dict):
                continue
            score = ev.get("score")
            ev_dataset = ev.get("dataset_id", "")

            if score is None or not isinstance(score, (int, float)):
                continue

            if ev_dataset in similar_dids:
                # High weight for evaluations on similar datasets
                w = 1.0 / (1.0 + distance_map.get(ev_dataset, 1.0))
            else:
                # Low weight for evaluations on dissimilar datasets
                w = 0.1

            weighted_sum += float(score) * w
            weight_total += w

        return weighted_sum / weight_total if weight_total > 0 else 0.0


# ==================================================================
# Module-level singleton
# ==================================================================

_store: ExperimentStore | None = None


async def init_experiment_store() -> ExperimentStore:
    """Initialize the global ExperimentStore singleton.

    Called once during app lifespan (main.py).
    """
    global _store
    _store = ExperimentStore()
    try:
        await _store.initialize()
    except Exception as exc:
        await aemit(Event("ExperimentStoreInitFailed", {"error": repr(exc)}))
        # Don't crash the app — the store is optional
    return _store


async def get_experiment_store() -> ExperimentStore:
    """Get the initialized ExperimentStore (async context)."""
    if _store is None:
        raise RuntimeError("ExperimentStore not initialized — call init_experiment_store() first")
    return _store


def get_experiment_store_sync() -> ExperimentStore | None:
    """Get the ExperimentStore if available (synchronous, for scoring).

    Returns None if not initialized — callers should fall back gracefully.
    """
    return _store


async def shutdown_experiment_store() -> None:
    """Clean up the ExperimentStore singleton."""
    global _store
    if _store is not None:
        await aemit(Event("ExperimentStoreShutdown", {}))
    _store = None
