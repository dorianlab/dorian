"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Workflow,
  Database,
  Plus,
  BarChart3,
  ExternalLink,
  FileText,
  GitBranch,
  Users,
  Layers,
  Target,
  FlaskConical,
  Boxes,
} from "lucide-react";
import { useChatSessions } from "@/hooks/useChatSessions";
import { apiClient } from "@/lib/api-client";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface PlatformStats {
  datasets: number;
  pipelines: number;
  sessions: number;
  ranking_objectives: number;
  evaluation_procedures: number;
  operators: number;
  tasks: number;
  contact_submissions: number;
}

export default function NoSession() {
  const router = useRouter();
  const { handleCreateSession } = useChatSessions();
  const [stats, setStats] = useState<PlatformStats | null>(null);
  const [isNewSessionDialogOpen, setIsNewSessionDialogOpen] = useState(false);
  const [newSessionName, setNewSessionName] = useState("");

  useEffect(() => {
    let cancelled = false;

    // Refetch on mount, on tab focus, and on a 30s interval. The
    // backend's ``/stats`` endpoint serves a 30s in-process cache
    // (3s when degraded), so the network cost is one HEAD-shaped
    // request; the refetch here is what unsticks the welcome
    // screen when the *first* render lands on a transient zero
    // (DB warm-up, brief counter timeout). Without it the
    // component would pin to whatever the first response said,
    // including all-zeros, until the user navigated away.
    const fetchStats = () => {
      apiClient
        .get<PlatformStats>("/stats")
        .then((r) => {
          if (!cancelled) setStats(r.data);
        })
        .catch(() => {});
    };

    fetchStats();
    const interval = window.setInterval(fetchStats, 30_000);
    const onFocus = () => fetchStats();
    const onVisibility = () => {
      if (document.visibilityState === "visible") fetchStats();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  const handleNewSession = () => {
    setIsNewSessionDialogOpen(true);
  };

  // Mirrors ChatSidebar.handleCreate: require a non-empty user-provided name,
  // close the dialog, and delegate creation to the shared hook. No
  // auto-generated names — the user names their own sessions.
  const handleCreate = async () => {
    if (newSessionName.trim()) {
      setIsNewSessionDialogOpen(false);
      const name = newSessionName.trim();
      setNewSessionName("");
      try {
        await handleCreateSession(name);
      } catch {
        const { toast } = await import("sonner");
        toast.error("Failed to create session");
      }
    }
  };

  return (
    <>
    <div className="flex w-full h-full items-center justify-center">
      <div className="text-center space-y-8 max-w-lg">
        {/* Logo section */}
        <div className="space-y-3">
          <div className="flex justify-center">
            <div className="rounded-full bg-muted p-5">
              <Workflow className="h-10 w-10 text-muted-foreground" />
            </div>
          </div>
          <h2 className="text-2xl font-semibold">Dorian Studio</h2>
          <p className="text-sm text-muted-foreground">
            Assisting tool for the design of data science pipelines
          </p>
        </div>

        {/* Platform stats */}
        {stats && (
          <div className="grid grid-cols-4 gap-3">
            <StatBadge
              icon={<Database className="h-3.5 w-3.5" />}
              value={stats.datasets}
              label="Datasets"
            />
            <StatBadge
              icon={<GitBranch className="h-3.5 w-3.5" />}
              value={stats.pipelines}
              label="Pipelines"
            />
            <StatBadge
              icon={<Users className="h-3.5 w-3.5" />}
              value={stats.sessions}
              label="Users"
            />
            <StatBadge
              icon={<Boxes className="h-3.5 w-3.5" />}
              value={stats.operators}
              label="Operators"
            />
          </div>
        )}

        {/* Quick actions */}
        <div className="space-y-3 text-left">
          <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider px-1">
            Start
          </h3>
          <QuickAction
            icon={<Plus className="h-4 w-4" />}
            label="New Session"
            description="Create a new pipeline workspace"
            onClick={handleNewSession}
          />
          <QuickAction
            icon={<FileText className="h-4 w-4" />}
            label="Select Session"
            description="Choose from existing sessions in the sidebar"
            subtle
          />
        </div>

        <div className="space-y-3 text-left">
          <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider px-1">
            Explore
          </h3>
          <QuickAction
            icon={<Database className="h-4 w-4" />}
            label="Public Datasets"
            description="Browse datasets, profiles, and pipeline leaderboards"
            href="/library"
            trailing={
              <ExternalLink className="h-3.5 w-3.5 text-muted-foreground" />
            }
          />
          <QuickAction
            icon={<BarChart3 className="h-4 w-4" />}
            label="Observability"
            description="Monitor system events, handler performance, and errors"
            href="/observability"
            trailing={
              <ExternalLink className="h-3.5 w-3.5 text-muted-foreground" />
            }
          />
        </div>

        {/* Knowledge stats */}
        {stats && (
          <div className="space-y-3 text-left">
            <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider px-1">
              Knowledge Base
            </h3>
            <div className="grid grid-cols-2 gap-2 px-1">
              <KBStat
                icon={<Boxes className="h-3.5 w-3.5" />}
                value={stats.operators}
                label="Operators"
              />
              <KBStat
                icon={<Target className="h-3.5 w-3.5" />}
                value={stats.tasks}
                label="Tasks"
              />
              <KBStat
                icon={<FlaskConical className="h-3.5 w-3.5" />}
                value={stats.evaluation_procedures}
                label="Eval Procedures"
              />
              <KBStat
                icon={<Layers className="h-3.5 w-3.5" />}
                value={stats.ranking_objectives}
                label="Ranking Objectives"
              />
            </div>
          </div>
        )}
      </div>
    </div>

    {/* Create Session Dialog — mirrors ChatSidebar */}
    <Dialog open={isNewSessionDialogOpen} onOpenChange={setIsNewSessionDialogOpen}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create New Session</DialogTitle>
          <DialogDescription>
            Give your new session a name to organize your pipelines.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-4">
          <Label htmlFor="new-session">Session Name</Label>
          <Input
            className="mt-2"
            id="new-session"
            placeholder="e.g., Customer Analytics"
            value={newSessionName}
            onChange={(e) => setNewSessionName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleCreate()}
          />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => setIsNewSessionDialogOpen(false)}>
            Cancel
          </Button>
          <Button onClick={handleCreate} disabled={!newSessionName.trim()}>
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    </>
  );
}

function StatBadge({
  icon,
  value,
  label,
}: {
  icon: React.ReactNode;
  value: number;
  label: string;
}) {
  return (
    <div className="flex flex-col items-center gap-1 rounded-lg bg-muted/50 py-2.5 px-2">
      <div className="flex items-center gap-1.5 text-foreground">
        <span className="text-muted-foreground">{icon}</span>
        <span className="text-lg font-semibold tabular-nums">{value}</span>
      </div>
      <span className="text-[10px] text-muted-foreground uppercase tracking-wider">
        {label}
      </span>
    </div>
  );
}

function KBStat({
  icon,
  value,
  label,
}: {
  icon: React.ReactNode;
  value: number;
  label: string;
}) {
  return (
    <div className="flex items-center gap-2 py-1">
      <span className="text-muted-foreground">{icon}</span>
      <span className="text-sm tabular-nums font-medium">{value}</span>
      <span className="text-xs text-muted-foreground">{label}</span>
    </div>
  );
}

function QuickAction({
  icon,
  label,
  description,
  onClick,
  href,
  trailing,
  subtle,
}: {
  icon: React.ReactNode;
  label: string;
  description: string;
  onClick?: () => void;
  href?: string;
  trailing?: React.ReactNode;
  subtle?: boolean;
}) {
  const isInteractive = Boolean(onClick || href);
  const className = `flex items-center gap-3 w-full px-3 py-2.5 rounded-lg text-left transition-colors ${
    isInteractive ? "hover:bg-accent cursor-pointer" : subtle ? "opacity-60" : ""
  }`;
  const body = (
    <>
      <div className="shrink-0 text-muted-foreground">{icon}</div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium">{label}</div>
        <div className="text-xs text-muted-foreground">{description}</div>
      </div>
      {trailing && <div className="shrink-0">{trailing}</div>}
    </>
  );
  // Render as <Link> when an href is supplied so right-click → open in
  // new tab, ctrl/cmd+click, middle-click, and "copy link" all work.
  // <button onClick={router.push}> breaks every one of those.
  if (href) {
    return (
      <Link href={href} className={className}>
        {body}
      </Link>
    );
  }
  if (onClick) {
    return (
      <button type="button" className={className} onClick={onClick}>
        {body}
      </button>
    );
  }
  return <div className={className}>{body}</div>;
}
