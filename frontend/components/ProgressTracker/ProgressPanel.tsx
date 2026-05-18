"use client";

import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { useDatasetStore } from "@/store/dataset";
import { useUIStore } from "@/store/ui";
import { CATEGORY_LABELS, CATEGORY_ORDER } from "./progress-types";
import { buildQualityInputQuestions } from "./quality-inputs";
import { ProcessCard } from "./ProcessCard";
import { CategoryHeader } from "./CategoryHeader";
import type { ProgressItem } from "@/types/pipeline";

// ---------------------------------------------------------------------------
// Main ProcessPanel — unified with category grouping
// ---------------------------------------------------------------------------

interface ProcessPanelProps {
  processes: ProgressItem[];
  currentProcessId?: string;
}

export function ProcessPanel({
  processes,
  currentProcessId,
}: ProcessPanelProps) {
  const datasets = useDatasetStore((state) => state.datasets);
  const setQueries = useUIStore((state) => state.setQueries);
  const setFeedbackModalOpen = useUIStore((state) => state.setFeedbackModalOpen);
  const activeDataset = datasets[datasets.length - 1];
  const did = activeDataset?.did;

  const [collapsedCategories, setCollapsedCategories] = useState<Set<string>>(new Set());

  // Cross-reference quality check results from dataset store
  const checkResultMap = useMemo(() => {
    const results = activeDataset?.quality_checks?.results ?? [];
    const map = new Map<string, { status: string; message?: string }>();
    for (const r of results) {
      map.set(r.check, { status: r.status, message: r.message });
    }
    return map;
  }, [activeDataset?.quality_checks?.results]);

  // Group processes by category
  const grouped = useMemo(() => {
    const groups: Record<string, ProgressItem[]> = {};
    for (const p of processes) {
      const cat = p.category ?? "data_profiling";
      (groups[cat] ??= []).push(p);
    }
    return groups;
  }, [processes]);

  const totalError = processes.filter((p) => p.status === "error").length;
  const totalComputed = processes.filter((p) => p.status === "computed").length;

  const toggleCategory = (cat: string) => {
    setCollapsedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) next.delete(cat);
      else next.add(cat);
      return next;
    });
  };

  // Active categories in display order
  const activeCategories = CATEGORY_ORDER.filter((c) => grouped[c]?.length);

  return (
    <div className="flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold">Processes</h2>
          {did && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 text-xs"
              onClick={() => {
                const questions = buildQualityInputQuestions(activeDataset);
                if (questions.length === 0) return;
                setQueries(questions);
                setFeedbackModalOpen(true);
              }}
            >
              Edit Inputs
            </Button>
          )}
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {totalError > 0 && (
            <span className="text-red-500 font-medium">{totalError} failed</span>
          )}
          {totalComputed > 0 && (
            <span className="text-green-600 font-medium">{totalComputed} done</span>
          )}
          <span>{processes.length} total</span>
        </div>
      </div>

      {/* Grouped process list */}
      <div className="overflow-y-auto max-h-[420px] p-2 space-y-1 small-scrollbar">
        {processes.length === 0 ? (
          <div className="text-center text-muted-foreground py-6 text-sm">
            No processes to display
          </div>
        ) : activeCategories.length <= 1 ? (
          // Single category — no headers needed
          processes.map((process) => (
            <ProcessCard
              key={process.pid}
              process={process}
              isCurrent={process.pid === currentProcessId}
              checkResult={checkResultMap.get(process.metafeature)}
              did={did}
            />
          ))
        ) : (
          // Multiple categories — render with collapsible headers
          activeCategories.map((cat) => {
            const items = grouped[cat]!;
            const collapsed = collapsedCategories.has(cat);
            const completedCount = items.filter((p) => p.status === "computed").length;
            const errorCount = items.filter((p) => p.status === "error").length;

            return (
              <div key={cat}>
                <CategoryHeader
                  label={CATEGORY_LABELS[cat] ?? cat}
                  count={items.length}
                  completedCount={completedCount}
                  errorCount={errorCount}
                  collapsed={collapsed}
                  onToggle={() => toggleCategory(cat)}
                />
                {!collapsed && (
                  <div className="space-y-1 mt-1">
                    {items.map((process) => (
                      <ProcessCard
                        key={process.pid}
                        process={process}
                        isCurrent={process.pid === currentProcessId}
                        checkResult={checkResultMap.get(process.metafeature)}
                        did={did}
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
