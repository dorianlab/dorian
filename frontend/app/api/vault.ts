/**
 * frontend/app/api/vault.ts
 * -------------------------
 * API client for the encrypted environment variable vault.
 *
 * All env var values are encrypted client-side before being sent to the
 * server.  The server stores only encrypted envelopes and cannot read
 * the plaintext values.
 */

import { apiClient } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface EnvVarEntry {
  name: string;
  hasValue: boolean;
}

export interface EncryptedEnvelope {
  ciphertext: string;
  iv: string;
  salt: string;
}

export interface EnvVarCheckResult {
  required: string[];
  available: string[];
  missing: string[];
}

// ---------------------------------------------------------------------------
// CRUD
// ---------------------------------------------------------------------------

/**
 * Store an encrypted env var on the server.
 *
 * The `envelope` is the ciphertext produced by `vault-crypto.ts#encrypt()`.
 */
export async function storeEnvVar(
  uid: string,
  varName: string,
  envelope: EncryptedEnvelope,
): Promise<void> {
  const formData = new FormData();
  formData.append("uid", uid);
  formData.append("var_name", varName);
  formData.append("envelope", JSON.stringify(envelope));

  await apiClient.post("/vault/env", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
}

/**
 * Delete an env var from the vault.
 */
export async function deleteEnvVar(
  uid: string,
  varName: string,
): Promise<void> {
  await apiClient.delete(`/vault/env/${encodeURIComponent(varName)}`, {
    params: { uid },
  });
}

/**
 * List all env var names (never values) for a user.
 */
export async function listEnvVars(uid: string): Promise<EnvVarEntry[]> {
  const { data } = await apiClient.get<EnvVarEntry[]>("/vault/env", {
    params: { uid },
  });
  return data;
}

// ---------------------------------------------------------------------------
// Pipeline reuse — env var availability check
// ---------------------------------------------------------------------------

/**
 * Check which env var references in a pipeline the user has defined.
 *
 * Used when importing/reusing another user's pipeline.
 */
export async function checkPipelineEnvVars(
  uid: string,
  pipelineJson: string,
): Promise<EnvVarCheckResult> {
  const formData = new FormData();
  formData.append("uid", uid);
  formData.append("pipeline_json", pipelineJson);

  const { data } = await apiClient.post<EnvVarCheckResult>(
    "/vault/env/check-pipeline",
    formData,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return data;
}

// ---------------------------------------------------------------------------
// Passphrase nonce (execution-time key transport)
// ---------------------------------------------------------------------------

/**
 * Send a passphrase + nonce to the server (60-second TTL).
 *
 * The nonce is then included in the `ExecutePipeline` WS payload so the
 * backend can retrieve the passphrase and decrypt env vars in-memory.
 */
export async function storePassphraseNonce(
  nonce: string,
  passphrase: string,
): Promise<void> {
  const formData = new FormData();
  formData.append("nonce", nonce);
  formData.append("passphrase", passphrase);

  await apiClient.post("/vault/nonce", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  });
}
