import { useCallback } from "react";
import { useReactFlow } from "@xyflow/react";
import type { Connection } from "@xyflow/react";

/**
 * Returns an `isValidConnection` callback for ReactFlow that rejects:
 * 1. Self-loops (source === target)
 * 2. Duplicate edges (same source + target + handles)
 * 3. Parameter-to-Parameter edges (both nodes are "parameter" type)
 * 4. Cycles (adding the edge would create a directed cycle)
 */
export function useEdgeValidation() {
  const { getNodes, getEdges } = useReactFlow();

  const isValidConnection = useCallback(
    (connection: Connection): boolean => {
      const { source, target, sourceHandle, targetHandle } = connection;
      if (!source || !target) return false;

      // 1. No self-loops
      if (source === target) return false;

      const edges = getEdges();
      const nodes = getNodes();

      // 2. No duplicate edges
      const duplicate = edges.some(
        (e) =>
          e.source === source &&
          e.target === target &&
          e.sourceHandle === sourceHandle &&
          e.targetHandle === targetHandle,
      );
      if (duplicate) return false;

      // 3. No Parameter-to-Parameter edges
      const nodeMap = new Map(nodes.map((n) => [n.id, n]));
      const sourceNode = nodeMap.get(source);
      const targetNode = nodeMap.get(target);
      if (sourceNode?.type === "parameter" && targetNode?.type === "parameter") {
        return false;
      }

      // 4. No cycles — DFS from target following existing edges to check
      //    whether source is reachable (i.e. target already leads to source).
      const adjacency = new Map<string, string[]>();
      for (const e of edges) {
        if (!adjacency.has(e.source)) adjacency.set(e.source, []);
        adjacency.get(e.source)!.push(e.target);
      }

      const visited = new Set<string>();
      const stack = [target];
      while (stack.length > 0) {
        const current = stack.pop()!;
        if (current === source) return false;
        if (visited.has(current)) continue;
        visited.add(current);
        const neighbors = adjacency.get(current);
        if (neighbors) {
          for (const n of neighbors) {
            if (!visited.has(n)) stack.push(n);
          }
        }
      }

      return true;
    },
    [getNodes, getEdges],
  );

  return isValidConnection;
}
