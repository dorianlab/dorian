"use client";

import { GitPullRequestArrow, Play, Square, Download, Save, Share2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { GuidedTooltip } from "@/components/ui/guided-tooltip";
import { ws } from "@/helpers/ws-events";
import { usePipelineStore } from "@/store/pipeline";
import { usePipelineRunStore } from "@/store/pipeline-run";
import { useSessionStore } from "@/store/session";
import { toast } from "sonner";
import { getPassphrase } from "@/lib/vault-crypto";
import { storePassphraseNonce } from "@/app/api/vault";
import type { Operator } from "@/types/pipeline";
import { randomUUID } from "@/helpers/uuid";

interface PipelineActionsProps {
  connectionStatus: string;
}

/**
 * Check whether the current pipeline contains any env-type parameter nodes.
 * These require vault passphrase transport at execution time.
 */
function _pipelineHasEnvParams(): boolean {
  const state = usePipelineStore.getState();
  // Check BOTH sources: draftPipeline (ReactFlow live state) may lag behind
  // tempPipeline when a mitigation rewrite injects new env-type nodes.
  // If either source contains an env param, we need the vault nonce.
  const sources = [state.draftPipeline?.nodes, state.tempPipeline?.nodes];
  for (const nodes of sources) {
    if (!nodes) continue;
    for (const n of Object.values(nodes as Record<string, Operator>)) {
      const dt = (n.dtype ?? n.type ?? "").toLowerCase();
      if (dt === "env") return true;
    }
  }
  return false;
}

export function PipelineActions({ connectionStatus }: PipelineActionsProps) {
  const tempPipeline = usePipelineStore((s) => s.tempPipeline);
  const pipelineHistory = usePipelineStore((s) => s.pipelineHistory);
  const sourceExtractionId = usePipelineStore((s) => s.sourceExtractionId);
  const setSourceExtractionId = usePipelineStore((s) => s.setSourceExtractionId);
  const createPipelineIfMissing = usePipelineStore((s) => s.createPipelineIfMissing);
  const saveNewVersionFromCurrent = usePipelineStore((s) => s.saveNewVersionFromCurrent);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const pipelineRun = usePipelineRunStore((s) => s.pipelineRun);

  const isRunning =
    pipelineRun?.status === "running" || pipelineRun?.status === "pending";

  const handleStopPipeline = () => {
    if (!pipelineRun?.run_id) return;
    ws.pipelineCancelClicked({ runId: pipelineRun.run_id });
  };

  const handleRunPipeline = async () => {
    if (!tempPipeline) return;

    if (!pipelineHistory) {
      createPipelineIfMissing?.();
    } else {
      saveNewVersionFromCurrent?.();
    }

    const latest = usePipelineStore.getState().pipelineHistory;

    ws.pipelineSaved(JSON.parse(JSON.stringify(latest ?? {})));

    // --- Vault nonce transport ---
    // If the pipeline contains env-type params (API keys stored in the vault),
    // we need to send the passphrase to the backend via a one-time nonce so
    // it can decrypt the secrets in-memory during execution.
    let vaultNonce: string | undefined;
    if (_pipelineHasEnvParams()) {
      const passphrase = getPassphrase();
      if (!passphrase) {
        toast.error(
          "Vault passphrase required — open Environment Variables and enter your passphrase first.",
        );
        return;
      }
      vaultNonce = randomUUID();
      try {
        await storePassphraseNonce(vaultNonce, passphrase);
      } catch {
        toast.error("Failed to prepare vault credentials for execution.");
        return;
      }
    }

    ws.pipelineRunClicked({
      sessionId: activeSessionId,
      pipelineId: latest?.headId,
      ...(vaultNonce ? { vaultNonce } : {}),
    });
  };

  const handleSubmitCorrection = () => {
    if (!sourceExtractionId || !tempPipeline) return;

    const correctedPipeline = JSON.parse(
      JSON.stringify({
        uuid: tempPipeline.uuid,
        nodes: tempPipeline.nodes,
        edges: tempPipeline.edges,
      }),
    );

    ws.extractionCorrected({
      extractionId: sourceExtractionId,
      correctedPipeline,
    });

    setSourceExtractionId(null);
    toast.success("Correction submitted — thank you for improving the extractor!");
  };

  if (!tempPipeline) return null;

  return (
    <TooltipProvider delayDuration={300}>
    <div className='flex items-center gap-2'>
      {sourceExtractionId && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button onClick={handleSubmitCorrection} variant='outline' size='sm'>
              <GitPullRequestArrow className='h-4 w-4' />
              Submit Correction
            </Button>
          </TooltipTrigger>
          <TooltipContent>Submit the corrected pipeline to improve future extractions</TooltipContent>
        </Tooltip>
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            onClick={() =>
              ws.pipelineShareClicked({
                sessionId: activeSessionId,
                pipelineId: tempPipeline.uuid,
              })
            }
            disabled
            variant='outline'
            size='sm'
          >
            <Share2 className='h-4 w-4' />
            Share
          </Button>
        </TooltipTrigger>
        <TooltipContent>Share this pipeline with collaborators (coming soon)</TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button disabled variant='outline' size='sm'>
            <Download className='h-4 w-4' />
            Export
          </Button>
        </TooltipTrigger>
        <TooltipContent>Export pipeline as JSON or Python script (coming soon)</TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            onClick={() =>
              ws.pipelineSaved({
                sessionId: activeSessionId,
                pipelineId: tempPipeline.uuid,
              })
            }
            disabled
            variant='outline'
            size='sm'
          >
            <Save className='h-4 w-4' />
            Save
          </Button>
        </TooltipTrigger>
        <TooltipContent>Save the current pipeline version (coming soon)</TooltipContent>
      </Tooltip>
      {isRunning ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              onClick={handleStopPipeline}
              size='sm'
              variant='destructive'
              disabled={connectionStatus !== "connected"}
            >
              <Square className='h-4 w-4' />
              Stop
            </Button>
          </TooltipTrigger>
          <TooltipContent>Stop the running pipeline</TooltipContent>
        </Tooltip>
      ) : (
        <GuidedTooltip targetId='run-pipeline-button' side='bottom'>
          <Button
            onClick={handleRunPipeline}
            size='sm'
            disabled={connectionStatus !== "connected"}
          >
            <Play className='h-4 w-4' />
            Run Pipeline
          </Button>
        </GuidedTooltip>
      )}
    </div>
    </TooltipProvider>
  );
}
