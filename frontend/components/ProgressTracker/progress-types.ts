// ---------------------------------------------------------------------------
// Shared types and constants for ProgressTracker components
// ---------------------------------------------------------------------------

export type MitigationAction = {
  id: string;
  action: string;
  title: string;
  description: string;
  kind: "dataset";
};

// ---------------------------------------------------------------------------
// Quality check mitigation actions
// ---------------------------------------------------------------------------

export const CHECK_MITIGATIONS: Record<string, MitigationAction[]> = {
  SyntacticDataAccuracy: [{
    id: "repair-syntactic-values", action: "repair_syntactic_values",
    title: "Repair invalid labels",
    description: "Normalize values, apply exact matching, and auto-correct high-confidence fuzzy matches.",
    kind: "dataset",
  }],
  DataRecordConsistency: [{
    id: "remove-duplicate-rows", action: "remove_duplicate_records",
    title: "Remove duplicate rows",
    description: "Drop duplicate records from the dataset.",
    kind: "dataset",
  }],
  LabelCompleteness: [{
    id: "remove-missing-label-rows", action: "remove_records_with_missing_label",
    title: "Remove rows with missing label",
    description: "Drop rows where the selected target label is missing.",
    kind: "dataset",
  }],
  DataAccuracyRange: [{
    id: "impute-range-outliers-with-mean", action: "impute_range_outliers_with_mean",
    title: "Impute out-of-range values",
    description: "Replace out-of-range numeric values with the in-range mean for each configured column.",
    kind: "dataset",
  }],
  DataItemCompliance: [{
    id: "enforce-compliance-rules", action: "enforce_compliance_rules",
    title: "Enforce compliance rules",
    description: "Clamp or normalize values for supported compliance predicates such as ranges and allowed sets.",
    kind: "dataset",
  }],
  DataFormatConsistency: [{
    id: "normalize-format-values", action: "normalize_format_values",
    title: "Normalize formats",
    description: "Convert values to the expected int, float, bool, str, or datetime format when possible.",
    kind: "dataset",
  }],
  PrecisionOfDataValues: [{
    id: "round-to-required-precision", action: "round_values_to_required_precision",
    title: "Round to required precision",
    description: "Round numeric values to the configured decimal precision for each column.",
    kind: "dataset",
  }],
  RecordRelevance: [{
    id: "remove-irrelevant-records", action: "remove_irrelevant_records",
    title: "Remove irrelevant rows",
    description: "Drop rows that do not satisfy the configured record relevance condition.",
    kind: "dataset",
  }],
};

/** Missing value actions used for RecordCompleteness, ValueCompleteness, FeatureCompleteness */
export const MISSING_VALUE_ACTIONS: MitigationAction[] = [
  {
    id: "remove-incomplete-rows", action: "remove_records_with_missing_values",
    title: "Remove incomplete rows",
    description: "Drop rows that contain at least one missing value.",
    kind: "dataset",
  },
  {
    id: "impute-missing-values", action: "impute_missing_values",
    title: "Impute missing values",
    description: "Fill missing values with the most frequent value per column.",
    kind: "dataset",
  },
];

export function mitigationActionsForCheck(check: string): MitigationAction[] {
  if (CHECK_MITIGATIONS[check]) return CHECK_MITIGATIONS[check];
  if (
    check === "RecordCompleteness" ||
    check.startsWith("ValueCompleteness:") ||
    check.startsWith("FeatureCompleteness:")
  ) return MISSING_VALUE_ACTIONS;
  return [];
}

// ---------------------------------------------------------------------------
// Category config
// ---------------------------------------------------------------------------

export const CATEGORY_LABELS: Record<string, string> = {
  data_profiling: "Data Profiling",
  data_quality: "Data Quality",
  quality_checks: "Quality Checks",
  pipeline_execution: "Pipeline Execution",
};

export const CATEGORY_ORDER: string[] = [
  "data_profiling",
  "data_quality",
  "quality_checks",
  "pipeline_execution",
];
