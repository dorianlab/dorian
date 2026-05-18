"""
dorian/experiment/trials.py
----------------------------
Deterministic cross-product trial scheduling.

When a new pipeline or dataset is added to the ExperimentStore, this module
schedules evaluation trials for every missing (pipeline, dataset) combination.
These trials run at BACKGROUND priority and must complete before any RL-based
generation takes place — they provide the cold-start data that bootstraps the
recommendation and scoring engines.

Design:
  - ``schedule_trials_for_new_dataset(dataset_id)`` — evaluates every existing
    pipeline on the new dataset.
  - ``schedule_trials_for_new_pipeline(pipeline_id)`` — evaluates the new
    pipeline on every existing dataset.
  - Both query Postgres for already-evaluated pairs to avoid duplicate work.
  - Trials are submitted via ``submit_background()`` at BACKGROUND priority,
    so user-initiated executions always take precedence.

Integration points:
  - Called from event handlers subscribed to ``DatasetUpserted`` and
    ``PipelineUpserted`` events.
  - The RL generator must wait for all pending cross-product trials to
    complete before starting generation episodes.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from backend.events import Event, aemit

_log = logging.getLogger(__name__)


def _cross_product_disabled() -> bool:
    """Kill-switch for cross-product trial submission.

    Set ``DISABLE_CROSS_PRODUCT_TRIALS=1`` in the backend environment to
    neutralize ``schedule_trials_for_new_dataset`` and
    ``schedule_trials_for_new_pipeline``.  This is a short-term safety net
    for the Dask scheduler SIGSEGV observed under RL-driven fan-out
    (see incident 2026-04-16).  The flag must be removed once the native
    crash is root-caused.
    """
    return os.environ.get("DISABLE_CROSS_PRODUCT_TRIALS", "").strip() == "1"


async def schedule_trials_for_new_dataset(dataset_id: str) -> int:
    """Schedule evaluation of every existing pipeline on a new dataset.

    Returns the number of trials enqueued.
    """
    if _cross_product_disabled():
        await aemit(Event("CrossProductTrialsDisabled", {
            "trigger": "new_dataset", "dataset_id": dataset_id,
            "reason": "DISABLE_CROSS_PRODUCT_TRIALS=1",
        }))
        return 0

    from backend.envs import get_pg_pool
    from backend.queue import submit_background

    pool = await get_pg_pool()

    async with pool.acquire() as conn:
        # Find all pipelines that have NOT been evaluated on this dataset
        rows = await conn.fetch(
            """
            SELECT p.id AS pipeline_id, p.session, p.dag
            FROM pipelines p
            WHERE NOT EXISTS (
                SELECT 1 FROM evaluations e
                WHERE e.pipeline_id = p.id AND e.dataset_id = $1
            )
            """,
            dataset_id,
        )

    if not rows:
        return 0

    enqueued = 0
    for row in rows:
        pipeline_id = row["pipeline_id"]
        session = row["session"]

        try:
            await submit_background(
                uid="system",
                session=session,
                pipeline_id=pipeline_id,
                source="cross_product_trial",
            )
            enqueued += 1
        except Exception as exc:
            await aemit(Event("TrialEnqueueFailed", {
                "pipeline_id": pipeline_id,
                "dataset_id": dataset_id,
                "error": str(exc),
            }))

    await aemit(Event("CrossProductTrialsScheduled", {
        "trigger": "new_dataset",
        "dataset_id": dataset_id,
        "trials_enqueued": enqueued,
        "pipelines_checked": len(rows),
    }))

    return enqueued


async def schedule_trials_for_new_pipeline(pipeline_id: str) -> int:
    """Schedule evaluation of a new pipeline on every existing dataset.

    Returns the number of trials enqueued.
    """
    if _cross_product_disabled():
        await aemit(Event("CrossProductTrialsDisabled", {
            "trigger": "new_pipeline", "pipeline_id": pipeline_id,
            "reason": "DISABLE_CROSS_PRODUCT_TRIALS=1",
        }))
        return 0

    from backend.envs import get_pg_pool
    from backend.queue import submit_background

    pool = await get_pg_pool()

    async with pool.acquire() as conn:
        # Get the pipeline's session for context
        pipeline_row = await conn.fetchrow(
            "SELECT session FROM pipelines WHERE id = $1", pipeline_id,
        )
        if not pipeline_row:
            return 0

        session = pipeline_row["session"]

        # Find all datasets that have NOT been evaluated with this pipeline
        rows = await conn.fetch(
            """
            SELECT d.id AS dataset_id
            FROM datasets d
            WHERE NOT EXISTS (
                SELECT 1 FROM evaluations e
                WHERE e.pipeline_id = $1 AND e.dataset_id = d.id
            )
            """,
            pipeline_id,
        )

    if not rows:
        return 0

    enqueued = 0
    for row in rows:
        dataset_id = row["dataset_id"]

        try:
            await submit_background(
                uid="system",
                session=session,
                pipeline_id=pipeline_id,
                source="cross_product_trial",
            )
            enqueued += 1
        except Exception as exc:
            await aemit(Event("TrialEnqueueFailed", {
                "pipeline_id": pipeline_id,
                "dataset_id": dataset_id,
                "error": str(exc),
            }))

    await aemit(Event("CrossProductTrialsScheduled", {
        "trigger": "new_pipeline",
        "pipeline_id": pipeline_id,
        "trials_enqueued": enqueued,
        "datasets_checked": len(rows),
    }))

    return enqueued


async def get_pending_trial_count() -> int:
    """Return the number of missing (pipeline, dataset) evaluation pairs.

    Used by the RL generator to decide whether to wait for cross-product
    trials to complete before starting generation.
    """
    from backend.envs import get_pg_pool

    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        # Total possible pairs minus already evaluated
        total_pairs = await conn.fetchval(
            """
            SELECT (SELECT COUNT(*) FROM pipelines)
                 * (SELECT COUNT(*) FROM datasets)
            """,
        )
        evaluated_pairs = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT (pipeline_id, dataset_id))
            FROM evaluations
            """,
        )

    return max(0, (total_pairs or 0) - (evaluated_pairs or 0))
