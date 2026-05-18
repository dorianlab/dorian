/**
 * frontend/components/vault/MissingEnvVarsDialog.tsx
 * ---------------------------------------------------
 * Modal dialog shown when a user loads/imports a pipeline created by another
 * user that contains env var references (``${VAR_NAME}``) not present in
 * the current user's vault.
 *
 * For each missing variable, the user can:
 *   - **Create new**: define the variable in their vault (opens inline form)
 *   - **Connect synonym**: map to an existing vault variable (dropdown)
 *
 * The "Continue" button is disabled until all missing vars are resolved.
 */
"use client";

import { useState, useCallback, useMemo } from "react";
import { AlertTriangle, Plus, Link2 } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";

import { useVaultStore } from "@/store/vault";
import { useSessionStore } from "@/store/session";
import { encrypt, getPassphrase } from "@/lib/vault-crypto";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Resolution =
  | { type: "created" }
  | { type: "synonym"; mappedTo: string };

interface Props {
  /** Callback when the user finishes resolving all missing vars.
   *  `mappings` is a Record<originalVarName, resolvedVarName> for synonyms.
   *  Optional — defaults to a no-op (vault mutations already happen internally). */
  onResolved?: (mappings: Record<string, string>) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const _noop = () => {};

export function MissingEnvVarsDialog({ onResolved = _noop }: Props) {
  const {
    missingVars,
    missingVarsDialogOpen,
    closeMissingVarsDialog,
    envVars,
    addEnvVar,
  } = useVaultStore();
  const { userId } = useSessionStore();

  // Track resolution state per missing var
  const [resolutions, setResolutions] = useState<Record<string, Resolution>>({});
  // Track which var is being "created" inline
  const [creatingVar, setCreatingVar] = useState<string | null>(null);
  const [newVarValue, setNewVarValue] = useState("");
  const [saving, setSaving] = useState(false);

  const allResolved = useMemo(
    () => missingVars.every((v) => v in resolutions),
    [missingVars, resolutions],
  );

  const handleCreateVar = useCallback(
    async (varName: string) => {
      if (!userId || !newVarValue.trim()) return;
      const passphrase = getPassphrase();
      if (!passphrase) return;

      setSaving(true);
      try {
        const envelope = await encrypt(newVarValue, passphrase);
        await addEnvVar(userId, varName, envelope);
        setResolutions((prev) => ({ ...prev, [varName]: { type: "created" } }));
        setCreatingVar(null);
        setNewVarValue("");
      } catch (err) {
        console.error("[vault] Failed to create env var:", err);
      } finally {
        setSaving(false);
      }
    },
    [userId, newVarValue, addEnvVar],
  );

  const handleSynonym = useCallback(
    (missingVar: string, existingVar: string) => {
      setResolutions((prev) => ({
        ...prev,
        [missingVar]: { type: "synonym", mappedTo: existingVar },
      }));
    },
    [],
  );

  const handleContinue = useCallback(() => {
    // Build synonym mappings for the caller to update pipeline references
    const mappings: Record<string, string> = {};
    for (const [varName, resolution] of Object.entries(resolutions)) {
      if (resolution.type === "synonym") {
        mappings[varName] = resolution.mappedTo;
      }
      // "created" vars keep their original name — no mapping needed
    }
    closeMissingVarsDialog();
    setResolutions({});
    onResolved(mappings);
  }, [resolutions, closeMissingVarsDialog, onResolved]);

  const handleClose = useCallback(() => {
    closeMissingVarsDialog();
    setResolutions({});
  }, [closeMissingVarsDialog]);

  if (!missingVarsDialogOpen || missingVars.length === 0) return null;

  return (
    <Dialog open={missingVarsDialogOpen} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-amber-500" />
            Pipeline Requires Environment Variables
          </DialogTitle>
          <DialogDescription>
            This pipeline references environment variables that are not in your
            vault. Please define them or connect existing variables.
          </DialogDescription>
        </DialogHeader>

        <ScrollArea className="max-h-[50vh]">
          <div className="space-y-3 pr-2">
            {missingVars.map((varName) => {
              const resolution = resolutions[varName];
              const isResolved = !!resolution;

              return (
                <div
                  key={varName}
                  className={`rounded-md border p-3 transition-colors ${
                    isResolved
                      ? "border-green-200 bg-green-50"
                      : "border-amber-200 bg-amber-50/50"
                  }`}
                >
                  <div className="flex items-center justify-between mb-2">
                    <code className="text-sm font-mono font-semibold">
                      ${"{"}
                      {varName}
                      {"}"}
                    </code>
                    {isResolved && (
                      <span className="text-xs text-green-600 font-medium">
                        {resolution.type === "created"
                          ? "Created"
                          : `Mapped to ${resolution.mappedTo}`}
                      </span>
                    )}
                  </div>

                  {!isResolved && creatingVar !== varName && (
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          setCreatingVar(varName);
                          setNewVarValue("");
                        }}
                      >
                        <Plus className="h-3 w-3 mr-1" /> Create New
                      </Button>
                      {envVars.length > 0 && (
                        <select
                          className="text-xs border rounded-md px-2 py-1"
                          defaultValue=""
                          onChange={(e) => {
                            if (e.target.value) {
                              handleSynonym(varName, e.target.value);
                            }
                          }}
                        >
                          <option value="" disabled>
                            Connect synonym...
                          </option>
                          {envVars.map((v) => (
                            <option key={v.name} value={v.name}>
                              {v.name}
                            </option>
                          ))}
                        </select>
                      )}
                    </div>
                  )}

                  {creatingVar === varName && (
                    <div className="flex gap-2 mt-1">
                      <Input
                        type="password"
                        placeholder="Enter secret value"
                        value={newVarValue}
                        onChange={(e) => setNewVarValue(e.target.value)}
                        onKeyDown={(e) =>
                          e.key === "Enter" && handleCreateVar(varName)
                        }
                        className="text-xs"
                        autoFocus
                      />
                      <Button
                        size="sm"
                        onClick={() => handleCreateVar(varName)}
                        disabled={!newVarValue.trim() || saving}
                      >
                        {saving ? "..." : "Save"}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setCreatingVar(null)}
                      >
                        Cancel
                      </Button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </ScrollArea>

        <DialogFooter>
          <Button variant="outline" onClick={handleClose}>
            Cancel
          </Button>
          <Button onClick={handleContinue} disabled={!allResolved}>
            Continue
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
