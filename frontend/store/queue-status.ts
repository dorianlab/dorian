import { create } from "zustand";

export interface QueueState {
  /** Whether this session's pipeline is currently queued. */
  queued: boolean;
  /** 1-based position in the queue (0 = not queued). */
  position: number;
  /** Number of tasks ahead of this one. */
  ahead: number;
  /** Estimated wait in seconds (based on observability data). */
  estimatedWaitS: number;
  /** Total tasks in the queue. */
  queueDepth: number;
  /** Estimated duration per task in seconds. */
  perTaskEstimateS: number;
  /** Timestamp of the last status update. */
  lastUpdated: number;

  /** Whether this user hit their concurrency limit. */
  concurrencyLimited: boolean;
  /** Current concurrent runs for this user. */
  concurrentCurrent: number;
  /** Max concurrent runs allowed by tier. */
  concurrentMax: number;
  /** User's tier name. */
  tier: string;
  /** User's tier display label. */
  tierLabel: string;
}

export interface QueueActions {
  /** Update queue position from a WS event. */
  setQueueStatus: (status: {
    queued: boolean;
    position: number;
    ahead: number;
    estimated_wait_s: number;
    queue_depth: number;
    per_task_estimate_s?: number;
  }) => void;

  /** Update concurrency limit from a WS event. */
  setConcurrencyLimit: (limit: {
    current: number;
    max: number;
    tier: string;
    tier_label: string;
  }) => void;

  /** Clear queue status (pipeline started running). */
  clearQueue: () => void;
}

export const useQueueStatusStore = create<QueueState & QueueActions>(
  (set) => ({
    // State
    queued: false,
    position: 0,
    ahead: 0,
    estimatedWaitS: 0,
    queueDepth: 0,
    perTaskEstimateS: 30,
    lastUpdated: 0,
    concurrencyLimited: false,
    concurrentCurrent: 0,
    concurrentMax: 2,
    tier: "free",
    tierLabel: "Free",

    // Actions
    setQueueStatus: (status) =>
      set({
        queued: status.queued,
        position: status.position,
        ahead: status.ahead,
        estimatedWaitS: status.estimated_wait_s,
        queueDepth: status.queue_depth,
        perTaskEstimateS: status.per_task_estimate_s ?? 30,
        lastUpdated: Date.now(),
      }),

    setConcurrencyLimit: (limit) =>
      set({
        concurrencyLimited: true,
        concurrentCurrent: limit.current,
        concurrentMax: limit.max,
        tier: limit.tier,
        tierLabel: limit.tier_label,
      }),

    clearQueue: () =>
      set({
        queued: false,
        position: 0,
        ahead: 0,
        estimatedWaitS: 0,
        queueDepth: 0,
        lastUpdated: Date.now(),
        concurrencyLimited: false,
      }),
  }),
);
