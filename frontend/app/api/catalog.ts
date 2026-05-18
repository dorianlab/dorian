import { createApiClient } from "@/lib/api-client";
import type {
  OperatorParamCatalog,
  OperatorCatalogEntry,
} from "@/types/pipeline";
import env from "@/env.config";

const catalogApi = createApiClient({
  baseURL: `${env.backend}/catalog`,
});

export interface CatalogItem {
  uuid: string;
  name: string;
}

export type { OperatorParamCatalog, OperatorCatalogEntry as OperatorParamEntry };

export interface FullCatalog {
  operators: CatalogItem[];
  tasks: CatalogItem[];
  objectives: CatalogItem[];
  evals: CatalogItem[];
  operatorParams: OperatorParamCatalog;
}

/**
 * Fetch the full catalog in a single request.
 * Preferred over individual endpoints to avoid 5 round-trips on page load.
 */
export async function fetchCatalog(): Promise<FullCatalog> {
  const { data } = await catalogApi.get<FullCatalog>("");
  return data;
}

export async function fetchOperators(): Promise<CatalogItem[]> {
  const { data } = await catalogApi.get<CatalogItem[]>("/operators");
  return data;
}

export async function fetchTasks(): Promise<CatalogItem[]> {
  const { data } = await catalogApi.get<CatalogItem[]>("/tasks");
  return data;
}

export async function fetchObjectives(): Promise<CatalogItem[]> {
  const { data } = await catalogApi.get<CatalogItem[]>("/objectives");
  return data;
}

export async function fetchEvals(): Promise<CatalogItem[]> {
  const { data } = await catalogApi.get<CatalogItem[]>("/evals");
  return data;
}

export async function fetchOperatorParams(): Promise<OperatorParamCatalog> {
  const { data } = await catalogApi.get<OperatorParamCatalog>("/operator-params");
  return data;
}
