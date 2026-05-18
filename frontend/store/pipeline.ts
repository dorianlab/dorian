import { create } from "zustand";
import type {
  PipelineState,
  PipelineHistory,
} from "@/types/pipeline";

const uid = () => Math.random().toString(36).slice(2);
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v));

export const usePipelineStore = create<PipelineState>((set, get) => ({
  tempPipeline: null,
  draftPipeline: null,
  pipelineHistory: null,
  sourceExtractionId: null,

  draggingNode: null,
  customOperators: [],
  operators: [],
  operatorParams: {},

  // ---- Group updates (backend → canvas bridge) ----
  pendingGroupUpdate: null,
  setPendingGroupUpdate: (update) => set({ pendingGroupUpdate: update }),

  // ---- Suggestions ----
  suggestions: [],

  addSuggestion: (suggestion) =>
    set((state) => {
      // keep your dedupe logic
      const exists = state.suggestions.some(
        (s) =>
          s.task === suggestion.task &&
          s.risk === suggestion.risk &&
          s.action === suggestion.action,
      );
      if (exists) return state;

      return { suggestions: [...state.suggestions, suggestion] };
    }),

  removeSuggestion: (target) =>
    set((state) => ({
      suggestions: state.suggestions.filter((s) => s.sid !== target.sid),
    })),

  setSuggestions: (suggestions) => set({ suggestions }),
  clearSuggestions: () => set({ suggestions: [] }),

  // ---- Progress (Record<pid, item> for O(1) upsert/lookup) ----
  progressItems: {},

  addProgressItem: (process) =>
    set((state) => ({
      progressItems: {
        ...state.progressItems,
        [process.pid]: { ...(state.progressItems[process.pid] ?? {}), ...process },
      },
    })),

  // Accepts a plain array (e.g. from server hydration) and converts to Record.
  setProgressItems: (items) =>
    set({ progressItems: Object.fromEntries(items.map((p) => [p.pid, p])) }),

  removeProgressItem: (progressItem) =>
    set((state) => {
      const { [progressItem.pid]: _, ...rest } = state.progressItems;
      return { progressItems: rest };
    }),

  updateProgressItem: (progressItem) =>
    set((state) => {
      const entry = state.progressItems[progressItem.pid];
      if (!entry) return state;
      return {
        progressItems: {
          ...state.progressItems,
          [progressItem.pid]: { ...entry, ...progressItem },
        },
      };
    }),

  clearProgressItems: () => set({ progressItems: {} }),

  // ---- Versioning (same as your logic) ----
  createPipelineIfMissing: () =>
    set((state) => {
      if (state.pipelineHistory) return state;

      const firstVersionId = uid();
      const now = new Date().toISOString();

      const working = state.tempPipeline as any;
      const draft = state.draftPipeline as any;

      const pipelineHistory: PipelineHistory = {
        uuid: uid(),
        headId: firstVersionId,
        pipelines: [
          {
            id: firstVersionId,
            createdAt: now,
            message: "Initial version",
            ...working,
            ...draft,
            timestamp: now,
          } as any,
        ],
      };

      return { pipelineHistory };
    }),

  getHeadVersion: () => {
    const h = get().pipelineHistory;
    if (!h) return null;
    return h.pipelines.find((v) => v.id === h.headId) ?? null;
  },

  updateHeadGraph: ({ nodes, edges }) =>
    set((state) => {
      const h = state.pipelineHistory;
      if (!h) return state;

      const headIndex = h.pipelines.findIndex((v) => v.id === h.headId);
      if (headIndex === -1) return state;

      const pipelines = [...h.pipelines];
      const head = pipelines[headIndex];

      pipelines[headIndex] = {
        ...head,
        nodes: nodes ? clone(nodes) : head.nodes,
        edges: edges ? clone(edges) : head.edges,
      };

      return { pipelineHistory: { ...h, pipelines } };
    }),

  saveNewVersionFromCurrent: (opts) =>
    set((state) => {
      const h = state.pipelineHistory;
      if (!h) return state;

      const head = h.pipelines.find((v) => v.id === h.headId);
      if (!head) return state;

      const draft = state.draftPipeline as any;

      const baseNodes = draft?.nodes ?? head.nodes;
      const baseEdges = draft?.edges ?? head.edges;

      const newId = uid();
      const now = new Date().toISOString();

      const newVersion = {
        id: newId,
        createdAt: now,
        message: opts?.message ?? "Saved",
        nodes: clone(baseNodes),
        edges: clone(baseEdges),
      };

      return {
        pipelineHistory: {
          ...h,
          headId: newId,
          pipelines: [...h.pipelines, newVersion as any],
        },
      };
    }),

  restoreVersion: (versionId) =>
    set((state) => {
      const h = state.pipelineHistory;
      if (!h) return state;

      const v = h.pipelines.find((x) => x.id === versionId);
      if (!v) return state;

      return {
        pipelineHistory: { ...h, headId: versionId },
        tempPipeline: {
          uuid: h.uuid,
          nodes: clone(v.nodes),
          edges: clone(v.edges),
        } as any,
      };
    }),
  //remove pipeline
  removePipeline: () =>
    set({
      pipelineHistory: null,
      tempPipeline: null,
      draftPipeline: null,
      suggestions: [],
      progressItems: {},
    }),

  setTempPipeline: (pipeline) => set({ tempPipeline: pipeline }),

  setDraftPipeline: (pipeline) => set({ draftPipeline: pipeline }),

  setPipelineHistory: (pipelineHistory) => set({ pipelineHistory }),

  setCustomOperators: (customOperators) => set({ customOperators }),

  setDraggingNode: (draggingNode) => set({ draggingNode }),
  setOperators: (operators) => set({ operators }),
  setOperatorParams: (operatorParams) => set({ operatorParams }),
  addCustomOperator: (operator) =>
    set((state) => ({
      customOperators: [...state.customOperators, operator],
    })),
  setAdapters: (adapters) => set({ adapters }),

  setSourceExtractionId: (sourceExtractionId) => set({ sourceExtractionId }),
}));
