// lib/ws-events.ts
import useWebSocketStore from "@/store/web-socket"; // adjust path
import { AppEventName, BasePayload, WsMessage } from "@/types";
import { useSessionStore } from "@/store/session";
import { useAgentStore } from "@/store/agent";
import { randomUUID } from "@/helpers/uuid";

const makeRequestId = () => `${Date.now()}-${randomUUID().slice(0, 14)}`;

export function emitEvent(event: AppEventName, payload: BasePayload = {}) {
  const { sendMessage } = useWebSocketStore.getState();

  //get session and user id
  const { userId, activeSessionId } = useSessionStore.getState();

  if (!userId || !activeSessionId) {
    // avoid sending malformed messages
    console.warn("[ws-events] Missing userId/session", {
      userId,
      activeSessionId,
    });
    return;
  }
  const { agentMode } = useAgentStore.getState();

  const msg: WsMessage = {
    event,
    payload: {
      ...payload,
      uid: userId,
      session: activeSessionId,
      ts: Date.now(),
      requestId: makeRequestId(),
      ...(agentMode && { agentDriven: true }),
    },
  };

  sendMessage(msg);
}

/** Convenience wrappers for common events (optional) */
export const ws = {
  datasetUploaded: (info: BasePayload) => emitEvent("DatasetUploaded", info),
  datasetImported: (info: BasePayload) => emitEvent("DatasetImported", info),
  datasetRemoved: (info: BasePayload) => emitEvent("DatasetRemoved", info),
  pipelineImported: (info: BasePayload) => emitEvent("PipelineImported", info),
  pipelineCreated: (info: BasePayload) => emitEvent("PipelineCreated", info),
  evaluationAdded: (evalItem: { uuid?: string; name?: string; code?: string; language?: string; outputs?: unknown[] } & BasePayload) =>
    emitEvent("EvaluationProcedureAdded", evalItem),
  evaluationSelected: (
    evalItem: { id?: string; name?: string } & BasePayload,
  ) => emitEvent("EvaluationProcedureSelected", evalItem),

  dataScienceTaskSelected: (task: BasePayload) =>
    emitEvent("DataScienceTaskSelected", task),
  dataScienceTaskAdded: (task: BasePayload) =>
    emitEvent("DataScienceTaskAdded", task),

  rankingObjectivesChanged: (objectives: BasePayload) =>
    emitEvent("RankingObjectivesChanged", objectives),

  rankingObjectiveAdded: (objective: BasePayload) =>
    emitEvent("RankingObjectiveAdded", objective),
  customOperatorAdded: (operator: BasePayload) =>
    emitEvent("CustomOperatorAdded", operator),
  customSnippetAdded: (snippet: BasePayload) =>
    emitEvent("CustomSnippetAdded", snippet),
  customParameterAdded: (parameter: BasePayload) =>
    emitEvent("CustomParameterAdded", parameter),
  pipelineUpdated: (pipeline: BasePayload) =>
    emitEvent("PipelineUpdated", pipeline),

  pipelineSaved: (info: BasePayload) => emitEvent("PipelineSaved", info),
  pipelineRecommendationSelected: (recommendation: BasePayload) =>
    emitEvent("PipelineRecommendationSelected", recommendation),
  pipelineRecommendationUpvoted: (recommendation: BasePayload) =>
    emitEvent("PipelineRecommendationUpvoted", recommendation),
  pipelineRecommendationDownvoted: (recommendation: BasePayload) =>
    emitEvent("PipelineRecommendationDownvoted", recommendation),
  pipelinePairwiseVoted: (voteInfo: BasePayload) =>
    emitEvent("PipelinePairwiseVoted", voteInfo),
  pipelineRemoved: (info: BasePayload) => emitEvent("PipelineRemoved", info),

  sessionRenamed: (info: BasePayload) => emitEvent("SessionRenamed", info),

  pipelineVersionRestored: (info: BasePayload) =>
    emitEvent("PipelineVersionRestored", info),
  pipelineRunClicked: (info: BasePayload) => emitEvent("ExecutePipeline", info),
  pipelineExportClicked: (info: BasePayload) =>
    emitEvent("PipelineExportClicked", info),
  pipelineShareClicked: (info: BasePayload) =>
    emitEvent("PipelineShareClicked", info),

  // Canvas interaction events
  pipelineNodeAdded: (info: BasePayload) =>
    emitEvent("PipelineNodeAdded", info),
  pipelineNodeRemoved: (info: BasePayload) =>
    emitEvent("PipelineNodeRemoved", info),
  pipelineEdgeAdded: (info: BasePayload) =>
    emitEvent("PipelineEdgeAdded", info),
  pipelineEdgeRemoved: (info: BasePayload) =>
    emitEvent("PipelineEdgeRemoved", info),
  pipelineNodeConfigured: (info: BasePayload) =>
    emitEvent("PipelineNodeConfigured", info),
  pipelineComposed: (info: BasePayload) =>
    emitEvent("PipelineComposed", info),
  // Debounced full-canvas snapshot — materialises session:meta.pipeline
  // on the backend so apply_mitigation / recommendations / replay have
  // a live DAG without requiring an explicit Save click.
  pipelineCanvasChanged: (info: BasePayload) =>
    emitEvent("PipelineCanvasChanged", info),

  // Feedback
  feedbackReceived: (info: BasePayload) =>
    emitEvent("FeedbackReceived", info),

  // AI Debugger — suggestion interaction
  suggestionInteraction: (info: BasePayload) =>
    emitEvent("SuggestionInteraction", info),
  dataMitigationDecision: (info: BasePayload) =>
    emitEvent("DataMitigationDecision", info),
  dataMitigationFinish: (info: BasePayload) =>
    emitEvent("DataMitigationFinish", info),
  dataMitigationReset: (info: BasePayload) =>
    emitEvent("DataMitigationReset", info),

  // Pipeline extraction from Python scripts
  extractPipeline: (info: BasePayload) =>
    emitEvent("ExtractPipeline", info),
  saveExtractionRules: (info: BasePayload) =>
    emitEvent("SaveExtractionRules", info),
  loadExtractionRules: (info: BasePayload) =>
    emitEvent("LoadExtractionRules", info),

  // Extraction correction — user submits a corrected pipeline
  extractionCorrected: (info: BasePayload) =>
    emitEvent("ExtractionCorrected", info),

  // LLM-assisted rule suggestions
  suggestRules: (info: BasePayload) =>
    emitEvent("SuggestExtractionRules", info),
  cancelSuggestRules: (info: BasePayload) =>
    emitEvent("CancelSuggestExtractionRules", info),
  acceptRule: (info: BasePayload) =>
    emitEvent("AcceptExtractionRule", info),
  rejectRule: (info: BasePayload) =>
    emitEvent("RejectExtractionRule", info),
  saveExtractionRuleSpecs: (info: BasePayload) =>
    emitEvent("SaveExtractionRuleSpecs", info),

  // MCP session handshake — issue a short-lived token bound to the
  // current (uid, session). The user pastes the token into their MCP
  // client config; subsequent MCP tool calls authenticate against it.
  createMcpToken: (info: BasePayload = {}) =>
    emitEvent("CreateMcpToken", info),

  // Vault — encrypted environment variable lifecycle
  vaultEnvVarStored: (info: BasePayload) =>
    emitEvent("VaultEnvVarStored", info),
  vaultEnvVarDeleted: (info: BasePayload) =>
    emitEvent("VaultEnvVarDeleted", info),

  // Pipeline cancellation
  pipelineCancelClicked: (info: BasePayload) =>
    emitEvent("CancelPipeline", info),

  // Feedback edit — request backend to re-send queries with current answers
  feedbackEditRequested: (info: BasePayload = {}) =>
    emitEvent("FeedbackEditRequested", info),

  // Onboarding tour
  onboardingTooltipFeedback: (info: BasePayload) =>
    emitEvent("OnboardingTooltipFeedback", info),
  onboardingTourCompleted: (info: BasePayload = {}) =>
    emitEvent("OnboardingTourCompleted", info),
};
