"use client";

import React from "react";
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
import { Suggestion } from "@/types/pipeline";
import { SEVERITY_COLORS, STATUS_LABEL, operatorDisplayName } from "./constants";

interface SuggestionCardProps {
  suggestion: Suggestion;
  onOpen: () => void;
  onInteraction: (action: "upvote" | "downvote" | "accept" | "reject") => void;
}

export const SuggestionCard = React.memo(function SuggestionCard({
  suggestion,
  onOpen,
  onInteraction,
}: SuggestionCardProps) {
  const severity = suggestion.severity ?? "medium";
  const status = suggestion.status ?? "potential";
  const isActionable = status === "actionable";
  const shortDesc = suggestion.description_short || "";
  const action = suggestion.action || "";

  return (
    <button
      type="button"
      onClick={onOpen}
      className={clsx(
        "min-w-[320px] max-w-[420px] h-[132px] shrink-0 rounded-md border px-5 py-3.5 flex flex-col justify-between cursor-pointer transition-colors text-left",
        isActionable
          ? "bg-red-50/60 dark:bg-red-900/10 border-red-200 dark:border-red-800/40 hover:bg-red-100/60 dark:hover:bg-red-900/20"
          : "bg-gray-50 dark:bg-gray-700/50 border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-600/50",
      )}
    >
      {/* Top: content rows */}
      <div className="min-w-0">
        {/* Row 1: Risk name + severity indicator */}
        <div className='flex items-center gap-1.5 text-sm text-gray-700 dark:text-gray-200'>
          {isActionable ? (
            <ShieldCheckIcon className='h-3.5 w-3.5 text-red-500 shrink-0' />
          ) : (
            <span
              className={clsx(
                "inline-block h-2 w-2 rounded-full shrink-0",
                SEVERITY_COLORS[severity] ?? SEVERITY_COLORS.medium,
              )}
            />
          )}
          <span className='font-semibold truncate'>
            {suggestion.risk}
          </span>
        </div>

        {/* Row 2: Target node badge */}
        {suggestion.task && (
          <div className='flex items-center gap-1 mt-1 text-[11px] text-gray-500 dark:text-gray-400'>
            <BoxIcon className='h-3 w-3 shrink-0' />
            <span className='font-mono truncate'>{operatorDisplayName(suggestion.task)}</span>
          </div>
        )}

        {/* Row 3: Mitigation action */}
        {action && (
          <div className='flex items-center gap-1 mt-0.5 text-xs text-amber-700 dark:text-amber-400'>
            <LightbulbIcon className='h-3 w-3 shrink-0' />
            <span className='truncate font-medium'>{action}</span>
          </div>
        )}

        {/* Row 3: Short description (2-line clamp) */}
        {shortDesc && (
          <p className='mt-1 text-[11px] leading-snug text-gray-500 dark:text-gray-400 line-clamp-2'>
            {shortDesc}
          </p>
        )}
      </div>

      {/* Bottom: Status badge + quick actions (always pinned) */}
      <div className='flex items-center justify-between'>
        <span
          className={clsx(
            "inline-flex items-center px-1.5 py-0 text-[10px] font-medium rounded shrink-0",
            isActionable
              ? "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
              : "bg-gray-200 text-gray-600 dark:bg-gray-600 dark:text-gray-300",
          )}
        >
          {STATUS_LABEL[status] ?? status}
        </span>

        <div className='flex items-center space-x-0.5'>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onInteraction("upvote");
            }}
            className='p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-400'
            aria-label='Upvote suggestion'
          >
            <ThumbsUpIcon className='h-3 w-3' />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onInteraction("downvote");
            }}
            className='p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-400'
            aria-label='Downvote suggestion'
          >
            <ThumbsDownIcon className='h-3 w-3' />
          </button>
          <Button
            size='sm'
            variant='outline'
            onClick={(e) => {
              e.stopPropagation();
              onInteraction("reject");
            }}
            className='h-6 px-1.5 text-xs'
          >
            <XIcon className='h-3 w-3' />
          </Button>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span>
                  <Button
                    size='sm'
                    disabled={!suggestion.has_rewrite}
                    onClick={(e) => {
                      e.stopPropagation();
                      onInteraction("accept");
                    }}
                    className='h-6 px-1.5 text-xs'
                  >
                    <CheckIcon className='h-3 w-3' />
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
    </button>
  );
});
