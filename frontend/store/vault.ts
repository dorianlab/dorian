/**
 * frontend/store/vault.ts
 * -----------------------
 * Zustand store for the user's encrypted environment variable vault.
 *
 * This store tracks:
 * - The list of env var *names* (never values — those are encrypted)
 * - Whether the vault passphrase is currently unlocked (in sessionStorage)
 * - Missing env vars detected when importing another user's pipeline
 *
 * The actual passphrase is held in `sessionStorage` (tab-scoped), not in
 * this store, so it's automatically cleared when the tab closes.
 */

import { create } from "zustand";
import {
  listEnvVars,
  storeEnvVar as apiStoreEnvVar,
  deleteEnvVar as apiDeleteEnvVar,
} from "@/app/api/vault";
import type { EnvVarEntry, EncryptedEnvelope } from "@/app/api/vault";
import { isVaultUnlocked } from "@/lib/vault-crypto";

export interface VaultState {
  /** Env var names (never values) — synced from the server. */
  envVars: EnvVarEntry[];

  /** True when the vault passphrase is in sessionStorage. */
  passphraseUnlocked: boolean;

  /** Whether a fetch/store operation is in flight. */
  loading: boolean;

  /** Missing env vars detected during pipeline import (for MissingEnvVarsDialog). */
  missingVars: string[];

  /** Whether the MissingEnvVarsDialog should be open. */
  missingVarsDialogOpen: boolean;

  // Actions
  setEnvVars: (vars: EnvVarEntry[]) => void;
  setPassphraseUnlocked: (v: boolean) => void;
  setMissingVars: (vars: string[]) => void;
  closeMissingVarsDialog: () => void;

  /** Fetch the env var list from the server. */
  fetchEnvVars: (uid: string) => Promise<void>;

  /** Store a new env var (already encrypted). */
  addEnvVar: (uid: string, name: string, envelope: EncryptedEnvelope) => Promise<void>;

  /** Delete an env var from the vault. */
  removeEnvVar: (uid: string, name: string) => Promise<void>;
}

const _initial = {
  envVars: [] as EnvVarEntry[],
  passphraseUnlocked: isVaultUnlocked(),
  loading: false,
  missingVars: [] as string[],
  missingVarsDialogOpen: false,
};

export const useVaultStore = create<VaultState>((set, get) => ({
  ..._initial,

  setEnvVars: (vars) => set({ envVars: vars }),
  setPassphraseUnlocked: (v) => set({ passphraseUnlocked: v }),
  setMissingVars: (vars) =>
    set({ missingVars: vars, missingVarsDialogOpen: vars.length > 0 }),
  closeMissingVarsDialog: () =>
    set({ missingVarsDialogOpen: false, missingVars: [] }),

  fetchEnvVars: async (uid) => {
    set({ loading: true });
    try {
      const vars = await listEnvVars(uid);
      set({ envVars: vars, loading: false });
    } catch (err) {
      console.error("[vault] Failed to fetch env vars:", err);
      set({ loading: false });
    }
  },

  addEnvVar: async (uid, name, envelope) => {
    set({ loading: true });
    try {
      await apiStoreEnvVar(uid, name, envelope);
      // Refresh the list from server to stay in sync
      const vars = await listEnvVars(uid);
      set({ envVars: vars, loading: false });
    } catch (err) {
      console.error("[vault] Failed to store env var:", err);
      set({ loading: false });
      throw err;
    }
  },

  removeEnvVar: async (uid, name) => {
    set({ loading: true });
    try {
      await apiDeleteEnvVar(uid, name);
      // Optimistic removal + server refresh
      set((s) => ({
        envVars: s.envVars.filter((v) => v.name !== name),
        loading: false,
      }));
    } catch (err) {
      console.error("[vault] Failed to delete env var:", err);
      set({ loading: false });
      throw err;
    }
  },
}));
