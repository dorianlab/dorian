"use client";
import React, { useState } from "react";
import { useReactFlow, Position } from "@xyflow/react";
import { Code } from "lucide-react";
import HandleRenderer from "./HandleRenderer";
import CodeViewer from "@/components/ui/code-viewer";
import type { SnippetProps } from "@/types/ui";
import { useUIStore } from "@/store/ui";
import NodeWrapper, { inferStatus } from "./wrapper";
import { useNodeHandles } from "@/hooks/useNodeHandles";

function SnippetNode({ data }: SnippetProps) {
  const { direction } = useUIStore();
  const { deleteElements } = useReactFlow();
  const [showCodeViewer, setShowCodeViewer] = useState(false);

  const handleDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    deleteElements({ nodes: [{ id: data.uuid }] });
  };

  const isTB = direction === "TB";

  const { sources, targets } = useNodeHandles({
    nodeId: data.uuid,
    outputs: data.outputs,
    inputs: data.inputs,
    isNewNode: data.isNewNode,
  });

  return (
    <NodeWrapper
      title={data.name}
      status={inferStatus(data)}
      errorMessage={data.execError}
      errorTrace={data.execTrace}
      startTime={data.execStartTime}
      duration={data.execDuration}
      onDelete={handleDelete}
      className='px-10 py-5 !cursor-pointer'
    >
      <Code
        onClick={() => setShowCodeViewer(true)}
        className='h-3 w-3 absolute top-3 right-3 cursor-pointer opacity-60 hover:opacity-100 transition-opacity'
      />
      <HandleRenderer
        type='source'
        items={sources}
        position={isTB ? Position.Bottom : Position.Right}
        nodeType='snippet'
      />
      <HandleRenderer
        items={targets}
        type='target'
        position={isTB ? Position.Top : Position.Left}
        nodeType='snippet'
      />
      <CodeViewer
        code={data.code || ""}
        language={data.language || "python"}
        show={showCodeViewer}
        setShow={setShowCodeViewer}
      />
    </NodeWrapper>
  );
}

export default React.memo(SnippetNode);
