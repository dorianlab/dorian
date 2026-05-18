/**
 * useCatalog
 * ----------
 * Fetches the static KB catalog (operators, tasks, objectives, evals,
 * operator-params) via a single REST call on mount and populates the
 * Zustand stores.
 *
 * Replaces the WS-based catalog push that previously happened inside
 * seed_session.  The catalog is session-independent and KB-cached,
 * so a single GET /catalog returns in <5 ms on warm cache.
 *
 * Call this hook once in the top-level page component.
 */
import { useEffect, useRef } from "react";
import { fetchCatalog } from "@/app/api/catalog";
import { usePipelineStore } from "@/store/pipeline";
import { useSessionStore } from "@/store/session";

export function useCatalog() {
  const fetched = useRef(false);

  useEffect(() => {
    if (fetched.current) return;
    fetched.current = true;

    fetchCatalog()
      .then((catalog) => {
        // Append the "Custom Operator" virtual entry (matches the old WS handler)
        const operators = [
          ...catalog.operators,
          { uuid: "custom", name: "Custom Operator" },
        ];

        usePipelineStore.getState().setOperators(operators);
        usePipelineStore.getState().setOperatorParams(catalog.operatorParams);
        useSessionStore.getState().setTasks(catalog.tasks);
        useSessionStore.getState().setObjectives(catalog.objectives);
        // Guard: filter out malformed entries (e.g. KB nodes with non-string names)
        const safeEvals = (catalog.evals ?? []).filter(
          (e: any) => e && typeof e.name === "string" && e.name.trim(),
        );
        useSessionStore.getState().setEvals(safeEvals);
      })
      .catch((err) => {
        console.error("[useCatalog] Failed to fetch catalog:", err);
      });
  }, []);
}
