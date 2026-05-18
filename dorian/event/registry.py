from backend.events import subscribe
from backend.queue import submit_for_execution
from dorian.event.types import EventType as E

from .handlers.lifecycle import handle_feedback, handle_feedback_edit_requested, persist_interaction_event
from .handlers.workers import record_worker_metrics
from .handlers.pipeline_events import (
    read_pipeline,
    start_debugging,
    check_data,
    handle_pipeline_saved,
    handle_profile_and_quality_completed,
    handle_operator_dropped,
)
from .handlers.risk_events import (
    identify_risks, identify_mitigations, render_suggestion,
    identify_operator_risks, run_data_checks, debug_recommended_pipelines,
    handle_suggestion_interaction, apply_mitigation,
    handle_node_removed, handle_pipeline_composed,
    sync_canvas_operators_from_pipeline,
    evaluate_pathways,
)
from .handlers.risk_debugger import handle_canvas_scope_updated
# notify ported to engine/backend/src/handlers/notify.rs.
from .handlers.session import seed_session, handle_kb_changed
# handle_websocket_disconnected ported to engine/backend handlers/session.rs.
from .handlers.recommendations import (
    handle_recommendation_selected,
    handle_recommendation_upvoted,
    handle_recommendation_downvoted,
    attempt_recommendations,
    handle_pipeline_objectives_switch,
)
# handle_auto_task_selection ported to engine/backend/src/handlers/auto_task.rs
# (csv-based target inspection, no pandas, no GIL).
# handle_data_science_task_selected ported to engine/backend/src/handlers/data_science_task.rs.
# EvaluationProcedureAdded ported to engine/backend/src/handlers/evaluation_procedure.rs;
# the user-code compile/exec slice runs as ``eval_procedure:validate`` in the
# python exec worker (see dorian/exec/eval_procedure.py).
# EvaluationProcedureSelected ported to engine/backend/src/handlers/session_meta.rs.
# Pipeline / dataset handlers all ported. PipelineCreated / PipelineUpdated
# had no-op python pass-throughs and were dropped without a rust port.
# PipelineSaved / PipelineRemoved / PipelineCanvasChanged → handlers/pipeline.rs.
# DatasetUploaded / DatasetImported logging → handlers/interactions.rs.
# DatasetRemoved cleanup → handlers/datasets.rs.
# custom-node handlers ported to engine/backend/src/handlers/custom_nodes.rs
# (single parameterised handler over the three event types). Python imports
# stay deleted so a stale duplicate-handler can't sneak back in.
from .handlers.experiment import (
    handle_dataset_profiled,
    handle_pipeline_saved_to_store,
    handle_run_completed,
    handle_recommendation_interaction_to_store,
    handle_dataset_upserted_trials,
    handle_pipeline_upserted_trials,
)
# RankingObjectivesChanged ported to engine/backend/src/handlers/session_meta.rs.
# RankingObjectiveAdded ported to engine/backend/src/handlers/ranking_objective.rs;
# the user-code compile/exec slice runs as ``objective:validate`` in the
# python exec worker (see dorian/exec/objective.py).
from .handlers.encoding import handle_encoding_on_metafeature_error
# All onboarding handlers (TourCompleted, TooltipFeedback, InitSession
# replay) now ported to engine/backend/src/handlers/onboarding.rs.
from .handlers.notifications import (
    slack_on_error,
    slack_on_feedback,
    slack_on_session_created,
    slack_on_session_init,
    slack_on_backup,
    slack_on_tooltip_feedback,
    slack_on_contact_form,
)
from .handlers.extraction import (
    # ``handle_extract_pipeline`` is retired — extraction is rust-native
    # via ``engine/backend/src/handlers/extraction.rs``. The remaining
    # handlers below stay python until each ports (LLM-driven
    # rule-suggestion path, save/load/accept rules, mcp token).
    handle_extraction_corrected,
    handle_save_rules,
    handle_load_rules,
    handle_save_rule_specs,
    handle_suggest_rules,
    handle_cancel_suggest_rules,
    handle_create_mcp_token,
    handle_accept_rule,
    handle_reject_rule,
)
# dataset_live broadcasts ported to engine/backend/src/handlers/dataset_live.rs.
# cancel handler ported to engine/backend/src/handlers/cancel.rs.
from .handlers.execution_error_handler import handle_node_execution_failed
from .handlers.rl_error_mitigation import (
    handle_rl_pipeline_run_failed,
    handle_rl_mitigation_child_completed,
)
from .helpers.lifecycle import with_envelope

_registered = False


def register_event_handlers():
    global _registered
    if _registered:
        return
    _registered = True
    # WebSocket message handlers (dispatched by websocket.py)
    # InitSession Phase 3 (tooltips + recommendations stub +
    # _verify_dataset_profiling): paused. Phase 1+2 (operators / tasks
    # / objectives / evals / dataset / pipeline state replay) is
    # already in rust ``session_seed``. Phase 3's ui/tooltips xadd
    # depends on the python tooltip catalog (``dorian/ui/tooltips.py``);
    # rust port of that needs the catalog moved into the KB or its own
    # JSON file. Until then tooltips don't replay on reconnect.
    # KBChanged → handle_kb_changed: catalog broadcast already in rust
    # (engine/backend/src/handlers/kb_changed.rs reloads state.kb +
    # pushes fresh catalogs to active connections). Python in-process
    # LRU cache invalidation is pointless once every KB-consuming
    # handler has ported; the few remaining python KB consumers
    # auto-refresh on next call (load_kb re-reads the snapshot).
    # WebsocketDisconnected owned by engine/backend handlers/session.rs.
    # FeedbackReceived + FeedbackEditRequested moved to rust
    # (engine/backend/src/handlers/feedback.rs). Pure I/O, no python
    # compute on the hot path.

    # PipelineExists / PipelineImported → handled by rust
    # (engine/backend/src/handlers/pipeline.rs::handle_read_pipeline).
    # Rust does the file read + meta update + state/pipeline xadd +
    # PipelineRetrieved emit; python ``start_debugging`` stays
    # subscribed to PipelineRetrieved as a downstream DAG-rewrite job.
    # PipelineRetrieved → start_debugging: dropped. The handler ran a
    # transform-rule that emitted OperatorFound + annotated tasks via
    # populate_tasks, but no other subscriber consumes OperatorFound
    # and the transform output was never persisted. The AI Debugger
    # chain is now triggered through the rust-emitted TaskIdentified
    # in risk_scope::sync_canvas_operators (PipelineRetrieved).
    # DQ check dispatch (DataExists, DataWritten →
    # ``check_data``; DQCheckProfileAndQualityCompleted → completion
    # handler) — paused. Both paths run python-side compute (CSV reads
    # + sklearn-style checks via ``dorian/toolbox``) — those are
    # "submitted compute" by the migration policy. Rust subscribers
    # need to push job records onto a python-worker queue; the queue
    # infra isn't wired yet. Until then the dq_check pipeline stops
    # running on every dataset write. The exec-worker still runs
    # individual ``dq_check:*`` jobs when they're already queued.
    # PipelineSaved was supposed to be owned by handlers/pipeline.rs,
    # but the engine/backend rust binary isn't deployed yet (no
    # Dockerfile, not in compose) — PipelineCanvasChanged stays in
    # rust-future-land, but PipelineSaved is the gating step before
    # ExecutePipeline (writes pipelineHistory + .pipeline to session
    # meta), so without a live handler every Run on a freshly-composed
    # canvas pipeline failed with ``pipeline_not_found``. Re-add the
    # python handler as the operational backstop. Drop this when the
    # rust subscriber is wired into compose.
    # PipelineSaved → handle_pipeline_saved already owned by rust
    # (engine/backend/src/handlers/pipeline.rs::handle_saved). The
    # python copy was a stand-in until the rust event-bus subscriber
    # deployed; that landed, so this duplicate is retired.
    # ExecutePipeline → ZADD priority queue is now in rust
    # (engine/backend/src/handlers/execute_pipeline.rs). The runner
    # that POPs the queue stays python (operator runtime) but no
    # longer subscribes to the event bus.
    # CancelPipeline is owned by engine/backend handlers/cancel.rs.

    # Recommendation events — explicit user interactions and
    # readiness-driven re-ranks (× 6 events). The redis-I/O slice
    # (interaction log, selected→meta save, RecommendationPipelineSaved
    # emit, ranking-objective default switch) is already in rust
    # (engine/backend/src/handlers/recommendation.rs). The remaining
    # ``suggest_with_status`` re-rank stays python until the
    # recommendation engine itself ports — once that lands, the
    # subscriptions below disappear.
    # Recommendation re-rank subscribers (× 7: Selected/Upvoted/
    # Downvoted, DataExists, DataProfiled, DataScienceTaskSelected,
    # EvaluationProcedureCommitted): paused. The redis-I/O slice is
    # already rust (record_interaction, selected→meta save,
    # RecommendationPipelineSaved emit). The remaining heavy compute
    # is ``suggest_with_status`` (KDTree similarity + objective
    # scoring + candidate fetch) — the rust ExperimentStore already
    # has the data + scoring (engine/optimizer/src/recommendation),
    # just need a rust orchestrator that ties it to ``state/pipelines/recommendation``.
    # Until that lands, recommendations don't refresh on context
    # changes; the SPA shows whatever was last sent.

    # Data science task & evaluation handlers (persist selections to session meta)
    # DataScienceTaskSelected meta-write + state/operators emit owned by
    # engine/backend handlers/data_science_task.rs.
    # DataScienceTaskAdded had a no-op python handler; dropped without
    # a rust port — re-emitting would loop, and persistence is not needed.
    # Auto-detect Classification/Regression from the dataset profile —
    # owned by engine/backend handlers/auto_task.rs (csv-based, GIL-free).
    # EvaluationProcedureAdded — owned by engine/backend handlers/evaluation_procedure.rs
    # (state writes) + dorian/exec/eval_procedure.py (decoupled compile/exec).
    # EvaluationProcedureSelected owned by engine/backend handlers/session_meta.rs.

    # Risk events — Debugger chain
    # Potential risks (KB-only, from canvas node addition):
    # TaskIdentified → identify_risks ported to rust
    # (engine/backend/src/handlers/risk_chain.rs). Rust queries the KB
    # snapshot directly (operator_risks) and emits
    # PotentialRiskIdentified.
    # PotentialRiskIdentified / RiskIdentified → identify_mitigations
    # ported to rust (engine/backend/src/handlers/risk_chain.rs). Same
    # for MitigationActionsIdentified → render_suggestion. The KB
    # description templates, principles_for_risk, checks_for_risk, and
    # rewrite-rule slug set are now pre-built lookups on KbSnapshot
    # (or cached on AppState for the postgres-sourced rewrite slugs).
    # PipelineNodeAdded → identify_operator_risks (debounced
    # risk-analysis trigger) ported to rust
    # (engine/backend/src/handlers/risk_chain.rs::handle_operator_dropped_debounce).
    # The DAG-construction handler (handle_operator_dropped → builds
    # compound sub-DAGs via the python ``group_builder`` for compound
    # operators) stays python until the group-builder ports.
    # PipelineNodeAdded → handle_operator_dropped: paused. The
    # python handler builds compound sub-DAGs via ``group_builder``
    # for compound operators (sklearn pipelines, guardrails, LLMs)
    # and emits ``state/group-created``. Without it, dropping a
    # compound operator on the canvas leaves it as a single node
    # rather than expanding into its method sequence. The rust port
    # needs the group builder logic which mirrors the KB
    # ``calls`` / ``has method`` predicates already exposed via
    # KbSnapshot.
    # subscribe(E.PipelineNodeAdded, handle_operator_dropped)
    # PipelineNodeRemoved → handle_node_removed: dropped. The python
    # debounce-cancel slice was the only reason this stayed subscribed
    # after the rust port; the rust risk_chain debounce now owns its
    # own state, so the python in-process dict no longer matters. Worst
    # case: a stale debounce timer in python fires on a removed node;
    # rust risk_scope's SREM has already flipped the canvas SET so
    # downstream identify_risks no-ops on the missing operator.
    # CanvasScopeUpdated → handle_canvas_scope_updated (CSV-backed
    # revalidation): paused. Same dq_check job-submit dependency as
    # check_data above. CSV revalidation on canvas-scope-shrink
    # doesn't re-run automatically until the job-submit infra lands.
    # PipelineComposed + canvas-operator-SET sync moved to rust
    # (engine/backend/src/handlers/risk_scope.rs). Rust does the SET
    # mutation + suggestions/reset; for sync_canvas it re-emits
    # ``TaskIdentified`` per operator so identify_risks (still python)
    # runs against the new scope.
    # handle_pipeline_objectives_switch (RecommendationPipelineSaved +
    # PipelineImported) ported to rust
    # (engine/backend/src/handlers/recommendation.rs).
    # DataProfiled → run_data_checks / evaluate_pathways and
    # RecommendationsFetched → debug_recommended_pipelines: paused.
    # All three orchestrate CSV-backed compute (chi-squared, NaN
    # counts, distribution shifts) which is "submitted compute" by the
    # migration policy. Need rust subscribers + python-worker queue.
    # Until that lands, profiling-time data checks and
    # recommendation-debug enrichment don't run on event arrival; the
    # exec-worker still services explicitly-queued ``dq_check:*`` jobs.
    # SuggestionInteraction + DataMitigationDecision → handle_suggestion_interaction
    # ported to rust (engine/backend/src/handlers/risk_chain.rs). Rust
    # persists the interaction log and emits SuggestionAccepted /
    # SuggestionRejected; the downstream apply_mitigation (DAG rewrite)
    # subscribers below stay python until that engine ports.
    # apply_mitigation (SuggestionAccepted, DataMitigationReset,
    # DataMitigationFinish): paused. The handler runs DAG rewrites
    # against the python rule engine + ``dorian/pipeline/transforms``
    # apply path. The rust side has the rewrite primitives in
    # ``engine/graph/src/rewrite.rs`` (RewriteRule + sync_apply); a
    # focused port wires SuggestionAccepted → rust rewrite directly,
    # but until that lands the user's "Apply" click on a suggestion
    # card no longer rewrites the canvas. The suggestion card itself
    # still appears (rust risk_chain owns suggestion render).

    # NodeExecutionFailed → handle_node_execution_failed: paused. The
    # python handler inspects an execution error and proposes parameter
    # fixes via a KB lookup + DAG rewrite. Same DAG-rewrite dependency
    # as apply_mitigation. The frontend still receives the run-failed
    # event; only the auto-suggested fix-parameter card stops appearing.

    # RL auto-mitigation chain (PipelineRunFailed → handle_rl_*; also
    # PipelineRunCompleted/Failed → handle_rl_mitigation_child_completed):
    # paused. RL trainer integration writes to its own postgres
    # tables and triggers child trial runs — heavy, RL-specific. Stays
    # off the event-bus until the RL ports land. RL training itself
    # still runs (the trainer subscribes to its own queue).

    # Canvas interaction events + extraction-correction + vault +
    # dataset uploads/imports — all persisted to the per-session
    # interaction log by ``engine/backend/src/handlers/interactions.rs``.
    # Python no longer subscribes to these for the persist path;
    # domain handlers below remain for the side-effects beyond
    # logging (e.g. dataset_removed clears redis keys).

    # Extraction handlers (× 9: ExtractPipeline, SaveExtractionRules,
    # SaveExtractionRuleSpecs, LoadExtractionRules,
    # SuggestExtractionRules, CancelSuggestExtractionRules,
    # AcceptExtractionRule, RejectExtractionRule, ExtractionCorrected):
    # paused. The extractor runs LLM calls against an external API —
    # "submitted compute" by the migration policy. The rust port should
    # subscribe + push extract-job records onto a python worker queue;
    # the queue infra isn't wired yet. Until then the SPA's
    # "extract from python file" flow stalls at the upload step.
    # CreateMcpToken is paused alongside (token issuance is small but
    # part of the same extraction module).

    # Vault env-var lifecycle — persisted by the rust ``interactions``
    # handler. Python no longer subscribes; domain logic for
    # encrypted store CRUD lives in the API layer, not here.

    # Dataset events — persist uploads / imports / removals.
    # All three are owned by engine/backend handlers (interactions.rs
    # for Uploaded/Imported logging, datasets.rs for Removed cleanup).
    # The pre-port python ``handle_dataset_uploaded`` / ``_imported``
    # were no-op pass-throughs; ``_removed`` is fully ported.

    # Pipeline lifecycle events
    # PipelineCreated / PipelineUpdated had no-op python handlers
    # (re-emitting would have looped); dropped without a rust port
    # because nothing acts on the inbound event today.
    # PipelineRemoved owned by engine/backend handlers/pipeline.rs.

    # Custom node events — owned by engine/backend handlers/custom_nodes.rs
    # (one parameterised handler over Operator/Snippet/Parameter).

    # Ranking objective handlers (persist user selections + custom objectives)
    # RankingObjectivesChanged owned by engine/backend handlers/session_meta.rs.
    # RankingObjectiveAdded owned by engine/backend handlers/ranking_objective.rs
    # (state writes) + dorian/exec/objective.py (decoupled compile/exec).

    # Re-rank when objectives change. Same race-fix as
    # EvaluationProcedureCommitted above: rust writes meta first then
    # emits RankingObjectivesCommitted; we run the rerun off that
    # so the user-curated list/order is on disk before scoring.
    # Live-drag from the sidebar fires this on every drop — ordering
    # has to land before re-rank or the new top entry won't reflect
    # what the user sees.
    # RankingObjectivesCommitted / RankingObjectiveAdded →
    # attempt_recommendations: paused alongside the other
    # recommendation subscribers. See above.

    # Experiment Store persistence — keep indices and relational data up to date.
    # These handlers never block the critical path; failures are logged and swallowed.
    # Experiment-store write handlers (DataProfiled, PipelineSaved,
    # PipelineRunCompleted/Failed, PipelineRecommendation*Interaction)
    # ported to rust (engine/backend/src/handlers/experiment_store.rs).
    # Pure postgres I/O; no python compute on the hot path.

    # Cross-product trial scheduling — deterministic cold-start coverage
    # Cross-product trial scheduling (DatasetUpserted +
    # PipelineUpserted) — paused. The trial submission path is python
    # (``backend.queue.submit_background``); rust orchestration of it
    # is a separate slice. Until that lands, new datasets / pipelines
    # don't auto-evaluate against the cross product. Run
    # ``scripts/cross_product_eval.py`` manually to backfill.

    # ── Live dataset broadcast (Go WS global channel) ──────────────────
    # The four broadcast handlers (DatasetUpserted, DatasetPersistedToDocstore,
    # EvaluationBatchRecorded, DatasetRemoved) are owned by
    # engine/backend handlers/dataset_live.rs. Python no longer subscribes.

    # Listeners
    # progress_update is owned by engine/backend handlers/listeners.rs.
    # Python no longer subscribes to the seven metafeature/quality
    # events for the WS-stream fan-out — the rust consumer-group
    # picks them up. handle_encoding_on_metafeature_error stays
    # python-side (KB-driven encoder injection) until the
    # encoding-mitigation chain is also ported.
    # MetafeatureError → handle_encoding_on_metafeature_error: paused.
    # The handler auto-injects an OrdinalEncoder upstream of the
    # offending node when a categorical-feature error surfaces during
    # profiling. DAG rewrite — same dependency on the rust rewrite
    # engine port as apply_mitigation.

    # In-app notifications — persistent bell center.
    # Owned by engine/backend handlers/notify.rs (4 event types).

    # Worker supervisor — feed host metrics into observability collector
    # WorkerMetricsCollected → in-process observability collector.
    # Disabled with the python event-bus; the
    # ``/observability/workers`` endpoint will return an empty list
    # until that subsystem moves to a redis-backed sink readable by
    # rust.

    # ── Slack notifications ──────────────────────────────────────────────
    # All slack-webhook fan-out moved to
    # ``engine/backend/src/handlers/slack.rs`` on 2026-04-30. The rust
    # handler subscribes to every error event in
    # ``slack::SLACK_ERROR_EVENTS`` plus FeedbackReceived /
    # SessionCreated / InitSession / SystemBackupCompleted /
    # ContactFormSubmitted / OnboardingTooltipFeedback. Same
    # engine-session filter (PipelineRunFailed from automl/rl/xproduct
    # is suppressed) and same redis-backed dedup window. Python's
    # ``slack_on_error`` and friends stay imported for unit tests but
    # are no longer subscribed — every event lands in the rust
    # handler instead, off the python event bus.
    #
    # Adding a new error-event type that should page slack: add the
    # name to ``SLACK_ERROR_EVENTS`` in ``slack.rs``, not here.
