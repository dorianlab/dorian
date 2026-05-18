"use client";

import { useMemo, useState } from "react";
import clsx from "clsx";
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleAlert,
  Clock3,
  WandSparkles,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ws } from "@/helpers/ws-events";
import { useDatasetStore } from "@/store/dataset";
import type { Dataset } from "@/types/dataset";

type MitigationAction = {
  id: string;
  action: string;
  title: string;
  description: string;
  kind: "dataset";
};

const MISSING_VALUE_ACTIONS: MitigationAction[] = [
  {
    id: "remove-incomplete-rows",
    action: "remove_records_with_missing_values",
    title: "Remove incomplete rows",
    description: "Drop rows that contain at least one missing value.",
    kind: "dataset",
  },
  {
    id: "impute-missing-values",
    action: "impute_missing_values",
    title: "Impute missing values",
    description: "Fill missing values with the most frequent value per column.",
    kind: "dataset",
  },
];

const DATA_RECORD_CONSISTENCY_ACTIONS: MitigationAction[] = [
  {
    id: "remove-duplicate-rows",
    action: "remove_duplicate_records",
    title: "Remove duplicate rows",
    description: "Drop duplicate records from the dataset.",
    kind: "dataset",
  },
];

const LABEL_COMPLETENESS_ACTIONS: MitigationAction[] = [
  {
    id: "remove-missing-label-rows",
    action: "remove_records_with_missing_label",
    title: "Remove rows with missing label",
    description: "Drop rows where the selected target label is missing.",
    kind: "dataset",
  },
];

const DATA_ACCURACY_RANGE_ACTIONS: MitigationAction[] = [
  {
    id: "impute-range-outliers-with-mean",
    action: "impute_range_outliers_with_mean",
    title: "Impute out-of-range values",
    description: "Replace out-of-range numeric values with the in-range mean for each configured column.",
    kind: "dataset",
  },
];

const SYNTACTIC_ACCURACY_ACTIONS: MitigationAction[] = [
  {
    id: "repair-syntactic-values",
    action: "repair_syntactic_values",
    title: "Repair invalid labels",
    description: "Normalize values, apply exact matching, and auto-correct high-confidence fuzzy matches.",
    kind: "dataset",
  },
];

const DATA_ITEM_COMPLIANCE_ACTIONS: MitigationAction[] = [
  {
    id: "enforce-compliance-rules",
    action: "enforce_compliance_rules",
    title: "Enforce compliance rules",
    description: "Clamp or normalize values for supported compliance predicates such as ranges and allowed sets.",
    kind: "dataset",
  },
];

const DATA_FORMAT_CONSISTENCY_ACTIONS: MitigationAction[] = [
  {
    id: "normalize-format-values",
    action: "normalize_format_values",
    title: "Normalize formats",
    description: "Convert values to the expected int, float, bool, str, or datetime format when possible.",
    kind: "dataset",
  },
];

const PRECISION_ACTIONS: MitigationAction[] = [
  {
    id: "round-to-required-precision",
    action: "round_values_to_required_precision",
    title: "Round to required precision",
    description: "Round numeric values to the configured decimal precision for each column.",
    kind: "dataset",
  },
];

const RECORD_RELEVANCE_ACTIONS: MitigationAction[] = [
  {
    id: "remove-irrelevant-records",
    action: "remove_irrelevant_records",
    title: "Remove irrelevant rows",
    description: "Drop rows that do not satisfy the configured record relevance condition.",
    kind: "dataset",
  },
];

function qualityStatusClass(status: string): string {
  switch (status) {
    case "passed":
      return "text-green-700 bg-green-100 dark:text-green-300 dark:bg-green-900/30";
    case "failed":
      return "text-red-700 bg-red-100 dark:text-red-300 dark:bg-red-900/30";
    case "pending":
      return "text-amber-700 bg-amber-100 dark:text-amber-300 dark:bg-amber-900/30";
    case "error":
      return "text-red-700 bg-red-100 dark:text-red-300 dark:bg-red-900/30";
    default:
      return "text-muted-foreground bg-muted";
  }
}

function mitigationActionsForCheck(check: string): MitigationAction[] {
  if (check === "SyntacticDataAccuracy") {
    return SYNTACTIC_ACCURACY_ACTIONS;
  }
  if (check === "DataRecordConsistency") {
    return DATA_RECORD_CONSISTENCY_ACTIONS;
  }
  if (check === "LabelCompleteness") {
    return LABEL_COMPLETENESS_ACTIONS;
  }
  if (check === "DataAccuracyRange") {
    return DATA_ACCURACY_RANGE_ACTIONS;
  }
  if (check === "DataItemCompliance") {
    return DATA_ITEM_COMPLIANCE_ACTIONS;
  }
  if (check === "DataFormatConsistency") {
    return DATA_FORMAT_CONSISTENCY_ACTIONS;
  }
  if (check === "PrecisionOfDataValues") {
    return PRECISION_ACTIONS;
  }
  if (check === "RecordRelevance") {
    return RECORD_RELEVANCE_ACTIONS;
  }
  if (
    check === "RecordCompleteness" ||
    check.startsWith("ValueCompleteness:") ||
    check.startsWith("FeatureCompleteness:")
  ) {
    return MISSING_VALUE_ACTIONS;
  }
  return [];
}

function fallbackSuggestion(check: string, status: string): string {
  if (status === "passed") {
    return "No mitigation needed.";
  }
  if (status === "pending") {
    return "Provide the required rule input and rerun the checks.";
  }
  if (status === "error") {
    return "Review the metric inputs or dataset values before rerunning this check.";
  }
  if (check.startsWith("SyntacticDataAccuracy")) {
    return "Use normalization, exact-match validation, fuzzy matching, or human review for invalid values.";
  }
  if (check.startsWith("SemanticDataAccuracy")) {
    return "Provide semantic rules, then rerun the checks.";
  }
  if (check.startsWith("RiskOfDatasetInaccuracy")) {
    return "Validate outliers, correct data-entry issues, cap extremes, impute, or remove corrupted rows.";
  }
  if (check.startsWith("DataAccuracyRange")) {
    return "Validate exceptions, correct values, clamp ranges, normalize units, or apply cross-field checks.";
  }
  if (check.startsWith("LabelCompleteness")) {
    return "Predict missing labels with a model or route uncertain cases for manual labeling.";
  }
  if (check.startsWith("ValueOccurrenceCompleteness")) {
    return "Adjust under- or over-represented values with imputation, duplication, removal, or downsampling.";
  }
  if (check.startsWith("DataFormatEfficiency")) {
    return "Standardize formats and normalize value representations across the dataset.";
  }
  if (check.startsWith("DataProcessingEfficiency")) {
    return "Review preprocessing bottlenecks and simplify expensive data-cleaning steps.";
  }
  if (check.startsWith("SampleSimilarity")) {
    return "Review highly similar records and consider deduplication or diversity balancing.";
  }
  if (check.startsWith("SampleTightness")) {
    return "Inspect clustered samples and evaluate whether additional variation is needed.";
  }
  if (check.startsWith("SampleIndependency")) {
    return "Review sample dependency and remove leakage or repeated observations where necessary.";
  }
  return "Review this check and apply a domain-specific correction or manual validation step.";
}

function EmptyState() {
  return (
    <div className="px-4 py-6 text-center text-sm text-muted-foreground">
      No data quality checks to display
    </div>
  );
}

function ChecksList({ dataset }: { dataset?: Dataset }) {
  const checks = dataset?.quality_checks;
  const results = checks?.results ?? [];
  const summary = checks?.summary;
  const did = dataset?.did;
  const [expandedCheck, setExpandedCheck] = useState<string | null>(null);

  if (!dataset?.did || results.length === 0) {
    return <EmptyState />;
  }

  return (
    <div className="flex flex-col">
      <div className="border-b px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold">Data Quality Checks</h2>
          </div>
          {summary && (
            <div className="flex items-center gap-1 text-[10px]">
              <span className="rounded px-1.5 py-0.5 bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300">
                {summary.passed} passed
              </span>
              <span className="rounded px-1.5 py-0.5 bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300">
                {summary.failed} failed
              </span>
              <span className="rounded px-1.5 py-0.5 bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
                {summary.pending} pending
              </span>
            </div>
          )}
        </div>
      </div>

      <div className="max-h-[420px] space-y-2 overflow-y-auto p-2 small-scrollbar">
        {results.map((result) => {
          const isExpanded = expandedCheck === result.check;
          const actions = mitigationActionsForCheck(result.check);
          const canMitigate = result.status === "failed" && actions.length > 0 && did;

          return (
            <div
              key={result.check}
              className="rounded-md border border-transparent bg-muted/30"
            >
              <button
                type="button"
                onClick={() => setExpandedCheck(isExpanded ? null : result.check)}
                className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm"
              >
                {result.status === "passed" && <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />}
                {result.status === "failed" && <XCircle className="h-3.5 w-3.5 text-rose-600" />}
                {result.status === "pending" && <Clock3 className="h-3.5 w-3.5 text-amber-600" />}
                {result.status === "error" && <CircleAlert className="h-3.5 w-3.5 text-slate-600" />}
                <span className="min-w-0 flex-1 break-words font-medium">{result.check}</span>
                <span
                  className={clsx(
                    "rounded px-1.5 py-0.5 text-[10px] font-medium capitalize",
                    qualityStatusClass(result.status),
                  )}
                >
                  {result.status}
                </span>
                {isExpanded ? (
                  <ChevronUp className="h-3.5 w-3.5 text-muted-foreground" />
                ) : (
                  <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
                )}
              </button>

              {isExpanded && (
                <div className="space-y-3 border-t px-3 py-3 text-xs">
                  {canMitigate ? (
                    <div className="space-y-2">
                      <div className="flex items-center gap-1.5 font-medium">
                        <WandSparkles className="h-3.5 w-3.5 text-slate-600" />
                        Mitigation actions
                      </div>
                      {actions.map((action) => {
                        return (
                          <div key={action.action} className="space-y-2 rounded border bg-background px-3 py-2">
                            <div className="min-w-0">
                              <div className="font-medium">{action.title}</div>
                              <div className="text-muted-foreground">{action.description}</div>
                            </div>
                            <div className="flex items-center gap-2">
                              <Button
                                size="sm"
                                variant="default"
                                className="h-7"
                                onClick={() => ws.dataMitigationDecision({
                                  did,
                                  check: result.check,
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
                                  check: result.check,
                                  mitigation_action: action,
                                  decision: "reject",
                                })}
                              >
                                Ignore
                              </Button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="text-xs text-muted-foreground">
                      {fallbackSuggestion(result.check, result.status)}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function QualityChecksPanel() {
  const datasets = useDatasetStore((state) => state.datasets);
  const activeDataset = useMemo(() => datasets[datasets.length - 1], [datasets]);

  return <ChecksList dataset={activeDataset} />;
}
