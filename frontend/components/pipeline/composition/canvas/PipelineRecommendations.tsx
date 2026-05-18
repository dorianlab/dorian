"use client";

import { AnimatePresence, motion } from "framer-motion";
import { RefreshCwIcon } from "lucide-react";
import Recommendation from "@/components/ui/pipeline-renderer";
import { useRecommendationEngineStore } from "@/store/recommendation-engine";
import { GuidedTooltip } from "@/components/ui/guided-tooltip";

/** Hard ceiling on rendered cards — the grid stops growing past this. */
const MAX_SLOTS = 8;

/** Stagger delay per card (seconds). */
const STAGGER = 0.06;

export function PipelineRecommendations({ onPick }: any) {
  const { recommendedPipelines, isUpdating } =
    useRecommendationEngineStore();

  return (
    <div className="relative w-full px-5 py-2 h-full flex flex-col min-h-0">
      {/* ── Tour anchor — wraps only the heading, not the full scroll area */}
      <GuidedTooltip targetId="recommendation-feed" side="bottom" wrapperClassName="shrink-0 w-fit">
        <h3 className="text-sm font-medium text-muted-foreground mb-3">
          Recommended Pipelines
        </h3>
      </GuidedTooltip>

      {/* ── Updating indicator ─────────────────────────────────── */}
      <AnimatePresence>
        {isUpdating && (
          <motion.div
            key="updating-banner"
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2 }}
            className="absolute top-22 left-1/2 -translate-x-1/2 z-10 flex items-center gap-2 bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 border border-blue-200 dark:border-blue-800 rounded-full px-4 py-1.5 text-xs font-medium shadow-sm"
          >
            <RefreshCwIcon className="h-3.5 w-3.5 animate-spin" />
            Updating recommendations...
          </motion.div>
        )}
      </AnimatePresence>

      {/* ── Card grid — flexes into remaining height after the header ─
          Render only as many cards as there are recommendations.
          Always-render-8-SLOTS used to leak undefined data to empty
          ``Recommendation`` cards, which surfaced as blank tiles in
          the feed even when only 3-4 pipelines were available. */}
      <div className="grid w-full grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 auto-rows-fr gap-2 flex-1 min-h-0">
        <AnimatePresence mode="popLayout">
          {recommendedPipelines.slice(0, MAX_SLOTS).map((pipeline: any, i: number) => {
            const p = pipeline as any;
            const key =
              p?.uuid ?? p?.id ?? p?.pipelineId ?? `slot-${i}`;

            return (
              <motion.div
                key={key}
                layout
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                transition={{
                  duration: 0.35,
                  delay: i * STAGGER,
                  ease: [0.22, 1, 0.36, 1],
                }}
                className="col-span-1 min-h-0"
              >
                <Recommendation
                  index={i}
                  data={pipeline}
                  isSmallCard
                  className="col-span-1 rounded-md h-full w-full"
                  onClick={(p: any) => onPick(p)}
                />
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>
    </div>
  );
}
