"use client";

import React, { useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
  useEdgesState,
  Background,
  Controls,
} from "@xyflow/react";
import Dagre from "@dagrejs/dagre";
import OperatorNode from "@/components/pipeline/composition/Nodes/operator";
import ParameterNode from "@/components/pipeline/composition/Nodes/parameter";
import SnippetNode from "@/components/pipeline/composition/Nodes/snippet";
import LabeledEdge from "@/components/pipeline/composition/Edges/LabeledEdge";
import { Node as FlowNode, Edge as FlowEdge } from "@/types/pipeline";
import "@xyflow/react/dist/style.css";
import { useRecommendationEngineStore } from "@/store/recommendation-engine";
import clsx from "clsx";

import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ThumbsUp, ThumbsDown, CheckCircle2 } from "lucide-react";
import { ws } from "@/helpers/ws-events";
import { useUIStore } from "@/store/ui";

export interface Node extends FlowNode {
  code: string;
  name: string;
  measured?: { width: number; height: number };
}
export interface Edge extends FlowEdge {
  id: string;
}

const DEFAULT_W = 200;
const DEFAULT_H = 60;

const getLayoutedElements = (
  nodes: Node[],
  edges: Edge[],
  options: { direction: "TB" | "LR" | "RL" | "BT"; compact?: boolean } = { direction: "TB" },
) => {
  const g = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: options.direction,
    nodesep: options.compact ? 20 : 40,
    ranksep: options.compact ? 30 : 80,
    marginx: options.compact ? 10 : 20,
    marginy: options.compact ? 10 : 20,
  });

  edges.forEach((edge) => g.setEdge(edge.source, edge.target));
  nodes.forEach((node) => {
    const w = node.measured?.width  ?? DEFAULT_W;
    const h = node.measured?.height ?? DEFAULT_H;
    g.setNode(node.id, { ...node, width: w, height: h });
  });

  Dagre.layout(g);

  return {
    nodes: nodes.map((node) => {
      const position = g.node(node.id);
      const w = node.measured?.width  ?? DEFAULT_W;
      const h = node.measured?.height ?? DEFAULT_H;
      const x = position.x - w / 2;
      const y = position.y - h / 2;
      return { ...node, position: { x, y } };
    }),
    edges,
  };
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- accepts multiple pipeline shapes (Pipeline, PipelineDraft, raw objects)
type PipelineData = Record<string, any>;

type PipelineRendererProps = {
  index?: number;
  data: PipelineData;
  className?: string;
  isSmallCard?: boolean;
  /** When false, hides the upvote/downvote/view/select action buttons. Default: true */
  showActions?: boolean;
  onClick?: (pipeline: PipelineData) => void;
};

function PipelineRendererCard({
  isSmallCard = false,
  data,
  className,
  index = 0,
  showActions = true,
  onClick,
}: PipelineRendererProps) {
  const pipelineData = data || {};

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const { direction } = useUIStore();

  const [vote, setVote] = useState<"up" | "down" | null>(null);

  const nodeTypes = useMemo(
    () => ({
      operator: OperatorNode,
      parameter: ParameterNode,
      snippet: SnippetNode,
    }),
    [],
  );

  const edgeTypes = useMemo(
    () => ({
      default: LabeledEdge,
      standard: LabeledEdge,
      labeled: LabeledEdge,
    }),
    [],
  );

  useEffect(() => {
    if (!pipelineData?.nodes || !pipelineData?.edges) return;

    // RL-generated / docstore-sourced pipelines use `class_type` (e.g. "Parameter")
    // while frontend-created pipelines use `type` (e.g. "parameter").
    const CT_MAP: Record<string, string> = {
      Parameter: "parameter",
      Operator: "operator",
      Snippet: "snippet",
      Group: "group",
    };

    const rfNodes: Node[] = Object.entries(
      pipelineData.nodes as Record<string, Node>,
    ).map(([id, node]) => {
      const rawType =
        node?.type ??
        CT_MAP[node?.class_type ?? ""] ??
        node?.class_type?.toLowerCase() ??
        "operator";
      return {
        id,
        type: rawType.toLowerCase(),
        position: { x: 0, y: 0 },
        code: node.code,
        name: node.name,
        data: { label: node.name || node.code || id, uuid: id, ...node },
      };
    });

    const rfEdges: Edge[] = pipelineData.edges.map((edge: Record<string, unknown>, i: number) => ({
      id: `e${String(edge.source)}-${String(edge.destination)}-${i}`,
      source: String(edge.source ?? ""),
      target: String(edge.destination ?? edge.target ?? ""),
      output: edge.output != null ? String(edge.output) : undefined,
      position: edge.position != null ? String(edge.position) : undefined,
      sourceHandle: edge.output != null ? String(edge.output) : undefined,
      targetHandle: edge.position != null ? String(edge.position) : undefined,
      type: "labeled",
    }));

    const layouted = getLayoutedElements(rfNodes, rfEdges, {
      direction: direction as "TB" | "LR" | "RL" | "BT",
      compact: isSmallCard,
    });

    setNodes(layouted.nodes);
    setEdges(layouted.edges);
  }, [pipelineData, direction, setNodes, setEdges]);

  const pipelineId =
    pipelineData?.uuid ||
    pipelineData?.id ||
    pipelineData?.pipelineId ||
    `rec-${index}`;

  /** Card click — open this pipeline in the composition canvas (with AI debugger). */
  const handleOpenInCanvas = () => {
    onClick?.(pipelineData);
  };

  /** Select button — mark as the preferred candidate for the next recommendation round. */
  const handleSelect = (e: React.MouseEvent) => {
    e.stopPropagation();
    // Emit selection with the full pipeline body so the backend can save
    // it to session meta as the "working pipeline" for future recommendations.
    ws.pipelineRecommendationSelected({
      pipelineId,
      recommendationId: pipelineId,
      name: pipelineData?.name ?? `Recommendation ${index + 1}`,
      pipeline: pipelineData,
    });
  };

  const handleUpvote = () => {
    setVote((v) => (v === "up" ? null : "up"));
    ws.pipelineRecommendationUpvoted({
      pipelineId,
      recommendationId: pipelineId,
    });
  };

  const handleDownvote = () => {
    setVote((v) => (v === "down" ? null : "down"));
    ws.pipelineRecommendationDownvoted({
      pipelineId,
      recommendationId: pipelineId,
    });
  };

  return (
    <Card
      className={clsx(
        "flex !p-2 gap-2 flex-col overflow-hidden",
        isSmallCard && "cursor-pointer",
        className,
      )}
      onClick={isSmallCard ? handleOpenInCanvas : undefined}
    >
      <CardContent className={clsx("p-0! h-full flex-1")}>
        <div className='rounded-md relative border bg-muted/40 h-full flex-1'>
          {/* pointer-events-none on small cards so clicks fall through to Card */}
          <div className={clsx(isSmallCard && "pointer-events-none", "h-full")}>
            <ReactFlow
              id={pipelineId + "card"}
              key={pipelineId + "card"}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={!isSmallCard}
              zoomOnScroll={!isSmallCard}
              panOnScroll={!isSmallCard}
              zoomOnPinch={!isSmallCard}
              zoomOnDoubleClick={!isSmallCard}
              fitView
            >
              <Background />
              {!isSmallCard && <Controls showInteractive={false} />}
            </ReactFlow>
          </div>

          {showActions && <div className='flex absolute bottom-2 right-2 w-fit items-center gap-1 pointer-events-auto' onClick={(e) => e.stopPropagation()}>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant={vote === "up" ? "default" : "outline"}
                    size='sm'
                    className='gap-1 w-fit cursor-pointer'
                    onClick={(e) => { e.stopPropagation(); handleUpvote(); }}
                  >
                    <ThumbsUp className='h-3 w-3' />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Upvote this recommendation</TooltipContent>
              </Tooltip>
            </TooltipProvider>

            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant={vote === "down" ? "default" : "outline"}
                    size='sm'
                    className='gap-1 w-fit cursor-pointer'
                    onClick={(e) => { e.stopPropagation(); handleDownvote(); }}
                  >
                    <ThumbsDown className='h-3 w-3' />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Downvote this recommendation</TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant='default'
                    size='sm'
                    className='gap-1 w-fit cursor-pointer'
                    onClick={handleSelect}
                  >
                    <CheckCircle2 className='h-3 w-3' />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Select for next round</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>}
        </div>
      </CardContent>
    </Card>
  );
}

export default function PipelineRenderer(props: PipelineRendererProps) {
  return (
    <ReactFlowProvider>
      <PipelineRendererCard {...props} showActions={props.showActions ?? true} />
    </ReactFlowProvider>
  );
}
