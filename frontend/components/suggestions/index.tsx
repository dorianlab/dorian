"use client";

import { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  CheckIcon,
  XIcon,
  ChevronRightIcon,
  ChevronLeftIcon,
  ShieldAlertIcon,
  FlaskConicalIcon,
} from "lucide-react";
import clsx from "clsx";
import { Suggestion } from "@/types/pipeline";
import { usePipelineStore } from "@/store/pipeline";
import { usePipelineRunStore } from "@/store/pipeline-run";
import { useSessionStore } from "@/store/session";
import { ws } from "@/helpers/ws-events";
import { SuggestionCard } from "./SuggestionCard";
import { SuggestionDetailDialog } from "./SuggestionDetail";
import { GuidedTooltip } from "@/components/ui/guided-tooltip";

interface SuggestionBarProps {
  className?: string;
}

export function SuggestionBar({ className }: SuggestionBarProps) {
  const { suggestions, removeSuggestion } = usePipelineStore();
  const { checkReport } = usePipelineRunStore();
  const { activeSessionId } = useSessionStore((state) => state);
  const [showCheckDetails, setShowCheckDetails] = useState(false);
  const [selectedSuggestion, setSelectedSuggestion] = useState<Suggestion | null>(null);

  // Scroll state for arrow buttons
  const scrollRef = useRef<HTMLDivElement>(null);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(false);

  const checkScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setCanScrollLeft(el.scrollLeft > 4);
    setCanScrollRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 4);
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    checkScroll();
    el.addEventListener("scroll", checkScroll, { passive: true });
    const ro = new ResizeObserver(checkScroll);
    ro.observe(el);
    return () => {
      el.removeEventListener("scroll", checkScroll);
      ro.disconnect();
    };
  }, [checkScroll]);

  const scrollBy = useCallback((dir: "left" | "right") => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollBy({ left: dir === "left" ? -320 : 320, behavior: "smooth" });
  }, []);

  const filtered = useMemo(
    () => suggestions.filter((s) => s.session === activeSessionId),
    [suggestions, activeSessionId],
  );

  // Sort: actionable first, then by severity
  const sorted = useMemo(() => {
    const order = { actionable: 0, potential: 1 };
    const sevOrder = { high: 0, medium: 1, low: 2 };
    return [...filtered].sort((a, b) => {
      const statusDiff =
        (order[a.status ?? "potential"] ?? 1) -
        (order[b.status ?? "potential"] ?? 1);
      if (statusDiff !== 0) return statusDiff;
      return (
        (sevOrder[a.severity ?? "medium"] ?? 1) -
        (sevOrder[b.severity ?? "medium"] ?? 1)
      );
    });
  }, [filtered]);

  const hasContent = sorted.length > 0 || checkReport !== null;

  // Re-check scroll arrows when sorted list changes
  useEffect(() => {
    requestAnimationFrame(checkScroll);
  }, [sorted.length, checkScroll]);

  const handleRecordInteraction = useCallback(
    (
      suggestion: Suggestion,
      action: "upvote" | "downvote" | "accept" | "reject",
    ) => {
      try {
        // When accepting, include the current canvas pipeline so the backend
        // can rewrite it even if the user hasn't explicitly saved yet.
        let inlinePipeline: Record<string, unknown> | undefined;
        if (action === "accept") {
          const { draftPipeline, tempPipeline, pipelineHistory } =
            usePipelineStore.getState();
          const head = pipelineHistory?.pipelines.find(
            (v) => v.id === pipelineHistory.headId,
          );
          const source = draftPipeline ?? head ?? tempPipeline;
          if (source) {
            inlinePipeline = JSON.parse(
              JSON.stringify({ nodes: (source as any).nodes, edges: (source as any).edges }),
            );
          }
        }

        ws.suggestionInteraction({
          suggestion_id: suggestion.sid,
          type: action,
          suggestion,
          ...(inlinePipeline ? { pipeline: inlinePipeline } : {}),
        });

        if (action === "accept" || action === "reject") {
          removeSuggestion(suggestion);
        }
      } catch (error) {
        console.error("Error recording interaction:", error);
      }
    },
    [removeSuggestion],
  );

  const actionableCount = sorted.filter(
    (s) => s.status === "actionable",
  ).length;
  const potentialCount = sorted.length - actionableCount;

  return (
    <AnimatePresence>
      {hasContent && (
    <motion.div
      key="suggestion-bar"
      initial={{ y: "100%", opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      exit={{ y: "100%", opacity: 0 }}
      transition={{ type: "spring", damping: 28, stiffness: 300 }}
      className={clsx("z-40 w-full mt-auto", className)}
    >
      <div aria-live="polite" className='bg-gray-50 dark:bg-gray-800 shadow-lg border border-gray-200 dark:border-gray-700 overflow-hidden'>
        {/* Header */}
        <GuidedTooltip targetId='debugger' side='top' wrapperClassName='contents'>
        <div className='flex items-center gap-2 px-5 py-2 border-b border-gray-200 dark:border-gray-700'>
          <ShieldAlertIcon className='h-4 w-4 text-amber-500' />
          <h2 className='font-semibold text-sm text-gray-800 dark:text-gray-200'>
            Debugger
          </h2>
          {sorted.length > 0 && (
            <span className='text-xs opacity-60'>
              {actionableCount > 0 && (
                <span className='text-red-500 font-medium'>
                  {actionableCount} confirmed
                </span>
              )}
              {actionableCount > 0 && potentialCount > 0 && " · "}
              {potentialCount > 0 && (
                <span>{potentialCount} potential</span>
              )}
            </span>
          )}

          {/* Check summary badges */}
          {checkReport && (
            <>
              <span className='text-gray-300 dark:text-gray-600 text-xs'>|</span>
              <button
                onClick={() => setShowCheckDetails((v) => !v)}
                className='flex items-center gap-1.5 text-xs hover:bg-gray-100 dark:hover:bg-gray-700 px-1.5 py-0.5 rounded transition-colors'
              >
                <FlaskConicalIcon className='h-3.5 w-3.5 text-blue-500' />
                <span className='text-gray-500 dark:text-gray-400'>
                  {checkReport.pipelineLabel}:
                </span>
                {checkReport.passed > 0 && (
                  <span className='inline-flex items-center gap-0.5 text-green-600 dark:text-green-400 font-medium'>
                    {checkReport.passed}
                    <CheckIcon className='h-3 w-3' />
                  </span>
                )}
                {checkReport.failed > 0 && (
                  <span className='inline-flex items-center gap-0.5 text-red-600 dark:text-red-400 font-medium'>
                    {checkReport.failed}
                    <XIcon className='h-3 w-3' />
                  </span>
                )}
                {checkReport.skipped > 0 && (
                  <span className='text-gray-400 dark:text-gray-500 font-medium'>
                    {checkReport.skipped}○
                  </span>
                )}
              </button>
            </>
          )}
        </div>
        </GuidedTooltip>

        {/* Check details dropdown */}
        {showCheckDetails && checkReport && checkReport.results.length > 0 && (
          <div className='border-b border-gray-200 dark:border-gray-700 bg-gray-100/50 dark:bg-gray-750 px-5 py-2 max-h-36 overflow-y-auto'>
            <div className='grid gap-1'>
              {checkReport.results.map((r, i) => (
                <div
                  key={`${r.check}-${r.operator}-${i}`}
                  className='flex items-center gap-2 text-xs'
                >
                  {r.status === "passed" ? (
                    <CheckIcon className='h-3 w-3 text-green-500 shrink-0' />
                  ) : r.status === "failed" ? (
                    <XIcon className='h-3 w-3 text-red-500 shrink-0' />
                  ) : (
                    <span className='h-3 w-3 text-gray-400 text-center shrink-0'>○</span>
                  )}
                  <span className='font-mono text-gray-600 dark:text-gray-300'>
                    {r.check}
                  </span>
                  <span className='text-gray-400 dark:text-gray-500 truncate'>
                    {r.message}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Cards — scrollable with arrow buttons */}
        <div className="relative">
          {/* Left scroll arrow */}
          {canScrollLeft && (
            <button
              onClick={() => scrollBy("left")}
              className="absolute left-0 top-0 bottom-0 z-10 w-8 flex items-center justify-center bg-gradient-to-r from-gray-50 dark:from-gray-800 to-transparent hover:from-gray-100 dark:hover:from-gray-700 transition-colors"
              aria-label="Scroll left"
            >
              <ChevronLeftIcon className="h-4 w-4 text-gray-500" />
            </button>
          )}

          {/* Right scroll arrow */}
          {canScrollRight && (
            <button
              onClick={() => scrollBy("right")}
              className="absolute right-0 top-0 bottom-0 z-10 w-8 flex items-center justify-center bg-gradient-to-l from-gray-50 dark:from-gray-800 to-transparent hover:from-gray-100 dark:hover:from-gray-700 transition-colors"
              aria-label="Scroll right"
            >
              <ChevronRightIcon className="h-4 w-4 text-gray-500" />
            </button>
          )}

          <div
            ref={scrollRef}
            className='w-full flex overflow-x-auto small-scrollbar gap-1.5 px-2 py-1.5'
          >
            {sorted.map((suggestion) => (
              <SuggestionCard
                key={suggestion.sid}
                suggestion={suggestion}
                onOpen={() => setSelectedSuggestion(suggestion)}
                onInteraction={(action) => handleRecordInteraction(suggestion, action)}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Detail modal */}
      <SuggestionDetailDialog
        suggestion={selectedSuggestion}
        onClose={() => setSelectedSuggestion(null)}
        onInteraction={(action) => {
          if (!selectedSuggestion) return;
          handleRecordInteraction(selectedSuggestion, action);
          if (action === "accept" || action === "reject") {
            setSelectedSuggestion(null);
          }
        }}
      />
    </motion.div>
      )}
    </AnimatePresence>
  );
}
