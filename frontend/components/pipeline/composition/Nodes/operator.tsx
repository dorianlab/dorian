import React, { useState } from "react";
import { Position, useReactFlow } from "@xyflow/react";
import HandleRenderer from "./HandleRenderer";
import { OperatorProps } from "@/types/ui";
import { useUIStore } from "@/store/ui";
import NodeWrapper, { inferStatus } from "./wrapper";
import { useNodeHandles } from "@/hooks/useNodeHandles";
import { Eye, ScanSearch } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { useModelTracingStore } from "@/store/model-tracing";

/** Minimum pixels per handle so labels don't overlap. */
const PX_PER_HANDLE = 48;
const MIN_NODE_W = 120;

/**
 * Estimate the minimum node width needed so that handle labels don't collide.
 * When handles have long names (e.g. "frequency_penalty"), their vertical text
 * labels occupy ~16px each but the handle dots need enough horizontal room so
 * the labels don't pile on top of each other.
 *
 * The width is the max of:
 *   1. The operator name text (rough 8px/char + padding)
 *   2. handleCount × PX_PER_HANDLE (dot spacing)
 *   3. A minimum of MIN_NODE_W
 */
function computeMinWidth(
  name: string,
  handleCount: number,
): number {
  const nameWidth = name.length * 8 + 40; // title text estimate
  const handleWidth = handleCount * PX_PER_HANDLE;
  return Math.max(MIN_NODE_W, nameWidth, handleWidth);
}

function OperatorNode({ data }: OperatorProps) {
  const { deleteElements, getNodes } = useReactFlow();
  const { direction } = useUIStore();
  const isTB = direction === "TB";
  const [inspectOpen, setInspectOpen] = useState(false);

  const { sources, targets } = useNodeHandles({
    nodeId: data.uuid,
    outputs: data.outputs,
    inputs: data.inputs,
    isNewNode: data.isNewNode,
  });

  const handleCount = Math.max(sources.length, targets.length, 1);
  const minWidth = computeMinWidth(data.name ?? "", handleCount);

  // Compound operator internals (populated by state/group-created WS event)
  const childEntries = Object.entries(data.children ?? {});
  const internalEdges = data.internalEdges ?? [];
  const ioEntries = Object.entries(data.ioMap ?? {});
  const isCompound = childEntries.length > 0;

  // Model tracing: eye button opens fly-in modal with trace output
  const isTracer = !!data.isTracer;
  const hasTraceOutput = data.output?.type === "trace_output";
  const openTracing = useModelTracingStore((s) => s.open);

  return (
    <>
      <NodeWrapper
        title={data.name}
        status={inferStatus(data)}
        errorMessage={data.execError}
        errorTrace={data.execTrace}
        startTime={data.execStartTime}
        duration={data.execDuration}
        style={{ width: minWidth, minWidth }}
        onDelete={() => {
          const childIds = getNodes()
            .filter((n: any) => n.data?.compoundGroupId === data.uuid)
            .map((n) => ({ id: n.id }));
          deleteElements({ nodes: [{ id: data.uuid }, ...childIds] });
        }}
      >
        {/* Eye button for compound operators */}
        {isCompound && (
          <button
            className="nodrag absolute -top-2 -right-2 bg-zinc-500/80 hover:bg-zinc-600 text-white rounded-full w-5 h-5 flex items-center justify-center transition-colors z-10"
            title="View internal structure"
            onClick={(e) => {
              e.stopPropagation();
              setInspectOpen(true);
            }}
          >
            <Eye size={10} />
          </button>
        )}

        {/* Eye button for model tracing operators */}
        {isTracer && (
          <button
            className={`nodrag absolute -top-2 ${isCompound ? "-right-8" : "-right-2"} bg-indigo-500/80 hover:bg-indigo-600 text-white rounded-full w-5 h-5 flex items-center justify-center transition-colors z-10 ${!hasTraceOutput ? "opacity-50 cursor-not-allowed" : ""}`}
            title={hasTraceOutput ? "View trace output" : "Run pipeline to generate traces"}
            onClick={(e) => {
              e.stopPropagation();
              if (hasTraceOutput) openTracing(data.uuid, data.output);
            }}
          >
            <ScanSearch size={10} />
          </button>
        )}

        {/* OUTPUTS => sources */}
        <HandleRenderer
          items={sources}
          type='source'
          position={isTB ? Position.Bottom : Position.Right}
          nodeType='operator'
        />

        {/* INPUTS => targets */}
        <HandleRenderer
          items={targets}
          type='target'
          position={isTB ? Position.Top : Position.Left}
          nodeType='operator'
        />
      </NodeWrapper>

      {/* Internal structure dialog for compound operators */}
      {isCompound && (
        <Dialog open={inspectOpen} onOpenChange={setInspectOpen}>
          <DialogContent className="w-[90vw] sm:max-w-[90vw] max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <Eye className="h-4 w-4" />
                {data.name} — Internal Structure
              </DialogTitle>
              <DialogDescription>
                Read-only view of the collapsed method chain and IO mapping.
              </DialogDescription>
            </DialogHeader>

            <Separator />

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {/* Method Nodes */}
              <div>
                <h4 className="text-sm font-semibold mb-2">
                  Method Nodes ({childEntries.length})
                </h4>
                <ScrollArea className="h-[50vh] rounded-md border p-3 bg-muted/30">
                  <div className="space-y-2">
                    {childEntries.map(([id, child]) => (
                      <div
                        key={id}
                        className="flex flex-col gap-0.5 px-3 py-2 rounded-md border bg-background"
                      >
                        <span className="text-xs font-medium">{child.name}</span>
                        <span className="text-[10px] text-muted-foreground font-mono truncate">
                          {id.split("_cx_").pop()}
                        </span>
                        <span className="text-[10px] text-muted-foreground">
                          {child.class_type}
                        </span>
                      </div>
                    ))}
                  </div>
                </ScrollArea>
              </div>

              {/* Internal Edges */}
              <div>
                <h4 className="text-sm font-semibold mb-2">
                  Internal Edges ({internalEdges.length})
                </h4>
                <ScrollArea className="h-[50vh] rounded-md border p-3 bg-muted/30">
                  <div className="space-y-2">
                    {internalEdges.map((edge, i) => (
                      <div
                        key={i}
                        className="flex items-center gap-2 px-3 py-2 rounded-md border bg-background text-xs font-mono"
                      >
                        <span className="truncate">
                          {(edge.source ?? "").split("_cx_").pop()}
                        </span>
                        <span className="text-muted-foreground shrink-0">→</span>
                        <span className="truncate">
                          {(edge.destination ?? "").split("_cx_").pop()}
                        </span>
                        <span className="ml-auto text-muted-foreground shrink-0">
                          pos: {String(edge.position)}
                        </span>
                      </div>
                    ))}
                  </div>
                </ScrollArea>
              </div>

              {/* IO Mapping */}
              <div>
                <h4 className="text-sm font-semibold mb-2">
                  IO Mapping ({ioEntries.length})
                </h4>
                <ScrollArea className="h-[50vh] rounded-md border p-3 bg-muted/30">
                  <div className="space-y-2">
                    {ioEntries.map(([handle, mapping]) => (
                      <div
                        key={handle}
                        className="flex items-center gap-2 px-3 py-2 rounded-md border bg-background text-xs font-mono"
                      >
                        <span
                          className={
                            mapping.direction === "input"
                              ? "text-blue-500 shrink-0"
                              : "text-emerald-500 shrink-0"
                          }
                        >
                          {mapping.direction === "input" ? "IN" : "OUT"}
                        </span>
                        <span className="font-medium truncate">{handle}</span>
                        <span className="ml-auto text-muted-foreground truncate text-right">
                          {mapping.internalNodeId?.split("_cx_").pop()}:
                          {String(mapping.internalHandle)}
                        </span>
                      </div>
                    ))}
                  </div>
                </ScrollArea>
              </div>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

export default React.memo(OperatorNode);
