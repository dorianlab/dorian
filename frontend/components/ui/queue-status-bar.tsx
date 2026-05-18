"use client";

import * as React from "react";
import { useQueueStatusStore } from "@/store/queue-status";
import { cn } from "@/helpers/utils";
import { Clock3, Loader2, Users } from "lucide-react";

function formatWait(seconds: number): string {
  if (seconds <= 0) return "starting soon";
  if (seconds < 60) return `~${Math.ceil(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.ceil(seconds % 60);
  if (m < 60) return s > 0 ? `~${m}m ${s}s` : `~${m}m`;
  const h = Math.floor(m / 60);
  return `~${h}h ${m % 60}m`;
}

/**
 * Inline status bar shown when the user's pipeline is queued.
 *
 * Displays:
 *   - Queue position ("3rd in queue")
 *   - Tasks ahead count
 *   - Estimated wait time (from observability data)
 *   - Total queue depth
 *
 * Automatically hides when the pipeline starts running.
 */
export default function QueueStatusBar({ className }: { className?: string }) {
  const queued = useQueueStatusStore((s) => s.queued);
  const position = useQueueStatusStore((s) => s.position);
  const ahead = useQueueStatusStore((s) => s.ahead);
  const estimatedWaitS = useQueueStatusStore((s) => s.estimatedWaitS);
  const queueDepth = useQueueStatusStore((s) => s.queueDepth);
  const concurrencyLimited = useQueueStatusStore((s) => s.concurrencyLimited);
  const concurrentCurrent = useQueueStatusStore((s) => s.concurrentCurrent);
  const concurrentMax = useQueueStatusStore((s) => s.concurrentMax);
  const tierLabel = useQueueStatusStore((s) => s.tierLabel);

  if (!queued && !concurrencyLimited) return null;

  return (
    <div
      className={cn(
        "flex items-center gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm dark:border-amber-800 dark:bg-amber-950/30",
        className,
      )}
      role="status"
      aria-live="polite"
    >
      <Loader2 className="h-4 w-4 animate-spin text-amber-600 dark:text-amber-400 shrink-0" />

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 flex-1 min-w-0">
        {queued && (
          <>
            <span className="font-medium text-amber-800 dark:text-amber-200">
              {ahead === 0
                ? "Your pipeline is next"
                : `Position ${position} in queue`}
            </span>

            {ahead > 0 && (
              <span className="flex items-center gap-1.5 text-amber-700 dark:text-amber-300">
                <Users className="h-3.5 w-3.5" />
                {ahead} workload{ahead !== 1 ? "s" : ""} ahead
              </span>
            )}

            {estimatedWaitS > 0 && (
              <span className="flex items-center gap-1.5 text-amber-700 dark:text-amber-300">
                <Clock3 className="h-3.5 w-3.5" />
                Est. wait: {formatWait(estimatedWaitS)}
              </span>
            )}

            {queueDepth > 1 && (
              <span className="text-xs text-amber-600/70 dark:text-amber-400/70">
                ({queueDepth} total in queue)
              </span>
            )}
          </>
        )}

        {concurrencyLimited && !queued && (
          <span className="text-amber-700 dark:text-amber-300">
            Running {concurrentCurrent}/{concurrentMax} concurrent pipelines
            <span className="text-xs ml-1.5 text-amber-600/70 dark:text-amber-400/70">
              ({tierLabel} tier)
            </span>
          </span>
        )}
      </div>
    </div>
  );
}
