"""
dorian/event/handlers/risk_checks.py
--------------------------------------
Data checks and mitigation application for the AI Debugger.

Contains ``run_data_checks``, ``handle_suggestion_interaction``, and
``apply_mitigation`` — the HITL feedback loop.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from backend.cache import cached_read_csv
from backend.config import config
from backend.events import Event, aemit
from backend.envs import aioredis
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
from dorian.knowledge.sources.mitigations import render_description

from .risk_kb import (
    _short_name,
    _load_active_dataset_meta,
    _emit_mitigation_session_state,
    _DATASET_CONFIG_SUFFIXES_TO_COPY,
)
from .risk_pathways import (
    _extract_operators,
    _debug_pipeline,
)


# ═══════════════════════════════════════════════════════════════════════════
# Data-driven checks — KB-driven check discovery + invocation
# ═══════════════════════════════════════════════════════════════════════════

async def run_data_checks(event: Event):
    """KB-driven data validation — thin wrapper around ``_debug_pipeline``.

    Loads the active pipeline from session meta and delegates to the core
    debugging loop.
    """
    uid, session = event.data["uid"], event.data["session"]
    did = event.data.get("did", "")

    # ── Load session meta + dataset ──────────────────────────────────────
    raw = await aioredis.get(RedisKeys.session_meta(session))
    if not raw:
        return
    meta = json.loads(raw)
    dataset = meta.get("dataset") or {}
    fpath = dataset.get("fpath")
    if not fpath:
        return

    try:
        df = cached_read_csv(fpath)
    except Exception as exc:
        await aemit(Event("RunDataChecksReadFailed", {"fpath": fpath, "error": str(exc)}))
        return

    # ── Load columns ─────────────────────────────────────────────────────
    target_raw = await aioredis.get(RedisKeys.dataset_target_columns(did))
    target_cols: list[str] = json.loads(target_raw) if target_raw else []

    protected_raw = await aioredis.get(f"dataset:{did}:protected_attributes")
    protected_attrs: list[str] = json.loads(protected_raw) if protected_raw else []

    # ── Active pipeline operators (live canvas scope) ────────────────────
    # Use the live canvas operator set instead of the (potentially stale)
    # pipeline saved in session meta.  This ensures data checks only run
    # for operators that are actually on the current canvas.
    canvas_ops_raw = await aioredis.smembers(RedisKeys.canvas_operators(session))
    operators = [
        op.decode() if isinstance(op, bytes) else op
        for op in canvas_ops_raw
    ]

    # Fall back to extracting from session meta if canvas set is empty
    # (e.g. pipeline was loaded before the canvas tracking was populated).
    if not operators:
        pipeline_json = meta.get("pipeline", {})
        operators = _extract_operators(pipeline_json)

    pipeline_label = "Current pipeline"

    await _debug_pipeline(
        uid, session, did,
        operators, pipeline_label,
        df, target_cols, protected_attrs,
    )


# ═══════════════════════════════════════════════════════════════════════════
# HITL — handle user accept/reject of suggestions
# ═══════════════════════════════════════════════════════════════════════════

async def handle_suggestion_interaction(event: Event):
    """Handle user accept/reject/upvote/downvote of a risk mitigation
    suggestion.  Persists the interaction for session replay."""
    uid = event.data.get("uid")
    session = event.data.get("session")
    payload = event.data.get("payload", event.data)

    if event.type == "DataMitigationDecision":
        did = payload.get("did")
        check = payload.get("check")
        mitigation_action = payload.get("mitigation_action") or {}
        if not isinstance(mitigation_action, dict):
            mitigation_action = {}
        action = mitigation_action.get("dataset", {}).get("action") or mitigation_action.get("action")
        decision = payload.get("decision")

        meta, dataset = await _load_active_dataset_meta(session)
        if not meta or not dataset or not did or dataset.get("did") != did:
            return
        # Closed-loop behavior requested for dataset-quality mitigations:
        # accept -> apply immediately -> create a new active dataset version
        # -> re-enter DataExists/DataProfiled/check chain.
        if decision == "accept" and action:
            await aemit(Event("SuggestionAccepted", data={
                "uid": uid,
                "session": session,
                "suggestion": {
                    "action": action,
                    "risk": check or "DataQuality",
                    "task": "dataset",
                    "dataset": {"action": action},
                    "title": mitigation_action.get("title") or action,
                    "description": mitigation_action.get("description") or "",
                },
            }))
        # reject/ignore keeps the current dataset active and simply returns.
        return

    action_type = payload.get("type")
    suggestion = payload.get("suggestion", {})

    # Persist the interaction
    await aioredis.rpush(
        f"interactions:{uid}:{session}",
        json.dumps({"event": "SuggestionInteraction", **payload}),
    )

    if action_type == "accept":
        await aemit(Event("SuggestionAccepted", data={
            "uid": uid,
            "session": session,
            "suggestion": suggestion,
            # Forward inline pipeline from the canvas so apply_mitigation
            # can rewrite it even when the user hasn't saved yet.
            "pipeline": payload.get("pipeline"),
        }))
    elif action_type == "reject":
        await aemit(Event("SuggestionRejected", data={
            "uid": uid,
            "session": session,
            "suggestion": suggestion,
        }))


async def _apply_parameter_change(
    event: Event,
    uid: str,
    session: str,
    suggestion: dict,
    stream: str,
) -> bool:
    """Handle ``fix_type: "parameter_change"`` from execution error suggestions.

    Finds the target Parameter node in the pipeline DAG and updates its value,
    then emits the rewritten pipeline to the frontend.

    Returns ``True`` if handled (caller should return), ``False`` otherwise.
    """
    from dorian.pipeline.execution import _parse_pipeline

    param_name = suggestion.get("fix_param_name", "")
    fix_value = suggestion.get("fix_param_value", "")
    target_node_id = suggestion.get("fix_node_id", "")

    if not param_name or not fix_value:
        return False

    # Load pipeline from session (same priority chain as KB rewrite)
    raw = await aioredis.get(RedisKeys.session_meta(session))
    if not raw:
        await aioredis.xadd(stream, {
            "event": "ui/mitigation-failed",
            "value": json.dumps({
                "mitigation": f"Fix: {param_name}",
                "risk": suggestion.get("risk", ""),
                "reason": "No active session",
            }),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)
        return True

    meta = json.loads(raw)

    # Resolve pipeline data (same priority chain as the KB rewrite path)
    pipeline_data = None
    inline = event.data.get("pipeline")
    if isinstance(inline, dict) and inline.get("nodes"):
        pipeline_data = inline

    if not pipeline_data:
        pipeline_data = meta.get("pipeline")

    if not pipeline_data:
        history = meta.get("pipelineHistory")
        if isinstance(history, str):
            history = json.loads(history)
        if isinstance(history, dict):
            head_id = history.get("headId")
            pipelines = history.get("pipelines") or []
            pipeline_data = next(
                (p for p in pipelines if p.get("id") == head_id),
                pipelines[-1] if pipelines else None,
            )

    if not pipeline_data:
        await aioredis.xadd(stream, {
            "event": "ui/mitigation-failed",
            "value": json.dumps({
                "mitigation": f"Fix: {param_name}",
                "risk": suggestion.get("risk", ""),
                "reason": "No pipeline to rewrite",
            }),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)
        return True

    if isinstance(pipeline_data, str):
        pipeline_data = json.loads(pipeline_data)

    # Find and update the parameter node value in the pipeline JSON
    nodes = pipeline_data.get("nodes", {})
    edges = pipeline_data.get("edges", [])
    updated = False

    def _node_props(node: dict) -> dict:
        """Unwrap node properties — handles both ``{data: {...}}`` (frontend)
        and flat ``{type, name, ...}`` (docstore / DAG.to_json_dict) formats."""
        if not isinstance(node, dict):
            return {}
        d = node.get("data", node) if isinstance(node.get("data"), dict) else node
        return d if isinstance(d, dict) else {}

    def _is_parameter(props: dict) -> bool:
        """Match Parameter nodes regardless of case — docstores
        ``"Parameter"`` (class name), frontend stores ``"parameter"``."""
        return props.get("type", "").lower() == "parameter"

    def _is_operator(props: dict) -> bool:
        return props.get("type", "").lower() == "operator"

    # Strategy: find Parameter nodes connected to the target operator node
    # that match the param_name.
    #
    # If target_node_id is a compound-expanded node (contains _cx_), we look
    # for the base operator node and its parameter children.
    target_base = target_node_id
    if "_cx_" in target_base:
        target_base = target_base[:target_base.index("_cx_")]

    # Collect all node IDs that are plausible targets for the parameter
    target_nids: set[str] = set()
    if target_base and target_base in nodes:
        target_nids.add(target_base)
    # Also match by operator name in the node data
    operator_fqn = suggestion.get("task", "")
    for nid, node in nodes.items():
        props = _node_props(node)
        if _is_operator(props) and props.get("name") == operator_fqn:
            target_nids.add(nid)

    # Find Parameter nodes named param_name that connect to any target
    param_node_ids: set[str] = set()
    for nid, node in nodes.items():
        props = _node_props(node)
        nname = props.get("name", "") or props.get("text", "")
        if _is_parameter(props) and nname == param_name:
            if target_nids:
                # Check if this parameter connects to any target operator
                for edge in edges:
                    e_src = edge.get("source", "")
                    e_dst = edge.get("destination", "")
                    if e_src == nid and e_dst in target_nids:
                        param_node_ids.add(nid)
                        break
            else:
                # No target operators identified — accept by name alone
                param_node_ids.add(nid)

    # Broader fallback: any Parameter node with matching name (regardless
    # of edge connectivity)
    if not param_node_ids:
        for nid, node in nodes.items():
            props = _node_props(node)
            nname = props.get("name", "") or props.get("text", "")
            if _is_parameter(props) and nname == param_name:
                param_node_ids.add(nid)

    if not param_node_ids:
        await aioredis.xadd(stream, {
            "event": "ui/mitigation-failed",
            "value": json.dumps({
                "mitigation": f"Fix: {param_name}",
                "risk": suggestion.get("risk", ""),
                "reason": f"Could not find parameter '{param_name}' in the pipeline",
            }),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)
        return True

    # Update all matching parameter node values
    for nid in param_node_ids:
        props = _node_props(nodes[nid])
        old_value = props.get("value", "")
        props["value"] = fix_value
        updated = True
        await aemit(Event("ParameterValueFixed", data={
            "source": "execution_error_handler",
            "node_id": nid,
            "param_name": param_name,
            "old_value": str(old_value),
            "new_value": fix_value,
            "session": session,
        }))

    if not updated:
        return False

    # Emit the rewritten pipeline to the frontend
    await aioredis.xadd(stream, {
        "event": "pipeline/rewritten",
        "value": json.dumps(pipeline_data),
        "type": "json",
    }, maxlen=STREAM_MAXLEN, approximate=True)

    await aioredis.xadd(stream, {
        "event": "ui/mitigation-applied",
        "value": json.dumps({
            "mitigation": f"Fix: {param_name}",
            "risk": suggestion.get("risk", "Parameter Mismatch"),
            "instruction": (
                f"Updated {param_name} to {fix_value}. "
                f"Re-run the pipeline to verify the fix."
            ),
        }),
        "type": "json",
    }, maxlen=STREAM_MAXLEN, approximate=True)

    return True


async def apply_mitigation(event: Event):
    """Apply an accepted mitigation.

    1. Try to build a DAG rewrite from KB annotations (or Python catalog).
    2. If a rewrite is available → transform the pipeline, save, and notify.
    3. If no rewrite (diagnostic mitigation) → emit instruction toast (legacy).
    """
    from dorian.pipeline.mitigation_rewrites import build_mitigation_rewrite
    from dorian.pipeline.execution import _parse_pipeline

    uid = event.data.get("uid")
    session = event.data.get("session")
    suggestion = event.data.get("suggestion", {})
    mitigation = suggestion.get("action", "")
    operator = suggestion.get("task", "")
    risk = suggestion.get("risk", "")
    stream = RedisKeys.stream(uid, session)
    dataset_actions = {
        "remove_duplicate_records",
        "remove_records_with_missing_values",
        "impute_missing_values",
        "remove_records_with_missing_label",
        "impute_range_outliers_with_mean",
        "repair_syntactic_values",
        "enforce_compliance_rules",
        "normalize_format_values",
        "round_values_to_required_precision",
        "remove_irrelevant_records",
    }

    async def _emit_mitigation_failed(reason: str) -> None:
        """Push a failure notification to the frontend."""
        await aioredis.xadd(stream, {
            "event": "ui/mitigation-failed",
            "value": json.dumps({
                "mitigation": mitigation,
                "risk": risk,
                "reason": reason,
            }),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)

    def _syntactic_summary_counts(
        repair_log: list[dict] | None,
        review_queue: list[dict] | None,
    ) -> dict[str, int]:
        counts = {"exact": 0, "fuzzy": 0, "llm": 0, "review": 0}
        for entry in repair_log or []:
            method = str(entry.get("method", "")).lower()
            if method in counts:
                counts[method] += 1
        counts["review"] = len(review_queue or [])
        return counts

    def _syntactic_summary_text(
        repair_log: list[dict] | None,
        review_queue: list[dict] | None,
    ) -> str:
        counts = _syntactic_summary_counts(repair_log, review_queue)
        return (
            "Syntactic repair summary: "
            f"exact {counts['exact']}, "
            f"fuzzy {counts['fuzzy']}, "
            f"llm {counts['llm']}, "
            f"review {counts['review']}."
        )

    def _build_review_questions(
        did: str, review_queue: list[dict],
    ) -> list[dict]:
        """Build feedback questions from the syntactic repair review queue.

        Groups items by column and emits one multi-select question per column
        so the user can accept or reject each candidate replacement.
        """
        by_column: dict[str, list[dict]] = {}
        for item in review_queue:
            col = item.get("column", "unknown")
            by_column.setdefault(col, []).append(item)

        questions: list[dict] = []
        for col, items in by_column.items():
            options = []
            for item in items:
                original = item.get("original", "?")
                candidates = item.get("candidates", [])
                best = candidates[0] if candidates else {}
                best_val = best.get("value", "") if isinstance(best, dict) else str(best)
                score = best.get("score", 0) if isinstance(best, dict) else 0
                label = (
                    f"Row {item.get('row_index', '?')}: "
                    f'"{original}" -> "{best_val}" '
                    f"(confidence {score:.0%})"
                )
                options.append(label)

            questions.append({
                "id": f"dq:{did}:review:syntactic:{col}",
                "type": "multi-select",
                "question": (
                    f"Column '{col}': the following values could not be "
                    f"automatically repaired. Select values to accept the "
                    f"best-match suggestion:"
                ),
                "options": options,
                "initialValue": [],
            })

        return questions

    async def _finish_staged_dataset_mitigations() -> bool:
        meta, dataset = await _load_active_dataset_meta(session)
        if not meta or not dataset:
            await _emit_mitigation_failed("No active dataset selected")
            return True

        mitigation_session = dataset.get("mitigation_session")
        if not isinstance(mitigation_session, dict):
            return False

        accepted_actions = mitigation_session.get("accepted_actions") or []
        if not accepted_actions:
            dataset["mitigation_session"] = None
            meta["dataset"] = dataset
            await aioredis.set(RedisKeys.session_meta(session), json.dumps(meta))
            await _emit_mitigation_session_state(uid, session, dataset)
            return True

        from dorian.tabular.data.quality.mitigation import (
            enforce_compliance_rules,
            impute_range_outliers_with_mean,
            normalize_format_values,
            repair_syntactic_dataset,
            remove_duplicate_records,
            impute_missing_values,
            remove_records_with_missing_label,
            remove_records_with_missing_values,
            remove_irrelevant_records,
            round_values_to_required_precision,
        )

        source_fpath = mitigation_session.get("base_fpath") or dataset.get("fpath", "")
        source_did = mitigation_session.get("base_did") or dataset.get("did", "")
        if not source_fpath or not Path(source_fpath).exists():
            await _emit_mitigation_failed("Active dataset file is missing")
            return True

        action_map = {
            "remove_duplicate_records": remove_duplicate_records,
            "remove_records_with_missing_values": remove_records_with_missing_values,
            "impute_missing_values": impute_missing_values,
        }

        # Batch all dataset config reads into a single MGET round-trip
        _config_keys = [
            RedisKeys.dataset_target_columns(source_did),
            f"dataset:{source_did}:range_rules",
            f"dataset:{source_did}:syntactic_allowed_values",
            f"dataset:{source_did}:sensitive_columns",
            f"dataset:{source_did}:compliance_rules",
            f"dataset:{source_did}:format_schema",
            f"dataset:{source_did}:precision_requirements",
            f"dataset:{source_did}:record_relevance_condition",
        ]
        _config_vals = await aioredis.mget(_config_keys)
        def _jload(raw, default):
            return json.loads(raw) if raw else default
        target_cols: list[str] = _jload(_config_vals[0], [])
        range_rules = _jload(_config_vals[1], {})
        syntactic_allowed_values = _jload(_config_vals[2], {})
        sensitive_columns = _jload(_config_vals[3], [])
        compliance_rules = _jload(_config_vals[4], {})
        format_schema = _jload(_config_vals[5], {})
        precision_requirements = _jload(_config_vals[6], {})
        record_relevance_condition = _jload(_config_vals[7], {})

        df = await asyncio.to_thread(cached_read_csv, source_fpath)
        cleaned_df = df
        applied_history = []
        all_repair_logs: list[dict] = []
        applied_actions = set()
        syntactic_repair_log = []
        syntactic_review_queue = []
        for entry in accepted_actions:
            action_name = (
                (entry.get("dataset") or {}).get("action")
                or entry.get("action")
            )
            fn = action_map.get(action_name)
            if action_name in applied_actions:
                continue
            step_log: list[dict] = []
            if action_name == "remove_records_with_missing_label":
                if not target_cols:
                    continue
                cleaned_df, step_log = await asyncio.to_thread(
                    remove_records_with_missing_label,
                    cleaned_df,
                    target_cols[0],
                )
            elif action_name == "impute_range_outliers_with_mean":
                if not isinstance(range_rules, dict) or not range_rules:
                    continue
                for column_name, rule in range_rules.items():
                    if (
                        not isinstance(rule, (list, tuple))
                        or len(rule) != 2
                    ):
                        continue
                    cleaned_df, col_log = await asyncio.to_thread(
                        impute_range_outliers_with_mean,
                        cleaned_df,
                        str(column_name),
                        (rule[0], rule[1]),
                    )
                    step_log.extend(col_log)
            elif action_name == "repair_syntactic_values":
                if not isinstance(syntactic_allowed_values, dict) or not syntactic_allowed_values:
                    continue
                cleaned_df, syntactic_repair_log, syntactic_review_queue = await asyncio.to_thread(
                    repair_syntactic_dataset,
                    cleaned_df,
                    syntactic_allowed_values,
                    90,
                    5,
                    0.85,
                    sensitive_columns,
                )
                step_log = syntactic_repair_log
            elif action_name == "enforce_compliance_rules":
                if not isinstance(compliance_rules, dict) or not compliance_rules:
                    continue
                cleaned_df, step_log = await asyncio.to_thread(
                    enforce_compliance_rules,
                    cleaned_df,
                    compliance_rules,
                )
            elif action_name == "normalize_format_values":
                if not isinstance(format_schema, dict) or not format_schema:
                    continue
                cleaned_df, step_log = await asyncio.to_thread(
                    normalize_format_values,
                    cleaned_df,
                    format_schema,
                )
            elif action_name == "round_values_to_required_precision":
                if not isinstance(precision_requirements, dict) or not precision_requirements:
                    continue
                cleaned_df, step_log = await asyncio.to_thread(
                    round_values_to_required_precision,
                    cleaned_df,
                    precision_requirements,
                )
            elif action_name == "remove_irrelevant_records":
                if not isinstance(record_relevance_condition, dict) or not record_relevance_condition:
                    continue
                cleaned_df, step_log = await asyncio.to_thread(
                    remove_irrelevant_records,
                    cleaned_df,
                    record_relevance_condition,
                )
            elif fn is not None:
                cleaned_df, step_log = await asyncio.to_thread(fn, cleaned_df)
            else:
                continue
            applied_actions.add(action_name)
            applied_history.append({
                "check": entry.get("check"),
                "mitigation": action_name,
                "source_did": source_did,
                "changes": len(step_log),
            })
            all_repair_logs.extend(
                {**entry_log, "mitigation": action_name} for entry_log in step_log
            )

        if applied_history and syntactic_repair_log or syntactic_review_queue:
            await aemit(Event("SyntacticRepairSummary", data={
                "uid": uid,
                "session": session,
                "did": source_did,
                **_syntactic_summary_counts(syntactic_repair_log, syntactic_review_queue),
            }))

        # ── HITL: emit review questions for unresolved syntactic values ──
        if syntactic_review_queue:
            review_questions = _build_review_questions(
                source_did, syntactic_review_queue,
            )
            if review_questions:
                await aioredis.xadd(stream, {
                    "event": "state/queries",
                    "value": json.dumps(review_questions),
                    "type": "json",
                }, maxlen=STREAM_MAXLEN, approximate=True)

        if cleaned_df.equals(df):
            if any(entry.get("mitigation") == "repair_syntactic_values" for entry in applied_history):
                await aioredis.xadd(stream, {
                    "event": "ui/mitigation-applied",
                    "value": json.dumps({
                        "mitigation": "repair_syntactic_values",
                        "risk": "SyntacticDataAccuracy",
                        "instruction": (
                            f"{_syntactic_summary_text(syntactic_repair_log, syntactic_review_queue)} "
                            "No dataset values were changed."
                        ),
                    }),
                    "type": "json",
                }, maxlen=STREAM_MAXLEN, approximate=True)
                return True
            await _emit_mitigation_failed("Accepted mitigations made no changes to the dataset")
            return True

        new_did = str(uuid4())
        source_name = Path(source_fpath).stem
        output_dir = Path(config.fs.data) / str(uid)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{source_name}__mitigated__{new_did[:8]}.csv"
        await asyncio.to_thread(cleaned_df.to_csv, output_path, index=False)

        new_dataset = dict(dataset)
        history = list(dataset.get("mitigation_history") or [])
        history.extend(applied_history)
        new_dataset.update({
            "did": new_did,
            "fpath": output_path.absolute().as_posix(),
            "uid": uid,
            "session": session,
            "quality": None,
            "quality_checks": None,
            "profile": None,
            "source_dataset_did": source_did,
            "mitigation_history": history,
            "mitigation_session": None,
            "repair_log": all_repair_logs,
            "syntactic_repair_log": syntactic_repair_log,
            "syntactic_review_queue": syntactic_review_queue,
        })
        meta["dataset"] = new_dataset
        await aioredis.set(RedisKeys.session_meta(session), json.dumps(meta))
        await aioredis.set(RedisKeys.dataset_fpath(new_did), output_path.absolute().as_posix())

        key_pairs = [
            (RedisKeys.dataset_feature_columns(source_did), RedisKeys.dataset_feature_columns(new_did)),
            (RedisKeys.dataset_target_columns(source_did), RedisKeys.dataset_target_columns(new_did)),
        ]
        key_pairs.extend(
            (
                f"dataset:{source_did}:{suffix}",
                f"dataset:{new_did}:{suffix}",
            )
            for suffix in _DATASET_CONFIG_SUFFIXES_TO_COPY
        )
        for src_key, dst_key in key_pairs:
            value = await aioredis.get(src_key)
            if value is not None:
                await aioredis.set(dst_key, value)

        await aioredis.xadd(stream, {
            "event": "state/dataset",
            "value": json.dumps(new_dataset),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)
        await aioredis.xadd(stream, {
            "event": "state/data-mitigation-session",
            "did": new_did,
            "value": json.dumps(None),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)
        await aioredis.xadd(stream, {
            "event": "ui/mitigation-applied",
            "value": json.dumps({
                "mitigation": "finish_mitigation",
                "risk": "DataQuality",
                "instruction": (
                    f"Saved cleaned dataset to {output_path.name}. "
                    f"{_syntactic_summary_text(syntactic_repair_log, syntactic_review_queue)}"
                    if syntactic_repair_log or syntactic_review_queue
                    else f"Saved cleaned dataset to {output_path.name}"
                ),
            }),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)

        await aemit(Event("DataExists", data={
            "uid": uid,
            "session": session,
            "did": new_did,
            "fpath": output_path.absolute().as_posix(),
        }))
        return True

    async def _reset_staged_dataset_mitigations() -> bool:
        meta, dataset = await _load_active_dataset_meta(session)
        if not meta or not dataset:
            return True
        dataset["mitigation_session"] = None
        meta["dataset"] = dataset
        await aioredis.set(RedisKeys.session_meta(session), json.dumps(meta))
        await _emit_mitigation_session_state(uid, session, dataset)
        return True

    async def _apply_dataset_mitigation() -> bool:
        from dorian.tabular.data.quality.mitigation import (
            enforce_compliance_rules,
            impute_range_outliers_with_mean,
            normalize_format_values,
            repair_syntactic_dataset,
            remove_duplicate_records,
            impute_missing_values,
            remove_records_with_missing_label,
            remove_records_with_missing_values,
            remove_irrelevant_records,
            round_values_to_required_precision,
        )

        meta, dataset = await _load_active_dataset_meta(session)
        if not meta or not dataset:
            await _emit_mitigation_failed("No active dataset selected")
            return True

        source_fpath = dataset.get("fpath", "")
        source_did = dataset.get("did", "")
        if not source_fpath or not Path(source_fpath).exists():
            await _emit_mitigation_failed("Active dataset file is missing")
            return True

        mitigation_fn = {
            "remove_duplicate_records": remove_duplicate_records,
            "remove_records_with_missing_values": remove_records_with_missing_values,
            "impute_missing_values": impute_missing_values,
        }.get(mitigation)

        df = await asyncio.to_thread(cached_read_csv, source_fpath)
        syntactic_repair_log = []
        syntactic_review_queue = []
        if mitigation == "remove_records_with_missing_label":
            target_raw = await aioredis.get(RedisKeys.dataset_target_columns(source_did))
            target_cols: list[str] = json.loads(target_raw) if target_raw else []
            if not target_cols:
                await _emit_mitigation_failed("No target column selected for label-completeness mitigation")
                return True
            cleaned_df = await asyncio.to_thread(remove_records_with_missing_label, df, target_cols[0])
        elif mitigation == "impute_range_outliers_with_mean":
            range_rules_raw = await aioredis.get(f"dataset:{source_did}:range_rules")
            range_rules = json.loads(range_rules_raw) if range_rules_raw else {}
            if not isinstance(range_rules, dict) or not range_rules:
                await _emit_mitigation_failed("No range rules configured for range-based mitigation")
                return True
            cleaned_df = df
            for column_name, rule in range_rules.items():
                if not isinstance(rule, (list, tuple)) or len(rule) != 2:
                    continue
                cleaned_df = await asyncio.to_thread(
                    impute_range_outliers_with_mean,
                    cleaned_df,
                    str(column_name),
                    (rule[0], rule[1]),
                )
        elif mitigation == "repair_syntactic_values":
            syntactic_allowed_values_raw = await aioredis.get(f"dataset:{source_did}:syntactic_allowed_values")
            syntactic_allowed_values = json.loads(syntactic_allowed_values_raw) if syntactic_allowed_values_raw else {}
            sensitive_columns_raw = await aioredis.get(f"dataset:{source_did}:sensitive_columns")
            sensitive_columns = json.loads(sensitive_columns_raw) if sensitive_columns_raw else []
            if not isinstance(syntactic_allowed_values, dict) or not syntactic_allowed_values:
                await _emit_mitigation_failed("No syntactic allowed values configured for syntactic repair")
                return True
            cleaned_df, syntactic_repair_log, syntactic_review_queue = await asyncio.to_thread(
                repair_syntactic_dataset,
                df,
                syntactic_allowed_values,
                90,
                5,
                0.85,
                sensitive_columns,
            )
        elif mitigation == "enforce_compliance_rules":
            compliance_rules_raw = await aioredis.get(f"dataset:{source_did}:compliance_rules")
            compliance_rules = json.loads(compliance_rules_raw) if compliance_rules_raw else {}
            if not isinstance(compliance_rules, dict) or not compliance_rules:
                await _emit_mitigation_failed("No compliance rules configured for compliance mitigation")
                return True
            cleaned_df = await asyncio.to_thread(
                enforce_compliance_rules,
                df,
                compliance_rules,
            )
        elif mitigation == "normalize_format_values":
            format_schema_raw = await aioredis.get(f"dataset:{source_did}:format_schema")
            format_schema = json.loads(format_schema_raw) if format_schema_raw else {}
            if not isinstance(format_schema, dict) or not format_schema:
                await _emit_mitigation_failed("No format schema configured for format normalization")
                return True
            cleaned_df = await asyncio.to_thread(
                normalize_format_values,
                df,
                format_schema,
            )
        elif mitigation == "round_values_to_required_precision":
            precision_requirements_raw = await aioredis.get(f"dataset:{source_did}:precision_requirements")
            precision_requirements = json.loads(precision_requirements_raw) if precision_requirements_raw else {}
            if not isinstance(precision_requirements, dict) or not precision_requirements:
                await _emit_mitigation_failed("No precision requirements configured for precision mitigation")
                return True
            cleaned_df = await asyncio.to_thread(
                round_values_to_required_precision,
                df,
                precision_requirements,
            )
        elif mitigation == "remove_irrelevant_records":
            record_relevance_condition_raw = await aioredis.get(f"dataset:{source_did}:record_relevance_condition")
            record_relevance_condition = json.loads(record_relevance_condition_raw) if record_relevance_condition_raw else {}
            if not isinstance(record_relevance_condition, dict) or not record_relevance_condition:
                await _emit_mitigation_failed("No record relevance condition configured for relevance mitigation")
                return True
            cleaned_df = await asyncio.to_thread(
                remove_irrelevant_records,
                df,
                record_relevance_condition,
            )
        elif mitigation_fn is not None:
            cleaned_df = await asyncio.to_thread(mitigation_fn, df)
        else:
            return False

        if mitigation == "repair_syntactic_values":
            await aemit(Event("SyntacticRepairSummary", data={
                "uid": uid,
                "session": session,
                "did": source_did,
                **_syntactic_summary_counts(syntactic_repair_log, syntactic_review_queue),
            }))

        if cleaned_df.equals(df):
            if mitigation == "repair_syntactic_values":
                await aioredis.xadd(stream, {
                    "event": "ui/mitigation-applied",
                    "value": json.dumps({
                        "mitigation": mitigation,
                        "risk": risk,
                        "instruction": (
                            f"{_syntactic_summary_text(syntactic_repair_log, syntactic_review_queue)} "
                            "No dataset values were changed."
                        ),
                    }),
                    "type": "json",
                }, maxlen=STREAM_MAXLEN, approximate=True)
                return True
            await _emit_mitigation_failed("Mitigation made no changes to the dataset")
            return True

        new_did = str(uuid4())
        source_name = Path(source_fpath).stem
        output_dir = Path(config.fs.data) / str(uid)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{source_name}__{mitigation}__{new_did[:8]}.csv"
        await asyncio.to_thread(cleaned_df.to_csv, output_path, index=False)

        new_dataset = dict(dataset)
        history = list(dataset.get("mitigation_history") or [])
        history.append({
            "mitigation": mitigation,
            "source_did": source_did,
            "output_did": new_did,
            "output_path": output_path.absolute().as_posix(),
        })
        new_dataset.update({
            "did": new_did,
            "fpath": output_path.absolute().as_posix(),
            "uid": uid,
            "session": session,
            "quality": None,
            "quality_checks": None,
            "profile": None,
            "source_dataset_did": source_did,
            "mitigation_history": history,
            "syntactic_repair_log": syntactic_repair_log,
            "syntactic_review_queue": syntactic_review_queue,
        })
        meta["dataset"] = new_dataset
        await aioredis.set(RedisKeys.session_meta(session), json.dumps(meta))
        await aioredis.set(RedisKeys.dataset_fpath(new_did), output_path.absolute().as_posix())

        key_pairs = [
            (RedisKeys.dataset_feature_columns(source_did), RedisKeys.dataset_feature_columns(new_did)),
            (RedisKeys.dataset_target_columns(source_did), RedisKeys.dataset_target_columns(new_did)),
        ]
        key_pairs.extend(
            (
                f"dataset:{source_did}:{suffix}",
                f"dataset:{new_did}:{suffix}",
            )
            for suffix in _DATASET_CONFIG_SUFFIXES_TO_COPY
        )
        for src_key, dst_key in key_pairs:
            value = await aioredis.get(src_key)
            if value is not None:
                await aioredis.set(dst_key, value)

        await aioredis.xadd(stream, {
            "event": "state/dataset",
            "value": json.dumps(new_dataset),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)
        await aioredis.xadd(stream, {
            "event": "ui/mitigation-applied",
            "value": json.dumps({
                "mitigation": mitigation,
                "risk": risk,
                "instruction": (
                    f"Saved cleaned dataset to {output_path.name}. "
                    f"{_syntactic_summary_text(syntactic_repair_log, syntactic_review_queue)}"
                    if mitigation == "repair_syntactic_values"
                    else f"Saved cleaned dataset to {output_path.name}"
                ),
            }),
            "type": "json",
        }, maxlen=STREAM_MAXLEN, approximate=True)

        await aemit(Event("DataExists", data={
            "uid": uid,
            "session": session,
            "did": new_did,
            "fpath": output_path.absolute().as_posix(),
        }))
        return True

    if event.type == "DataMitigationFinish":
        handled = await _finish_staged_dataset_mitigations()
        if handled:
            return

    if event.type == "DataMitigationReset":
        handled = await _reset_staged_dataset_mitigations()
        if handled:
            return

    if mitigation in dataset_actions:
        handled = await _apply_dataset_mitigation()
        if handled:
            return

    # ── Execution-error parameter change ─────────────────────────────────
    fix_type = suggestion.get("fix_type", "")
    if fix_type == "parameter_change":
        handled = await _apply_parameter_change(event, uid, session, suggestion, stream)
        if handled:
            return

    # ── Execution-error structural rewrite ───────────────────────────────
    # For structural fixes (e.g. "insert an OrdinalEncoder before the
    # failing estimator"), the suggestion carries the ``doc_rewrites``
    # slug directly in ``fix_mitigation_slug``. We resolve the slug to a
    # compiled ``RewriteRule`` and fall through into the generic
    # rewrite-application code below by rebinding ``mitigation`` to the
    # slug so ``build_mitigation_rewrite`` below looks it up correctly.
    if fix_type == "structural_rewrite":
        slug = suggestion.get("fix_mitigation_slug", "")
        if slug:
            # Rebind the mitigation *name* used by the generic path below
            # so it finds our slug in the docstore via
            # ``find_one({"_id": slug})``. ``operator`` already comes from
            # ``suggestion["task"]`` which is the failing operator FQN.
            mitigation = slug

    # ── Load the pipeline BEFORE building the rewrite ─────────────────────
    # A structural_rewrite's docstore rule pattern matches on the failing
    # operator's class FQN (e.g. ``sklearn.linear_model.LogisticRegression``).
    # The suggestion's ``task`` field carries whatever ``_extract_operator_fqn``
    # produced at error-emit time — which, for compound-expanded failure
    # sites (``{uuid}_cx_fit_1``), is a module path from the traceback
    # (e.g. ``sklearn.linear_model._logistic``), NOT the class FQN.
    # The pattern then fails to match any DAG node and the rewrite is a
    # no-op. Load the pipeline now so we can re-resolve ``operator`` from
    # the actual pre-expansion DAG node identified by ``suggestion.node_id``.
    raw = await aioredis.get(RedisKeys.session_meta(session))
    if not raw:
        await aemit(Event("ApplyMitigationNoSessionMeta", {"session": session}))
        await _emit_mitigation_failed("No active session — upload a dataset and select a pipeline first")
        return

    meta = json.loads(raw)
    pipeline_data = None

    # Priority 0: inline pipeline sent with the acceptance event
    # (the canvas may not have been saved to Redis yet).
    inline = event.data.get("pipeline")
    if isinstance(inline, dict) and inline.get("nodes"):
        pipeline_data = inline
        await aemit(Event("ApplyMitigationUsingInlinePipeline", {"session": session}))

    # Priority 1: if the suggestion references a specific recommendation
    # candidate, load that pipeline from the recommendation cache.
    if not pipeline_data:
        suggestion_pipeline_id = suggestion.get("pipeline_id", "")
        if suggestion_pipeline_id:
            cached = await aioredis.get(
                RedisKeys.recommendation_pipeline(session, suggestion_pipeline_id)
            )
            if cached:
                pipeline_data = json.loads(cached)
                await aemit(Event("ApplyMitigationLoadedRecommendation", {"pipeline_id": suggestion_pipeline_id}))

    # Priority 2: current pipeline from session meta
    if not pipeline_data:
        pipeline_data = meta.get("pipeline")

    # Priority 3: head version from pipelineHistory
    if not pipeline_data:
        history = meta.get("pipelineHistory")
        if isinstance(history, str):
            history = json.loads(history)
        if isinstance(history, dict):
            head_id = history.get("headId")
            pipelines = history.get("pipelines") or []
            pipeline_data = next(
                (p for p in pipelines if p.get("id") == head_id),
                pipelines[-1] if pipelines else None,
            )

    if not pipeline_data:
        await aemit(Event("ApplyMitigationNoPipeline", {"session": session}))
        await _emit_mitigation_failed("No pipeline to rewrite — select or build a pipeline first")
        return
    if isinstance(pipeline_data, str):
        pipeline_data = json.loads(pipeline_data)

    # ── Re-resolve operator FQN from the actual pipeline ─────────────────
    # Use the suggestion's node_id (which points at the compound-expanded
    # fit/predict/transform node) to look up the parent class operator.
    # Falls back to the original ``operator`` string when resolution fails
    # (e.g. top-level non-compound nodes).
    suggestion_node_id = suggestion.get("node_id", "")
    if fix_type == "structural_rewrite" and suggestion_node_id:
        try:
            _parsed = await asyncio.to_thread(
                _parse_pipeline, pipeline_data, False,
            )
            from dorian.dag import Operator as _Operator
            base = suggestion_node_id
            if "_cx_" in base:
                base = base[: base.index("_cx_")]
            _n = _parsed.nodes.get(base)
            if isinstance(_n, _Operator) and _n.name and "." in _n.name:
                if _n.name != operator:
                    await aemit(Event("ApplyMitigationFqnResolved", {
                        "session": session,
                        "original": operator,
                        "resolved": _n.name,
                        "node_id": suggestion_node_id,
                    }))
                operator = _n.name
        except Exception:
            pass  # resolution is best-effort; fall through with original

    # ── Try KB-driven rewrite ─────────────────────────────────────────────
    import time as _time
    _t_start = _time.perf_counter()
    rewrite_fn = await build_mitigation_rewrite(mitigation, operator, suggestion)
    _t_build = _time.perf_counter()

    if rewrite_fn is not None:

        try:
            import time as _time

            # ── Phase 1: Parse + rewrite (sync-heavy — run in thread) ──
            _t0 = _time.perf_counter()

            def _sync_rewrite():
                dag = _parse_pipeline(pipeline_data, flatten_groups=False)
                rewritten_dag = rewrite_fn(dag)
                return rewritten_dag.to_frontend_dict()

            rewritten_json = await asyncio.to_thread(_sync_rewrite)
            _t1 = _time.perf_counter()

            # ── Phase 2: Merge UI metadata + KB enrichment ─────────────
            orig_nodes = (pipeline_data.get("nodes") or {})
            rewritten_edges = rewritten_json.get("edges", [])

            def _enrich_nodes():
                """Sync KB queries — run in thread to avoid blocking the event loop."""
                for nid, rn in rewritten_json.get("nodes", {}).items():
                    orig = orig_nodes.get(nid)
                    if orig:
                        orig_data = orig.get("data", orig) if isinstance(orig.get("data"), dict) else orig
                        for key in ("inputs", "outputs", "children", "internalEdges",
                                    "ioMap", "collapsed", "sourceInterface", "position"):
                            if key in orig_data and key not in rn:
                                rn[key] = orig_data[key]
                            elif key in orig and key not in rn:
                                rn[key] = orig[key]
                    elif rn.get("type") == "operator" and "." in rn.get("name", ""):
                        # Use the full generation-catalog resolver so
                        # per-operator Python overrides (e.g. the 4-output
                        # shape of sklearn.model_selection.train_test_split
                        # in _FUNCTION_IO_OVERRIDES) take precedence over
                        # the generic interface-level template. Without
                        # this, train_test_split renders with the single
                        # Function-interface output and the UI loses the
                        # X_train / X_test / y_train / y_test handles
                        # after a mitigation rewrite passes through here.
                        try:
                            from dorian.pipeline.generation.catalog import _resolve_io
                            from dorian.knowledge.queries import get_operator_interface
                            iface = get_operator_interface(rn["name"])
                            io = _resolve_io(rn["name"], iface)
                            if io is not None:
                                inputs_spec, outputs_spec = io
                                if inputs_spec:
                                    rn["inputs"] = [
                                        {"name": p.name, "position": p.position, "type": p.dtype}
                                        for p in inputs_spec
                                    ]
                                if outputs_spec:
                                    rn["outputs"] = [
                                        {"name": p.name, "position": p.position, "type": p.dtype}
                                        for p in outputs_spec
                                    ]
                        except Exception:
                            pass

                # Derive handles from actual DAG edges for ALL nodes.
                all_nodes = rewritten_json.get("nodes", {})
                for edge in rewritten_edges:
                    dst_id = edge.get("destination")
                    src_id = edge.get("source")
                    pos = edge.get("position")
                    out = edge.get("output")

                    dst_node = all_nodes.get(dst_id)
                    if dst_node and pos is not None:
                        inputs = dst_node.setdefault("inputs", [])
                        pos_str = str(pos)
                        if not any(str(inp.get("name", inp.get("position", ""))) == pos_str for inp in inputs):
                            src_node = all_nodes.get(src_id, {})
                            inp_type = src_node.get("dtype", "any")
                            inputs.append({"name": pos_str, "type": inp_type})

                    src_node = all_nodes.get(src_id)
                    if src_node and out is not None:
                        outputs = src_node.setdefault("outputs", [])
                        out_str = str(out)
                        if not any(str(o.get("name", o.get("position", ""))) == out_str for o in outputs):
                            outputs.append({"name": out_str, "type": "any"})

            await asyncio.to_thread(_enrich_nodes)
            _t2 = _time.perf_counter()

            # Update session meta with the rewritten pipeline
            meta["pipeline"] = rewritten_json
            await aioredis.set(RedisKeys.session_meta(session), json.dumps(meta))

            # Build a pipeline history entry for the frontend
            pipeline_history = meta.get("pipelineHistory")
            if isinstance(pipeline_history, str):
                pipeline_history = json.loads(pipeline_history)

            # Initialise pipelineHistory when none exists yet (e.g. the
            # user is viewing a recommendation candidate that hasn't been
            # "selected" into the sidebar history).
            if not pipeline_history or not isinstance(pipeline_history, dict):
                orig_id = str(uuid4())
                pipeline_history = {
                    "uuid": str(uuid4()),
                    "headId": orig_id,
                    "pipelines": [{
                        "id": orig_id,
                        "createdAt": datetime.now().isoformat(),
                        "message": "Original pipeline",
                        "pipeline": pipeline_data,
                        "nodes": pipeline_data.get("nodes", {}),
                        "edges": pipeline_data.get("edges", []),
                    }],
                }

            # Add rewritten pipeline as a new version in the history
            version_id = str(uuid4())
            new_version = {
                "id": version_id,
                "parentPipelineId": pipeline_history.get("uuid", ""),
                "createdAt": datetime.now().isoformat(),
                "message": f"Mitigation: {mitigation} for {risk}",
                "pipeline": rewritten_json,
                "nodes": rewritten_json.get("nodes", {}),
                "edges": rewritten_json.get("edges", []),
            }
            pipeline_history.setdefault("pipelines", []).append(new_version)
            pipeline_history["headId"] = version_id
            meta["pipelineHistory"] = pipeline_history
            await aioredis.set(RedisKeys.session_meta(session), json.dumps(meta))

            # Emit updated pipeline history to frontend
            await aioredis.xadd(stream, {
                "event": "state/pipeline",
                "value": json.dumps(pipeline_history),
                "type": "json",
            }, maxlen=STREAM_MAXLEN, approximate=True)

            # Emit rewrite notification — includes the full rewritten
            # pipeline so the frontend can update tempPipeline immediately.
            summary = f"{mitigation} applied: {_short_name(operator)} → rewritten"
            await aioredis.xadd(stream, {
                "event": "pipeline/rewritten",
                "mitigation": mitigation,
                "operator": operator,
                "risk": risk,
                "summary": summary,
                "pipeline": json.dumps(rewritten_json),
            }, maxlen=STREAM_MAXLEN, approximate=True)

            # Mirror the structural rewrite to the user-facing toast
            # channel so the "what changed and why" message format
            # matches the parameter_change branch (line ~344). The
            # canvas already updates via pipeline/rewritten above; this
            # second event is the explicit notification — important
            # when a mitigation auto-applies (RL system uid skips this
            # by design; future human auto-apply modes inherit the
            # toast for free).
            await aioredis.xadd(stream, {
                "event": "ui/mitigation-applied",
                "value": json.dumps({
                    "mitigation": mitigation,
                    "risk": risk,
                    "instruction": (
                        f"Applied {mitigation} to {_short_name(operator)}. "
                        f"Re-run the pipeline to verify the fix."
                    ),
                }),
                "type": "json",
            }, maxlen=STREAM_MAXLEN, approximate=True)

            _t3 = _time.perf_counter()
            await aemit(Event("MitigationRewriteApplied", {
                "mitigation": mitigation, "risk": risk, "operator": operator,
                "timing": {
                    "build_rewrite_s": round(_t_build - _t_start, 3),
                    "parse_and_rewrite_s": round(_t1 - _t0, 3),
                    "kb_enrichment_s": round(_t2 - _t1, 3),
                    "redis_and_emit_s": round(_t3 - _t2, 3),
                    "total_s": round(_t3 - _t_start, 3),
                },
            }))
            return

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            await aemit(Event("MitigationRewriteFailed", {
                "mitigation": mitigation, "operator": operator,
                "error": str(exc), "traceback": tb,
            }))
            await _emit_mitigation_failed(f"Rewrite failed for {mitigation} on {operator}: {exc}")
            # Fall through to toast behavior

    # ── Fallback: diagnostic mitigation → instruction toast ───────────────
    instruction = suggestion.get("description_long", "")
    if not instruction:
        instruction = render_description(
            mitigation, operator=operator, risk=risk, long=True,
        )

    await aioredis.xadd(stream, {
        "event": "ui/mitigation-applied",
        "value": json.dumps({
            "mitigation": mitigation,
            "operator": operator,
            "risk": risk,
            "instruction": instruction,
            "alternatives": suggestion.get("alternatives", "[]"),
            "status": "suggested",
        }),
        "type": "json",
    }, maxlen=STREAM_MAXLEN, approximate=True)

    await aemit(Event("MitigationInstructionEmitted", {"mitigation": mitigation, "risk": risk, "operator": operator}))
