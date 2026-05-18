/**
 * frontend/components/vault/EnvironmentPanel.tsx
 * -----------------------------------------------
 * UI panel for managing encrypted user environment variables.
 *
 * Shows a passphrase gate when the vault is locked, and an env var list
 * with add/delete controls when unlocked.  Env var values are encrypted
 * client-side before being sent to the server — the server never sees
 * plaintext.
 *
 * Located in the pipeline composition sidebar as a collapsible section
 * or tab.
 */
"use client";

import { useState, useCallback } from "react";
import {
  Lock,
  Unlock,
  Plus,
  Trash2,
  Info,
  Eye,
  EyeOff,
  KeyRound,
  ShieldCheck,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";

import { useVaultStore } from "@/store/vault";
import { useSessionStore } from "@/store/session";
import {
  encrypt,
  storePassphrase,
  clearPassphrase,
  getPassphrase,
} from "@/lib/vault-crypto";
import { SecurityInfoPanel } from "./SecurityInfoPanel";

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function PassphraseGate({ onUnlock }: { onUnlock: () => void }) {
  const [passphrase, setPassphrase] = useState("");
  const [showPass, setShowPass] = useState(false);

  const handleUnlock = () => {
    if (!passphrase.trim()) return;
    storePassphrase(passphrase);
    onUnlock();
    setPassphrase("");
  };

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Lock className="h-4 w-4" />
        <span>Enter your vault passphrase to manage environment variables.</span>
      </div>
      <div className="relative">
        <Input
          type={showPass ? "text" : "password"}
          placeholder="Vault passphrase"
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleUnlock()}
          className="pr-10"
        />
        <button
          type="button"
          onClick={() => setShowPass(!showPass)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
        >
          {showPass ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
        </button>
      </div>
      <Button size="sm" onClick={handleUnlock} disabled={!passphrase.trim()}>
        <Unlock className="h-4 w-4 mr-1" /> Unlock Vault
      </Button>
    </div>
  );
}

function AddEnvVarForm({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [showValue, setShowValue] = useState(false);
  const [saving, setSaving] = useState(false);

  const { userId } = useSessionStore();
  const { addEnvVar } = useVaultStore();

  const handleSave = useCallback(async () => {
    if (!name.trim() || !value.trim() || !userId) return;
    const passphrase = getPassphrase();
    if (!passphrase) return;

    setSaving(true);
    try {
      const envelope = await encrypt(value, passphrase);
      await addEnvVar(userId, name.trim().toUpperCase(), envelope);
      // Clear value from memory immediately
      setValue("");
      setName("");
      onClose();
    } catch (err) {
      console.error("[vault] Failed to save env var:", err);
    } finally {
      setSaving(false);
    }
  }, [name, value, userId, addEnvVar, onClose]);

  return (
    <div className="flex flex-col gap-2 p-2 border rounded-md bg-muted/30">
      <Input
        type="text"
        placeholder="Variable name (e.g. OPENROUTER_API_KEY)"
        value={name}
        onChange={(e) => setName(e.target.value.replace(/[^A-Za-z0-9_]/g, ""))}
        className="font-mono text-xs"
      />
      <div className="relative">
        <Input
          type={showValue ? "text" : "password"}
          placeholder="Secret value"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSave()}
          className="pr-10 text-xs"
        />
        <button
          type="button"
          onClick={() => setShowValue(!showValue)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
        >
          {showValue ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
        </button>
      </div>
      <div className="flex gap-2">
        <Button
          size="sm"
          variant="default"
          onClick={handleSave}
          disabled={!name.trim() || !value.trim() || saving}
          className="flex-1"
        >
          {saving ? "Encrypting..." : "Save"}
        </Button>
        <Button size="sm" variant="outline" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function EnvironmentPanel() {
  const [showAddForm, setShowAddForm] = useState(false);
  const [showSecurityInfo, setShowSecurityInfo] = useState(false);

  const { userId } = useSessionStore();
  const {
    envVars,
    passphraseUnlocked,
    loading,
    setPassphraseUnlocked,
    removeEnvVar,
    fetchEnvVars,
  } = useVaultStore();

  const handleUnlock = useCallback(() => {
    setPassphraseUnlocked(true);
    if (userId) fetchEnvVars(userId);
  }, [setPassphraseUnlocked, fetchEnvVars, userId]);

  const handleLock = useCallback(() => {
    clearPassphrase();
    setPassphraseUnlocked(false);
  }, [setPassphraseUnlocked]);

  const handleDelete = useCallback(
    async (varName: string) => {
      if (!userId) return;
      try {
        await removeEnvVar(userId, varName);
      } catch (err) {
        console.error("[vault] Failed to delete env var:", err);
      }
    },
    [userId, removeEnvVar],
  );

  // Security info panel (overlay)
  if (showSecurityInfo) {
    return <SecurityInfoPanel onClose={() => setShowSecurityInfo(false)} />;
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2">
        <div className="flex items-center gap-1.5 text-sm font-medium">
          <KeyRound className="h-4 w-4" />
          <span>Environment Variables</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setShowSecurityInfo(true)}
            className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground"
            title="How Dorian secures your variables"
          >
            <Info className="h-4 w-4" />
          </button>
          {passphraseUnlocked && (
            <button
              onClick={handleLock}
              className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground"
              title="Lock vault"
            >
              <Lock className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>

      <Separator />

      {/* Body */}
      {!passphraseUnlocked ? (
        <PassphraseGate onUnlock={handleUnlock} />
      ) : (
        <div className="flex flex-col flex-1 min-h-0">
          {/* Add button */}
          <div className="px-3 py-2">
            {showAddForm ? (
              <AddEnvVarForm onClose={() => setShowAddForm(false)} />
            ) : (
              <Button
                size="sm"
                variant="outline"
                onClick={() => setShowAddForm(true)}
                className="w-full"
              >
                <Plus className="h-4 w-4 mr-1" /> Add Variable
              </Button>
            )}
          </div>

          <Separator />

          {/* Variable list */}
          <ScrollArea className="flex-1 px-3">
            {loading ? (
              <div className="py-4 text-center text-xs text-muted-foreground">
                Loading...
              </div>
            ) : envVars.length === 0 ? (
              <div className="py-4 text-center text-xs text-muted-foreground">
                <ShieldCheck className="h-8 w-8 mx-auto mb-2 opacity-30" />
                <p>No environment variables defined.</p>
                <p className="mt-1">
                  Add API keys and secrets that your pipeline operators need.
                </p>
              </div>
            ) : (
              <ul className="py-2 space-y-1">
                {envVars.map((v) => (
                  <li
                    key={v.name}
                    className="flex items-center justify-between px-2 py-1.5 rounded hover:bg-muted group"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <Lock className="h-3 w-3 text-amber-500 flex-shrink-0" />
                      <span className="text-xs font-mono truncate">
                        {v.name}
                      </span>
                    </div>
                    <button
                      onClick={() => handleDelete(v.name)}
                      className="p-1 rounded opacity-0 group-hover:opacity-100 hover:bg-destructive/10 text-destructive transition-opacity"
                      title={`Delete ${v.name}`}
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </ScrollArea>

          {/* Footer status */}
          {envVars.length > 0 && (
            <>
              <Separator />
              <div className="px-3 py-1.5 text-[10px] text-muted-foreground flex items-center gap-1">
                <ShieldCheck className="h-3 w-3" />
                {envVars.length} variable{envVars.length !== 1 ? "s" : ""} encrypted
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
