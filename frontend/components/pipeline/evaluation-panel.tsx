"use client";

import { useState, useEffect, useRef } from "react";
import { X, CheckCircle2, XCircle, BarChart3 } from "lucide-react";
import { cn } from "@/helpers/utils";
import { usePipelineRunStore } from "@/store/pipeline-run";

/**
 * EvaluationPanel — displays evaluation metrics after a pipeline run.
 *
 * Shown at the bottom of the canvas when:
 *   - pipelineRun.status is "success" and metrics exist, OR
 *   - pipelineRun.status is "failed" (shows error summary)
 *
 * Dismissed via the (x) button; re-appears on the next run.
 */
export function EvaluationPanel({ className }: { className?: string }) {
  const pipelineRun = usePipelineRunStore((s) => s.pipelineRun);
  const [dismissed, setDismissed] = useState(false);
  const prevRunIdRef = useRef<string | null>(null);

  // Re-show panel when a new run arrives
  useEffect(() => {
    if (pipelineRun?.run_id && pipelineRun.run_id !== prevRunIdRef.current) {
      setDismissed(false);
      prevRunIdRef.current = pipelineRun.run_id;
    }
  }, [pipelineRun?.run_id]);

  if (dismissed || !pipelineRun) return null;

  const isSuccess = pipelineRun.status === "success";
  const isFailed = pipelineRun.status === "failed";
  const metrics = pipelineRun.metrics;
  const hasMetrics = metrics && Object.keys(metrics).length > 0;

  // Only show for completed runs (success with metrics, or failure)
  if (!isSuccess && !isFailed) return null;
  if (isSuccess && !hasMetrics) return null;

  return (
    <div
      className={cn(
        "absolute top-2 left-4 right-4 z-30 rounded-lg border px-4 py-3 shadow-sm",
        "bg-card/95 backdrop-blur-sm",
        "animate-in slide-in-from-top-2 duration-300",
        isSuccess
          ? "border-emerald-200 bg-emerald-50/80"
          : "border-rose-200 bg-rose-50/80",
        className,
      )}
    >
      {/* Header row */}
      <div className='flex items-center justify-between mb-2'>
        <div className='flex items-center gap-2 text-sm font-medium'>
          {isSuccess ? (
            <>
              <CheckCircle2 className='h-4 w-4 text-emerald-600' />
              <span className='text-emerald-700'>Run completed</span>
              <BarChart3 className='h-3.5 w-3.5 text-emerald-500 ml-1' />
            </>
          ) : (
            <>
              <XCircle className='h-4 w-4 text-rose-600' />
              <span className='text-rose-700'>Run failed</span>
            </>
          )}
        </div>
        <button
          onClick={() => setDismissed(true)}
          className={cn(
            "rounded-md p-0.5 transition-colors",
            isSuccess
              ? "text-emerald-400 hover:text-emerald-600 hover:bg-emerald-100"
              : "text-rose-400 hover:text-rose-600 hover:bg-rose-100",
          )}
          aria-label='Dismiss evaluation panel'
        >
          <X className='h-3.5 w-3.5' />
        </button>
      </div>

      {/* Metrics grid (success) */}
      {isSuccess && hasMetrics && (
        <div className='flex flex-wrap gap-x-6 gap-y-1'>
          {Object.entries(metrics!).map(([name, value]) => (
            <div key={name} className='flex items-baseline gap-1.5'>
              <span className='text-xs text-muted-foreground lowercase'>
                {name}
              </span>
              <span className='text-sm font-semibold font-mono text-foreground'>
                {typeof value === "number" ? value.toFixed(4) : String(value)}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Error summary (failure) */}
      {isFailed && (
        <p className='text-xs text-rose-600 line-clamp-2'>
          {getFirstError(pipelineRun.node_states)}
        </p>
      )}
    </div>
  );
}

/** Extract the first error message from node states. */
function getFirstError(
  nodeStates: Record<string, { status: string; error?: string }>,
): string {
  if (!nodeStates) return "Pipeline execution failed";
  for (const ns of Object.values(nodeStates)) {
    if (ns.status === "failed" && ns.error) {
      return ns.error;
    }
  }
  return "Pipeline execution failed";
}
