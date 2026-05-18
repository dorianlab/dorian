"""
dorian/pipeline/shadow.py
--------------------------
Shadow engine integration for Phase 1.7.

Runs the Rust engine's structural validation in parallel with the Python/Dask
execution pipeline, comparing:
- Node count and IDs
- Execution levels and topological order
- Sink node identification
- Runtime assignment per node
- Parse and plan timing

The shadow engine does NOT execute operators — it validates the graph structure
and scheduling plan. Full execution shadowing comes in Phase 3 when the Rust
engine's runtime dispatch layer (subprocess pool) is wired up.

Usage
-----
Called from ``run_pipeline()`` in ``execution.py`` when the config flag
``execution.shadow_rust_engine`` is truthy.  Runs in a background thread
to avoid adding latency to the critical path.

Discrepancies are logged via the event bus (``ShadowEngineDiscrepancy``)
and to the observability collector.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

from backend.events import Event, emit


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def _shadow_enabled() -> bool:
    """Check if shadow engine comparison is enabled via config.

    Defaults to False — opt-in via ``config.execution.shadow_rust_engine``.
    """
    try:
        from backend.config import config
        return bool(getattr(config.execution, "shadow_rust_engine", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Rust native bridge
# ---------------------------------------------------------------------------

def _get_native():
    """Import the dorian_native extension module.

    Returns None if the module is not available (e.g. not compiled yet).
    """
    try:
        import dorian_native
        return dorian_native
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Shadow validation (structural comparison)
# ---------------------------------------------------------------------------

def shadow_validate(
    run_id: str,
    uid: str,
    session: str,
    pipeline_json: str,
    python_node_ids: List[str],
    python_sink_nodes: List[str],
    python_graph_depth: int,
) -> Optional[Dict[str, Any]]:
    """Run the Rust engine's structural validation and compare with Python.

    This is the main entry point for the shadow engine.  It:
    1. Parses the pipeline JSON into a Rust ProcessGraph
    2. Computes an execution plan (topological sort, levels, sinks)
    3. Compares the Rust plan against the Python execution metadata
    4. Returns a discrepancy report (or None if everything matches)

    Called from a background thread — safe to use synchronous operations.
    """
    native = _get_native()
    if native is None:
        emit(Event("ShadowEngineSkipped", {
            "source": "shadow.shadow_validate",
            "run_id": run_id,
            "reason": "dorian_native module not available",
        }))
        return None

    t0 = time.perf_counter()
    discrepancies: List[str] = []

    # Step 1: Validate and build Rust execution plan.
    try:
        plan_json = native.shadow_validate_plan(pipeline_json)
        plan = json.loads(plan_json)
    except Exception as exc:
        emit(Event("ShadowEngineError", {
            "source": "shadow.shadow_validate",
            "run_id": run_id,
            "error": f"Rust plan validation failed: {exc}",
            "trace": traceback.format_exc(),
        }))
        return {"error": str(exc)}

    # Step 2: Compare graphs.
    try:
        compare_json = native.shadow_compare_graphs(
            pipeline_json,
            python_node_ids,
            python_sink_nodes,
            python_graph_depth,
        )
        comparison = json.loads(compare_json)
    except Exception as exc:
        emit(Event("ShadowEngineError", {
            "source": "shadow.shadow_validate",
            "run_id": run_id,
            "error": f"Rust graph comparison failed: {exc}",
            "trace": traceback.format_exc(),
        }))
        return {"error": str(exc)}

    # Step 3: Identify discrepancies.
    if not plan.get("valid"):
        discrepancies.append(
            f"Rust graph validation failed: {plan.get('errors', [])}"
        )

    if not comparison.get("node_count_match"):
        discrepancies.append(
            f"Node count mismatch: Python={comparison.get('python_node_count')} "
            f"vs Rust={comparison.get('rust_node_count')}"
        )

    if not comparison.get("sink_match"):
        discrepancies.append(
            f"Sink nodes mismatch: Python={comparison.get('python_sink_nodes')} "
            f"vs Rust={comparison.get('rust_sink_nodes')}"
        )

    if not comparison.get("level_count_match"):
        discrepancies.append(
            f"Graph depth mismatch: Python={comparison.get('python_depth')} "
            f"vs Rust={comparison.get('rust_depth')}"
        )

    missing = comparison.get("missing_in_rust", [])
    if missing:
        discrepancies.append(
            f"Nodes in Python but missing in Rust: {missing}"
        )

    extra = comparison.get("extra_in_rust", [])
    if extra:
        discrepancies.append(
            f"Nodes in Rust but not in Python: {extra}"
        )

    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Step 4: Report.
    report = {
        "run_id": run_id,
        "valid": plan.get("valid", False),
        "discrepancies": discrepancies,
        "discrepancy_count": len(discrepancies),
        "rust_plan": {
            "node_count": plan.get("node_count"),
            "depth": plan.get("depth"),
            "max_concurrency": plan.get("max_concurrency"),
            "sink_nodes": plan.get("sink_nodes"),
            "parse_time_ms": plan.get("parse_time_ms"),
            "plan_time_ms": plan.get("plan_time_ms"),
        },
        "comparison": comparison,
        "total_shadow_time_ms": elapsed_ms,
    }

    if discrepancies:
        emit(Event("ShadowEngineDiscrepancy", {
            "source": "shadow.shadow_validate",
            "run_id": run_id,
            "uid": uid,
            "session": session,
            "discrepancies": discrepancies,
            "discrepancy_count": len(discrepancies),
            "rust_plan": report["rust_plan"],
            "total_shadow_time_ms": elapsed_ms,
        }))
    else:
        emit(Event("ShadowEngineMatch", {
            "source": "shadow.shadow_validate",
            "run_id": run_id,
            "uid": uid,
            "session": session,
            "rust_plan": report["rust_plan"],
            "total_shadow_time_ms": elapsed_ms,
        }))

    return report


# ---------------------------------------------------------------------------
# Background runner (non-blocking integration with run_pipeline)
# ---------------------------------------------------------------------------

def launch_shadow_validation(
    run_id: str,
    uid: str,
    session: str,
    pipeline_json: str,
    python_node_ids: List[str],
    python_sink_nodes: List[str],
    python_graph_depth: int,
) -> Optional[threading.Thread]:
    """Launch shadow validation in a background daemon thread.

    Returns the thread handle (or None if shadow engine is disabled).
    The thread runs ``shadow_validate()`` and emits events on completion.
    It is a daemon thread — it will not block process shutdown.

    Called from ``run_pipeline()`` right before the Dask execution starts.
    The shadow engine runs in parallel with Dask; results are collected
    after Dask completes.
    """
    if not _shadow_enabled():
        return None

    def _run():
        try:
            shadow_validate(
                run_id=run_id,
                uid=uid,
                session=session,
                pipeline_json=pipeline_json,
                python_node_ids=python_node_ids,
                python_sink_nodes=python_sink_nodes,
                python_graph_depth=python_graph_depth,
            )
        except Exception as exc:
            # Catch-all: shadow engine must never crash the pipeline.
            emit(Event("ShadowEngineError", {
                "source": "shadow.launch_shadow_validation",
                "run_id": run_id,
                "error": f"Unhandled exception: {exc}",
                "trace": traceback.format_exc(),
            }))

    thread = threading.Thread(
        target=_run,
        daemon=True,
        name=f"shadow-engine-{run_id[:8]}",
    )
    thread.start()
    return thread
