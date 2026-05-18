import React, { useState } from "react";
import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  useReactFlow,
  type EdgeProps,
} from "@xyflow/react";
import { X } from "lucide-react";

export default function CustomEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  style = {},
  markerEnd,
}: EdgeProps) {
  const { setEdges } = useReactFlow();
  const [hovered, setHovered] = useState(false);

  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  const onEdgeDelete = (e: React.MouseEvent) => {
    e.stopPropagation(); // prevent canvas pan/zoom
    setEdges((edges) => edges.filter((edge) => edge.id !== id));
  };

  return (
    <>
      {/* 1. The main edge */}
      <BaseEdge path={edgePath} markerEnd={markerEnd} style={style} />

      {/* 2. Transparent path to detect hover */}
      <path
        d={edgePath}
        fill='none'
        stroke='transparent'
        strokeWidth={20}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
      />

      {/* 3. Delete icon in center, only shown on hover */}
      {hovered && (
        <EdgeLabelRenderer>
          <div
            onMouseOver={() => setHovered(true)}
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: "auto",
            }}
            className='bg-card p-1 rounded-full border border-border shadow hover:bg-red-100 dark:hover:bg-red-950/50 cursor-pointer'
            onClick={onEdgeDelete}
          >
            <X size={12} className='text-red-600' />
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
