"use client";

import React, { useMemo, useRef, useState } from "react";
import { useNodesState, useEdgesState, useReactFlow } from "@xyflow/react";
import ReactFlowWrapper from "../ReactFlowWrapper";
import OperatorNode from "@/components/pipeline/composition/Nodes/operator";
import ParameterNode from "@/components/pipeline/composition/Nodes/parameter";
import SnippetNode from "@/components/pipeline/composition/Nodes/snippet";
import LabeledEdge from "@/components/pipeline/composition/Edges/LabeledEdge";
import { DnDProvider } from "../DndContext";

import PipelineHeader from "@/components/layout/header/index";
import Sidebar from "@/components/layout/sidebar/index";
import NoSessionSelected from "@/components/ui/empty-states/no-session-selected";
import FlyInWrapper from "@/components/ui/fly-in-wrapper";
import CompositionSidebar from "@/components/pipeline/composition/sidebar";
import DroppableSidebar from "@/components/pipeline/composition/sidebar/droppable-operators";

import { PipelineLayout } from "./PipelineLayout";
import { PipelineCanvas } from "./PipelineCanvas";
import { PipelineRecommendations } from "./PipelineRecommendations";
import { QueryResolver } from "./QueryResolver";
import ModelTracingModal from "@/components/pipeline/model-tracing-modal";

import {
  usePipelineSnapshotEmitter,
  usePipelineInitFromTemp,
  useReactFlowHandlers,
  usePipelineDnD,
  useCustomOperatorsEffect,
  useGroupUpdateEffect,
} from "@/hooks/usePipelineComposition";
import { usePipelineAutoSave } from "@/hooks/usePipelineAutoSave";
import { useExecutionStatusBridge } from "@/hooks/useExecutionStatusBridge";
import { useEdgeValidation } from "@/hooks/useEdgeValidation";
import { randomUUID } from "@/helpers/uuid";
import VisualizerNode from "../Nodes/visualizer";
import { SuggestionBar } from "../../../suggestions";
import { EvaluationPanel } from "../../evaluation-panel";
import { usePipelineStore } from "@/store/pipeline";
import { useSessionStore } from "@/store/session";
import { useUIStore } from "@/store/ui";
import { useRecommendationEngineStore } from "@/store/recommendation-engine";
import { emitEvent } from "@/helpers/ws-events";
import { useExtractionStore } from "@/store/extraction";
import { GuidedTooltip } from "@/components/ui/guided-tooltip";
import { useTooltipStore } from "@/store/tooltip";
import ExtractionView from "@/components/pipeline/extraction/ExtractionView";

export function PipelineScreen({ className }: { className?: string }) {
  const [nodes, setNodes] = useNodesState<any>([]);
  const [edges, setEdges] = useEdgesState<any>([]);
  const reactFlowWrapper = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(true);

  const { screenToFlowPosition } = useReactFlow();
  // Alias used by useCustomOperatorsEffect; same instance, no second hook call needed.
  const stfp = screenToFlowPosition;

  const { activeSessionId } = useSessionStore();
  const {
    draggingNode,
    customOperators,

    setDraftPipeline,
    setTempPipeline,
    tempPipeline,
  } = usePipelineStore();
  const { recommendedPipelines } = useRecommendationEngineStore();

  const nodeTypes = useMemo(
    () => ({
      operator: OperatorNode,
      parameter: ParameterNode,
      snippet: SnippetNode,
      visualizer: VisualizerNode,
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

  const { direction } = useUIStore();

  const { emitFrom, resetDedupe } = usePipelineSnapshotEmitter({
    tempPipeline,
    setDraftPipeline,
  });

  // Auto-save: persist canvas → Redis on every change (debounced 1 s).
  usePipelineAutoSave();

  const edgesRef = React.useRef(edges);
  React.useEffect(() => {
    edgesRef.current = edges;
  }, [edges]);

  const emitFromRef = React.useRef(emitFrom);
  React.useEffect(() => {
    emitFromRef.current = emitFrom;
  }, [emitFrom]);

  const updateNodeData = React.useCallback(
    (
      nodeId: string,
      patch:
        | Record<string, any>
        | ((prev: Record<string, any>) => Record<string, any>),
    ) => {
      setNodes((prev) => {
        const next = prev.map((n) =>
          n.id === nodeId
            ? {
                ...n,
                data: {
                  ...(n.data ?? {}),
                  ...(typeof patch === "function"
                    ? patch(n.data ?? {})
                    : patch),
                },
              }
            : n,
        );

        //  use refs to avoid changing callback identity
        emitFromRef.current(next, edgesRef.current);

        return next;
      });

      // Emit config change event outside setState (keys, not values, for minimal payload).
      // Skip emission for internal-only patches (e.g. execution status bridge sets
      // only "status") — those aren't user configuration changes.
      const keys = typeof patch === "function" ? [] : Object.keys(patch);
      const INTERNAL_KEYS = new Set(["status", "execError", "execTrace", "execStartTime", "execDuration", "output"]);
      const isInternalOnly =
        keys.length > 0 && keys.every((k) => INTERNAL_KEYS.has(k));
      if (!isInternalOnly) {
        emitEvent("PipelineNodeConfigured", {
          nodeId,
          patchKeys: keys,
          pipelineId: tempPipeline?.uuid ?? null,
        });
      }
    },
    [setNodes, tempPipeline],
  );

  usePipelineInitFromTemp({
    tempPipeline,
    direction,
    setNodes,
    setEdges,
    setVisible,
    resetDedupe,
    updateNodeData,
  });

  const { onNodesChange, onEdgesChange, onConnect } = useReactFlowHandlers({
    nodes,
    edges,
    setNodes,
    setEdges,
    emitFrom,
  });

  const { onDrop, onDragOver, onDragStart } = usePipelineDnD({
    draggingNode,
    screenToFlowPosition,
    edges,
    setNodes,
    setEdges,
    emitFrom,
    updateNodeData,
  });

  useCustomOperatorsEffect({
    customOperators,
    stfp,
    setNodes,
    setEdges,
    edges,
    emitFrom,
    updateNodeData,
  });

  // Bridge: backend group creation → update RF node data in-place.
  useGroupUpdateEffect({ setNodes });

  // Bridge: execution status → ReactFlow node data → border colour / glow.
  useExecutionStatusBridge(updateNodeData);

  // Edge validation: reject self-loops, duplicates, param→param, and cycles.
  const isValidConnection = useEdgeValidation();

  const { active: extractionActive } = useExtractionStore();
  const queries = useUIStore((s) => s.queries);
  const hasPendingSetupQueries = queries.some(
    (q) =>
      q.id.endsWith(":task_selection") || q.id.endsWith(":eval_selection"),
  );
  const showRecommendations =
    activeSessionId && !tempPipeline && recommendedPipelines.length > 0;
  const showQueryResolver =
    activeSessionId &&
    !tempPipeline &&
    recommendedPipelines.length === 0 &&
    hasPendingSetupQueries;
  const showCanvas = activeSessionId && tempPipeline;

  // Tour assist: pre-open a blank canvas as soon as the tour reaches the
  // compose-pipeline step (9) — that way step 10 (canvas) is already
  // mounted by the time the user clicks Next, and `nextStep` doesn't skip
  // it.  Also fires if pendingStep is 10 (someone explicitly queued the
  // canvas advance).  Idempotent: the !tempPipeline guard prevents loops.
  const tourActive = useTooltipStore((s) => s.tourActive);
  const tourStep = useTooltipStore((s) => s.tourStep);
  const pendingStep = useTooltipStore((s) => s.pendingStep);
  React.useEffect(() => {
    if (!tourActive || !activeSessionId || tempPipeline) return;
    if (tourStep < 9 && pendingStep !== 10) return;
    const pipelineId = randomUUID();
    setTempPipeline({
      uuid: pipelineId,
      nodes: {},
      edges: [],
      createdAt: new Date().toISOString(),
      createdBy: null,
      sessionId: activeSessionId ?? null,
    } as any);
  }, [tourActive, tourStep, pendingStep, activeSessionId, tempPipeline, setTempPipeline]);

  return (
    <PipelineLayout
      className={className}
      header={activeSessionId ? <PipelineHeader /> : null}
      sidebar={<Sidebar />}
    >
      {!activeSessionId ? (
        <NoSessionSelected />
      ) : extractionActive ? (
        <div className='relative flex flex-row h-full w-full'>
          <div className='relative flex flex-col flex-1 min-w-0'>
            <ExtractionView />
          </div>
          <div className='h-full flex flex-col py-4 w-72 bg-card shadow-xl border-l border-border'>
            <DroppableSidebar />
          </div>
        </div>
      ) : showQueryResolver ? (
        <QueryResolver />
      ) : showRecommendations ? (
          <PipelineRecommendations
            recommendedPipelines={recommendedPipelines}
            onPick={(p: any) => {
              // Normalize recommendation pipeline to PipelineDraft shape.
              // docstore/RL pipelines use `pipeline_id` (not `uuid`) and
              // `class_type` (not `type`) — remap for canvas + Run Pipeline.
              const CT_MAP: Record<string, string> = {
                Parameter: "parameter",
                Operator: "operator",
                Snippet: "snippet",
                Group: "group",
              };

              const normalizedNodes: Record<string, any> = {};
              if (p?.nodes) {
                for (const [id, node] of Object.entries(p.nodes as Record<string, any>)) {
                  const ct = node?.class_type;
                  const type =
                    node?.type ?? CT_MAP[ct] ?? ct?.toLowerCase() ?? "operator";
                  normalizedNodes[id] = { ...node, type };
                }
              }

              const normalizedEdges = (p?.edges ?? []).map((e: any) => ({
                ...e,
                target: e.target ?? e.destination,
              }));

              const draft = {
                uuid: p?.uuid ?? p?.pipeline_id ?? p?.pipelineId ?? p?._id ?? randomUUID(),
                nodes: normalizedNodes,
                edges: normalizedEdges,
              };

              setTempPipeline(draft as any);
              // Follow the user into the canvas if the tour is on the
              // recommendation step (or earlier) — same mechanism as the
              // Compose button: queues `canvas` as pendingStep, registerStep
              // promotes it once the canvas tooltip mounts.
              useTooltipStore.getState().advanceTourTo("canvas");
            }}
          />
      ) : showCanvas ? (
        <FlyInWrapper isVisible={visible}>
          <div
            className='relative flex flex-row h-full w-full'
            ref={reactFlowWrapper}
          >
            <div className='relative flex flex-col flex-1 min-w-0'>
              <PipelineCanvas
                wrapperRef={reactFlowWrapper}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onDrop={onDrop}
                onDragOver={onDragOver}
                onDragStart={onDragStart}
                isValidConnection={isValidConnection}
              />
              <GuidedTooltip targetId='evaluation-panel' side='top' wrapperClassName='shrink-0'>
                <EvaluationPanel />
              </GuidedTooltip>
              <GuidedTooltip targetId='suggestion-bar' side='top' wrapperClassName='shrink-0'>
                <SuggestionBar className='w-full' />
              </GuidedTooltip>
            </div>
            <GuidedTooltip targetId='operator-catalog' side='left' wrapperClassName='h-full shrink-0'>
              <CompositionSidebar />
            </GuidedTooltip>
          </div>
          <ModelTracingModal />
        </FlyInWrapper>

      ) : null}
    </PipelineLayout>
  );
}

export default function Page() {
  return (
    <ReactFlowWrapper>
      <DnDProvider>
        <PipelineScreen />
      </DnDProvider>
    </ReactFlowWrapper>
  );
}
