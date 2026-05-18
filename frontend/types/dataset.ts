interface Profile {
  [key: string]: number;
}

interface QualityChecks {
  applied_threshold?: number;
  summary?: {
    passed: number;
    failed: number;
    pending: number;
    error: number;
    total: number;
  };
  results?: Array<{
    check: string;
    status: string;
    value?: unknown;
    threshold?: number;
    message?: string;
  }>;
}

export interface Dataset {
  uuid: string;
  filename: string;
  size: number;
  hasLabels: boolean;
  stage?: string;
  progress?: number;
  target?: string;
  columns?: string[];
  profile?: Profile;
  quality?: Record<string, unknown>;
  quality_checks?: QualityChecks;
  quality_inputs?: Record<string, unknown>;
  mitigation_session?: Record<string, unknown>;
  features?: string[];
  /** Per-column profiling metadata from the backend Dask graph. */
  columnProfiles?: Record<string, import("@/types/ui").ColumnProfile>;
  /** docstore document ID — set when dataset is persisted for cross-session discovery. */
  did?: string;
  /** Whether this dataset is publicly visible to other users. */
  isPublic?: boolean;
}

/** Shape returned by GET /datasets (docstore documents). */
export interface AvailableDataset {
  id: string;
  name: string;
  description?: string | null;
  isPublic: boolean;
  ownerId: string | null;
  itemCount?: number;
  source?: { type?: string };
  storage?: { location?: { path?: string } };
  profile?: Profile | null;
  features?: string[] | null;
  targets?: string[] | null;
  createdAt?: string;
}

export type DatasetState = {
  datasets: Dataset[];
  progress: number;

  addDatasets: (datasets: Dataset[]) => void;
  removeDataset: (uuid: string) => void;
  updateDataset: <K extends keyof Dataset>(
    uuid: string,
    key: K,
    value: Dataset[K],
  ) => void;
  setDatasets: (datasets: Dataset[]) => void;

  setProgress: (value: number) => void;
};
