import { Objective, Eval } from "@/types/session";
import { Adapter, Task, IOMapping } from "./pipeline";

/** Per-column metadata computed by the backend profiling graph. */
export interface ColumnProfile {
  dtype: string;
  inferred_type: "int" | "float" | "str" | "bool" | "datetime";
  mixed_types: boolean;
  scale: "binary" | "categorical" | "ordinal" | "continuous";
  unique_count: number;
  null_count: number;
  null_pct: number;
  min: number | string | null;
  max: number | string | null;
  mean: number | null;
  std: number | null;
  sample_values: (string | number | boolean | null)[];
  is_numeric: boolean;
}

/**
 * Cell type discriminant for `column-table` questions.
 * Each cell type maps to a specific inline editor rendered per-row.
 */
export type CellType =
  | "tag-list"        // multi-value tags (e.g. allowed values per column)
  | "range"           // [min, max] numeric pair
  | "type-select"     // dropdown: int | float | str | bool | datetime
  | "number"          // single numeric input
  | "predicate"       // condition builder (op + value)
  | "condition-list"  // list of predicate conditions
  | "yesno"           // inline yes/no toggle
  | "text";           // free-text input

/** One column definition inside a column-table question. */
export interface ColumnTableField {
  /** Field key — appended to row key for answer scoping, e.g. "allowed_values". */
  key: string;
  /** Column header label shown in the table. */
  label: string;
  /** Which inline editor to render for each cell. */
  cellType: CellType;
  /** Placeholder text for empty cells. */
  placeholder?: string;
}

export type Question =
  | {
      id: string;
      type: "yesno" | "text";
      question: string;
      multiline?: boolean;
      initialValue?: string | string[];
      defaultValue?: string;
      /** Logical section key for grouping + incremental save. */
      section?: string;
    }
  | {
      id: string;
      type: "select" | "multi-select";
      question: string;
      options: string[];
      multiline?: boolean;
      initialValue?: string | string[];
      defaultValue?: string | string[];
      /** Logical section key for grouping + incremental save. */
      section?: string;
    }
  | {
      id: string;
      type: "tag-list";
      question: string;
      /** Suggested values shown in the autocomplete dropdown. */
      suggestions?: string[];
      placeholder?: string;
      initialValue?: string[];
      defaultValue?: string[];
      /** Logical section key for grouping + incremental save. */
      section?: string;
    }
  | {
      id: string;
      type: "column-table";
      question: string;
      /** Row keys = column names from the dataset. */
      rows: string[];
      /** Column definitions for the inline editor table. */
      fields: ColumnTableField[];
      /** Per-row column profiles used to prefill cells. */
      profiles?: Record<string, ColumnProfile>;
      /** Pre-populated defaults: { "col_name:field_key": value } */
      initialValue?: Record<string, unknown>;
      defaultValue?: Record<string, unknown>;
      /** Logical section key for grouping + incremental save. */
      section?: string;
    };

/** Section metadata for grouped feedback questions. */
export interface QuestionSection {
  key: string;
  title: string;
  description?: string;
}

type IOType = "int" | "string" | "array" | "float" | "character" | "object";

type InputSpec = { name: string; type: IOType; defaultValue?: string };
type OutputSpec = { name: string; type: IOType };

/** Fields injected by useExecutionStatusBridge at runtime. */
type ExecBridgeFields = {
  status?: string;
  execError?: string;
  execTrace?: string;
  /** Unix timestamp (seconds) when node execution started. */
  execStartTime?: number;
  /** Final duration in seconds (set on completion/failure). */
  execDuration?: number;
};

export interface OperatorProps {
  data: {
    uuid: string;
    name: string;
    isNewNode?: boolean;
    // NEW: drive handles from these (optional for back-compat)
    inputs?: InputSpec[];
    outputs?: OutputSpec[];
    // Compound operator internals (populated by state/group-created WS event)
    children?: Record<string, { name: string; class_type: string; language?: string }>;
    internalEdges?: Array<{ source: string; destination: string; position: number | string; output: number }>;
    ioMap?: Record<string, IOMapping>;
    collapsed?: boolean;
    sourceInterface?: string;
    // Model tracing
    isTracer?: boolean;
    output?: any;
  } & ExecBridgeFields;
}
export interface ParameterProps {
  data: {
    uuid: string;
    name: string;
    isNewNode?: boolean;
    type: string;
    value: string;
    inputs?: InputSpec[];
    outputs?: OutputSpec[];
    updateNodeData: (
      nodeId: string,
      patch:
        | Record<string, any>
        | ((prevData: Record<string, any>) => Record<string, any>),
    ) => void;
  } & ExecBridgeFields;
}

export interface SnippetProps {
  data: {
    uuid: string;
    code: string;
    name: string;
    language: string;
    isNewNode?: boolean;
    inputs?: InputSpec[];
    outputs?: OutputSpec[];
    updateNodeData: (
      nodeId: string,
      patch:
        | Record<string, any>
        | ((prevData: Record<string, any>) => Record<string, any>),
    ) => void;
  } & ExecBridgeFields;
}

export interface SortableListProps {
  items: SortableItemProps[];
  setItems: (items: SortableItemProps[]) => void;
}

export type SortableItemProps = Pick<Objective, "uuid" | "name">;

export interface ObjectiveStatus {
  name: string;
  status: "active" | "degraded";
  missing: string[];
}

/// Pending conflict between user-customised ranking objectives and a
/// pipeline's recommended defaults. Emitted by the rust
/// ``recommendation`` handler as ``state/objectives/conflict-prompt``;
/// the SPA renders a multi-select dialog from these fields.
///
/// The user composes any subset of ``shared ∪ current_only ∪
/// suggested_only`` and the dialog emits a normal
/// ``RankingObjectivesChanged`` with the result. The
/// ``RankingObjectivesAcceptPipelineDefaults`` shortcut still exists
/// for one-click "use suggested" but it is not the only path.
export interface ObjectivesConflict {
  current: Objective[];
  suggested: Objective[];
  shared: Objective[];
  current_only: Objective[];
  suggested_only: Objective[];
  trigger: string;
}

export type Toggles = {
  DatasetUpload: boolean;
  DatasetDelete: boolean;
  TaskSelection: boolean;
  EvalSelection: boolean;
  ObjectiveSelection: boolean;
  ObjectiveDelete: boolean;
  ObjectiveDragging: boolean;
  PipelineImport: boolean;
  PipelineComposition: boolean;
};

export type UIState = {
  pointer: { x: number; y: number };
  username?: string;
  avatar?: string;
  /** Graph layout direction. All four Dagre directions are supported. */
  direction: "TB" | "LR" | "RL" | "BT";

  code: string;
  language: string;
  showCodeViewer: boolean;

  command: boolean;
  selectedEval?: Eval;

  selectedObjectives: Objective[];
  objectiveStatus: ObjectiveStatus[];
  /** When set, the SPA shows a reconcile dialog. Cleared by the user
   *  applying their selection (via RankingObjectivesChanged) or
   *  dismissing the dialog. */
  objectivesConflict: ObjectivesConflict | null;
  queries: Question[];
  feedbackModalOpen: boolean;
  selectedTask?: Task;

  /**
   * Fine-grained feature toggles driven by the backend (sent via `state/query`
   * WebSocket event).  Use ``toggles.DatasetUpload`` etc. instead of the
   * removed ``enableDatasetUpload`` scalar fields — the ``Toggles`` object is
   * the single source of truth for all permission flags.
   */
  toggles: Toggles;

  setPointer: (pointer: { x: number; y: number }) => void;
  setName: (name: string) => void;
  setAvatar: (url: string) => void;

  setCode: (code: string) => void;
  setLanguage: (language: string) => void;
  setShowCodeViewer: (show: boolean) => void;
  setDirection: (direction: "TB" | "LR" | "RL" | "BT") => void;

  setCommand: (open: boolean) => void;
  setSelectedTask: (task: Task | undefined) => void;
  setSelectedEval: (evaluation: Eval | undefined) => void;
  setSelectedObjectives: (objectives: Objective[]) => void;
  setObjectiveStatus: (status: ObjectiveStatus[]) => void;
  setObjectivesConflict: (conflict: ObjectivesConflict | null) => void;

  setQueries: (queries: Question[]) => void;
  removeQueries: (ids: string[]) => void;
  setFeedbackModalOpen: (open: boolean) => void;

  setToggle: <K extends keyof Toggles>(key: K, value: Toggles[K]) => void;

  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
};
