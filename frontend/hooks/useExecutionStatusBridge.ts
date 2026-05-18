/**
 * useExecutionStatusBridge
 * -------------------------
 * Subscribes to usePipelineRunStore and patches ReactFlow node data with the
 * current execution status (+ error/trace for failed nodes) so that
 * NodeWrapper renders the correct border color / glow and the error popover
 * can display failure details on hover.
 *
 * Must be called from a component that has access to `updateNodeData`.
 */
import { useEffect, useRef } from "react";
import { useReactFlow } from "@xyflow/react";
import { usePipelineRunStore } from "@/store/pipeline-run";
import { runStatusToNodeStatus } from "@/components/pipeline/composition/Nodes/wrapper";
import type { UpdateNodeData } from "@/hooks/usePipelineComposition";
import type { NodeRunState } from "@/types/pipeline";

/** Serialise the fields we care about into a single string for cheap diffing. */
function fingerprint(ns: NodeRunState): string {
  const outputKey = ns.output != null ? "has_output" : "";
  return `${ns.status}|${ns.error ?? ""}|${ns.trace ?? ""}|${ns.start_time ?? ""}|${ns.duration ?? ""}|${outputKey}`;
}

/**
 * Map an internal sub-DAG node ID back to its canvas-level parent.
 * Compound expansion produces IDs like `<uuid>_cx_init`, `<uuid>_cx_call_0`.
 * Printout expansion produces `printout_<uuid>`.
 * The canvas only knows the original `<uuid>`.
 */
function toCanvasId(nodeId: string): string {
  if (nodeId.startsWith("printout_")) return nodeId.slice("printout_".length);
  const idx = nodeId.indexOf("_cx_");
  return idx !== -1 ? nodeId.slice(0, idx) : nodeId;
}

export function useExecutionStatusBridge(updateNodeData: UpdateNodeData) {
  const { getNodes } = useReactFlow();
  // Track previous fingerprint per node so we only patch on actual changes,
  // avoiding unnecessary re-renders and snapshot emissions.
  const prevRef = useRef<Record<string, string>>({});

  useEffect(() => {
    const unsub = usePipelineRunStore.subscribe((state) => {
      const pipelineRun = state.pipelineRun;

      if (!pipelineRun) {
        // Run cleared — reset all tracked nodes to idle.
        const prev = prevRef.current;
        if (Object.keys(prev).length === 0) return;
        for (const nodeId of Object.keys(prev)) {
          updateNodeData(nodeId, {
            status: undefined,
            execError: undefined,
            execTrace: undefined,
            execStartTime: undefined,
            execDuration: undefined,
          });
        }
        prevRef.current = {};
        return;
      }

      const nodeStates = pipelineRun.node_states;

      // Run-level failure (expansion/graph-build error): the backend marks the
      // run as failed but individual nodes stay pending/skipped — none carry the
      // actual error.  Propagate the run-level error to every node so that
      // NodeWrapper renders red borders and the error popover shows details.
      if (pipelineRun.status === "failed" && pipelineRun.error) {
        const hasAnyNodeFailed = Object.values(nodeStates).some(
          (ns) => ns.status === "failed",
        );
        if (!hasAnyNodeFailed) {
          const prev = prevRef.current;
          const runErrFp = `run-error|${pipelineRun.error}`;
          const nextPrev: Record<string, string> = {};

          // When expansion fails early, node_states may be empty — fall back
          // to the current canvas nodes so every visible node gets a red border.
          const nodeIds =
            Object.keys(nodeStates).length > 0
              ? Object.keys(nodeStates)
              : getNodes().map((n) => n.id);

          for (const rawId of nodeIds) {
            const cid = toCanvasId(rawId);
            nextPrev[cid] = runErrFp;
            if (prev[cid] !== runErrFp) {
              updateNodeData(cid, {
                status: runStatusToNodeStatus("failed"),
                execError: pipelineRun.error,
                execTrace: undefined,
                execStartTime: undefined,
                execDuration: undefined,
              });
            }
          }
          prevRef.current = nextPrev;
          return;
        }
      }
      const prev = prevRef.current;

      // Collapse sub-DAG node states (_cx_init, _cx_call_0, …) to their
      // canvas-level parent.  When multiple sub-nodes map to the same
      // canvas node, the worst status wins (failed > running > others).
      const STATUS_PRIORITY: Record<string, number> = {
        failed: 3,
        running: 2,
        success: 1,
        skipped: 0,
        pending: 0,
      };

      const merged: Record<string, NodeRunState> = {};
      for (const [rawId, ns] of Object.entries(nodeStates)) {
        const cid = toCanvasId(rawId);
        const existing = merged[cid];
        if (
          !existing ||
          (STATUS_PRIORITY[ns.status] ?? 0) >
            (STATUS_PRIORITY[existing.status] ?? 0)
        ) {
          // Preserve output from a previously merged entry (e.g. trace-output
          // arrives on the parent ID while status comes from a _cx_ sub-node).
          const preservedOutput = existing?.output;
          merged[cid] = ns.output != null ? ns : { ...ns, ...(preservedOutput != null ? { output: preservedOutput } : {}) };
        } else if (ns.output != null && existing && existing.output == null) {
          // A lower-priority entry carries output — attach it to the winner.
          existing.output = ns.output;
        }
      }

      const nextPrev: Record<string, string> = {};

      for (const [nodeId, ns] of Object.entries(merged)) {
        const fp = fingerprint(ns);
        nextPrev[nodeId] = fp;

        if (prev[nodeId] !== fp) {
          const patch: Record<string, unknown> = {
            status: runStatusToNodeStatus(ns.status),
            execError: ns.error ?? undefined,
            execTrace: ns.trace ?? undefined,
            execStartTime: ns.start_time ?? undefined,
            execDuration: ns.duration ?? undefined,
          };
          // Inject inline output for printout/visualizer nodes
          if (ns.output != null) {
            patch.output = ns.output;
          }
          updateNodeData(nodeId, patch);
        }
      }

      // Reset nodes from previous run that are absent in current run
      for (const nodeId of Object.keys(prev)) {
        if (!(nodeId in merged) && !(nodeId in nextPrev)) {
          updateNodeData(nodeId, {
            status: undefined,
            execError: undefined,
            execTrace: undefined,
            execStartTime: undefined,
            execDuration: undefined,
          });
        }
      }

      prevRef.current = nextPrev;
    });

    return unsub;
  }, [updateNodeData, getNodes]);
}
