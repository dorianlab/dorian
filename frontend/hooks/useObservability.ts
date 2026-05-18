"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useObservabilityStore } from "@/store";
import { apiClient } from "@/lib/api-client";

type FetchState<T> = {
  data: T | null;
  loading: boolean;
  error: string | null;
};

/**
 * Generic polling hook for an observability endpoint.
 *
 * @param path   e.g. "/observability/handlers"
 * @param interval  polling interval in ms (default 10 000)
 * @param extra  extra query params beyond `since` and `uid`
 */
export function useObservabilityPoll<T>(
  path: string,
  interval = 10_000,
  extra: Record<string, string | number> = {},
): FetchState<T> {
  const { scope, since } = useObservabilityStore();
  const [state, setState] = useState<FetchState<T>>({
    data: null,
    loading: true,
    error: null,
  });
  const mountedRef = useRef(true);

  const fetchData = useCallback(async () => {
    try {
      const params: Record<string, string | number> = { since };
      if (scope !== "global") params.uid = scope;
      for (const [k, v] of Object.entries(extra)) {
        params[k] = v;
      }
      const res = await apiClient.get<T>(path, { params });
      if (mountedRef.current) {
        setState({ data: res.data, loading: false, error: null });
      }
    } catch (err: any) {
      if (mountedRef.current) {
        setState((s) => ({ ...s, loading: false, error: err.message }));
      }
    }
  }, [path, scope, since, JSON.stringify(extra)]);

  useEffect(() => {
    mountedRef.current = true;
    fetchData();
    const id = setInterval(fetchData, interval);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [fetchData, interval]);

  return state;
}

// ---- Typed convenience hooks ------------------------------------------------

export interface HandlerStat {
  fn_name: string;
  event_type: string;
  count: number;
  total_wall_s: number;
  avg_wall_s: number;
  max_wall_s: number;
  error_count: number;
  error_rate: number;
  last_errors: string[];
}

export interface PipelineStat {
  run_id: string;
  uid: string;
  session: string;
  status: string;
  start_ts: number;
  end_ts: number | null;
  duration_s: number | null;
  node_count: number;
  error: string | null;
  /** Failure stage: parse | validation | expansion | graph_build | execution */
  stage: string | null;
  /** Full traceback for failures */
  trace: string | null;
  /** "user" or "rl" */
  source: string | null;
  /** Pipeline UUID */
  pipeline_id: string | null;
  /** Comma-separated operator FQNs */
  node_types: string | null;
  /** Node ID of first failure */
  failed_node: string | null;
}

export interface ThroughputBucket {
  ts: number;
  total: number;
  [eventType: string]: number;
}

export interface ErrorEntry {
  fn_name: string;
  error_count: number;
  total_count: number;
  error_rate: number;
  event_types: string[];
  last_errors: string[];
}

export interface SystemSnapshot {
  cpu_percent: number;
  rss_mb: number;
  event_bus: {
    pool_size: number;
    active_workers: number;
    // Legacy single-queue fields — mapped to the user lane post-bulletproofing.
    queue_depth: number;
    queue_capacity: number;
    // Two-lane stats introduced by the event-bus bulletproofing.
    user_queue?: { size: number; capacity: number };
    bg_queue?: { size: number; capacity: number };
    drops_by_reason?: Record<string, number>;
    enqueues_by_lane?: Record<string, number>;
  };
  rl?: {
    inflight: number;
    limit: number;
  };
  dask: {
    workers: number;
    inflight: number;
  };
  disk?: Array<{
    path: string;
    total_gb: number;
    used_gb: number;
    free_gb: number;
    used_pct: number;
    error?: string;
  }>;
  ts: number;
}

export type EventMap = Record<string, string[]>;

export const useHandlerStats = () =>
  useObservabilityPoll<HandlerStat[]>("/observability/handlers", 10_000);

export const usePipelineStats = () =>
  useObservabilityPoll<PipelineStat[]>("/observability/pipelines", 10_000);

export const useThroughput = () =>
  useObservabilityPoll<ThroughputBucket[]>("/observability/throughput", 10_000, {
    bucket: 10,
  });

export const useErrorSummary = () =>
  useObservabilityPoll<ErrorEntry[]>("/observability/errors", 10_000);

export const useSystemSnapshot = () =>
  useObservabilityPoll<SystemSnapshot>("/observability/system", 5_000);

export const useEventMap = () =>
  useObservabilityPoll<EventMap>("/observability/event-map", 600_000); // fetch once effectively
