import { create } from "zustand";

export interface TraceOutput {
  type: "trace_output";
  images: string[];
  logs: { path: string; content: string }[];
}

interface ModelTracingState {
  /** The node whose traces are currently displayed. */
  activeNodeId: string | null;
  /** Structured trace output for the active node. */
  traceOutput: TraceOutput | null;
  /** Open the tracing modal for a given node. */
  open: (nodeId: string, output: TraceOutput) => void;
  /** Close the modal. */
  close: () => void;
}

export const useModelTracingStore = create<ModelTracingState>((set) => ({
  activeNodeId: null,
  traceOutput: null,
  open: (nodeId, output) => set({ activeNodeId: nodeId, traceOutput: output }),
  close: () => set({ activeNodeId: null, traceOutput: null }),
}));
