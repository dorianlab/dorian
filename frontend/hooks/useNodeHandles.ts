"use client";
/**
 * useNodeHandles
 * ---------------
 * Derives deduplicated source/target handle descriptors for a pipeline node.
 *
 * All three node types (Operator, Parameter, Snippet) share identical logic:
 *   1. Prefer IO-spec arrays (data.inputs / data.outputs) when provided.
 *   2. Fall back to deduplicated edge-derived handles when IO specs are absent.
 *   3. Guarantee at least one handle each for brand-new nodes.
 *
 * Centralising this here eliminates ~50 lines of copy-pasted code that lived
 * in operator.tsx, parameter.tsx, and snippet.tsx.
 */
import { useReactFlow } from "@xyflow/react";
import { useMemo } from "react";

type HandleItem = { position: string; name?: string; type?: string };

interface IOSpec {
  name?: string;
  type?: string;
}

interface UseNodeHandlesOptions {
  /** UUID of the node — used to filter the ReactFlow edge list. */
  nodeId: string;
  /** Outputs spec from node data (drives source handles when populated). */
  outputs?: IOSpec[];
  /** Inputs spec from node data (drives target handles when populated). */
  inputs?: IOSpec[];
  /** When true, ensures at least one source and one target handle. */
  isNewNode?: boolean;
}

interface NodeHandles {
  sources: HandleItem[];
  targets: HandleItem[];
}

export function useNodeHandles({
  nodeId,
  outputs,
  inputs,
  isNewNode,
}: UseNodeHandlesOptions): NodeHandles {
  const { getEdges } = useReactFlow();
  const edges = getEdges() as any[];

  return useMemo(() => {
    // IO-spec-driven handles.
    // When IO specs carry a `name`, propagate it so HandleRenderer can use it as
    // the handle id / label (e.g. keyword-arg names like "n_estimators").
    const sourcesFromIO: HandleItem[] = (outputs ?? []).map((o, i) => ({
      position: String(i),
      ...(o.name ? { name: o.name } : {}),
      ...(o.type ? { type: o.type } : {}),
    }));
    const targetsFromIO: HandleItem[] = (inputs ?? []).map((inp, i) => ({
      // Use the input's name as position when present (kwarg handle, e.g. "messages"),
      // otherwise fall back to array index (positional handle).
      position: inp.name ?? String(i),
      ...(inp.name ? { name: inp.name } : {}),
      ...(inp.type ? { type: inp.type } : {}),
    }));

    // Edge-driven handles: deduplicate by port position so multiple edges sharing
    // the same output/input port render as one handle, not one per edge.
    const sourcesFromEdges: HandleItem[] = Array.from(
      new Map(
        edges
          .filter((e) => e.source === nodeId)
          .map((e) => {
            const pos = String(e.sourceHandle ?? e.output ?? 0);
            return [pos, { position: pos }] as const;
          }),
      ).values(),
    );

    const targetsFromEdges: HandleItem[] = Array.from(
      new Map(
        edges
          .filter((e) => e.target === nodeId)
          .map((e) => {
            const pos = String(e.targetHandle ?? e.position ?? 0);
            // When the handle id is a non-numeric string (keyword arg name),
            // propagate it as `name` so HandleRenderer uses it as the label.
            const isName = pos && Number.isNaN(Number(pos));
            return [pos, { position: pos, ...(isName ? { name: pos } : {}) }] as const;
          }),
      ).values(),
    );

    const hasOutputs = Array.isArray(outputs) && outputs.length > 0;
    const hasInputs = Array.isArray(inputs) && inputs.length > 0;

    let sources: HandleItem[] = hasOutputs ? sourcesFromIO : sourcesFromEdges;
    let targets: HandleItem[] = hasInputs ? targetsFromIO : targetsFromEdges;

    if (isNewNode && sources.length === 0) sources = [{ position: "0" }];
    if (isNewNode && targets.length === 0) targets = [{ position: "0" }];

    return { sources, targets };
  }, [edges, nodeId, outputs, inputs, isNewNode]);
}
