import { create } from "zustand";

export interface ObservabilityState {
  /** "global" or a specific uid string */
  scope: string;
  /** Lookback window in seconds */
  since: number;
  setScope: (scope: string) => void;
  setSince: (since: number) => void;
}

export const useObservabilityStore = create<ObservabilityState>((set) => ({
  scope: "global",
  since: 3600,
  setScope: (scope) => set({ scope }),
  setSince: (since) => set({ since }),
}));
