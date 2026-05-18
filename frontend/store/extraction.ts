import { create } from "zustand";
import type { PipelineDraft } from "@/types/pipeline";

export interface RuleSuggestion {
  ruleId: string;
  description: string;
  spec: Record<string, unknown>;
  valid: boolean;
  errors: string[];
  warnings: string[];
  /** True when applying this rule improves GED against the target but
   *  doesn't close the gap fully. The card renders "Partial accept" and
   *  the continuation triggers another suggest round from the improved
   *  baseline. */
  isPartial?: boolean;
  /** DAG produced by applying the rule to auto_dag — the new baseline
   *  for the follow-up suggest when the user accepts the partial. */
  intermediateDag?: Record<string, unknown>;
  gedBefore?: number | null;
  gedAfter?: number | null;
}

export interface CompatRegression {
  extraction_id: string;
  diff_summary: string;
  missing_ops: string[];
  extra_ops: string[];
}

export interface CompatRegressionReport {
  status: "blocked";
  reason: "backward_compat";
  rulesHash: string;
  regressions: CompatRegression[];
  corpus_size: number;
  checked: number;
  elapsed_ms: number;
  capped: boolean;
}

export interface ExtractionState {
  /** Whether the extraction split view is active. */
  active: boolean;
  /** The Python source code being edited. */
  code: string;
  /** The language (always "python" for now). */
  language: string;
  /** The filename of the uploaded .py file (for display). */
  filename: string;
  /** The extracted pipeline draft (null until first extraction). */
  extractedPipeline: PipelineDraft | null;
  /** User-edited version of the pipeline (null until first canvas edit). */
  editedPipeline: PipelineDraft | null;
  /** Whether an extraction request is in flight. */
  isExtracting: boolean;
  /** Error message from the last extraction attempt. */
  extractionError: string | null;
  /** Full Python traceback from the last failed extraction. */
  extractionTrace: string | null;
  /** Backend extraction ID (for linking corrections). */
  extractionId: string | null;
  /** Content hash of the rule set used for this extraction. */
  rulesVersion: string | null;
  /** Current extraction rules editor content (loaded via WS). */
  rulesContent: string;
  /** Whether the rules came from the user or the default. */
  rulesSource: "user" | "default";
  /**
   * Canonical format of ``rulesContent``. ``json_specs`` → the card UI
   * renders the spec array; ``python_rules`` → legacy Monaco fallback.
   */
  rulesFormat: "json_specs" | "python_rules";
  /** True once the backend has responded to ``loadExtractionRules``.
   *  Consumers gate their loading spinners on this instead of on the
   *  truthiness of ``rulesContent`` (empty string is a valid response
   *  for "no rules yet," not a not-loaded sentinel). */
  rulesLoaded: boolean;
  /** Whether an LLM rule suggestion request is in flight. */
  isSuggestingRules: boolean;
  /** True when the user cancelled a pending suggestion — discard next result. */
  isSuggestionCancelled: boolean;
  /** The active suggestion batch (null until first suggestion arrives). */
  ruleSuggestions: RuleSuggestion[] | null;
  /** The suggestion batch ID returned by the backend. */
  suggestionId: string | null;
  /** Which suggestion card is currently expanded (null = all collapsed). */
  expandedRuleId: string | null;
  /**
   * Backend-reported backward-compat regressions for the last rules save
   * attempt. Non-null = a save was blocked; the ExtractionView renders
   * a diff modal with three actions: abandon, edit, or override (retry
   * with skipCompatCheck=true).
   */
  compatRegressionReport: CompatRegressionReport | null;

  startExtraction: (code: string, filename: string) => void;
  setCode: (code: string) => void;
  setExtractedPipeline: (pipeline: PipelineDraft | null) => void;
  setEditedPipeline: (pipeline: PipelineDraft | null) => void;
  setIsExtracting: (v: boolean) => void;
  setExtractionError: (err: string | null, trace?: string | null) => void;
  setExtractionMeta: (id: string, version: string) => void;
  setRulesContent: (
    content: string,
    source: "user" | "default",
    format?: "json_specs" | "python_rules",
  ) => void;
  setIsSuggestingRules: (v: boolean) => void;
  /** Called when LLM result arrives — silently discards if user already cancelled. */
  setRuleSuggestions: (suggestionId: string, rules: RuleSuggestion[]) => void;
  /** Cancel the in-flight suggestion request; next arriving result is discarded. */
  cancelSuggestion: () => void;
  dismissRuleSuggestion: (ruleId: string) => void;
  clearRuleSuggestions: () => void;
  setExpandedRule: (id: string | null) => void;
  setCompatRegressionReport: (report: CompatRegressionReport | null) => void;
  reset: () => void;
}

const _initial = {
  active: false,
  code: "",
  language: "python",
  filename: "",
  extractedPipeline: null,
  editedPipeline: null,
  isExtracting: false,
  extractionError: null,
  extractionTrace: null,
  extractionId: null,
  rulesVersion: null,
  rulesContent: "",
  rulesSource: "default",
  rulesFormat: "python_rules",
  rulesLoaded: false,
  isSuggestingRules: false,
  isSuggestionCancelled: false,
  ruleSuggestions: null,
  suggestionId: null,
  expandedRuleId: null,
  compatRegressionReport: null,
} satisfies Omit<
  ExtractionState,
  | "startExtraction"
  | "setCode"
  | "setExtractedPipeline"
  | "setEditedPipeline"
  | "setIsExtracting"
  | "setExtractionError"
  | "setExtractionMeta"
  | "setRulesContent"
  | "setIsSuggestingRules"
  | "setRuleSuggestions"
  | "cancelSuggestion"
  | "dismissRuleSuggestion"
  | "clearRuleSuggestions"
  | "setExpandedRule"
  | "setCompatRegressionReport"
  | "reset"
>;

export const useExtractionStore = create<ExtractionState>((set) => ({
  ..._initial,

  startExtraction: (code, filename) =>
    set({
      active: true,
      code,
      filename,
      language: "python",
      extractedPipeline: null,
      editedPipeline: null,
      extractionError: null,
      extractionTrace: null,
      isExtracting: false,
    }),

  setCode: (code) => set({ code }),

  setExtractedPipeline: (pipeline) =>
    set({
      extractedPipeline: pipeline,
      editedPipeline: null,
      isExtracting: false,
      extractionError: null,
      extractionTrace: null,
    }),

  setEditedPipeline: (pipeline) => set({ editedPipeline: pipeline }),

  setIsExtracting: (v) => set({ isExtracting: v }),

  setExtractionError: (err, trace) =>
    set({ extractionError: err, extractionTrace: trace ?? null, isExtracting: false }),

  setExtractionMeta: (id, version) =>
    set({ extractionId: id, rulesVersion: version }),

  setRulesContent: (content, source, format) =>
    set({
      rulesContent: content,
      rulesSource: source,
      rulesFormat: format ?? "python_rules",
      rulesLoaded: true,
    }),

  setIsSuggestingRules: (v) => set({ isSuggestingRules: v }),

  // If the user already cancelled, reset the flag and silently drop the result.
  setRuleSuggestions: (suggestionId, rules) =>
    set((s) =>
      s.isSuggestionCancelled
        ? { isSuggestionCancelled: false, isSuggestingRules: false }
        : { suggestionId, ruleSuggestions: rules, isSuggestingRules: false },
    ),

  cancelSuggestion: () =>
    set({ isSuggestingRules: false, isSuggestionCancelled: true }),

  dismissRuleSuggestion: (ruleId) =>
    set((s) => ({
      ruleSuggestions: (s.ruleSuggestions ?? []).filter((r) => r.ruleId !== ruleId),
    })),

  clearRuleSuggestions: () =>
    set({ ruleSuggestions: null, suggestionId: null }),

  setExpandedRule: (id) => set({ expandedRuleId: id }),

  setCompatRegressionReport: (report) => set({ compatRegressionReport: report }),

  reset: () => set({ ..._initial }),
}));
