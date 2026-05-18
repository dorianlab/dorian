"use client";

import { useState, useEffect } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Copy, Check, Loader2, Link2 } from "lucide-react";
import { toast } from "sonner";
import { ws } from "@/helpers/ws-events";

/**
 * "Connect MCP" handshake dialog.
 *
 * Emits ``CreateMcpToken`` on open; listens for ``mcp/token-issued``
 * via a one-shot WindowEvent (the socket hook re-dispatches WS events
 * as DOM events for consumers that aren't wired into the Zustand
 * stores). Shows the issued token with a Copy-to-clipboard button and
 * a short explainer.
 *
 * The token is short-lived (1 hour) and binds the MCP client to the
 * current session. Pasted into an MCP client config, it authenticates
 * every tool call against the user's live extraction state.
 */
interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function McpConnectDialog({ open, onOpenChange }: Props) {
  const [token, setToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setToken(null);
    setError(null);
    setCopied(false);
    ws.createMcpToken();

    const listener = (ev: Event) => {
      const detail = (ev as CustomEvent<{ token?: string; error?: string }>).detail;
      if (!detail) return;
      if (detail.error) {
        setError(detail.error);
        return;
      }
      if (detail.token) {
        setToken(detail.token);
      }
    };
    window.addEventListener("mcp-token-issued", listener);
    return () => window.removeEventListener("mcp-token-issued", listener);
  }, [open]);

  const copy = async () => {
    if (!token) return;
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
      toast.success("Token copied");
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Clipboard write failed — copy it manually");
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Link2 className="h-5 w-5 text-primary" />
            Connect MCP client
          </DialogTitle>
          <DialogDescription>
            An MCP client plugged into this token can read the active
            extraction (code, auto-DAG, corrected-DAG, current rules list)
            and persist new rules to your session without going through
            the card UI. The token lives for 1 hour and is bound to
            your current browser session.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {error ? (
            <p className="text-sm text-destructive">{error}</p>
          ) : !token ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Issuing token…
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2">
                <Input
                  readOnly
                  value={token}
                  className="font-mono text-xs"
                  onFocus={(e) => e.currentTarget.select()}
                />
                <Button size="sm" variant="secondary" onClick={copy} className="shrink-0">
                  {copied ? (
                    <Check className="h-3.5 w-3.5" />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                </Button>
              </div>
              <div className="text-xs text-muted-foreground space-y-1">
                <p className="font-medium text-foreground">Tools exposed to the MCP client:</p>
                <ul className="list-disc pl-4 space-y-0.5">
                  <li><code className="text-[10px]">session_info(token)</code> — which extraction is active</li>
                  <li><code className="text-[10px]">session_read_extraction(token)</code> — code + DAGs + rules</li>
                  <li><code className="text-[10px]">session_read_rules(token)</code> — your current json_specs list</li>
                  <li><code className="text-[10px]">dry_run_rule(spec, dag, target?)</code> — non-terminal test</li>
                  <li><code className="text-[10px]">graph_edit_path(a, b)</code> — corrective diff sequence</li>
                  <li><code className="text-[10px]">rule_persist_to_session(token, spec, insert_at)</code> — commit</li>
                </ul>
                <p className="pt-1">
                  The MCP server runs via{" "}
                  <code className="text-[10px]">python -m dorian.mcp.server</code>{" "}
                  (stdio) or{" "}
                  <code className="text-[10px]">--http --port 8765</code>.
                </p>
              </div>
            </>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
