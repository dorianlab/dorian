import { apiClient } from "@/lib/api-client";
import type { AvailableDataset } from "@/types/dataset";

export async function uploadDataset(
  file: File,
  setProgress: (value: number) => void,
  sessionId: string,
  userId: string,
  description?: string,
) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("session_id", sessionId);
  formData.append("user_id", userId);
  if (description && description.trim()) {
    formData.append("description", description.trim());
  }
  return apiClient
    .post("/upload", formData, {
      onUploadProgress: (p) => {
        setProgress(Math.round((100 * p.loaded) / p.total));
      },
    })
    .then((resp) => {
      setProgress(100);
      return resp.data as { status: string; did?: string };
    });
}

export async function importDataset(
  file: File,
  setProgress: (value: number) => void,
  sessionId: string,
  userId: string
): Promise<string> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("session_id", sessionId);
  formData.append("user_id", userId);

  return apiClient
    .post<string>("/import", formData, {
      onUploadProgress: (p) => {
        setProgress(Math.round((100 * p.loaded) / p.total));
      },
    })
    .then((resp) => {
      setProgress(100);
      return resp.data;
    });
}

// ---------------------------------------------------------------------------
// Dataset discovery & import (cross-session)
// ---------------------------------------------------------------------------

export async function listAvailableDatasets(
  uid: string,
): Promise<AvailableDataset[]> {
  const { data } = await apiClient.get<AvailableDataset[]>("/datasets", {
    params: { uid },
  });
  return data;
}

export async function importExistingDataset(
  did: string,
  sessionId: string,
  uid: string,
): Promise<{ did: string; name: string; fpath: string }> {
  const { data } = await apiClient.post<{ did: string; name: string; fpath: string }>(`/datasets/${did}/import`, null, {
    params: { session_id: sessionId, user_id: uid },
  });
  return data;
}

export async function getDatasetDetail(
  did: string,
): Promise<AvailableDataset & { profile?: Record<string, number>; updatedAt?: string }> {
  const { data } = await apiClient.get<AvailableDataset & { profile?: Record<string, number>; updatedAt?: string }>(
    `/datasets/${did}`,
  );
  return data;
}

export interface LeaderboardEntry {
  rank: number;
  pipeline_id: string;
  metric_value: number;
  run_id: string;
  operators: string[];
  task: string | null;
  provenance: string | null;
  created_at: string | null;
}

export interface DatasetLeaderboard {
  dataset_id: string;
  metric: string;
  entries: LeaderboardEntry[];
}

export async function getDatasetLeaderboard(
  did: string,
  metric = "accuracy",
  limit = 50,
): Promise<DatasetLeaderboard> {
  const { data } = await apiClient.get<DatasetLeaderboard>(
    `/datasets/${did}/leaderboard`,
    { params: { metric, limit } },
  );
  return data;
}

export interface MetricInfo {
  name: string;
  count: number;
}

export async function getDatasetMetrics(did: string): Promise<MetricInfo[]> {
  const { data } = await apiClient.get<MetricInfo[]>(
    `/datasets/${did}/metrics`,
  );
  return data;
}

export async function updateDatasetDescription(
  did: string,
  uid: string,
  description: string,
): Promise<{ did: string; description: string | null }> {
  const formData = new FormData();
  formData.append("user_id", uid);
  formData.append("description", description);
  const { data } = await apiClient.patch<{ did: string; description: string | null }>(
    `/datasets/${did}/description`,
    formData,
  );
  return data;
}

export async function toggleDatasetVisibility(
  did: string,
  uid: string,
  isPublic: boolean,
): Promise<{ did: string; isPublic: boolean }> {
  const { data } = await apiClient.patch<{ did: string; isPublic: boolean }>(
    `/datasets/${did}/visibility`,
    null,
    { params: { user_id: uid, is_public: isPublic } },
  );
  return data;
}
