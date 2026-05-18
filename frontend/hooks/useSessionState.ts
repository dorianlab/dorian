/**
 * useSessionState
 * ---------------
 * Fetches session-specific UI state via REST when the active session changes.
 * Replaces the WS-based seed_session push for session data (pipeline, dataset,
 * target, lastRun, selectedTask, selectedEval, selectedObjectives).
 *
 * WS remains for server-initiated push (recommendations, suggestions, progress,
 * run status, tooltips).
 */
import { useEffect, useRef } from "react";
import { fetchSessionState, type SessionState } from "@/app/api/sessions";
import { useSessionStore } from "@/store/session";
import { usePipelineStore } from "@/store/pipeline";
import { usePipelineRunStore } from "@/store/pipeline-run";
import { useUIStore } from "@/store/ui";

export function useSessionState() {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const lastFetched = useRef<string | null>(null);

  useEffect(() => {
    if (!activeSessionId || activeSessionId === lastFetched.current) return;
    lastFetched.current = activeSessionId;

    fetchSessionState(activeSessionId)
      .then((state) => {
        const pipelineStore = usePipelineStore.getState();
        const uiStore = useUIStore.getState();
        // Pipeline
        if (state.pipeline && Object.keys(state.pipeline).length > 0) {
          pipelineStore.setPipelineHistory(state.pipeline as any);
        }

        // Last run
        if (state.lastRun) {
          usePipelineRunStore.getState().setPipelineRun(state.lastRun as any);
        }

        // Selected task (REST returns name string; store expects Task object)
        if (state.selectedTask) {
          uiStore.setSelectedTask({ name: state.selectedTask });
        }

        // Selected eval (REST returns name string; store expects Eval object)
        if (state.selectedEval) {
          uiStore.setSelectedEval({ uuid: "", name: state.selectedEval });
        }

        // Selected objectives
        if (state.selectedObjectives?.length) {
          uiStore.setSelectedObjectives(state.selectedObjectives);
        }
      })
      .catch((err) => {
        // 404 = new session with no state yet — not an error
        if (err?.response?.status === 404) return;
        console.error("[useSessionState] Failed to fetch session state:", err);
      });
  }, [activeSessionId]);
}
