export type { WsPayloadMap, TypedMsgHandler, WsInboundEvent } from "./ws-payloads";

export type UUID = string;

export type AppEventName =
  | "DatasetUploaded"
  | "DatasetImported"
  | "DatasetRemoved"
  | "PipelineImported"
  | "PipelineCreated"
  | "EvaluationProcedureAdded"
  | "EvaluationProcedureSelected"
  | "DataScienceTaskSelected"
  | "DataScienceTaskAdded"
  | "RankingObjectivesChanged"
  | "RankingObjectivesAcceptPipelineDefaults"
  | "RankingObjectiveAdded"
  | "CustomOperatorAdded"
  | "CustomSnippetAdded"
  | "CustomParameterAdded"
  | "PipelineUpdated"
  | "PipelineSaved"
  | "PipelineRemoved"
  | "PipelineRecommendationSelected"
  | "PipelineRecommendationUpvoted"
  | "PipelineRecommendationDownvoted"
  | "PipelinePairwiseVoted"
  | "SessionRenamed"
  | "PipelineVersionRestored"
  | "ExecutePipeline"
  | "PipelineExportClicked"
  | "PipelineShareClicked"
  // Canvas interaction events (node/edge lifecycle on the composition canvas)
  | "PipelineNodeAdded"
  | "PipelineNodeRemoved"
  | "PipelineEdgeAdded"
  | "PipelineEdgeRemoved"
  | "PipelineNodeConfigured"
  | "PipelineComposed"
  | "PipelineCanvasChanged"
  // Feedback
  | "FeedbackReceived"
  // AI Debugger — suggestion interaction
  | "SuggestionInteraction"
  | "DataMitigationDecision"
  | "DataMitigationFinish"
  | "DataMitigationReset"
  // Pipeline extraction from Python scripts (outbound WS events)
  | "ExtractPipeline"
  | "SaveExtractionRules"
  | "LoadExtractionRules"
  // Extraction correction (user submits a corrected pipeline)
  | "ExtractionCorrected"
  // LLM-assisted rule suggestions
  | "SuggestExtractionRules"
  | "CancelSuggestExtractionRules"
  | "AcceptExtractionRule"
  | "RejectExtractionRule"
  | "SaveExtractionRuleSpecs"
  | "CreateMcpToken"
  // Vault — encrypted environment variable lifecycle
  | "VaultEnvVarStored"
  | "VaultEnvVarDeleted"
  // Pipeline cancellation
  | "CancelPipeline"
  // Feedback edit — re-open feedback modal with previous answers
  | "FeedbackEditRequested"
  // Onboarding tour events
  | "OnboardingTooltipFeedback"
  | "OnboardingTourCompleted";

export type BasePayload = Record<string, any>;
export type WsMessage = {
  event: AppEventName;

  payload: BasePayload;
};
