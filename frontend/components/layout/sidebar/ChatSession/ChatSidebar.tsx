"use client";

import { useState, useMemo } from "react";
import { Plus, Search, MessageSquare, Bot } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useChatSessions } from "@/hooks/useChatSessions";
import type { ChatSession } from "@/types/session";
import { ChatSessionItem } from "./ChatSessionItem";
import { useSessionStore } from "@/store/session";
import { useAgentStore } from "@/store/agent";
import { DarkModeToggle } from "@/components/ui/dark-mode-toggle";
import { GuidedTooltip } from "@/components/ui/guided-tooltip";

export function ChatSidebar() {
  const {
    handleCreateSession,
    handleDeleteSession,
    handleRenameSession,
    selectSession,
  } = useChatSessions();

  const { sessions, activeSessionId } = useSessionStore();
  const { setPanelOpen, agentMode } = useAgentStore();

  const [searchQuery, setSearchQuery] = useState("");
  const [isNewSessionDialogOpen, setIsNewSessionDialogOpen] = useState(false);
  const [isRenameDialogOpen, setIsRenameDialogOpen] = useState(false);
  const [newSessionName, setNewSessionName] = useState("");
  const [sessionToRename, setSessionToRename] = useState<ChatSession | null>(
    null,
  );

  // Filter sessions by search
  const filteredSessions = useMemo(() => {
    if (!searchQuery.trim()) return sessions;
    const query = searchQuery.toLowerCase();
    return sessions.filter((s) => s.name.toLowerCase().includes(query));
  }, [sessions, searchQuery]);

  // Group sessions by date
  const groupedSessions = useMemo(() => {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const lastWeek = new Date(today.getTime() - 7 * 24 * 60 * 60 * 1000);

    const groups = {
      today: [] as ChatSession[],
      lastWeek: [] as ChatSession[],
      older: [] as ChatSession[],
    };

    filteredSessions.forEach((s) => {
      const sessionDate = new Date(s.updated_at);
      if (sessionDate >= today) groups.today.push(s);
      else if (sessionDate >= lastWeek) groups.lastWeek.push(s);
      else groups.older.push(s);
    });

    return groups;
  }, [filteredSessions]);

  const openRenameDialog = (session: ChatSession) => {
    setSessionToRename(session);
    setNewSessionName(session.name);
    setIsRenameDialogOpen(true);
  };

  const handleRename = () => {
    if (sessionToRename && newSessionName.trim()) {
      handleRenameSession(sessionToRename.session_id, newSessionName.trim());
    }
    setIsRenameDialogOpen(false);
    setNewSessionName("");
    setSessionToRename(null);
  };

  const handleCreate = async () => {
    if (newSessionName.trim()) {
      setIsNewSessionDialogOpen(false);
      setNewSessionName("");
      try {
        await handleCreateSession(newSessionName.trim());
      } catch (err) {
        const { toast } = await import("sonner");
        toast.error("Failed to create session");
      }
    }
  };

  return (
    <>
      <div className='flex h-full w-full flex-col bg-card px-3'>
        {/* Sessions header */}
        <div className='flex items-center justify-between border-b border-border py-3'>
          <div className='flex items-center gap-2'>
            <MessageSquare className='h-4 w-4 text-muted-foreground' />
            <span className='text-sm font-medium'>Sessions</span>
          </div>
          <Button
            size='icon'
            variant='ghost'
            className='h-7 w-7'
            onClick={() => setIsNewSessionDialogOpen(true)}
          >
            <Plus className='h-4 w-4' />
          </Button>
        </div>

        {/* Search */}
        <div className=' py-3 mb-3'>
          <div className='relative'>
            <Search className='absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground' />
            <Input
              placeholder='Search sessions...'
              className='pl-9 bg-background'
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>
        </div>

        {/* Session list */}

        {Object.entries(groupedSessions).map(([key, list]) => {
          if (!list.length) return null;
          const label =
            key === "today"
              ? "Today"
              : key === "lastWeek"
                ? "Last 7 days"
                : "Older";

          return (
            <div key={key} className='mb-4'>
              <div className='mb-2 px-2 text-xs font-medium text-muted-foreground uppercase tracking-wider'>
                {label}
              </div>
              <div className='space-y-1'>
                {list
                  .sort(
                    (a, b) =>
                      new Date(b.updated_at).getTime() -
                      new Date(a.updated_at).getTime(),
                  )
                  .map((session) => (
                    <ChatSessionItem
                      key={session.session_id}
                      session={session}
                      isActive={session.session_id === activeSessionId}
                      onSelect={() => selectSession(session.session_id)}
                      onRename={() => openRenameDialog(session)}
                      onDelete={() => handleDeleteSession(session.session_id)}
                    />
                  ))}
              </div>
            </div>
          );
        })}

        {/* Footer */}
        <div className='mt-auto border-t border-border pt-4 pb-4'>
          <div className='flex items-center justify-between'>
            <GuidedTooltip targetId='agent-panel' side='top' wrapperClassName='contents'>
              <Button
                variant='ghost'
                size='icon'
                className='h-7 w-7 relative'
                onClick={() => setPanelOpen(true)}
                title='Agent Panel'
              >
                <Bot className='h-4 w-4' />
                {agentMode && (
                  <span className='absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-primary' />
                )}
              </Button>
            </GuidedTooltip>
            <div className='text-xs text-muted-foreground'>
              &copy; 2022&ndash;{new Date().getFullYear()} Dorian
            </div>
          </div>
        </div>
      </div>

      {/* Create Session Dialog */}
      <Dialog
        open={isNewSessionDialogOpen}
        onOpenChange={setIsNewSessionDialogOpen}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create New Session</DialogTitle>
            <DialogDescription>
              Give your new session a name to organize your pipelines.
            </DialogDescription>
          </DialogHeader>
          <div className='space-y-4 py-4'>
            <Label htmlFor='new-session'>Session Name</Label>
            <Input
              className='mt-2'
              id='new-session'
              placeholder='e.g., Customer Analytics'
              value={newSessionName}
              onChange={(e) => setNewSessionName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleCreate()}
            />
          </div>
          <DialogFooter>
            <Button
              variant='outline'
              onClick={() => setIsNewSessionDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={!newSessionName.trim()}>
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Rename Dialog */}
      <Dialog open={isRenameDialogOpen} onOpenChange={setIsRenameDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rename Session</DialogTitle>
            <DialogDescription>
              Enter a new name for this session.
            </DialogDescription>
          </DialogHeader>
          <div className='space-y-4 py-4'>
            <Label htmlFor='rename-session'>New Name</Label>
            <Input
              id='rename-session'
              value={newSessionName}
              onChange={(e) => setNewSessionName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleRename()}
            />
          </div>
          <DialogFooter>
            <Button
              variant='outline'
              onClick={() => setIsRenameDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button onClick={handleRename} disabled={!newSessionName.trim()}>
              Rename
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
