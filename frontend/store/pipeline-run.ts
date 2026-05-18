import { create } from "zustand";
import type { PipelineRun, NodeRunState, CheckReport } from "@/types/pipeline";

/**
 * usePipelineRunStore — execution-time state only.
 *
 * Separated from usePipelineStore so that run-lifecycle events
 * (node started / completed / failed, pipeline status) do not cause
 * re-renders in components that only care about the design-time graph
 * (history, operators, suggestions).
 */
export type PipelineRunState = {
  pipelineRun: PipelineRun | null;
  checkReport: CheckReport | null;

  setPipelineRun: (run: PipelineRun | null) => void;
  updateNodeRunState: (
    run_id: string,
    node_id: string,
    patch: Partial<NodeRunState>,
  ) => void;
  setPipelineRunStatus: (
    run_id: string,
    status: PipelineRun["status"],
    error?: string,
  ) => void;
  setPipelineRunMetrics: (
    run_id: string,
    metrics: Record<string, number>,
  ) => void;
  setCheckReport: (report: CheckReport) => void;
  clearCheckReport: () => void;
  /** Reset all execution state (run + checks). Call on pipeline switch. */
  clearRun: () => void;
};

export const usePipelineRunStore = create<PipelineRunState>((set) => ({
  pipelineRun: null,
  checkReport: null,

  setPipelineRun: (run) => set({ pipelineRun: run }),

  updateNodeRunState: (run_id, node_id, patch) =>
    set((state) => {
      const cur = state.pipelineRun;
      // Silently ignore updates that belong to a different (stale) run.
      if (!cur || cur.run_id !== run_id) return state;
      return {
        pipelineRun: {
          ...cur,
          node_states: {
            ...cur.node_states,
            [node_id]: {
              ...(cur.node_states[node_id] ?? { status: "pending" }),
              ...patch,
            },
          },
        },
      };
    }),

  setPipelineRunStatus: (run_id, status, error) =>
    set((state) => {
      const cur = state.pipelineRun;
      if (!cur || cur.run_id !== run_id) return state;
      return { pipelineRun: { ...cur, status, error: error ?? cur.error } };
    }),

  setPipelineRunMetrics: (run_id, metrics) =>
    set((state) => {
      const cur = state.pipelineRun;
      if (!cur || cur.run_id !== run_id) return state;
      return { pipelineRun: { ...cur, metrics } };
    }),

  setCheckReport: (report) => set({ checkReport: report }),
  clearCheckReport: () => set({ checkReport: null }),
  clearRun: () => set({ pipelineRun: null, checkReport: null }),
}));
