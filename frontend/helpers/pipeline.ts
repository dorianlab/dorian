import Dagre from "@dagrejs/dagre";
import type { Node as RFNode, Edge as RFEdge, NodeChange, EdgeChange } from "@xyflow/react";

/**
 * Default node dimensions used when ReactFlow hasn't measured the DOM yet
 * (first render).  Without these, Dagre treats every node as 0×0 and stacks
 * them on top of each other.
 */
const DEFAULT_NODE_W = 200;
const DEFAULT_NODE_H = 60;

export const getLayoutedElements = <
  N extends Pick<RFNode, "id" | "type"> & { measured?: { width?: number; height?: number }; position?: { x: number; y: number } },
>(
  nodes: N[],
  edges: Pick<RFEdge, "id" | "source" | "target">[],
  options: { direction: "TB" | "LR" | "RL" | "BT" } = { direction: "TB" },
) => {
  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: options.direction,
    nodesep: 40,
    ranksep: 100,
    marginx: 20,
    marginy: 20,
  });

  const nodeIds = new Set(nodes.map((n) => n.id));
  nodes.forEach((node) => {
    const w = node.measured?.width  ?? DEFAULT_NODE_W;
    const h = node.measured?.height ?? DEFAULT_NODE_H;
    g.setNode(node.id, { width: w, height: h });
  });
  edges.forEach((edge) => {
    if (nodeIds.has(edge.source) && nodeIds.has(edge.target)) {
      g.setEdge(edge.source, edge.target);
    }
  });

  if (process.env.NODE_ENV !== "production") {
    const edgeCount = g.edgeCount();
    const nodeCount = g.nodeCount();
    console.debug(`[dagre] layout: ${nodeCount} nodes, ${edgeCount} edges, dir=${options.direction}`);
  }

  Dagre.layout(g);

  return {
    nodes: nodes.map((node) => {
      const position = g.node(node.id);
      const w = node.measured?.width  ?? DEFAULT_NODE_W;
      const h = node.measured?.height ?? DEFAULT_NODE_H;
      // Centre the node on the Dagre-computed coordinate.
      const x = position.x - w / 2;
      const y = position.y - h / 2;
      return { ...node, position: { x, y } };
    }),
    edges,
  };
};

/** Stable identity key for a pipeline snapshot (used for dedup). */
interface SnapshotLike {
  uuid?: string | null;
  sessionId?: string | null;
  nodes: Record<string, { type?: string; name?: string; position?: { x?: number; y?: number } }>;
  edges: Array<{ source: string; destination: string; output?: string | number | null; position?: string | number | null }>;
}

export function snapshotKey(s: SnapshotLike) {
  const nodeIds = Object.keys(s.nodes).sort();
  const nodesPart = nodeIds
    .map((id) => {
      const n = s.nodes[id];
      const pos = n.position || {};
      return `${id}|${n.type ?? ""}|${n.name ?? ""}|${pos.x ?? 0},${
        pos.y ?? 0
      }`;
    })
    .join(";");

  const edgesPart = [...s.edges]
    .map((e) => ({
      s: e.source,
      t: e.destination,
      sh: e.output ?? "",
      th: e.position ?? "",
    }))
    .sort((a, b) =>
      a.s === b.s
        ? a.t === b.t
          ? (a.sh + "" + a.th).localeCompare(b.sh + "" + b.th)
          : a.t.localeCompare(b.t)
        : a.s.localeCompare(b.s),
    )
    .map((e) => `${e.s}->${e.t}[${e.sh}:${e.th}]`)
    .join(";");

  return `${s.uuid ?? ""}|${s.sessionId ?? ""}|N:${nodesPart}|E:${edgesPart}`;
}

/** Pipeline base metadata (the parts we read when building a snapshot). */
interface PipelineBase {
  uuid?: string;
  id?: string;
  pipelineId?: string;
  sessionId?: string | null;
  createdAt?: string | null;
  createdBy?: string | null;
}

/** Node shape as stored in the pipeline snapshot. */
interface SnapshotNode {
  id: string;
  type: string;
  name: string;
  position: { x: number; y: number };
  data: Record<string, unknown>;
  dtype?: string;
}

export function buildPipelineSnapshot(
  base: PipelineBase | null | undefined,
  rfNodes: RFNode[],
  rfEdges: RFEdge[],
) {
  const nodesRecord = rfNodes.reduce(
    (acc, n) => {
      acc[n.id] = {
        id: n.id,
        type: n.type ?? "operator",
        name: (n.data as Record<string, unknown>)?.name as string ?? (n.data as Record<string, unknown>)?.label as string ?? n.id,
        position: n.position,
        data: n.data as Record<string, unknown>,
        // Hoist dtype for parameter nodes so the backend _parse_pipeline
        // finds it at top level.  Frontend stores the actual param dtype
        // (e.g. "env", "eval") in data.type; n.type is the ReactFlow
        // node category ("parameter").  Without this, the backend falls
        // back to "str" and env-var nodes revert to regular parameters.
        ...(n.type === "parameter" && (n.data as Record<string, unknown>)?.type
          ? { dtype: (n.data as Record<string, unknown>).type as string }
          : {}),
      };
      return acc;
    },
    {} as Record<string, unknown>,
  );

  const edgesArray = rfEdges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    destination: e.target,
    sourceHandle: e.sourceHandle ?? null,
    targetHandle: e.targetHandle ?? null,
    output: e.sourceHandle ?? null,
    position: e.targetHandle ?? null,
  }));

  return {
    uuid: base?.uuid ?? base?.id ?? base?.pipelineId,
    sessionId: base?.sessionId ?? null,
    createdAt: base?.createdAt ?? null,
    createdBy: base?.createdBy ?? null,
    nodes: nodesRecord,
    edges: edgesArray,
  };
}

export const shouldEmitFromNodeChanges = (changes: NodeChange[]) =>
  changes.some(
    (c) => c.type === "add" || c.type === "remove" || c.type === "dimensions",
  );

export const shouldEmitFromEdgeChanges = (changes: EdgeChange[]) =>
  changes.some((c) => c.type === "add" || c.type === "remove");

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- JSON parser returns genuinely dynamic data
function safeJsonParse(v: any): any {
  if (typeof v !== "string") return v;
  try {
    return JSON.parse(v);
  } catch {
    return v;
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- JSON parser returns genuinely dynamic data
export function parseJsonDeep(input: any): any {
  // parse top-level string if it's JSON
  input = safeJsonParse(input);

  if (Array.isArray(input)) return input.map(parseJsonDeep);

  if (input && typeof input === "object") {
    const out: Record<string, any> = {};
    for (const [k, v] of Object.entries(input)) {
      out[k] = parseJsonDeep(v);
    }
    return out;
  }

  // IMPORTANT: do NOT parse numbers, only strings
  return input;
}
