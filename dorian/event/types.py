"""
dorian/event/types.py
----------------------
Canonical event-name enumeration.

``EventType`` is a :class:`~enum.StrEnum` so its members compare equal to
plain strings — existing ``subscribe("InitSession", ...)`` calls keep
working.  New code should prefer the enum to catch typos at import time.

The enum covers every event that has at least one registered handler in
``dorian/event/registry.py``.  Observability-only events (emitted but
never subscribed to) are intentionally excluded; they can use raw strings
until a handler is added.
"""
from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    """Typed catalogue of event names with at least one handler."""

    # ── Session lifecycle ───────────────────────────────────────────────
    InitSession = "InitSession"
    SessionCreated = "SessionCreated"
    KBChanged = "KBChanged"
    WebsocketDisconnected = "WebsocketDisconnected"

    # ── Feedback ────────────────────────────────────────────────────────
    FeedbackReceived = "FeedbackReceived"
    FeedbackEditRequested = "FeedbackEditRequested"

    # ── Pipeline lifecycle ──────────────────────────────────────────────
    PipelineExists = "PipelineExists"
    PipelineImported = "PipelineImported"
    PipelineRetrieved = "PipelineRetrieved"
    PipelineSaved = "PipelineSaved"
    PipelineCreated = "PipelineCreated"
    PipelineUpdated = "PipelineUpdated"
    PipelineRemoved = "PipelineRemoved"
    PipelineComposed = "PipelineComposed"
    ExecutePipeline = "ExecutePipeline"
    CancelPipeline = "CancelPipeline"
    PipelineRunCompleted = "PipelineRunCompleted"
    PipelineRunFailed = "PipelineRunFailed"

    # ── Data lifecycle ──────────────────────────────────────────────────
    DataExists = "DataExists"
    DataWritten = "DataWritten"
    DataProfiled = "DataProfiled"
    # Dataset profiling has been migrated to the exec-worker; the
    # backend submits a dq_check:profile_and_quality job on DataExists
    # and reacts to the completion event below.
    DQCheckProfileAndQualityCompleted = "DQCheckProfileAndQualityCompleted"
    DataProfilingFailed = "DataProfilingFailed"
    DatasetUploaded = "DatasetUploaded"
    DatasetImported = "DatasetImported"
    DatasetRemoved = "DatasetRemoved"
    DataScienceTaskSelected = "DataScienceTaskSelected"
    DataScienceTaskAdded = "DataScienceTaskAdded"
    EvaluationProcedureAdded = "EvaluationProcedureAdded"
    EvaluationProcedureSelected = "EvaluationProcedureSelected"
    # Emitted by engine/backend after the rust handler commits the
    # selectedEvaluationProcedure* fields. Lets python attempt_recommendations
    # rerun strictly after the redis write — see registry.py.
    EvaluationProcedureCommitted = "EvaluationProcedureCommitted"

    # ── Canvas events ───────────────────────────────────────────────────
    PipelineNodeAdded = "PipelineNodeAdded"
    PipelineNodeRemoved = "PipelineNodeRemoved"
    PipelineEdgeAdded = "PipelineEdgeAdded"
    PipelineEdgeRemoved = "PipelineEdgeRemoved"
    PipelineNodeConfigured = "PipelineNodeConfigured"
    # Full canvas DAG snapshot, emitted debounced after structural edits
    # (add/remove/config/edge). Materialises session:meta.pipeline so
    # mitigation / recommendation / execution have a live queryable DAG
    # without requiring an explicit Save click.
    PipelineCanvasChanged = "PipelineCanvasChanged"
    PipelineExportClicked = "PipelineExportClicked"
    PipelineShareClicked = "PipelineShareClicked"
    # Emitted by rust ``risk_scope::handle_node_removed`` after the
    # canvas SET shrinks. Carries ``affected_operators`` (the new,
    # smaller scope) so python's ``handle_canvas_scope_updated`` can
    # run incremental CSV revalidation off it.
    CanvasScopeUpdated = "CanvasScopeUpdated"
    # Emitted by rust when a pipeline lands in a session whose
    # ranking objectives are user-customised (objectiveMode=custom).
    # The SPA renders a reconcile prompt; user resolves via
    # ``RankingObjectivesAcceptPipelineDefaults`` (switch) or by
    # ignoring the prompt (keep custom).
    RankingObjectivesConflictRaised = "RankingObjectivesConflictRaised"
    RankingObjectivesAcceptPipelineDefaults = (
        "RankingObjectivesAcceptPipelineDefaults"
    )

    # ── AI Debugger / Risk analysis ─────────────────────────────────────
    TaskIdentified = "TaskIdentified"
    PotentialRiskIdentified = "PotentialRiskIdentified"
    RiskIdentified = "RiskIdentified"
    MitigationActionsIdentified = "MitigationActionsIdentified"
    SuggestionInteraction = "SuggestionInteraction"
    SuggestionAccepted = "SuggestionAccepted"
    DataMitigationDecision = "DataMitigationDecision"
    DataMitigationReset = "DataMitigationReset"
    DataMitigationFinish = "DataMitigationFinish"

    # ── Recommendations ─────────────────────────────────────────────────
    PipelineRecommendationSelected = "PipelineRecommendationSelected"
    PipelineRecommendationUpvoted = "PipelineRecommendationUpvoted"
    PipelineRecommendationDownvoted = "PipelineRecommendationDownvoted"
    RecommendationsFetched = "RecommendationsFetched"
    RecommendationPipelineSaved = "RecommendationPipelineSaved"

    # ── Ranking objectives ──────────────────────────────────────────────
    RankingObjectivesChanged = "RankingObjectivesChanged"
    RankingObjectiveAdded = "RankingObjectiveAdded"
    # Emitted by engine/backend after the rust handler commits the
    # rankingObjectives + objectiveMode fields. attempt_recommendations
    # subscribes to this rather than RankingObjectivesChanged so the
    # rerun reads the freshly-curated list. Live-drag from the sidebar
    # fires this on every drop.
    RankingObjectivesCommitted = "RankingObjectivesCommitted"

    # ── Custom nodes ────────────────────────────────────────────────────
    CustomOperatorAdded = "CustomOperatorAdded"
    CustomSnippetAdded = "CustomSnippetAdded"
    CustomParameterAdded = "CustomParameterAdded"

    # ── Extraction ──────────────────────────────────────────────────────
    ExtractPipeline = "ExtractPipeline"
    ExtractionCorrected = "ExtractionCorrected"
    SaveExtractionRules = "SaveExtractionRules"
    LoadExtractionRules = "LoadExtractionRules"
    SuggestExtractionRules = "SuggestExtractionRules"
    AcceptExtractionRule = "AcceptExtractionRule"
    RejectExtractionRule = "RejectExtractionRule"

    # ── Vault ───────────────────────────────────────────────────────────
    VaultEnvVarStored = "VaultEnvVarStored"
    VaultEnvVarDeleted = "VaultEnvVarDeleted"

    # ── Onboarding ──────────────────────────────────────────────────────
    OnboardingTooltipFeedback = "OnboardingTooltipFeedback"
    OnboardingTourCompleted = "OnboardingTourCompleted"

    # ── Execution error mitigation ─────────────────────────────────────
    NodeExecutionFailed = "NodeExecutionFailed"

    # ── Experiment Store lifecycle ─────────────────────────────────────
    DatasetUpserted = "DatasetUpserted"
    DatasetPersistedToDocstore = "DatasetPersistedToDocstore"
    PipelineUpserted = "PipelineUpserted"
    EvaluationBatchRecorded = "EvaluationBatchRecorded"

    # ── Workers / Observability ─────────────────────────────────────────
    WorkerMetricsCollected = "WorkerMetricsCollected"

    # ── Progress listeners ──────────────────────────────────────────────
    ComputingMetafeature = "ComputingMetafeature"
    MetafeatureComputed = "MetafeatureComputed"
    MetafeatureError = "MetafeatureError"
    ComputingQualityMetric = "ComputingQualityMetric"
    QualityMetricComputed = "QualityMetricComputed"
    QualityMetricError = "QualityMetricError"
    QualityMetricPendingInput = "QualityMetricPendingInput"

    # ── Slack error notifications ───────────────────────────────────────
    EventHandlerError = "EventHandlerError"
    BackgroundTaskFailed = "BackgroundTaskFailed"
    SessionInitFailed = "SessionInitFailed"
    RecommendationEngineFailed = "RecommendationEngineFailed"
    WebsocketPayloadTooLarge = "WebsocketPayloadTooLarge"
    WebsocketMalformedPayload = "WebsocketMalformedPayload"
    WebsocketOnReceiveError = "WebsocketOnReceiveError"
    WebsocketOnSendError = "WebsocketOnSendError"
    # Evaluation / k-fold
    MetricComputeFailed = "MetricComputeFailed"
    KFoldPipelineBuildFailed = "KFoldPipelineBuildFailed"
    KFoldFailed = "KFoldFailed"
    CustomEvalCompileFailed = "CustomEvalCompileFailed"
    CustomEvalFailed = "CustomEvalFailed"
    # Knowledge base
    KBQueryFailed = "KBQueryFailed"
    # Experiment store
    TrialEnqueueFailed = "TrialEnqueueFailed"
    ExperimentStoreInitFailed = "ExperimentStoreInitFailed"
    # Pipeline generation
    GeneratedPipelineIndexFailed = "GeneratedPipelineIndexFailed"
    GeneratedPipelineSubmitFailed = "GeneratedPipelineSubmitFailed"
    GeneratedPipelineSubmitSkipped = "GeneratedPipelineSubmitSkipped"
    GeneratedPipelineExecutionFailed = "GeneratedPipelineExecutionFailed"
    GeneratedPipelineDeduplicated = "GeneratedPipelineDeduplicated"
    PipelineDedupLookupFailed = "PipelineDedupLookupFailed"
    GenerationBatchFailed = "GenerationBatchFailed"
    SyntheticSessionSeedFailed = "SyntheticSessionSeedFailed"
    # BK-Tree lifecycle
    BKTreeReady = "BKTreeReady"
    BKTreeLoadFailed = "BKTreeLoadFailed"
    BKTreeDrainFailed = "BKTreeDrainFailed"
    # Pipeline lookup
    PostgresPipelineLookupFailed = "PostgresPipelineLookupFailed"
    # API-layer errors
    ExtractionPersistenceFailed = "ExtractionPersistenceFailed"
    LeaderboardQueryFailed = "LeaderboardQueryFailed"
    Neo4jQueryFailed = "Neo4jQueryFailed"
    # Operator resolution
    OperatorResolutionFailed = "OperatorResolutionFailed"
    # MCP tools
    KbCypherQueryFailed = "KbCypherQueryFailed"
    LlmJsonParseFailed = "LlmJsonParseFailed"
    # Metafeatures
    MetafeaturesImportFailed = "MetafeaturesImportFailed"
    # Evaluation procedures
    EvalProcedurePersistFailed = "EvalProcedurePersistFailed"

    # ── Contact form ───────────────────────────────────────────────────
    ContactFormSubmitted = "ContactFormSubmitted"

    # ── System ──────────────────────────────────────────────────────────
    SystemBackupCompleted = "SystemBackupCompleted"
    GracefulShutdownRequested = "GracefulShutdownRequested"
