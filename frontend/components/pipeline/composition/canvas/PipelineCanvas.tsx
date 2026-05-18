"use client";

import React from "react";
import { ReactFlow, Background, Controls, MiniMap } from "@xyflow/react";
import { useTheme } from "next-themes";

export function PipelineCanvas({
  wrapperRef,
  nodeTypes,
  edgeTypes,
  nodes,
  edges,
  onNodesChange,
  onEdgesChange,
  onConnect,
  onDrop,
  onDragOver,
  onDragStart,
  isValidConnection,
}: any) {
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";

  return (
    <div
      ref={wrapperRef}
      className='relative flex flex-row h-[90vh] w-full bg-gray-100 dark:bg-gray-900 rounded-[10px]'
    >
      <div className='reactflow-wrapper'>
        <ReactFlow
          colorMode={isDark ? "dark" : "light"}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          nodes={nodes}
          edges={edges}
          fitView
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onDrop={onDrop}
          onDragStart={onDragStart}
          onDragOver={onDragOver}
          isValidConnection={isValidConnection}
        >
          <Background />
          <Controls />
          <MiniMap
            nodeColor={isDark ? "#374151" : "#d1d5db"}
            maskColor={isDark ? "rgba(17,24,39,0.6)" : "rgba(243,244,246,0.6)"}
          />
        </ReactFlow>
      </div>
    </div>
  );
}
