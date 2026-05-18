"""Pipeline generation executor — persists generated DAGs and executes them.

Flow:
  1. GenerationEngine produces a DAG via PipelineGenEnv
  2. Executor saves the DAG to the docstore (``pipelines`` collection)
  3. Executor upserts the pipeline into ExperimentStore (Postgres + BK-Tree)
  4. Executor either:
     a. **Standalone mode** (CLI script): executes directly via
        ``handle_pipeline_execution`` — no queue, no bridge_logic needed.
     b. **Deployed mode** (inside FastAPI): submits to the Redis priority
        queue, consumed by ``bridge_logic`` with backpressure.
  5. Existing event handlers record evaluation metrics → leaderboard updates

The executor is fully async and should be called from the generation scheduler's
event loop.
"""
from __future__ import annotations

import json
import logging
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backend.events import Event, aemit


async def _ensure_synthetic_session_dataset(
    session: str, dataset_id: str
) -> str | None:
    """Seed Redis ``session:{session}:meta`` with a minimal dataset binding.

    The RL generation scheduler submits pipelines under synthetic sessions
    (``rl:round-N:{did}``) that never go through the normal upload flow, so
    ``session:{session}:meta`` is empty in Redis. ``expand_dataset_refs``
    reads fpath from that meta — without seeding, every ``dorian.io.dataset``
    node fails the post-expansion guard with "please upload a dataset".

    This helper reads the docstore dataset document, resolves the storage
    path (supporting both absolute and OpenML-style relative paths), and
    writes a minimal ``meta`` + supporting Redis keys so the expansion and
    state-resolver machinery work transparently against synthetic sessions.
    Returns the resolved fpath or ``None`` if the dataset can't be located.
    """
    try:
        from backend.envs import aioredis, expdb
        from backend.config import config
        from dorian.infra.keys import RedisKeys

        # Datasets are keyed by TEXT id in the Postgres document store.
        doc = await expdb.datasets.find_one({"_id": dataset_id})
        if not doc:
            return None

        raw_path = (
            (doc.get("storage") or {}).get("location", {}).get("path", "")
        )
        if not raw_path:
            return None

        fpath_obj = Path(raw_path)
        if not fpath_obj.is_absolute():
            # ``populate.py`` writes CSVs under ``Path(config.fs.data) / datasets/...``
            # — mirror that resolution here. ``config.fs.data`` is the canonical
            # data root (e.g. ``/app/data`` in Docker); there is no ``data_dir`` key.
            data_dir = Path(config.fs.data)
            fpath_obj = data_dir / fpath_obj
        if not fpath_obj.exists():
            return None
        fpath = str(fpath_obj)

        columns = doc.get("columns") or {}
        features = doc.get("features") or columns.get("features")
        targets = doc.get("targets") or columns.get("targets")

        dataset_meta = {
            "did": dataset_id,
            "fpath": fpath,
            "uid": "system",
            "mime": "text/csv",
        }
        profile = doc.get("profile")
        if profile:
            dataset_meta["profile"] = profile
        # Embed features/target directly into dataset meta so the
        # ``DATASET_EXPANSION_RULE`` can inject an X/y split snippet without
        # extra Redis round-trips. ``targets`` may be a list with a single
        # column (from the profiler), so collapse it to a bare name when
        # possible for the snippet's kwarg.
        if features:
            dataset_meta["features"] = list(features)
        if targets:
            if isinstance(targets, (list, tuple)):
                dataset_meta["target"] = targets[0] if targets else ""
                dataset_meta["targets"] = list(targets)
            else:
                dataset_meta["target"] = targets

        raw_meta = await aioredis.get(RedisKeys.session_meta(session))
        meta = json.loads(raw_meta) if raw_meta else {"uid": "system", "session": session}
        meta["dataset"] = dataset_meta
        if isinstance(doc.get("task"), dict):
            task_type = doc["task"].get("type")
            if task_type:
                meta.setdefault(
                    "selectedDataScienceTask",
                    {"id": None, "name": str(task_type).capitalize()},
                )

        # 24h TTL: synthetic sessions are short-lived; garbage-collect on their own.
        await aioredis.set(
            RedisKeys.session_meta(session), json.dumps(meta), ex=86400,
        )
        await aioredis.set(RedisKeys.dataset_fpath(dataset_id), fpath, ex=86400)
        if features:
            await aioredis.set(
                RedisKeys.dataset_feature_columns(dataset_id),
                json.dumps(features), ex=86400,
            )
        if targets:
            await aioredis.set(
                RedisKeys.dataset_target_columns(dataset_id),
                json.dumps(targets), ex=86400,
            )
        return fpath
    except Exception:
        await aemit(Event("SyntheticSessionSeedFailed", {
            "session": session, "dataset_id": dataset_id,
            "error": traceback.format_exc(),
        }))
        return None

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Execution mode — standalone (direct) vs deployed (queue)
# ---------------------------------------------------------------------------

_standalone_mode: bool = False


def set_standalone_mode(enabled: bool = True) -> None:
    """Switch between standalone and deployed execution modes.

    Call ``set_standalone_mode(True)`` from CLI scripts (e.g.
    ``generate_pipelines.py``) that run outside the FastAPI server.
    In standalone mode, pipelines are executed directly via
    ``handle_pipeline_execution`` instead of being submitted to the
    Redis queue (which has no consumer outside the server process).

    The default is deployed mode (queue-based), which is correct when
    the generation scheduler runs inside the FastAPI lifespan.
    """
    global _standalone_mode
    _standalone_mode = enabled
    _log.info("Executor mode: %s", "standalone (direct execution)" if enabled else "deployed (queue-based)")


async def persist_generation_errors(
    errors: list[dict],
    *,
    dataset_id: str,
    task: str | None = None,
    session: str = "",
    source: str = "rl_generator",
) -> None:
    """Persist RL generation errors to the docstore for analysis and mitigation.

    Errors are first-class citizens — they inform future operator casting,
    constraint relaxation, and debugging.  Each error document includes the
    full context (dataset, task, session, episode, step, type, detail).

    Parameters
    ----------
    errors : list[dict]
        Error dicts accumulated during generation (from PipelineGenEnv / engine).
    dataset_id : str
        The dataset the generation was targeting.
    task : str or None
        Data science task.
    session : str
        Synthetic session identifier.
    source : str
        Provenance tag.
    """
    if not errors:
        return

    try:
        from backend.envs import expdb

        now = datetime.now(timezone.utc)
        doc = {
            "dataset_id": dataset_id,
            "task": task,
            "session": session,
            "source": source,
            "errors": errors,
            "error_count": len(errors),
            "createdAt": now,
        }
        await expdb.generation_errors.insert_one(doc)
        _log.debug(
            "Persisted %d generation errors for dataset %s.",
            len(errors), dataset_id,
        )
    except Exception:
        # Log but don't propagate — error persistence is best-effort
        _log.warning(
            "Failed to persist generation errors: %s",
            traceback.format_exc(),
        )


async def persist_and_submit(
    dag,
    *,
    dataset_id: str,
    task: str | None = None,
    session: str = "",
    source: str = "rl_generator",
) -> str | None:
    """Persist a generated DAG and submit it for background execution.

    Returns the pipeline_id on success, None on failure.

    Parameters
    ----------
    dag : dorian.dag.DAG
        Completed pipeline DAG from PipelineGenEnv.
    dataset_id : str
        docstore _id of the dataset to evaluate the pipeline against.
    task : str or None
        Data science task (e.g. "Classification").
    session : str
        Synthetic session identifier for grouping generated pipelines.
    source : str
        Provenance tag stored in Postgres.
    """
    pipeline_id = uuid4().hex
    doc_id: str | None = None

    try:
        from backend.envs import expdb

        dag_dict = dag.to_json_dict()
        now = datetime.now(timezone.utc)
        exec_session = session or f"rl:{source}"

        # ── 1. DAG-based dedup via BK-Tree (GED = 0) ───────────────────
        #
        # The BK-Tree is the source of truth for pipeline structural
        # identity — two DAGs with graph edit distance 0 are the same
        # pipeline, regardless of the fresh uuid4 we just minted.  If
        # an exact match exists, collapse onto it: skip docstore insert,
        # skip Postgres upsert, skip BK-Tree add, and reuse the existing
        # pipeline_id for downstream execution so the dataset crossing
        # still happens (dedup on identity, not on evaluation).
        #
        # During BK-Tree warmup ``find_exact_match`` returns None, which
        # we treat as "unknown" and fall through to insertion.  The
        # warmup window is short and bounded, so the occasional race is
        # acceptable — a later dedup pass would collapse duplicates.
        try:
            from dorian.experiment.store import get_experiment_store
            store = await get_experiment_store()
            existing_id = await store.find_exact_match(dag_dict)
        except Exception:
            existing_id = None
            await aemit(Event("PipelineDedupLookupFailed", {
                "error": traceback.format_exc(),
            }))

        if existing_id is not None:
            await aemit(Event("GeneratedPipelineDeduplicated", {
                "existing_pipeline_id": existing_id,
                "discarded_pipeline_id": pipeline_id,
                "dataset_id": dataset_id,
                "source": source,
            }))
            pipeline_id = existing_id
            # Skip persistence — the canonical row already exists.  Fall
            # through to the submission block so the (existing pipeline,
            # new dataset) pair still runs an evaluation.
        else:
            # ── 2. Persist to the docstore ──────────────────────────────────
            doc = {
                "pipeline_id": pipeline_id,
                "nodes": dag_dict["nodes"],
                "edges": dag_dict["edges"],
                "provenance": source,
                "task": task,
                "dataset_id": dataset_id,
                "createdAt": now,
            }

            result = await expdb.pipelines.insert_one(doc)
            doc_id = str(result.inserted_id)

            _log.debug("Pipeline %s persisted to docstore (oid=%s).", pipeline_id, doc_id)

            # ── 3. Upsert into ExperimentStore (Postgres + BK-Tree) ───
            try:
                await store.upsert_pipeline(
                    pipeline_id=pipeline_id,
                    session=exec_session,
                    dag_json=dag_dict,
                    task=task,
                    provenance=source,
                )
            except Exception:
                # Non-fatal — pipeline is already in the docstore, just missing from indices.
                await aemit(Event("GeneratedPipelineIndexFailed", {
                    "pipeline_id": pipeline_id,
                    "error": traceback.format_exc(),
                }))

        # ── 4. Execute or enqueue ──────────────────────────────────────
        # Synthetic sessions never see the upload path, so the runtime has
        # no way to resolve ``dorian.io.dataset`` during expansion. Seed the
        # session's Redis meta from the docstore dataset document once per
        # submission — cheap, idempotent, and scoped with a 24h TTL.
        seeded_fpath = await _ensure_synthetic_session_dataset(
            exec_session, dataset_id
        )
        if seeded_fpath is None:
            await aemit(Event("GeneratedPipelineSubmitSkipped", {
                "pipeline_id": pipeline_id,
                "doc_id": doc_id,
                "dataset_id": dataset_id,
                "reason": "dataset file not resolvable for synthetic session",
            }))
            return pipeline_id

        if _standalone_mode:
            # Standalone mode (CLI script): execute directly via
            # handle_pipeline_execution.  The Redis queue bridge only runs
            # inside the FastAPI server — standalone scripts have no consumer.
            from dorian.pipeline.execution import handle_pipeline_execution

            exec_payload = {
                "uid": "system",
                "session": exec_session,
                # Pass the LOGICAL pipeline_id — Postgres is the source of
                # truth for pipeline identity, not the docstore ObjectId.
                # ``handle_pipeline_execution`` probes Postgres first and
                # falls back to the docstore for legacy ObjectId payloads.
                "pipelineId": pipeline_id,
                "datasetId": dataset_id,
                "_source": source,
            }

            try:
                await handle_pipeline_execution(exec_payload)
            except Exception:
                # Non-fatal — pipeline is persisted, execution failed
                await aemit(Event("GeneratedPipelineExecutionFailed", {
                    "pipeline_id": pipeline_id,
                    "doc_id": doc_id,
                    "error": traceback.format_exc(),
                }))
        else:
            # Deployed mode (inside FastAPI process): submit to Redis queue.
            # bridge_logic consumes the queue and executes in the same process,
            # respecting backpressure and Dask cluster capacity.
            from backend.queue import submit_background

            await submit_background(
                uid="system",
                session=exec_session,
                pipeline_id=pipeline_id,
                dataset_id=dataset_id,
                source=source,
            )

        await aemit(Event("GeneratedPipelineSubmitted", {
            "pipeline_id": pipeline_id,
            "doc_id": doc_id,
            "dataset_id": dataset_id,
            "source": source,
        }))

        return pipeline_id

    except Exception:
        await aemit(Event("GeneratedPipelineSubmitFailed", {
            "pipeline_id": pipeline_id,
            "error": traceback.format_exc(),
        }))
        return None
