/**
 * frontend/components/vault/SecurityInfoPanel.tsx
 * ------------------------------------------------
 * Informational panel explaining how Dorian secures user environment
 * variables, with a prominent research-prototype disclaimer.
 *
 * Accessible via the (i) icon on the EnvironmentPanel header.
 */
"use client";

import {
  ArrowLeft,
  ShieldCheck,
  AlertTriangle,
  Lock,
  Server,
  Key,
  Trash2,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";

interface Props {
  onClose: () => void;
}

export function SecurityInfoPanel({ onClose }: Props) {
  const [showTechnical, setShowTechnical] = useState(false);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div className="flex items-center gap-1.5 text-sm font-medium">
          <ShieldCheck className="h-4 w-4" />
          <span>Security Information</span>
        </div>
      </div>

      <Separator />

      <ScrollArea className="flex-1">
        <div className="px-3 py-3 space-y-4">
          {/* ========================================================= */}
          {/* Research Prototype Disclaimer — visually prominent         */}
          {/* ========================================================= */}
          <div className="rounded-md border border-amber-300 bg-amber-50 dark:bg-amber-950/30 dark:border-amber-700 p-3">
            <div className="flex items-start gap-2">
              <AlertTriangle className="h-5 w-5 text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
              <div className="space-y-1.5">
                <p className="text-sm font-semibold text-amber-800 dark:text-amber-300">
                  Research Prototype
                </p>
                <p className="text-xs text-amber-700 dark:text-amber-400">
                  Dorian is a research prototype developed at DFKI. It is{" "}
                  <strong>not a production-ready environment</strong>.
                </p>
                <ul className="text-xs text-amber-700 dark:text-amber-400 space-y-1 list-disc list-inside">
                  <li>
                    Do not store production API keys or credentials with high
                    financial exposure.
                  </li>
                  <li>
                    The encryption implementation has not been independently
                    audited.
                  </li>
                  <li>
                    For production workloads, use dedicated secrets managers
                    (AWS Secrets Manager, HashiCorp Vault, etc.).
                  </li>
                </ul>
              </div>
            </div>
          </div>

          {/* ========================================================= */}
          {/* How Dorian secures your variables                         */}
          {/* ========================================================= */}
          <div className="space-y-3">
            <h3 className="text-sm font-semibold">
              How Dorian Secures Your Environment Variables
            </h3>

            <div className="space-y-2.5">
              <SecurityPoint
                icon={<Lock className="h-4 w-4 text-blue-500" />}
                title="Browser-Side Encryption"
                description="Your variable values are encrypted in your browser before leaving your device. The encryption key is derived from your vault passphrase using PBKDF2 — only you know it."
              />
              <SecurityPoint
                icon={<Server className="h-4 w-4 text-blue-500" />}
                title="Server Stores Only Ciphertext"
                description="The Dorian server receives and stores only encrypted data. It cannot read your variable values. Even the service team has no access to your plaintext secrets."
              />
              <SecurityPoint
                icon={<Key className="h-4 w-4 text-blue-500" />}
                title="Passphrase Never Stored on Server"
                description="Your passphrase is held only in your browser tab's session memory. It is sent to the server only when you run a pipeline — through a one-time-use nonce that expires in 60 seconds."
              />
              <SecurityPoint
                icon={<Trash2 className="h-4 w-4 text-blue-500" />}
                title="Forgotten After Use"
                description="When your pipeline runs, the server decrypts your variables in memory, injects them into the pipeline execution, and immediately forgets both the passphrase and the plaintext values."
              />
            </div>
          </div>

          <Separator />

          {/* ========================================================= */}
          {/* Technical details (collapsible)                           */}
          {/* ========================================================= */}
          <button
            onClick={() => setShowTechnical(!showTechnical)}
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground w-full"
          >
            {showTechnical ? (
              <ChevronUp className="h-3 w-3" />
            ) : (
              <ChevronDown className="h-3 w-3" />
            )}
            Technical Details
          </button>

          {showTechnical && (
            <div className="rounded-md border bg-muted/30 p-3 space-y-2 text-xs text-muted-foreground">
              <TechDetail label="Cipher" value="AES-256-GCM (authenticated encryption)" />
              <TechDetail label="Key Derivation" value="PBKDF2-HMAC-SHA256, 600,000 iterations" />
              <TechDetail label="Salt" value="16 random bytes, unique per variable" />
              <TechDetail label="IV / Nonce" value="12 random bytes, unique per encryption" />
              <TechDetail label="Storage" value="Redis, scoped to your user ID" />
              <TechDetail label="Passphrase Scope" value="Browser sessionStorage (tab-scoped, cleared on close)" />
              <TechDetail label="Execution Nonce" value="One-time use, 60-second TTL, deleted after decryption" />
            </div>
          )}
        </div>
      </ScrollArea>

      {/* Footer */}
      <Separator />
      <div className="px-3 py-2">
        <Button size="sm" variant="outline" onClick={onClose} className="w-full">
          Back to Environment Variables
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper sub-components
// ---------------------------------------------------------------------------

function SecurityPoint({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex items-start gap-2.5">
      <div className="flex-shrink-0 mt-0.5">{icon}</div>
      <div>
        <p className="text-xs font-medium">{title}</p>
        <p className="text-[11px] text-muted-foreground leading-relaxed">
          {description}
        </p>
      </div>
    </div>
  );
}

function TechDetail({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="font-medium text-foreground/70 whitespace-nowrap">
        {label}:
      </span>
      <span>{value}</span>
    </div>
  );
}
