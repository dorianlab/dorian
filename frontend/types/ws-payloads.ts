// types/ws-payloads.ts — Typed payload interfaces for inbound WebSocket events.
//
// This module provides compile-time type safety for the most critical server-sent
// WS event payloads.  Handlers in `usePipelineSocket.ts` can reference these via
// the `WsPayloadMap` discriminated map so that field access is checked at build time.
//
// Incremental: events not yet listed here fall back to `any` via the
// `WsInboundEvent` helper type.

import type { Pipeline } from "./pipeline";

// ── Pipeline lifecycle ────────────────────────────────────────────────────────

export interface PipelineSavedPayload {
  pipeline: Pipeline;
}

export interface PipelineRewrittenPayload {
  pipeline: Pipeline | string;
  summary?: string;
}

// ── Pipeline execution ────────────────────────────────────────────────────────

export interface PipelineRunInitialisedPayload {
  run_id: string;
}

export interface PipelineRunStartedPayload {
  run_id: string;
}

export interface PipelineRunCompletedPayload {
  run_id: string;
  metrics?: Record<string, unknown> | string;
}

export interface PipelineRunFailedPayload {
  run_id: string;
  error?: string;
  status?: string;
  stage?: string;
}

export interface PipelineRunCancelledPayload {
  run_id: string;
}

export interface PipelineRunErrorPayload {
  reason?: string;
}

// ── Node-level execution ──────────────────────────────────────────────────────

export interface PipelineNodeStartedPayload {
  run_id: string;
  node_id: string;
  start_time?: string;
}

export interface PipelineNodeCompletedPayload {
  run_id: string;
  node_id: string;
  duration?: string;
  output?: string;
}

export interface PipelineNodeFailedPayload {
  run_id: string;
  node_id: string;
  error?: string;
  trace?: string;
  duration?: string;
}

export interface PipelineNodeSkippedPayload {
  run_id: string;
  node_id: string;
}

export interface PipelineNodeCancelledPayload {
  run_id: string;
  node_id: string;
}

export interface PipelineNodeTraceOutputPayload {
  run_id: string;
  node_id: string;
  output?: string;
}

// ── State hydration ───────────────────────────────────────────────────────────

export interface StateDatasetPayload {
  value: string | {
    did?: string;
    fpath?: string;
    profile?: unknown;
    quality?: unknown;
    quality_checks?: unknown;
    quality_inputs?: unknown;
    mitigation_session?: unknown;
    target?: unknown;
    columns?: string[];
    features?: string[];
  };
}

export interface StatePipelinePayload {
  value: string | { pipelines?: unknown[] };
}

export interface StateLastRunPayload {
  value: string | {
    run_id?: string;
    status?: string;
    metrics?: Record<string, unknown>;
  };
}

// ── AI Debugger suggestions ───────────────────────────────────────────────────

export interface SuggestionPayload {
  task: string;
  principles?: string | unknown[];
  checks?: string | unknown[];
  alternatives?: string | unknown[];
  has_rewrite?: string | boolean;
  [key: string]: unknown;
}

export interface SuggestionsRevokePayload {
  operator?: string;
}

export interface SuggestionsResetPayload {
  [key: string]: unknown;
}

// ── Check report (AI Debugger) ────────────────────────────────────────────────

export interface CheckReportPayload {
  pipeline_label?: string;
  total?: string;
  passed?: string;
  failed?: string;
  skipped?: string;
  results?: string | unknown[];
}

export interface CheckEventPayload {
  check?: string;
  risk?: string;
  pipeline_label?: string;
  message?: string;
}

// ── Progress (metafeature profiling) ──────────────────────────────────────────

export interface ProgressPayload {
  session?: string;
  uid?: string;
  did?: string;
  metafeature?: string;
  metric?: string;
  value?: unknown;
  status?: string;
}

// ── Notifications ─────────────────────────────────────────────────────────────

export interface NotificationPayload {
  id: string;
  kind: string;
  title: string;
  message: string;
  createdAt?: string;
}

// ── Queue status ─────────────────────────────────────────────────────────────

export interface QueueStatusPayload {
  value: string | {
    queued: boolean;
    position: number;
    ahead: number;
    estimated_wait_s: number;
    queue_depth: number;
    per_task_estimate_s?: number;
  };
}

export interface QueueConcurrencyLimitPayload {
  value: string | {
    current: number;
    max: number;
    tier: string;
    tier_label: string;
  };
}

// ── Batch notifications (reconnect replay) ───────────────────────────────────

export interface NotificationBatchPayload {
  value: string | Array<{
    id: string;
    kind: string;
    title: string;
    message?: string;
    createdAt?: string;
    meta?: Record<string, unknown>;
  }>;
}

// ── Rate limiting ─────────────────────────────────────────────────────────────

export interface RateLimitedPayload {
  eventName?: string;
  retryAfter?: string | number;
  limit?: string | number;
}

// ═══════════════════════════════════════════════════════════════════════════════
// WsPayloadMap — maps inbound event name strings to their payload types.
// ═══════════════════════════════════════════════════════════════════════════════

export interface WsPayloadMap {
  // Pipeline lifecycle
  "pipeline/rewritten": PipelineRewrittenPayload;

  // Pipeline execution
  "pipeline/run/initialised": PipelineRunInitialisedPayload;
  "pipeline/run/started": PipelineRunStartedPayload;
  "pipeline/run/completed": PipelineRunCompletedPayload;
  "pipeline/run/failed": PipelineRunFailedPayload;
  "pipeline/run/cancelled": PipelineRunCancelledPayload;
  "pipeline/run/error": PipelineRunErrorPayload;

  // Node-level execution
  "pipeline/node/started": PipelineNodeStartedPayload;
  "pipeline/node/completed": PipelineNodeCompletedPayload;
  "pipeline/node/failed": PipelineNodeFailedPayload;
  "pipeline/node/skipped": PipelineNodeSkippedPayload;
  "pipeline/node/cancelled": PipelineNodeCancelledPayload;
  "pipeline/node/trace-output": PipelineNodeTraceOutputPayload;

  // State hydration
  "state/dataset": StateDatasetPayload;
  "state/pipeline": StatePipelinePayload;
  "state/lastRun": StateLastRunPayload;

  // AI Debugger
  suggestion: SuggestionPayload;
  "suggestions/revoke": SuggestionsRevokePayload;
  "suggestions/reset": SuggestionsResetPayload;

  // Check events
  "check/started": CheckEventPayload;
  "check/passed": CheckEventPayload;
  "check/failed": CheckEventPayload;
  "check/report": CheckReportPayload;

  // Progress
  progress: ProgressPayload;

  // Notifications
  notification: NotificationPayload;
  "notifications/batch": NotificationBatchPayload;

  // Queue status
  "queue/status": QueueStatusPayload;
  "queue/concurrency-limit": QueueConcurrencyLimitPayload;

  // Rate limiting
  "error/rate-limited": RateLimitedPayload;
}

/** Typed handler for a specific event in the payload map. */
export type TypedMsgHandler<K extends keyof WsPayloadMap> = (
  resp: WsPayloadMap[K],
) => void;

/**
 * Helper: resolves to the typed payload if the event is in WsPayloadMap,
 * otherwise falls back to `any`.  Useful for the mixed eventHandlers record.
 */
export type WsInboundEvent<E extends string> = E extends keyof WsPayloadMap
  ? WsPayloadMap[E]
  : any;
