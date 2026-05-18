"""Persist valid RL pipelines to the document store + Postgres so
leaderboards pick them up.

Before this module the v2 trainer only wrote valid pipelines to the
pyo3 ``ExperimentGraph`` (cache-affinity index). User-facing leaderboards
pull from ``doc_pipelines`` + Postgres ``evaluations``, so v2
pipelines were invisible in the UI.

Two writes per valid episode:

1. ``doc_pipelines`` — one doc per unique pipeline class hash.
   Schema mirrors v1's shape (``pipeline_id``, ``nodes``, ``edges``,
   ``provenance``, ``task``, ``dataset_id``) so downstream leaderboard
   code works without changes.

2. Postgres ``evaluations`` — one row per (pipeline, dataset, run)
   with the episode's terminal reward as ``metric_value``. This is
   the leaderboard's actual ranking source.

Both writes are idempotent: re-committing the same class hash on the
same dataset replaces the eval row if the new reward is higher.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistent commit loop
# ---------------------------------------------------------------------------
# Running ``asyncio.run(commit_rl_pipeline(...))`` inline from the RL
# trainer's rollout loop creates and tears down a fresh event loop on
# every valid episode. That breaks everything the ExperimentStore
# singleton holds that's loop-bound: ``asyncio.Event`` / ``asyncio.Lock``
# (from ``__init__``), the asyncpg pool (from ``get_pg_pool``), and
# background tasks created on the first loop (BK-Tree warm-up). The
# second call sees "connection was closed in the middle of operation"
# and similar "loop is closed" tracebacks.
#
# Fix: a single daemon thread runs one long-lived event loop and every
# commit is scheduled onto it via ``run_coroutine_threadsafe``. The
# ExperimentStore initialises once on that loop and stays valid for
# the lifetime of the trainer process.

_commit_loop: asyncio.AbstractEventLoop | None = None
_commit_thread: threading.Thread | None = None
_commit_lock = threading.Lock()


def _get_commit_loop() -> asyncio.AbstractEventLoop:
    """Return the persistent commit loop, starting the worker thread on first use."""
    global _commit_loop, _commit_thread
    with _commit_lock:
        if _commit_loop is not None and not _commit_loop.is_closed():
            return _commit_loop
        _commit_loop = asyncio.new_event_loop()
        _commit_thread = threading.Thread(
            target=_commit_loop.run_forever,
            daemon=True,
            name="rl-commit-loop",
        )
        _commit_thread.start()
    return _commit_loop


async def _resolve_dataset_uuid_by_name(dataset_name: str) -> str | None:
    """Look up the ``datasets.id`` UUID for a dataset passed by
    human-friendly name (``"credit-g"``, ``"kr-vs-kp"``).

    The RL trainer carries names end-to-end because they're what
    the user interacts with. The ``evaluations`` table FK's to the
    UUID ``datasets.id`` column, so every insert needs this
    translation. Cross-references via ``doc_datasets`` which
    stores the ``name`` field. Each document collection has its
    own ``doc_<name>`` Postgres table — see
    ``backend/db/pg_docstore.py``.
    """
    import asyncpg
    from backend.config import config as _cfg

    pg = _cfg.postgresql
    conn = await asyncpg.connect(
        host=pg.host,
        port=int(pg.port),
        user="dorian",
        password=pg.password,
        database="dorian",
    )
    try:
        row = await conn.fetchrow(
            """
            SELECT p.id
            FROM doc_datasets p
            WHERE (p.data->>'name') = $1
            LIMIT 1
            """,
            dataset_name,
        )
        if row is None:
            return None
        # Verify the ``datasets`` row exists — the FK expects it.
        exists = await conn.fetchval(
            "SELECT 1 FROM datasets WHERE id = $1",
            row["id"],
        )
        return row["id"] if exists else None
    finally:
        await conn.close()


async def _upsert_doc_store_pipeline(pipeline_id: str, doc: dict) -> None:
    """Upsert a pipeline into ``doc_pipelines`` directly via asyncpg.

    Bypasses the ``backend.db.pg_docstore`` facade because its
    shared pool is tied to the FastAPI loop; this function is
    called from the RL trainer's persistent commit loop, where the
    facade pool raises ``pool is closed`` on acquire. Uses a
    short-lived connection so no cross-loop pool state is
    involved.

    Target table: ``doc_pipelines`` (one ``doc_<name>`` table per
    document collection; see ``backend/db/pg_docstore.py``).
    Columns: ``id / data / created_at / updated_at``; primary key
    is ``id``.
    """
    import asyncpg
    from backend.config import config

    pg = config.postgresql
    conn = await asyncpg.connect(
        host=pg.host,
        port=int(pg.port),
        user="dorian",
        password=pg.password,
        database="dorian",
    )
    try:
        await conn.execute(
            """
            INSERT INTO doc_pipelines (id, data, created_at, updated_at)
            VALUES ($1, $2::jsonb, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET
                data       = EXCLUDED.data,
                updated_at = NOW()
            """,
            pipeline_id,
            json.dumps(doc),
        )
    finally:
        await conn.close()


def _safe_expdb():
    """Return the ``expdb`` document-store handle, or None if unreachable.

    Every ``expdb.<collection>`` call lands on a Postgres ``doc_<name>``
    JSONB table via the facade in ``backend.db.pg_docstore``. A
    historical bug where this helper silently returned None caused
    ``commit_rl_pipeline`` to short-circuit on line 2, dropping
    every valid RL pipeline (30+ per session) before the
    ExperimentStore / Postgres-pipelines branch could run — the UI
    leaderboard kept showing only the 500 trial-config seed
    pipelines as a result.
    """
    try:
        from backend.envs import expdb
        return expdb
    except Exception as exc:
        _log.warning("expdb unavailable: %s", exc)
        return None


async def commit_rl_pipeline(
    *,
    dag_json: str,
    class_hash: str,
    dataset_id: str,
    terminal_reward: float,
    wall_clock_secs: float,
    policy_kind: str,
    episode: int,
) -> str | None:
    """Write a valid RL-generated pipeline to the document store + Postgres.

    Returns the ``pipeline_id`` on success, ``None`` on failure.

    Identity is derived from ``canonical_instance_hash`` — the
    **value-sensitive** hash — NOT ``class_hash`` / structural hash.
    Two pipelines with the same operators but different
    hyperparameters (``C=0.5`` vs ``C=1.0``) earn separate rows;
    class_hash would collapse them and is reserved for rewrite-rule
    matching where value insensitivity is deliberate.
    """
    expdb = _safe_expdb()
    now = datetime.now(timezone.utc)

    try:
        dag_raw = json.loads(dag_json)
    except (ValueError, TypeError):
        _log.warning("invalid dag_json for class %s", class_hash)
        return None

    # Reconstruct a DAG object so we can compute the instance hash.
    # ``canonical_instance_hash`` needs the dataclass form, not raw
    # JSON, because it uses ``isinstance`` dispatch to pick per-node
    # fingerprint logic.
    try:
        from dorian.dag import DAG as _DAG, Operator as _Op, Parameter as _P, Snippet as _Sn, Edge as _E
        nodes = {}
        for nid, ndata in (dag_raw.get("nodes") or {}).items():
            t = (ndata.get("class_type") or ndata.get("type") or "").lower()
            if t == "operator":
                nodes[nid] = _Op(
                    name=ndata.get("name", ""),
                    language=ndata.get("language", "python"),
                    tasks=ndata.get("tasks") or [],
                )
            elif t == "parameter":
                nodes[nid] = _P(
                    name=ndata.get("name", ""),
                    dtype=ndata.get("dtype", "string"),
                    value=str(ndata.get("value", "")),
                )
            elif t == "snippet":
                nodes[nid] = _Sn(
                    name=ndata.get("name", ""),
                    code=ndata.get("code", ""),
                    language=ndata.get("language", "python"),
                )
        edges = []
        for edata in (dag_raw.get("edges") or []):
            edges.append(_E(
                source=edata["source"],
                destination=edata["destination"],
                position=edata.get("position", 0),
                output=edata.get("output", 0),
            ))
        reconstructed = _DAG(nodes=nodes, edges=edges)
        from dorian.pipeline.canonical import canonical_instance_hash
        instance_hash = canonical_instance_hash(reconstructed)
    except Exception:
        _log.exception("instance_hash computation failed; falling back to class_hash")
        instance_hash = class_hash

    pipeline_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"rl-v2/{instance_hash}")
    )
    dag = dag_raw

    # ── Document-store ``doc_pipelines`` upsert ──────────────────────
    # Writes directly via a fresh asyncpg connection — the ``expdb``
    # facade's shared pool is bound to whichever loop first awaited
    # it (typically the backend's FastAPI loop), and the RL trainer's
    # persistent commit loop is a different loop, so every call
    # through ``expdb.pipelines.update_one`` fails with
    # ``asyncpg.InterfaceError: pool is closed``. A per-call
    # connection sidesteps that entirely; the write target is the
    # same ``doc_pipelines`` row the facade would have upserted.
    #
    # Keeps the RL-v2 row discoverable by the frontend's pipelines
    # list, which reads from the document-store view.
    doc = {
        "_id": pipeline_id,
        "pipeline_id": pipeline_id,
        "class_hash": class_hash,
        "nodes": dag.get("nodes", {}),
        "edges": dag.get("edges", []),
        "task": "classification",
        "dataset_id": dataset_id,
        "provenance": {
            "source": "rl-v2",
            "policy": policy_kind,
            "last_episode": episode,
            "class_hash": class_hash,
        },
        "updatedAt": now.isoformat(),
        "createdAt": now.isoformat(),
        "source": "rl-v2",
        "bestReward": float(terminal_reward),
    }
    try:
        await _upsert_doc_store_pipeline(pipeline_id, doc)
    except Exception:  # pragma: no cover
        _log.exception("document-store pipelines upsert failed")
        # Fall through — the Postgres-relational write below is
        # what actually backs the leaderboard ranking.

    # ── Postgres ``pipelines`` + ``evaluations`` ──────────────────────
    # ExperimentStore owns both tables. ``record_evaluation`` skips
    # the insert if the pipeline or dataset is missing; we upsert
    # the pipeline first so that precondition always holds.
    #
    # The RL trainer is a separate process from the FastAPI backend
    # that normally initialises the store during lifespan, so we
    # lazy-init it here. Repeat calls short-circuit on the singleton.
    try:
        from dorian.experiment.store import (
            get_experiment_store,
            init_experiment_store,
        )
        try:
            store = await get_experiment_store()
        except RuntimeError:
            store = await init_experiment_store()
        await store.upsert_pipeline(
            pipeline_id=pipeline_id,
            session=f"rl-v2:{policy_kind}",
            dag_json=dag,
            task="classification",
            provenance="rl-v2",
        )
        # Resolve the dataset NAME (what the trainer carries) to
        # the ``datasets.id`` UUID that ``evaluations`` FK's to.
        # ``record_evaluation`` checks the dataset exists before
        # inserting; passing a name directly makes every insert a
        # silent skip (``EvaluationRecordSkipped`` event).
        dataset_uuid = await _resolve_dataset_uuid_by_name(dataset_id)
        if dataset_uuid is None:
            _log.warning(
                "rl-v2: dataset %r not in datasets table — "
                "skipping evaluation insert",
                dataset_id,
            )
        else:
            await store.record_evaluation(
                pipeline_id=pipeline_id,
                dataset_id=dataset_uuid,
                run_id=str(uuid.uuid4()),
                metric_name="terminal_reward",
                metric_value=float(terminal_reward),
                eval_config={
                    "source": "rl-v2",
                    "policy": policy_kind,
                    "dataset_name": dataset_id,
                    "wall_clock_secs": wall_clock_secs,
                },
            )
    except Exception:  # pragma: no cover
        _log.exception("postgres pipeline+eval persistence failed")

    return pipeline_id


def refresh_bk_tree_sync(timeout: float = 15.0) -> int:
    """Synchronously poll the shared ``pipelines`` table and merge any
    newly-landed rows into the trainer's live BK-Tree.

    Called from the trainer's batch loop so pipelines written by
    external processes (the FLAML seeder, future AutoML imports,
    user submissions landing through the backend API) become
    available as warm-start neighbours without a trainer restart.
    Returns the number of newly-added pipelines — 0 when no new
    rows, the store isn't initialised, or the tree hasn't
    finished its initial load yet.
    """
    async def _do() -> int:
        from dorian.experiment.store import get_experiment_store_sync
        store = get_experiment_store_sync()
        if store is None or not store.bk_tree_ready:
            return 0
        return await store.refresh_bk_tree_from_db()

    loop = _get_commit_loop()
    fut = asyncio.run_coroutine_threadsafe(_do(), loop)
    try:
        return fut.result(timeout=timeout)
    except Exception:
        _log.exception("BK-Tree refresh failed")
        return 0


def commit_rl_pipeline_sync(**kwargs) -> str | None:
    """Sync wrapper for callers running outside an event loop.

    Schedules the coroutine onto a persistent daemon-thread loop so the
    ExperimentStore singleton + asyncpg pool initialise once and stay
    valid. See ``_get_commit_loop`` for the motivation — in short,
    repeated ``asyncio.run(...)`` from the rollout loop was producing
    "connection was closed in the middle of operation" and "loop is
    closed" errors on every valid-pipeline commit.
    """
    loop = _get_commit_loop()
    fut = asyncio.run_coroutine_threadsafe(commit_rl_pipeline(**kwargs), loop)
    try:
        return fut.result(timeout=30)
    except Exception:
        _log.exception("rl commit failed")
        return None
