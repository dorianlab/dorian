// ---------------------------------------------------------------------------
// Quality input questions builder (for "Edit Inputs" button)
// ---------------------------------------------------------------------------

import type { Question } from "@/types/ui";

export function buildQualityInputQuestions(dataset: any): Question[] {
  const did = dataset?.did;
  if (!did) return [];

  const qualityInputs = dataset?.quality_inputs ?? {};
  const allColumns = Array.isArray(dataset?.columns) ? dataset.columns : [];
  const featureOptions = Array.isArray(dataset?.features) && dataset.features.length > 0
    ? dataset.features
    : allColumns;

  return [
    {
      id: `dataset:${did}:feature_columns`,
      type: "multi-select",
      question: "Which columns should be used as features?",
      options: allColumns,
      initialValue: Array.isArray(dataset?.features) ? dataset.features : [],
    },
    {
      id: `dataset:${did}:target_columns`,
      type: "select",
      question: "Which column should be used as the target label?",
      options: allColumns,
      initialValue: dataset?.target ? String(dataset.target) : "",
    },
    {
      id: `dataset:${did}:quality_threshold_mode`,
      type: "select",
      question: "Use the default global quality threshold, or override it for this dataset?",
      options: ["accept_default", "override"],
      initialValue: String(qualityInputs.quality_threshold_mode || "accept_default"),
    },
    {
      id: `dataset:${did}:quality_threshold_override`,
      type: "text",
      question: "If overriding, enter one threshold between 0 and 1 for all quality checks. Leave blank to keep the default.",
      initialValue:
        qualityInputs.quality_threshold_override == null
          ? ""
          : String(qualityInputs.quality_threshold_override),
    },
    {
      id: `dataset:${did}:syntactic_allowed_values`,
      type: "text",
      multiline: true,
      question:
        'Syntactic data accuracy. Optional JSON object mapping column to allowed values. Example: {"person_gender":["male","female"]}',
      initialValue: JSON.stringify(qualityInputs.syntactic_allowed_values ?? {}, null, 2),
    },
    {
      id: `dataset:${did}:semantic_accuracy_rules`,
      type: "text",
      multiline: true,
      question:
        'Semantic data accuracy. Optional JSON list of conditional rules. Example: [{"condition":{"operator":"AND","clauses":[{"column":"country","value":"DE"}]},"target_column":"currency","valid_values":["EUR"]}]',
      initialValue: JSON.stringify(qualityInputs.semantic_accuracy_rules ?? [], null, 2),
    },
    {
      id: `dataset:${did}:inaccuracy_columns`,
      type: "multi-select",
      question: "Risk of dataset inaccuracy. Select columns that should be checked for outliers/inaccuracy.",
      options: featureOptions,
      initialValue: Array.isArray(qualityInputs.inaccuracy_columns) ? qualityInputs.inaccuracy_columns : [],
    },
    {
      id: `dataset:${did}:range_rules`,
      type: "text",
      multiline: true,
      question:
        'Data accuracy range. Optional JSON object mapping column to [min, max]. Example: {"person_age":[18,100],"credit_score":[300,850]}',
      initialValue: JSON.stringify(qualityInputs.range_rules ?? {}, null, 2),
    },
    {
      id: `dataset:${did}:value_occurrence_expectations`,
      type: "text",
      multiline: true,
      question:
        'Value occurrence completeness. Optional JSON list of [column, value, expected_count]. Example: [["loan_status",1,1200]]',
      initialValue: JSON.stringify(qualityInputs.value_occurrence_expectations ?? [], null, 2),
    },
    {
      id: `dataset:${did}:sensitive_columns`,
      type: "multi-select",
      question: "Select any columns containing sensitive data that should be excluded from LLM-based mitigation.",
      options: allColumns,
      initialValue: Array.isArray(qualityInputs.sensitive_columns) ? qualityInputs.sensitive_columns : [],
    },
    {
      id: `dataset:${did}:category_column`,
      type: "select",
      question: "Select a category column for balance/diversity metrics.",
      options: allColumns,
      initialValue: typeof qualityInputs.category_column === "string" ? qualityInputs.category_column : "",
    },
    {
      id: `dataset:${did}:balance_target_labels`,
      type: "text",
      multiline: true,
      question:
        'Optional JSON list of label values for balance/diversity metrics. Leave blank to use the target column values. Example: [0,1]',
      initialValue: JSON.stringify(qualityInputs.balance_target_labels ?? [], null, 2),
    },
    {
      id: `dataset:${did}:compliance_rules`,
      type: "text",
      multiline: true,
      question:
        'Optional JSON object of compliance rules by column. Example: {"person_age":{"op":"between","value":[18,100]},"loan_status":{"op":"in","value":[0,1]}}',
      initialValue: JSON.stringify(qualityInputs.compliance_rules ?? {}, null, 2),
    },
    {
      id: `dataset:${did}:consistency_label_threshold`,
      type: "text",
      question:
        "Optional clustering threshold for data label consistency. Leave blank to use 0.5.",
      initialValue:
        qualityInputs.consistency_label_threshold == null
          ? ""
          : String(qualityInputs.consistency_label_threshold),
    },
    {
      id: `dataset:${did}:format_schema`,
      type: "text",
      multiline: true,
      question:
        'Optional JSON object mapping column to expected type for format consistency. Types: int, float, str, bool, datetime. Example: {"person_age":"int","loan_int_rate":"float"}',
      initialValue: JSON.stringify(qualityInputs.format_schema ?? {}, null, 2),
    },
    {
      id: `dataset:${did}:semantic_consistency_rules`,
      type: "text",
      multiline: true,
      question:
        'Optional JSON list of row-level consistency rules. Example: [{"operator":"AND","clauses":[{"column":"loan_status","op":"in","value":[0,1]}]}]',
      initialValue: JSON.stringify(qualityInputs.semantic_consistency_rules ?? [], null, 2),
    },
    {
      id: `dataset:${did}:feature_effectiveness_rules`,
      type: "text",
      multiline: true,
      question:
        'Optional JSON object mapping feature to predicate list for feature effectiveness. Example: {"person_age":[{"op":"between","value":[18,100]}]}',
      initialValue: JSON.stringify(qualityInputs.feature_effectiveness_rules ?? {}, null, 2),
    },
    {
      id: `dataset:${did}:category_size_threshold`,
      type: "text",
      question:
        "Optional numeric threshold for category size effectiveness. Leave blank to skip.",
      initialValue:
        qualityInputs.category_size_threshold == null
          ? ""
          : String(qualityInputs.category_size_threshold),
    },
    {
      id: `dataset:${did}:label_effectiveness_rules`,
      type: "text",
      multiline: true,
      question:
        'Optional JSON list of predicate rules for label effectiveness. Example: [{"op":"in","value":[0,1]}]',
      initialValue: JSON.stringify(qualityInputs.label_effectiveness_rules ?? [], null, 2),
    },
    {
      id: `dataset:${did}:target_size`,
      type: "text",
      question:
        "Optional target size in bytes for risk of wasted space. Leave blank to skip.",
      initialValue:
        qualityInputs.target_size == null
          ? ""
          : String(qualityInputs.target_size),
    },
    {
      id: `dataset:${did}:precision_requirements`,
      type: "text",
      multiline: true,
      question:
        'Optional JSON object mapping numeric column to required decimal places. Example: {"loan_int_rate":2}',
      initialValue: JSON.stringify(qualityInputs.precision_requirements ?? {}, null, 2),
    },
    {
      id: `dataset:${did}:relevant_features`,
      type: "multi-select",
      question: "Select relevant features for feature relevance.",
      options: featureOptions,
      initialValue: Array.isArray(qualityInputs.relevant_features) ? qualityInputs.relevant_features : [],
    },
    {
      id: `dataset:${did}:record_relevance_condition`,
      type: "text",
      multiline: true,
      question:
        'Optional JSON row condition for record relevance. Example: {"operator":"AND","clauses":[{"column":"loan_status","op":"eq","value":1}]}',
      initialValue: JSON.stringify(qualityInputs.record_relevance_condition ?? {}, null, 2),
    },
    {
      id: `dataset:${did}:required_attributes`,
      type: "multi-select",
      question: "Select required attributes for representativeness ratio.",
      options: allColumns,
      initialValue: Array.isArray(qualityInputs.required_attributes) ? qualityInputs.required_attributes : [],
    },
  ];
}
