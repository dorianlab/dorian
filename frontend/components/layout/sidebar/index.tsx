"use client";

import Image from "next/image";
import Logo from "@/app/logo.svg";
import { useEffect } from "react";
import { signOut } from "next-auth/react";
import { ChatSidebar } from "@/components/layout/sidebar/ChatSession/ChatSidebar";
import { useChatSessions } from "@/hooks/useChatSessions";
import { LogOut, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import PipelineToolbar from "@/components/layout/sidebar/imports";
import { useSessionStore } from "@/store/session";
import { useUIStore } from "@/store/ui";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { DarkModeToggle } from "@/components/ui/dark-mode-toggle";

export default function Sidebar() {
  const { handleRenameSession } = useChatSessions();
  const { activeSessionId } = useSessionStore((state) => state);
  const { sidebarCollapsed, setSidebarCollapsed } = useUIStore();

  // Always expand when leaving a session
  useEffect(() => {
    if (!activeSessionId) setSidebarCollapsed(false);
  }, [activeSessionId]);

  return (
    <div className='flex flex-col w-72 h-full bg-card border-r border-border'>

      {/* ── Header — only when no session ── */}
      {!activeSessionId && (
        <div className='flex items-center h-16 shrink-0 px-4 gap-3 border-b border-border'>
          <Image className='h-8 w-auto shrink-0' src={Logo} alt='Dorian' priority />
          <h1 className='text-lg font-serif text-foreground whitespace-nowrap'>Dorian</h1>
          <div className='ml-auto flex items-center gap-1 shrink-0'>
            <DarkModeToggle />
            <TooltipProvider delayDuration={300}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant='ghost'
                    size='icon'
                    className='text-muted-foreground hover:text-foreground'
                    onClick={() => signOut({ callbackUrl: "/login" })}
                  >
                    <LogOut className='h-4 w-4' />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Log out</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
        </div>
      )}

      {/* no spacer needed — header is in a separate row above the sidebar */}

      {/* ── Scrollable body ── */}
      <div className={`flex-1 overflow-y-auto overflow-x-hidden small-scrollbar ${activeSessionId ? 'py-3' : ''} ${sidebarCollapsed ? 'px-2' : activeSessionId ? 'px-3' : ''}`}>
        {activeSessionId ? <PipelineToolbar /> : <ChatSidebar />}
      </div>

      {/* ── Collapse toggle — only when session is active ── */}
      {activeSessionId && (
        <div className='shrink-0 border-t border-border p-2'>
          <TooltipProvider delayDuration={300}>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
                  className='flex items-center gap-3 w-full px-3 py-2 rounded-md
                             text-muted-foreground hover:text-foreground hover:bg-accent
                             transition-colors text-sm'
                >
                  {sidebarCollapsed ? (
                    <PanelLeftOpen className='h-4 w-4 shrink-0' />
                  ) : (
                    <>
                      <PanelLeftClose className='h-4 w-4 shrink-0' />
                      <span className='whitespace-nowrap'>Collapse sidebar</span>
                    </>
                  )}
                </button>
              </TooltipTrigger>
              <TooltipContent side='right'>
                {sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      )}
    </div>
  );
}
