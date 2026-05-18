"""Event handlers for the Experiment Store.

Persist datasets, pipelines, evaluations, and user interactions to Postgres
and keep in-memory indices (KD-Tree, BK-Tree) up to date.

Each handler is a thin async function subscribed to an existing event type.
They never block the critical path — failures are logged and swallowed so
the main user-facing flow continues uninterrupted.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from pathlib import PurePosixPath

from backend.envs import aioredis, expdb
from backend.events import Event, aemit
from dorian.infra.keys import RedisKeys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_session_meta(session: str) -> dict | None:
    """Read session meta from Redis (same pattern as pipeline.py handlers)."""
    from dorian.infra.redis_utils import redis_get_json
    return await redis_get_json(aioredis, RedisKeys.session_meta(session))


def _extract_pipeline_json(meta: dict) -> tuple[str | None, dict | None]:
    """Extract (pipeline_id, pipeline_json_dict) from session meta.

    The pipeline is stored at meta["pipeline"]["pipeline"] as a JSON string
    (from the pipelineHistory format), or directly as a dict.
    """
    pipeline_entry = meta.get("pipeline")
    if not pipeline_entry or not isinstance(pipeline_entry, dict):
        return None, None

    pipeline_id = pipeline_entry.get("id")

    raw_pipeline = pipeline_entry.get("pipeline")
    if raw_pipeline is None:
        return pipeline_id, None

    if isinstance(raw_pipeline, str):
        try:
            return pipeline_id, json.loads(raw_pipeline)
        except (json.JSONDecodeError, TypeError):
            return pipeline_id, None

    if isinstance(raw_pipeline, dict):
        return pipeline_id, raw_pipeline

    return pipeline_id, None


# ---------------------------------------------------------------------------
# 1. DataProfiled → Persist dataset profile + update KD-Tree
# ---------------------------------------------------------------------------

async def handle_dataset_profiled(event: Event) -> None:
    """Persist dataset profile to Postgres and update KD-Tree index.

    Subscribed to: ``DataProfiled``

    The profile dict is already computed by ``check_data()`` in
    ``pipeline_events.py`` and stored in ``session:{session}:meta → dataset.profile``.
    This handler reads it from Redis and persists to Postgres.
    """
    try:
        session = event.data.get("session")
        did = event.data.get("did")
        if not session or not did:
            return

        meta = await _get_session_meta(session)
        if not meta:
            return

        dataset = meta.get("dataset")
        if not isinstance(dataset, dict):
            return

        profile = dataset.get("profile")
        if not profile or not isinstance(profile, dict):
            return

        from dorian.experiment.store import get_experiment_store
        store = await get_experiment_store()
        await store.upsert_dataset(did, session, profile)

        # ── Persist to the docstore for cross-session discovery ──────────────
        try:
            uid = dataset.get("uid") or event.data.get("uid")
            fpath = dataset.get("fpath") or event.data.get("fpath", "")
            filename = PurePosixPath(fpath).name if fpath else did

            # Read feature/target column lists from Redis
            features_raw = await aioredis.get(RedisKeys.dataset_feature_columns(did))
            targets_raw = await aioredis.get(RedisKeys.dataset_target_columns(did))
            features = json.loads(features_raw) if features_raw else None
            targets = json.loads(targets_raw) if targets_raw else None

            now = datetime.now(timezone.utc)
            col = expdb.datasets

            # If this did already resolves to a PUBLIC dataset (OpenML-seeded
            # or otherwise), the user hit the content-hash dedup path in
            # ``/upload`` — we must NOT rewrite ownership / source / name /
            # storage. Only refresh the profile/updatedAt so the KD-Tree stays
            # in sync without clobbering the public doc.
            existing = await col.find_one(
                {"_id": did},
                projection={
                    "isPublic": 1, "source.type": 1, "ownerId": 1,
                },
            )
            is_public_dedup = bool(
                existing and (
                    existing.get("isPublic") is True
                    or (existing.get("source") or {}).get("type") == "openml"
                )
            )

            if is_public_dedup:
                # Profile + updatedAt only. Leave name/source/ownership/storage
                # as the catalogue owns them.
                set_fields: dict = {
                    "profile": profile,
                    "updatedAt": now,
                }
            else:
                set_fields = {
                    "ownerId": uid,
                    "isPublic": False,
                    "name": filename,
                    "dataType": "tabular",
                    "itemCount": int(profile.get("NumberOfInstances", 0)),
                    "source": {
                        "type": "user-upload",
                        "originalId": did,
                        "url": None,
                    },
                    "storage": {
                        "format": "csv",
                        "location": {"type": "local", "path": fpath},
                    },
                    "profile": profile,
                    "features": features,
                    "targets": targets,
                    "updatedAt": now,
                }
            if not is_public_dedup:
                # Thread the upload-time content hash through so future
                # uploads can dedupe against this entry via the
                # ``contentHash`` index. Set at upload in
                # ``dorian/api/routes/file.py::upload_data``.
                content_hash = dataset.get("content_hash")
                if isinstance(content_hash, str) and content_hash:
                    set_fields["contentHash"] = content_hash

                # Description is user-authored at upload time. Only $set it if
                # the upload supplied one — otherwise leave any later user
                # edit alone. Never overwrite a public dataset's description.
                description = dataset.get("description")
                if isinstance(description, str) and description.strip():
                    set_fields["description"] = description.strip()

            await col.update_one(
                {"_id": did},
                {
                    "$set": set_fields,
                    "$setOnInsert": {
                        "createdAt": now,
                        "schemaVersion": 1,
                        "task": None,
                    },
                },
                upsert=True,
            )
            await aemit(Event("DatasetPersistedToDocstore", {"did": did}))
        except Exception:
            await aemit(Event("DatasetPersistenceFailed", {"error": traceback.format_exc()}))

    except Exception:
        await aemit(Event("HandleDatasetProfiledFailed", {"error": traceback.format_exc()}))


# ---------------------------------------------------------------------------
# 2. PipelineSaved → Persist pipeline reference + update BK-Tree
# ---------------------------------------------------------------------------

async def handle_pipeline_saved_to_store(event: Event) -> None:
    """Persist pipeline DAG to Postgres and update BK-Tree index.

    Subscribed to: ``PipelineSaved``

    The pipeline is read from session meta (already saved by
    ``handle_pipeline_saved`` in ``handlers/pipeline.py``).
    """
    try:
        session = event.data.get("session")
        if not session:
            return

        meta = await _get_session_meta(session)
        if not meta:
            return

        pipeline_id, dag_json = _extract_pipeline_json(meta)
        if not pipeline_id or not dag_json:
            await aemit(Event("NoPipelineJsonForExperimentStore", {"session": session}))
            return

        # Get the task from session meta
        selected_task = meta.get("selectedDataScienceTask")
        task = selected_task.get("name") if isinstance(selected_task, dict) else None

        from dorian.experiment.store import get_experiment_store
        store = await get_experiment_store()
        await store.upsert_pipeline(pipeline_id, session, dag_json, task, "user")

    except Exception:
        await aemit(Event("HandlePipelineSavedToStoreFailed", {"error": traceback.format_exc()}))


# ---------------------------------------------------------------------------
# 3. PipelineRunCompleted / PipelineRunFailed → Record evaluation
# ---------------------------------------------------------------------------

async def handle_run_completed(event: Event) -> None:
    """Record ALL pipeline evaluation metrics in Postgres.

    Subscribed to: ``PipelineRunCompleted``, ``PipelineRunFailed``

    Event data (from execution.py ``run_pipeline``):
    - ``run_id``: str
    - ``uid``: str
    - ``session``: str
    - ``status``: str
    - ``summary``: dict (from PipelineExecution.summary())
    - ``metrics``: dict[str, float]  ← authoritative source from evaluation
    """
    try:
        run_id = event.data.get("run_id")
        session = event.data.get("session")
        status = event.data.get("status", "")

        if not run_id or not session:
            return

        # Only record evaluations for completed (not failed) runs
        if "failed" in status.lower():
            await aemit(Event("EvaluationRecordingSkipped", {"run_id": run_id, "reason": "failed_run"}))
            return

        # ── Collect ALL metrics ──
        # The authoritative source is the "metrics" key emitted directly
        # by run_pipeline (from evaluation DAG execution).  Fall back to
        # the summary blob for backward compat.
        metrics: dict[str, float] = {}

        # 1. Authoritative: direct metrics dict from evaluation
        raw_metrics = event.data.get("metrics")
        if isinstance(raw_metrics, dict):
            for k, v in raw_metrics.items():
                if isinstance(v, (int, float)):
                    metrics[k] = float(v)

        # 2. Fallback: dig into summary
        if not metrics:
            summary = event.data.get("summary", {})
            if isinstance(summary, dict):
                # Try nested "metrics" dict first
                nested = summary.get("metrics")
                if isinstance(nested, dict):
                    for k, v in nested.items():
                        if isinstance(v, (int, float)):
                            metrics[k] = float(v)

                # Try well-known top-level keys
                if not metrics:
                    for key in ("accuracy", "score", "f1", "auc", "rmse", "mse"):
                        if key in summary and isinstance(summary[key], (int, float)):
                            metrics[key] = float(summary[key])

        if not metrics:
            await aemit(Event("NoMetricInRunSummary", {"run_id": run_id}))
            return

        # ── Resolve pipeline_id and dataset_id ──
        # Prefer the authoritative ``pipeline_id`` from the event payload —
        # forwarded by ``run_pipeline`` — and fall back to session meta
        # only for legacy user sessions that don't yet carry it.  RL
        # generator sessions (``rl:round-N:{did}``) never populate
        # ``meta['pipeline']`` so meta-based lookup silently dropped
        # every evaluation on the floor.
        pipeline_id = event.data.get("pipeline_id")
        meta = await _get_session_meta(session)
        if not pipeline_id:
            if not meta:
                await aemit(Event("EvaluationRecordingSkipped", {
                    "run_id": run_id, "session": session,
                    "reason": "no_pipeline_id_and_no_session_meta",
                }))
                return
            pipeline_id, _ = _extract_pipeline_json(meta)
            if not pipeline_id:
                await aemit(Event("EvaluationRecordingSkipped", {
                    "run_id": run_id, "session": session,
                    "reason": "no_pipeline_id_in_meta",
                }))
                return

        # Prefer dataset_id from event payload (forwarded by run_pipeline for
        # RL generator sessions); fall back to session meta for user sessions.
        dataset_id: str | None = event.data.get("dataset_id")
        if not dataset_id and isinstance(meta, dict):
            dataset = meta.get("dataset")
            if isinstance(dataset, dict):
                dataset_id = dataset.get("did")
        if not dataset_id:
            await aemit(Event("EvaluationRecordingSkipped", {
                "run_id": run_id, "session": session,
                "pipeline_id": pipeline_id,
                "reason": "no_dataset_id",
            }))
            return

        from dorian.experiment.store import get_experiment_store
        store = await get_experiment_store()
        await store.record_evaluation_batch(
            pipeline_id, dataset_id, run_id, metrics,
        )
        await aemit(Event("EvaluationBatchRecorded", {
            "run_id": run_id, "pipeline_id": pipeline_id,
            "dataset_id": dataset_id, "metrics": list(metrics.keys()),
        }))

    except Exception:
        await aemit(Event("HandleRunCompletedFailed", {"error": traceback.format_exc()}))


# ---------------------------------------------------------------------------
# 4. Recommendation interactions → Interaction Table
# ---------------------------------------------------------------------------

async def handle_recommendation_interaction_to_store(event: Event) -> None:
    """Record pairwise interaction in the Interaction Table.

    Subscribed to: ``PipelineRecommendationSelected``,
    ``PipelineRecommendationUpvoted``, ``PipelineRecommendationDownvoted``

    When a user selects/upvotes pipeline A:
    - A is 'preferred', the other shown candidates are 'compared'

    When a user downvotes pipeline A:
    - A is 'discarded' (stored as compared, with null preferred)
    """
    try:
        uid = event.data.get("uid")
        session = event.data.get("session")
        pipeline_id = (
            event.data.get("pipelineId")
            or event.data.get("payload", {}).get("pipelineId")
        )
        event_name = event.type if hasattr(event, "type") else event.data.get("_event_type", "")

        if not uid or not session or not pipeline_id:
            return

        # Get dataset_id and task from session meta
        meta = await _get_session_meta(session)
        if not meta:
            return

        dataset = meta.get("dataset")
        dataset_id = dataset.get("did") if isinstance(dataset, dict) else None
        if not dataset_id:
            return

        selected_task = meta.get("selectedDataScienceTask")
        task = selected_task.get("name") if isinstance(selected_task, dict) else None

        from dorian.experiment.store import get_experiment_store
        store = await get_experiment_store()

        if "Downvoted" in event_name:
            # Downvote: record pipeline as discarded (compared but not preferred)
            # Use pipeline_id as both compared and discarded, with a dummy preferred
            await aemit(Event("RecordingDownvote", {"pipeline_id": pipeline_id}))
            # We can't record without a preferred_id, so we store it differently:
            # compared_id = pipeline_id (the one being compared)
            # preferred_id = pipeline_id (self, indicating "not this one")
            # discarded_id = pipeline_id
            # This is a special case — queries should handle it
            await store.record_interaction(
                dataset_id=dataset_id,
                task=task,
                compared_id=pipeline_id,
                preferred_id=pipeline_id,
                user_id=uid,
                discarded_id=pipeline_id,
            )
        else:
            # Selected or Upvoted: pipeline_id is preferred
            # We don't have the full list of alternatives in the event data,
            # so we record a single preference event.
            # The recommendation engine tracks suggested IDs separately.
            await store.record_interaction(
                dataset_id=dataset_id,
                task=task,
                compared_id=pipeline_id,
                preferred_id=pipeline_id,
                user_id=uid,
            )

    except Exception:
        await aemit(Event("HandleRecommendationInteractionFailed", {"error": traceback.format_exc()}))


# ---------------------------------------------------------------------------
# 5. Cross-product trial scheduling
# ---------------------------------------------------------------------------

async def handle_dataset_upserted_trials(event: Event) -> None:
    """Schedule cross-product trials when a new dataset enters the store.

    Subscribed to: ``DatasetUpserted``

    Evaluates every existing pipeline on the new dataset at BACKGROUND
    priority.  These trials must complete before RL generation starts.
    """
    try:
        did = event.data.get("did")
        if not did:
            return

        from dorian.experiment.trials import schedule_trials_for_new_dataset
        enqueued = await schedule_trials_for_new_dataset(did)
        if enqueued:
            await aemit(Event("CrossProductTrialsQueued", {
                "trigger": "DatasetUpserted",
                "dataset_id": did,
                "count": enqueued,
            }))
    except Exception:
        await aemit(Event("CrossProductTrialSchedulingFailed", {
            "trigger": "DatasetUpserted",
            "error": traceback.format_exc(),
        }))


async def handle_pipeline_upserted_trials(event: Event) -> None:
    """Schedule cross-product trials when a new pipeline enters the store.

    Subscribed to: ``PipelineUpserted``

    Evaluates the new pipeline on every existing dataset at BACKGROUND
    priority.  These trials must complete before RL generation starts.
    """
    try:
        pipeline_id = event.data.get("pipeline_id")
        if not pipeline_id:
            return

        from dorian.experiment.trials import schedule_trials_for_new_pipeline
        enqueued = await schedule_trials_for_new_pipeline(pipeline_id)
        if enqueued:
            await aemit(Event("CrossProductTrialsQueued", {
                "trigger": "PipelineUpserted",
                "pipeline_id": pipeline_id,
                "count": enqueued,
            }))
    except Exception:
        await aemit(Event("CrossProductTrialSchedulingFailed", {
            "trigger": "PipelineUpserted",
            "error": traceback.format_exc(),
        }))
