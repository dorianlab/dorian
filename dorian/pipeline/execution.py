"""
dorian/pipeline/execution.py
-----------------------------
Core Pipeline Execution Engine.

Entry points
------------
    handle_pipeline_execution  – async; called by the queue bridge
    run_pipeline               – sync; runs in a thread via asyncio.to_thread
    _instrument                – wraps every node callable with event/state hooks

Architecture
------------
* Async layer  (asyncio event loop): reads docstore, creates state, spawns thread.
* Sync layer   (background thread):  builds the graph, calls dask.threaded.get() to run it
                                     with the synchronous thread-pool scheduler.
* Operator resolution & graph build: delegated to operator_resolver.py.
* State:  dorian/state/execution.py  (Redis-backed PipelineExecution objects).
* Results: dorian/state/results.py   (inline Redis or file-based for large outputs).

Sub-modules
-----------
* dag_analysis  — DAG parsing, validation, shadow helpers (pure functions)
* run_state     — Redis-backed execution state persistence & instrumentation
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from backend.config import config
from backend.envs import aioredis, redis
from backend.events import Event, aemit, emit
from dorian.dag import DAG, Edge, Group, IOMapping, Operator, Parameter, Snippet
from dorian.models.execution import (
    NodeState,
    NodeStatus,
    PipelineExecution,
    PipelineRunStatus,
)
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
from dorian.pipeline.operator_resolver import build_dag_graph
from dorian.pipeline.printout import expand_printout_nodes
from dorian.pipeline.state import expand_state_refs
from dorian.pipeline.transforms import (
    expand_categorical_encoding,
    expand_compound_operators,
    expand_dataset_refs,
)
from dorian.pipeline.shadow import launch_shadow_validation
from dorian.state.execution import StateTracker

# Re-export from dag_analysis — keeps existing import paths working
from dorian.pipeline.dag_analysis import (  # noqa: F401
    _compute_graph_depth,
    _flatten_groups,
    _node_to_shadow_dict,
    _parse_pipeline,
    _sink_nodes,
    _validate_pipeline,
)

# Re-export from run_state — keeps existing import paths working
from dorian.pipeline.run_state import (  # noqa: F401
    _build_trace_output,
    _gather_node_states,
    _instrument,
    _load_result_sync,
    _node_failed_sync,
    _node_running_sync,
    _node_success_sync,
    _observe_node,
    _patch_node_state,
    _read_execution,
    _store_result_sync,
    _stream_sync,
    _to_fields,
    _write_execution,
    cleanup_stale_run,
)

_exec_cfg = config.execution
_RESULT_TTL = int(_exec_cfg.result_ttl)
_EXECUTION_TIMEOUT = int(getattr(_exec_cfg, "timeout", 600))  # seconds


# ---------------------------------------------------------------------------
# Sentinel for failed upstream nodes
# ---------------------------------------------------------------------------

class NodeExecutionError(Exception):
    """Raised when a pipeline node fails during execution.

    Dask propagates the exception to every downstream task, which in turn
    catches it and marks themselves as SKIPPED — giving us proper failure
    propagation through the graph instead of silently passing None.
    """

    def __init__(self, node_id: str, message: str):
        self.node_id = node_id
        super().__init__(f"Node '{node_id}' failed: {message}")


class PipelineCancelled(Exception):
    """Raised when a pipeline run is cancelled by the user.

    The cancellation flag is set in Redis by the ``CancelPipeline`` event
    handler and checked cooperatively by ``_instrument()`` before each node
    starts executing.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"Pipeline run '{run_id}' cancelled by user")


# ---------------------------------------------------------------------------
# Stream helpers (async variant — sync version lives in run_state.py)
# ---------------------------------------------------------------------------

async def _stream_async(uid: str, session: str, msg: dict) -> None:
    if uid == "system" and (session or "").startswith("rl:"):
        return
    await aioredis.xadd(RedisKeys.stream(uid, session), _to_fields(msg), maxlen=STREAM_MAXLEN, approximate=True)


# ---------------------------------------------------------------------------
# Evaluation — post-execution metric computation (Automated Hold-out)
# ---------------------------------------------------------------------------

def _find_split_node(pipeline: DAG) -> str | None:
    """Find the train_test_split node in the pipeline (by operator name).

    Scans all nodes for an Operator whose dotted name contains
    ``train_test_split``.  Returns the node ID or ``None``.
    """
    for nid, node in pipeline.nodes.items():
        if isinstance(node, Operator) and "train_test_split" in node.name:
            return nid
    return None


def _resolve_eval_graph(graph: dict, sink_key: str):
    """Resolve a Dask-style ``{key: value | (callable, *deps)}`` graph
    in topological order. Drop-in replacement for
    ``dask.threaded.get(graph, sink_key)`` with no Dask dependency.

    The evaluation DAG is shallow (metrics → predictions → split) and
    only one sink, so a recursive resolver is sufficient and keeps the
    Rust-runner code path free of lingering Dask imports.
    """
    resolved: dict = {}

    def _resolve(key):
        # Only string keys reference other graph nodes — every other
        # tuple element is an inline value (a kwargs ``dict``, a
        # display ``str`` that doesn't appear as a graph key, etc.)
        # and must pass through unchanged. Without this guard, a
        # ``dict`` arg trips ``key in resolved`` with
        # ``unhashable type: 'dict'`` and the whole evaluation pipeline
        # silently returns no metrics — which is exactly what was
        # happening for AutoML/xproduct trials.
        if not isinstance(key, str) or key not in graph:
            return key
        if key in resolved:
            return resolved[key]
        spec = graph.get(key)
        if not isinstance(spec, tuple) or not spec or not callable(spec[0]):
            resolved[key] = spec
            return spec
        fn, *deps = spec
        args = [_resolve(d) for d in deps]
        result = fn(*args)
        resolved[key] = result
        return result

    return _resolve(sink_key)


def _evaluate_pipeline_sync(
    run_id: str,
    uid: str,
    session: str,
    pipeline: DAG,
    node_states: Dict[str, NodeState],
) -> Dict[str, float]:
    """Evaluate a completed pipeline via the selected evaluation procedure.

    Resolves the evaluation procedure from session meta, builds an
    evaluation DAG (metrics as parallel Dask nodes), executes it via
    ``dask.threaded.get()``, and returns ``{display_name: value}``.

    Supports all procedure types:
    - **hold-out**: parallel metric computation on y_test vs y_pred
    - **k-fold**: k-fold CV with averaged metrics
    - **custom**: user-provided code via Snippet (``foo(y_test, y_pred, X_test)``)
    - **pairwise / none**: skip inline evaluation

    Returns an empty dict when evaluation is skipped or fails gracefully.
    All exceptions are caught internally — this function never raises.
    """
    from dorian.evaluation.resolver import resolve_eval_procedure
    from dorian.evaluation.dag_builder import (
        EvalContext,
        TASK_METRIC_KWARGS,
        build_evaluation_dag,
    )
    from dorian.knowledge.queries import get_metrics_for_task, get_metric_display_name

    # ── Read session meta ──
    raw_meta = redis.get(RedisKeys.session_meta(session))
    if not raw_meta:
        return {}
    meta = json.loads(raw_meta)

    # ── Resolve the evaluation procedure ──
    procedure = resolve_eval_procedure(meta)
    if procedure.type in ("none", "pairwise"):
        return {}

    task_info = meta.get("selectedDataScienceTask") or {}
    task_name = task_info.get("name")

    ds = meta.get("dataset") or {}
    if not task_name:
        return {}

    # ── Extract pipeline outputs ──
    split_nid = _find_split_node(pipeline)
    if not split_nid:
        return {}

    split_state = node_states.get(split_nid)
    if not split_state or split_state.status != NodeStatus.SUCCESS:
        return {}

    split_result = _load_result_sync(split_state.result_ref)
    if split_result is None or not hasattr(split_result, "__getitem__"):
        return {}

    try:
        X_test = split_result[1]   # (X_train, X_test, y_train, y_test)
        y_test = split_result[3]
    except (IndexError, KeyError, TypeError):
        return {}

    # Locate y_pred. The naive "take the sink's result" breaks when the
    # pipeline already contains an inline metric node (the RL eval
    # template ends with ``sklearn.metrics.accuracy_score`` so its
    # output is a scalar, not an array). Detect that case and walk back
    # one edge to the metric's ``y_pred`` input — that source's result
    # IS the predictions array the evaluation procedure needs. If the
    # sink isn't a metric, use its result directly as before.
    #
    # Prefer inline-metric sinks ahead of every other terminal. After
    # compound expansion a transformer's ``fit_transform`` method node
    # also ends up sinkless (it's the chain terminus with no outgoing
    # edges), so the naive iteration order would pick THAT first and
    # hand the (instance, X_transformed) tuple to the metric — y_pred
    # then has length 2 (the tuple) instead of the predictions array.
    # Filtering metric sinks first guarantees the predictions branch
    # wins whenever the pipeline carries an explicit metric node.
    sinks = _sink_nodes(pipeline)

    def _is_metric_sink(nid: str) -> bool:
        n = pipeline.nodes.get(nid)
        return (
            isinstance(n, Operator)
            and isinstance(n.name, str)
            and n.name.startswith("sklearn.metrics.")
        )

    sinks = sorted(sinks, key=lambda nid: 0 if _is_metric_sink(nid) else 1)
    y_pred = None
    for sink_id in sinks:
        sink_node = pipeline.nodes.get(sink_id)
        is_inline_metric = (
            isinstance(sink_node, Operator)
            and isinstance(sink_node.name, str)
            and sink_node.name.startswith("sklearn.metrics.")
        )
        if is_inline_metric:
            # Walk back: find the edge feeding the metric's y_pred port.
            # The eval template wires ``y_pred`` at position 1 (see
            # dorian/pipeline/generation/catalog.py _FUNCTION_IO_OVERRIDES).
            # Honor the edge's ``output`` port when slicing — compound-
            # expanded method nodes (``*_cx_predict_*``) return a 2-tuple
            # ``(instance, predictions)`` and the edge uses ``output=1``
            # to grab the predictions. Loading the raw result_ref without
            # applying ``output`` yields the whole tuple → length-2 y_pred
            # → shape mismatch with y_test (12000 samples).
            source_id: str | None = None
            source_output: int | str = 0
            for e in pipeline.edges:
                if e.destination == sink_id and (
                    e.position == 1 or e.position == "1" or e.position == "y_pred"
                ):
                    source_id = e.source
                    source_output = e.output
                    break
            if source_id:
                src_state = node_states.get(source_id)
                if src_state and src_state.status == NodeStatus.SUCCESS and src_state.result_ref:
                    raw = _load_result_sync(src_state.result_ref)
                    if raw is not None:
                        # Apply the output port as a subscript when the
                        # source produces a tuple-like multi-output. Method
                        # nodes always do; plain function operators don't.
                        try:
                            idx = int(source_output)
                        except (TypeError, ValueError):
                            idx = 0
                        if idx != 0 and hasattr(raw, "__getitem__") and not hasattr(raw, "shape"):
                            try:
                                y_pred = raw[idx]
                            except (IndexError, KeyError, TypeError):
                                y_pred = raw
                        else:
                            y_pred = raw
                        if y_pred is not None:
                            break
            # If we couldn't resolve the predictions source, fall through
            # and don't accept the metric's scalar as y_pred — that's the
            # exact bug this branch is avoiding.
            continue

        ns = node_states.get(sink_id)
        if ns and ns.status == NodeStatus.SUCCESS and ns.result_ref:
            y_pred = _load_result_sync(ns.result_ref)
            if y_pred is not None:
                break

    if y_pred is None:
        return {}

    # ── Resolve metrics from KB ──
    metric_fqns = get_metrics_for_task(task_name)
    if not metric_fqns and procedure.type != "custom":
        return {}

    display_names = {}
    for fqn in metric_fqns:
        dn = get_metric_display_name(fqn)
        if dn:
            display_names[fqn] = dn

    # ── Build evaluation context ──
    ctx = EvalContext(
        y_test=y_test,
        y_pred=y_pred,
        X_test=X_test,
        metric_fqns=metric_fqns,
        metric_display_names=display_names,
        metric_kwargs=TASK_METRIC_KWARGS,
        run_id=run_id,
        task_name=task_name,
    )

    # Custom procedure: inject code from resolver
    if procedure.type == "custom":
        ctx.custom_code = procedure.config.get("code")

    # K-fold: inject full dataset (pre-split) if available
    if procedure.type == "kfold":
        try:
            X_train = split_result[0]
            y_train = split_result[2]
            import numpy as _np
            # Reconstruct full X and y from train/test split
            if hasattr(X_train, "values"):
                import pandas as _pd
                ctx.X_full = _pd.concat([X_train, X_test], ignore_index=True)
            else:
                ctx.X_full = _np.concatenate([X_train, X_test])
            ctx.y_full = _np.concatenate([y_train, y_test])
        except Exception:
            pass  # k-fold will fall back to available data

    # ── Build and execute the evaluation DAG ──
    try:
        eval_graph, sink_key = build_evaluation_dag(procedure.type, ctx)
        metrics = _resolve_eval_graph(eval_graph, sink_key)
    except Exception as exc:
        emit(Event("EvaluationDagFailed", {
            "run_id": run_id,
            "procedure": procedure.name,
            "error": str(exc),
        }))
        return {}

    if not isinstance(metrics, dict):
        return {}

    return metrics


# ---------------------------------------------------------------------------
# Observability: record pipeline failures at every stage
# ---------------------------------------------------------------------------

def _record_run_to_obs(
    run_id: str,
    uid: str,
    session: str,
    status: str,
    start_ts: float,
    error: str | None = None,
    *,
    stage: str = "",
    trace: str | None = None,
    source: str = "",
    pipeline_id: str | None = None,
    node_count: int = 0,
    node_types: str | None = None,
    failed_node: str | None = None,
) -> None:
    """Best-effort write to the in-memory observability collector."""
    try:
        from dorian.observability.collector import collector as _obs
        _obs.record_pipeline(
            run_id=run_id, uid=uid, session=session,
            status=status, start_ts=start_ts, end_ts=time.time(),
            node_count=node_count, error=error,
            stage=stage, trace=trace, source=source,
            pipeline_id=pipeline_id, node_types=node_types,
            failed_node=failed_node,
        )
    except Exception:
        pass  # non-fatal


def _summarise_node_types(pipeline: DAG) -> str:
    """Return a short comma-separated summary of operator FQNs in the pipeline."""
    names: list[str] = []
    for node in pipeline.nodes.values():
        if isinstance(node, Operator):
            names.append(node.name)
        elif isinstance(node, Snippet):
            names.append(f"snippet:{node.name}")
    return ",".join(names[:30])  # cap at 30 to avoid huge strings


# ---------------------------------------------------------------------------
# Main orchestration function  (runs in a background thread – no asyncio loop)
# ---------------------------------------------------------------------------

def _run_via_rust_runner(
    graph: dict,
    sinks: list[str],
    cache_node_keys: dict[str, str] | None = None,
    pipeline=None,
    key_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Walk the Dask graph in Rust-determined topological order.

    Drop-in replacement for ``dask.threaded.get(graph, sinks)``
    when ``DORIAN_USE_RUST_RUNNER=1``. Preserves the existing
    instrumented callables; only the topology + scheduling moves
    to Rust.

    Strategy:
      1. Adapt the Dask graph (``{key: (fn, *args)}``) into a
         minimal Dorian-shaped JSON the pyo3
         ``run_pipeline`` accepts: each Dask key becomes an
         ``Operator`` node; each string dependency becomes an
         ``Edge``. Slice / integer args become Parameter nodes
         so Rust's topology has the full structure.
      2. ``dorian_native.run_pipeline`` walks that graph and
         calls back into this function for every Operator
         node — at that point we resolve the actual Dask
         entry, feed real args, and run the
         already-instrumented callable. The result lands in the
         ``results`` dict so downstream nodes can read it.
      3. After Rust returns, we extract sink results from
         ``results`` to mirror what ``dask.threaded.get`` would
         have returned.

    Errors raised by node callables propagate via Rust's
    ``node_failed`` event → re-raise on the Python side so the
    surrounding try/except branches in ``run_pipeline`` catch
    them with the same semantics as Dask's exception path.
    """
    import dorian_native

    # Templates (DAGs containing LogicalTask placeholder nodes)
    # aren't executable. The AutoML/RL binding step is responsible
    # for replacing every LogicalTask with a concrete Operator
    # before submission. If one slipped through, fail loud rather
    # than letting the rust runner produce a confusing error.
    if pipeline is not None and getattr(pipeline, "is_template", False):
        unbound = [
            (nid, getattr(node, "path", ()))
            for nid, node in pipeline.nodes.items()
            if type(node).__name__ == "LogicalTask"
        ]
        raise RuntimeError(
            f"rust runner: pipeline contains {len(unbound)} unbound "
            f"LogicalTask placeholder(s); bind them via the AutoML / "
            f"RL binding step before submitting. unbound={unbound!r}"
        )

    # Cache elision — when caller didn't pre-compute keys but did
    # pass the pipeline, do the prefix-elision pass here so callers
    # like the RL executor (which calls this directly, not via
    # ``run_pipeline``) get the same cache benefits as the user
    # pipeline path. Caller-supplied ``cache_node_keys`` (from
    # ``run_pipeline``) take precedence — we don't double-elide.
    if cache_node_keys is None and pipeline is not None:
        try:
            from dorian.exec.intermediates_cache import (
                elide_cached_nodes,
                ensure_open,
            )
            ensure_open()
            graph, cache_node_keys, _stats = elide_cached_nodes(
                graph, pipeline, key_map=key_map,
            )
        except Exception:  # noqa: BLE001 — cache is optional
            cache_node_keys = {}

    results: dict[str, Any] = {}

    # 1. Build the faux Dorian DAG. Constants land directly in
    #    ``results`` (Rust's run_pipeline skips Parameter nodes
    #    engine-side, so the ``_fire`` callback never sees them);
    #    operators land in the faux graph as ``Operator`` nodes
    #    that Rust will dispatch through ``_fire``.
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for key, entry in graph.items():
        if not isinstance(entry, tuple) or not entry:
            results[key] = entry
            nodes[key] = {
                "class_type": "Parameter",
                "name": key, "dtype": "eval", "value": "",
            }
            continue
        nodes[key] = {
            "class_type": "Operator", "name": key, "language": "python",
        }
        _fn, *args = entry
        for pos, arg in enumerate(args):
            if isinstance(arg, str) and arg in graph:
                edges.append({
                    "source": arg, "destination": key,
                    "position": pos, "output": 0,
                })
    pipeline_json_for_rust = json.dumps({"nodes": nodes, "edges": edges})

    # 2. Fire callback resolves the Dask entry and runs it.
    failures: list[BaseException] = []

    def _fire(node_id: str, payload_json: str, inputs_json: str) -> None:
        if failures:
            # An earlier node failed; let the rest of the topology
            # walk no-op. We don't raise here because Rust would
            # mark this node as failed when we want skipped semantics
            # to mirror Dask's "already-failed task → child skipped".
            return
        entry = graph.get(node_id)
        if not isinstance(entry, tuple) or not entry:
            # Constants are pre-populated above; this path should be
            # unreachable because Rust skips Parameter nodes.
            results[node_id] = entry
            return
        fn, *args = entry
        resolved: list = []
        for arg in args:
            if isinstance(arg, str) and arg in graph:
                if arg not in results:
                    failures.append(NodeExecutionError(
                        node_id,
                        f"upstream key '{arg}' has no result yet — "
                        "topology order violation",
                    ))
                    return
                resolved.append(results[arg])
            else:
                resolved.append(arg)
        try:
            results[node_id] = fn(*resolved)
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            failures.append(exc)
            raise

    events_json = dorian_native.run_pipeline(pipeline_json_for_rust, _fire)
    # Persist any newly-computed outputs to the intermediates cache
    # BEFORE we surface failures. Each cache key uniquely identifies
    # one operator firing — partial-pipeline outputs stay valid for
    # future trials even when downstream nodes fail. RL trials in
    # particular always have some failures (it's exploring random
    # configs); blocking storage on full success would mean the
    # cache never warms up. Errors here are non-fatal — the cache
    # simply doesn't memoise this firing.
    if cache_node_keys:
        try:
            from dorian.exec.intermediates_cache import store_node_outputs
            store_node_outputs(results, cache_node_keys)
        except Exception as _store_exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "intermediates_cache.store raised: %s", _store_exc,
            )
    # Surface the first failure with its original exception type so
    # the existing except-branch logic (NodeExecutionError vs other)
    # keeps working unchanged.
    if failures:
        raise failures[0]
    # Sanity: every sink must have a result.
    for sink in sinks:
        if sink not in results:
            raise RuntimeError(
                f"rust runner: sink {sink!r} produced no result — "
                "topology incomplete"
            )
    # Returning ``results`` lets callers that want the full keyed
    # output (RL executor's metric extraction, mainly) read sink
    # values directly without re-routing through Dask.
    return results


def run_pipeline(run_id: str, uid: str, session: str, pipeline_json: str,
                  vault_passphrase: str | None = None,
                  pipeline_id: str | None = None) -> dict:
    """
    Build and execute a pipeline DAG via the Dask cluster.

    This is a plain function (not async) – it runs in a background thread
    spawned by asyncio.to_thread().  The Dask graph is executed via
    dask.threaded.get(), which blocks until all sink nodes complete.
    All Redis interactions use the synchronous `redis` client from backend.envs.
    """
    _t0 = time.time()
    # Detect RL-originated runs: pipeline_id set by the RL engine, or
    # "rl" uid prefix used by the generation worker.
    _source = "rl" if (uid or "").startswith("rl") or (pipeline_id or "").startswith("rl") else "user"

    emit(Event("PipelineOrchestrationStarted", {
        "source": "execution.run_pipeline",
        "run_id": run_id,
        "uid": uid,
        "session": session,
    }))

    # 1. Parse pipeline
    try:
        pipeline = _parse_pipeline(json.loads(pipeline_json))
    except Exception as exc:
        _tb = traceback.format_exc()
        emit(Event("PipelineRunFailed", {
            "source": "execution.run_pipeline",
            "run_id": run_id,
            "uid": uid,
            "session": session,
            "error": str(exc),
            "trace": _tb,
            "stage": "parse",
        }))
        _stream_sync(uid, session, {"event": "pipeline/run/failed", "run_id": run_id, "error": str(exc)})
        _record_run_to_obs(run_id, uid, session, "failed", _t0, str(exc),
                           stage="parse", trace=_tb, source=_source, pipeline_id=pipeline_id)
        return {"run_id": run_id, "status": "FAILED"}

    if not pipeline.nodes:
        emit(Event("PipelineRunFailed", {
            "source": "execution.run_pipeline",
            "run_id": run_id,
            "uid": uid,
            "session": session,
            "error": "Parsed pipeline contains no nodes",
            "stage": "validation",
        }))
        _stream_sync(uid, session, {"event": "pipeline/run/failed", "run_id": run_id, "error": "Pipeline has no nodes"})
        _record_run_to_obs(run_id, uid, session, "failed", _t0, "Pipeline has no nodes",
                           stage="validation", source=_source, pipeline_id=pipeline_id)
        return {"run_id": run_id, "status": "FAILED"}

    # 1b. Structural validation (cycles, dangling edges)
    _node_types_summary = _summarise_node_types(pipeline)
    validation_errors = _validate_pipeline(pipeline)
    if validation_errors:
        msg = "; ".join(validation_errors)
        emit(Event("PipelineRunFailed", {
            "source": "execution.run_pipeline",
            "run_id": run_id,
            "uid": uid,
            "session": session,
            "error": msg,
            "stage": "validation",
        }))
        _stream_sync(uid, session, {"event": "pipeline/run/failed", "run_id": run_id, "error": msg})
        _record_run_to_obs(run_id, uid, session, "failed", _t0, msg,
                           stage="validation", source=_source, pipeline_id=pipeline_id,
                           node_count=len(pipeline.nodes), node_types=_node_types_summary)
        return {"run_id": run_id, "status": "FAILED"}

    # 2. Transition run → RUNNING and initialise per-node keys
    exc_obj = _read_execution(run_id)
    if exc_obj:
        exc_obj.status = PipelineRunStatus.RUNNING
        exc_obj.start_time = time.time()
        # Don't store node_states in the monolithic blob — each node gets its
        # own Redis key (written by _patch_node_state) to avoid WATCH contention.
        _write_execution(exc_obj)

    # Also persist RUNNING in session meta so stale-run detection works
    # even if the process crashes before reaching the final write at step 8.
    try:
        raw_meta = redis.get(RedisKeys.session_meta(session))
        if raw_meta:
            _meta = json.loads(raw_meta)
            _meta["lastRun"] = {
                "run_id": run_id,
                "status": str(PipelineRunStatus.RUNNING),
                "ts": time.time(),
            }
            redis.set(RedisKeys.session_meta(session), json.dumps(_meta))
    except Exception:
        pass  # best-effort — don't block execution

    _run_start_ts = time.time()

    emit(Event("PipelineRunStarted", {
        "run_id": run_id,
        "uid": uid,
        "session": session,
        "node_count": len(pipeline.nodes),
        "node_ids": list(pipeline.nodes.keys()),
    }))
    _stream_sync(uid, session, {
        "event": "pipeline/run/started",
        "run_id": run_id,
        "node_count": len(pipeline.nodes),
    })

    # 3. Expand platform-level operators before graph compilation.
    #    a) dorian.io.dataset nodes → concrete file-loader sub-chain.
    #    b) Compound (class-interface) operators → __init__ / fit / infer
    #       sub-DAG derived from the KB method sequence.  Function-interface
    #       operators and operators whose KB entry is absent pass through
    #       unchanged (the latter also emit a warning log).
    try:
        pipeline = expand_dataset_refs(pipeline, session)
        pipeline = expand_state_refs(pipeline, session)
        pipeline = expand_categorical_encoding(pipeline, session)
        pipeline = expand_compound_operators(pipeline, session)
        pipeline = expand_printout_nodes(pipeline, session)
    except Exception as exc:
        # Surgical error path: ``CompoundExpansionError`` carries the
        # specific node + operator that failed so the SPA can mark
        # ONE box red instead of blanket-failing every node. Bare
        # exceptions (KeyError on a missing KB field, etc.) still flow
        # through the generic path with the full traceback for
        # debugging.
        from dorian.pipeline.transforms import CompoundExpansionError
        if isinstance(exc, CompoundExpansionError):
            msg = str(exc)
            failed_node = exc.node_id
            failed_operator = exc.operator
            reason = exc.reason
        else:
            msg = f"Pipeline expansion failed: {exc}"
            failed_node = None
            failed_operator = None
            reason = "expansion"
        _tb = traceback.format_exc()
        emit(Event("PipelineExpansionFailed", {
            "run_id": run_id, "stage": "expansion", "error": str(exc),
            "failed_node": failed_node,
            "failed_operator": failed_operator,
            "reason": reason,
        }))
        emit(Event("PipelineRunFailed", {
            "source": "execution.run_pipeline",
            "run_id": run_id, "uid": uid, "session": session,
            "error": msg, "trace": _tb,
            "stage": "expansion",
            "failed_node": failed_node,
            "failed_operator": failed_operator,
        }))
        _stream_sync(uid, session, {
            "event": "pipeline/run/failed",
            "run_id": run_id,
            "error": msg,
            # Frontend can highlight one node when these are present
            # instead of marking the whole DAG failed.
            "failed_node": failed_node,
            "failed_operator": failed_operator,
        })
        _record_run_to_obs(run_id, uid, session, "failed", _t0, msg,
                           stage="expansion", trace=_tb, source=_source,
                           pipeline_id=pipeline_id, node_count=len(pipeline.nodes),
                           node_types=_node_types_summary,
                           failed_node=failed_node)
        return {"run_id": run_id, "status": "FAILED"}

    # 3b. Post-expansion structural validation — expansion can introduce
    #     cycles (e.g. from duplicate KB method entries) that the pre-expansion
    #     validation at step 1b would not catch.
    post_validation_errors = _validate_pipeline(pipeline)
    if post_validation_errors:
        msg = "Post-expansion: " + "; ".join(post_validation_errors)
        emit(Event("PipelineRunFailed", {
            "source": "execution.run_pipeline",
            "run_id": run_id, "uid": uid, "session": session,
            "error": msg, "stage": "post_expansion_validation",
        }))
        _stream_sync(uid, session, {"event": "pipeline/run/failed", "run_id": run_id, "error": msg})
        _record_run_to_obs(run_id, uid, session, "failed", _t0, msg,
                           stage="post_expansion_validation", source=_source,
                           pipeline_id=pipeline_id, node_count=len(pipeline.nodes),
                           node_types=_summarise_node_types(pipeline))
        return {"run_id": run_id, "status": "FAILED"}

    # Resolve encrypted environment variable references (${VAR_NAME} → plaintext).
    # This must happen after compound expansion (which may introduce api_key params)
    # but before graph building.  The resolved DAG is never persisted.
    if vault_passphrase:
        try:
            from dorian.pipeline.vault_transform import resolve_vault_references
            pipeline = resolve_vault_references(pipeline, uid, vault_passphrase)
        except Exception as exc:
            emit(Event("VaultResolutionFailed", {"run_id": run_id, "error": str(exc)}))
            emit(Event("PipelineRunFailed", {
                "source": "execution.run_pipeline",
                "run_id": run_id, "uid": uid, "session": session,
                "error": str(exc), "stage": "vault_resolution",
            }))
            _stream_sync(uid, session, {"event": "pipeline/run/failed", "run_id": run_id, "error": str(exc)})
            _record_run_to_obs(run_id, uid, session, "failed", _t0, str(exc),
                               stage="vault_resolution", source=_source, pipeline_id=pipeline_id,
                               node_count=len(pipeline.nodes))
            return {"run_id": run_id, "status": "FAILED"}
        finally:
            vault_passphrase = None  # forget passphrase immediately

    # Guard: detect unresolved env-var references that survived vault resolution.
    # This happens when the frontend did not send a vault nonce (e.g. the user
    # hasn't entered their passphrase, or _pipelineHasEnvParams missed the param).
    import re as _re
    _ENV_PAT = _re.compile(r"^\$\{(\w+)\}$")
    for nid, node in pipeline.nodes.items():
        if isinstance(node, Parameter) and node.dtype == "env":
            m = _ENV_PAT.match(node.value or "")
            var_name = m.group(1) if m else node.value
            msg = (
                f"Environment variable '{var_name}' was not decrypted. "
                f"Please open the Environment Variables panel, enter your vault "
                f"passphrase, and run the pipeline again."
            )
            emit(Event("PipelineRunFailed", {
                "source": "execution.run_pipeline",
                "run_id": run_id, "uid": uid, "session": session,
                "error": msg, "stage": "vault_resolution",
            }))
            _stream_sync(uid, session, {"event": "pipeline/run/failed", "run_id": run_id, "error": msg})
            _record_run_to_obs(run_id, uid, session, "failed", _t0, msg,
                               stage="vault_resolution", source=_source, pipeline_id=pipeline_id,
                               node_count=len(pipeline.nodes), failed_node=nid)
            return {"run_id": run_id, "status": "FAILED"}

    # Guard: detect unresolved platform primitives that slipped through expansion.
    # Most common case: dorian.io.dataset survives because no dataset has been
    # uploaded yet (expand_dataset_refs returns unchanged when fpath is missing).
    # Also catch Parameter(dtype="state") that failed to expand.
    for nid, node in pipeline.nodes.items():
        if isinstance(node, Parameter) and node.dtype == "state":
            msg = (
                f"State parameter '{node.value}' could not be expanded — "
                "the requested state key is invalid or missing."
            )
            emit(Event("UnresolvedPlatformOperator", {"run_id": run_id, "node_id": nid, "operator": node.name}))
            emit(Event("PipelineRunFailed", {
                "source": "execution.run_pipeline",
                "run_id": run_id, "uid": uid, "session": session,
                "error": msg, "stage": "expansion",
            }))
            _stream_sync(uid, session, {"event": "pipeline/run/failed", "run_id": run_id, "error": msg})
            _record_run_to_obs(run_id, uid, session, "failed", _t0, msg,
                               stage="expansion", source=_source, pipeline_id=pipeline_id,
                               node_count=len(pipeline.nodes), failed_node=nid,
                               node_types=_node_types_summary)
            return {"run_id": run_id, "status": "FAILED"}
        if isinstance(node, Operator) and node.name.startswith("dorian."):
            if node.name == "dorian.io.state":
                msg = (
                    f"Operator '{node.name}' could not be expanded — "
                    "the requested state key is invalid or missing."
                )
            else:
                msg = (
                    f"Operator '{node.name}' could not be expanded — "
                    "please upload a dataset before running the pipeline."
                )
            emit(Event("UnresolvedPlatformOperator", {"run_id": run_id, "node_id": nid, "operator": node.name}))
            emit(Event("PipelineRunFailed", {
                "source": "execution.run_pipeline",
                "run_id": run_id, "uid": uid, "session": session,
                "error": msg, "stage": "expansion",
            }))
            _stream_sync(uid, session, {"event": "pipeline/run/failed", "run_id": run_id, "error": msg})
            _record_run_to_obs(run_id, uid, session, "failed", _t0, msg,
                               stage="expansion", source=_source, pipeline_id=pipeline_id,
                               node_count=len(pipeline.nodes), failed_node=nid,
                               node_types=_node_types_summary)
            return {"run_id": run_id, "status": "FAILED"}

    # Seed per-node keys with PENDING state (after expansion so we cover all
    # nodes that will actually be executed, including expanded sub-DAG nodes).
    for nid in pipeline.nodes:
        _patch_node_state(run_id, nid, status=NodeStatus.PENDING)

    emit(Event("PipelineGraphBuilding", {
        "run_id": run_id,
        "uid": uid,
        "session": session,
    }))
    try:
        raw_graph = build_dag_graph(pipeline)
    except (ImportError, Exception) as exc:
        msg = f"Graph build failed: {exc}"
        _tb = traceback.format_exc()
        emit(Event("PipelineRunFailed", {
            "source": "execution.run_pipeline",
            "run_id": run_id, "uid": uid, "session": session,
            "error": msg, "stage": "graph_build",
        }))
        _stream_sync(uid, session, {"event": "pipeline/run/failed", "run_id": run_id, "error": msg})
        _record_run_to_obs(run_id, uid, session, "failed", _t0, msg,
                           stage="graph_build", trace=_tb, source=_source,
                           pipeline_id=pipeline_id, node_count=len(pipeline.nodes),
                           node_types=_summarise_node_types(pipeline))
        return {"run_id": run_id, "status": "FAILED"}
    graph: dict = {}

    def _is_int_like(v) -> bool:
        """Return True for integer or string representation of an integer (e.g. '0', '1').

        build_dag_graph stores slice indices as integers, but if edge positions/outputs
        came from JSON as strings the index may be a string like '0'.  Both forms
        represent literal slice indices (not graph dependency keys) and must NOT be
        instrumented.
        """
        if isinstance(v, int):
            return True
        try:
            int(v)
            return True
        except (TypeError, ValueError):
            return False

    # Prefix all Dask graph keys with run_id to prevent key collisions when the
    # same pipeline structure is submitted a second time.  The Dask scheduler
    # caches tasks by key; reusing the same key with a different callable causes
    # "Detected different run_spec" warnings and potential deadlocks.
    key_map = {k: f"{run_id}_{k}" for k in raw_graph}

    for old_key, entry in raw_graph.items():
        new_key = key_map[old_key]
        if not isinstance(entry, tuple) or not entry:
            graph[new_key] = entry
            continue
        fn, *deps = entry
        # Rename string dependency keys; leave integers (slice indices) unchanged.
        new_deps = [key_map.get(d, d) if isinstance(d, str) else d for d in deps]
        # Slice entries produced by build_dag_graph: (lambda, src_key, int_or_str_int)
        if new_deps and _is_int_like(new_deps[-1]) and len(new_deps) == 2:
            graph[new_key] = (fn,) + tuple(new_deps)  # pass-through – don't instrument slices
        else:
            # Resolve the logical node_id from the original (un-prefixed) key.
            node_id = old_key
            if old_key not in pipeline.nodes:
                base = old_key.rsplit("_", 1)[0]
                if base in pipeline.nodes:
                    node_id = base
            if node_id in pipeline.nodes:
                graph[new_key] = (
                    _instrument(run_id, node_id, uid, session, _observe_node(node_id, fn)),
                ) + tuple(new_deps)
            else:
                graph[new_key] = (fn,) + tuple(new_deps)

    # 4. Execute graph via the global Dask client (mirrors check_data pattern)
    sinks = [key_map[s] for s in _sink_nodes(pipeline)]
    emit(Event("PipelineGraphBuilt", {
        "run_id": run_id,
        "uid": uid,
        "session": session,
        "graph_keys": list(graph.keys()),
        "sink_nodes": sinks,
    }))

    # 4a. Shadow engine: launch Rust structural validation in parallel.
    #     Runs in a daemon thread — never blocks or delays Dask execution.
    #     Compares: node count, sink nodes, execution levels, runtime assignment.
    _shadow_pipeline_json = json.dumps({
        "nodes": {
            nid: _node_to_shadow_dict(node)
            for nid, node in pipeline.nodes.items()
        },
        "edges": [
            {
                "source": e.source,
                "destination": e.destination,
                "position": e.position,
                "output": e.output,
            }
            for e in pipeline.edges
        ],
    })
    _shadow_thread = launch_shadow_validation(
        run_id=run_id,
        uid=uid,
        session=session,
        pipeline_json=_shadow_pipeline_json,
        python_node_ids=list(pipeline.nodes.keys()),
        python_sink_nodes=[s.replace(f"{run_id}_", "") for s in sinks],
        python_graph_depth=_compute_graph_depth(pipeline),
    )

    failed = False
    cancelled = False
    _run_error_msg: str | None = None

    # Rust-driven topology walk. Default ON since 2026-04-27 — the
    # python ``dask.threaded.get`` path holds the GIL during topology
    # scheduling and was causing 3.5s "Event loop unresponsive in
    # Nanny" stalls in the trainer. Set ``DORIAN_USE_RUST_RUNNER=0``
    # to roll back. The dorian_native.run_pipeline pyo3 entry computes
    # the topological order over the Dask graph + invokes the existing
    # ``_instrument``-wrapped callables in that order. Same Python
    # operator bodies, same observability hooks, same error
    # categorisation — only the topology + scheduling moves to Rust.
    # Cancel polling stays Python-side via the daemon-thread loop
    # below; setting the flag makes ``run_pipeline`` the body of
    # that thread instead of ``dask.threaded.get``.
    _use_rust_runner = (os.environ.get("DORIAN_USE_RUST_RUNNER", "1")
                        .lower() in ("1", "true", "yes", "on"))

    # ── Intermediates cache (Tier 2 ArrowStore) ────────────────────
    # Walk the graph in topological order, compute each operator's
    # content-addressed key, look up cached outputs, and replace hit
    # entries with constants. Operators that resolve to constants
    # never reach the runner — pure short-circuit. Misses fall
    # through unchanged; we record their keys so the post-run pass
    # can persist newly-computed outputs.
    #
    # Disabled when:
    #   * ``DORIAN_CACHE_ENABLED=0`` (operator override)
    #   * dorian_native is not installed (older deploys)
    #   * The pipeline isn't a Dorian DAG with .nodes / .edges
    # In any of those cases ``elide_cached_nodes`` returns the graph
    # unchanged + zero stats, and the runner sees the original work.
    # Dataset id from the session's meta — SHA-256'd so it matches
    # the rust ``cache_compute_key``'s 64-char-hex contract. Used as
    # the cache key's ``root_hash_hex`` so two pipelines on different
    # datasets don't collide on their fit step. Without this,
    # xproduct's cross-product loop produced
    # ``imputer_cx_transform_2`` failures ("feature names should
    # match those passed during fit") because the imputer's fit
    # cache hit on dataset A while the transform ran on dataset B's
    # columns. The rust digest takes a 64-char hex input — the
    # 32-char ``did`` UUID would be rejected with
    # "root_hash_hex must be 64-char hex".
    _cache_root_hash: str | None = None
    try:
        _raw_meta_for_cache = redis.get(RedisKeys.session_meta(session))
        if _raw_meta_for_cache:
            _m_for_cache = json.loads(_raw_meta_for_cache)
            _ds_for_cache = (
                _m_for_cache.get("dataset")
                if isinstance(_m_for_cache, dict) else None
            )
            if isinstance(_ds_for_cache, dict):
                _did_for_cache = _ds_for_cache.get("did")
                if _did_for_cache:
                    import hashlib as _hl
                    _cache_root_hash = _hl.sha256(
                        str(_did_for_cache).encode("utf-8")
                    ).hexdigest()
    except Exception:
        pass
    try:
        from dorian.exec.intermediates_cache import (
            elide_cached_nodes,
            ensure_open,
            store_node_outputs,
        )
        ensure_open()
        graph, _cache_node_keys, _cache_stats = elide_cached_nodes(
            graph, pipeline, key_map=key_map,
            root_hash_hex=_cache_root_hash,
        )
        if any(_cache_stats[k] for k in ("hits", "misses", "uncacheable")):
            emit(Event("PipelineCacheElision", {
                "run_id": run_id,
                "uid": uid,
                "session": session,
                **_cache_stats,
            }))
    except Exception as _cache_exc:  # noqa: BLE001 — non-fatal
        emit(Event("PipelineCacheElisionFailed", {
            "run_id": run_id, "error": str(_cache_exc),
        }))
        _cache_node_keys = {}
    try:
        first, *rest = sinks
        # Use dask.threaded.get (synchronous thread-pool scheduler) instead of
        # executor.get (distributed Client).  The Dask cluster runs with
        # processes=False (all workers are threads in the same process), so the
        # distributed scheduler's serialization layer is unnecessary — and it
        # fails because our _instrument closures capture non-picklable objects
        # (Redis clients with _thread.RLock).  dask.threaded.get calls tasks
        # directly without serialization while still running them in parallel.
        #
        # We run dask.threaded.get in a daemon thread so the main thread can
        # poll the Redis cancel flag every 0.5 s.  This lets us respond to
        # cancellation *during* a long-running node (e.g. LlamaGuard model
        # load) instead of waiting for it to finish.  The daemon thread will
        # eventually stop on its own: subsequent nodes see the cancel flag and
        # raise PipelineCancelled, which propagates through Dask.
        _dask_exc: list[BaseException | None] = [None]
        _dask_done = threading.Event()

        def _dask_target():
            try:
                if _use_rust_runner:
                    _run_via_rust_runner(graph, sinks, cache_node_keys=_cache_node_keys)
                else:
                    # Lazy import so the default rust-runner path
                    # never imports dask at module load.
                    from dask.threaded import get as _dask_threaded_get
                    _dask_threaded_get(graph, first if not rest else sinks)
            except BaseException as _e:
                _dask_exc[0] = _e
            finally:
                _dask_done.set()

        _dask_thread = threading.Thread(target=_dask_target, daemon=True)
        _dask_thread.start()

        # Poll: wait for Dask to finish, cancel flag, or timeout.
        _deadline = time.monotonic() + _EXECUTION_TIMEOUT
        while not _dask_done.wait(timeout=0.5):
            if redis.exists(RedisKeys.cancel_run(run_id)):
                raise PipelineCancelled(run_id)
            if time.monotonic() > _deadline:
                emit(Event("PipelineRunFailed", {
                    "source": "execution.run_pipeline",
                    "run_id": run_id,
                    "uid": uid,
                    "session": session,
                    "error": f"Execution timeout ({_EXECUTION_TIMEOUT}s)",
                    "trace": "",
                    "stage": "execution",
                }))
                raise PipelineCancelled(run_id)

        # Dask finished — propagate any exception it raised.
        if _dask_exc[0] is not None:
            raise _dask_exc[0]
    except PipelineCancelled:
        failed = False
        cancelled = True
        _run_error_msg = "Cancelled by user"
    except NodeExecutionError as exc:
        # Expected — a node failed and the exception propagated to a sink.
        # Individual node failures are already recorded by _instrument; the
        # run-level check below (exc_obj.has_failures) will set the final status.
        failed = True
        _run_error_msg = str(exc)
    except Exception as exc:
        emit(Event("PipelineExecutionError", {
            "source": "execution.run_pipeline",
            "run_id": run_id,
            "uid": uid,
            "session": session,
            "error": str(exc),
            "trace": traceback.format_exc(),
        }))
        failed = True
        _run_error_msg = str(exc)

    # 5. Gather per-node states and sweep abandoned nodes.
    #    dask.threaded.get stops immediately on exception — tasks that never
    #    ran stay PENDING, and in-flight tasks may be abandoned at RUNNING.
    #    Mark both as SKIPPED so the summary is accurate and the frontend
    #    shows the correct status for every node.
    all_node_ids = list(pipeline.nodes.keys())
    node_states = _gather_node_states(run_id, all_node_ids)

    has_any_failure = failed or any(
        ns.status in (NodeStatus.FAILED, NodeStatus.SKIPPED)
        for ns in node_states.values()
    )
    if cancelled:
        # Mark remaining PENDING/RUNNING nodes as CANCELLED
        for nid in all_node_ids:
            ns = node_states.get(nid)
            if ns and ns.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                _patch_node_state(run_id, nid, status=NodeStatus.CANCELLED, end_time=time.time())
                _stream_sync(uid, session, {
                    "event": "pipeline/node/cancelled",
                    "run_id": run_id,
                    "node_id": nid,
                    "status": "CANCELLED",
                })
        node_states = _gather_node_states(run_id, all_node_ids)
        # Clean up the cancellation flag
        redis.delete(RedisKeys.cancel_run(run_id))
    elif has_any_failure:
        for nid in all_node_ids:
            ns = node_states.get(nid)
            if ns and ns.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                _patch_node_state(run_id, nid, status=NodeStatus.SKIPPED, end_time=time.time())
                _stream_sync(uid, session, {
                    "event": "pipeline/node/skipped",
                    "run_id": run_id,
                    "node_id": nid,
                    "status": "SKIPPED",
                    "reason": "pipeline failed — node was not reached",
                })
        # Re-gather after patching
        node_states = _gather_node_states(run_id, all_node_ids)
        failed = True

    # 6. Evaluation — compute metrics for successful runs.
    #    Uses the KB to resolve metric operators dynamically (no hard-coded
    #    sklearn imports) and the default evaluation procedure resolver.
    metrics: Dict[str, float] = {}
    if not failed and not cancelled:
        try:
            metrics = _evaluate_pipeline_sync(run_id, uid, session, pipeline, node_states)
        except Exception as exc:
            emit(Event("EvaluationMetricsFailed", {"run_id": run_id, "error": str(exc)}))

    # 7. Finalise — write the monolithic blob with gathered node states so
    #    downstream consumers (frontend summary, StateTracker) see everything.
    exc_obj = _read_execution(run_id)
    if cancelled:
        final_status = PipelineRunStatus.CANCELLED
    elif failed:
        final_status = PipelineRunStatus.FAILED
    else:
        final_status = PipelineRunStatus.SUCCESS
    if exc_obj:
        exc_obj.status = final_status
        exc_obj.end_time = time.time()
        exc_obj.node_states = node_states
        _write_execution(exc_obj)

    # Feed the observability collector — find the first failed node for diagnostics
    _first_failed_node: str | None = None
    for _nid, _ns in node_states.items():
        if _ns.status == NodeStatus.FAILED:
            _first_failed_node = _nid
            break

    _record_run_to_obs(
        run_id, uid, session,
        status="cancelled" if cancelled else ("failed" if failed else "completed"),
        start_ts=_run_start_ts,
        error=_run_error_msg,
        stage="execution",
        source=_source,
        pipeline_id=pipeline_id,
        node_count=len(all_node_ids),
        node_types=_summarise_node_types(pipeline),
        failed_node=_first_failed_node,
    )
    try:
        pass  # observability recording done above via _record_run_to_obs
    except Exception:
        pass  # non-fatal — observability is best-effort

    summary = exc_obj.summary() if exc_obj else {}
    if cancelled:
        event_type = "PipelineRunCancelled"
        stream_event = "pipeline/run/cancelled"
    elif failed:
        event_type = "PipelineRunFailed"
        stream_event = "pipeline/run/failed"
    else:
        event_type = "PipelineRunCompleted"
        stream_event = "pipeline/run/completed"

    # Also forward dataset_id so the experiment store recorder can persist
    # evaluations for synthetic sessions (RL generator) without needing to
    # re-read session meta in the async handler.
    _dataset_id: str | None = None
    try:
        _raw_meta_final = redis.get(RedisKeys.session_meta(session))
        if _raw_meta_final:
            _m = json.loads(_raw_meta_final)
            _ds = _m.get("dataset") if isinstance(_m, dict) else None
            if isinstance(_ds, dict):
                _dataset_id = _ds.get("did")
    except Exception:
        pass

    emit(Event(event_type, {
        "source": "execution.run_pipeline",
        "run_id": run_id,
        "uid": uid,
        "session": session,
        "status": str(final_status),
        "summary": summary,
        "metrics": metrics,
        # pipeline_id is forwarded so downstream handlers (notably the
        # experiment store recorder) can persist evaluations without
        # having to reconstruct the id from Redis session meta — which
        # breaks for synthetic sessions like the RL generator's
        # ``rl:round-N:{did}``, where meta carries only ``dataset``.
        "pipeline_id": pipeline_id,
        "dataset_id": _dataset_id,
    }))
    _stream_sync(uid, session, {
        "event": stream_event,
        "run_id": run_id,
        "status": str(final_status),
        "summary": summary,
        "metrics": json.dumps(metrics) if metrics else "{}",
    })

    # Trial-session stream cleanup. RL / AutoML / cross-product
    # trials don't have a frontend WebSocket consumer reading +
    # acking the per-run event stream, so the stream would
    # accumulate forever. The prior incident: 12,684 orphan
    # streams cost ~9.5 GiB of redis memory before we noticed.
    #
    # The 5-minute reaper in dorian-engines is the safety net for
    # crashes mid-execution; this is the immediate-cleanup hook
    # that runs as soon as the trial's terminal event is written.
    # Combined, no orphan streams ever accumulate.
    if uid in ("xproduct", "rl", "automl") or session.startswith(("xproduct:", "rl:", "automl:")):
        try:
            redis.delete(RedisKeys.stream(uid, session))
        except Exception:
            pass  # non-fatal — reaper will catch it

    # Push a persistent notification (survives reconnect / be-right-back).
    try:
        from dorian.infra.notifications import push_notification
        import asyncio as _aio
        _duration = round(time.time() - _run_start_ts, 1) if _run_start_ts else None
        _dur_str = f" in {_duration}s" if _duration else ""
        if cancelled:
            _note = {"kind": "warning", "title": "Pipeline cancelled", "message": f"Run {run_id[:8]}… was cancelled{_dur_str}"}
        elif failed:
            _err_short = (summary.get("error") or "unknown error")[:100] if isinstance(summary, dict) else "unknown error"
            _note = {"kind": "error", "title": "Pipeline failed", "message": f"{_err_short}{_dur_str}"}
        else:
            _note = {"kind": "success", "title": "Pipeline completed", "message": f"Finished successfully{_dur_str}"}
        _note["meta"] = {"run_id": run_id}
        # push_notification is async — bridge from sync context
        from backend.events import _loop as _evt_loop
        if _evt_loop is not None and _evt_loop.is_running():
            _aio.run_coroutine_threadsafe(push_notification(uid, session, _note), _evt_loop)
        else:
            pass  # skip if no event loop (test context)
    except Exception:
        pass  # never let notification failure break execution

    # 8. Persist last run info to session meta for reconnect restoration.
    #    Always write — not just on metrics — so stale-run detection works.
    try:
        raw_meta = redis.get(RedisKeys.session_meta(session))
        if raw_meta:
            meta = json.loads(raw_meta)
            meta["lastRun"] = {
                "run_id": run_id,
                "metrics": metrics or {},
                "status": str(final_status),
                "ts": time.time(),
            }
            redis.set(RedisKeys.session_meta(session), json.dumps(meta))
    except Exception as exc:
        emit(Event("LastRunPersistFailed", {"run_id": run_id, "error": str(exc)}))

    return {"run_id": run_id, "status": str(final_status)}


# ---------------------------------------------------------------------------
# Async entry-point (event handler)
# ---------------------------------------------------------------------------

async def handle_pipeline_execution(payload: dict) -> None:
    """
    Triggered by the queue bridge after an 'ExecutePipeline' event.

    1. Validates and fetches the pipeline document from the docstore.
    2. Creates a PipelineExecution state object in Redis.
    3. Notifies the frontend via the Redis stream.
    4. Offloads run_pipeline() to a background thread (non-blocking).
    """
    uid = payload.get("uid")
    session = payload.get("session")
    request_id = payload.get("requestId")
    pipeline_oid_str = payload.get("pipelineId")

    # Lazy import to avoid top-level circular — the pipeline module is pulled
    # into backend.events.handlers before envs.py finishes wiring up.
    from backend.envs import expdb as db

    # 1. Validate pipelineId
    if not pipeline_oid_str:
        await aemit(Event("PipelineExecutionError", {
            "source": "execution.handle_pipeline_execution",
            "error": "Missing 'pipelineId' in payload",
        }))
        await _stream_async(uid, session, {
            "event": "pipeline/run/error",
            "reason": "missing_pipeline_id",
            "request_id": request_id,
        })
        return

    # 2. Fetch the pipeline — probe Postgres first (RL-generated & user-saved
    #    pipelines live there under a logical uuid4().hex id), then fall back
    #    to the docstore ObjectId (legacy / imported pipelines), then session meta
    #    (user-composed pipelines whose pipelineId is a client-generated headId).
    pipeline_doc = None

    try:
        from backend.envs import get_pg_pool
        pg_pool = await get_pg_pool()
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, session, task, dag, provenance "
                "FROM pipelines WHERE id = $1",
                pipeline_oid_str,
            )
        if row is not None:
            dag_json = row["dag"]
            if isinstance(dag_json, str):
                dag_json = json.loads(dag_json)
            pipeline_doc = {
                "id": row["id"],
                "pipeline_id": row["id"],
                "task": row["task"],
                "provenance": row["provenance"],
                "nodes": dag_json.get("nodes", []),
                "edges": dag_json.get("edges", []),
            }
    except Exception:
        await aemit(Event("PostgresPipelineLookupFailed", {
            "pipeline_id": pipeline_oid_str,
            "error": traceback.format_exc(),
        }))

    if pipeline_doc is None:
        # Direct pipelineId lookup — the document store keys on TEXT.
        pipeline_doc = await db.pipelines.find_one({"_id": pipeline_oid_str})

    if pipeline_doc is None:
        # Look up from the pipelineHistory stored in session meta
        raw_meta = await aioredis.get(RedisKeys.session_meta(session))
        if raw_meta:
            meta = json.loads(raw_meta)
            history = meta.get("pipelineHistory") or {}
            all_versions = history.get("pipelines") or []

            # Prefer an exact id match; fall back to the current head
            for version in all_versions:
                if version.get("id") == pipeline_oid_str:
                    pipeline_doc = version
                    break

            if pipeline_doc is None and history.get("headId") == pipeline_oid_str:
                # headId matches — use the head version
                head_id = history["headId"]
                for version in all_versions:
                    if version.get("id") == head_id:
                        pipeline_doc = version
                        break

    if pipeline_doc is None:
        await aemit(Event("PipelineRunRequestFailed", {
            "source": "execution.handle_pipeline_execution",
            "uid": uid,
            "session": session,
            "request_id": request_id,
            "error": f"Pipeline {pipeline_oid_str} not found in database or session",
        }))
        await _stream_async(uid, session, {
            "event": "pipeline/run/error",
            "reason": "pipeline_not_found",
            "request_id": request_id,
        })
        return

    # 3. Create execution state
    run_id = str(uuid4())
    logical_pipeline_id = str(
        pipeline_doc.get("id")
        or pipeline_doc.get("uuid")
        or pipeline_oid_str
    )

    execution = PipelineExecution(
        run_id=run_id,
        session_id=session,
        pipeline_id=logical_pipeline_id,
        uid=uid,
        status=PipelineRunStatus.PENDING,
    )
    await StateTracker.create_run(execution)

    await aemit(Event("PipelineRunInitialised", {
        "run_id": run_id,
        "uid": uid,
        "session": session,
        "pipeline_id": logical_pipeline_id,
        "request_id": request_id,
    }))
    await _stream_async(uid, session, {
        "event": "pipeline/run/initialised",
        "run_id": run_id,
        "pipeline_id": logical_pipeline_id,
        "request_id": request_id,
    })

    # 4. Serialise the pipeline document for the sync worker thread.
    #    Convert BSON ObjectId (if present) to string so json.dumps works.
    if "_id" in pipeline_doc:
        pipeline_doc["_id"] = str(pipeline_doc["_id"])
    # docstore docs may carry datetime (created_at/updated_at) and other
    # non-JSON-native BSON types — stringify them via a permissive default.
    pipeline_json = json.dumps(pipeline_doc, default=str)

    # Consume vault passphrase nonce (if present).
    # The frontend sends a nonce in the ExecutePipeline payload; the actual
    # passphrase lives in a 60-second TTL Redis key set via POST /vault/nonce.
    vault_passphrase = None
    vault_nonce = payload.get("vaultNonce")
    await aemit(Event("VaultNonceDebug", {
        "run_id": run_id,
        "vault_nonce_present": vault_nonce is not None,
        "payload_keys": list(payload.keys()),
    }))
    if vault_nonce:
        from dorian.vault.storage import consume_passphrase_nonce
        vault_passphrase = await consume_passphrase_nonce(vault_nonce)
        await aemit(Event("VaultNonceConsumed", {
            "run_id": run_id,
            "passphrase_obtained": vault_passphrase is not None,
        }))

    # Fire-and-forget: run orchestration in a background thread so the event
    # loop is not blocked.  run_pipeline calls dask.threaded.get() internally.
    asyncio.create_task(asyncio.to_thread(
        run_pipeline, run_id, uid, session, pipeline_json,
        vault_passphrase=vault_passphrase,
        pipeline_id=logical_pipeline_id,
    ))


# ---------------------------------------------------------------------------
# Event-handler bridge — invoked by the rust pipeline handler when
# the user clicks "Run". The python pipeline.py handler module that
# used to call this was retired (#71 / #81 ports).
# ---------------------------------------------------------------------------

async def handle_pipeline_run_clicked(
    uid: str,
    session: str,
    payload: dict,
    request_id: str,
) -> None:
    """Bridge the 'PipelineRunClicked' event to handle_pipeline_execution.

    The event handler layer (dorian/event/handlers/pipeline.py) calls this
    with the decomposed event fields.  We reassemble a payload dict and
    delegate to handle_pipeline_execution which handles all the heavy lifting.

    The frontend typically sends the current head pipeline's id as the
    reference; we probe several common key names so both user-composed and
    seeder-imported pipelines are handled.
    """
    pipeline_id = (
        payload.get("pipelineId")
        or payload.get("headId")
        or payload.get("id")
        or payload.get("uuid")
    )
    await handle_pipeline_execution({
        "uid": uid,
        "session": session,
        "requestId": request_id,
        "pipelineId": pipeline_id,
        "vaultNonce": payload.get("vaultNonce"),
    })
