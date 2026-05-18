"""
rl/priors/flaml_seeder.py
-------------------------
Long-running FLAML seeding daemon.

Runs as a dedicated service (same pattern as the ``rl-trainer``
compose service) and continuously exercises FLAML against every
public dataset. Every single trial FLAML evaluates — not just the
best-per-estimator — flows through the pipeline index:

  1. Convert the trial's sklearn ``Pipeline`` to a Dorian DAG via
     the shared extractor bridge (``rl.priors.flaml_import``).
  2. Compute ``canonical_instance_hash`` — value-sensitive identity
     so ``C=0.5`` and ``C=1.0`` are distinct pipelines. Using
     ``class_hash`` here would collapse them and is the bug the
     RL-store share of this work also fixed.
  3. Upsert the pipeline row in ``per-collection doc_* tables`` + relational
     ``pipelines`` if structurally new. Always insert an
     ``evaluations`` row for this (pipeline × dataset) pair so the
     leaderboard picks up *every* trial's score.

Budget: 1.5h PER dataset (not total). The loop walks datasets
smallest-first and, on completion of the last one, either exits
(``--once``) or starts over so newer dataset revisions get picked
up without a restart. Can be stopped with SIGTERM safely — each
trial commits inline.

Environment:

  * ``DORIAN_FLAML_HOURS_PER_DATASET`` (default 1.5)
  * ``DORIAN_FLAML_ONCE``             (default 0 — loop forever)
  * ``DORIAN_FLAML_ESTIMATORS``       comma-separated estimator kinds
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataset selection
# ---------------------------------------------------------------------------

@dataclass
class DatasetPlan:
    name: str
    fpath: str
    n_rows: int
    n_features: int
    target_col: str | None


def _collect_datasets() -> list[DatasetPlan]:
    from rl.env.datasets import (
        CC18_SUBSET,
        DatasetRegistry,
        load_public_datasets,
    )
    fetched = load_public_datasets() or CC18_SUBSET
    registry = DatasetRegistry()

    plans: list[DatasetPlan] = []
    for ds in fetched:
        try:
            fpath = registry.path_for(ds)
        except Exception:
            _log.exception("could not materialise CSV for %s", ds.name)
            continue
        plans.append(DatasetPlan(
            name=ds.name,
            fpath=fpath,
            n_rows=ds.n_rows_approx,
            n_features=ds.n_features_approx,
            target_col=None,
        ))
    plans.sort(key=lambda d: d.n_rows)
    return plans


# ---------------------------------------------------------------------------
# Trial harvesting — intercept every FLAML trial as it completes
# ---------------------------------------------------------------------------

def _coerce_arrow_to_numpy(df: Any) -> None:
    df.columns = df.columns.astype(object)
    for col in df.columns:
        arr_type = type(df[col].array).__name__
        if "Arrow" in arr_type or "StringDtype" in str(df[col].dtype):
            df[col] = df[col].astype(object)


@dataclass
class Trial:
    """Captured FLAML trial — one sklearn ``Pipeline`` + its val score."""
    estimator_kind: str
    model: Any
    score: float


def _run_flaml_live_harvest(
    dataset: DatasetPlan,
    time_budget_s: float,
    estimator_list: list[str],
    starting_points: dict | None,
    on_trial: Callable[[Trial], None],
) -> None:
    """Fit FLAML and invoke ``on_trial`` for each completed trial
    as soon as FLAML records it to its JSONL log.

    FLAML 2.5 has no per-trial callback in its public API but writes
    every completed trial to the configured ``log_file_name`` with
    ``{config, learner, validation_loss, record_id, ...}`` on one
    line as soon as the trial finishes. We run the fit in a daemon
    thread and tail the file from the main thread, dispatching each
    new line as a ``Trial`` (using
    ``automl.best_model_for_estimator(kind)`` for the model
    snapshot — read-only across the GIL-atomic attribute, so a
    slightly-stale read at worst; over many trials the aggregate
    harvest is the same).
    """
    import pandas as pd
    import threading
    from flaml import AutoML
    import tempfile

    df = pd.read_csv(dataset.fpath)
    _coerce_arrow_to_numpy(df)
    target_col = dataset.target_col or df.columns[-1]
    y = df[target_col]
    X = df.drop(columns=[target_col])

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tmp:
        log_path = tmp.name

    automl = AutoML()
    fit_kwargs: dict[str, Any] = dict(
        X_train=X,
        y_train=y,
        task="classification",
        time_budget=time_budget_s,
        estimator_list=estimator_list,
        log_file_name=log_path,
        verbose=0,
    )
    if starting_points:
        fit_kwargs["starting_points"] = starting_points

    fit_exc: list[BaseException] = []

    def _fit_worker() -> None:
        try:
            automl.fit(**fit_kwargs)
        except BaseException as exc:  # noqa: BLE001 — propagated via list
            fit_exc.append(exc)

    _populate_flaml_kind_registry()
    worker = threading.Thread(target=_fit_worker, daemon=True, name="flaml-fit")
    worker.start()
    _log.info("[flaml-harvest] fit worker started; tailing %s", log_path)

    seen_records: set[int] = set()
    # Coarse poll interval — a trial runs in O(seconds) so 0.5s
    # between tails is enough to stream them, and avoids hammering
    # the filesystem while FLAML is working.
    poll_interval = 0.5
    loops = 0
    while worker.is_alive() or _log_has_unseen_records(log_path, seen_records):
        loops += 1
        progressed = _drain_log(
            log_path, seen_records, automl, on_trial,
        )
        if loops % 20 == 0:
            _log.info(
                "[flaml-harvest] loops=%d seen=%d progressed=%s worker_alive=%s",
                loops, len(seen_records), progressed, worker.is_alive(),
            )
        if not progressed:
            time.sleep(poll_interval)

    worker.join()
    if fit_exc:
        raise fit_exc[0]

    # Final overall-best snapshot — helpful for dashboards wanting
    # a pointer to "the" model FLAML converged on, even if its
    # config was already persisted trial-by-trial.
    overall = getattr(automl, "model", None) or getattr(automl, "best_estimator_", None)
    if overall is not None:
        on_trial(Trial(
            estimator_kind="__overall__",
            model=overall,
            score=float(getattr(automl, "best_loss", 0.0)),
        ))

    try:
        os.unlink(log_path)
    except Exception:
        pass


def _log_has_unseen_records(path: str, seen: set[int]) -> bool:
    """Cheap check: does the FLAML log file exist and have more
    records than we've dispatched? Called after the fit thread
    exits to drain any final entries."""
    try:
        with open(path, "r") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rid = rec.get("record_id")
                if rid is not None and rid not in seen:
                    return True
    except FileNotFoundError:
        pass
    return False


def _instantiate_estimator(kind: str, config: dict) -> Any | None:
    """Map a FLAML learner kind + config dict to a concrete sklearn-
    compatible estimator instance.

    Using the plain sklearn / lgbm / xgboost classes rather than
    FLAML's ``LGBMEstimator`` wrappers because downstream
    ``sklearn_pipeline_to_code`` expects a library-native estimator
    with a stable public ``__module__.__name__`` for its import
    line.

    Unknown keys in ``config`` are silently dropped when the
    concrete class doesn't accept them — this happens occasionally
    when FLAML's search surface has hyperparameters the underlying
    class renamed or removed across versions.
    """
    import inspect

    cls = _SKLEARN_LIKE_BY_FLAML_KIND.get(kind)
    if cls is None:
        return None
    try:
        sig = inspect.signature(cls.__init__)
        accepted = {name for name in sig.parameters if name != "self"}
        filtered = {k: v for k, v in config.items() if k in accepted}
        return cls(**filtered)
    except Exception:
        return None


_SKLEARN_LIKE_BY_FLAML_KIND: dict[str, type] = {}


def _populate_flaml_kind_registry() -> None:
    """Lazy import — sklearn / lgbm / xgboost can be slow to import.
    Populated on first call to ``_instantiate_estimator``.
    """
    global _SKLEARN_LIKE_BY_FLAML_KIND
    if _SKLEARN_LIKE_BY_FLAML_KIND:
        return
    mapping: dict[str, type] = {}
    try:
        from lightgbm import LGBMClassifier  # type: ignore
        mapping["lgbm"] = LGBMClassifier
    except Exception:
        pass
    try:
        from xgboost import XGBClassifier  # type: ignore
        mapping["xgboost"] = XGBClassifier
    except Exception:
        pass
    try:
        from sklearn.ensemble import RandomForestClassifier, ExtraTreesClassifier
        mapping["rf"] = RandomForestClassifier
        mapping["extra_tree"] = ExtraTreesClassifier
    except Exception:
        pass
    try:
        from sklearn.linear_model import LogisticRegression
        # FLAML uses l1 / l2 variants; both map to LogisticRegression
        # with different penalty. The config dict carries the
        # penalty name so one class handles both.
        mapping["lrl1"] = LogisticRegression
        mapping["lrl2"] = LogisticRegression
    except Exception:
        pass
    try:
        from sklearn.neighbors import KNeighborsClassifier
        mapping["kneighbor"] = KNeighborsClassifier
    except Exception:
        pass
    _SKLEARN_LIKE_BY_FLAML_KIND = mapping


def _drain_log(
    path: str,
    seen: set[int],
    automl: Any,
    on_trial: Callable[["Trial"], None],
) -> bool:
    """Parse every unseen line in *path* and call ``on_trial`` per
    new record. Returns True if any new record was dispatched."""
    progressed = False
    try:
        with open(path, "r") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rid = rec.get("record_id")
                kind = rec.get("learner")
                loss = rec.get("validation_loss")
                if rid is None or kind is None or loss is None:
                    continue
                if rid in seen:
                    continue
                seen.add(rid)
                # Reconstruct the model from the log entry's
                # ``config`` dict. ``best_model_for_estimator`` is
                # unreliable during the fit (per-kind best only
                # updates post-fit in FLAML 2.5); direct
                # reconstruction sidesteps that and gives a
                # 1:1 correspondence between log entries and
                # persisted trials.
                config = rec.get("config") or {}
                model = _instantiate_estimator(kind, config)
                if model is None:
                    _log.warning(
                        "[flaml-harvest] cannot instantiate %s(%s) — skipping",
                        kind, config,
                    )
                    continue
                on_trial(Trial(
                    estimator_kind=str(kind),
                    model=model,
                    score=float(loss),
                ))
                progressed = True
    except FileNotFoundError:
        pass
    return progressed


def _run_flaml_harvesting_all_trials(
    dataset: DatasetPlan,
    time_budget_s: float,
    estimator_list: list[str],
    starting_points: dict | None = None,
) -> list[Trial]:
    """Fit FLAML with a custom ``log_file_name`` + harvester.

    FLAML 2.5 doesn't expose an ``on_trial_complete`` hook the way
    we'd want, but it DOES write every trial's config + loss to
    ``log_file_name`` as JSON-lines. After fit we:

      * read the log file,
      * for each completed trial look up the best-so-far model for
        its estimator kind via ``automl.best_model_for_estimator``,
      * yield one ``Trial`` per log line whose estimator kind we
        haven't yielded yet at that loss level (dedupe by kind +
        loss rounded to 6 decimals — FLAML often re-evaluates the
        same config during its adaptive search).

    Coarse-grained but avoids relying on FLAML internals. Future
    refinement: reconstruct each trial's estimator directly from the
    logged config rather than taking ``best_model_for_estimator``
    snapshots.
    """
    import pandas as pd
    from flaml import AutoML
    import tempfile

    df = pd.read_csv(dataset.fpath)
    _coerce_arrow_to_numpy(df)
    target_col = dataset.target_col or df.columns[-1]
    y = df[target_col]
    X = df.drop(columns=[target_col])

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tmp:
        log_path = tmp.name

    automl = AutoML()
    fit_kwargs: dict[str, Any] = dict(
        X_train=X,
        y_train=y,
        task="classification",
        time_budget=time_budget_s,
        estimator_list=estimator_list,
        log_file_name=log_path,
        verbose=0,
    )
    if starting_points:
        # FLAML consumes ``starting_points`` as a dict keyed by
        # estimator kind with the previous-run hyperparameter
        # dict. Seeds the new search around that config so short
        # chunks don't start cold each time.
        fit_kwargs["starting_points"] = starting_points
    automl.fit(**fit_kwargs)

    # Harvest every distinct (kind, loss) from the log. Each such
    # point has a corresponding best-so-far model available via
    # ``best_model_for_estimator`` — FLAML keeps per-kind best.
    seen: set[tuple[str, float]] = set()
    trials: list[Trial] = []
    try:
        with open(log_path, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                kind = entry.get("learner")
                loss = entry.get("validation_loss")
                if kind is None or loss is None:
                    continue
                key = (str(kind), round(float(loss), 6))
                if key in seen:
                    continue
                seen.add(key)
    finally:
        try:
            os.unlink(log_path)
        except Exception:
            pass

    # For each estimator kind, snapshot the best-so-far model at
    # the end of search. One instance per kind — matches FLAML's
    # ``best_config_per_estimator`` layout. Richer per-trial
    # reconstruction (rebuild an instance from the log-file config)
    # is the next refinement: it'll give us N distinct models per
    # kind instead of the best-per-kind here.
    kinds_seen_best: dict[str, Trial] = {}
    for kind, loss in seen:
        if kind in kinds_seen_best:
            # Keep the best-loss trial per kind as the anchor.
            if loss < kinds_seen_best[kind].score:
                try:
                    m = automl.best_model_for_estimator(kind)
                except Exception:
                    continue
                if m is not None:
                    kinds_seen_best[kind] = Trial(kind, m, loss)
            continue
        try:
            m = automl.best_model_for_estimator(kind)
        except Exception:
            continue
        if m is not None:
            kinds_seen_best[kind] = Trial(kind, m, loss)

    # Always include the overall best explicitly in case the log
    # format changed and we missed it above.
    overall = getattr(automl, "model", None) or getattr(automl, "best_estimator_", None)
    if overall is not None:
        trials.append(Trial("__overall__", overall, float(getattr(automl, "best_loss", 0.0))))
    trials.extend(kinds_seen_best.values())
    return trials


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def _resolve_dataset_uuid(conn, dataset_name: str) -> str | None:
    """Resolve a dataset name to its content-hash ``datasets.id``.

    ``evaluations.dataset_id`` is FK to ``datasets.id`` (a UUID),
    not to the friendlier human name. Look it up via doc_datasets
    (document store) which stores the name field. The unified
    ``per-collection doc_* tables`` table was retired in 25c79a4 — each
    collection now lives in its own ``doc_<name>`` table.
    """
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
    # Verify the id also exists in the relational datasets table;
    # the FK enforces that.
    exists = await conn.fetchval(
        "SELECT 1 FROM datasets WHERE id = $1",
        row["id"],
    )
    return row["id"] if exists else None


async def _persist_trial(
    dataset: DatasetPlan,
    trial: Trial,
    pool_dsn_kwargs: dict[str, Any],
) -> tuple[str | None, bool]:
    """Convert + upsert one trial.

    Returns ``(pipeline_id, inserted_new)``. ``inserted_new`` is
    True when the pipeline was structurally unknown (new row in
    per-collection doc_* tables + relational pipelines); False when it was a
    dedupe (only the evaluations row is new).
    """
    from rl.priors.flaml_import import sklearn_pipeline_to_dag
    from dorian.pipeline.canonical import canonical_instance_hash

    try:
        dag = sklearn_pipeline_to_dag(trial.model)
    except Exception:
        _log.exception("extractor failed on %s / %s", dataset.name, trial.estimator_kind)
        return (None, False)

    instance_hash = canonical_instance_hash(dag)
    pipeline_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"flaml-v2/{instance_hash}"))

    dag_json = dag.to_json_dict()

    # 1) Upsert into per-collection doc_* tables (document store → UI).
    # 2) Upsert into relational pipelines (leaderboard source).
    # 3) Always insert evaluations row for this (pipeline × dataset).
    import asyncpg
    conn = await asyncpg.connect(**pool_dsn_kwargs)
    try:
        # doc_pipelines upsert (per-collection table; per-collection doc_* tables retired)
        await conn.execute(
            """
            INSERT INTO doc_pipelines (id, data, created_at, updated_at)
            VALUES ($1, $2::jsonb, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET
                data = EXCLUDED.data, updated_at = NOW()
            """,
            pipeline_id,
            json.dumps({
                "_id": pipeline_id,
                "pipeline_id": pipeline_id,
                "nodes": dag_json.get("nodes", {}),
                "edges": dag_json.get("edges", []),
                "task": "classification",
                "provenance": {"source": "flaml-v2", "estimator": trial.estimator_kind},
                "source": "flaml-v2",
            }),
        )

        # relational pipelines upsert — ExperimentStore.upsert_pipeline's schema.
        row_existed = await conn.fetchval(
            "SELECT 1 FROM pipelines WHERE id = $1",
            pipeline_id,
        )
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
            f"flaml-seed:{dataset.name}",
            "classification",
            json.dumps(dag_json),
            _extract_operator_names(dag_json),
            "flaml",
        )

        # evaluations row — always, for every trial × dataset.
        # FLAML's ``validation_loss`` is lower-is-better; convert
        # to ``metric_value`` as ``1 - loss`` so the leaderboard
        # sees a higher-is-better score.
        dataset_uuid = await _resolve_dataset_uuid(conn, dataset.name)
        if dataset_uuid is None:
            _log.warning(
                "dataset %s has no ``datasets`` row — skipping evaluation insert",
                dataset.name,
            )
        else:
            await conn.execute(
                """
                INSERT INTO evaluations (
                    pipeline_id, dataset_id, run_id, metric_name,
                    metric_value, eval_config
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                pipeline_id,
                dataset_uuid,
                str(uuid.uuid4()),
                "flaml_val_accuracy",
                float(max(0.0, 1.0 - trial.score)),
                json.dumps({
                    "source": "flaml-v2",
                    "estimator": trial.estimator_kind,
                    "val_loss": float(trial.score),
                    "dataset_name": dataset.name,
                }),
            )
    finally:
        await conn.close()

    return (pipeline_id, row_existed is None)


def _extract_operator_names(dag_json: dict) -> list[str]:
    out = []
    for n in (dag_json.get("nodes") or {}).values():
        if n.get("class_type") == "Operator" or n.get("type") == "operator":
            name = n.get("name")
            if name:
                out.append(name)
    return sorted(set(out))


def _dsn_kwargs() -> dict[str, Any]:
    from backend.config import config
    pg = config.postgresql
    return dict(
        host=pg.host,
        port=int(pg.port),
        user="dorian",
        password=pg.password,
        database="dorian",
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_STOPPING = False


def _install_signal_handlers() -> None:
    def _stop(*_):
        global _STOPPING
        _STOPPING = True
        _log.warning("SIGTERM/SIGINT — stopping after current trial")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


async def _main_async(args: argparse.Namespace) -> int:
    budget_per_ds = args.hours_per_dataset * 3600.0
    estimator_list = [s.strip() for s in args.estimators.split(",") if s.strip()]
    once = args.once

    iteration = 0
    while not _STOPPING:
        iteration += 1
        plans = _collect_datasets()
        if not plans:
            _log.error("no datasets available")
            return 1
        _log.info(
            "seeder iteration %d: %d datasets, %.1fh per dataset "
            "(live per-trial commits)",
            iteration, len(plans), args.hours_per_dataset,
        )

        for plan in plans:
            if _STOPPING:
                break
            _log.info("[flaml] fitting %s (rows=%d)", plan.name, plan.n_rows)
            t0 = time.monotonic()
            dsn_kwargs = _dsn_kwargs()

            # Running counters updated from the worker callback.
            counters = {"new": 0, "evals": 0, "seen": 0}

            # The tailer thread runs the on_trial callback
            # synchronously. ``_persist_trial`` is async, so we
            # schedule it onto the main event loop via
            # ``run_coroutine_threadsafe`` and wait on the result.
            loop = asyncio.get_running_loop()

            def _on_trial(trial: Trial) -> None:
                counters["seen"] += 1
                _log.info(
                    "[flaml] %s on_trial #%d kind=%s loss=%.4f — persisting",
                    plan.name, counters["seen"],
                    trial.estimator_kind, trial.score,
                )
                fut = asyncio.run_coroutine_threadsafe(
                    _persist_trial(plan, trial, dsn_kwargs),
                    loop,
                )
                try:
                    pid, inserted = fut.result(timeout=30)
                except Exception:
                    _log.exception(
                        "persist failed on %s / %s",
                        plan.name, trial.estimator_kind,
                    )
                    return
                _log.info(
                    "[flaml] %s trial #%d persisted: pid=%s new=%s",
                    plan.name, counters["seen"], pid, inserted,
                )
                if pid:
                    counters["evals"] += 1
                    if inserted:
                        counters["new"] += 1

            try:
                # Fit runs in a dedicated thread inside the harvester;
                # offloading the whole function to ``to_thread`` keeps
                # the async loop free to serve persist futures from
                # the on_trial callback.
                await asyncio.to_thread(
                    _run_flaml_live_harvest,
                    plan, budget_per_ds, estimator_list,
                    None,  # no starting_points for a single long fit
                    _on_trial,
                )
            except Exception:
                _log.exception("FLAML fit failed for %s", plan.name)
                continue
            fit_s = time.monotonic() - t0

            _log.info(
                "[flaml] %s done: fit=%.0fs trials_seen=%d "
                "new_pipelines=%d evals=%d",
                plan.name, fit_s,
                counters["seen"], counters["new"], counters["evals"],
            )

        if once:
            break
    return 0


def _warm_points_from_trials(trials: list["Trial"]) -> dict | None:
    """Build a FLAML ``starting_points`` dict from the prior-chunk
    trials. Keyed by estimator kind, the value is the sklearn-ish
    ``get_params()`` of the best-loss model per kind — FLAML
    accepts this shape and uses it to seed the next search.
    """
    if not trials:
        return None
    # Pick the best-loss (lowest) trial per kind.
    best_by_kind: dict[str, Trial] = {}
    for t in trials:
        if t.estimator_kind == "__overall__":
            continue
        cur = best_by_kind.get(t.estimator_kind)
        if cur is None or t.score < cur.score:
            best_by_kind[t.estimator_kind] = t
    out: dict = {}
    for kind, t in best_by_kind.items():
        try:
            out[kind] = t.model.get_params()
        except Exception:
            continue
    return out or None


def main() -> int:
    # ``force=True`` so our config wins even if an import-time
    # library (flaml's tune module is one) already installed a
    # root-logger handler. Without it, our ``__main__`` log lines
    # are silently dropped — the seeder's progress becomes
    # invisible under the sklearn warning flood.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    _install_signal_handlers()

    env = os.environ.get
    ap = argparse.ArgumentParser(description="FLAML warm-start seeding daemon")
    ap.add_argument(
        "--hours-per-dataset", type=float,
        default=float(env("DORIAN_FLAML_HOURS_PER_DATASET", "1.5")),
        help="Total FLAML time budget per dataset (hours).",
    )
    ap.add_argument(
        "--chunk-minutes", type=float,
        default=float(env("DORIAN_FLAML_CHUNK_MINUTES", "15")),
        help="Split the per-dataset budget into this-many-minute "
             "chunks; persist trials after each chunk so pipelines "
             "land incrementally.",
    )
    ap.add_argument(
        "--once", action="store_true",
        default=(env("DORIAN_FLAML_ONCE", "0") in ("1", "true", "yes", "on")),
        help="Run one pass over all datasets then exit "
             "(default: loop forever).",
    )
    ap.add_argument(
        "--estimators", type=str,
        default=env("DORIAN_FLAML_ESTIMATORS",
                    "lgbm,xgboost,rf,extra_tree,lrl1,lrl2,kneighbor"),
        help="Comma-separated FLAML estimator list.",
    )
    args = ap.parse_args()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
