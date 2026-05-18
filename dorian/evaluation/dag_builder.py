"""
dorian/evaluation/dag_builder.py
---------------------------------
Builds evaluation DAGs — small Dask task graphs that compute metrics from
pipeline execution outputs.

Each evaluation procedure type (hold-out, k-fold, custom, none) produces a
dict-based Dask graph executed via ``dask.threaded.get()``, giving free
parallelism for independent metric computations.

Integration points:
  - Called from ``dorian.pipeline.execution._evaluate_pipeline_sync``
    after a pipeline run completes.
  - Metrics dict flows to ``ExperimentStore.record_evaluation_batch``.
  - RL generator receives the same metrics dict as its reward signal.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict

import numpy as np

from backend.events import Event, emit


# ═══════════════════════════════════════════════════════════════════════════
# Context passed to evaluation DAG builders
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class EvalContext:
    """Everything the evaluation DAG needs to compute metrics."""

    # Pipeline outputs
    y_test: Any                           # ground-truth labels
    y_pred: Any                           # predicted labels/values
    X_test: Any = None                    # optional — custom procedures may need it

    # For k-fold: full dataset (pre-split) + pipeline rebuilder
    X_full: Any = None
    y_full: Any = None
    pipeline_builder: Callable | None = None  # () -> fitted estimator

    # KB-resolved metric operators
    metric_fqns: list[str] = field(default_factory=list)
    metric_display_names: dict[str, str] = field(default_factory=dict)

    # Task-aware kwargs per metric FQN
    metric_kwargs: dict[str, dict] = field(default_factory=dict)

    # Custom procedure code (if type == "custom")
    custom_code: str | None = None
    custom_language: str = "python"

    # Run context
    run_id: str = ""
    task_name: str = ""


# Task-aware metric kwargs — moved here from execution.py so they're
# shared across all evaluation procedure types.
TASK_METRIC_KWARGS: Dict[str, Dict[str, Any]] = {
    "sklearn.metrics.f1_score": {"average": "weighted"},
    "sklearn.metrics.precision_score": {"average": "weighted"},
    "sklearn.metrics.recall_score": {"average": "weighted"},
    "sklearn.metrics.mean_squared_error": {"squared": False},  # RMSE
}


# ═══════════════════════════════════════════════════════════════════════════
# Evaluation DAG builders
# ═══════════════════════════════════════════════════════════════════════════

def build_evaluation_dag(
    procedure_type: str,
    ctx: EvalContext,
) -> tuple[dict, str]:
    """Build a Dask task graph for the requested evaluation procedure.

    Returns ``(graph, sink_key)`` where ``graph`` is a dict suitable for
    ``dask.threaded.get(graph, sink_key)`` and the result is a
    ``dict[str, float]`` of metric name → value.
    """
    builders = {
        "holdout": _build_holdout_dag,
        "kfold": _build_kfold_dag,
        "custom": _build_custom_dag,
        "none": _build_none_dag,
        "pairwise": _build_none_dag,  # pairwise is post-hoc, not inline
    }

    builder = builders.get(procedure_type, _build_holdout_dag)
    return builder(ctx)


# ── Hold-out ─────────────────────────────────────────────────────────────

def _build_holdout_dag(ctx: EvalContext) -> tuple[dict, str]:
    """Automated hold-out: compute each metric in parallel, aggregate."""
    from dorian.pipeline.operator_resolver import _resolve_dotted

    graph: dict[str, Any] = {
        "y_test": ctx.y_test,
        "y_pred": ctx.y_pred,
    }

    metric_keys: list[str] = []

    for fqn in ctx.metric_fqns:
        display = ctx.metric_display_names.get(fqn) or fqn.rsplit(".", 1)[-1]
        key = f"metric:{display}"
        kwargs = dict(ctx.metric_kwargs.get(fqn) or TASK_METRIC_KWARGS.get(fqn, {}))

        try:
            fn = _resolve_dotted(fqn)
        except Exception:
            continue

        graph[key] = (_safe_metric, fn, "y_test", "y_pred", kwargs, display, ctx.run_id)
        metric_keys.append(key)

    graph["__eval_result__"] = (_aggregate_metrics, *metric_keys)
    return graph, "__eval_result__"


def _safe_metric(
    fn: Callable,
    y_test: Any,
    y_pred: Any,
    kwargs: dict,
    display_name: str,
    run_id: str,
) -> tuple[str, float]:
    """Compute a single metric.

    A metric is an explicit node in the pipeline DAG. If it fails, the
    pipeline should fail — same contract as every other node. The old
    "swallow exception, return None, keep completing" behaviour hid
    genuine wiring bugs (shape mismatches, y-encoding asymmetries,
    NaN leakage) behind a green "completed" badge.

    We still emit a ``MetricComputeFailed`` event for observability,
    but then re-raise so the surrounding Dask task marks the node
    failed and propagates up to the pipeline status.
    """
    try:
        value = fn(y_test, y_pred, **kwargs)
    except Exception as exc:
        emit(Event("MetricComputeFailed", {
            "run_id": run_id, "metric": display_name, "error": str(exc),
        }))
        raise
    return (display_name, round(float(value), 4))


def _aggregate_metrics(*results: tuple[str, float]) -> dict[str, float]:
    """Collect (name, value) tuples into a dict.

    No None-filter needed: _safe_metric now raises on failure instead
    of producing (name, None), so every tuple here has a real value.
    """
    return {name: val for name, val in results}


# ── K-fold Cross-Validation ─────────────────────────────────────────────

def _build_kfold_dag(ctx: EvalContext, k: int = 5) -> tuple[dict, str]:
    """K-fold CV: each fold trains + predicts + evaluates, then averages.

    For MVP this runs as a single Dask node (sequential folds internally)
    to avoid the complexity of re-executing partial pipeline DAGs.  The
    fold-level parallelism can be added later by splitting into k sub-DAGs.
    """
    graph: dict[str, Any] = {
        "__eval_result__": (
            _kfold_evaluate,
            ctx.X_full if ctx.X_full is not None else ctx.X_test,
            ctx.y_full if ctx.y_full is not None else ctx.y_test,
            ctx.pipeline_builder,
            ctx.metric_fqns,
            ctx.metric_display_names,
            ctx.metric_kwargs,
            ctx.run_id,
            k,
        ),
    }
    return graph, "__eval_result__"


def _kfold_evaluate(
    X: Any,
    y: Any,
    pipeline_builder: Callable | None,
    metric_fqns: list[str],
    display_names: dict[str, str],
    metric_kwargs: dict[str, dict],
    run_id: str,
    k: int,
) -> dict[str, float]:
    """Run k-fold cross-validation and return averaged metrics."""
    from sklearn.model_selection import KFold, StratifiedKFold
    from sklearn.base import clone
    from dorian.pipeline.operator_resolver import _resolve_dotted

    if X is None or y is None or pipeline_builder is None:
        emit(Event("KFoldSkipped", {
            "run_id": run_id,
            "reason": "missing X_full/y_full or pipeline_builder",
        }))
        return {}

    # Pick stratified for classification (discrete y), regular for regression
    try:
        unique_ratio = len(np.unique(y)) / len(y)
        if unique_ratio < 0.05:  # likely classification
            splitter = StratifiedKFold(n_splits=k, shuffle=True, random_state=42)
        else:
            splitter = KFold(n_splits=k, shuffle=True, random_state=42)
    except Exception:
        splitter = KFold(n_splits=k, shuffle=True, random_state=42)

    # Resolve metric functions
    metric_fns: list[tuple[str, Callable, dict]] = []
    for fqn in metric_fqns:
        display = display_names.get(fqn) or fqn.rsplit(".", 1)[-1]
        kwargs = dict(metric_kwargs.get(fqn) or TASK_METRIC_KWARGS.get(fqn, {}))
        try:
            metric_fns.append((display, _resolve_dotted(fqn), kwargs))
        except Exception:
            pass

    if not metric_fns:
        return {}

    # Accumulate per-fold scores
    fold_scores: dict[str, list[float]] = {name: [] for name, _, _ in metric_fns}

    try:
        estimator = pipeline_builder()
    except Exception as exc:
        emit(Event("KFoldPipelineBuildFailed", {"run_id": run_id, "error": str(exc)}))
        return {}

    for fold_idx, (train_idx, test_idx) in enumerate(splitter.split(X, y)):
        try:
            X_train, X_test = X[train_idx] if hasattr(X, "__getitem__") else X.iloc[train_idx], \
                               X[test_idx] if hasattr(X, "__getitem__") else X.iloc[test_idx]
            y_train, y_test = y[train_idx], y[test_idx]

            fold_est = clone(estimator)
            fold_est.fit(X_train, y_train)
            y_pred = fold_est.predict(X_test)

            for name, fn, kwargs in metric_fns:
                try:
                    val = fn(y_test, y_pred, **kwargs)
                    fold_scores[name].append(float(val))
                except Exception:
                    pass
        except Exception as exc:
            emit(Event("KFoldFailed", {
                "run_id": run_id, "fold": fold_idx, "error": str(exc),
            }))

    # Average across folds
    return {
        name: round(float(np.mean(scores)), 4)
        for name, scores in fold_scores.items()
        if scores
    }


# ── Custom Evaluation Procedure ──────────────────────────────────────────

def _build_custom_dag(ctx: EvalContext) -> tuple[dict, str]:
    """Custom user code: receives (y_test, y_pred, X_test), returns metrics."""
    graph: dict[str, Any] = {
        "y_test": ctx.y_test,
        "y_pred": ctx.y_pred,
        "X_test": ctx.X_test,
        "__eval_result__": (
            _run_custom_eval, ctx.custom_code, "y_test", "y_pred", "X_test", ctx.run_id,
        ),
    }
    return graph, "__eval_result__"


def _run_custom_eval(
    code: str | None,
    y_test: Any,
    y_pred: Any,
    X_test: Any,
    run_id: str,
) -> dict[str, float]:
    """Execute user-provided evaluation code in a restricted namespace."""
    if not code:
        return {}

    # Restricted builtins — same sandbox as operator_resolver Snippets
    safe_builtins = {
        "abs": abs, "all": all, "any": any, "bool": bool,
        "dict": dict, "enumerate": enumerate, "filter": filter,
        "float": float, "frozenset": frozenset, "hasattr": hasattr,
        "int": int, "isinstance": isinstance, "len": len,
        "list": list, "map": map, "max": max, "min": min,
        "print": print, "range": range, "round": round, "set": set,
        "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
        "type": type, "zip": zip, "__import__": __import__,
    }

    namespace: dict[str, Any] = {"__builtins__": safe_builtins}

    try:
        exec(compile(code, "<custom_eval>", "exec"), namespace)  # noqa: S102
    except Exception as exc:
        emit(Event("CustomEvalCompileFailed", {"run_id": run_id, "error": str(exc)}))
        return {}

    # The custom code must define `foo(y_test, y_pred, X_test)` → dict
    fn = namespace.get("foo")
    if not callable(fn):
        emit(Event("CustomEvalMissingFoo", {"run_id": run_id}))
        return {}

    try:
        result = fn(y_test, y_pred, X_test)
    except Exception as exc:
        emit(Event("CustomEvalFailed", {"run_id": run_id, "error": str(exc)}))
        return {}

    if not isinstance(result, dict):
        emit(Event("CustomEvalBadReturn", {"run_id": run_id, "type": type(result).__name__}))
        return {}

    return {
        str(k): round(float(v), 4)
        for k, v in result.items()
        if isinstance(v, (int, float))
    }


# ── None / Pairwise (skip) ──────────────────────────────────────────────

def _build_none_dag(_ctx: EvalContext) -> tuple[dict, str]:
    """No evaluation — return empty metrics."""
    return {"__eval_result__": {}}, "__eval_result__"
