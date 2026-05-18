import { create } from "zustand";
import type { UIState, Toggles } from "@/types/ui";

const defaultToggles: Toggles = {
  DatasetUpload: false,
  DatasetDelete: false,
  TaskSelection: false,
  EvalSelection: false,
  ObjectiveSelection: false,
  ObjectiveDelete: false,
  ObjectiveDragging: false,
  PipelineImport: false,
  PipelineComposition: true,
};

export const useUIStore = create<UIState>((set) => ({
  pointer: { x: -1, y: -1 },
  username: undefined,
  avatar: undefined,
  code: "",
  language: "python",
  showCodeViewer: false,
  direction: "TB",
  command: false,
  selectedTask: undefined,
  selectedEval: undefined,

  selectedObjectives: [],
  objectiveStatus: [],
  objectivesConflict: null,
  queries: [],
  feedbackModalOpen: false,

  toggles: defaultToggles,

  setPointer: (pointer) => set({ pointer }),
  setName: (name) => set({ username: name }),
  setAvatar: (url) => set({ avatar: url }),

  setCode: (code) => set({ code }),
  setLanguage: (language) => set({ language }),
  setShowCodeViewer: (show) => set({ showCodeViewer: show }),
  setDirection: (direction) => set({ direction }),

  setCommand: (open) => set({ command: open }),
  setSelectedTask: (task) => set({ selectedTask: task }),
  setSelectedEval: (evaluation) => set({ selectedEval: evaluation }),
  setSelectedObjectives: (selectedObjectives) => set({ selectedObjectives }),
  setObjectiveStatus: (objectiveStatus) => set({ objectiveStatus }),
  setObjectivesConflict: (objectivesConflict) => set({ objectivesConflict }),

  setQueries: (queries) =>
    set((s) => {
      // Deduplicate by id to prevent duplicate React keys when the same
      // query is emitted multiple times (e.g., stream replay, re-triggered
      // attempt_recommendations).
      const merged = new Map(s.queries.map((q) => [q.id, q]));
      for (const q of queries) merged.set(q.id, q);
      return { queries: Array.from(merged.values()) };
    }),

  removeQueries: (ids) =>
    set((s) => ({
      queries: s.queries.filter((q) => !ids.includes(q.id)),
    })),

  setFeedbackModalOpen: (open) => set({ feedbackModalOpen: open }),

  setToggle: (key, value) =>
    set((s) => ({ toggles: { ...s.toggles, [key]: value } })),

  sidebarCollapsed: false,
  setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
}));
