"use client";

import {
  CheckIcon,
  XIcon,
  ThumbsUpIcon,
  ThumbsDownIcon,
  LightbulbIcon,
  ShieldCheckIcon,
  BoxIcon,
} from "lucide-react";
import clsx from "clsx";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Separator } from "@/components/ui/separator";
import { Suggestion } from "@/types/pipeline";
import {
  SEVERITY_COLORS,
  SEVERITY_BADGE_STYLES,
  STATUS_LABEL,
  parseJsonArray,
  operatorDisplayName,
} from "./constants";

interface SuggestionDetailDialogProps {
  suggestion: Suggestion | null;
  onClose: () => void;
  onInteraction: (action: "upvote" | "downvote" | "accept" | "reject") => void;
}

export function SuggestionDetailDialog({
  suggestion,
  onClose,
  onInteraction,
}: SuggestionDetailDialogProps) {
  if (!suggestion) return null;

  const principles = parseJsonArray(suggestion.principles);
  const alternatives = parseJsonArray(suggestion.alternatives);
  const checks = parseJsonArray(suggestion.checks);
  const severity = suggestion.severity ?? "medium";
  const status = suggestion.status ?? "potential";
  const isActionable = status === "actionable";
  const shortDesc = suggestion.description_short || "";
  const longDesc = suggestion.description_long || "";

  return (
    <Dialog open={!!suggestion} onOpenChange={(v) => !v && onClose()}>
      <DialogContent
        className='max-w-lg p-0 gap-0 overflow-hidden'
        aria-describedby={undefined}
      >
        {/* Header */}
        <DialogHeader
          className={clsx(
            "px-5 py-4",
            isActionable
              ? "bg-red-50 dark:bg-red-900/20"
              : "bg-gray-50 dark:bg-gray-800",
          )}
        >
          <div className='flex items-center gap-2'>
            {isActionable ? (
              <ShieldCheckIcon className='h-5 w-5 text-red-500 shrink-0' />
            ) : (
              <span
                className={clsx(
                  "inline-block h-3 w-3 rounded-full shrink-0",
                  SEVERITY_COLORS[severity] ?? SEVERITY_COLORS.medium,
                )}
              />
            )}
            <DialogTitle className='text-base leading-tight'>
              {suggestion.risk}
            </DialogTitle>
          </div>

          <div className='flex items-center gap-1.5 mt-2 flex-wrap'>
            <span
              className={clsx(
                "inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded",
                isActionable
                  ? "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
                  : "bg-gray-200 text-gray-600 dark:bg-gray-600 dark:text-gray-300",
              )}
            >
              {STATUS_LABEL[status] ?? status}
            </span>
            <span
              className={clsx(
                "inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded capitalize",
                SEVERITY_BADGE_STYLES[severity] ?? "bg-gray-200 text-gray-600",
              )}
            >
              {severity} severity
            </span>
            {suggestion.task && (
              <span className='inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium font-mono rounded bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300'>
                <BoxIcon className='h-3 w-3' />
                {operatorDisplayName(suggestion.task)}
              </span>
            )}
            {suggestion.pipeline_label && (
              <span className='inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded bg-gray-200 text-gray-500 dark:bg-gray-600 dark:text-gray-400'>
                {suggestion.pipeline_label}
              </span>
            )}
          </div>
        </DialogHeader>

        {/* Body */}
        <div className='px-5 py-4 space-y-4 max-h-[60vh] overflow-y-auto'>
          {/* Mitigation action */}
          {suggestion.action && (
            <div>
              <h4 className='text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1'>
                Suggested Mitigation
              </h4>
              <div className='flex items-center gap-2 text-sm font-medium text-gray-800 dark:text-gray-200'>
                <LightbulbIcon className='h-4 w-4 text-amber-500 shrink-0' />
                {suggestion.action}
              </div>
            </div>
          )}

          {/* Short description */}
          {shortDesc && (
            <p className='text-sm text-gray-600 dark:text-gray-300'>
              {shortDesc}
            </p>
          )}

          {/* Check message */}
          {isActionable && suggestion.check_message && (
            <div className='rounded-md bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 px-3 py-2'>
              <p className='text-xs text-red-700 dark:text-red-300 italic'>
                {suggestion.check_message}
              </p>
            </div>
          )}

          {/* Long description */}
          {longDesc && (
            <div>
              <h4 className='text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1'>
                Details
              </h4>
              <p className='text-sm text-gray-600 dark:text-gray-300 leading-relaxed'>
                {longDesc}
              </p>
            </div>
          )}

          {/* Target node */}
          {suggestion.task && (
            <div>
              <h4 className='text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1'>
                Target Node
              </h4>
              <div className='flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300'>
                <BoxIcon className='h-4 w-4 text-gray-400 shrink-0' />
                <span className='font-mono'>{suggestion.task}</span>
              </div>
            </div>
          )}

          {/* Alternatives */}
          {alternatives.length > 0 && (
            <div>
              <h4 className='text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1'>
                Alternative Mitigations
              </h4>
              <div className='flex flex-wrap gap-1.5'>
                {alternatives.map((alt) => (
                  <span
                    key={alt}
                    className='inline-flex items-center px-2 py-0.5 text-xs rounded bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300 font-mono'
                  >
                    {alt.split(".").pop()}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* EU principles */}
          {principles.length > 0 && (
            <div>
              <h4 className='text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1'>
                EU AI Act Principles
              </h4>
              <div className='flex flex-wrap gap-1.5'>
                {principles.map((p) => (
                  <span
                    key={p}
                    className='inline-flex items-center px-2 py-0.5 text-xs font-medium rounded bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                  >
                    {p}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Checks */}
          {checks.length > 0 && (
            <div>
              <h4 className='text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1'>
                Applicable Checks
              </h4>
              <div className='flex flex-wrap gap-1.5'>
                {checks.map((c) => (
                  <span
                    key={c}
                    className='inline-flex items-center px-2 py-0.5 text-xs rounded bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
                  >
                    {c}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Footer actions */}
        <Separator />
        <div className='flex items-center justify-between px-5 py-3'>
          <div className='flex items-center gap-1'>
            <Button
              size='sm'
              variant='ghost'
              onClick={() => onInteraction("upvote")}
              className='gap-1.5 text-gray-500'
            >
              <ThumbsUpIcon className='h-4 w-4' />
              Helpful
            </Button>
            <Button
              size='sm'
              variant='ghost'
              onClick={() => onInteraction("downvote")}
              className='gap-1.5 text-gray-500'
            >
              <ThumbsDownIcon className='h-4 w-4' />
              Not helpful
            </Button>
          </div>
          <div className='flex items-center gap-2'>
            <Button
              size='sm'
              variant='outline'
              onClick={() => onInteraction("reject")}
            >
              <XIcon className='h-4 w-4 mr-1.5' />
              Dismiss
            </Button>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span>
                    <Button
                      size='sm'
                      disabled={!suggestion.has_rewrite}
                      onClick={() => onInteraction("accept")}
                    >
                      <CheckIcon className='h-4 w-4 mr-1.5' />
                      Accept Mitigation
                    </Button>
                  </span>
                </TooltipTrigger>
                {!suggestion.has_rewrite && (
                  <TooltipContent>
                    <p>No rewrite rule available for this mitigation</p>
                  </TooltipContent>
                )}
              </Tooltip>
            </TooltipProvider>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
