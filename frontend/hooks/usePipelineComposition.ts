import { useMemo, useRef, useCallback, useEffect } from "react";
import {
  addEdge,
  applyNodeChanges,
  applyEdgeChanges,
  type NodeChange,
  type EdgeChange,
  type Connection,
  type Node as RFNode,
  type Edge as RFEdge,
} from "@xyflow/react";

import { throttle } from "@/helpers/throttle";
import {
  snapshotKey,
  buildPipelineSnapshot,
  getLayoutedElements,
  shouldEmitFromNodeChanges,
  shouldEmitFromEdgeChanges,
} from "@/helpers/pipeline";
import { emitEvent } from "@/helpers/ws-events";
import { usePipelineStore } from "@/store/pipeline";
import toast from "react-hot-toast";

import type {
  UUID,
  Operator,
  Edge as PipelineEdge,
  PipelineDraft,
  OperatorParamSpec,
} from "@/types/pipeline";

/** ---------- Shared types ---------- */

export type UpdateNodeData = (
  nodeId: string,
  patch:
    | Record<string, any>
    | ((prevData: Record<string, any>) => Record<string, any>),
) => void;

export type PipelineRFNodeData = {
  label: string;
  uuid: UUID;
  name: string;
  isNewNode?: boolean;
  // optional, depending on your nodes
  value?: string;
  type?: string;
  code?: string;
  language?: string;
  inputs?: any[];
  outputs?: any[];
  updateNodeData?: UpdateNodeData;
  // allow extra fields
  [key: string]: any;
};

export type PipelineRFNode = RFNode<PipelineRFNodeData>;
export type PipelineRFEdge = RFEdge & {
  output?: string | null;
  position?: string | null;
};

type Direction = "TB" | "LR" | "RL" | "BT";

type SetNodes = React.Dispatch<React.SetStateAction<PipelineRFNode[]>>;
type SetEdges = React.Dispatch<React.SetStateAction<PipelineRFEdge[]>>;
type SetVisible = React.Dispatch<React.SetStateAction<boolean>>;

/** ---------- Hook: Snapshot Emitter ---------- */

export function usePipelineSnapshotEmitter({
  tempPipeline,
  setDraftPipeline,
}: {
  tempPipeline: PipelineDraft | null | undefined;
  setDraftPipeline: (s: PipelineDraft) => void;
}) {
  const lastKeyRef = useRef<string | null>(null);

  const getPipelineId = useCallback((): string | undefined => {
    return tempPipeline?.uuid;
  }, [tempPipeline]);

  const emitThrottled = useMemo(() => {
    return throttle((snapshot: PipelineDraft) => {
      setDraftPipeline(snapshot);

      const key = snapshotKey(snapshot as any);
      if (lastKeyRef.current === key) return;
      lastKeyRef.current = key;

      // Also push the live canvas DAG to the backend so
      // session:meta.pipeline stays in step with Zustand. Without this,
      // apply_mitigation / recommendation / replay silently see an
      // empty pipeline until the user hits Save. Fire-and-forget; the
      // WS hook ignores when not connected. Lazy-import to avoid a
      // circular dep with the WS events barrel.
      try {
        // eslint-disable-next-line @typescript-eslint/no-var-requires
        const { ws } = require("@/helpers/ws-events");
        ws.pipelineCanvasChanged({
          nodes: (snapshot as any)?.nodes ?? {},
          edges: (snapshot as any)?.edges ?? [],
        });
      } catch {
        // Snapshot emission must never throw back to the caller.
      }
    }, 300);
  }, [setDraftPipeline]);

  const emitFrom = useCallback(
    (nextNodes: PipelineRFNode[], nextEdges: PipelineRFEdge[]) => {
      const snapshot = buildPipelineSnapshot(
        { ...tempPipeline, uuid: getPipelineId() },
        nextNodes,
        nextEdges,
      ) as PipelineDraft;

      emitThrottled(snapshot);
    },
    [tempPipeline, getPipelineId, emitThrottled],
  );

  const resetDedupe = useCallback(() => {
    lastKeyRef.current = null;
  }, []);

  return { emitFrom, resetDedupe };
}

/** ---------- Hook: Init RF state from tempPipeline ---------- */

export function usePipelineInitFromTemp({
  tempPipeline,
  direction,
  setNodes,
  setEdges,
  setVisible,
  resetDedupe,
  updateNodeData,
}: {
  tempPipeline: any; // your tempPipeline shape is currently `{ nodes: Record<string, ...>, edges: ... }`
  direction: Direction;
  setNodes: SetNodes;
  setEdges: SetEdges;
  setVisible: SetVisible;
  resetDedupe?: () => void;
  updateNodeData: UpdateNodeData;
}) {
  useEffect(() => {
    if (!tempPipeline?.nodes || !tempPipeline?.edges) return;

    setVisible(true);

    // Build lookup of operator names → type overrides from the backend
    // so we can derive isTracer / visualizer on pipeline load (the JSON
    // pipeline doesn't carry these flags).
    const operators = usePipelineStore.getState().operators;
    const tracerNames = new Set(
      operators.filter((o) => o.type === "tracer").map((o) => o.name),
    );
    const visualizerNames = new Set(
      operators.filter((o) => o.type === "visualizer").map((o) => o.name),
    );
    // Hardcoded fallback — printout is always a visualizer
    visualizerNames.add(`dorian.io.printout`);

    const rfNodes: PipelineRFNode[] = Object.entries(tempPipeline.nodes).map(
      ([id, node]: [string, any]) => {
        // Flatten node.data into the RF data bag so properties like `value`
        // land at `data.value` (not `data.data.value`).  The snapshot stores
        // them under `node.data`; spreading `node` would nest them one level
        // too deep, causing parameter values to appear empty on reload.
        const nodeName = node.data?.name ?? node.name ?? id;
        const isTracer = tracerNames.has(nodeName) || nodeName.startsWith("model_tracing.");
        const mergedData: PipelineRFNodeData = {
          ...(node ?? {}),
          ...(node?.data ?? {}),
          label: nodeName,
          uuid: id as UUID,
          name: nodeName,
          // After DAG round-trip the normalisation layer moves the original
          // dtype (e.g. "env") into `dtype` and overwrites `type` with the
          // ReactFlow category ("parameter" / "operator").  Restore the
          // original dtype into data.type so components like ParameterNode
          // can branch on it (e.g. data.type === "env" → env-var UI).
          ...(node?.dtype ? { type: node.dtype } : {}),
          ...(isTracer ? { isTracer: true } : {}),

          updateNodeData,
        };

        // Derive the ReactFlow node type: visualizer and tracer overrides
        // take precedence over the raw JSON type.
        //
        // RL-generated and docstore-sourced pipelines use `class_type`
        // (e.g. "Parameter", "Operator") from DAG.to_json_dict(), while
        // frontend-created pipelines use `type` (e.g. "parameter").
        // Support both: prefer `type`, fall back to `class_type` mapping.
        const CT_MAP: Record<string, string> = {
          Parameter: "parameter",
          Operator: "operator",
          Snippet: "snippet",
          Group: "group",
        };
        const rawType =
          node?.type ??
          CT_MAP[node?.class_type ?? ""] ??
          node?.class_type?.toLowerCase() ??
          "operator";
        let rfType = rawType.toLowerCase() as string;
        if (visualizerNames.has(nodeName)) rfType = "visualizer";
        else if (isTracer) rfType = "operator"; // tracers render as operators with eye button

        return {
          id,
          type: rfType,
          position: { x: 0, y: 0 },
          data: mergedData,
        };
      },
    );

    const rfEdges: PipelineRFEdge[] = (tempPipeline.edges ?? []).map(
      (edge: any, i: number) => {
        const source: string = String(edge.source);
        const target: string = String(edge.destination ?? edge.target);

        const sourceHandle =
          (edge.output ?? edge.sourceHandle ?? null)?.toString() ?? null;
        const targetHandle =
          (edge.position ?? edge.targetHandle ?? null)?.toString() ?? null;

        return {
          id: edge.id ?? `e${source}-${target}-${i}`,
          source,
          target,
          sourceHandle,
          targetHandle,
          type: "labeled",
        };
      },
    );

    const layouted = getLayoutedElements(rfNodes as any, rfEdges as any, {
      direction,
    }) as { nodes: PipelineRFNode[]; edges: PipelineRFEdge[] };

    setNodes(layouted.nodes);
    setEdges(layouted.edges);

    resetDedupe?.();
  }, [
    tempPipeline,
    direction,
    setNodes,
    setEdges,
    setVisible,
    resetDedupe,
    updateNodeData,
  ]);
}

/** ---------- Hook: Apply pending group updates from backend ---------- */

export function useGroupUpdateEffect({
  setNodes,
}: {
  setNodes: SetNodes;
}) {
  const pendingGroupUpdate = usePipelineStore((s) => s.pendingGroupUpdate);
  const setPendingGroupUpdate = usePipelineStore((s) => s.setPendingGroupUpdate);

  useEffect(() => {
    if (!pendingGroupUpdate) return;

    const { nodeId, data } = pendingGroupUpdate;

    // Update the matching RF node's data in place (merge backend Group data).
    setNodes((prev) =>
      prev.map((n) => {
        if (n.id !== nodeId) return n;
        return {
          ...n,
          data: {
            ...n.data,
            ...data,
            uuid: nodeId as UUID,
          },
        };
      }),
    );

    // Clear the pending update so it doesn't re-apply.
    setPendingGroupUpdate(null);
  }, [pendingGroupUpdate, setNodes, setPendingGroupUpdate]);
}

/** ---------- Hook: ReactFlow handlers ---------- */

export function useReactFlowHandlers({
  nodes,
  edges,
  setNodes,
  setEdges,
  emitFrom,
}: {
  nodes: PipelineRFNode[];
  edges: PipelineRFEdge[];
  setNodes: SetNodes;
  setEdges: SetEdges;
  emitFrom: (nextNodes: PipelineRFNode[], nextEdges: PipelineRFEdge[]) => void;
}) {
  const edgesRef = useRef(edges);
  edgesRef.current = edges;
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      setNodes((prev) => {
        // Emit removal events BEFORE applying changes — node data is still
        // accessible in `prev`.  Using `prev` (not the outer `nodes`) avoids
        // a stale-closure bug where `nodes` captures an outdated reference
        // because it is not in the useCallback dependency array.
        const pipelineId = usePipelineStore.getState().tempPipeline?.uuid ?? null;
        for (const c of changes) {
          if (c.type === "remove") {
            const node = prev.find((n) => n.id === c.id);
            emitEvent("PipelineNodeRemoved", {
              nodeId: c.id,
              nodeName: node?.data?.name ?? "",
              nodeType: node?.type ?? "",
              pipelineId,
            });
          }
        }

        const next = applyNodeChanges(changes, prev) as PipelineRFNode[];
        if (shouldEmitFromNodeChanges(changes as any)) emitFrom(next, edgesRef.current);
        return next;
      });
    },
    [setNodes, emitFrom],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      const pipelineId = usePipelineStore.getState().tempPipeline?.uuid ?? null;
      for (const c of changes) {
        if (c.type === "remove") {
          emitEvent("PipelineEdgeRemoved", { edgeId: c.id, pipelineId });
        }
      }

      setEdges((prev) => {
        const next = applyEdgeChanges(changes, prev) as PipelineRFEdge[];
        if (shouldEmitFromEdgeChanges(changes as any)) emitFrom(nodesRef.current, next);
        return next;
      });
    },
    [setEdges, emitFrom],
  );

  const onConnect = useCallback(
    (params: Connection) => {
      const pipelineId = usePipelineStore.getState().tempPipeline?.uuid ?? null;
      emitEvent("PipelineEdgeAdded", {
        source: params.source,
        target: params.target,
        sourceHandle: params.sourceHandle ?? null,
        targetHandle: params.targetHandle ?? null,
        pipelineId,
      });

      setEdges((prev) => {
        const next = addEdge(
          { ...params, type: "labeled" },
          prev,
        ) as PipelineRFEdge[];
        emitFrom(nodesRef.current, next);
        return next;
      });
    },
    [setEdges, emitFrom],
  );

  return { onNodesChange, onEdgesChange, onConnect };
}

/** ---------- helpers: compound subgraph builder ---------- */

const _uid = () => Math.random().toString(36).slice(2, 11);

// ── Layout constants (tree-like depth spacing) ──────────────────────────
//
// The compound subgraph has 3 depth layers:
//   depth 0 — parameter nodes (leaves / inputs)
//   depth 1 — operator / method nodes (processing)
//   depth 2 — companion nodes (outputs)
//
// DEPTH_GAP is the vertical distance between adjacent layers.  It must be
// large enough for handle labels + edge routing to remain readable.
const DEPTH_GAP = 132;

/** Minimum rendered width of a parameter node (must match parameter.tsx). */
const MIN_PARAM_W = 140;
/** Maximum rendered width of a parameter node (must match parameter.tsx). */
const MAX_PARAM_W = 340;

/** Extra gap between adjacent parameter nodes so they don't touch. */
const PARAM_GAP = 20;

/**
 * Estimate the rendered width of a parameter node.
 * Must stay in sync with parameter.tsx `computeParamWidth`:
 *   min(MAX_PARAM_W, max(MIN_PARAM_W, name*8+40, value*6+50))
 * The layout adds PARAM_GAP so nodes don't touch.
 */
function _estimateParamWidth(name: string, value = ""): number {
  const nameW = name.length * 8 + 40;
  const valueW = value.length * 6 + 50;
  return Math.min(MAX_PARAM_W, Math.max(MIN_PARAM_W, nameW, valueW)) + PARAM_GAP;
}

/**
 * Compute the total fan width for a set of parameter specs.
 */
function _paramFanWidth(specs: { name: string; value?: string }[]): number {
  if (specs.length === 0) return 0;
  return specs.reduce((acc, s) => acc + _estimateParamWidth(s.name, s.value), 0);
}

/**
 * Return an array of x-offsets (relative to the parent node center)
 * for each parameter so that none of them overlap.
 */
function _paramXOffsets(specs: { name: string; value?: string }[]): number[] {
  if (specs.length === 0) return [];
  const widths = specs.map((s) => _estimateParamWidth(s.name, s.value));
  const totalWidth = widths.reduce((a, b) => a + b, 0);
  let cursor = -totalWidth / 2;
  return widths.map((w) => {
    const x = cursor + w / 2;
    cursor += w;
    return x;
  });
}

/**
 * Companion operators added downstream when a specific operator is dropped.
 * Each entry maps an operator FQN to a list of downstream operators that are
 * automatically connected to the primary operator's output.
 */
const COMPOUND_COMPANIONS: Record<
  string,
  { name: string; type: string }[]
> = {
  "openrouter.chat.completion": [
    { name: "dorian.io.printout", type: "visualizer" },
  ],
};

/**
 * Build an operator sub-DAG on the canvas with explicit method nodes.
 *
 * For compound interfaces (2+ methods in the ``calls`` chain), creates:
 *   - An operator node representing ``__init__`` (the class constructor)
 *   - One method node per subsequent method (e.g. ``chat.send``, ``fit``)
 *   - Parameter nodes routed to their declaring method
 *   - Internal edges passing the instance through the method chain
 *   - Data I/O handles on the terminal method node
 *
 * For simple operators (no methods / single method), falls back to a
 * single operator node with all params attached.
 *
 * Companion operators (e.g. printout after LLM) are appended downstream.
 */
/** Serialize a param default to a display string. Objects/arrays → JSON. */
function _defaultStr(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function buildOperatorNode(
  operator: Operator,
  position: { x: number; y: number },
  updateNodeData: UpdateNodeData,
): { nodes: PipelineRFNode[]; edges: PipelineRFEdge[] } {
  const catalog = usePipelineStore.getState().operatorParams;
  const entry = catalog[operator.name];
  const params: OperatorParamSpec[] = entry?.params ?? [];
  const methods: string[] = entry?.methods ?? [];

  const opId = `${operator.name}-${_uid()}`;
  const allNodes: PipelineRFNode[] = [];
  const allEdges: PipelineRFEdge[] = [];

  // ── Determine node type ──────────────────────────────────────────────
  // Compound operators render as regular operator nodes; the eye button
  // for viewing internal structure is added by OperatorNode when
  // data.children is populated (via state/group-created WS event).
  const isCompound = methods.length >= 2;
  const isTracer = operator.type?.toLowerCase() === "tracer";
  const nodeType = isTracer ? "operator" : (operator.type?.toLowerCase() || "operator") as string;

  const paramInputs = params.map((p) => ({ name: p.name, type: p.dtype }));
  const dataInputs = (entry?.inputs ?? []).map((io) => ({
    name: io.name,
    type: io.type,
  }));
  const inputsSpec = [...dataInputs, ...paramInputs];

  const outputsSpec = (entry?.outputs ?? []).map((io) => ({
    type: io.name,
  }));

  const opNode: PipelineRFNode = {
    id: opId,
    type: nodeType,
    position,
    data: {
      ...operator,
      uuid: opId as UUID,
      label: operator.name,
      name: operator.name,
      isNewNode: true,
      updateNodeData,
      ...(inputsSpec.length > 0 ? { inputs: inputsSpec } : {}),
      ...(outputsSpec.length > 0 ? { outputs: outputsSpec } : {}),
      // Group-specific fields (populated by state/group-created WS response)
      ...(isCompound ? { collapsed: true, ioMap: {}, children: {}, sourceInterface: "" } : {}),
      ...(isTracer ? { isTracer: true } : {}),
    },
  };

  if (params.length === 0 && !(operator.name in COMPOUND_COMPANIONS)) {
    return { nodes: [opNode], edges: [] };
  }

  const simpleSpecs = params.map((p) => ({ name: p.name, value: _defaultStr(p.default) }));
  const simpleXOffsets = _paramXOffsets(simpleSpecs);
  params.forEach((p, i) => {
    const paramId = `${p.name}-${_uid()}`;
    const paramX = position.x + simpleXOffsets[i];
    const paramY = position.y - DEPTH_GAP;

    const defaultStr = _defaultStr(p.default);

    allNodes.push({
      id: paramId,
      type: "parameter",
      position: { x: paramX, y: paramY },
      data: {
        uuid: paramId as UUID,
        label: p.name,
        name: p.name,
        value: defaultStr,
        type: p.dtype,
        isNewNode: true,
        updateNodeData,
        compoundGroupId: opId,
      },
    });

    allEdges.push({
      id: `e${paramId}-${opId}-${p.name}`,
      source: paramId,
      target: opId,
      sourceHandle: "0",
      targetHandle: p.name,
      type: "labeled",
    });
  });

  const companions = COMPOUND_COMPANIONS[operator.name] ?? [];
  companions.forEach((comp, i) => {
    const compId = `${comp.name}-${_uid()}`;
    const compX = position.x + (i - (companions.length - 1) / 2) * 220;
    const compY = position.y + DEPTH_GAP;

    allNodes.push({
      id: compId,
      type: comp.type,
      position: { x: compX, y: compY },
      data: {
        uuid: compId as UUID,
        label: comp.name,
        name: comp.name,
        isNewNode: true,
        updateNodeData,
        compoundGroupId: opId,
      },
    });

    allEdges.push({
      id: `e${opId}-${compId}-companion-${i}`,
      source: opId,
      target: compId,
      sourceHandle: "0",
      targetHandle: "0",
      type: "labeled",
    });
  });

  return { nodes: [opNode, ...allNodes], edges: allEdges };
}

/** ---------- Hook: Drag & Drop ---------- */

export function usePipelineDnD({
  draggingNode,
  screenToFlowPosition,
  edges,
  setNodes,
  setEdges,
  emitFrom,
  updateNodeData,
}: {
  draggingNode: Operator | null | undefined;
  screenToFlowPosition: (p: { x: number; y: number }) => {
    x: number;
    y: number;
  };
  edges: PipelineRFEdge[]; // TODO: remove from props
  setNodes: SetNodes;
  setEdges: SetEdges;
  emitFrom: (nextNodes: PipelineRFNode[], nextEdges: PipelineRFEdge[]) => void;
  updateNodeData: UpdateNodeData;
}) {
  const onDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }, []);

  const onDragStart = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.dataTransfer.setData("text/plain", "operator");
    e.dataTransfer.effectAllowed = "move";
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      if (!draggingNode) return;

      const position = screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });

      const { nodes: newNodes, edges: newEdges } = buildOperatorNode(
        draggingNode,
        position,
        updateNodeData,
      );

      const pipelineId = usePipelineStore.getState().tempPipeline?.uuid ?? null;

      // Emit events for every node in the subgraph
      for (const n of newNodes) {
        emitEvent("PipelineNodeAdded", {
          nodeId: n.id,
          nodeType: n.type,
          nodeName: n.data.name,
          pipelineId,
        });
      }
      for (const e of newEdges) {
        emitEvent("PipelineEdgeAdded", {
          source: e.source,
          target: e.target,
          sourceHandle: e.sourceHandle ?? null,
          targetHandle: e.targetHandle ?? null,
          pipelineId,
        });
      }

      setNodes((prev) => {
        const nextNodes = prev.concat(newNodes);
        setEdges((prevEdges) => {
          const nextEdges = prevEdges.concat(newEdges);
          emitFrom(nextNodes, nextEdges);
          return nextEdges;
        });
        return nextNodes;
      });

      // Hint: keyword parameters were pre-connected, but positional inputs
      // (e.g. training data) still need manual edge connections.
      if (newEdges.length > 0) {
        toast(
          `${draggingNode.name}: keyword parameters connected. ` +
            `Connect positional inputs (data sources) manually.`,
          { icon: "🔗", duration: 5000 },
        );
      }
    },
    [
      draggingNode,
      screenToFlowPosition,
      setNodes,
      setEdges,
      emitFrom,
      updateNodeData,
    ],
  );

  return { onDrop, onDragOver, onDragStart };
}

/** ---------- Hook: Add custom operators ---------- */

export function useCustomOperatorsEffect({
  customOperators,
  stfp,
  setNodes,
  setEdges,
  edges,
  emitFrom,
  updateNodeData,
}: {
  customOperators: Operator[] | undefined;
  stfp: (p: { x: number; y: number }) => { x: number; y: number };
  setNodes: SetNodes;
  setEdges: SetEdges;
  edges: PipelineRFEdge[]; // TODO: remove from props
  emitFrom: (nextNodes: PipelineRFNode[], nextEdges: PipelineRFEdge[]) => void;
  updateNodeData: UpdateNodeData;
}) {
  const addCustomNode = useCallback(
    (node: Operator) => {
      const position = stfp({
        x: window.innerWidth / 2,
        y: window.innerHeight / 2,
      });

      const { nodes: newNodes, edges: newEdges } = buildOperatorNode(
        node,
        position,
        updateNodeData,
      );

      const pipelineId = usePipelineStore.getState().tempPipeline?.uuid ?? null;

      for (const n of newNodes) {
        emitEvent("PipelineNodeAdded", {
          nodeId: n.id,
          nodeType: n.type,
          nodeName: n.data.name,
          pipelineId,
          source: "custom-operator-panel",
        });
      }
      for (const e of newEdges) {
        emitEvent("PipelineEdgeAdded", {
          source: e.source,
          target: e.target,
          sourceHandle: e.sourceHandle ?? null,
          targetHandle: e.targetHandle ?? null,
          pipelineId,
        });
      }

      setNodes((prev) => {
        const nextNodes = prev.concat(newNodes);
        setEdges((prevEdges) => {
          const nextEdges = prevEdges.concat(newEdges);
          emitFrom(nextNodes, nextEdges);
          return nextEdges;
        });
        return nextNodes;
      });
    },
    [stfp, setNodes, setEdges, emitFrom, updateNodeData],
  );

  useEffect(() => {
    if (customOperators?.length) {
      addCustomNode(customOperators[customOperators.length - 1]);
    }
  }, [customOperators, addCustomNode]);
}
