"use client";

import { useState } from "react";
import { Bot } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useChatSessions } from "@/hooks/useChatSessions";
import useWebSocketStore from "@/store/web-socket";
import { usePipelineStore } from "@/store/pipeline";
import { usePipelineRunStore } from "@/store/pipeline-run";
import { useSessionStore } from "@/store/session";
import { useNotificationsStore } from "@/store/notifications";
import { useAgentStore } from "@/store/agent";
import { ws } from "@/helpers/ws-events";
import ProgressBar from "@/components/ProgressTracker/ProgressBar";
import NotificationsPopover from "./notifications";
import { SessionControls } from "./SessionControls";
import { PipelineActions } from "./PipelineActions";
import { UserMenu } from "./UserMenu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { GuidedTooltip } from "@/components/ui/guided-tooltip";

export default function PipelineHeader() {
  const pipelineHistory = usePipelineStore((s) => s.pipelineHistory);
  const tempPipeline = usePipelineStore((s) => s.tempPipeline);
  const setTempPipeline = usePipelineStore((s) => s.setTempPipeline);
  const setSuggestions = usePipelineStore((s) => s.setSuggestions);
  const setProgressItems = usePipelineStore((s) => s.setProgressItems);
  const items = useNotificationsStore((s) => s.items);
  const markAllRead = useNotificationsStore((s) => s.markAllRead);
  const clear = useNotificationsStore((s) => s.clear);
  const markRead = useNotificationsStore((s) => s.markRead);
  const { handleRenameSession, selectSession } = useChatSessions();
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const clearRun = usePipelineRunStore((s) => s.clearRun);
  const disconnect = useWebSocketStore((s) => s.disconnect);
  const connectionStatus = useWebSocketStore((s) => s.connectionStatus);
  const setPanelOpen = useAgentStore((s) => s.setPanelOpen);
  const agentMode = useAgentStore((s) => s.agentMode);

  const activeSession = activeSessionId
    ? sessions.find((s) => s.session_id === activeSessionId)
    : null;

  const [sessionName, setSessionName] = useState(activeSession?.name || "");

  const onSessionNameChange = (name: string) => {
    setSessionName(name);
    if (activeSessionId) {
      handleRenameSession(activeSessionId, name.trim());
      ws.sessionRenamed({ sessionId: activeSessionId, name: name.trim() });
    }
  };

  const goBack = () => {
    if (tempPipeline) {
      setTempPipeline(null);
      setSuggestions([]);
      setProgressItems([]);
      clearRun();
    } else {
      selectSession("");
      disconnect();
      setActiveSessionId(null);
    }
  };

  return (
    <div className='flex w-full items-center justify-between border-b border-border bg-card px-4 py-3'>
      <SessionControls
        sessionName={sessionName}
        connectionStatus={connectionStatus}
        onGoBack={goBack}
        onSessionNameChange={onSessionNameChange}
      />

      <ProgressBar />

      <div className='flex items-center gap-2'>
        <PipelineActions connectionStatus={connectionStatus} />

        <NotificationsPopover
          items={items}
          onMarkAllRead={markAllRead}
          onClear={clear}
          onItemClick={(n) => markRead(n.id)}
        />

        <GuidedTooltip targetId='agent-panel' side='bottom'>
          <TooltipProvider delayDuration={300}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant='ghost'
                  size='icon'
                  className='relative'
                  onClick={() => setPanelOpen(true)}
                >
                  <Bot className='h-4 w-4' />
                  {agentMode && (
                    <span className='absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-primary' />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>Agent Panel</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </GuidedTooltip>

        <UserMenu />
      </div>
    </div>
  );
}
