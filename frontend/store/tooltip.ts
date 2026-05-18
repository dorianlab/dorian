import { create } from "zustand";
import type { TooltipEntry, TooltipMap } from "@/types/tooltip";
import { ws } from "@/helpers/ws-events";

type TooltipVote = { vote: "up" | "down"; dwell_ms: number; ts: string };

// ── localStorage helpers for dismissed tooltips ────────────────────────────
const LS_DISMISSED = "dorian:dismissed_tooltips";

function loadDismissed(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = localStorage.getItem(LS_DISMISSED);
    return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
  } catch {
    return new Set();
  }
}

function saveDismissed(ids: Set<string>) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(LS_DISMISSED, JSON.stringify(Array.from(ids)));
  } catch { /* quota exceeded — ignore */ }
}

type TooltipState = {
  /** Tooltip content received from the backend (null = not yet loaded). */
  tooltips: TooltipMap | null;

  /** Whether the onboarding tour is currently active. */
  tourActive: boolean;

  /** Index of the currently highlighted tour step (1-based). */
  tourStep: number;

  /** Whether this user has completed the tour previously. */
  tourCompleted: boolean;

  /** Votes received from the backend (persisted across sessions). */
  tooltipVotes: Record<string, TooltipVote>;

  /** Timestamp (ms) when the current tour step was first shown. */
  stepShownAt: number | null;

  /** Set of step numbers whose GuidedTooltip components are currently mounted. */
  mountedSteps: Set<number>;

  /** Step the tour is waiting to mount. When the GuidedTooltip for this
   *  step calls registerStep, the tour automatically advances to it.
   *  Used for view-transition steps (e.g. clicking Compose opens the canvas
   *  asynchronously — the tour must wait for the canvas tooltip to mount
   *  before highlighting it). */
  pendingStep: number | null;

  /** Tooltip IDs the user has dismissed ("don't show again"). Persisted in localStorage. */
  dismissedTooltips: Set<string>;

  // ── Setters ──────────────────────────────────────────────────────────────
  setTooltips: (tooltips: TooltipMap) => void;

  /** Register/unregister a step as mounted (called by GuidedTooltip on mount/unmount). */
  registerStep: (step: number) => void;
  unregisterStep: (step: number) => void;
  getTooltip: (id: string) => TooltipEntry | undefined;
  setOnboardingState: (state: { tour_completed: boolean; tooltip_votes: Record<string, TooltipVote> }) => void;

  /** Start the sequential onboarding tour from step 1. */
  startTour: () => void;

  /** Advance to the next tour step (or finish if on the last step). */
  nextStep: () => void;

  /** Go back to the previous tour step (no-op on step 1). */
  prevStep: () => void;

  /** Skip the tour and mark it as completed. */
  skipTour: () => void;

  /** End the tour (internal). */
  endTour: () => void;

  /** Record that the current step was shown (starts dwell timer). */
  markStepShown: () => void;

  /** Submit a vote for the current tooltip and advance. */
  voteAndAdvance: (tooltipId: string, vote: "up" | "down") => void;

  /** Advance the tour to the step identified by ``targetId``.  If that
   *  step is already mounted, jump immediately; otherwise queue it as
   *  ``pendingStep`` so the tour follows the user once the new view
   *  renders.  No-op when the tour is inactive or the target is unknown. */
  advanceTourTo: (targetId: string) => void;

  /** Permanently hide a hover tooltip ("don't show again"). */
  dismissTooltip: (id: string) => void;

  /** Check if a tooltip has been dismissed. */
  isDismissed: (id: string) => boolean;
};

function getMaxStep(tooltips: TooltipMap | null): number {
  if (!tooltips) return 0;
  return Math.max(
    0,
    ...Object.values(tooltips)
      .map((t) => t.step)
      .filter((s) => s > 0),
  );
}

function getTooltipIdForStep(tooltips: TooltipMap | null, step: number): string | null {
  if (!tooltips) return null;
  for (const [id, entry] of Object.entries(tooltips)) {
    if (entry.step === step) return id;
  }
  return null;
}

export const useTooltipStore = create<TooltipState>((set, get) => {
  // Expose store for demo recording tooling
  if (typeof window !== "undefined") {
    (window as any).__tooltipStore = { getState: get, setState: set };
  }
  return ({
  tooltips: null,
  tourActive: false,
  tourStep: 1,
  tourCompleted: false,
  tooltipVotes: {},
  stepShownAt: null,
  mountedSteps: new Set<number>(),
  pendingStep: null,
  dismissedTooltips: loadDismissed(),

  setTooltips: (tooltips) => set({ tooltips }),

  registerStep: (step) =>
    set((s) => {
      const next = new Set(s.mountedSteps);
      next.add(step);
      // Case 1: the tour was explicitly waiting for this step to mount.
      if (s.tourActive && s.pendingStep === step) {
        return {
          mountedSteps: next,
          tourStep: step,
          stepShownAt: Date.now(),
          pendingStep: null,
        };
      }
      // Case 2: view transition without an explicit advanceTourTo call.
      // The canvas (step 10) is the one step that requires opening a new
      // view to mount.  When it mounts during an active tour and the user
      // is sitting somewhere in the sidebar group (steps <10), follow them
      // onto the canvas automatically — this is robust against any path
      // the user takes to open the canvas (Compose button, recommendation
      // pick, pipeline import, history restore).
      if (s.tourActive && step === 10 && s.tourStep < 10) {
        return {
          mountedSteps: next,
          tourStep: 10,
          stepShownAt: Date.now(),
          pendingStep: null,
        };
      }
      return { mountedSteps: next };
    }),

  unregisterStep: (step) =>
    set((s) => {
      const next = new Set(s.mountedSteps);
      next.delete(step);
      return { mountedSteps: next };
    }),

  getTooltip: (id) => get().tooltips?.[id],

  setOnboardingState: ({ tour_completed, tooltip_votes }) =>
    set({
      tourCompleted: tour_completed,
      tooltipVotes: tooltip_votes ?? {},
    }),

  startTour: () => set({ tourActive: true, tourStep: 1, stepShownAt: Date.now(), pendingStep: null }),

  advanceTourTo: (targetId) => {
    const { tooltips, tourActive, mountedSteps } = get();
    if (!tourActive || !tooltips) return;
    const entry = tooltips[targetId];
    if (!entry || entry.step <= 0) return;
    if (mountedSteps.has(entry.step)) {
      set({ tourStep: entry.step, stepShownAt: Date.now(), pendingStep: null });
    } else {
      set({ pendingStep: entry.step });
    }
  },

  nextStep: () => {
    const { tooltips, tourStep, mountedSteps } = get();
    const maxStep = getMaxStep(tooltips);
    // Find the next mounted step, skipping unmounted ones
    let next = tourStep + 1;
    while (next <= maxStep && !mountedSteps.has(next)) {
      next++;
    }
    if (next > maxStep) {
      get().endTour();
    } else {
      set({ tourStep: next, stepShownAt: Date.now() });
    }
  },

  prevStep: () => {
    const { tourStep, mountedSteps } = get();
    // Find the previous mounted step, skipping unmounted ones
    let prev = tourStep - 1;
    while (prev >= 1 && !mountedSteps.has(prev)) {
      prev--;
    }
    if (prev >= 1) {
      set({ tourStep: prev, stepShownAt: Date.now() });
    }
  },

  skipTour: () => {
    ws.onboardingTourCompleted({});
    set({ tourActive: false, tourStep: 1, tourCompleted: true, stepShownAt: null, pendingStep: null });
  },

  endTour: () => {
    ws.onboardingTourCompleted({});
    set({ tourActive: false, tourStep: 1, tourCompleted: true, stepShownAt: null, pendingStep: null });
  },

  markStepShown: () => {
    if (get().stepShownAt === null) {
      set({ stepShownAt: Date.now() });
    }
  },

  voteAndAdvance: (tooltipId, vote) => {
    const { stepShownAt, tourStep, tooltips } = get();
    const dwellMs = stepShownAt ? Date.now() - stepShownAt : 0;

    // Send feedback to backend
    ws.onboardingTooltipFeedback({
      tooltip_id: tooltipId,
      vote,
      dwell_ms: dwellMs,
    });

    // Update local state
    set((s) => ({
      tooltipVotes: {
        ...s.tooltipVotes,
        [tooltipId]: { vote, dwell_ms: dwellMs, ts: new Date().toISOString() },
      },
    }));

    // Advance to next step
    get().nextStep();
  },

  dismissTooltip: (id) =>
    set((s) => {
      const next = new Set(s.dismissedTooltips);
      next.add(id);
      saveDismissed(next);
      return { dismissedTooltips: next };
    }),

  isDismissed: (id) => get().dismissedTooltips.has(id),
});
});
