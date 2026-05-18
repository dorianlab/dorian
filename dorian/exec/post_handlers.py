"""Worker-side wrappers for what used to be event-bus handlers.

Each ``@register("post:NAME")`` function below mirrors a former
``async def handle_X(event)`` in ``dorian/event/handlers/``. The rust
backend now subscribes to the original event types and submits these
jobs onto the ``exec:jobs`` stream; the python worker pops + runs the
job here. The functions return ``None`` (the completion event is
emitted by the worker but no rust handler subscribes to it — these
are end-of-chain side effects).

Why this pattern:
  * Rust owns event-bus subscription (the dispatch / routing layer).
  * Python owns the *implementation* logic that touches:
      - pandas / numpy / sklearn (CSV reads, statistical checks)
      - python-resident KB-query helpers that haven't ported yet
      - the python rule-rewrite engine (``dorian/code/parsing``)
      - the python recommendation engine (``dorian/pipeline/recommendation``)
      - the LLM extractor + MCP token issuance (HTTP fan-out)
  * All compute jobs share one queue + worker pool. No process-local
    asyncio event-bus.

Adding a new wrapper here is two steps:
  1. Wrap the function body. ``inputs`` replaces ``event.data``;
     ``aemit`` / ``aioredis`` continue to work — the worker runs in
     the same backend env.
  2. Add a rust handler that subscribes to the original event and
     calls ``submit_exec_job(state, "post:NAME", payload)``.

Removing a wrapper requires the matching rust subscriber to do the
work natively — preferred for hot-path orchestration. Each comment
below tags whether the wrapper is *temporary* (waiting on a full
rust port) or *terminal* (the implementation is genuinely python-
specific and stays here long-term).
"""

from __future__ import annotations

from typing import Any

from dorian.exec.registry import register


# ───────────────────────────────────────────────────────────────────
# DQ check chain — temporary; rust orchestration port pending.
# ───────────────────────────────────────────────────────────────────


@register("post:dq_check_profile_and_quality_completed")
async def post_profile_and_quality_completed(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Post-processing for ``DQCheckProfileAndQualityCompleted``.

    Mirrors ``dorian/event/handlers/pipeline_events.py::
    handle_profile_and_quality_completed`` byte-for-byte; only the
    outer-shell event signature changes.
    """
    from dorian.event.handlers.pipeline_events import (
        handle_profile_and_quality_completed,
    )
    from backend.events import Event

    event = Event(
        type="DQCheckProfileAndQualityCompleted",
        data=inputs,
    )
    await handle_profile_and_quality_completed(event)
    return {"ok": True}


@register("post:run_data_checks")
async def post_run_data_checks(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """KB-driven data validation — invokes the python toolbox checks
    on a dataset CSV. Mirrors ``risk_checks.run_data_checks``."""
    from dorian.event.handlers.risk_checks import run_data_checks
    from backend.events import Event
    await run_data_checks(Event(type="DataProfiled", data=inputs))
    return {"ok": True}


@register("post:evaluate_pathways")
async def post_evaluate_pathways(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Pathway evaluation — DQ-metric → model-risk pathway match.
    Mirrors ``risk_pathways.evaluate_pathways``."""
    from dorian.event.handlers.risk_pathways import evaluate_pathways
    from backend.events import Event
    await evaluate_pathways(Event(type="DataProfiled", data=inputs))
    return {"ok": True}


@register("post:debug_recommended_pipelines")
async def post_debug_recommended_pipelines(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Run the data checks against each recommended pipeline so the
    SPA can show check/report cards. Mirrors
    ``risk_pathways.debug_recommended_pipelines``."""
    from dorian.event.handlers.risk_pathways import debug_recommended_pipelines
    from backend.events import Event
    await debug_recommended_pipelines(Event(type="RecommendationsFetched", data=inputs))
    return {"ok": True}


@register("post:canvas_scope_revalidate")
async def post_canvas_scope_revalidate(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """CSV-backed revalidation triggered by canvas operator removal.
    Mirrors ``risk_debugger.handle_canvas_scope_updated``."""
    from dorian.event.handlers.risk_debugger import handle_canvas_scope_updated
    from backend.events import Event
    await handle_canvas_scope_updated(Event(type="CanvasScopeUpdated", data=inputs))
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────
# Recommendation engine — temporary; rust port retires python KDTree.
# ───────────────────────────────────────────────────────────────────


@register("post:attempt_recommendations")
async def post_attempt_recommendations(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Re-rank recommendations against the current session context.
    Mirrors ``recommendations.attempt_recommendations``."""
    from dorian.event.handlers.recommendations import attempt_recommendations
    from backend.events import Event
    trigger = inputs.pop("_trigger", "DataProfiled")
    await attempt_recommendations(Event(type=trigger, data=inputs))
    return {"ok": True}


@register("post:recommendation_interaction")
async def post_recommendation_interaction(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Re-rank after the user accepts/upvotes/downvotes a card.
    Mirrors ``recommendations._handle_interaction``."""
    from dorian.event.handlers.recommendations import _handle_interaction
    from backend.events import Event
    kind = inputs.pop("_kind", "selected")
    trigger = inputs.pop("_trigger", "PipelineRecommendationSelected")
    await _handle_interaction(Event(type=trigger, data=inputs), kind)
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────
# DAG rewrite chain — temporary; rust port uses engine/graph.
# ───────────────────────────────────────────────────────────────────


@register("post:apply_mitigation")
async def post_apply_mitigation(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Run a mitigation rewrite (KB rule or parameter change) against
    the active session pipeline. Mirrors
    ``risk_checks.apply_mitigation``."""
    from dorian.event.handlers.risk_checks import apply_mitigation
    from backend.events import Event
    trigger = inputs.pop("_trigger", "SuggestionAccepted")
    await apply_mitigation(Event(type=trigger, data=inputs))
    return {"ok": True}


@register("post:encoding_metafeature_error")
async def post_encoding_metafeature_error(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Auto-inject OrdinalEncoder when profiling reports a categorical
    error. Mirrors ``encoding.handle_encoding_on_metafeature_error``."""
    from dorian.event.handlers.encoding import handle_encoding_on_metafeature_error
    from backend.events import Event
    await handle_encoding_on_metafeature_error(
        Event(type="MetafeatureError", data=inputs)
    )
    return {"ok": True}


@register("post:node_execution_failed")
async def post_node_execution_failed(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Inspect a node-level execution failure and propose parameter
    fixes. Mirrors ``execution_error_handler.handle_node_execution_failed``."""
    from dorian.event.handlers.execution_error_handler import (
        handle_node_execution_failed,
    )
    from backend.events import Event
    await handle_node_execution_failed(Event(type="NodeExecutionFailed", data=inputs))
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────
# Compound DAG construction — temporary; group_builder rust port pending.
# ───────────────────────────────────────────────────────────────────


@register("post:operator_dropped")
async def post_operator_dropped(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Build a compound sub-DAG for a dropped operator (sklearn
    pipelines, guardrails, LLMs). Mirrors
    ``pipeline_events.handle_operator_dropped``."""
    from dorian.event.handlers.pipeline_events import handle_operator_dropped
    from backend.events import Event
    await handle_operator_dropped(Event(type="PipelineNodeAdded", data=inputs))
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────
# RL chain — terminal; the trainer is python-resident.
# ───────────────────────────────────────────────────────────────────


@register("post:rl_pipeline_run_failed")
async def post_rl_pipeline_run_failed(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.rl_error_mitigation import (
        handle_rl_pipeline_run_failed,
    )
    from backend.events import Event
    await handle_rl_pipeline_run_failed(Event(type="PipelineRunFailed", data=inputs))
    return {"ok": True}


@register("post:rl_mitigation_child_completed")
async def post_rl_mitigation_child_completed(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.rl_error_mitigation import (
        handle_rl_mitigation_child_completed,
    )
    from backend.events import Event
    trigger = inputs.pop("_trigger", "PipelineRunCompleted")
    await handle_rl_mitigation_child_completed(Event(type=trigger, data=inputs))
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────
# Extraction chain — terminal; LLM API calls.
# ───────────────────────────────────────────────────────────────────


@register("post:extract_pipeline")
async def post_extract_pipeline(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.extraction import handle_extract_pipeline
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_extract_pipeline)(Event(type="ExtractPipeline", data=inputs))
    return {"ok": True}


@register("post:extraction_corrected")
async def post_extraction_corrected(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.extraction import handle_extraction_corrected
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_extraction_corrected)(
        Event(type="ExtractionCorrected", data=inputs)
    )
    return {"ok": True}


@register("post:save_extraction_rules")
async def post_save_extraction_rules(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.extraction import handle_save_rules
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_save_rules)(Event(type="SaveExtractionRules", data=inputs))
    return {"ok": True}


@register("post:save_extraction_rule_specs")
async def post_save_extraction_rule_specs(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.extraction import handle_save_rule_specs
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_save_rule_specs)(
        Event(type="SaveExtractionRuleSpecs", data=inputs)
    )
    return {"ok": True}


@register("post:load_extraction_rules")
async def post_load_extraction_rules(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.extraction import handle_load_rules
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_load_rules)(Event(type="LoadExtractionRules", data=inputs))
    return {"ok": True}


@register("post:suggest_extraction_rules")
async def post_suggest_extraction_rules(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.extraction import handle_suggest_rules
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_suggest_rules)(Event(type="SuggestExtractionRules", data=inputs))
    return {"ok": True}


@register("post:cancel_suggest_extraction_rules")
async def post_cancel_suggest_extraction_rules(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.extraction import handle_cancel_suggest_rules
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_cancel_suggest_rules)(
        Event(type="CancelSuggestExtractionRules", data=inputs)
    )
    return {"ok": True}


@register("post:accept_extraction_rule")
async def post_accept_extraction_rule(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.extraction import handle_accept_rule
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_accept_rule)(Event(type="AcceptExtractionRule", data=inputs))
    return {"ok": True}


@register("post:reject_extraction_rule")
async def post_reject_extraction_rule(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.extraction import handle_reject_rule
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_reject_rule)(Event(type="RejectExtractionRule", data=inputs))
    return {"ok": True}


@register("post:create_mcp_token")
async def post_create_mcp_token(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    from dorian.event.handlers.mcp_handlers import handle_create_mcp_token
    from dorian.event.helpers.lifecycle import with_envelope
    from backend.events import Event
    await with_envelope(handle_create_mcp_token)(Event(type="CreateMcpToken", data=inputs))
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────
# Session-init Phase 3 (tooltips + ``state/queries`` fallback) — temporary.
# ───────────────────────────────────────────────────────────────────


@register("post:session_init_phase3")
async def post_session_init_phase3(
    inputs: dict[str, Any], *, job_id: str,
) -> dict[str, Any]:
    """Phase 3 of seed_session — fires the python-only tooltip + recs
    + dataset-profile-verification slice. Phase 1 + Phase 2 are
    rust-side."""
    from dorian.event.handlers.session import seed_session
    from backend.events import Event
    await seed_session(Event(type="InitSession", data=inputs))
    return {"ok": True}
