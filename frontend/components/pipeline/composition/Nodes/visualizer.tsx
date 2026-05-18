"use client";

import * as React from "react";
import { useMemo, useState, useEffect, useCallback } from "react";
import { useReactFlow, Position } from "@xyflow/react";
import HandleRenderer from "./HandleRenderer";
import {
  Eye,
  Image as ImageIcon,
  FileText,
  TerminalSquare,
} from "lucide-react";
import { EdgeLike } from "@/types/pipeline";

// shadcn
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { useUIStore } from "@/store/ui";
import NodeWrapper, { inferStatus } from "./wrapper";

type VisualizerKind = "auto" | "text" | "json" | "image" | "logs";

export interface VisualizerProps {
  data: {
    uuid: string;
    name: string;
    isNewNode?: boolean;

    // optional ports
    inputs?: any[];
    outputs?: any[];

    // visualizer specific
    kind?: VisualizerKind;

    /**
     * The "viewable output".
     * - string => text/logs
     * - object/array => json
     * - { type:"image", src, alt? } => image
     */
    output?: any;

    // Execution status bridge fields
    status?: string;
    execError?: string;
    execTrace?: string;

    updateNodeData?: (
      nodeId: string,
      patch:
        | Record<string, any>
        | ((prevData: Record<string, any>) => Record<string, any>),
    ) => void;
  };
}

function isImagePayload(
  v: any,
): v is { type: "image"; src: string; alt?: string } {
  return (
    !!v &&
    typeof v === "object" &&
    v.type === "image" &&
    typeof v.src === "string"
  );
}
function isTypedPayload(
  v: any,
): v is { type: string; value?: any; content?: any } {
  return (
    !!v &&
    typeof v === "object" &&
    typeof v.type === "string" &&
    ("value" in v || "content" in v)
  );
}

function inferKind(
  kind: VisualizerKind | undefined,
  output: any,
): VisualizerKind {
  if (kind && kind !== "auto") return kind;

  if (isImagePayload(output)) return "image";
  if (isTypedPayload(output)) {
    if (output.type === "image") return "image";
    if (output.type === "json" || output.type === "llm_response" || output.type === "dataframe" || output.type === "array" || output.type === "scalar") return "json";
    if (output.type === "logs") return "logs";
    if (output.type === "text") return "text";
  }

  if (typeof output === "string") return "text";
  if (typeof output === "object") return "json";
  return "text";
}

function normalizeValue(kind: VisualizerKind, output: any) {
  if (isImagePayload(output))
    return { imageSrc: output.src, imageAlt: output.alt ?? "Output" };

  if (isTypedPayload(output)) {
    const payload = output.value ?? output.content;
    if (output.type === "image")
      return {
        imageSrc: payload?.src ?? "",
        imageAlt: payload?.alt ?? "Output",
      };
    return { value: payload };
  }

  if (kind === "image") {
    if (typeof output === "string")
      return { imageSrc: output, imageAlt: "Output" };
    return { imageSrc: output?.src ?? "", imageAlt: output?.alt ?? "Output" };
  }

  return { value: output };
}

export default function VisualizerNode({ data }: VisualizerProps) {
  const { direction } = useUIStore();
  const { deleteElements, getEdges } = useReactFlow();

  const [open, setOpen] = useState(false);
  const [kindDraft, setKindDraft] = useState<VisualizerKind>(
    data.kind ?? "auto",
  );

  useEffect(() => {
    setKindDraft(data.kind ?? "auto");
  }, [data.kind]);

  const edges = getEdges();
  const isTB = direction === "TB";
  const isNewNode = data.isNewNode;

  const sourcesFromIO: EdgeLike[] = (data.outputs ?? []).map((_, i) => ({
    source: data.uuid,
    target: "__pending__",
    position: i,
    output: i,
  }));
  const targetsFromIO: EdgeLike[] = (data.inputs ?? []).map((_, i) => ({
    source: "__pending__",
    target: data.uuid,
    position: i,
    output: i,
  }));
  const sourcesFromEdges = edges.filter(
    (e) => e.source === data.uuid,
  ) as unknown as EdgeLike[];
  const targetsFromEdges = edges.filter(
    (e) => e.target === data.uuid,
  ) as unknown as EdgeLike[];

  let sources: EdgeLike[] =
    data.outputs && data.outputs.length > 0 ? sourcesFromIO : sourcesFromEdges;
  let targets: EdgeLike[] =
    data.inputs && data.inputs.length > 0 ? targetsFromIO : targetsFromEdges;

  if (isNewNode && targets.length === 0) {
    targets = [
      { source: "__pending__", target: data.uuid, position: 0, output: 0 },
    ];
  }

  const handleDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    deleteElements({ nodes: [{ id: data.uuid }] });
  };

  const effectiveKind = useMemo(
    () => inferKind(kindDraft, data.output),
    [kindDraft, data.output],
  );
  const normalized = useMemo(
    () => normalizeValue(effectiveKind, data.output),
    [effectiveKind, data.output],
  );

  const headerIcon = useMemo(() => {
    switch (effectiveKind) {
      case "image":
        return <ImageIcon className='h-4 w-4' />;
      case "json":
        return <FileText className='h-4 w-4' />;
      case "logs":
        return <TerminalSquare className='h-4 w-4' />;
      default:
        return <Eye className='h-4 w-4' />;
    }
  }, [effectiveKind]);

  const setKind = useCallback(
    (k: VisualizerKind) => {
      setKindDraft(k);
      data.updateNodeData?.(data.uuid, { kind: k });
    },
    [data],
  );

  return (
    <>
      <NodeWrapper
        title={undefined}
        status={inferStatus(data)}
        errorMessage={data.execError}
        errorTrace={data.execTrace}
        onDelete={handleDelete}
        onClick={() => setOpen(true)}
        className='px-10 py-5 cursor-pointer relative'
      >
        <div className='flex items-center gap-1.5 px-3 pb-1 text-sm font-medium'>
          <Eye className='h-3.5 w-3.5 absolute top-1 left-1 text-muted-foreground' />
          {data.name ?? "Visualizer"}
        </div>
        <HandleRenderer
          items={targets}
          nodeType='visualizer'
          type='target'
          position={isTB ? Position.Top : Position.Left}
        />
        {sources?.length > 0 && (
          <HandleRenderer
            type='source'
            items={sources}
            nodeType='visualizer'
            position={isTB ? Position.Bottom : Position.Right}
          />
        )}
      </NodeWrapper>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className='w-[90vw] sm:max-w-[90vw] max-h-[90vh] overflow-y-auto'>
          <DialogHeader>
            <DialogTitle className='flex items-center gap-2'>
              {headerIcon}
              {data.name ?? "Visualizer"}
            </DialogTitle>
            <DialogDescription>
              Inspect pipeline output. Supports text, JSON, images, and logs.
            </DialogDescription>
          </DialogHeader>

          <Separator />

          {data.output == null && (
            <div className='rounded-md border border-dashed border-muted-foreground/30 bg-muted/20 p-4 text-center text-sm text-muted-foreground'>
              No output yet — run the pipeline to see results here.
            </div>
          )}

          <Tabs
            defaultValue={effectiveKind}
            value={effectiveKind}
            onValueChange={(v) => setKind(v as VisualizerKind)}
          >
            <TabsList className='grid w-full grid-cols-4'>
              <TabsTrigger value='text'>Text</TabsTrigger>
              <TabsTrigger value='json'>JSON</TabsTrigger>
              <TabsTrigger value='image'>Image</TabsTrigger>
              <TabsTrigger value='logs'>Logs</TabsTrigger>
            </TabsList>

            <TabsContent value='text' className='mt-4'>
              <ScrollArea className='h-[70vh] rounded-md border p-4 bg-muted/30'>
                <pre className='whitespace-pre-wrap break-all text-sm'>
                  {typeof normalized.value === "string"
                    ? normalized.value
                    : normalized.value == null
                      ? "No text output"
                      : String(normalized.value)}
                </pre>
              </ScrollArea>
            </TabsContent>

            <TabsContent value='logs' className='mt-4'>
              <ScrollArea className='h-[70vh] rounded-md border p-4 bg-black text-white'>
                <pre className='whitespace-pre-wrap break-all text-xs leading-relaxed'>
                  {typeof normalized.value === "string"
                    ? normalized.value
                    : normalized.value == null
                      ? "No logs"
                      : JSON.stringify(normalized.value, null, 2)}
                </pre>
              </ScrollArea>
            </TabsContent>

            <TabsContent value='json' className='mt-4'>
              <ScrollArea className='h-[70vh] rounded-md border p-4 bg-muted/30'>
                <pre className='whitespace-pre-wrap break-all text-sm'>
                  {normalized.value == null
                    ? "No JSON output"
                    : JSON.stringify(normalized.value, null, 2)}
                </pre>
              </ScrollArea>
            </TabsContent>

            <TabsContent value='image' className='mt-4'>
              <div className='h-[65vh] rounded-md border bg-muted/30 flex items-center justify-center overflow-hidden'>
                {normalized.imageSrc ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={normalized.imageSrc}
                    alt={normalized.imageAlt ?? "Output"}
                    className='max-h-[65vh] max-w-full object-contain'
                  />
                ) : (
                  <div className='text-sm text-muted-foreground'>
                    No image output
                  </div>
                )}
              </div>
            </TabsContent>

          </Tabs>
        </DialogContent>
      </Dialog>
    </>
  );
}
