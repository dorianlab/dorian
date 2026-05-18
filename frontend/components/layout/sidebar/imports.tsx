"use client";

import { useState } from "react";
import type React from "react";
import { importPipeline } from "@/app/api/pipeline";
import { useExtractionStore } from "@/store/extraction";
import { uploadDataset, importDataset, toggleDatasetVisibility } from "@/app/api/dataset";
import {
  Database, Upload, Plus, X, File, ChartNetwork, Globe, Lock,
  ListChecks, FlaskConical, Trophy, PencilLine,
} from "lucide-react";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import RankingObjectives from "@/components/objectives";
import TaskSelector from "@/components/tasks";
import EvaluationProcedure from "@/components/evaluation";
import DatasetImportDialog from "@/components/datasets/DatasetImportDialog";
import { DatasetUploadDialog } from "@/components/datasets/dataset-upload-dialog";
import { ws } from "@/helpers/ws-events";
import { randomUUID } from "@/helpers/uuid";
import { GuidedTooltip } from "@/components/ui/guided-tooltip";
import { useSessionStore } from "@/store/session";
import { usePipelineStore } from "@/store/pipeline";
import { usePipelineRunStore } from "@/store/pipeline-run";
import { useDatasetStore } from "@/store/dataset";
import { useUIStore } from "@/store/ui";
import { useTooltipStore } from "@/store/tooltip";
import { toast } from "sonner";
import { isRateLimitError } from "@/lib/api-client";

const isCsvFile = (file: File) => {
  const nameOk = file.name.toLowerCase().endsWith(".csv");
  const typeOk =
    file.type === "text/csv" ||
    file.type === "application/vnd.ms-excel" ||
    file.type === "";
  return nameOk || typeOk;
};

// ── Icon-only button used in collapsed mode ───────────────────────────────────
function IconBtn({
  icon: Icon,
  title,
  onClick,
  disabled,
}: {
  icon: React.ElementType;
  title: string;
  onClick?: () => void;
  disabled?: boolean;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          disabled={disabled}
          onClick={onClick}
          className="w-9 h-9 text-muted-foreground hover:text-foreground"
        >
          <Icon className="h-4 w-4" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="right" sideOffset={8}>
        {title}
      </TooltipContent>
    </Tooltip>
  );
}

export default function PipelineToolbar() {
  const { sidebarCollapsed, setSidebarCollapsed, toggles } = useUIStore();
  const hasPipelineComposition = toggles.PipelineComposition;
  const { activeSessionId, userId } = useSessionStore();
  const { pipelineHistory, setTempPipeline, removePipeline, getHeadVersion } = usePipelineStore();
  const { clearRun } = usePipelineRunStore();
  const { addDatasets, datasets, setProgress, removeDataset, updateDataset } = useDatasetStore();
  const [importOpen, setImportOpen] = useState(false);
  const [pendingUpload, setPendingUpload] = useState<File | null>(null);

  const handleToggleVisibility = async (dataset: any) => {
    if (!dataset.did || !userId) return;
    const newValue = !dataset.isPublic;
    try {
      await toggleDatasetVisibility(dataset.did, userId, newValue);
      updateDataset(dataset.uuid, "isPublic", newValue);
      toast.success(newValue ? "Dataset is now public" : "Dataset is now private");
    } catch (err) {
      if (isRateLimitError(err)) return;
      toast.error("Could not change dataset visibility");
    }
  };

  const handlePipelineCompose = () => {
    const pipelineId = randomUUID();
    const pipeline = {
      uuid: pipelineId,
      nodes: {},
      edges: [],
      createdAt: new Date().toISOString(),
      createdBy: userId ?? null,
      sessionId: activeSessionId ?? null,
    };
    setTempPipeline(pipeline);
    clearRun();
    ws.pipelineCreated({ pipeline });
    ws.pipelineComposed({ pipelineId });
    // If the onboarding tour is currently sitting on the compose-pipeline
    // step (or earlier), follow the user into the canvas view.  The canvas
    // tooltip mounts asynchronously, so advanceTourTo queues it as
    // pendingStep and registerStep promotes it once the canvas renders.
    useTooltipStore.getState().advanceTourTo("canvas");
  };

  const handleImportPipeline = async (e: React.ChangeEvent<HTMLInputElement>) => {
    e.preventDefault();
    if (!e.target.files?.length) return;
    const file = e.target.files[0];
    if (!file) return;

    if (file.name.toLowerCase().endsWith(".py")) {
      const code = await file.text();
      const { startExtraction, setIsExtracting } = useExtractionStore.getState();
      startExtraction(code, file.name);
      setIsExtracting(true);
      setSidebarCollapsed(true);
      ws.extractPipeline({ code, language: "python", filename: file.name });
    } else {
      ws.pipelineImported({ file });
      await importPipeline(file, setProgress, activeSessionId || "", userId);
      ws.pipelineImported({ file });

      // Open the canvas with the imported pipeline
      try {
        const text = await file.text();
        const parsed = JSON.parse(text);
        // Support both raw pipeline objects and wrapped {pipeline: {...}} format
        const pipeline = parsed.pipeline ?? parsed;
        if (pipeline.nodes || pipeline.edges) {
          if (!pipeline.uuid) pipeline.uuid = randomUUID();
          setTempPipeline(pipeline);
          clearRun();
          setSidebarCollapsed(true);
        }
      } catch {
        // If parsing fails, the backend handler will still process the import
        // and the pipeline will be accessible from history.
      }
    }
    e.target.value = "";
  };

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    e.preventDefault();
    const files = Array.from(e.target.files ?? []);
    if (!files.length) return;
    const invalidFiles = files.filter((f) => !isCsvFile(f));
    if (invalidFiles.length) {
      toast.error(`Some files were not CSV and were skipped: ${invalidFiles.map((f) => f.name).join(", ")}`);
      e.target.value = "";
      return;
    }
    const file = e.target.files!.item(0)!;
    setPendingUpload(file);
    // Reset the input so picking the same file again re-opens the dialog.
    e.target.value = "";
  };

  const handleConfirmUpload = async (description: string) => {
    const file = pendingUpload;
    if (!file) return;
    const _dataset = {
      uuid: randomUUID(),
      filename: file.name,
      size: file.size,
      hasLabels: false,
      progress: 0,
    };
    addDatasets([_dataset]);
    ws.datasetUploaded({ dataset: _dataset });
    try {
      const result = await uploadDataset(
        file,
        (value: number) => setProgress(value),
        activeSessionId || "",
        userId,
        description,
      );
      if (result?.did) updateDataset(_dataset.uuid, "did", result.did);
    } catch {
      toast.error("Upload failed");
    } finally {
      setPendingUpload(null);
    }
  };

  const handleFileImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    e.preventDefault();
    if (e.target.files?.length) {
      const file = e.target.files[0];
      if (file) {
        const _dataset = { uuid: randomUUID(), filename: file.name, size: file.size, hasLabels: false, progress: 0 };
        addDatasets([_dataset]);
        ws.datasetImported({ dataset: _dataset });
        await importDataset(file, setProgress, activeSessionId || "", userId);
      }
    }
  };

  const handleRemoveDataset = (id: string) => {
    removeDataset(id);
    ws.datasetRemoved({ datasetId: id });
  };

  const handleRemovePipeline = (id: string) => {
    removePipeline();
    clearRun();
    ws.pipelineRemoved({ pipelineId: id });
  };

  const handleOpenPipeline = () => {
    const head = getHeadVersion();
    if (!head) return;
    setTempPipeline({ uuid: head.id, nodes: head.nodes, edges: head.edges });
  };

  // Expand sidebar then trigger an action
  const expandThen = (fn: () => void) => {
    setSidebarCollapsed(false);
    setTimeout(fn, 320); // wait for transition
  };

  // ── Collapsed icon strip ──────────────────────────────────────────────────
  // items-start keeps every icon at x=0 inside the body's px-2 padding,
  // so all icons land at x=8–44px within the 56px visible clip.
  if (sidebarCollapsed) {
    return (
      <TooltipProvider delayDuration={300}>
      <div className="flex flex-col items-start gap-0.5">
        {/* Data */}
        <IconBtn icon={Database} title="Import dataset" onClick={() => expandThen(() => setImportOpen(true))} />
        <IconBtn icon={Upload} title="Upload dataset" onClick={() => document.getElementById("dataset-upload")?.click()} />
        <input id="dataset-upload" type="file" accept=".csv,text/csv" onChange={handleFileUpload} multiple hidden />

        {datasets.map((d: any) => (
          <IconBtn key={d.uuid} icon={File} title={d.filename} onClick={() => setSidebarCollapsed(false)} />
        ))}

        <div className="w-8 border-t border-border my-1" />

        {/* Pipeline */}
        <input id="pipeline-import" type="file" accept=".json,.py" hidden onChange={handleImportPipeline} />
        <IconBtn
          icon={Database}
          title="Import pipeline"
          disabled={!hasPipelineComposition}
          onClick={() => document.getElementById("pipeline-import")?.click()}
        />
        <IconBtn
          icon={Plus}
          title="Compose pipeline"
          disabled={!hasPipelineComposition}
          onClick={handlePipelineCompose}
        />
        {pipelineHistory && (
          <IconBtn icon={ChartNetwork} title="Open pipeline" onClick={handleOpenPipeline} />
        )}

        <div className="w-8 border-t border-border my-1" />

        {/* Complex panels — expands sidebar */}
        <IconBtn icon={ListChecks} title="Data Science Task" onClick={() => setSidebarCollapsed(false)} />
        <IconBtn icon={FlaskConical} title="Evaluation Procedure" onClick={() => setSidebarCollapsed(false)} />
        <IconBtn icon={Trophy} title="Ranking Objectives" onClick={() => setSidebarCollapsed(false)} />

        <DatasetImportDialog open={importOpen} setOpen={setImportOpen} />
        <DatasetUploadDialog
          file={pendingUpload}
          onCancel={() => setPendingUpload(null)}
          onConfirm={handleConfirmUpload}
        />
      </div>
      </TooltipProvider>
    );
  }

  // ── Full expanded toolbar ─────────────────────────────────────────────────
  return (
    <TooltipProvider delayDuration={300}>
    <div className='flex flex-col'>
      <div className='space-y-6'>
        <div className='space-y-2'>
          <div>
            <h3 className='text-sm font-semibold'>Data</h3>
            <p className='text-xs text-muted-foreground mb-3'>
              Only <span className='font-medium'>.csv</span> files are supported.
            </p>
          </div>

          <div className='space-y-2'>
            <GuidedTooltip targetId='dataset-import' side='right' wrapperClassName='w-full'>
              <Button
                variant='outline'
                className='w-full justify-start bg-transparent'
                size='sm'
                onClick={() => setImportOpen(true)}
              >
                <Database className='h-4 w-4 mr-2' />
                Import
              </Button>
            </GuidedTooltip>
            <DatasetImportDialog open={importOpen} setOpen={setImportOpen} />
            <GuidedTooltip targetId='dataset-upload' side='right' wrapperClassName='w-full'>
              <Button
                variant='outline'
                className='w-full justify-start bg-transparent'
                size='sm'
                onClick={() => document.getElementById("dataset-upload")?.click()}
              >
                <Upload className='h-4 w-4 mr-2' />
                Upload
                <input
                  id='dataset-upload'
                  type='file'
                  accept='.csv,text/csv'
                  onChange={handleFileUpload}
                  multiple
                  hidden
                />
              </Button>
            </GuidedTooltip>
          </div>

          {datasets.length > 0 && (
            <GuidedTooltip targetId='loaded-files' side='right' wrapperClassName='w-full'>
            <div className='mt-4 space-y-2'>
              <h4 className='text-xs font-medium text-muted-foreground'>Loaded Files</h4>
              {datasets.map((dataset: any, index: number) => (
                <div
                  key={dataset.uuid ?? index}
                  className='flex items-center justify-between rounded-md border bg-card px-3 py-2 text-sm'
                >
                  <div className='flex items-center gap-2 flex-1 min-w-0'>
                    <File className='h-4 w-4 text-muted-foreground flex-shrink-0' />
                    <div className='flex-1 min-w-0'>
                      <p className='font-medium truncate'>{dataset.filename}</p>
                    </div>
                  </div>
                  <div className='flex items-center gap-0 flex-shrink-0'>
                    {dataset.did && (
                      <>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant='ghost'
                              size='icon'
                              className='h-6 w-6'
                              onClick={() => ws.feedbackEditRequested()}
                            >
                              <PencilLine className='h-3 w-3 text-muted-foreground' />
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side='right'>Edit column selection</TooltipContent>
                        </Tooltip>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Button
                              variant='ghost'
                              size='icon'
                              className='h-6 w-6'
                              onClick={() => handleToggleVisibility(dataset)}
                            >
                              {dataset.isPublic ? (
                                <Globe className='h-3 w-3 text-blue-500' />
                              ) : (
                                <Lock className='h-3 w-3 text-muted-foreground' />
                              )}
                            </Button>
                          </TooltipTrigger>
                          <TooltipContent side='right'>
                            {dataset.isPublic ? "Make private" : "Make public"}
                          </TooltipContent>
                        </Tooltip>
                      </>
                    )}
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant='ghost'
                          size='icon'
                          className='h-6 w-6'
                          onClick={() => handleRemoveDataset(dataset.uuid)}
                        >
                          <X className='h-3 w-3' />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side='right'>Remove dataset</TooltipContent>
                    </Tooltip>
                  </div>
                </div>
              ))}
            </div>
            </GuidedTooltip>
          )}
        </div>

        <GuidedTooltip targetId='task-selection' side='right' wrapperClassName='w-full'>
          <div className='mt-5 flex flex-col gap-y-1 w-full'>
            <Label>Data Science Task</Label>
            <TaskSelector key='task-selector' />
          </div>
        </GuidedTooltip>

        <div>
          <h3 className='text-sm font-semibold mb-3'>Pipeline</h3>
          <div className='space-y-2'>
            <GuidedTooltip targetId='pipeline-import' side='right' wrapperClassName='w-full'>
              <Button
                variant='outline'
                className='w-full justify-start bg-transparent'
                size='sm'
                onClick={() => document.getElementById("pipeline-import")?.click()}
                disabled={!hasPipelineComposition}
              >
                <input id='pipeline-import' type='file' accept='.json,.py' hidden onChange={handleImportPipeline} />
                <Database className='h-4 w-4 mr-2' />
                Import
              </Button>
            </GuidedTooltip>
            <GuidedTooltip targetId='compose-pipeline' side='right' wrapperClassName='w-full'>
              <Button
                variant='outline'
                className='w-full justify-start bg-transparent'
                size='sm'
                onClick={handlePipelineCompose}
                disabled={!hasPipelineComposition}
              >
                <Plus className='h-4 w-4 mr-2' />
                Compose
              </Button>
            </GuidedTooltip>
          </div>

          {pipelineHistory && (
            <div className='mt-4 space-y-2'>
              <h4 className='text-xs font-medium text-muted-foreground'>Pipelines</h4>
              <div
                onClick={handleOpenPipeline}
                className='flex items-center cursor-pointer justify-between rounded-md border bg-card px-3 py-2 text-sm'
              >
                <div className='flex items-center gap-2 flex-1 min-w-0'>
                  <ChartNetwork className='h-4 w-4 text-muted-foreground' />
                  <div className='flex-1 min-w-0'>
                    <p className='font-medium truncate'>Pipeline</p>
                  </div>
                </div>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant='ghost'
                      size='icon'
                      className='h-6 w-6'
                      onClick={(e) => {
                        e.stopPropagation();
                        handleRemovePipeline(pipelineHistory.uuid);
                      }}
                    >
                      <X className='h-3 w-3' />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent side='right'>Remove pipeline</TooltipContent>
                </Tooltip>
              </div>
            </div>
          )}
        </div>

        <GuidedTooltip targetId='eval-selection' side='right' wrapperClassName='w-full'>
          <div className='flex gap-y-1 flex-col w-full'>
            <Label>Evaluation Procedure</Label>
            <EvaluationProcedure />
          </div>
        </GuidedTooltip>

        <GuidedTooltip targetId='objectives-panel' side='right' wrapperClassName='w-full'>
          <div className='mt-3 flex flex-col gap-y-1 w-full'>
            <Label>Ranking Objectives</Label>
            <RankingObjectives key='ranking-objectives' />
          </div>
        </GuidedTooltip>
      </div>
      <DatasetUploadDialog
        file={pendingUpload}
        onCancel={() => setPendingUpload(null)}
        onConfirm={handleConfirmUpload}
      />
    </div>
    </TooltipProvider>
  );
}
