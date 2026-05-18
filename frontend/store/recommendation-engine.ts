import { create } from "zustand";
import type { Pipeline } from "@/types/pipeline";

type RecommendationEngineState = {
  recommendedPipelines: Pipeline[];
  /** True while a debounced update is pending (cards about to change). */
  isUpdating: boolean;
  setRecommendedPipelines: (pipelines: Pipeline[]) => void;
  clearRecommendedPipelines: () => void;
};

/** Module-scoped debounce timer for recommendation updates. */
let _debounceTimer: ReturnType<typeof setTimeout> | null = null;
const DEBOUNCE_MS = 800;

export const useRecommendationEngineStore = create<RecommendationEngineState>(
  (set, get) => ({
    recommendedPipelines: [],
    isUpdating: false,

    setRecommendedPipelines: (pipelines) => {
      const current = get().recommendedPipelines;

      // First batch — show immediately (no debounce for initial load).
      if (current.length === 0) {
        if (_debounceTimer) {
          clearTimeout(_debounceTimer);
          _debounceTimer = null;
        }
        set({ recommendedPipelines: pipelines, isUpdating: false });
        return;
      }

      // Subsequent updates — debounce so rapid re-ranks don't cause jarring
      // card swaps.  Show isUpdating=true immediately; swap cards after settle.
      set({ isUpdating: true });
      if (_debounceTimer) clearTimeout(_debounceTimer);
      _debounceTimer = setTimeout(() => {
        _debounceTimer = null;
        set({ recommendedPipelines: pipelines, isUpdating: false });
      }, DEBOUNCE_MS);
    },

    clearRecommendedPipelines: () => {
      if (_debounceTimer) {
        clearTimeout(_debounceTimer);
        _debounceTimer = null;
      }
      set({ recommendedPipelines: [], isUpdating: false });
    },
  }),
);
