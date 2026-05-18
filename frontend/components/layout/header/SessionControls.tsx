"use client";

import { useMemo } from "react";
import { ArrowLeft, History } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { usePipelineStore } from "@/store/pipeline";
import { useSessionStore } from "@/store/session";
import { ws } from "@/helpers/ws-events";
import { GuidedTooltip } from "@/components/ui/guided-tooltip";
import moment from "moment";

interface SessionControlsProps {
  sessionName: string;
  connectionStatus: string;
  onGoBack: () => void;
  onSessionNameChange: (name: string) => void;
}

const statusDotClass: Record<string, string> = {
  idle: "bg-gray-400",
  connecting: "bg-amber-400 animate-pulse",
  connected: "bg-green-500",
  reconnecting: "bg-amber-400 animate-pulse",
  offline: "bg-red-500",
  error: "bg-red-500 animate-pulse",
};

export function SessionControls({
  sessionName,
  connectionStatus,
  onGoBack,
  onSessionNameChange,
}: SessionControlsProps) {
  const pipelineHistory = usePipelineStore((s) => s.pipelineHistory);
  const tempPipeline = usePipelineStore((s) => s.tempPipeline);
  const restoreVersion = usePipelineStore((s) => s.restoreVersion);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);

  const versions = useMemo(() => {
    if (!pipelineHistory?.pipelines?.length) return [];

    const sorted = [...pipelineHistory.pipelines].sort((a, b) => {
      const ta = new Date(a.createdAt || 0).getTime();
      const tb = new Date(b.createdAt || 0).getTime();
      return tb - ta;
    });

    return sorted.map((v) => ({
      id: v.id,
      timestamp: moment(v.createdAt).fromNow(),
      author: v.createdBy || "You",
      changes: v.message || "Saved",
      isCurrent: v.id === pipelineHistory.headId,
    }));
  }, [pipelineHistory?.pipelines, pipelineHistory?.headId]);

  const handleRestore = (versionId: string) => {
    if (!pipelineHistory || !restoreVersion) return;
    restoreVersion(versionId);
    ws.pipelineVersionRestored({
      sessionId: activeSessionId,
      pipelineId: pipelineHistory.uuid,
      fromHeadId: pipelineHistory.headId,
      versionId,
    });
  };

  const dotClass = statusDotClass[connectionStatus] ?? "bg-gray-400";

  return (
    <TooltipProvider delayDuration={300}>
    <div className='flex items-center gap-4'>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant='ghost' size='icon' onClick={onGoBack}>
            <ArrowLeft className='h-4 w-4' />
          </Button>
        </TooltipTrigger>
        <TooltipContent>{tempPipeline ? 'Back to recommendations' : 'Back to sessions'}</TooltipContent>
      </Tooltip>

      <Separator orientation='vertical' className='h-6' />

      {tempPipeline && (
        <GuidedTooltip targetId='version-history' side='bottom'>
        <Popover>
          <PopoverTrigger asChild>
            <Button variant='ghost' size='icon'>
              <History className='h-4 w-4' />
            </Button>
          </PopoverTrigger>

          <PopoverContent
            className='w-80 ms-3 max-h-[400px] overflow-y-auto small-scrollbar'
            align='end'
          >
            <div className='space-y-4'>
              <div>
                <h4 className='font-semibold text-sm mb-1'>Version History</h4>
                <p className='text-xs text-muted-foreground'>
                  View and restore previous versions
                </p>
              </div>

              {!versions.length ? (
                <div className='text-sm text-muted-foreground'>
                  No versions yet. Click Save to create your first version.
                </div>
              ) : (
                <div className='space-y-2'>
                  {versions.map((version) => (
                    <div
                      key={version.id}
                      className='flex items-start gap-3 p-3 rounded-lg border border-border hover:bg-accent/50 transition-colors'
                    >
                      <div className='flex-1 min-w-0'>
                        <div className='flex items-center gap-2 mb-1'>
                          <span className='font-medium text-sm'>
                            {version.id}
                          </span>
                          {version.isCurrent && (
                            <Badge variant='secondary' className='text-xs'>
                              Current
                            </Badge>
                          )}
                        </div>

                        <p className='text-sm text-foreground mb-1'>
                          {version.changes}
                        </p>

                        <div className='flex items-center gap-2 text-xs text-muted-foreground'>
                          <span>{version.author}</span>
                          <span>•</span>
                          <span>{version.timestamp}</span>
                        </div>
                      </div>

                      {!version.isCurrent && (
                        <Button
                          variant='ghost'
                          size='sm'
                          className='text-xs'
                          onClick={() => handleRestore(version.id)}
                        >
                          Restore
                        </Button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </PopoverContent>
        </Popover>
        </GuidedTooltip>
      )}

      <div className='flex items-center gap-3'>
        <input
          type='text'
          value={sessionName}
          onChange={(e) => onSessionNameChange(e.target.value)}
          className='bg-transparent w-fit text-lg font-semibold outline-none focus:ring-2 focus:ring-ring rounded px-2 py-1'
        />
        <Tooltip>
          <TooltipTrigger asChild>
            <span
              className={`h-2 w-2 rounded-full flex-shrink-0 ${dotClass}`}
              aria-label={`Connection status: ${connectionStatus}`}
            />
          </TooltipTrigger>
          <TooltipContent>Connection: {connectionStatus}</TooltipContent>
        </Tooltip>
      </div>
    </div>
    </TooltipProvider>
  );
}
