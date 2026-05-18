import type { UUID } from "@/types/index";
export type { UUID };

type NodeRunStatus = "pending" | "running" | "success" | "failed" | "skipped" | "cancelled";

export interface NodeRunState {
  status: NodeRunStatus;
  error?: string;
  trace?: string;
  /** Unix timestamp (seconds) when the node started executing. */
  start_time?: number;
  /** Elapsed seconds from start to completion/failure. */
  duration?: number;
  /** Inline result payload (printout/visualizer nodes). */
  output?: unknown;
}

export interface PipelineRun {
  run_id: string;
  status: NodeRunStatus;
  node_states: Record<string, NodeRunState>;
  /** Evaluation metrics computed after a successful run (e.g., accuracy, f1). */
  metrics?: Record<string, number>;
  /** Run-level error message (e.g. expansion/graph-build failure). */
  error?: string;
}

export interface Operator {
  uuid: UUID;
  name: string;
  type?: string;
  /** docstore/RL-generated pipelines use `class_type` (e.g. "Parameter") instead of `type`. */
  class_type?: string;
  /** Parameter dtype (e.g. "int", "float", "env", "categorical"). */
  dtype?: string;
  codePen?: {
    language: string;
    code: string;
  };
  position?: {
    x: number;
    y: number;
  };
}

export interface Edge {
  source: UUID;
  output: string;
  target: UUID;
  position: string;
}

export interface Node {
  id: string;
  type: string;
  /** docstore/RL-generated pipelines use `class_type` (e.g. "Parameter") instead of `type`. */
  class_type?: string;
  position: {
    x: number;
    y: number;
  };
  data: {
    label: string;
    uuid: UUID;
    name: string;
  };
}

//pipeline
export interface Pipeline {
  id: UUID; // version id
  parentPipelineId: UUID; // parent pipeline uuid
  createdAt: string; // ISO
  createdBy?: string;

  // optional metadata
  message?: string; // "Saved before trying X"
  parentId?: UUID | null; // for linear history or branching
  tags?: string[];

  // the actual graph snapshot
  nodes: Record<string, Operator>;
  edges: Edge[];
}

//pipeline history
export interface PipelineHistory {
  uuid: UUID;
  headId: UUID;
  pipelines: Pipeline[];
}

export interface PipelineDraft {
  uuid: UUID;
  nodes: Record<string, Operator>;
  edges: Edge[];
}

export type EdgeLike = {
  source: string;
  position: number | string;
  output: number;
  target?: string;
};

export interface Task {
  name: string;
  id?: string;
  /** True when the backend auto-detected this task from the dataset profile. */
  auto?: boolean;
  /** Short human-readable rationale shown alongside the auto-detected badge. */
  reason?: string;
}

export interface Adapter {
  name: string;
}

/** Parameter spec from the KB (sent during session seed). */
export interface OperatorParamSpec {
  name: string;
  dtype: string;
  default: any;
  /** Method this param routes to (null/undefined → __init__). */
  method?: string | null;
}

export interface OperatorIOSpec {
  name: string;
  position: number | string;
  type: string;
  /** Pre-filled default value — when present, DnD auto-creates a connected parameter node. */
  default?: string;
}

/** Operator FQN → parameter + I/O specs for compound DnD. */
export interface OperatorCatalogEntry {
  params: OperatorParamSpec[];
  /** Interface method sequence (e.g. ["__init__", "chat.send"]). */
  methods?: string[];
  inputs?: OperatorIOSpec[];
  outputs?: OperatorIOSpec[];
}

export type OperatorParamCatalog = Record<string, OperatorCatalogEntry>;

export interface Suggestion {
  sid: string;
  action: string;
  event: string;
  risk: string;
  session: string;
  task: string;
  uid: string;
  /** Templated short description (interpolated with operator/risk names). */
  description_short?: string;
  /** Templated long description for detailed tooltip / accept dialog. */
  description_long?: string;
  /** FQN alternatives for "Direct Alternative" mitigation. */
  alternatives?: string[];
  /** EU AI Act principles threatened by this risk. */
  principles?: string[];
  /** Available toolbox check names for this risk. */
  checks?: string[];
  severity?: "low" | "medium" | "high";
  /** Whether this risk has been validated on data ("actionable") or is
   *  KB-only knowledge about the operator ("potential"). */
  status?: "potential" | "actionable";
  source?: "kb" | "data_check" | "pathway";
  /** Human label for which pipeline this suggestion applies to. */
  pipeline_label?: string;
  /** ID of the recommendation pipeline this suggestion targets (for mitigation rewrites). */
  pipeline_id?: string;
  /** Human-readable message from the data check that confirmed this risk. */
  check_message?: string;
  /** Whether a graph rewrite rule exists in the docstore for this mitigation. */
  has_rewrite?: boolean;
}

export interface CheckResultItem {
  check: string;
  risk: string;
  operator: string;
  status: "passed" | "failed" | "skipped" | "error";
  message: string;
}

export interface CheckReport {
  pipelineLabel: string;
  total: number;
  passed: number;
  failed: number;
  skipped: number;
  results: CheckResultItem[];
}

export type ProcessCategory = "data_profiling" | "data_quality" | "quality_checks" | "pipeline_execution";

export interface ProgressItem {
  uid: string;
  session: string;
  did: string;
  metafeature: string;
  value: any;
  error?: string;
  status: string;
  pid: string;
  /** Category for grouped rendering in the progress panel. */
  category?: ProcessCategory;
}

// ---------------------------------------------------------------------------
// Group Node types (collapsed compound operator sub-DAGs)
// ---------------------------------------------------------------------------

export interface IOMapping {
  direction: "input" | "output";
  internalNodeId: string;
  internalHandle: string | number;
}

export interface GroupNodeData {
  name: string;
  children: Record<string, any>;
  internalEdges: Array<{
    source: string;
    destination: string;
    position: number | string;
    output: number;
  }>;
  ioMap: Record<string, IOMapping>;
  collapsed: boolean;
  sourceInterface?: string;
  sourcePipelineId?: string;
}

export type PipelineState = {
  // pipeline graph state
  tempPipeline: PipelineDraft | null;
  draftPipeline: PipelineDraft | null;
  pipelineHistory: PipelineHistory | null;

  draggingNode: Operator | null;
  customOperators: Operator[];
  operators: Operator[];

  /** Parameter catalog keyed by operator FQN — populated from backend on session seed. */
  operatorParams: OperatorParamCatalog;

  adapters?: Adapter[];

  // ✅ pipeline-specific "intelligence"
  suggestions: Suggestion[];
  addSuggestion: (suggestion: Suggestion) => void;
  removeSuggestion: (suggestion: Suggestion) => void;
  setSuggestions: (suggestions: Suggestion[]) => void;
  clearSuggestions: () => void;

  // ✅ pipeline process state  (Record<pid, item> — O(1) upsert/lookup)
  progressItems: Record<string, ProgressItem>;
  addProgressItem: (progressItem: ProgressItem) => void;
  setProgressItems: (progressItems: ProgressItem[]) => void;
  removeProgressItem: (progressItem: ProgressItem) => void;
  updateProgressItem: (progressItem: ProgressItem) => void;
  clearProgressItems: () => void;

  // versioning (keep yours)
  createPipelineIfMissing: () => void;
  getHeadVersion: () => Pipeline | null;
  updateHeadGraph: (patch: { nodes?: Record<string, Operator>; edges?: Edge[] }) => void;
  saveNewVersionFromCurrent: (opts?: { message?: string }) => void;
  restoreVersion: (versionId: string) => void;
  removePipeline: () => void;

  // extraction origin tracking
  /** Extraction ID if this pipeline was created from code extraction (for correction flow). */
  sourceExtractionId: string | null;
  setSourceExtractionId: (id: string | null) => void;

  // setters
  setTempPipeline: (pipeline: PipelineDraft | null) => void;
  setPipelineHistory: (pipelineHistory: PipelineHistory | null) => void;
  setDraftPipeline: (pipeline: PipelineDraft | null) => void;
  setDraggingNode: (node: Operator | null) => void;
  setCustomOperators: (operators: Operator[]) => void;
  addCustomOperator: (operator: Operator) => void;
  setOperators: (operators: Operator[]) => void;
  setOperatorParams: (params: OperatorParamCatalog) => void;

  setAdapters: (adapters: Adapter[]) => void;

  /** Pending group update from backend WS — consumed by canvas effect. */
  pendingGroupUpdate: { nodeId: string; data: GroupNodeData } | null;
  setPendingGroupUpdate: (update: { nodeId: string; data: GroupNodeData } | null) => void;
};
