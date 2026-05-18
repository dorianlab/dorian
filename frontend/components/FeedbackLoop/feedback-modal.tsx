import FeedbackLoop from "./feedback-loop";
import { useUIStore } from "@/store/ui";
import { usePipelineStore } from "@/store/pipeline";
import { emitEvent, ws } from "@/helpers/ws-events";
import { useSessionStore } from "@/store/session";
import { useDatasetStore } from "@/store/dataset";

type FeedbackModalProps = {
  isOpen: boolean;
  onClose: () => void;
};

export default function FeedbackModal({ isOpen, onClose }: FeedbackModalProps) {
  const {
    queries,
    removeQueries,
    setSelectedTask,
    setSelectedEval,
    selectedTask,
    selectedEval,
  } = useUIStore();
  const { tasks, evals, activeSessionId } = useSessionStore();
  const { datasets, updateDataset } = useDatasetStore();
  if (!isOpen) return null;

  const activeDataset = datasets[datasets.length - 1];

  const hasDatasetSetupQuestions = queries.some((q) => q.id.startsWith("dataset:"));
  const queryIds = new Set(queries.map((q) => q.id));
  const augmentedQueries = [...queries];

  // Only ask about the task if it hasn't been auto-detected already.
  // When the backend auto-detects the task from the dataset profile, it
  // sends a state/selected-task WS event that sets selectedTask in the
  // UI store — no need to ask the user again.
  if (
    hasDatasetSetupQuestions &&
    !selectedTask &&
    !queryIds.has("session:synthetic:task_selection") &&
    !queries.some((q) => q.id.endsWith(":task_selection")) &&
    tasks.length > 0
  ) {
    augmentedQueries.push({
      id: "session:synthetic:task_selection",
      type: "select",
      question: "What data science task would you like to perform?",
      options: tasks.map((t) => t.name),
      initialValue: "",
    });
  }

  // Same for evaluation procedure — skip if already auto-selected.
  if (
    hasDatasetSetupQuestions &&
    !selectedEval &&
    !queryIds.has("session:synthetic:eval_selection") &&
    !queries.some((q) => q.id.endsWith(":eval_selection")) &&
    evals.length > 0
  ) {
    augmentedQueries.push({
      id: "session:synthetic:eval_selection",
      type: "select",
      question: "Which evaluation procedure should be used?",
      options: evals.map((e) => e.name),
      initialValue: "",
    });
  }

  const orderedQuestions = [...augmentedQueries].sort((a, b) => {
    const orderFor = (id: string) => {
      if (id.endsWith(":feature_columns")) return 0;
      if (id.endsWith(":target_columns")) return 1;
      if (id.endsWith(":quality_threshold_mode")) return 2;
      if (id.endsWith(":quality_threshold_override")) return 3;
      if (id.endsWith(":syntactic_allowed_values")) return 4;
      if (id.endsWith(":semantic_accuracy_rules")) return 5;
      if (id.endsWith(":inaccuracy_columns")) return 6;
      if (id.endsWith(":range_rules")) return 7;
      if (id.endsWith(":value_occurrence_expectations")) return 8;
      if (id.endsWith(":sensitive_columns")) return 9;
      if (id.endsWith(":category_column")) return 10;
      if (id.endsWith(":balance_target_labels")) return 11;
      if (id.endsWith(":compliance_rules")) return 12;
      if (id.endsWith(":consistency_label_threshold")) return 13;
      if (id.endsWith(":format_schema")) return 14;
      if (id.endsWith(":semantic_consistency_rules")) return 15;
      if (id.endsWith(":feature_effectiveness_rules")) return 16;
      if (id.endsWith(":category_size_threshold")) return 17;
      if (id.endsWith(":label_effectiveness_rules")) return 18;
      if (id.endsWith(":target_size")) return 19;
      if (id.endsWith(":precision_requirements")) return 20;
      if (id.endsWith(":relevant_features")) return 21;
      if (id.endsWith(":record_relevance_condition")) return 22;
      if (id.endsWith(":required_attributes")) return 23;
      if (id.endsWith(":task_selection")) return 24;
      if (id.endsWith(":eval_selection")) return 25;
      return 100;
    };
    return orderFor(a.id) - orderFor(b.id);
  });

  /**
   * Convert column-table cell map {"col:field_key": value} into a
   * per-column object {"col": value} suitable for quality_inputs.
   * When a question has multiple fields, produces {"col": {field1: v1, field2: v2}}.
   */
  const flattenColumnTable = (
    raw: unknown,
    singleFieldKey?: string,
  ): Record<string, unknown> => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    const map = raw as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const [compositeKey, value] of Object.entries(map)) {
      if (value == null) continue;
      const sepIdx = compositeKey.lastIndexOf(":");
      if (sepIdx === -1) continue;
      const col = compositeKey.slice(0, sepIdx);
      const fieldKey = compositeKey.slice(sepIdx + 1);
      if (singleFieldKey && fieldKey === singleFieldKey) {
        out[col] = value;
      } else {
        const prev = (out[col] ?? {}) as Record<string, unknown>;
        prev[fieldKey] = value;
        out[col] = prev;
      }
    }
    return out;
  };

  const onSubmit = (answers: Record<string, string | string[] | Record<string, unknown>>) => {
    const normalizedAnswers = { ...answers };
    const hasSyntheticTask = orderedQuestions.some(
      (q) => q.id === "session:synthetic:task_selection",
    );
    const hasSyntheticEval = orderedQuestions.some(
      (q) => q.id === "session:synthetic:eval_selection",
    );

    if (activeSessionId && hasSyntheticTask) {
      const selectedTask =
        normalizedAnswers["session:synthetic:task_selection"] || "__skip__";
      normalizedAnswers[`session:${activeSessionId}:task_selection`] =
        String(selectedTask);
      delete normalizedAnswers["session:synthetic:task_selection"];
    }
    if (activeSessionId && hasSyntheticEval) {
      const selectedEval =
        normalizedAnswers["session:synthetic:eval_selection"] || "__skip__";
      normalizedAnswers[`session:${activeSessionId}:eval_selection`] =
        String(selectedEval);
      delete normalizedAnswers["session:synthetic:eval_selection"];
    }
    // Serialize column-table answers (Record<string, unknown>) to JSON
    // strings so the backend can write them to Redis consistently.
    for (const [key, val] of Object.entries(normalizedAnswers)) {
      if (val && typeof val === "object" && !Array.isArray(val)) {
        (normalizedAnswers as Record<string, unknown>)[key] = JSON.stringify(val);
      }
    }

    // Include the current pipeline ID so feedback can be correlated with a
    // specific pipeline state for replay.  uid/session/ts/requestId are
    // injected automatically by emitEvent.
    const pipelineId = usePipelineStore.getState().tempPipeline?.uuid ?? null;
    emitEvent("FeedbackReceived", {
      answers: normalizedAnswers,
      pipelineId,
      view: "feedback-modal",
    });

    if (activeDataset?.did) {
      const did = activeDataset.did;
      const qualityInputs = {
        ...(activeDataset.quality_inputs ?? {}),
      } as Record<string, unknown>;

      const parseJsonOrFallback = (
        raw: unknown,
        fallback: unknown,
      ) => {
        if (typeof raw !== "string") return fallback;
        const trimmed = raw.trim();
        if (!trimmed) return fallback;
        try {
          return JSON.parse(trimmed);
        } catch {
          return fallback;
        }
      };

      const featureColumns = normalizedAnswers[`dataset:${did}:feature_columns`];
      if (Array.isArray(featureColumns)) {
        updateDataset(activeDataset.uuid, "features", featureColumns);
      }

      const targetColumn = normalizedAnswers[`dataset:${did}:target_columns`];
      if (typeof targetColumn === "string") {
        updateDataset(activeDataset.uuid, "target", targetColumn);
      }

      const thresholdMode = normalizedAnswers[`dataset:${did}:quality_threshold_mode`];
      if (typeof thresholdMode === "string") {
        qualityInputs.quality_threshold_mode = thresholdMode;
      }

      const thresholdOverride = normalizedAnswers[`dataset:${did}:quality_threshold_override`];
      if (typeof thresholdOverride === "string") {
        const trimmed = thresholdOverride.trim();
        qualityInputs.quality_threshold_override = trimmed === "" ? null : trimmed;
      }

      // ── Column-table questions (structured answers) ──────────────
      const syntacticRaw = normalizedAnswers[`dataset:${did}:syntactic_allowed_values`];
      qualityInputs.syntactic_allowed_values =
        typeof syntacticRaw === "object" && !Array.isArray(syntacticRaw)
          ? flattenColumnTable(syntacticRaw, "allowed_values")
          : parseJsonOrFallback(syntacticRaw, qualityInputs.syntactic_allowed_values ?? {});

      const rangeRaw = normalizedAnswers[`dataset:${did}:range_rules`];
      qualityInputs.range_rules =
        typeof rangeRaw === "object" && !Array.isArray(rangeRaw)
          ? flattenColumnTable(rangeRaw, "range")
          : parseJsonOrFallback(rangeRaw, qualityInputs.range_rules ?? {});

      const formatRaw = normalizedAnswers[`dataset:${did}:format_schema`];
      qualityInputs.format_schema =
        typeof formatRaw === "object" && !Array.isArray(formatRaw)
          ? flattenColumnTable(formatRaw, "expected_type")
          : parseJsonOrFallback(formatRaw, qualityInputs.format_schema ?? {});

      const precisionRaw = normalizedAnswers[`dataset:${did}:precision_requirements`];
      qualityInputs.precision_requirements =
        typeof precisionRaw === "object" && !Array.isArray(precisionRaw)
          ? flattenColumnTable(precisionRaw, "decimals")
          : parseJsonOrFallback(precisionRaw, qualityInputs.precision_requirements ?? {});

      const complianceRaw = normalizedAnswers[`dataset:${did}:compliance_rules`];
      qualityInputs.compliance_rules =
        typeof complianceRaw === "object" && !Array.isArray(complianceRaw)
          ? flattenColumnTable(complianceRaw, "rule")
          : parseJsonOrFallback(complianceRaw, qualityInputs.compliance_rules ?? {});

      // ── Remaining text/select questions (unchanged formats) ───────
      qualityInputs.semantic_accuracy_rules = parseJsonOrFallback(
        normalizedAnswers[`dataset:${did}:semantic_accuracy_rules`],
        qualityInputs.semantic_accuracy_rules ?? [],
      );
      qualityInputs.inaccuracy_columns = Array.isArray(
        normalizedAnswers[`dataset:${did}:inaccuracy_columns`],
      )
        ? normalizedAnswers[`dataset:${did}:inaccuracy_columns`]
        : (qualityInputs.inaccuracy_columns ?? []);
      // value_occurrence_expectations: now a column-table with expected_value + expected_count
      const vocRaw = normalizedAnswers[`dataset:${did}:value_occurrence_expectations`];
      if (typeof vocRaw === "object" && !Array.isArray(vocRaw)) {
        // Convert {"col:expected_value": v, "col:expected_count": n} → [[col, v, n], ...]
        const flat = vocRaw as Record<string, unknown>;
        const vocList: [string, unknown, number][] = [];
        const seen = new Set<string>();
        for (const key of Object.keys(flat)) {
          const sep = key.lastIndexOf(":");
          if (sep === -1) continue;
          const col = key.slice(0, sep);
          if (seen.has(col)) continue;
          seen.add(col);
          const val = flat[`${col}:expected_value`];
          const cnt = flat[`${col}:expected_count`];
          if (val != null && cnt != null) {
            vocList.push([col, val, Number(cnt)]);
          }
        }
        qualityInputs.value_occurrence_expectations = vocList;
      } else {
        qualityInputs.value_occurrence_expectations = parseJsonOrFallback(
          vocRaw,
          qualityInputs.value_occurrence_expectations ?? [],
        );
      }
      qualityInputs.sensitive_columns = Array.isArray(
        normalizedAnswers[`dataset:${did}:sensitive_columns`],
      )
        ? normalizedAnswers[`dataset:${did}:sensitive_columns`]
        : (qualityInputs.sensitive_columns ?? []);
      const categoryColumn = normalizedAnswers[`dataset:${did}:category_column`];
      if (typeof categoryColumn === "string") {
        qualityInputs.category_column = categoryColumn;
      }
      // balance_target_labels: now a tag-list (string[])
      const btlRaw = normalizedAnswers[`dataset:${did}:balance_target_labels`];
      qualityInputs.balance_target_labels = Array.isArray(btlRaw)
        ? btlRaw
        : parseJsonOrFallback(btlRaw, qualityInputs.balance_target_labels ?? []);
      const consistencyLabelThreshold = normalizedAnswers[`dataset:${did}:consistency_label_threshold`];
      if (typeof consistencyLabelThreshold === "string") {
        const trimmed = consistencyLabelThreshold.trim();
        qualityInputs.consistency_label_threshold = trimmed === "" ? null : trimmed;
      }
      qualityInputs.semantic_consistency_rules = parseJsonOrFallback(
        normalizedAnswers[`dataset:${did}:semantic_consistency_rules`],
        qualityInputs.semantic_consistency_rules ?? [],
      );
      // feature_effectiveness_rules: now a column-table with predicate per feature
      const ferRaw = normalizedAnswers[`dataset:${did}:feature_effectiveness_rules`];
      qualityInputs.feature_effectiveness_rules =
        typeof ferRaw === "object" && !Array.isArray(ferRaw)
          ? flattenColumnTable(ferRaw, "rule")
          : parseJsonOrFallback(ferRaw, qualityInputs.feature_effectiveness_rules ?? {});
      const categorySizeThreshold = normalizedAnswers[`dataset:${did}:category_size_threshold`];
      if (typeof categorySizeThreshold === "string") {
        const trimmed = categorySizeThreshold.trim();
        qualityInputs.category_size_threshold = trimmed === "" ? null : trimmed;
      }
      // label_effectiveness_rules: now a tag-list (string[])
      const lerRaw = normalizedAnswers[`dataset:${did}:label_effectiveness_rules`];
      qualityInputs.label_effectiveness_rules = Array.isArray(lerRaw)
        ? lerRaw
        : parseJsonOrFallback(lerRaw, qualityInputs.label_effectiveness_rules ?? [],
      );
      const targetSize = normalizedAnswers[`dataset:${did}:target_size`];
      if (typeof targetSize === "string") {
        const trimmed = targetSize.trim();
        qualityInputs.target_size = trimmed === "" ? null : trimmed;
      }
      qualityInputs.relevant_features = Array.isArray(
        normalizedAnswers[`dataset:${did}:relevant_features`],
      )
        ? normalizedAnswers[`dataset:${did}:relevant_features`]
        : (qualityInputs.relevant_features ?? []);
      qualityInputs.record_relevance_condition = parseJsonOrFallback(
        normalizedAnswers[`dataset:${did}:record_relevance_condition`],
        qualityInputs.record_relevance_condition ?? {},
      );
      qualityInputs.required_attributes = Array.isArray(
        normalizedAnswers[`dataset:${did}:required_attributes`],
      )
        ? normalizedAnswers[`dataset:${did}:required_attributes`]
        : (qualityInputs.required_attributes ?? []);

      updateDataset(activeDataset.uuid, "quality_inputs", qualityInputs);
    }

    // ── Sync sidebar stores with the feedback answers ──────────────
    // The feedback modal and the sidebar TaskSelector / EvaluationProcedure
    // components are independent UI paths to the same selection.  Mirror the
    // same store updates + WS events that the sidebar components emit so the
    // sidebar reflects the user's choices immediately.
    for (const [qid, value] of Object.entries(normalizedAnswers)) {
      if (typeof value !== "string") continue;

      if (qid.endsWith(":task_selection") && value && value !== "__skip__") {
        setSelectedTask(value as any);
        ws.dataScienceTaskSelected({ name: value });
      }

      if (qid.endsWith(":eval_selection") && value && value !== "__skip__") {
        setSelectedEval(value as any);
        ws.evaluationSelected({ name: value });
      }
    }
  };

  /** Save a single section incrementally without closing the modal. */
  const onSaveSection = (
    _sectionKey: string,
    sectionAnswers: Record<string, string | string[] | Record<string, unknown>>,
  ) => {
    const partial = { ...sectionAnswers };
    // Serialize column-table answers to JSON strings
    for (const [key, val] of Object.entries(partial)) {
      if (val && typeof val === "object" && !Array.isArray(val)) {
        (partial as Record<string, unknown>)[key] = JSON.stringify(val);
      }
    }
    const pipelineId = usePipelineStore.getState().tempPipeline?.uuid ?? null;
    emitEvent("FeedbackReceived", {
      answers: partial,
      pipelineId,
      view: "feedback-modal-section",
    });
  };

  return (
    <div className='fixed inset-0 z-50 bg-black/40 flex items-center justify-center'>
      <div className='bg-card flex flex-col h-[80vh] max-w-2xl w-full p-6 rounded shadow-lg relative'>
        <FeedbackLoop
          questions={orderedQuestions}
          onSaveSection={onSaveSection}
          onSubmit={(answers) => {
            onSubmit(answers);
            // Remove the full visible batch, not just fields the user typed
            // into. Optional blank questions would otherwise remain in the
            // store and immediately reopen the modal after submit.
            const idsToRemove = orderedQuestions.map((q) => q.id);
            if (activeSessionId) {
              idsToRemove.push(`session:${activeSessionId}:task_selection`);
              idsToRemove.push(`session:${activeSessionId}:eval_selection`);
            }
            removeQueries(idsToRemove);
            onClose();
          }}
          onCancel={onClose}
        />
      </div>
    </div>
  );
}
