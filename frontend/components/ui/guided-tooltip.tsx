"use client";

/**
 * GuidedTooltip
 * -------------
 * A drop-in wrapper that reads tooltip content from `useTooltipStore`
 * (populated from the backend via the `ui/tooltips` WebSocket event).
 *
 * During the onboarding tour, the current step's tooltip is shown as a
 * popover with skip/next/got it controls and upvote/downvote buttons.
 * Dwell time (how long the tooltip is visible before the user acts) is
 * tracked and sent alongside votes.
 *
 * Usage
 * -----
 * Wrap any UI element and give it a `targetId` that matches a key in
 * `dorian/ui/tooltips.py`:
 *
 *   <GuidedTooltip targetId="dataset-upload">
 *     <Button>Upload</Button>
 *   </GuidedTooltip>
 */

import React, { useEffect, useRef } from "react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useTooltipStore } from "@/store/tooltip";
import { cn } from "@/helpers/utils";
import { ThumbsUp, ThumbsDown, ChevronLeft, ChevronRight, X } from "lucide-react";

interface GuidedTooltipProps {
  targetId: string;
  children: React.ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  align?: "start" | "center" | "end";
  className?: string;
  /** Override the wrapper's layout class. Defaults to "flex w-full h-full".
   *  Pass "contents" for fixed-position children that must not participate
   *  in parent flex layout (CSS `display: contents` makes the wrapper
   *  invisible to layout). */
  wrapperClassName?: string;
}

export function GuidedTooltip({
  targetId,
  children,
  side = "top",
  align,
  className,
  wrapperClassName,
}: GuidedTooltipProps) {
  const getTooltip = useTooltipStore((s) => s.getTooltip);
  const tourActive = useTooltipStore((s) => s.tourActive);
  const tourStep = useTooltipStore((s) => s.tourStep);
  const tooltips = useTooltipStore((s) => s.tooltips);
  const skipTour = useTooltipStore((s) => s.skipTour);
  const nextStep = useTooltipStore((s) => s.nextStep);
  const prevStep = useTooltipStore((s) => s.prevStep);
  const markStepShown = useTooltipStore((s) => s.markStepShown);
  const voteAndAdvance = useTooltipStore((s) => s.voteAndAdvance);
  const tooltipVotes = useTooltipStore((s) => s.tooltipVotes);
  const dismissTooltip = useTooltipStore((s) => s.dismissTooltip);
  const dismissedTooltips = useTooltipStore((s) => s.dismissedTooltips);

  const registerStep = useTooltipStore((s) => s.registerStep);
  const unregisterStep = useTooltipStore((s) => s.unregisterStep);

  const entry = getTooltip(targetId);
  const isDismissed = dismissedTooltips.has(targetId);
  const triggerRef = useRef<HTMLDivElement>(null);

  const isCurrentStep = tourActive && entry != null && entry.step === tourStep;
  const previousVote = tooltipVotes[targetId]?.vote;

  // Compute max step for progress display
  const maxStep = tooltips
    ? Math.max(
        0,
        ...Object.values(tooltips)
          .map((t) => t.step)
          .filter((s) => s > 0),
      )
    : 0;

  // Register this step as mounted so the tour can skip unmounted steps
  useEffect(() => {
    if (entry && entry.step > 0) {
      registerStep(entry.step);
      return () => unregisterStep(entry.step);
    }
  }, [entry, registerStep, unregisterStep]);

  // Mark step shown when this becomes the current tour step
  useEffect(() => {
    if (isCurrentStep) {
      markStepShown();
      // Scroll into view if needed
      triggerRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [isCurrentStep, markStepShown]);

  // If no tooltip entry, render children as-is
  if (!entry) {
    return <>{children}</>;
  }

  // During active tour on the current step, show as controlled popover
  if (isCurrentStep) {
    // "contents" removes the wrapper from layout, leaving the tooltip with
    // no anchor box.  For fixed-position children (e.g. the feedback bug
    // button) we must give the wrapper its own box so Radix can compute
    // the popover position.  We inherit the child's fixed positioning by
    // swapping "contents" for a concrete class.
    const tourWrapperClass =
      wrapperClassName === "contents"
        ? "fixed bottom-1/3 right-0 z-[100]"
        : (wrapperClassName ?? "flex w-full h-full");

    return (
      <TooltipProvider>
        <Tooltip open delayDuration={0}>
          <TooltipTrigger asChild>
            <div
              ref={triggerRef}
              className={cn(
                // Defaults FIRST so wrapperClassName overrides can win the
                // tailwind-merge contest (e.g. `absolute` overrides `relative`
                // for the canvas badge that anchors a viewport-bound popover).
                "relative ring-2 ring-primary ring-offset-2 rounded-md",
                tourWrapperClass,
                className,
              )}
            >
              {children}
            </div>
          </TooltipTrigger>

          <TooltipContent
            side={side}
            align={align}
            sticky="always"
            collisionPadding={20}
            className='max-w-sm p-0 bg-white text-foreground border border-border shadow-lg z-[200]'
            onPointerDownOutside={(e) => e.preventDefault()}
          >
            <div className='p-3'>
              {/* Header with step counter */}
              <div className='flex items-center justify-between mb-1'>
                <p className='font-semibold text-sm'>{entry.title}</p>
                <span className='text-[10px] text-muted-foreground ml-2 whitespace-nowrap'>
                  {entry.step}/{maxStep}
                </span>
              </div>

              <p className='text-xs text-muted-foreground leading-relaxed mb-3 text-gray-600'>
                {entry.content}
              </p>

              {/* Action bar */}
              <div className='flex items-center justify-between'>
                {/* Vote buttons */}
                <div className='flex items-center gap-1'>
                  <span className='text-[10px] text-muted-foreground mr-1'>Helpful?</span>
                  <button
                    onClick={() => voteAndAdvance(targetId, "up")}
                    className={cn(
                      "p-1 rounded hover:bg-muted transition-colors",
                      previousVote === "up" && "text-green-500",
                    )}
                    title='Helpful'
                  >
                    <ThumbsUp className='h-3.5 w-3.5' />
                  </button>
                  <button
                    onClick={() => voteAndAdvance(targetId, "down")}
                    className={cn(
                      "p-1 rounded hover:bg-muted transition-colors",
                      previousVote === "down" && "text-red-500",
                    )}
                    title='Not helpful'
                  >
                    <ThumbsDown className='h-3.5 w-3.5' />
                  </button>
                </div>

                {/* Navigation buttons */}
                <div className='flex items-center gap-1'>
                  <button
                    onClick={skipTour}
                    className='text-[11px] text-muted-foreground hover:text-foreground px-2 py-1 rounded hover:bg-muted transition-colors'
                  >
                    Skip tour
                  </button>
                  {tourStep > 1 && (
                    <button
                      onClick={prevStep}
                      className='text-[11px] flex items-center gap-0.5 text-muted-foreground hover:text-foreground px-2 py-1 rounded hover:bg-muted transition-colors'
                    >
                      <ChevronLeft className='h-3 w-3' />
                      Back
                    </button>
                  )}
                  <button
                    onClick={nextStep}
                    className='text-[11px] flex items-center gap-0.5 bg-primary text-primary-foreground px-2.5 py-1 rounded hover:bg-primary/90 transition-colors'
                  >
                    {tourStep >= maxStep ? "Got it!" : "Next"}
                    {tourStep < maxStep && <ChevronRight className='h-3 w-3' />}
                  </button>
                </div>
              </div>
            </div>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  // Normal hover tooltip (outside tour or non-sequential tooltips).
  // If the user dismissed this tooltip, render children without tooltip.
  if (isDismissed) {
    return (
      <div className={cn(wrapperClassName ?? "flex w-full h-full", className)}>
        {children}
      </div>
    );
  }

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <div className={cn(wrapperClassName ?? "flex w-full h-full", className)}>
            {children}
          </div>
        </TooltipTrigger>

        <TooltipContent side={side} className='max-w-xs'>
          <p className='font-semibold text-sm mb-1'>{entry.title}</p>
          <p className='text-xs text-muted-foreground leading-relaxed'>
            {entry.content}
          </p>
          <button
            onClick={(e) => {
              e.stopPropagation();
              dismissTooltip(targetId);
            }}
            className='mt-1.5 text-[10px] text-muted-foreground/60 hover:text-muted-foreground transition-colors'
          >
            Don&apos;t show again
          </button>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export default GuidedTooltip;
