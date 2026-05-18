// services/admin.ts
import { createApiClient } from "@/lib/api-client";
import env from "@/env.config";

const adminApi = createApiClient({
  baseURL: `${env.backend}/admin`,
});

export async function checkAdmin(username: string): Promise<boolean> {
  try {
    const res = await adminApi.get<{ admin: boolean }>("/check", {
      params: { username },
    });
    return res.data.admin;
  } catch {
    return false;
  }
}

export type BackupResult = {
  path: string;
  errors: string[];
  ok: boolean;
  counts?: Record<string, unknown>;
};

export async function triggerBackup(username: string): Promise<BackupResult> {
  const form = new FormData();
  form.append("username", username);
  const res = await adminApi.post<BackupResult>("/backup", form);
  return res.data;
}

export type BackupEntry = {
  name: string;
  path: string;
  manifest: Record<string, unknown>;
};

export async function listBackups(username: string): Promise<BackupEntry[]> {
  const res = await adminApi.get<{ backups: BackupEntry[] }>("/backups", {
    params: { username },
  });
  return res.data.backups;
}

export type RestoreResult = {
  source: string;
  restored: Record<string, Record<string, unknown>>;
  errors: string[];
  ok: boolean;
};

export async function triggerRestore(
  username: string,
  backupName: string,
): Promise<RestoreResult> {
  const form = new FormData();
  form.append("username", username);
  form.append("backup_name", backupName);
  const res = await adminApi.post<RestoreResult>("/restore", form);
  return res.data;
}

export async function triggerShutdown(username: string) {
  const form = new FormData();
  form.append("username", username);
  const res = await adminApi.post<{
    status: string;
    backup: { path: string; errors: string[]; ok: boolean };
  }>("/shutdown", form);
  return res.data;
}
