"use client";

import { useEffect, useRef, useCallback } from "react";
import { decode } from "@msgpack/msgpack";
import config from "@/env.config";
import { useSessionStore } from "@/store/session";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface DatasetLiveEvent {
  kind: "dataset_updated" | "dataset_removed" | "evaluation_recorded";
  data: Record<string, unknown>;
}

type Listener = (event: DatasetLiveEvent) => void;

// ---------------------------------------------------------------------------
// Reconnect parameters (same strategy as usePipelineSocket)
// ---------------------------------------------------------------------------
const BASE_DELAY = 1000;
const MAX_DELAY = 30000;

/**
 * useDatasetLive connects a WebSocket to the global broadcast channel
 * (session=__global__) and calls `onEvent` for every datasets:live message.
 *
 * The hook manages reconnection with exponential backoff, and tears down
 * cleanly when the component unmounts.
 */
export function useDatasetLive(onEvent: Listener) {
  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);
  const mountedRef = useRef(true);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const { userId } = useSessionStore();

  const connect = useCallback(() => {
    if (!mountedRef.current || !userId) return;

    const url = `${config.ws}?uid=${userId}&session=__global__`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      retriesRef.current = 0;
    };

    ws.onmessage = (event) => {
      try {
        const decoded = decode(new Uint8Array(event.data as ArrayBuffer)) as Record<string, unknown>;
        const eventName = decoded?.event as string;
        if (eventName !== "datasets/live") return;

        const raw = decoded?.value as string;
        if (!raw) return;

        const parsed = JSON.parse(raw) as DatasetLiveEvent;
        onEventRef.current(parsed);
      } catch {
        // Silently ignore malformed messages.
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
      if (!mountedRef.current) return;
      const delay = Math.min(BASE_DELAY * 2 ** retriesRef.current, MAX_DELAY);
      retriesRef.current++;
      setTimeout(connect, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [userId]);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);
}
