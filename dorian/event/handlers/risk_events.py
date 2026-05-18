"""
dorian/event/handlers/risk_events.py
--------------------------------------
Barrel re-export — preserves the original public API so that
``dorian.event.registry`` (and any other importer) continues to work
unchanged after the split into sub-modules.

Sub-modules:
  - risk_kb        — KB fetch helpers, CheckResult, shared utilities
  - risk_debugger  — core AI Debugger event handlers + canvas scope
  - risk_pathways  — pipeline extraction + pathway evaluation
  - risk_checks    — data checks + mitigation application (HITL)
"""

# ── risk_kb (shared helpers) ────────────────────────────────────────────────
from .risk_kb import (                          # noqa: F401
    CheckResult,
    _short_name,
    _has_rewrite_rule,
    _resolve_check_fn,
    _record_completeness_actions,
    _emit_mitigation_session_state,
    _load_active_dataset_meta,
    _kb_risks_for_operator,
    _kb_mitigations_for_risk,
    _kb_principles_for_risk,
    _kb_checks_for_risk,
    _kb_descriptions_for_mitigation,
    _kb_mitigations_with_descriptions,
    _kb_direct_alternatives,
    _mitigation_cache,
    _DATASET_CONFIG_SUFFIXES_TO_COPY,
)

# ── risk_debugger (core AI Debugger chain) ──────────────────────────────────
from .risk_debugger import (                    # noqa: F401
    identify_risks,
    identify_mitigations,
    render_suggestion,
    _debounced_risk_analysis,
    _pending_operators,
    _pending_risk_tasks,
    _pending_uids,
    _RISK_DEBOUNCE_SECONDS,
    identify_operator_risks,
    handle_node_removed,
    _revalidate_data_checks,
    handle_pipeline_composed,
    sync_canvas_operators_from_pipeline,
)

# ── risk_pathways (pipeline extraction + pathway evaluation) ────────────────
from .risk_pathways import (                    # noqa: F401
    _extract_pipeline_from_event,
    _extract_operators,
    _debug_pipeline,
    debug_recommended_pipelines,
    evaluate_pathways,
    _invoke_check,
    _CHECKS_NEEDING_SPLITS,
    _CHECKS_NEEDING_TRANSFORM,
    _LLM_CHECKS,
)

# ── risk_checks (data checks + mitigation application) ─────────────────────
from .risk_checks import (                      # noqa: F401
    run_data_checks,
    handle_suggestion_interaction,
    apply_mitigation,
)
