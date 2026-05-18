import React, { useState } from "react";
import { Position, useReactFlow } from "@xyflow/react";
import HandleRenderer from "./HandleRenderer";
import { useUIStore } from "@/store/ui";
import NodeWrapper, { inferStatus } from "./wrapper";
import { useNodeHandles } from "@/hooks/useNodeHandles";
import type { IOMapping } from "@/types/pipeline";
import { Eye } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";

/** Minimum pixels per handle so labels don't overlap. */
const PX_PER_HANDLE = 48;
const MIN_NODE_W = 120;

function computeMinWidth(name: string, handleCount: number): number {
  const nameWidth = name.length * 8 + 40;
  const handleWidth = handleCount * PX_PER_HANDLE;
  return Math.max(MIN_NODE_W, nameWidth, handleWidth);
}

export interface GroupProps {
  data: {
    uuid: string;
    name: string;
    collapsed: boolean;
    ioMap: Record<string, IOMapping>;
    children: Record<string, any>;
    internalEdges?: Array<{
      source: string;
      destination: string;
      position: number | string;
      output: number;
    }>;
    sourceInterface?: string;
    execError?: string;
    execTrace?: string;
    execStartTime?: number;
    execDuration?: number;
    inputs?: Array<{ name: string; type?: string }>;
    outputs?: Array<{ name: string; type?: string }>;
    isNewNode?: boolean;
    updateNodeData?: any;
    [key: string]: any;
  };
}

function GroupNode({ data }: GroupProps) {
  const { deleteElements, getNodes } = useReactFlow();
  const { direction } = useUIStore();
  const isTB = direction === "TB";
  const [open, setOpen] = useState(false);

  const { sources, targets } = useNodeHandles({
    nodeId: data.uuid,
    outputs: data.outputs,
    inputs: data.inputs,
    isNewNode: data.isNewNode,
  });

  const handleCount = Math.max(sources.length, targets.length, 1);

  // In TB layout handles sit along horizontal edges → need width.
  // In LR layout handles sit along vertical edges → need height.
  const minWidth = isTB
    ? computeMinWidth(data.name ?? "", handleCount)
    : computeMinWidth(data.name ?? "", 1);
  const minHeight = isTB ? undefined : handleCount * PX_PER_HANDLE;

  const childEntries = Object.entries(data.children ?? {});
  const internalEdges = data.internalEdges ?? [];
  const ioEntries = Object.entries(data.ioMap ?? {});

  return (
    <>
      <NodeWrapper
        title={data.name}
        status={inferStatus(data)}
        errorMessage={data.execError}
        errorTrace={data.execTrace}
        startTime={data.execStartTime}
        duration={data.execDuration}
        style={{ width: minWidth, minWidth, minHeight }}
        onDelete={() => {
          const childIds = getNodes()
            .filter((n: any) => n.data?.compoundGroupId === data.uuid)
            .map((n) => ({ id: n.id }));
          deleteElements({ nodes: [{ id: data.uuid }, ...childIds] });
        }}
      >
        {/* Eye button to inspect internals */}
        {childEntries.length > 0 && (
          <button
            className="absolute -top-2 -right-2 bg-zinc-500/80 hover:bg-zinc-600 text-white rounded-full w-5 h-5 flex items-center justify-center transition-colors"
            title="View internal structure"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(true);
            }}
          >
            <Eye size={10} />
          </button>
        )}

        <HandleRenderer
          items={sources}
          type="source"
          position={isTB ? Position.Bottom : Position.Right}
          nodeType="operator"
        />

        <HandleRenderer
          items={targets}
          type="target"
          position={isTB ? Position.Top : Position.Left}
          nodeType="operator"
        />
      </NodeWrapper>

      {/* Internal structure dialog (same layout as dorian.io.printout) */}
      <Dialog open={open} onOpenChange={setOpen}>
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
                      <span className="text-xs font-medium">
                        {child.name}
                      </span>
                      <span className="text-[10px] text-muted-foreground font-mono truncate">
                        {id.split("_cx_").pop()}
                      </span>
                      <span className="text-[10px] text-muted-foreground">
                        {child.class_type}
                      </span>
                    </div>
                  ))}
                  {childEntries.length === 0 && (
                    <p className="text-xs text-muted-foreground">
                      No internal nodes yet.
                    </p>
                  )}
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
                  {internalEdges.map((edge: any, i: number) => (
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
                  {internalEdges.length === 0 && (
                    <p className="text-xs text-muted-foreground">
                      No internal edges yet.
                    </p>
                  )}
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
                  {ioEntries.length === 0 && (
                    <p className="text-xs text-muted-foreground">
                      No IO mapping yet.
                    </p>
                  )}
                </div>
              </ScrollArea>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

export default React.memo(GroupNode);
