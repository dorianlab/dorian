/**
 * frontend/components/vault/EnvironmentDialog.tsx
 * -------------------------------------------------
 * Modal dialog for managing encrypted user environment variables.
 *
 * Opened from the UserMenu.  Contains a passphrase gate, add/delete
 * controls, and a variable list.
 */
"use client";

import { useState, useCallback, useEffect } from "react";
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
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import { useVaultStore } from "@/store/vault";
import { useSessionStore } from "@/store/session";
import {
  encrypt,
  storePassphrase,
  clearPassphrase,
  getPassphrase,
  isVaultUnlocked,
} from "@/lib/vault-crypto";
import { SecurityInfoPanel } from "./SecurityInfoPanel";
import { toast } from "sonner";

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
    <div className="flex flex-col gap-3 py-4">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Lock className="h-4 w-4 flex-shrink-0" />
        <span>Choose a passphrase to encrypt your environment variables.</span>
      </div>
      <div className="relative">
        <Input
          type={showPass ? "text" : "password"}
          placeholder="Vault passphrase"
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleUnlock()}
          className="pr-10"
          autoComplete="off"
          data-1p-ignore
          data-lpignore="true"
          data-form-type="other"
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
    if (!passphrase) {
      toast.error("Vault is locked. Please re-enter your passphrase.");
      return;
    }

    setSaving(true);
    try {
      const envelope = await encrypt(value, passphrase);
      await addEnvVar(userId, name.trim().toUpperCase(), envelope);
      setValue("");
      setName("");
      onClose();
    } catch (err) {
      console.error("[vault] Failed to save env var:", err);
      toast.error("Failed to save variable");
    } finally {
      setSaving(false);
    }
  }, [name, value, userId, addEnvVar, onClose]);

  return (
    <form
      className="flex flex-col gap-2 p-3 border rounded-md bg-muted/30"
      onSubmit={(e) => { e.preventDefault(); handleSave(); }}
      autoComplete="off"
    >
      {/* Hidden fields defeat browser autofill heuristics */}
      <input type="text" name="prevent_autofill" className="hidden" tabIndex={-1} />
      <input type="password" name="prevent_autofill_pw" className="hidden" tabIndex={-1} />

      <Input
        type="text"
        placeholder="Variable name (e.g. OPENROUTER_API_KEY)"
        value={name}
        onChange={(e) => setName(e.target.value.replace(/[^A-Za-z0-9_]/g, ""))}
        className="font-mono text-xs"
        autoComplete="off"
        data-1p-ignore
        data-lpignore="true"
        data-form-type="other"
        name="env_var_name"
      />
      <div className="relative">
        <Input
          type={showValue ? "text" : "password"}
          placeholder="Secret value"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSave()}
          className="pr-10 text-xs"
          autoComplete="new-password"
          data-1p-ignore
          data-lpignore="true"
          data-form-type="other"
          name="env_var_value"
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
          type="submit"
          disabled={!name.trim() || !value.trim() || saving}
          className="flex-1"
        >
          {saving ? "Encrypting..." : "Save"}
        </Button>
        <Button size="sm" variant="outline" type="button" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Main dialog
// ---------------------------------------------------------------------------

interface EnvironmentDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function EnvironmentDialog({ open, onOpenChange }: EnvironmentDialogProps) {
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

  // Re-check sessionStorage on open — if the passphrase expired, reset state
  useEffect(() => {
    if (open) {
      const unlocked = isVaultUnlocked();
      setPassphraseUnlocked(unlocked);
      if (unlocked && userId) fetchEnvVars(userId);
    }
  }, [open, userId, setPassphraseUnlocked, fetchEnvVars]);

  const handleUnlock = useCallback(() => {
    setPassphraseUnlocked(true);
    if (userId) fetchEnvVars(userId);
  }, [setPassphraseUnlocked, fetchEnvVars, userId]);

  const handleLock = useCallback(() => {
    clearPassphrase();
    setPassphraseUnlocked(false);
    setShowAddForm(false);
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

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" aria-describedby={undefined}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-8">
            <KeyRound className="h-4 w-4 flex-shrink-0" />
            <span className="flex-1">Environment Variables</span>
            <button
              onClick={() => setShowSecurityInfo(!showSecurityInfo)}
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
          </DialogTitle>
        </DialogHeader>

        {showSecurityInfo ? (
          <SecurityInfoPanel onClose={() => setShowSecurityInfo(false)} />
        ) : !passphraseUnlocked ? (
          <PassphraseGate onUnlock={handleUnlock} />
        ) : (
          <div className="flex flex-col gap-3">
            {/* Add button / form */}
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

            <Separator />

            {/* Variable list */}
            <ScrollArea className="max-h-64">
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
                <ul className="space-y-1">
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
              <div className="text-[10px] text-muted-foreground flex items-center gap-1">
                <ShieldCheck className="h-3 w-3" />
                {envVars.length} variable{envVars.length !== 1 ? "s" : ""} encrypted
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
