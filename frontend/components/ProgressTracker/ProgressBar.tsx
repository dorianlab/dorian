"use client";

import clsx from "clsx";
import { AlertCircleIcon, CheckCircleIcon } from "lucide-react";
import { ProcessPanel } from "./ProgressPanel";
import { SimpleLoader } from "./SimpleLoader";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { usePipelineStore } from "@/store/pipeline";
import { useDatasetStore } from "@/store/dataset";

const QUALITY_METRIC_ORDER = [
  "SyntacticDataAccuracy",
  "SemanticDataAccuracy",
  "RiskOfDatasetInaccuracy",
  "DataAccuracyRange",
  "ValueCompleteness",
  "FeatureCompleteness",
  "RecordCompleteness",
  "LabelCompleteness",
  "ValueOccurrenceCompleteness",
  "DataRecordConsistency",
  "DataLabelConsistency",
  "DataFormatConsistency",
  "SemanticConsistency",
  "DataFormatEfficiency",
  "DataProcessingEfficiency",
  "RiskOfWastedSpace",
  "LabelProportionBalance",
  "LabelDistributionBalance",
  "DataItemCompliance",
  "LabelRichness",
  "RelativeLabelAbundance",
  "CategorySizeDiversity",
  "FeatureEffectiveness",
  "CategorySizeEffectiveness",
  "LabelEffectiveness",
  "PrecisionOfDataValues",
  "FeatureRelevance",
  "RecordRelevance",
  "RepresentativenessRatio",
  "SampleSimilarity",
  "SampleTightness",
  "SampleIndependency",
];

const QUALITY_METRIC_NAMES = new Set(QUALITY_METRIC_ORDER);
const QUALITY_METRIC_INDEX = new Map(
  QUALITY_METRIC_ORDER.map((name, index) => [name, index]),
);

// Inline navbar pill that opens a dropdown list of background processes.
// Renders nothing when no processes have been observed in this session.
function ProcessBar() {
  const progressMap = usePipelineStore((state) => state.progressItems);
  const datasets = useDatasetStore((state) => state.datasets);
  const activeDataset = datasets[datasets.length - 1];
  const activeDid = activeDataset?.did;
  const processes = Object.values(progressMap)
    .filter((p) => !activeDid || p.did === activeDid)
    .sort((a, b) => {
      const aBucket = QUALITY_METRIC_NAMES.has(a.metafeature) ? 1 : 0;
      const bBucket = QUALITY_METRIC_NAMES.has(b.metafeature) ? 1 : 0;
      if (aBucket !== bBucket) return aBucket - bBucket;
      if (aBucket === 1) {
        const aIndex = QUALITY_METRIC_INDEX.get(a.metafeature) ?? Number.MAX_SAFE_INTEGER;
        const bIndex = QUALITY_METRIC_INDEX.get(b.metafeature) ?? Number.MAX_SAFE_INTEGER;
        return aIndex - bIndex;
      }
      return 0;
    });

  const computingProcesses = processes.filter(
    (p) => p.status === "computing" || p.status === "pending",
  );
  const currentProcess = computingProcesses[computingProcesses.length - 1];

  const hasCompletedProcesses = processes.some(
    (p) => p.status !== "computing" && p.status !== "pending",
  );

  const completedCounts = processes.reduce(
    (acc, process) => {
      if (process.status === "computed") acc.success++;
      else if (process.status === "error") acc.error++;
      else if (process.status === "warning") acc.warning++;
      return acc;
    },
    { success: 0, error: 0, warning: 0 },
  );

  const qualityChecks = activeDataset?.quality_checks;
  const qualitySummary = qualityChecks?.summary;
  const failedChecks = qualitySummary?.failed ?? 0;

  if (!currentProcess && !hasCompletedProcesses) return null;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="max-w-[280px] gap-1.5 flex-shrink-0"
        >
          {currentProcess ? (
            <>
              <SimpleLoader
                status={currentProcess.status}
                size="sm"
                className="flex-shrink-0"
              />
              <span className="truncate text-sm font-medium">
                {currentProcess.metafeature}
              </span>
            </>
          ) : (
            <>
              <CheckCircleIcon className="h-3.5 w-3.5 text-green-500 flex-shrink-0" />
              <span className="text-sm font-medium">Processes</span>
            </>
          )}

          {hasCompletedProcesses && (
            <div className="flex items-center gap-1 pl-0.5">
              {completedCounts.success > 0 && (
                <span className="bg-green-100 dark:bg-green-900/30 rounded-full px-1.5 py-0.5 text-xs font-medium text-green-700 dark:text-green-300 flex items-center gap-0.5">
                  <CheckCircleIcon className="h-3 w-3" />
                  {completedCounts.success}
                </span>
              )}
              {completedCounts.error > 0 && (
                <span className="bg-red-100 dark:bg-red-900/30 rounded-full px-1.5 py-0.5 text-xs font-medium text-red-700 dark:text-red-300 flex items-center gap-0.5">
                  <AlertCircleIcon className="h-3 w-3" />
                  {completedCounts.error}
                </span>
              )}
              {failedChecks > 0 && (
                <span className={clsx(
                  "rounded-full px-1.5 py-0.5 text-xs font-medium",
                  "text-red-700 bg-red-100 dark:text-red-300 dark:bg-red-900/30",
                )}>
                  {failedChecks} checks
                </span>
              )}
            </div>
          )}
        </Button>
      </PopoverTrigger>

      <PopoverContent
        className="w-[460px] p-0 overflow-hidden"
        align="center"
        sideOffset={8}
      >
        <ProcessPanel
          processes={processes}
          currentProcessId={currentProcess?.pid}
        />
      </PopoverContent>
    </Popover>
  );
}

export default ProcessBar;
