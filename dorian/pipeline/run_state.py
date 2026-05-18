"""
dorian/pipeline/run_state.py
----------------------------
Execution state persistence — all Redis I/O for pipeline run tracking.

Extracted from ``execution.py`` to isolate state management from
orchestration logic.  All function signatures are identical to the originals.
"""
from __future__ import annotations

import json
import pickle
import time
from pathlib import Path
from typing import Any, Dict, Optional

import psutil as _psutil

from backend.config import config
from backend.envs import redis
from backend.events import Event, emit
from dorian.models.execution import (
    NodeState,
    NodeStatus,
    PipelineExecution,
    PipelineRunStatus,
)
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN

_exec_cfg = config.execution
_RESULT_TTL = int(_exec_cfg.result_ttl)
_RESULT_DIR = Path(str(_exec_cfg.result_dir))
_INLINE_LIMIT = int(_exec_cfg.inline_limit)

_obs_process = None

# Background RSS sampling — avoids 2 × psutil.memory_info() GIL-blocking
# calls per Dask node.  Same pattern as backend/events.py.
import threading as _threading
_sampled_rss_run: int = 0

def _rss_sampler_run(interval: float = 1.0) -> None:
    global _sampled_rss_run, _obs_process
    while True:
        try:
            if _obs_process is None:
                _obs_process = _psutil.Process()
            _sampled_rss_run = _obs_process.memory_info().rss
        except Exception:
            pass
        _threading.Event().wait(interval)

_rss_thread_run = _threading.Thread(target=_rss_sampler_run, daemon=True, name="rss-sampler-run")
_rss_thread_run.start()


# ---------------------------------------------------------------------------
# Stream helpers (sync — used inside Dask workers)
# ---------------------------------------------------------------------------

def _stream_sync(uid: str, session: str, msg: dict) -> None:
    """Push msg to the per-user stream from a sync (Dask worker) context.

    Silently skips RL-generated pipeline runs (uid="system", session="rl:...")
    to avoid flooding the WebSocket queue with thousands of node-level events
    that no frontend consumer will ever read.
    """
    if uid == "system" and (session or "").startswith("rl:"):
        return
    redis.xadd(RedisKeys.stream(uid, session), _to_fields(msg), maxlen=STREAM_MAXLEN, approximate=True)


def _to_fields(msg: dict) -> dict:
    return {
        str(k): json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        for k, v in msg.items()
    }


# ---------------------------------------------------------------------------
# Synchronous Redis helpers (for use inside Dask workers)
# ---------------------------------------------------------------------------

_STALE_RUN_TIMEOUT = 60 * 30  # 30 minutes — if a run has been RUNNING longer, it's dead


def _read_execution(run_id: str) -> Optional[PipelineExecution]:
    raw = redis.get(RedisKeys.execution(run_id))
    return PipelineExecution.model_validate_json(raw) if raw else None


def _write_execution(execution: PipelineExecution) -> None:
    redis.set(RedisKeys.execution(execution.run_id), execution.model_dump_json(), ex=_RESULT_TTL)


def _store_result_sync(run_id: str, node_id: str, value: Any) -> Optional[str]:
    key = RedisKeys.result(run_id, node_id)
    try:
        encoded = json.dumps(value).encode()
        if len(encoded) <= _INLINE_LIMIT:
            redis.set(key, encoded, ex=_RESULT_TTL)
            return f"redis:{key}"
    except (TypeError, ValueError):
        pass
    path = _RESULT_DIR / run_id / f"{node_id}.pkl"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "wb") as fh:
            pickle.dump(value, fh)
        ref = f"file:{path}"
        redis.set(key, ref.encode(), ex=_RESULT_TTL)
        return ref
    except Exception as exc:
        emit(Event("ResultPersistFailed", {
            "source": "execution._store_result_sync",
            "run_id": run_id,
            "node_id": node_id,
            "error": str(exc),
        }))
        return None


def _patch_node_state(run_id: str, node_id: str, **patch: Any) -> None:
    """Update a single NodeState stored in its own per-node Redis key.

    Each node is stored at ``execution:{run_id}:node:{node_id}``.  Because only
    one Dask worker thread ever touches a given node_id at a time, there is zero
    contention and no need for WATCH/MULTI optimistic locking.
    """
    key = RedisKeys.node_state(run_id, node_id)
    raw = redis.get(key)
    ns = NodeState.model_validate_json(raw) if raw else NodeState(node_id=node_id)
    for field_name, value in patch.items():
        setattr(ns, field_name, value)
    redis.set(key, ns.model_dump_json(), ex=_RESULT_TTL)


def _gather_node_states(run_id: str, node_ids: list[str]) -> Dict[str, NodeState]:
    """Read all per-node keys for a run and return the combined dict.

    Uses a pipelined batch of GETs for a single round-trip (compatible with
    Redis ACLs that only allow ``+get``; MGET is a separate command).
    """
    if not node_ids:
        return {}
    pipe = redis.pipeline(transaction=False)
    for nid in node_ids:
        pipe.get(RedisKeys.node_state(run_id, nid))
    raw_values = pipe.execute()
    states: Dict[str, NodeState] = {}
    for nid, raw in zip(node_ids, raw_values):
        if raw:
            states[nid] = NodeState.model_validate_json(raw)
    return states


def cleanup_stale_run(run_id: str, uid: str, session: str) -> bool:
    """Mark a zombie run as FAILED if it's been RUNNING beyond the timeout.

    Returns True if cleanup was performed (the run was stale), False otherwise.
    Called at the start of a new pipeline execution and during session seed to
    ensure the frontend never shows a permanently-stuck RUNNING state.
    """
    exc_obj = _read_execution(run_id)
    if not exc_obj or exc_obj.status != PipelineRunStatus.RUNNING:
        return False

    elapsed = time.time() - (exc_obj.start_time or 0)
    if elapsed < _STALE_RUN_TIMEOUT:
        return False

    emit(Event("StaleRunDetected", {
        "source": "execution.cleanup_stale_run",
        "run_id": run_id,
        "elapsed": elapsed,
    }))

    # Sweep node states — mark any still RUNNING/PENDING as FAILED
    node_ids = list(exc_obj.node_states.keys()) if exc_obj.node_states else []
    # Also check per-node keys (post-expansion node IDs may differ)
    for nid in node_ids:
        key = RedisKeys.node_state(run_id, nid)
        raw = redis.get(key)
        if raw:
            ns = NodeState.model_validate_json(raw)
            if ns.status in (NodeStatus.PENDING, NodeStatus.RUNNING):
                _patch_node_state(run_id, nid, status=NodeStatus.FAILED,
                                  end_time=time.time(),
                                  error="Process terminated unexpectedly")
                _stream_sync(uid, session, {
                    "event": "pipeline/node/failed",
                    "run_id": run_id,
                    "node_id": nid,
                    "status": "FAILED",
                    "error": "Process terminated unexpectedly",
                })

    exc_obj.status = PipelineRunStatus.FAILED
    exc_obj.end_time = time.time()
    _write_execution(exc_obj)
    _stream_sync(uid, session, {
        "event": "pipeline/run/failed",
        "run_id": run_id,
        "error": "Pipeline timed out — the backend process may have crashed",
    })
    return True


def _load_result_sync(ref: str) -> Any:
    """Load a stored node result from its reference string.

    Handles two storage formats:
      - ``'redis:<key>'``  — JSON-encoded value in Redis
      - ``'file:<path>'``  — pickled object on disk

    Returns ``None`` if the result cannot be loaded.
    """
    if not ref:
        return None
    if ref.startswith("redis:"):
        raw = redis.get(ref[len("redis:"):])
        if raw:
            return json.loads(raw)
    elif ref.startswith("file:"):
        path = ref[len("file:"):]
        try:
            with open(path, "rb") as fh:
                return pickle.load(fh)  # noqa: S301
        except Exception as exc:
            emit(Event("ResultLoadFailed", {"path": path, "error": str(exc)}))
    return None


def _node_running_sync(run_id: str, node_id: str) -> None:
    _patch_node_state(run_id, node_id, status=NodeStatus.RUNNING, start_time=time.time())


def _node_success_sync(run_id: str, node_id: str, ref: Optional[str]) -> None:
    _patch_node_state(run_id, node_id, status=NodeStatus.SUCCESS, end_time=time.time(), result_ref=ref)


def _node_failed_sync(run_id: str, node_id: str, error: str) -> None:
    _patch_node_state(run_id, node_id, status=NodeStatus.FAILED, end_time=time.time(), error=error)


# ---------------------------------------------------------------------------
# Observability wrapper  (innermost — measures only the operator execution)
# ---------------------------------------------------------------------------

def _observe_node(node_id: str, fn):
    """Wrap a node callable with per-task timing metrics.

    Captures wall-clock time, per-thread CPU time (accurate for Dask worker
    threads), and process RSS memory delta.  Re-raises any exception so the
    outer ``_instrument`` wrapper can handle failures normally.

    Compose as the *innermost* layer around the actual callable::

        _instrument(run_id, node_id, uid, session, _observe_node(node_id, fn))
    """
    def wrapper(*args, **kwargs):
        _wall_start = time.perf_counter()
        _cpu_start  = time.thread_time()   # accurate per Dask worker thread
        _rss_before = _sampled_rss_run
        _error = False
        try:
            return fn(*args, **kwargs)
        except Exception:
            _error = True
            raise
        finally:
            _wall_s  = time.perf_counter() - _wall_start
            _cpu_s   = time.thread_time()  - _cpu_start
            _rss_after = _sampled_rss_run
            emit(Event("NodeObservability", {
                "node_id": node_id,
                "wall_s": round(_wall_s, 3),
                "cpu_s": round(_cpu_s, 3),
                "rss_mb": round(_rss_after / (1024 ** 2), 1),
                "delta_mb": round((_rss_after - _rss_before) / (1024 ** 2), 1),
                "error": _error,
            }))
    return wrapper


# ---------------------------------------------------------------------------
# Trace output helper
# ---------------------------------------------------------------------------

_IMAGE_EXTS = {".png", ".svg", ".jpg", ".jpeg", ".gif"}
_TEXT_EXTS = {".txt", ".log"}


def _build_trace_output(file_paths: list | tuple) -> dict | None:
    """Transform a list of file paths into structured trace output.

    Returns ``{type: "trace_output", images: [...], logs: [...]}`` or None
    if the input doesn't look like file paths.
    """
    if not file_paths or not all(isinstance(s, str) for s in file_paths):
        return None

    from pathlib import Path
    has_known_ext = any(
        Path(s).suffix.lower() in _IMAGE_EXTS | _TEXT_EXTS for s in file_paths
    )
    if not has_known_ext:
        return None

    images, logs = [], []
    for path in file_paths:
        ext = Path(path).suffix.lower()
        url = "/outputs/" + path.lstrip("/").lstrip("./").split("outputs/", 1)[-1]
        if ext in _IMAGE_EXTS:
            images.append(url)
        elif ext in _TEXT_EXTS:
            try:
                logs.append({"path": path, "content": Path(path).read_text()})
            except Exception:
                logs.append({"path": path, "content": "(unreadable)"})

    return {"type": "trace_output", "images": images, "logs": logs}


# ---------------------------------------------------------------------------
# Instrumentation wrapper  (outer — emits events + persists results)
# ---------------------------------------------------------------------------

def _instrument(run_id: str, node_id: str, uid: str, session: str, fn):
    """Wrap a node callable to emit events, persist results, and propagate failures.

    Failure propagation strategy:
      - When ``fn`` raises, we mark the node FAILED, emit events, then **re-raise**
        as ``NodeExecutionError``.  Dask propagates the exception to all downstream
        tasks automatically.
      - When an upstream ``NodeExecutionError`` arrives as an argument (Dask raises
        it before calling us), we mark this node SKIPPED and re-raise immediately.
        This avoids misleading "node failed" events for nodes that never ran.
    """
    import traceback
    from dorian.pipeline.execution import NodeExecutionError, PipelineCancelled

    def wrapper(*args, **kwargs):
        # --- Check for cancellation first ---
        if redis.exists(RedisKeys.cancel_run(run_id)):
            _patch_node_state(run_id, node_id, status=NodeStatus.CANCELLED, end_time=time.time())
            _stream_sync(uid, session, {
                "event": "pipeline/node/cancelled",
                "run_id": run_id,
                "node_id": node_id,
                "status": "CANCELLED",
            })
            raise PipelineCancelled(run_id)

        # --- Check for upstream failures ---
        # Dask will raise the exception from a failed dependency when its result
        # is accessed.  But for argument-level detection (e.g. tuple entries),
        # we also scan positional args for NodeExecutionError instances that may
        # have leaked through slice lambdas.
        for arg in args:
            if isinstance(arg, NodeExecutionError):
                _patch_node_state(run_id, node_id, status=NodeStatus.SKIPPED, end_time=time.time())
                _stream_sync(uid, session, {
                    "event": "pipeline/node/skipped",
                    "run_id": run_id,
                    "node_id": node_id,
                    "status": "SKIPPED",
                    "reason": f"upstream node '{arg.node_id}' failed",
                })
                raise arg

        t0 = time.time()
        _node_running_sync(run_id, node_id)
        _stream_sync(uid, session, {
            "event": "pipeline/node/started",
            "run_id": run_id,
            "node_id": node_id,
            "status": "RUNNING",
            "start_time": str(t0),
        })
        emit(Event("NodeExecutionStarted", {"run_id": run_id, "node_id": node_id, "uid": uid, "session": session}))

        try:
            result = fn(*args, **kwargs)
        except NodeExecutionError:
            # Upstream failure propagated by Dask — mark SKIPPED, not FAILED
            _patch_node_state(run_id, node_id, status=NodeStatus.SKIPPED, end_time=time.time())
            _stream_sync(uid, session, {
                "event": "pipeline/node/skipped",
                "run_id": run_id,
                "node_id": node_id,
                "status": "SKIPPED",
            })
            raise
        except PipelineCancelled:
            _patch_node_state(run_id, node_id, status=NodeStatus.CANCELLED, end_time=time.time())
            _stream_sync(uid, session, {
                "event": "pipeline/node/cancelled",
                "run_id": run_id,
                "node_id": node_id,
                "status": "CANCELLED",
            })
            raise
        except Exception as exc:
            tb = traceback.format_exc()
            _node_failed_sync(run_id, node_id, tb)
            _stream_sync(uid, session, {
                "event": "pipeline/node/failed",
                "run_id": run_id,
                "node_id": node_id,
                "status": "FAILED",
                "error": str(exc),
                "trace": tb,
                "duration": str(time.time() - t0),
            })
            emit(Event("NodeExecutionFailed", {
                "source": "execution._instrument",
                "run_id": run_id,
                "node_id": node_id,
                "uid": uid,
                "session": session,
                "error": str(exc),
                "trace": tb,
            }))
            raise NodeExecutionError(node_id, str(exc)) from exc

        ref = _store_result_sync(run_id, node_id, result)
        _node_success_sync(run_id, node_id, ref)
        completed_msg: dict = {
            "event": "pipeline/node/completed",
            "run_id": run_id,
            "node_id": node_id,
            "status": "SUCCESS",
            "result_ref": ref or "",
            "duration": str(time.time() - t0),
        }
        # Inline the result for printout/visualizer nodes so the frontend
        # can display it without a separate fetch round-trip.
        if "printout" in node_id and isinstance(result, dict):
            try:
                completed_msg["output"] = json.dumps(result)
            except (TypeError, ValueError):
                pass

        # Inline trace output for model tracing nodes.
        # The trace method returns a list of file paths (images + logs).
        # Method shortcuts return (instance, actual_result) tuples — unwrap.
        # Transform into structured {type, images, logs} for the frontend.
        if "_cx_trace_" in node_id:
            trace_data = result[1] if isinstance(result, tuple) and len(result) == 2 else result
            if isinstance(trace_data, (list, tuple)):
                trace_data = list(trace_data)
            trace_output = _build_trace_output(trace_data) if isinstance(trace_data, list) else None
            if trace_output:
                try:
                    completed_msg["output"] = json.dumps(trace_output)
                    # Also emit a dedicated event for the parent operator node
                    # so the frontend can attribute trace output to the canvas node.
                    parent_id = node_id.split("_cx_")[0]
                    _stream_sync(uid, session, {
                        "event": "pipeline/node/trace-output",
                        "run_id": run_id,
                        "node_id": parent_id,
                        "output": json.dumps(trace_output),
                    })
                except (TypeError, ValueError):
                    pass
        _stream_sync(uid, session, completed_msg)
        emit(Event("NodeExecutionCompleted", {"run_id": run_id, "node_id": node_id, "uid": uid, "session": session, "result_ref": ref}))
        return result

    return wrapper
