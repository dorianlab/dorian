"use client";

import React, { useEffect, useMemo, useState } from "react";
import { cn } from "@/helpers/utils";
import { X, AlertCircle, Timer } from "lucide-react";
import {
  Popover,
  PopoverTrigger,
  PopoverContent,
} from "@/components/ui/popover";

export type NodeStatus =
  | "success"
  | "warning"
  | "error"
  | "running"
  | "skipped"
  | "idle";

type Props = {
  title?: string;
  status?: NodeStatus;
  /** Short error message (one-liner) shown in the popover header. */
  errorMessage?: string;
  /** Full traceback shown in a scrollable code block inside the popover. */
  errorTrace?: string;
  /** Unix timestamp (seconds) when execution started — drives live counter. */
  startTime?: number;
  /** Final duration in seconds (set on completion/failure). */
  duration?: number;
  showDelete?: boolean;
  onDelete?: (e: React.MouseEvent) => void;
  onClick?: (e: React.MouseEvent) => void;
  className?: string;
  style?: React.CSSProperties;
  children: React.ReactNode;
};

const STATUS_GLOW: Record<
  NodeStatus,
  { bg: string; ring: string; border: string; shadow: string; extra: string }
> = {
  success: {
    bg: "!bg-emerald-50 dark:!bg-emerald-950/40",
    ring: "ring-1 ring-emerald-600/50",
    border: "border-emerald-600/60",
    shadow:
      "shadow-[0_0_0_1px_rgba(5,150,105,0.25),0_0_14px_rgba(5,150,105,0.25)]",
    extra: "",
  },
  running: {
    bg: "bg-sky-50 dark:bg-sky-950/40",
    ring: "ring-1 ring-sky-500/50",
    border: "border-sky-500/60",
    shadow:
      "shadow-[0_0_0_1px_rgba(14,165,233,0.3),0_0_14px_rgba(14,165,233,0.3)]",
    extra: "animate-pulse",
  },
  warning: {
    bg: "bg-amber-50 dark:bg-amber-950/40",
    ring: "ring-1 ring-amber-500/30",
    border: "border-amber-500/40",
    shadow:
      "shadow-[0_0_0_1px_rgba(245,158,11,0.15),0_0_8px_rgba(245,158,11,0.15)]",
    extra: "",
  },
  error: {
    bg: "bg-rose-50 dark:bg-rose-950/40",
    ring: "ring-1 ring-rose-600/60",
    border: "border-rose-600/70",
    shadow:
      "shadow-[0_0_0_1px_rgba(190,18,60,0.3),0_0_18px_rgba(190,18,60,0.35)]",
    extra: "",
  },
  skipped: {
    bg: "bg-slate-100 dark:bg-slate-800/60",
    ring: "ring-1 ring-slate-400/40",
    border: "border-slate-400/50",
    shadow: "shadow-sm",
    extra: "opacity-60",
  },
  idle: {
    bg: "bg-card",
    ring: "ring-1 ring-border",
    border: "border-border",
    shadow: "shadow-sm",
    extra: "",
  },
};

function statusToGlow(status: NodeStatus) {
  return STATUS_GLOW[status] ?? STATUS_GLOW.idle;
}

/**
 * Maps execution-time run status to the visual NodeStatus.
 * Kept as a pure lookup so the bridge hook can call it with the raw value
 * from pipelineRunStore without importing switch logic.
 */
const _RUN_STATUS_MAP: Record<string, NodeStatus> = {
  pending: "idle",
  running: "running",
  success: "success",
  failed: "error",
  skipped: "skipped",
};

export function runStatusToNodeStatus(
  runStatus: string | undefined,
): NodeStatus {
  return _RUN_STATUS_MAP[runStatus ?? ""] ?? "idle";
}

/**
 * If you want auto status:
 * - data.status => direct mapping (set by the execution bridge)
 * - data.execError => error  (execution-time failure)
 * - data.error => error      (design-time validation)
 * - data.warnings?.length => warning
 * - data.lastRunOk or data.output != null => success
 * else idle
 */
export function inferStatus(data: any): NodeStatus {
  if (data?.status) return data.status as NodeStatus;
  if (data?.execError) return "error";
  if (data?.error) return "error";
  if (Array.isArray(data?.warnings) && data.warnings.length > 0)
    return "warning";
  if (data?.lastRunOk === true) return "success";
  if (data?.output != null) return "success";
  return "idle";
}

// ─── Error badge (bottom-right of the node) ────────────────────────────────

function ErrorBadge({
  errorMessage,
  errorTrace,
}: {
  errorMessage: string;
  errorTrace?: string;
}) {
  const [open, setOpen] = useState(false);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          onClick={(e) => {
            e.stopPropagation();
            setOpen((v) => !v);
          }}
          className='nodrag absolute -bottom-2.5 -right-2.5 z-10 flex items-center justify-center
                     h-5 w-5 rounded-full bg-rose-600 text-white shadow-md
                     hover:bg-rose-700 transition-colors cursor-pointer'
          title='Click to see error details'
        >
          <AlertCircle size={12} />
        </button>
      </PopoverTrigger>
      <PopoverContent
        side='bottom'
        align='end'
        className='w-[420px] max-h-[360px] overflow-auto p-0'
        onPointerDownOutside={() => setOpen(false)}
      >
        {/* Header */}
        <div className='px-3 py-2 border-b bg-rose-50 dark:bg-rose-950/40'>
          <p className='text-xs font-semibold text-rose-700 dark:text-rose-400'>
            Execution Error
          </p>
          <p className='text-xs text-rose-600 dark:text-rose-400 mt-0.5 break-words'>
            {errorMessage}
          </p>
        </div>
        {/* Trace */}
        {errorTrace && (
          <pre
            className='px-3 py-2 text-[10px] leading-tight font-mono text-muted-foreground
                         whitespace-pre-wrap break-words max-h-[260px] overflow-auto
                         bg-muted select-text'
          >
            {errorTrace}
          </pre>
        )}
      </PopoverContent>
    </Popover>
  );
}

// ─── Execution timer (shows elapsed / final duration on nodes) ───────────────

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}

function ExecutionTimer({
  status,
  startTime,
  duration,
}: {
  status: NodeStatus;
  startTime?: number;
  duration?: number;
}) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (status !== "running" || !startTime) return;
    const tick = () => setElapsed(Date.now() / 1000 - startTime);
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [status, startTime]);

  const seconds =
    status === "running" && startTime
      ? elapsed
      : duration != null
        ? duration
        : null;

  if (seconds == null) return null;

  return (
    <span className='absolute -bottom-5 right-0 text-[10px] text-muted-foreground flex items-center gap-0.5 select-none'>
      <Timer size={10} />
      {formatDuration(seconds)}
    </span>
  );
}

// ─── NodeWrapper ────────────────────────────────────────────────────────────

export default function NodeWrapper({
  title,
  status = "idle",
  errorMessage,
  errorTrace,
  startTime,
  duration,
  showDelete = true,
  onDelete,
  onClick,
  className,
  style,
  children,
}: Props) {
  const styles = useMemo(() => statusToGlow(status), [status]);

  return (
    <div
      onClick={onClick}
      style={style}
      className={cn(
        "group relative bg-card p-2 py-4 rounded-lg border transition-all select-none",
        "hover:shadow-md",
        styles.bg,
        styles.border,
        styles.ring,
        styles.shadow,
        styles.extra,
        className,
      )}
    >
      {showDelete && onDelete && (
        <button
          aria-label='Delete node'
          onClick={(e) => {
            e.stopPropagation();
            onDelete(e);
          }}
          className='absolute opacity-0 group-hover:opacity-100 top-0 -right-7 p-1 rounded-full bg-card hover:bg-rose-50 dark:hover:bg-rose-950/50 shadow border border-border'
        >
          <X size={14} className='text-rose-600' />
        </button>
      )}

      {title ? (
        <div className='px-3 text-sm font-medium truncate' title={title}>
          {title}
        </div>
      ) : null}

      <div className={cn(title ? "px-2 " : "")}>{children}</div>

      {/* Error badge — only visible on error/failed nodes with a message */}
      {status === "error" && errorMessage && (
        <ErrorBadge errorMessage={errorMessage} errorTrace={errorTrace} />
      )}

      {/* Execution timer — live counter while running, final duration after */}
      <ExecutionTimer
        status={status}
        startTime={startTime}
        duration={duration}
      />
    </div>
  );
}
