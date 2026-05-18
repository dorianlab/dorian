"use client";

import React, { useMemo, useCallback, useState, useRef, useEffect } from "react";
import Editor from "@monaco-editor/react";
import { useTheme } from "next-themes";
import {
  useNodesState,
  useEdgesState,
  useReactFlow,
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  ReactFlowProvider,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { Group as PanelGroup, Panel, Separator as PanelResizeHandle } from "react-resizable-panels";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Loader2, Check, X, RefreshCw, FileCode, ScrollText, Save, Sparkles, ChevronDown, ChevronUp, Copy } from "lucide-react";
import { toast } from "sonner";

import { useExtractionStore } from "@/store/extraction";
import { usePipelineStore } from "@/store/pipeline";
import {
  usePipelineInitFromTemp,
  useReactFlowHandlers,
  usePipelineDnD,
} from "@/hooks/usePipelineComposition";
import { buildPipelineSnapshot } from "@/helpers/pipeline";
import { ws } from "@/helpers/ws-events";

import OperatorNode from "@/components/pipeline/composition/Nodes/operator";
import ParameterNode from "@/components/pipeline/composition/Nodes/parameter";
import SnippetNode from "@/components/pipeline/composition/Nodes/snippet";
import LabeledEdge from "@/components/pipeline/composition/Edges/LabeledEdge";

import type { PipelineDraft } from "@/types/pipeline";

import { RuleCardList } from "./RuleCardList";
import type { RuleSpec } from "./RuleCard";
import { CompatRegressionModal } from "./CompatRegressionModal";
import { McpConnectDialog } from "./McpConnectDialog";
import { Link2 } from "lucide-react";

// ---------------------------------------------------------------------------
// Inner canvas component — needs its own ReactFlowProvider context
// ---------------------------------------------------------------------------

function ExtractionCanvas() {
  const { extractedPipeline, setEditedPipeline } = useExtractionStore();
  const { draggingNode } = usePipelineStore();
  const [nodes, setNodes] = useNodesState<any>([]);
  const [edges, setEdges] = useEdgesState<any>([]);
  const [visible, setVisible] = useState(true);

  const { screenToFlowPosition } = useReactFlow();

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

  // -- emitFrom: build pipeline snapshot and sync to extraction store ------

  const edgesRef = useRef(edges);
  edgesRef.current = edges;

  const emitFromRef = useRef(
    (_n: any[], _e: any[]) => {},
  );

  const emitFrom = useCallback(
    (nextNodes: any[], nextEdges: any[]) => {
      const base = extractedPipeline ?? {};
      const snapshot = buildPipelineSnapshot(
        base,
        nextNodes,
        nextEdges,
      ) as PipelineDraft;
      setEditedPipeline(snapshot);
    },
    [extractedPipeline, setEditedPipeline],
  );

  emitFromRef.current = emitFrom;

  // -- updateNodeData: real implementation (mirrors main canvas) -----------

  const updateNodeData = useCallback(
    (
      nodeId: string,
      patch:
        | Record<string, any>
        | ((prev: Record<string, any>) => Record<string, any>),
    ) => {
      setNodes((prev) => {
        const next = prev.map((n: any) =>
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
        emitFromRef.current(next, edgesRef.current);
        return next;
      });
    },
    [setNodes],
  );

  // -- Init from extracted pipeline ----------------------------------------

  usePipelineInitFromTemp({
    tempPipeline: extractedPipeline,
    direction: "TB",
    setNodes,
    setEdges,
    setVisible,
    updateNodeData,
  });

  // -- ReactFlow interaction handlers (reuse from composition hooks) -------

  const { onNodesChange, onEdgesChange, onConnect } = useReactFlowHandlers({
    nodes,
    edges,
    setNodes,
    setEdges,
    emitFrom,
  });

  const { onDrop, onDragOver } = usePipelineDnD({
    draggingNode,
    screenToFlowPosition,
    edges,
    setNodes,
    setEdges,
    emitFrom,
    updateNodeData,
  });

  if (!extractedPipeline) return null;

  return (
    <ReactFlow
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={onConnect}
      onDrop={onDrop}
      onDragOver={onDragOver}
      fitView
      nodesDraggable
      proOptions={{ hideAttribution: true }}
    >
      <Background />
      <Controls />
      <MiniMap />
    </ReactFlow>
  );
}

// ---------------------------------------------------------------------------
// Main extraction view — split-pane: editor (left) + canvas (right)
// ---------------------------------------------------------------------------

export default function ExtractionView() {
  const { resolvedTheme } = useTheme();
  const monacoTheme = resolvedTheme === "dark" ? "vs-dark" : "vs";

  const {
    code,
    setCode,
    filename,
    isExtracting,
    extractionError,
    extractionTrace,
    extractedPipeline,
    extractionId,
    rulesVersion,
    setIsExtracting,
    setExtractionError,
    setIsSuggestingRules,
    rulesContent,
    rulesFormat,
    rulesLoaded,
    isSuggestingRules,
    ruleSuggestions,
    suggestionId,
    dismissRuleSuggestion,
    cancelSuggestion,
    expandedRuleId,
    setExpandedRule,
    reset,
  } = useExtractionStore();
  const { setTempPipeline, setSourceExtractionId } = usePipelineStore();

  // -- Rules editor state --
  const [rulesCode, setRulesCode] = useState("");
  const [suggestionExpanded, setSuggestionExpanded] = useState(true);
  // Card-UI state — authoritative when rulesFormat === "json_specs".
  const [specList, setSpecList] = useState<RuleSpec[]>([]);
  const [specsDirty, setSpecsDirty] = useState(false);
  const [specsSaving, setSpecsSaving] = useState(false);
  const [mcpDialogOpen, setMcpDialogOpen] = useState(false);
  // Loading spinner: show until the store flips rulesLoaded=true (the
  // WS response from loadExtractionRules). Never gate on rulesContent
  // being truthy — empty content is a valid "no rules yet" answer.
  const rulesLoading = !rulesLoaded;

  // Load rules on mount via WS. The extraction/rules response updates
  // rulesContent in the store, which triggers the effect below.
  useEffect(() => {
    ws.loadExtractionRules({ filename });
  }, [filename]);

  useEffect(() => {
    if (!rulesLoaded) return;  // wait for the WS response
    if (rulesFormat === "json_specs") {
      if (rulesContent) {
        try {
          const parsed = JSON.parse(rulesContent);
          if (Array.isArray(parsed)) {
            setSpecList(parsed as RuleSpec[]);
            setSpecsDirty(false);
          }
        } catch {
          setSpecList([]);
        }
      } else {
        setSpecList([]);
      }
    } else if (rulesContent) {
      setRulesCode(rulesContent);
    }
  }, [rulesContent, rulesFormat, rulesLoaded]);

  const nodeCount = extractedPipeline
    ? Object.keys(extractedPipeline.nodes ?? {}).length
    : 0;
  const edgeCount = extractedPipeline?.edges?.length ?? 0;

  // -- Actions --

  const handleReExtract = () => {
    setIsExtracting(true);
    setExtractionError(null);
    ws.extractPipeline({ code, language: "python", filename });
  };

  const handleConfirm = () => {
    if (!extractedPipeline) return;
    // Use the user-edited version if available, otherwise the original extraction
    const { editedPipeline } = useExtractionStore.getState();
    const pipeline = editedPipeline ?? extractedPipeline;
    const eid = extractionId;
    reset();
    setTempPipeline(pipeline);
    setSourceExtractionId(eid);
    ws.pipelineCreated({ pipeline, source: "extraction", filename, extractionId: eid });

    // Notify the AI Debugger about every operator in the extracted pipeline
    // so it can identify risks (same as drag-dropping nodes onto the canvas).
    const pipelineId = (pipeline as any)?.uuid ?? null;
    const nodes: Record<string, any> = (pipeline as any)?.nodes ?? {};
    for (const [nodeId, node] of Object.entries(nodes)) {
      const nodeName: string = (node as any)?.name ?? "";
      const nodeType: string = (node as any)?.type?.toLowerCase() ?? "operator";
      if (nodeName && nodeName.includes(".")) {
        ws.pipelineNodeAdded({ nodeId, nodeType, nodeName, pipelineId });
      }
    }
  };

  const handleCancel = () => {
    reset();
  };

  const handleSaveRules = () => {
    if (rulesFormat === "json_specs") {
      setSpecsSaving(true);
      ws.saveExtractionRuleSpecs({ specs: specList, filename });
      setTimeout(() => setSpecsSaving(false), 1500);
      setSpecsDirty(false);
    } else {
      ws.saveExtractionRules({ content: rulesCode, filename });
    }
  };

  const handleSaveAndReExtract = () => {
    // Save first, then extract. Backend processes events in FIFO order
    // so the new rules are persisted before extraction reads them.
    handleSaveRules();
    handleReExtract();
  };

  // Card-UI save override after user dismissed the compat-regression modal
  // with "Override & save anyway". Re-emits with skipCompatCheck=true so
  // the backend accepts the regressions (audit flag is stored on the doc).
  const handleCompatOverride = () => {
    if (rulesFormat === "json_specs") {
      setSpecsSaving(true);
      ws.saveExtractionRuleSpecs({ specs: specList, filename, skipCompatCheck: true });
      setTimeout(() => setSpecsSaving(false), 1500);
      setSpecsDirty(false);
    } else {
      ws.saveExtractionRules({ content: rulesCode, filename, skipCompatCheck: true });
    }
  };

  const handleSpecsChange = (next: RuleSpec[]) => {
    setSpecList(next);
    setSpecsDirty(true);
  };

  const handleSuggestRules = () => {
    if (!extractionId) return;
    setIsSuggestingRules(true);
    setSuggestionExpanded(true);
    ws.suggestRules({ extractionId, rulesVersion });
  };

  const handleAcceptRule = (ruleId: string) => {
    if (!suggestionId) return;
    // Optimistic dismiss
    dismissRuleSuggestion(ruleId);
    ws.acceptRule({ suggestionId, ruleId });
  };

  const handleRejectRule = (ruleId: string) => {
    if (!suggestionId) return;
    // Optimistic dismiss
    dismissRuleSuggestion(ruleId);
    ws.rejectRule({ suggestionId, ruleId });
  };

  const handleCancelSuggest = () => {
    cancelSuggestion();
    ws.cancelSuggestRules({});
  };

  // -- Render --

  return (
    <div className='flex flex-col h-full w-full'>
      {/* Toolbar */}
      <div className='flex items-center justify-between px-4 py-2 border-b bg-card'>
        <div className='flex items-center gap-2'>
          <FileCode className='h-4 w-4 text-muted-foreground' />
          <span className='text-sm font-medium'>
            Extracting pipeline from{" "}
            <code className='text-xs bg-muted px-1 py-0.5 rounded'>
              {filename}
            </code>
          </span>
          {extractedPipeline && (
            <Badge variant='secondary' className='text-xs'>
              {nodeCount} nodes &middot; {edgeCount} edges
            </Badge>
          )}
        </div>

        <div className='flex items-center gap-2'>
          <Button
            variant='outline'
            size='sm'
            onClick={handleReExtract}
            disabled={isExtracting || !code.trim()}
          >
            {isExtracting ? (
              <Loader2 className='h-4 w-4 animate-spin mr-1' />
            ) : (
              <RefreshCw className='h-4 w-4 mr-1' />
            )}
            {isExtracting ? "Extracting..." : "Re-Extract"}
          </Button>
          <Button
            variant='default'
            size='sm'
            onClick={handleConfirm}
            disabled={!extractedPipeline || isExtracting}
          >
            <Check className='h-4 w-4 mr-1' />
            Confirm
          </Button>
          <Button
            variant='ghost'
            size='sm'
            onClick={() => setMcpDialogOpen(true)}
            title='Issue a token for an external MCP client'
          >
            <Link2 className='h-4 w-4 mr-1' />
            Connect MCP
          </Button>
          <Button variant='ghost' size='sm' onClick={handleCancel}>
            <X className='h-4 w-4 mr-1' />
            Cancel
          </Button>
        </div>
      </div>

      {/* Split view — left tabbed editor | right canvas */}
      <PanelGroup orientation='horizontal' className='flex-1 min-h-0'>

        {/* Left panel: tabs for Rules + Code */}
        <Panel defaultSize={50} minSize={20} className='flex flex-col min-h-0'>
          <Tabs defaultValue='code' className='flex flex-col flex-1 min-h-0'>

            {/* Tab bar */}
            <div className='flex items-center border-b bg-muted/40 px-2 shrink-0 gap-2'>
              <TabsList className='h-8 bg-transparent gap-0 p-0 rounded-none'>
                <TabsTrigger
                  value='rules'
                  className='h-8 rounded-none px-3 text-xs data-[state=active]:bg-background data-[state=active]:shadow-none data-[state=active]:border-b-2 data-[state=active]:border-primary'
                >
                  <ScrollText className='h-3 w-3 mr-1.5' />
                  Extraction Rules
                </TabsTrigger>
                <TabsTrigger
                  value='code'
                  className='h-8 rounded-none px-3 text-xs data-[state=active]:bg-background data-[state=active]:shadow-none data-[state=active]:border-b-2 data-[state=active]:border-primary'
                >
                  <FileCode className='h-3 w-3 mr-1.5' />
                  Pipeline Code
                </TabsTrigger>
              </TabsList>

              {/* Rules save actions — only visible on the rules tab */}
              <TabsContent value='rules' className='mt-0 ml-auto flex items-center gap-1'>
                <Button
                  variant='ghost'
                  size='sm'
                  className='h-6 text-xs px-2'
                  onClick={handleSaveRules}
                  disabled={rulesLoading}
                >
                  <Save className='h-3 w-3 mr-1' />
                  Save
                </Button>
                <Button
                  variant='outline'
                  size='sm'
                  className='h-6 text-xs px-2'
                  onClick={handleSaveAndReExtract}
                  disabled={rulesLoading || isExtracting || !code.trim()}
                >
                  {isExtracting ? (
                    <Loader2 className='h-3 w-3 animate-spin mr-1' />
                  ) : (
                    <RefreshCw className='h-3 w-3 mr-1' />
                  )}
                  Save & Re-E...
                </Button>
                {isSuggestingRules ? (
                  <Button
                    variant='outline'
                    size='sm'
                    className='h-6 text-xs px-2 text-destructive border-destructive/30 hover:bg-destructive/10'
                    onClick={handleCancelSuggest}
                  >
                    <Loader2 className='h-3 w-3 animate-spin mr-1' />
                    Stop
                  </Button>
                ) : (
                  <Button
                    variant='outline'
                    size='sm'
                    className='h-6 text-xs px-2 text-violet-600 border-violet-300 hover:bg-violet-50 dark:hover:bg-violet-950 dark:border-violet-700 dark:text-violet-400'
                    onClick={handleSuggestRules}
                    disabled={!extractionId}
                  >
                    <Sparkles className='h-3 w-3 mr-1' />
                    Suggest Rules
                  </Button>
                )}
              </TabsContent>
            </div>

            {/* Rules editor content */}
            <TabsContent value='rules' className='flex flex-col flex-1 min-h-0 mt-0 overflow-hidden data-[state=inactive]:hidden'>
              {/* Card list (json_specs) OR legacy Monaco (python_rules) */}
              <div className='flex-1 min-h-0 overflow-hidden'>
                {rulesLoading ? (
                  <div className='h-full flex items-center justify-center'>
                    <Loader2 className='h-5 w-5 animate-spin text-muted-foreground' />
                  </div>
                ) : rulesFormat === "json_specs" ? (
                  <RuleCardList
                    specs={specList}
                    onChange={handleSpecsChange}
                    onSave={handleSaveRules}
                    saving={specsSaving}
                    dirty={specsDirty}
                  />
                ) : (
                  // Legacy python_rules path — the card UI is the
                  // canonical surface. Rather than render a Monaco
                  // editor (which has a model-creation race with
                  // @monaco-editor/react 4.7 under React 19 conditional
                  // mount), show a banner pointing the user at the
                  // migration action. Legacy rules remain honoured at
                  // extraction time via handle_extract_pipeline's
                  // format-aware loader.
                  <div className='h-full flex flex-col items-center justify-center p-6 text-center gap-3'>
                    <ScrollText className='h-8 w-8 text-muted-foreground' />
                    <div className='max-w-md space-y-1'>
                      <p className='text-sm font-medium'>
                        Legacy Python rules detected
                      </p>
                      <p className='text-xs text-muted-foreground'>
                        Your saved rules are in the old Python-source format.
                        The card UI only works with JSON specs. Your
                        extractions keep using the legacy rules until you
                        migrate — nothing is lost.
                      </p>
                    </div>
                    <Button
                      size='sm'
                      variant='default'
                      onClick={() => {
                        // Seed the card UI with an empty spec list +
                        // switch format to json_specs. User can add new
                        // rules; when they hit Save, the json_specs
                        // path takes over and the legacy doc stops
                        // being the source of truth.
                        setSpecList([]);
                        setSpecsDirty(true);
                        useExtractionStore.getState().setRulesContent(
                          "[]", "user", "json_specs",
                        );
                      }}
                    >
                      Switch to card UI
                    </Button>
                  </div>
                )}
              </div>

              {/* Suggestion bottom bar */}
              {(isSuggestingRules || (ruleSuggestions && ruleSuggestions.length > 0)) && (
                <div className='shrink-0 border-t bg-muted/30'>
                  {/* Bar header */}
                  <button
                    className='w-full flex items-center justify-between px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors'
                    onClick={() => setSuggestionExpanded((v) => !v)}
                  >
                    <div className='flex items-center gap-1.5'>
                      <Sparkles className='h-3 w-3 text-violet-500' />
                      {isSuggestingRules
                        ? "AI is analysing the extraction…"
                        : `${ruleSuggestions?.length ?? 0} rule suggestion${(ruleSuggestions?.length ?? 0) === 1 ? "" : "s"}`}
                    </div>
                    {suggestionExpanded ? (
                      <ChevronDown className='h-3 w-3' />
                    ) : (
                      <ChevronUp className='h-3 w-3' />
                    )}
                  </button>

                  {/* Suggestions list */}
                  {suggestionExpanded && (
                    <div className='max-h-52 overflow-y-auto divide-y divide-border'>
                      {isSuggestingRules && (!ruleSuggestions || ruleSuggestions.length === 0) && (
                        <div className='flex items-center gap-2 px-3 py-3 text-xs text-muted-foreground'>
                          <Loader2 className='h-3.5 w-3.5 animate-spin shrink-0' />
                          Asking the LLM to suggest new rewrite rules…
                        </div>
                      )}
                      {(ruleSuggestions ?? []).map((rule) => (
                        <div key={rule.ruleId} className='border-b last:border-b-0'>
                          {/* Card header row */}
                          <div className='flex items-center gap-2 px-3 py-2'>
                            <button
                              className='flex-1 min-w-0 flex items-center gap-1.5 text-left'
                              onClick={() => setExpandedRule(expandedRuleId === rule.ruleId ? null : rule.ruleId)}
                            >
                              <ChevronDown
                                className={`h-3 w-3 shrink-0 text-muted-foreground transition-transform ${expandedRuleId === rule.ruleId ? "rotate-180" : ""}`}
                              />
                              <p className='text-xs font-medium leading-tight truncate'>
                                {rule.description || rule.ruleId}
                              </p>
                              {rule.isPartial && (
                                <Badge variant='outline' className='text-[9px] px-1 h-4 shrink-0 border-amber-500/40 text-amber-700 dark:text-amber-400'>
                                  partial
                                </Badge>
                              )}
                              {!rule.valid && (
                                <Badge variant='destructive' className='text-[9px] px-1 h-4 shrink-0'>
                                  invalid
                                </Badge>
                              )}
                            </button>
                            <div className='flex items-center gap-1 shrink-0'>
                              {/* Single action button — "Accept" when full-match,
                                  "Partial accept" when the rule improves GED but
                                  doesn't fully close the gap. Per UX spec: never
                                  show both at the same time. */}
                              {rule.isPartial ? (
                                <Button
                                  variant='ghost'
                                  size='sm'
                                  className='h-6 px-2 text-[10px] text-amber-700 hover:text-amber-800 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-950'
                                  title={rule.gedBefore != null && rule.gedAfter != null
                                    ? `Apply; GED ${rule.gedBefore} → ${rule.gedAfter}. Remaining gap covered by a follow-up.`
                                    : 'Apply partial rule; gap reduced but not fully closed'}
                                  onClick={() => handleAcceptRule(rule.ruleId)}
                                >
                                  <Check className='h-3 w-3 mr-1' />
                                  Partial accept
                                </Button>
                              ) : (
                                <Button
                                  variant='ghost'
                                  size='sm'
                                  className='h-6 w-6 p-0 text-emerald-600 hover:text-emerald-700 hover:bg-emerald-50 dark:hover:bg-emerald-950'
                                  title='Accept rule'
                                  onClick={() => handleAcceptRule(rule.ruleId)}
                                >
                                  <Check className='h-3.5 w-3.5' />
                                </Button>
                              )}
                              <Button
                                variant='ghost'
                                size='sm'
                                className='h-6 w-6 p-0 text-muted-foreground hover:text-destructive hover:bg-destructive/10'
                                title='Reject rule'
                                onClick={() => handleRejectRule(rule.ruleId)}
                              >
                                <X className='h-3.5 w-3.5' />
                              </Button>
                            </div>
                          </div>
                          {/* Expanded detail panel */}
                          {expandedRuleId === rule.ruleId && (
                            <div className='px-3 pb-2 space-y-1.5 bg-muted/20'>
                              {rule.isPartial && (
                                <p className='text-[10px] text-amber-700 dark:text-amber-400'>
                                  This rule improves the extraction but doesn't
                                  fully match the target yet
                                  {rule.gedBefore != null && rule.gedAfter != null
                                    ? ` (GED ${rule.gedBefore} → ${rule.gedAfter})`
                                    : ""}
                                  . Accepting will apply it and request a
                                  follow-up suggestion that closes the remaining
                                  gap.
                                </p>
                              )}
                              {rule.errors?.length > 0 && (
                                <p className='text-[10px] text-destructive'>
                                  {rule.errors.join(" · ")}
                                </p>
                              )}
                              {rule.warnings?.length > 0 && (
                                <p className='text-[10px] text-amber-600 dark:text-amber-400'>
                                  {rule.warnings.join(" · ")}
                                </p>
                              )}
                              <pre className='text-[10px] font-mono bg-background rounded border border-border p-2 max-h-36 overflow-auto whitespace-pre-wrap break-all select-text'>
                                {rule.spec
                                  ? JSON.stringify(rule.spec, null, 2)
                                  : "(no spec)"}
                              </pre>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </TabsContent>

            {/* Pipeline code editor content */}
            <TabsContent value='code' className='flex-1 min-h-0 mt-0 overflow-hidden data-[state=inactive]:hidden'>
              <Editor
                key={monacoTheme}
                height='100%'
                language='python'
                value={code}
                onChange={(val) => setCode(val ?? "")}
                theme={monacoTheme}
                options={{
                  fontSize: 14,
                  minimap: { enabled: false },
                  wordWrap: "on",
                  automaticLayout: true,
                  tabSize: 4,
                  readOnly: false,
                  scrollBeyondLastLine: false,
                }}
              />
            </TabsContent>

          </Tabs>
        </Panel>

        <PanelResizeHandle className='w-1 bg-border hover:bg-primary/40 active:bg-primary/60 transition-colors cursor-col-resize' />

        {/* Right panel: Pipeline canvas */}
        <Panel defaultSize={50} minSize={20} className='relative bg-muted/30'>
          {/* Copy DAG JSON — top-right, hidden until extraction arrives.
              Useful for: pasting into bug reports, feeding to an MCP
              client that can't read the live session, sharing with
              collaborators. Dumps the user-edited DAG if available
              (the "corrected" version), falling back to the raw
              auto-extracted DAG. */}
          {extractedPipeline && (
            <Button
              variant='ghost'
              size='sm'
              className='absolute top-2 right-2 z-20 h-7 text-xs bg-background/70 backdrop-blur-sm hover:bg-background'
              onClick={async () => {
                const edited = useExtractionStore.getState().editedPipeline;
                const dag = edited ?? extractedPipeline;
                const blob = {
                  extractionId,
                  rulesVersion,
                  autoDag: extractedPipeline,
                  correctedDag: edited,
                  currentDag: dag,
                };
                try {
                  await navigator.clipboard.writeText(JSON.stringify(blob, null, 2));
                  toast.success(edited
                    ? 'Copied DAG (auto + corrected)'
                    : 'Copied auto-extracted DAG');
                } catch {
                  toast.error('Clipboard write failed');
                }
              }}
              title='Copy DAG JSON to clipboard'
            >
              <Copy className='h-3 w-3 mr-1' />
              Copy DAG
            </Button>
          )}

          {/* Error overlay */}
          {extractionError && (
            <div className='absolute inset-0 z-10 flex flex-col items-center justify-start pt-8 bg-background/80 overflow-auto'>
              <div className='p-6 max-w-lg w-full'>
                <p className='font-medium text-destructive mb-1'>
                  Extraction Error
                </p>
                <p className='text-sm text-muted-foreground break-words'>
                  {extractionError}
                </p>
                {extractionTrace && (
                  <pre className='mt-3 px-3 py-2 text-[10px] leading-tight font-mono text-muted-foreground
                                  whitespace-pre-wrap break-words max-h-[260px] overflow-auto
                                  bg-muted rounded border border-border select-text'>
                    {extractionTrace}
                  </pre>
                )}
                <Button
                  variant='outline'
                  size='sm'
                  className='mt-3'
                  onClick={handleReExtract}
                >
                  <RefreshCw className='h-3 w-3 mr-1' />
                  Retry
                </Button>
              </div>
            </div>
          )}

          {/* Loading overlay — initial extraction */}
          {isExtracting && !extractedPipeline && (
            <div className='absolute inset-0 z-10 flex items-center justify-center bg-background/80'>
              <div className='text-center'>
                <Loader2 className='h-8 w-8 animate-spin text-muted-foreground mx-auto mb-2' />
                <p className='text-sm text-muted-foreground'>
                  Parsing Python code...
                </p>
              </div>
            </div>
          )}

          {/* Loading overlay — re-extraction over existing canvas */}
          {isExtracting && extractedPipeline && (
            <div className='absolute inset-0 z-10 flex items-center justify-center bg-background/60 backdrop-blur-sm'>
              <div className='text-center'>
                <Loader2 className='h-6 w-6 animate-spin text-muted-foreground mx-auto mb-2' />
                <p className='text-sm text-muted-foreground'>Re-extracting...</p>
              </div>
            </div>
          )}

          {/* Empty state */}
          {!extractedPipeline && !isExtracting && !extractionError && (
            <div className='absolute inset-0 z-10 flex items-center justify-center'>
              <p className='text-sm text-muted-foreground'>
                Pipeline preview will appear here
              </p>
            </div>
          )}

          <ReactFlowProvider>
            <ExtractionCanvas />
          </ReactFlowProvider>
        </Panel>

      </PanelGroup>

      {/* Backward-compat regression modal — only renders when the backend
          pushed a compatRegressionReport into the store (save was blocked). */}
      <CompatRegressionModal
        onOverride={handleCompatOverride}
        onEdit={() => { /* dialog closes itself; user stays on the rules pane */ }}
      />

      {/* MCP client handshake dialog — issues a short-lived token the
          user pastes into their MCP client config. */}
      <McpConnectDialog open={mcpDialogOpen} onOpenChange={setMcpDialogOpen} />
    </div>
  );
}
