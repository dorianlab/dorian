import { create } from "zustand";
import type { DatasetState } from "@/types/dataset";

export const useDatasetStore = create<DatasetState>((set) => ({
  datasets: [],
  progress: 0,

  addDatasets: (datasets) =>
    set((s) => ({ datasets: [...s.datasets, ...datasets] })),

  removeDataset: (uuid) =>
    set((s) => ({ datasets: s.datasets.filter((d) => d.uuid !== uuid) })),

  updateDataset: (uuid, key, value) =>
    set((s) => ({
      datasets: s.datasets.map((d) =>
        d.uuid === uuid ? { ...d, [key]: value } : d,
      ),
    })),
  setDatasets: (datasets) => set({ datasets }),

  setProgress: (value) => set({ progress: value }),
}));
