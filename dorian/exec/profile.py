"""
dorian/exec/profile.py
----------------------
Dataset profiling + quality-check exec job.

Registers ``dq_check:profile_and_quality`` -- the full 70-node Dask
graph that used to run inline in ``pipeline_events.check_data``.
Moving this into the exec-worker container means:

  * the backend event-loop handler doesn't spend seconds-to-tens-of-
    seconds on pandas/numpy/sklearn compute;
  * the exec-worker can be scaled horizontally for import-heavy
    workloads without touching the backend replica count;
  * delivery + tracing + retry + backpressure flow through the
    ``exec:jobs`` Redis Stream + consumer group (same pattern as
    the ``dq_check:missing_values`` / ``dq_check:uniqueness`` kinds).

Inputs (from the submitter):

    {
        "uid": str,
        "session": str,
        "did": str,
        "fpath": str,
        # Echoed in the completion event so the backend handler can
        # clean up its idempotency lock:
        "lock_key": str,
        "rerun_key": str,
    }

Output (becomes the completion event's ``result`` payload):

    {
        "uid": ..., "session": ..., "did": ..., "fpath": ...,
        "profile": {metafeature_name: value, ...},
        "quality": {metric_name: value, ...},
        "columns": [col, ...],
        "column_profiles": {col: {...}, ...},
        "lock_key": ..., "rerun_key": ...,
    }

Per-metafeature progress events (``ComputingMetafeature`` /
``MetafeatureComputed``) are NOT emitted in this first cut -- doing
them from the exec-worker would require direct XADD to ``events:bg``
per metric, adding complexity without unblocking the user-visible
regression. Re-wire them as a follow-up if progressive UI rendering
becomes important.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from dorian.exec.registry import register

_log = logging.getLogger(__name__)


@register("dq_check:profile_and_quality")
async def profile_and_quality(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Run the dataset profiling + quality-check Dask graph.

    The compute lives here (in the exec-worker process); the result
    dict becomes the ``DQCheckProfileAndQualityCompleted`` event's
    ``result`` payload. The backend handler
    ``handle_profile_and_quality_completed`` writes session meta +
    emits ``state/*`` xadds + releases the idempotency lock when that
    event lands.
    """
    # Lazy imports: keep the exec-worker module-load lean for
    # non-profiling jobs. Only workers that actually process a
    # profile job pay the import cost.
    import asyncio as _asyncio
    import pandas as pd  # noqa: F401 -- loaded lazily; used by helpers

    from dorian.event.handlers.pipeline_events import (
        _json_safe,
        are_fairness_checks_required,
        check_for_protected_attributes,
        get_balance_target_labels,
        get_category_column,
        get_category_size_threshold,
        get_compliance_rules,
        get_consistency_label_threshold,
        get_feature_effectiveness_rules,
        get_features,
        get_format_schema,
        get_inaccuracy_columns,
        get_label_effectiveness_rules,
        get_precision_requirements,
        get_range_rules,
        get_record_relevance_condition,
        get_relevant_features,
        get_required_attributes,
        get_semantic_accuracy_rules,
        get_semantic_consistency_rules,
        get_target_size,
        get_targets,
        get_validation_rules,
        get_value_occurrence_expectations,
        _is_missing_required_input,
    )
    from dorian.tabular.data.profiling.column_profile import (
        compute_column_profiles,
    )
    from dorian.tabular.data.profiling.metafeatures import mf
    from dorian.tabular.data.quality.metrics import qm
    from backend.cache import cached_read_table

    uid = inputs.get("uid", "") or ""
    session = inputs.get("session", "") or ""
    did = inputs.get("did", "") or ""
    fpath = inputs.get("fpath", "") or ""
    if not fpath:
        return {
            "error": "missing fpath",
            "uid": uid, "session": session, "did": did,
            "lock_key": inputs.get("lock_key"),
            "rerun_key": inputs.get("rerun_key"),
        }

    def to_list(*args):
        return args

    def safe_mf(fn: Callable, name: str) -> Callable:
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # pragma: no cover -- metric failure
                _log.warning("metafeature %s failed: %s", name, exc)
                return None
        return wrapper

    # Pending-input bookkeeping mirrors the pre-migration check_data
    # logic so the downstream completion handler sees the same shape
    # (``{"status": "pending_input", "requires": [...]}`` sentinels).
    _PENDING_INPUTS: dict[str, tuple[str, ...]] = {
        "SyntacticDataAccuracy": ("validation_rules",),
        "SemanticDataAccuracy": ("semantic_accuracy_rules",),
        "RiskOfDatasetInaccuracy": ("inaccuracy_columns",),
        "DataAccuracyRange": ("range_rules",),
        "LabelProportionBalance": ("category_column",),
        "DataItemCompliance": ("compliance_rules",),
        "DataFormatConsistency": ("format_schema",),
        "SemanticConsistency": ("semantic_consistency_rules",),
        "CategorySizeDiversity": ("category_column",),
        "FeatureEffectiveness": ("feature_effectiveness_rules",),
        "CategorySizeEffectiveness": (
            "category_column", "category_size_threshold",
        ),
        "LabelEffectiveness": ("label_effectiveness_rules",),
        "RiskOfWastedSpace": ("target_size",),
        "PrecisionOfDataValues": ("precision_requirements",),
        "FeatureRelevance": ("relevant_features",),
        "RecordRelevance": ("record_relevance_condition",),
        "RepresentativenessRatio": ("required_attributes",),
    }
    _TARGET_REQUIRED = frozenset({
        "LabelCompleteness", "DataLabelConsistency",
        "LabelDistributionBalance", "LabelRichness",
        "RelativeLabelAbundance", "LabelEffectiveness",
    })

    def safe_quality(spec, name: str) -> Callable:
        fn = spec.fn
        parameter_names = tuple(inspect.signature(fn).parameters)

        def wrapper(*args, **kwargs):
            if name in _TARGET_REQUIRED:
                target = kwargs.get("target")
                if target is None and len(args) >= 2:
                    target = args[1]
                if not target:
                    return {"status": "pending_input", "requires": ["target"]}
            if name == "ValueOccurrenceCompleteness":
                expectations = kwargs.get("value_occurrence_expectations")
                if expectations is None and len(args) >= 2:
                    expectations = args[1]
                if _is_missing_required_input(expectations):
                    return {
                        "status": "pending_input",
                        "requires": ["value_occurrence_expectations"],
                    }
            required = _PENDING_INPUTS.get(name)
            if required:
                missing = [
                    req for req in required
                    if _is_missing_required_input(kwargs.get(req))
                    and not (
                        req in parameter_names
                        and len(args) > parameter_names.index(req)
                        and not _is_missing_required_input(
                            args[parameter_names.index(req)]
                        )
                    )
                ]
                if missing:
                    return {"status": "pending_input", "requires": missing}
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # pragma: no cover
                _log.warning("quality %s failed: %s", name, exc)
                return None
        return wrapper

    # Arrow-first CSV read: parse the file once via pyarrow, pass the
    # ``pa.Table`` through the graph, and derive the pandas view from
    # it for downstream nodes that still consume ``pd.DataFrame``.
    # Future Arrow-native metafeature / DQ implementations can bind
    # to ``'table'`` directly and skip the ``to_pandas`` step.
    from backend.cache import _arrow_to_numpy  # local import: leaf node only

    def _table_to_df(table):
        return _arrow_to_numpy(table.to_pandas())

    graph: dict[str, Any] = {
        'user': uid,
        'session': session,
        'did': did,
        'fpath': fpath,
        'table': (cached_read_table, 'fpath'),
        'df': (_table_to_df, 'table'),
        'columns': (lambda df: df.columns.tolist(), 'df'),
        'column_profiles': (compute_column_profiles, 'df'),
        'features': (get_features, 'user', 'session', 'did', 'columns', 'column_profiles'),
        'feature_columns': (lambda features: features, 'features'),
        'targets': (get_targets, 'user', 'session', 'did', 'columns', 'column_profiles'),
        'target': (lambda targets: targets[0] if targets else None, 'targets'),
        'value_occurrence_expectations': (get_value_occurrence_expectations, 'did'),
        'range_rules': (get_range_rules, 'did'),
        'inaccuracy_columns': (get_inaccuracy_columns, 'did'),
        'semantic_accuracy_rules': (get_semantic_accuracy_rules, 'did'),
        'validation_rules': (get_validation_rules, 'did'),
        'category_column': (get_category_column, 'did'),
        'balance_target_labels': (get_balance_target_labels, 'did'),
        'compliance_rules': (get_compliance_rules, 'did'),
        'consistency_label_threshold': (get_consistency_label_threshold, 'did'),
        'format_schema': (get_format_schema, 'did'),
        'semantic_consistency_rules': (get_semantic_consistency_rules, 'did'),
        'feature_effectiveness_rules': (get_feature_effectiveness_rules, 'did'),
        'category_size_threshold': (get_category_size_threshold, 'did'),
        'label_effectiveness_rules': (get_label_effectiveness_rules, 'did'),
        'target_size': (get_target_size, 'did'),
        'precision_requirements': (get_precision_requirements, 'did'),
        'relevant_features': (get_relevant_features, 'did'),
        'record_relevance_condition': (get_record_relevance_condition, 'did'),
        'required_attributes': (get_required_attributes, 'did'),
        'are_fairness_checks_required': (are_fairness_checks_required, 'user', 'session', 'did'),
        'protected_attributes': (check_for_protected_attributes, 'user', 'session', 'did', 'columns', 'are_fairness_checks_required'),
        'X': (lambda df, cols: df[cols], 'df', 'features'),
        'y': (lambda df, cols: df[cols[0]].to_numpy() if len(cols) == 1 else df[cols].to_numpy(), 'df', 'targets'),
        # ``X.dtypes`` returns numpy dtype objects; the metafeature
        # registry compares against the string literals
        # ``"numerical"`` / ``"categorical"`` so the raw Series
        # silently misses on every column. See
        # ``dorian.tabular.data.profiling.profile_dataset`` — same
        # fix applied to the in-process profiler.
        'feat_type': (
            lambda X: X.dtypes.apply(
                lambda dt: (
                    "numerical" if pd.api.types.is_numeric_dtype(dt)
                    else "categorical"
                )
            ),
            'X',
        ),
        'profile': (to_list,) + tuple(mf),
        'quality': (to_list,) + tuple(qm.keys()),
    }
    for name, fn in mf.items():
        graph[name] = (safe_mf(fn, name),) + tuple(
            inspect.signature(fn).parameters
        )
    for name, spec in qm.items():
        graph[name] = (safe_quality(spec, name),) + tuple(
            inspect.signature(spec.fn).parameters
        )

    # Resolve the graph in topological order — same shape as the
    # previous ``dask.threaded.get`` consumed (each node is either a
    # bare value or ``(callable, *dep_keys)``). The exec-worker
    # already dedicates one slot to this job, so parallelising the
    # graph internally competes with the worker pool. Per the
    # ``DORIAN_USE_RUST_RUNNER`` directive, no Dask code path here.
    def _resolve(key, _resolved):
        if key in _resolved:
            return _resolved[key]
        spec = graph.get(key)
        if not isinstance(spec, tuple) or not spec or not callable(spec[0]):
            _resolved[key] = spec
            return spec
        fn, *deps = spec
        args = [_resolve(d, _resolved) for d in deps]
        result = fn(*args)
        _resolved[key] = result
        return result

    def _run_graph():
        out: dict[str, Any] = {}
        return [
            _resolve(k, out)
            for k in ('profile', 'quality', 'columns', 'column_profiles')
        ]

    profile, quality, columns, col_profiles = await _asyncio.to_thread(_run_graph)

    mf_names = list(mf.keys())
    profile_dict = {
        mf_names[i]: _json_safe(profile[i] if i < len(profile) else None)
        for i in range(len(mf_names))
    }
    quality_names = list(qm.keys())
    quality_dict = {
        quality_names[i]: _json_safe(quality[i] if i < len(quality) else None)
        for i in range(len(quality_names))
    }

    return {
        "uid": uid,
        "session": session,
        "did": did,
        "fpath": fpath,
        "profile": profile_dict,
        "quality": quality_dict,
        "columns": list(columns) if columns is not None else [],
        "column_profiles": col_profiles,
        "lock_key": inputs.get("lock_key"),
        "rerun_key": inputs.get("rerun_key"),
    }
