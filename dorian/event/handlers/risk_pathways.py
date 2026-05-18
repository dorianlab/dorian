"""
dorian/event/handlers/risk_pathways.py
---------------------------------------
Pipeline extraction utilities and pathway evaluation for the AI Debugger.

Contains pipeline JSON extraction helpers, the core ``_debug_pipeline`` loop,
``debug_recommended_pipelines``, and ``evaluate_pathways``.
"""

import asyncio
import json
from collections import defaultdict

import pandas as pd

from backend.cache import cached_read_csv
from backend.events import Event, aemit
from backend.envs import aioredis
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
from dorian.knowledge.queries import get_model_family, get_all_pathways

from .risk_kb import (
    CheckResult,
    _kb_risks_for_operator,
    _kb_checks_for_risk,
    _resolve_check_fn,
)


# ── Check deferral sets ─────────────────────────────────────────────────────

# Check functions have heterogeneous signatures.  At DataProfiled time we
# have: df, target_cols, feature_cols, protected_attributes.  Checks
# requiring train/test splits or before/after scaling data cannot run yet —
# they will be deferred to execution time.

_CHECKS_NEEDING_SPLITS = frozenset({
    "covariate_shift",
    "selection_bias",
    "sampling_bias",
    "domain_shift_bias",
})

_CHECKS_NEEDING_TRANSFORM = frozenset({
    "feature_scaling_bias",
    "outlier_bias",
})


# LLM guardrail checks operate on text, not DataFrames.
# They are skipped during dataset profiling and will be invoked at
# pipeline execution time when runtime text is available.
_LLM_CHECKS = frozenset({
    "prompt_injection_scan", "toxicity_scan", "pii_leak_scan", "hallucination_check",
    "sexual_content_scan", "discrimination_scan",
})


# ── Pipeline extraction helpers ─────────────────────────────────────────────

def _extract_pipeline_from_event(value) -> dict:
    """Extract a pipeline JSON dict from the various shapes ``PipelineRetrieved``
    can carry (pipelineHistory, version dict, or raw JSON string)."""
    if value is None:
        return {}

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {}

    # pipelineHistory → pick head pipeline
    if isinstance(value, dict) and "pipelines" in value:
        head_id = value.get("headId")
        for p in value["pipelines"]:
            if p.get("id") == head_id:
                pipeline_raw = p.get("pipeline", p)
                if isinstance(pipeline_raw, str):
                    try:
                        return json.loads(pipeline_raw)
                    except Exception:
                        return {}
                return pipeline_raw if isinstance(pipeline_raw, dict) else {}
        # Fallback: first pipeline
        if value["pipelines"]:
            p = value["pipelines"][0]
            pipeline_raw = p.get("pipeline", p)
            if isinstance(pipeline_raw, str):
                try:
                    return json.loads(pipeline_raw)
                except Exception:
                    return {}
            return pipeline_raw if isinstance(pipeline_raw, dict) else {}
        return {}

    # Single version dict with "pipeline" key
    if isinstance(value, dict) and "pipeline" in value:
        pipeline_raw = value["pipeline"]
        if isinstance(pipeline_raw, str):
            try:
                return json.loads(pipeline_raw)
            except Exception:
                return {}
        return pipeline_raw if isinstance(pipeline_raw, dict) else {}

    # Already a pipeline dict
    return value if isinstance(value, dict) else {}


def _extract_operators(pipeline_json) -> list[str]:
    """Extract operator FQNs from a pipeline JSON structure."""
    if pipeline_json is None:
        return []

    if isinstance(pipeline_json, str):
        try:
            pipeline_json = json.loads(pipeline_json)
        except Exception:
            return []

    body = pipeline_json.get("pipeline", pipeline_json)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return []

    nodes = body.get("nodes", {})
    operators: list[str] = []
    items = nodes.values() if isinstance(nodes, dict) else (nodes if isinstance(nodes, list) else [])
    for node in items:
        name = node.get("name", "")
        if "." in name:
            operators.append(name)
    return operators


# ── Check invocation ─────────────────────────────────────────────────────────

def _invoke_check(fn, check_name: str, df, target_cols, protected_attrs) -> CheckResult:
    """Call a toolbox check function with the right arguments.

    Different checks have different signatures — we inspect the name to
    decide which arguments to pass.  Returns a ``CheckResult`` with a
    human-readable message describing what was found.
    """
    if check_name in _LLM_CHECKS:
        return CheckResult(
            True,
            f"{check_name}: LLM content risk — guardrail mitigation recommended",
        )

    if check_name == "class_imbalance":
        if not target_cols:
            return CheckResult(False, "No target column specified")
        confirmed = bool(fn(df, target_cols[0]))
        msg = (
            f"Chi-squared test detected significant class imbalance in '{target_cols[0]}'"
            if confirmed else f"No significant class imbalance in '{target_cols[0]}'"
        )
        return CheckResult(confirmed, msg)

    if check_name == "group_bias":
        if not protected_attrs or not target_cols:
            return CheckResult(False, "No protected attributes or target column specified")
        confirmed = bool(fn(df, protected_attrs, target_cols[0]))
        msg = (
            f"Group bias detected between protected attributes {protected_attrs} and '{target_cols[0]}'"
            if confirmed else f"No group bias between {protected_attrs} and '{target_cols[0]}'"
        )
        return CheckResult(confirmed, msg)

    if check_name == "zero_variance_feature_bias":
        confirmed = bool(fn(df))
        n = sum(1 for c in df.columns if df[c].nunique() <= 1)
        msg = f"Found {n} zero-variance feature(s)" if confirmed else "No zero-variance features"
        return CheckResult(confirmed, msg)

    if check_name == "loss_of_ordinality_bias":
        confirmed = bool(fn(df))
        msg = "Unordered categorical columns detected" if confirmed else "No ordinality issues"
        return CheckResult(confirmed, msg)

    if check_name == "label_noise":
        return CheckResult(False, "Check not yet implemented")

    if check_name == "aggregation_bias":
        return CheckResult(False, "Check not yet implemented")

    if check_name in ("measurement_bias", "proxy_features", "distribution_shift"):
        return CheckResult(False, "Deferred — needs additional context")

    # Fallback: try calling with just df
    try:
        confirmed = bool(fn(df))
        msg = f"{check_name} detected" if confirmed else f"{check_name} not detected"
        return CheckResult(confirmed, msg)
    except TypeError:
        return CheckResult(False, f"{check_name} could not be invoked")


# ── Core debugging loop ─────────────────────────────────────────────────────

async def _debug_pipeline(
    uid: str,
    session: str,
    did: str,
    pipeline_body_or_operators: dict | list[str],
    pipeline_label: str,
    df: pd.DataFrame,
    target_cols: list[str],
    protected_attrs: list[str],
    *,
    pipeline_id: str = "",
) -> None:
    """Core debugging loop — run all applicable checks on a specific pipeline.

    Emits ``check/*`` progress events to the Redis stream so the frontend can
    display a live check report, and emits ``RiskIdentified`` for confirmed risks.

    ``pipeline_body_or_operators`` can be either a pipeline JSON dict (legacy
    path — operators are extracted via ``_extract_operators``) or a pre-built
    ``list[str]`` of operator FQNs (new path — used by ``run_data_checks``
    which reads the live canvas operator set from Redis).
    """
    stream = RedisKeys.stream(uid, session)

    if isinstance(pipeline_body_or_operators, list):
        operators = pipeline_body_or_operators
    else:
        operators = _extract_operators(pipeline_body_or_operators)

    if not operators:
        # Empty pipeline → nothing to debug (no operators to surface risks for).
        return

    # Accumulate results for the summary report
    results: list[dict] = []
    total = passed = failed = skipped = 0

    for operator in operators:
        if operator == "dataset":
            risks_to_check = [{"risk_name": "Class Imbalance"}] if target_cols else []
        else:
            risks_to_check = await _kb_risks_for_operator(operator)

        for risk_row in risks_to_check:
            risk_name = risk_row["risk_name"]
            check_names = await _kb_checks_for_risk(risk_name)
            if not check_names:
                continue

            for check_name in check_names:
                total += 1

                # Skip checks we can't run at profiling time
                if check_name in _CHECKS_NEEDING_SPLITS or check_name in _CHECKS_NEEDING_TRANSFORM:
                    skipped += 1
                    results.append({
                        "check": check_name, "risk": risk_name,
                        "operator": operator, "status": "skipped",
                        "message": "Deferred — needs train/test split or transform data",
                    })
                    continue

                fn = _resolve_check_fn(check_name)
                if fn is None:
                    skipped += 1
                    results.append({
                        "check": check_name, "risk": risk_name,
                        "operator": operator, "status": "skipped",
                        "message": f"Check function '{check_name}' not found",
                    })
                    continue

                # Emit check/started
                await aioredis.xadd(stream, {
                    "event": "check/started",
                    "check": check_name,
                    "risk": risk_name,
                    "operator": operator,
                    "pipeline_label": pipeline_label,
                }, maxlen=STREAM_MAXLEN, approximate=True)

                try:
                    # Run in a thread — check functions use sync emit()
                    # which raises in an async context.
                    result = await asyncio.to_thread(
                        _invoke_check, fn, check_name, df, target_cols, protected_attrs,
                    )
                except Exception as exc:
                    await aemit(Event("DataCheckError", {"check": check_name, "error": str(exc)}))
                    skipped += 1
                    results.append({
                        "check": check_name, "risk": risk_name,
                        "operator": operator, "status": "error",
                        "message": str(exc),
                    })
                    continue

                if result.confirmed:
                    failed += 1
                    results.append({
                        "check": check_name, "risk": risk_name,
                        "operator": operator, "status": "failed",
                        "message": result.message,
                    })

                    await aioredis.xadd(stream, {
                        "event": "check/failed",
                        "check": check_name,
                        "risk": risk_name,
                        "operator": operator,
                        "pipeline_label": pipeline_label,
                        "message": result.message,
                    }, maxlen=STREAM_MAXLEN, approximate=True)

                    await aemit(Event("DataCheckConfirmed", {"risk": risk_name, "operator": operator, "check": check_name, "pipeline_label": pipeline_label}))

                    await aemit(Event("RiskIdentified", data={
                        "uid": uid,
                        "session": session,
                        "operator": operator,
                        "risk": risk_name,
                        "status": "actionable",
                        "severity": "high",
                        "source": "data_check",
                        "check": check_name,
                        "pipeline_label": pipeline_label,
                        "pipeline_id": pipeline_id,
                        "check_message": result.message,
                    }))
                else:
                    passed += 1
                    results.append({
                        "check": check_name, "risk": risk_name,
                        "operator": operator, "status": "passed",
                        "message": result.message,
                    })

                    await aioredis.xadd(stream, {
                        "event": "check/passed",
                        "check": check_name,
                        "risk": risk_name,
                        "operator": operator,
                        "pipeline_label": pipeline_label,
                    }, maxlen=STREAM_MAXLEN, approximate=True)

    # Emit summary report
    await aioredis.xadd(stream, {
        "event": "check/report",
        "pipeline_label": pipeline_label,
        "total": str(total),
        "passed": str(passed),
        "failed": str(failed),
        "skipped": str(skipped),
        "results": json.dumps(results),
    }, maxlen=STREAM_MAXLEN, approximate=True)


# ═══════════════════════════════════════════════════════════════════════════
# Recommended pipeline debugging
# ═══════════════════════════════════════════════════════════════════════════

async def debug_recommended_pipelines(event: Event):
    """Debug all recommended pipelines in background.

    Triggered by ``RecommendationsFetched`` — runs data checks on each
    recommended pipeline and emits ``check/report`` + ``RiskIdentified``
    events tagged with the recommendation's label.

    This is fire-and-forget — does NOT block the recommendation display.
    """
    uid, session = event.data["uid"], event.data["session"]
    suggestions = event.data.get("suggestions", [])
    if not suggestions:
        return

    # ── Load session data (same as run_data_checks) ───────────────────────
    raw = await aioredis.get(RedisKeys.session_meta(session))
    if not raw:
        return
    meta = json.loads(raw)
    dataset = meta.get("dataset") or {}
    did = dataset.get("did", "")
    fpath = dataset.get("fpath")
    if not fpath:
        return

    try:
        df = cached_read_csv(fpath)
    except Exception as exc:
        await aemit(Event("DebugRecommendedPipelinesReadFailed", {"fpath": fpath, "error": str(exc)}))
        return

    target_raw = await aioredis.get(RedisKeys.dataset_target_columns(did))
    target_cols: list[str] = json.loads(target_raw) if target_raw else []

    protected_raw = await aioredis.get(f"dataset:{did}:protected_attributes")
    protected_attrs: list[str] = json.loads(protected_raw) if protected_raw else []

    # ── Debug each recommendation ─────────────────────────────────────────
    for idx, suggestion in enumerate(suggestions, 1):
        pipeline_body = suggestion.get("pipeline", suggestion)
        name = suggestion.get("name", "")
        label = name or f"Recommendation #{idx}"
        pipeline_id = str(
            pipeline_body.get("uuid")
            or pipeline_body.get("id")
            or pipeline_body.get("pipelineId")
            or f"rec-{idx}"
        )

        # Cache pipeline body so apply_mitigation can load it later.
        # default=str handles docstore ObjectId values that aren't JSON-serializable.
        await aioredis.set(
            RedisKeys.recommendation_pipeline(session, pipeline_id),
            json.dumps(pipeline_body, default=str),
            ex=3600,  # 1-hour TTL
        )

        try:
            await _debug_pipeline(
                uid, session, did,
                pipeline_body, label,
                df, target_cols, protected_attrs,
                pipeline_id=pipeline_id,
            )
        except Exception as exc:
            await aemit(Event("DebugRecommendedPipelinesFailed", {"label": label, "error": str(exc)}))


# ═══════════════════════════════════════════════════════════════════════════
# Pathway evaluation
# ═══════════════════════════════════════════════════════════════════════════

async def evaluate_pathways(event: Event):
    """Cross-view pathway evaluator — connects data quality to model selection.

    Triggered by ``DataProfiled``.  Queries KB pathway rules, checks current
    DQ metric values against pathway thresholds, filters by pipeline operator
    families, and emits matching pathways as AI Debugger suggestion cards
    through the existing ``MitigationActionsIdentified`` event chain.
    """
    uid, session = event.data["uid"], event.data["session"]
    did = event.data.get("did", "")

    # ── Collect current DQ metric values from Redis progress items ──────
    stream_key = RedisKeys.stream(uid, session)
    raw_entries = await aioredis.xrevrange(stream_key, count=500)
    metric_values: dict[str, float] = {}
    for _entry_id, entry in raw_entries:
        ev = entry.get("event", "")
        if ev != "progress":
            continue
        status = entry.get("status", "")
        if status != "computed":
            continue
        name = entry.get("metafeature", "")
        val = entry.get("value", "")
        if not name or not val:
            continue
        # Quality metric values may be JSON (e.g. ValueCompleteness returns
        # {overall: 0.95, ...}).  Extract 'overall' for dict values.
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                v = parsed.get("overall", parsed.get("value"))
                if v is not None:
                    metric_values.setdefault(name, float(v))
            elif isinstance(parsed, (int, float)):
                metric_values.setdefault(name, float(parsed))
        except (json.JSONDecodeError, TypeError, ValueError):
            try:
                metric_values.setdefault(name, float(val))
            except (TypeError, ValueError):
                pass

    if not metric_values:
        return

    # ── Collect pipeline operators from canvas ──────────────────────────
    operators = await aioredis.smembers(RedisKeys.canvas_operators(session))
    if not operators:
        return

    # ── Resolve operator families via KB ────────────────────────────────
    op_families: dict[str, str | None] = {}
    families_on_canvas: set[str] = set()
    for op in operators:
        fam = get_model_family(op)
        op_families[op] = fam
        if fam:
            families_on_canvas.add(fam)

    # ── Resolve task(s) on canvas ───────────────────────────────────────
    # We infer tasks from operators that perform them.
    tasks_on_canvas: set[str] = set()
    for op in operators:
        if op_families.get(op):
            # If it has a family, it's a model -> check what task it performs
            from dorian.knowledge.queries import get_operators_for_task
            for task_name in ("Classification", "Regression"):
                task_ops = get_operators_for_task(task_name)
                if op in task_ops:
                    tasks_on_canvas.add(task_name)

    # ── Load all pathway rules from KB ──────────────────────────────────
    pathways = get_all_pathways()

    for pathway in pathways:
        metric = pathway["metric"]
        direction = pathway["direction"]
        threshold = pathway["threshold"]

        # Check metric condition
        current_value = metric_values.get(metric)
        if current_value is None:
            continue

        if direction == "below" and current_value >= threshold:
            continue
        if direction == "above" and current_value <= threshold:
            continue

        # Check family filter
        pathway_families = pathway.get("families", [])
        if pathway_families and not families_on_canvas.intersection(pathway_families):
            continue

        # Check task filter
        pathway_task = pathway.get("task")
        if pathway_task and pathway_task not in tasks_on_canvas:
            continue

        # ── Emit as suggestion through existing pipeline ────────────────
        # Find specific operators on canvas that belong to target families
        target_ops = [
            op for op in operators
            if op_families.get(op) in pathway_families
        ] if pathway_families else list(operators)[:1]

        for target_op in (target_ops or ["dataset"]):
            family = op_families.get(target_op, "")
            desc = pathway["description"].format_map(defaultdict(str, {
                "operator": target_op.rsplit(".", 1)[-1] if "." in target_op else target_op,
                "family": family or "",
                "metric_value": f"{current_value:.2f}",
                "metric_value_pct": f"{current_value * 100:.0f}",
            }))

            risk = pathway["risk"]

            actions = [{
                "name": pathway["name"],
                "short": desc,
                "long": "",
            }]

            # Add preprocessing suggestions as alternatives field
            if pathway.get("preprocessing"):
                actions[0]["preprocessing"] = pathway["preprocessing"]
            if pathway.get("replacement"):
                actions[0]["replacement"] = pathway["replacement"]

            await aemit(Event("MitigationActionsIdentified", data={
                "uid": uid,
                "session": session,
                "operator": target_op,
                "risk": risk,
                "status": "actionable",
                "source": "pathway",
                "actions": actions,
                "pipeline_label": "Canvas",
                "check_message": f"{metric} = {current_value:.2f} "
                                 f"({'below' if direction == 'below' else 'above'} "
                                 f"threshold {threshold})",
            }))
