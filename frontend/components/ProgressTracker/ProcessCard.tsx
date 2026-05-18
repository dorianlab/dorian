"use client";

import React, { useState } from "react";
import clsx from "clsx";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  WandSparkles,
} from "lucide-react";
import { SimpleLoader } from "./SimpleLoader";
import { Button } from "@/components/ui/button";
import { ws } from "@/helpers/ws-events";
import { mitigationActionsForCheck } from "./progress-types";
import {
  formatValue,
  renderExpandedValue,
  qualityStatusIcon,
  qualityStatusClass,
} from "./value-format";
import type { ProgressItem } from "@/types/pipeline";

// ---------------------------------------------------------------------------
// Process card — single progress item
// ---------------------------------------------------------------------------

interface ProcessCardProps {
  process: ProgressItem;
  isCurrent: boolean;
  checkResult?: { status: string; message?: string };
  did?: string;
}

function ProcessCardInner({
  process,
  isCurrent,
  checkResult,
  did,
}: ProcessCardProps) {
  const [expanded, setExpanded] = useState(false);
  const hasError = process.status === "error" && !!process.error;
  const displayValue = formatValue(process.value);
  const hasStructuredValue =
    process.value !== null && typeof process.value === "object";

  const isQuality = process.category === "data_quality";
  const checkFailed = checkResult?.status === "failed";
  const actions = checkFailed ? mitigationActionsForCheck(process.metafeature) : [];
  const isExpandable =
    hasError || hasStructuredValue || (displayValue && displayValue.length > 20) || actions.length > 0;

  return (
    <div
      className={clsx(
        "rounded-md border transition-colors",
        isCurrent
          ? "border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-900/20"
          : hasError
            ? "border-red-200/60 bg-red-50/40 dark:border-red-900/40 dark:bg-red-900/10"
            : "border-transparent bg-muted/30 hover:bg-muted/60",
      )}
    >
      {/* Main row */}
      <div
        className={clsx(
          "flex items-center gap-3 px-3 py-2 text-sm",
          isExpandable && "cursor-pointer",
        )}
        onClick={isExpandable ? () => setExpanded((v) => !v) : undefined}
      >
        <div className="flex-shrink-0">
          {isQuality && checkResult ? (
            qualityStatusIcon(checkResult.status)
          ) : (
            <SimpleLoader status={process.status} size="sm" />
          )}
        </div>

        <span className="flex-1 min-w-0 font-medium break-words">
          {process.metafeature}
        </span>

        <div className="flex items-center gap-1.5 flex-shrink-0">
          {process.status === "computed" && displayValue && (
            <span className="text-xs text-muted-foreground font-mono max-w-[140px] truncate">
              {displayValue}
            </span>
          )}
          {isQuality && checkResult && (
            <span
              className={clsx(
                "rounded px-1.5 py-0.5 text-[10px] font-medium capitalize",
                qualityStatusClass(checkResult.status),
              )}
            >
              {checkResult.status}
            </span>
          )}
          {hasError && !expanded && (
            <span className="text-[10px] text-red-500 font-medium px-1.5 py-0.5 rounded bg-red-100 dark:bg-red-900/30">
              error
            </span>
          )}
          {isExpandable && (
            expanded ? (
              <ChevronUpIcon className="h-3.5 w-3.5 text-muted-foreground" />
            ) : (
              <ChevronDownIcon className="h-3.5 w-3.5 text-muted-foreground" />
            )
          )}
        </div>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="px-3 pb-2 pt-0 pl-10 space-y-2">
          {hasError && (
            <p className="text-xs text-red-600 dark:text-red-400 leading-relaxed break-words whitespace-pre-wrap">
              {process.error}
            </p>
          )}
          {!hasError && displayValue && renderExpandedValue(process.value)}
          {checkResult?.message && (
            <p className="text-xs text-muted-foreground">{checkResult.message}</p>
          )}
          {actions.length > 0 && did && (
            <div className="space-y-2 pt-1">
              <div className="flex items-center gap-1.5 text-xs font-medium">
                <WandSparkles className="h-3.5 w-3.5 text-slate-600" />
                Mitigation actions
              </div>
              {actions.map((action) => (
                <div key={action.action} className="space-y-2 rounded border bg-background px-3 py-2">
                  <div className="min-w-0">
                    <div className="text-xs font-medium">{action.title}</div>
                    <div className="text-xs text-muted-foreground">{action.description}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="default"
                      className="h-7"
                      onClick={() => ws.dataMitigationDecision({
                        did,
                        check: process.metafeature,
                        mitigation_action: action,
                        decision: "accept",
                      })}
                    >
                      Apply
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-7"
                      onClick={() => ws.dataMitigationDecision({
                        did,
                        check: process.metafeature,
                        mitigation_action: action,
                        decision: "reject",
                      })}
                    >
                      Ignore
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export const ProcessCard = React.memo(ProcessCardInner);
