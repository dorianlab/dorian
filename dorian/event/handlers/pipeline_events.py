import json
import asyncio
import inspect
import logging
import time
import math
import numpy as np
import pandas as pd
from time import sleep
from pipe import select
from typing import Callable
from uuid import uuid4
from datetime import datetime, timezone
from numbers import Number
from dorian.dag import DAG, Operator, Snippet, Parameter
from dorian.pipeline.parser import transform
from dorian.pipeline.task_rules import rules
from dorian.tabular.data.profiling.column_profile import compute_column_profiles
from dorian.tabular.data.profiling.metafeatures import mf
from dorian.tabular.data.quality.metrics import qm
from dorian.tabular.data.quality.accuracy import BuildValidationRulesFromAllowedValues

from backend.events import Event, emit, aemit
from backend.envs import redis, aioredis
from backend.cache import cached_read_csv

from dorian.pipeline.utils.feature import get_features, get_targets
from dorian.types import UUID
from dorian.knowledge.ontology_kb import load_kb
from dorian.tabular.data.quality.decision import (
    DecisionFunctionSpec,
    build_decision_specs,
    evaluate_decision_function,
    extract_history_values,
)
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN

_log = logging.getLogger(__name__)



def _to_stream_fields(msg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in msg.items():
        if isinstance(v, (dict, list)):
            out[str(k)] = json.dumps(v)
        else:
            out[str(k)] = "" if v is None else str(v)
    return out

def snake_to_title(s: str) -> str:
    """Convert a snake_case string to Title Case (e.g. 'some_label' -> 'Some Label')."""
    return s.replace('_', ' ').title()


# ── Column auto-detection ────────────────────────────────────────────
#
# When a user drops a CSV on the canvas we want to infer sensible
# defaults for feature / target columns so the downstream pipeline
# (task inference, eval-procedure prompt, recommendations) can make
# progress without a five-click column-selection dialog. The user is
# always free to override — this just picks a plausible starting
# point.
#
# Target heuristic (in order):
#   1. A column whose name is one of the conventional target markers
#      (``class``, ``target``, ``label``, ``y``, ``outcome``, ``output``).
#   2. The LAST column in the file if it's categorical or low-cardinality
#      numeric (≤ 50 unique values). sklearn / OpenML convention.
#   3. Otherwise no target guessed — the user answers the question.
_TARGET_NAME_MARKERS = frozenset({
    "class", "target", "label", "y", "outcome", "output", "churn",
})


def _guess_target_column(columns: list[str], col_profiles: dict | None) -> str | None:
    """Return the most plausible target column name, or None.

    Pure function — no I/O. Consumes the column list + per-column
    profile dict produced by the profiler.
    """
    if not columns:
        return None
    profiles = col_profiles or {}
    lower_to_actual = {c.lower(): c for c in columns}
    # 1. Conventional name match.
    for marker in _TARGET_NAME_MARKERS:
        if marker in lower_to_actual:
            return lower_to_actual[marker]
    # 2. Last column, if low-cardinality.
    last = columns[-1]
    last_profile = profiles.get(last, {})
    n_unique = last_profile.get("n_unique") or last_profile.get("unique_count")
    is_numeric = last_profile.get("is_numeric", False)
    if n_unique is not None and n_unique <= 50:
        return last
    if not is_numeric:  # string column without a profile → treat as categorical
        return last
    return None


async def _autodetect_columns_if_missing(
    *,
    user: str,
    session: str,
    did: str,
    fpath: str,
    all_columns: list[str] | None,
    col_profiles: dict | None,
) -> None:
    """Populate ``dataset:{did}:{feature,target}_columns`` when absent.

    Called at the end of ``check_data`` right before ``DataProfiled``
    is emitted, so downstream handlers (``handle_auto_task_selection``,
    ``attempt_recommendations``) see real column hints on a brand-new
    dataset without requiring the user to walk through the quality-check
    dialog first.

    Idempotent: if either key is already set we leave it alone.
    """
    target_key = RedisKeys.dataset_target_columns(did)
    feature_key = RedisKeys.dataset_feature_columns(did)

    # Already answered? Respect the user's choice.
    if await aioredis.exists(target_key) or await aioredis.exists(feature_key):
        return

    columns = list(all_columns or [])
    if not columns:
        try:
            columns = cached_read_csv(fpath).columns.tolist()
        except Exception:
            return

    target = _guess_target_column(columns, col_profiles)
    if not target:
        # No confident guess — let the user pick. But at least write an
        # empty-list sentinel so attempt_recommendations knows we tried
        # (distinguish "not profiled yet" from "profiled, no target").
        await aemit(Event("DatasetTargetAutodetectSkipped", {
            "did": did, "session": session, "columns": len(columns),
        }))
        return

    features = [c for c in columns if c != target]

    # Write feature/target to Redis in the same shape the column-table
    # question handler would have produced (JSON list of strings).
    await aioredis.set(feature_key, json.dumps(features))
    await aioredis.set(target_key, json.dumps([target]))

    # Mirror into session meta's dataset block so seed-session sees it
    # on reconnect without a second Redis round-trip.
    try:
        raw = await aioredis.get(RedisKeys.session_meta(session))
        if raw:
            meta = json.loads(raw)
            ds = meta.get("dataset") or {}
            ds["features"] = features
            ds["target"] = [target]
            meta["dataset"] = ds
            await aioredis.set(RedisKeys.session_meta(session), json.dumps(meta))
    except Exception:
        pass  # best effort

    # Tell the frontend the inferred columns so the UI can render the
    # "auto-detected" badge and the user can override in one click.
    await aioredis.xadd(
        RedisKeys.stream(user, session),
        {
            "event": "state/dataset/columns-autodetected",
            "value": json.dumps({
                "did": did, "features": features, "target": [target],
                "source": "last-column-heuristic" if target == columns[-1]
                          else "name-marker",
            }),
            "type": "json",
        },
        maxlen=STREAM_MAXLEN, approximate=True,
    )
    await aemit(Event("DatasetColumnsAutodetected", {
        "uid": user, "session": session, "did": did,
        "target": target, "n_features": len(features),
    }))


def _json_safe(value):
    """Convert nested profile/quality values into JSON-safe primitives."""
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, dict):
        return {str(_json_safe(k)): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)
# lower = lambda ll: [l.lower() for l in ll]
# approx_match = lambda token, options: difflib.get_close_matches(token, options, n=3, cutoff=0.2)
# exact_match = lambda token, options: token if token.lower() in lower(options) else False
# to_camel = lambda s: s.replace(' ', '_').lower()
from_camel = lambda s: s.replace('_', ' ').title()
delay = 1.


def are_fairness_checks_required(user: UUID, session: UUID, did: UUID):
    questions = [{
        "id": f"dataset:{did}:fairness_checks",
        "type": "select",
        "question": "Does your dataset model people or describe any of the protected attributes?",
        "options": ["yes", "no"]
    }]
    callback = f'callback:fairness_checks:{did}'  # TODO: add to RedisKeys

    if not redis.exists(callback):
        message = {
            "event": "state/queries",
            "value":  json.dumps(questions),
            "uid": user,
            "session": session,
            'callback': callback,
        }
        redis.xadd(
            RedisKeys.stream(user, session),
            {str(k): str(v) for k, v in message.items()},
            maxlen=STREAM_MAXLEN, approximate=True,
        )

        deadline = time.monotonic() + 120
        while not redis.exists(callback):
            if time.monotonic() > deadline:
                emit(Event("UserResponseTimeout", {"callback": callback}))
                return False
            sleep(delay)

    values = redis.get(callback)
    mapping = {
        "yes": True,
        "no": False
    }
    return mapping.get(json.loads(values), False)


async def get_protected_attributes():
    """Return KB-declared protected attributes.

    The DSL doesn't declare any ``Protected Attribute`` nodes today
    (the ``label='Protected Attribute'`` pattern came from an earlier
    Cypher-only era). Until a curated source adds them — most likely
    via ``X is a Protected Attribute`` — the list stays empty.
    """
    kb = load_kb()
    members = kb.incoming("Protected Attribute", "is_a")
    return sorted(set(members))


def what_attributes_are_protected(user, session, did, columns, protected_attributes):
    questions = [{
        "id": f"dataset:{did}:{column}:protected_attribute",
        "type": "select",
        "question": f"What protected attribute does column {column} represent?",
        "options": ['None'] + protected_attributes + ['Other']
    } for column in columns]
    callback = f'callback:protected_attributes:{did}'  # TODO: add to RedisKeys

    if not redis.exists(callback):
        message = {
            "event": "state/queries",
            "value":  json.dumps(questions),
            "uid": user,
            "session": session,
            'callback': callback,
        }
        redis.xadd(
            RedisKeys.stream(user, session),
            {str(k): str(v) for k, v in message.items()},
            maxlen=STREAM_MAXLEN, approximate=True,
        )

        deadline = time.monotonic() + 120
        while not redis.exists(callback):
            if time.monotonic() > deadline:
                emit(Event("UserResponseTimeout", {"callback": callback}))
                return {}
            sleep(delay)

    values = redis.get(callback)
    return json.loads(values)

async def handle_pipeline_saved(event: Event):
    """Persist the canvas pipeline to ``session:meta`` on PipelineSaved.

    The frontend emits ``PipelineSaved`` with the full pipelineHistory
    (``{uuid, headId, pipelines}``) right before ``ExecutePipeline``
    so the run can look up the pipeline by ``headId``. The python
    event registry's stub comment claimed this was "owned by
    engine/backend handlers/pipeline.rs", but that rust binary isn't
    deployed (no Dockerfile, not in compose) so PipelineSaved had no
    live handler — every Run on a freshly-composed canvas pipeline
    failed with ``pipeline_not_found`` because session.meta had no
    ``pipelineHistory`` key when execution.py looked.

    Mirrors the rust implementation (engine/backend/src/handlers/pipeline.rs)
    field-for-field so a future re-deploy of the rust event-bus
    subscriber drops in cleanly. When that lands, drop the
    subscription in dorian/event/registry.py.
    """
    session_id = event.data.get("session")
    if not session_id:
        return
    payload = event.data
    head_id = payload.get("headId")
    if not head_id:
        # Re-emitted completion or malformed — same fast-exit as rust.
        return
    pipelines = payload.get("pipelines") or []
    uuid_field = payload.get("uuid")

    head_pipeline = next(
        (p for p in pipelines if p.get("id") == head_id),
        pipelines[-1] if pipelines else None,
    )
    if head_pipeline is None:
        await aemit(Event("PipelineSaveError", {
            "source": "handlers.pipeline.handle_pipeline_saved",
            "error": "missing_head_pipeline",
            "session": session_id,
        }))
        return

    raw_meta = await aioredis.get(RedisKeys.session_meta(session_id))
    if not raw_meta:
        await aemit(Event("SessionNotFound", {
            "source": "handlers.pipeline.handle_pipeline_saved",
            "uid": event.data.get("uid"),
            "session": session_id,
        }))
        return

    meta = json.loads(raw_meta)
    meta["pipelineHistory"] = {
        "uuid": uuid_field,
        "headId": head_id,
        "pipelines": pipelines,
    }
    meta["pipeline"] = head_pipeline
    await aioredis.set(RedisKeys.session_meta(session_id), json.dumps(meta))


async def read_pipeline(event: Event):
    user_id, session_id = event.data.get('uid'), event.data.get('session')
    fpath: str = str(event.data.get('fpath'))

    raw = await aioredis.get(RedisKeys.session_meta(session_id))
    if not raw:
        await aemit(Event("SessionNotFound", data={'uid': user_id, 'session': session_id}))
        return

    session = json.loads(raw)

    def _read_file(p: str) -> str:
        with open(p, 'r') as f:
            return f.read()
    try:
        pipeline = await asyncio.to_thread(_read_file, fpath)
    except FileNotFoundError:
        await aemit(Event("PipelineFileNotFound", {"fpath": fpath}))
        return

    # ---------------------------
    # Initialize pipelineHistory
    # ---------------------------
    pipeline_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    pipeline_version = {
        "id": pipeline_id,
        "parentPipelineId": session_id,
        "createdAt": now,
        "message": "Imported",
        "pipeline": pipeline,
    }

    session["pipeline"] = pipeline_version

    session["pipelineHistory"] = {
        "uuid": session_id,
        "headId": pipeline_id,
        "pipelines": [pipeline_version],
    }
    message = {
        "event": 'state/pipeline',
        "uid": str(user_id),
        "session": str(session_id),
        "value": session["pipelineHistory"],
    }

    await aioredis.xadd(
        RedisKeys.stream(user_id, session_id),
        _to_stream_fields(message),
        maxlen=STREAM_MAXLEN, approximate=True,
    )

    await aioredis.set(RedisKeys.session_meta(session_id), json.dumps(session))

    await aemit(Event('PipelineRetrieved', data=message))


async def start_debugging(event: Event):
    value = event.data.get("value")

    if isinstance(value, str):
        try:
            value = json.loads(value)  # maybe pipeline_version JSON
        except Exception:
            # legacy: raw pipeline JSON string
            pipeline_json = value
        else:
            pipeline_json = value.get("pipeline")
    elif isinstance(value, dict):
        pipeline_json = value.get("pipeline")
    else:
        pipeline_json = None

    if not pipeline_json:
        await aemit(Event("MalformedPipeline", data={"error": "Missing pipeline in value"}))
        return

    pipeline = DAG(**json.loads(pipeline_json))

    _nodes = {}
    for k, op in pipeline.nodes.items():
        match op:
            case { 'type': 'Operator', **kw }:
                _nodes[k] = Operator(**kw)
            case { 'type': 'Parameter', **kw }:
                _nodes[k] = Parameter(**kw)
            case { 'type': 'Snippet', **kw }:
                _nodes[k] = Snippet(**kw)
            case unknown:
                await aemit(Event('MalformedPipeline', data={ 'error': f'Malformed operator {unknown}'}))
    await transform(DAG(nodes=_nodes, edges=pipeline.edges), rules, meta=event.data)


def check_for_protected_attributes(user, session, did, columns, are_fairness_checks_required):
    if not are_fairness_checks_required:
        return []

    # get_protected_attributes is async; bridge to the main event loop from
    # the Dask worker thread.  asyncio.run() would create a *new* loop, which
    # risks corruption — use the running loop instead.
    loop = asyncio.get_event_loop()
    protected_attributes = asyncio.run_coroutine_threadsafe(
        get_protected_attributes(), loop
    ).result(timeout=30)
    responses = what_attributes_are_protected(user, session, did, columns, protected_attributes)
    selected = [responses.get(f"dataset:{did}:{column}:protected_attribute", 'None') for column in columns]
    return [s for s in selected if s != 'None']


def get_value_occurrence_expectations(did):
    """Optional user-provided expectations for ValueOccurrenceCompleteness.

    Stored as JSON under ``dataset:{did}:value_occurrence_expectations``.
    Returns ``None`` when not yet provided.
    """
    callback = f"dataset:{did}:value_occurrence_expectations"
    raw = redis.get(callback)
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == "":
        return []
    try:
        return json.loads(raw)
    except Exception:
        return None


def _get_dataset_json_config(did: str, suffix: str):
    key = f"dataset:{did}:{suffix}"
    raw = redis.get(key)
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == "":
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _get_dataset_scalar_config(did: str, suffix: str):
    key = f"dataset:{did}:{suffix}"
    raw = redis.get(key)
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == "":
        return None
    return raw


def get_range_rules(did):
    return _get_dataset_json_config(did, "range_rules")


def get_inaccuracy_columns(did):
    return _get_dataset_json_config(did, "inaccuracy_columns")


def get_semantic_accuracy_rules(did):
    return _get_dataset_json_config(did, "semantic_accuracy_rules")


def get_syntactic_allowed_values(did):
    return _get_dataset_json_config(did, "syntactic_allowed_values")


def get_sensitive_columns(did):
    return _get_dataset_json_config(did, "sensitive_columns")


def get_validation_rules(did):
    syntactic_allowed_values = get_syntactic_allowed_values(did)
    if not syntactic_allowed_values:
        return None
    return BuildValidationRulesFromAllowedValues(syntactic_allowed_values)


def get_category_column(did):
    return _get_dataset_scalar_config(did, "category_column")


def get_balance_target_labels(did):
    return _get_dataset_json_config(did, "balance_target_labels")


def get_compliance_rules(did):
    return _get_dataset_json_config(did, "compliance_rules")


def get_consistency_label_threshold(did):
    raw = _get_dataset_scalar_config(did, "consistency_label_threshold")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_format_schema(did):
    return _get_dataset_json_config(did, "format_schema")


def get_semantic_consistency_rules(did):
    return _get_dataset_json_config(did, "semantic_consistency_rules")


def get_feature_effectiveness_rules(did):
    return _get_dataset_json_config(did, "feature_effectiveness_rules")


def get_category_size_threshold(did):
    raw = _get_dataset_scalar_config(did, "category_size_threshold")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_label_effectiveness_rules(did):
    return _get_dataset_json_config(did, "label_effectiveness_rules")


def get_target_size(did):
    raw = _get_dataset_scalar_config(did, "target_size")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_precision_requirements(did):
    return _get_dataset_json_config(did, "precision_requirements")


def get_relevant_features(did):
    return _get_dataset_json_config(did, "relevant_features")


def get_record_relevance_condition(did):
    return _get_dataset_json_config(did, "record_relevance_condition")


def get_required_attributes(did):
    return _get_dataset_json_config(did, "required_attributes")


async def get_quality_thresholds_from_kg(metric_names):
    """Load default thresholds from the rust KB snapshot; fall back to
    0.95 per metric when a metric has no curated threshold.
    """
    defaults = {name: 0.95 for name in metric_names}
    try:
        kb = load_kb()
        for metric in metric_names:
            for raw in kb.out(metric, "has_threshold"):
                value = kb.display(raw)
                try:
                    defaults[metric] = float(value)
                    break
                except (TypeError, ValueError):
                    pass
    except Exception:
        # KB is best-effort here; keep stable fallback behavior.
        pass
    return defaults


async def get_quality_decision_specs_from_kg(metric_names):
    """Load decision-function + threshold pairs from the rust KB snapshot.

    Falls back to constant-threshold specs (handled by
    ``build_decision_specs``) when a metric is missing from the KB.
    """
    threshold_rows: list[dict] = []
    decision_rows: list[dict] = []
    try:
        kb = load_kb()
        for metric in metric_names:
            for raw in kb.out(metric, "has_threshold"):
                threshold_rows.append({
                    "metric": metric,
                    "threshold": kb.display(raw),
                })
            for raw in kb.out(metric, "has_decision_function"):
                decision_rows.append({
                    "metric": metric,
                    "decision_function": kb.display(raw),
                })
    except Exception:
        threshold_rows = []
        decision_rows = []
    return build_decision_specs(list(metric_names), threshold_rows, decision_rows)


def get_quality_threshold_override(did):
    mode_key = f"dataset:{did}:quality_threshold_mode"
    override_key = f"dataset:{did}:quality_threshold_override"
    mode = redis.get(mode_key) if redis.exists(mode_key) else ""
    mode = (mode or "").strip().lower()
    raw_override = redis.get(override_key) if redis.exists(override_key) else ""
    raw_override = (raw_override or "").strip()
    if mode == "override" and raw_override:
        try:
            val = float(raw_override)
            if 0 <= val <= 1:
                return val
        except (TypeError, ValueError):
            return None
    return None


def get_quality_threshold_mode(did):
    mode_key = f"dataset:{did}:quality_threshold_mode"
    mode = redis.get(mode_key) if redis.exists(mode_key) else ""
    return (mode or "").strip().lower()


def _as_float(value):
    # pd.NA is not an instance of Number and float(pd.NA) raises — catch it first.
    if value is pd.NA:
        return None
    if isinstance(value, Number):
        f = float(value)
        return None if np.isnan(f) or np.isinf(f) else f
    if isinstance(value, str):
        try:
            f = float(value)
            return None if np.isnan(f) or np.isinf(f) else f
        except ValueError:
            return None
    return None


def _is_missing_required_input(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _quality_comparison(fval: float, threshold: float, comparator: str) -> tuple[str, str]:
    passed = fval >= threshold if comparator == "gte" else fval <= threshold
    if comparator == "gte":
        operator = ">=" if passed else "<"
    else:
        operator = "<=" if passed else ">"
    return ("passed" if passed else "failed", operator)


def build_quality_checks(quality_dict, decision_specs, override, metric_specs, quality_history=None):
    checks = []
    for metric_name, value in quality_dict.items():
        spec = metric_specs.get(metric_name)
        if spec is not None and not getattr(spec, "checkable", True):
            continue
        comparator = getattr(spec, "comparator", "gte") if spec is not None else "gte"
        decision_spec = decision_specs.get(metric_name, DecisionFunctionSpec())
        if override is not None:
            decision_spec = DecisionFunctionSpec(kind="constant_threshold", threshold=override)
        threshold = decision_spec.threshold

        # Pending input marker from persist_quality
        if isinstance(value, dict) and value.get("status") == "pending_input":
            checks.append({
                "check": metric_name,
                "status": "pending",
                "value": None,
                "threshold": threshold,
                "decision_function": decision_spec.kind,
                "message": f"Waiting for input: {', '.join(value.get('requires', []))}",
            })
            continue

        # Composite metric (e.g. ValueOccurrenceCompleteness -> dict of ratios)
        if isinstance(value, dict):
            for key, subval in value.items():
                fval = _as_float(subval)
                if fval is None:
                    checks.append({
                        "check": f"{metric_name}:{key}",
                        "status": "error",
                        "value": subval,
                        "threshold": threshold,
                        "decision_function": decision_spec.kind,
                        "message": "Non-numeric value",
                    })
                else:
                    outcome = evaluate_decision_function(
                        fval,
                        decision_spec,
                        comparator=comparator,
                        history_values=extract_history_values(quality_history, metric_name, subkey=key),
                    )
                    checks.append({
                        "check": f"{metric_name}:{key}",
                        "status": outcome.status,
                        "value": fval,
                        "threshold": outcome.threshold,
                        "decision_function": outcome.decision_type,
                        "message": outcome.message,
                    })
            continue

        fval = _as_float(value)
        if fval is None:
            checks.append({
                "check": metric_name,
                "status": "error",
                "value": value,
                "threshold": threshold,
                "decision_function": decision_spec.kind,
                "message": "Non-numeric value",
            })
            continue

        outcome = evaluate_decision_function(
            fval,
            decision_spec,
            comparator=comparator,
            history_values=extract_history_values(quality_history, metric_name),
        )
        checks.append({
            "check": metric_name,
            "status": outcome.status,
            "value": fval,
            "threshold": outcome.threshold,
            "decision_function": outcome.decision_type,
            "message": outcome.message,
        })

    return checks


async def sync_record_completeness_suggestions(
    user: str,
    session: str,
    did: str,
    checks: list[dict],
) -> None:
    """Publish or revoke RecordCompleteness mitigation suggestions."""
    stream = f"{user}:{session}:stream"
    task = "dataset:record_completeness"

    await aioredis.xadd(stream, {
        "event": "suggestions/revoke",
        "operator": task,
    }, maxlen=STREAM_MAXLEN, approximate=True)


async def check_data(event: Event):
    """DataExists / DataWritten handler.

    Profiling a real dataset (70-node Dask graph with PCA, statistical
    moments, 20+ quality metrics, KB reads for decision specs) takes
    seconds-to-tens-of-seconds. Running that inline would:
      * occupy an event-bus worker slot for the whole duration, and
      * stall any coroutine awaiting ``DataProfiled`` / ``state/dataset``.

    Instead: validate input + acquire the idempotency lock here, then
    spawn the profiling as a background task so the handler returns in
    O(ms). The UI already receives progressive updates via per-metafeature
    events emitted inside the dask graph's persist wrappers. Full
    migration to the exec-worker (per the CLAUDE.md event-bus split) is
    the follow-up; this fix removes the ``asyncio.to_thread`` block
    from the event-loop handler immediately.
    """
    user, session, did, fpath = event.data['uid'], event.data['session'], event.data['did'], event.data['fpath']

    # Idempotency: prevent concurrent profiling for same DID+session
    # (e.g. rapid page refresh, or seed_session re-triggering DataExists
    # while a previous profiling run is still in progress).
    lock_key = f"profiling:lock:{did}:{session}"
    rerun_key = f"{lock_key}:rerun"
    acquired = await aioredis.set(lock_key, "1", nx=True, ex=600)  # 10-min TTL
    if not acquired:
        # A profiling run is already in progress. Queue exactly one rerun so
        # answers arriving mid-run (e.g. quality metric inputs) are applied
        # immediately after the current run releases the lock.
        await aioredis.set(rerun_key, "1", ex=600)
        return  # already in progress

    # Lock acquired -- fire the actual profiling in the background and
    # return control to the event bus immediately. The task owns the
    # lock's lifecycle (release + rerun handling) via the try/finally
    # in _run_profiling.
    asyncio.create_task(
        _run_profiling(user, session, did, fpath, lock_key, rerun_key),
        name=f"profiling:{did}:{session[:8]}",
    )


async def _run_profiling(
    user: str,
    session: str,
    did: str,
    fpath: str,
    lock_key: str,
    rerun_key: str,
):
    """Background profiling job. Holds the profiling lock throughout;
    emits ``DataProfiled`` + state events on completion; on exception
    logs + releases the lock so the UI isn't stuck waiting on a ghost
    run. Safe to spawn via ``asyncio.create_task``."""
    import uuid as _uuid
    try:
        await aemit(Event('CheckingData', {
            'uid': user,
            'session': session,
            'did': did,
            'fpath': fpath,
        }))
        # Submit to the exec-worker: all pandas/numpy/sklearn compute
        # runs in the exec-worker container. The backend handler
        # ``handle_profile_and_quality_completed`` picks up the
        # ``DQCheckProfileAndQualityCompleted`` event and finishes the
        # work (Redis session meta, state/* xadd, column autodetect,
        # lock release, rerun handling).
        job_id = _uuid.uuid4().hex[:16]
        job_inputs = {
            'uid': user,
            'session': session,
            'did': did,
            'fpath': fpath,
            'lock_key': lock_key,
            'rerun_key': rerun_key,
            'lane': 'user',   # completion on user lane for snappy UI response
        }
        await aioredis.xadd(
            'exec:jobs',
            {
                'kind': 'dq_check:profile_and_quality',
                'job_id': job_id,
                'inputs': json.dumps(job_inputs),
                'submitted_at': repr(time.time()),
            },
            maxlen=100000, approximate=True,
        )
        # Lock stays held; release + rerun handling are the completion
        # handler's job. Return immediately.
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).exception(
            "profile-job submit failed for did=%s session=%s: %s",
            did, session, exc,
        )
        await aemit(Event("DataProfilingFailed", data={
            "uid": user, "session": session, "did": did,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }))
        # Submission failure: release the lock ourselves so the UI
        # isn't stuck waiting on a ghost job.
        await aioredis.delete(lock_key)
        rerun = await aioredis.get(rerun_key)
        if rerun:
            await aioredis.delete(rerun_key)


async def handle_profile_and_quality_completed(event: Event):
    """Completion handler for ``dq_check:profile_and_quality``.

    Receives ``DQCheckProfileAndQualityCompleted`` events emitted by
    the exec-worker after running the profiling + quality Dask graph.
    Responsible for everything that STAYS in the backend because it
    needs Neo4j (decision specs) / full session state / the Python
    event bus:

      * Neo4j KB read for quality decision specs.
      * Redis session-meta store (profile + quality + history +
        checks + inputs + column_profiles).
      * state/quality, state/quality-checks, state/column-profiles
        xadds on the user stream.
      * Column autodetect for first-time uploads (feature / target
        columns) so downstream task-inference can proceed.
      * Final DataProfiled emit + state/dataset xadd.
      * Releases the idempotency lock; processes the queued rerun.

    All compute-heavy work (pandas / numpy / sklearn) has already
    happened in the exec-worker; this handler is pure coordination.
    """
    payload = event.data or {}
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    error = payload.get("error")

    def _first(*vs, default=""):
        for v in vs:
            if v:
                return v
        return default

    user = _first(inputs.get("uid"), result.get("uid"))
    session = _first(inputs.get("session"), result.get("session"))
    did = _first(inputs.get("did"), result.get("did"))
    fpath = _first(inputs.get("fpath"), result.get("fpath"))
    lock_key = _first(
        inputs.get("lock_key"), result.get("lock_key"),
        default=f"profiling:lock:{did}:{session}",
    )
    rerun_key = _first(
        inputs.get("rerun_key"), result.get("rerun_key"),
        default=f"{lock_key}:rerun",
    )

    meta = None
    raw = None
    try:
        if error or not result:
            await aemit(Event("DataProfilingFailed", data={
                "uid": user, "session": session, "did": did,
                "error_message": error or "empty result from exec-worker",
            }))
            return

        profile_dict = dict(result.get("profile") or {})
        quality_dict = dict(result.get("quality") or {})
        all_columns = list(result.get("columns") or [])
        col_profiles = dict(result.get("column_profiles") or {})

        quality_names = list(quality_dict.keys())
        decision_specs = await get_quality_decision_specs_from_kg(quality_names)
        default_thresholds = {
            name: spec.threshold for name, spec in decision_specs.items()
        }
        threshold_override = get_quality_threshold_override(did)
        applied_threshold = (
            threshold_override if threshold_override is not None else 0.95
        )
        existing_history = []
        raw = await aioredis.get(RedisKeys.session_meta(session))
        if raw:
            meta = json.loads(raw)
            existing_history = list(
                (meta.get("dataset") or {}).get("quality_history") or []
            )
        checks = build_quality_checks(
            quality_dict,
            decision_specs,
            threshold_override,
            qm,
            quality_history=existing_history,
        )
        checks_summary = {
            "passed": sum(1 for c in checks if c["status"] == "passed"),
            "failed": sum(1 for c in checks if c["status"] == "failed"),
            "warning": sum(1 for c in checks if c["status"] == "warning"),
            "pending": sum(1 for c in checks if c["status"] == "pending"),
            "error": sum(1 for c in checks if c["status"] == "error"),
            "total": len(checks),
        }

        if raw:
            meta = json.loads(raw)
            ds = meta.get("dataset") or {}
            quality_history = list(ds.get("quality_history") or [])
            quality_history.append(quality_dict)
            quality_history = quality_history[-20:]
            ds["profile"] = profile_dict
            ds["quality"] = quality_dict
            ds["quality_history"] = quality_history
            ds["quality_checks"] = {
                "default_thresholds": default_thresholds,
                "threshold_override": threshold_override,
                "applied_threshold": applied_threshold,
                "decision_functions": {
                    name: spec.kind for name, spec in decision_specs.items()
                },
                "summary": checks_summary,
                "results": checks,
            }
            ds["quality_inputs"] = {
                "quality_threshold_mode": get_quality_threshold_mode(did),
                "quality_threshold_override": threshold_override,
                "syntactic_allowed_values": get_syntactic_allowed_values(did) or {},
                "sensitive_columns": get_sensitive_columns(did) or [],
                "semantic_accuracy_rules": get_semantic_accuracy_rules(did) or [],
                "inaccuracy_columns": get_inaccuracy_columns(did) or [],
                "range_rules": get_range_rules(did) or {},
                "value_occurrence_expectations": get_value_occurrence_expectations(did) or [],
                "category_column": get_category_column(did) or "",
                "balance_target_labels": get_balance_target_labels(did) or [],
                "compliance_rules": get_compliance_rules(did) or {},
                "consistency_label_threshold": get_consistency_label_threshold(did),
                "format_schema": get_format_schema(did) or {},
                "semantic_consistency_rules": get_semantic_consistency_rules(did) or [],
                "feature_effectiveness_rules": get_feature_effectiveness_rules(did) or {},
                "category_size_threshold": get_category_size_threshold(did),
                "label_effectiveness_rules": get_label_effectiveness_rules(did) or [],
                "target_size": get_target_size(did),
                "precision_requirements": get_precision_requirements(did) or {},
                "relevant_features": get_relevant_features(did) or [],
                "record_relevance_condition": get_record_relevance_condition(did) or {},
                "required_attributes": get_required_attributes(did) or [],
            }
            ds["columns"] = all_columns
            ds["column_profiles"] = col_profiles

            feat_raw = await aioredis.get(RedisKeys.dataset_feature_columns(did))
            tgt_raw = await aioredis.get(RedisKeys.dataset_target_columns(did))
            if feat_raw:
                ds["features"] = json.loads(feat_raw)
            if tgt_raw:
                ds["target"] = json.loads(tgt_raw)
            if "columns" not in ds:
                ds["columns"] = cached_read_csv(fpath).columns.tolist()

            meta["dataset"] = ds
            # Batch the SET + 3 XADDs into one Redis pipeline: was 4
            # sequential round-trips, now a single one. Shaves ~30-60ms
            # from the profile-completion latency on loaded networks.
            stream_key = RedisKeys.stream(user, session)
            pipe = aioredis.pipeline(transaction=False)
            pipe.set(RedisKeys.session_meta(session), json.dumps(meta))
            pipe.xadd(
                stream_key,
                {
                    "event": "state/quality",
                    "did": did,
                    "value": json.dumps(quality_dict),
                    "type": "json",
                },
                maxlen=STREAM_MAXLEN, approximate=True,
            )
            pipe.xadd(
                stream_key,
                {
                    "event": "state/quality-checks",
                    "did": did,
                    "value": json.dumps({
                        "default_thresholds": default_thresholds,
                        "threshold_override": threshold_override,
                        "applied_threshold": applied_threshold,
                        "decision_functions": {
                            name: spec.kind for name, spec in decision_specs.items()
                        },
                        "summary": checks_summary,
                        "results": checks,
                    }),
                    "type": "json",
                },
                maxlen=STREAM_MAXLEN, approximate=True,
            )
            pipe.xadd(
                stream_key,
                {
                    "event": "state/column-profiles",
                    "did": did,
                    "value": json.dumps(col_profiles),
                    "type": "json",
                },
                maxlen=STREAM_MAXLEN, approximate=True,
            )
            await pipe.execute()

        await sync_record_completeness_suggestions(user, session, did, checks)

        await _autodetect_columns_if_missing(
            user=user, session=session, did=did, fpath=fpath,
            all_columns=list(all_columns) if all_columns else None,
            col_profiles=col_profiles if col_profiles else None,
        )

        await aemit(Event("DataProfiled", data={
            "uid": user, "session": session, "did": did,
        }))

        stream = RedisKeys.stream(user, session)
        ds_blob = (
            meta.get("dataset", {}) if (meta is not None)
            else {"did": did, "fpath": fpath}
        )
        await aioredis.xadd(stream, {
            "event": "state/dataset",
            "value": json.dumps(ds_blob),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)
    except Exception as exc:
        _log.exception(
            "profile-completion handler failed for did=%s session=%s: %s",
            did, session, exc,
        )
        await aemit(Event("DataProfilingFailed", data={
            "uid": user, "session": session, "did": did,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }))
    finally:
        if lock_key:
            await aioredis.delete(lock_key)
        rerun = await aioredis.get(rerun_key) if rerun_key else None
        if rerun:
            await aioredis.delete(rerun_key)
            await aemit(Event("DataExists", data={
                "uid": user,
                "session": session,
                "did": did,
                "fpath": fpath,
            }))


# ---------------------------------------------------------------------------
# Group Node construction — triggered on operator drag-and-drop
# ---------------------------------------------------------------------------

async def handle_operator_dropped(event: Event):
    """Build a Group (compound sub-DAG) for a dropped operator and send it
    back to the frontend via WS.

    Triggered by ``PipelineNodeAdded``. If the operator is a compound
    operator, a ``state/group-created`` event is sent. Otherwise
    ``state/node-created`` is sent for simple operators.
    """
    from dorian.pipeline.group_builder import build_group

    uid = event.data.get("uid")
    session = event.data.get("session")
    payload = event.data.get("payload", event.data)

    op_name = payload.get("nodeName") or payload.get("name", "")
    node_id = payload.get("nodeId") or payload.get("node_id", "")

    if not op_name or "." not in op_name:
        return

    if not uid or not session:
        _log.warning("handle_operator_dropped: missing uid/session")
        return

    stream = RedisKeys.stream(uid, session)

    try:
        group = build_group(op_name, node_id)
    except Exception as exc:
        _log.error("Group build failed for %s: %s", op_name, exc, exc_info=True)
        group = None

    if group is not None:
        msg = {
            "event": "state/group-created",
            "nodeId": node_id,
            "value": json.dumps(group.to_dict()),
            "type": "json",
        }
        await aioredis.xadd(stream, _to_stream_fields(msg), maxlen=STREAM_MAXLEN, approximate=True)
        _log.info("Group created for %s (node %s)", op_name, node_id)
    else:
        msg = {
            "event": "state/node-created",
            "nodeId": node_id,
            "value": json.dumps({"name": op_name, "type": "operator"}),
            "type": "json",
        }
        await aioredis.xadd(stream, _to_stream_fields(msg), maxlen=STREAM_MAXLEN, approximate=True)
        _log.debug("Simple node created for %s (node %s)", op_name, node_id)
