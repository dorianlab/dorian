/**
 * usePipelineAutoSave
 * --------------------
 * Debounced auto-save: persists the current canvas state to the backend
 * whenever the user modifies the pipeline (add/remove nodes, connect edges,
 * edit parameters).
 *
 * How it works:
 *   1. `usePipelineSnapshotEmitter` writes every canvas change to
 *      `draftPipeline` in the Zustand store (throttled 300 ms).
 *   2. This hook watches `draftPipeline` via Zustand's vanilla `subscribe`
 *      API (does NOT cause React re-renders).  After 1 s of quiet it:
 *        a. ensures `pipelineHistory` exists (creates the first version),
 *        b. updates the HEAD version's nodes/edges in-place,
 *        c. emits `PipelineSaved` over WS so the backend persists the
 *           pipeline in Redis `session:meta`.
 *
 * No explicit Save button is required — the backend always has a
 * recent snapshot of the canvas.
 *
 * IMPORTANT: This hook uses `usePipelineStore.subscribe()` (Zustand vanilla
 * subscriber) instead of the React selector hook to avoid triggering
 * re-renders of the host component on every draftPipeline change.
 */
import { useEffect, useRef } from "react";
import { usePipelineStore } from "@/store/pipeline";
import { ws } from "@/helpers/ws-events";

const AUTO_SAVE_DEBOUNCE_MS = 1_000;

export function usePipelineAutoSave() {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    // Subscribe to store changes without causing React re-renders.
    // Zustand v5 subscribe passes (state, prevState).
    const unsub = usePipelineStore.subscribe((state, prevState) => {
      // Only react to draftPipeline changes.
      if (state.draftPipeline === prevState.draftPipeline) return;
      if (!state.draftPipeline) return;

      // Clear any pending timer — restart the debounce window.
      if (timerRef.current) clearTimeout(timerRef.current);

      timerRef.current = setTimeout(() => {
        const store = usePipelineStore.getState();

        // Nothing to save (shouldn't happen, but guard).
        if (!store.draftPipeline) return;

        // 1. Ensure a pipeline history entry exists.
        if (!store.pipelineHistory) {
          store.createPipelineIfMissing();
        }

        // 2. Update the HEAD version in-place with the latest canvas state.
        const draft = store.draftPipeline as any;
        store.updateHeadGraph({
          nodes: draft.nodes,
          edges: draft.edges,
        });

        // 3. Emit to backend — JSON-roundtrip strips non-serialisable
        //    values (functions React Flow attaches to node.data).
        const latest = usePipelineStore.getState().pipelineHistory;
        if (latest) {
          ws.pipelineSaved(JSON.parse(JSON.stringify(latest)));
        }
      }, AUTO_SAVE_DEBOUNCE_MS);
    });

    return () => {
      unsub();
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);
}
