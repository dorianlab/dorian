"use client";

import { useCallback, useEffect, useState } from "react";
import { signOut, useSession } from "next-auth/react";
import { LogOut, User, KeyRound, HardDrive, Loader2, BookOpen, Power, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import useWebSocketStore from "@/store/web-socket";
import { useSessionStore } from "@/store/session";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { DarkModeToggle } from "@/components/ui/dark-mode-toggle";
import { EnvironmentDialog } from "@/components/vault/EnvironmentDialog";
import {
  checkAdmin,
  triggerBackup,
  triggerShutdown,
  listBackups,
  triggerRestore,
  type BackupEntry,
} from "@/app/api/admin";
import { useTooltipStore } from "@/store/tooltip";
import toast from "react-hot-toast";

export function UserMenu() {
  const { data: authSession } = useSession();
  const { disconnect } = useWebSocketStore();
  const { setActiveSessionId } = useSessionStore();
  const startTour = useTooltipStore((s) => s.startTour);
  const [envDialogOpen, setEnvDialogOpen] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);
  const [backingUp, setBackingUp] = useState(false);
  const [shuttingDown, setShuttingDown] = useState(false);
  const [restoreDialogOpen, setRestoreDialogOpen] = useState(false);
  const [backups, setBackups] = useState<BackupEntry[]>([]);
  const [loadingBackups, setLoadingBackups] = useState(false);
  const [selectedBackup, setSelectedBackup] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);

  // Use GitHub login (username) for admin checks, falling back to display name
  // for demo users.  The NextAuth jwt callback persists profile.login into the
  // session so config.admin.usernames matches GitHub logins, not display names.
  const login = (authSession?.user as Record<string, unknown> | undefined)?.login as string | undefined;
  const username = login ?? authSession?.user?.name ?? "";

  useEffect(() => {
    if (username) {
      checkAdmin(username).then(setIsAdmin);
    }
  }, [username]);

  const handleBackup = useCallback(async () => {
    if (!username) return;
    setBackingUp(true);
    try {
      const result = await triggerBackup(username);
      if (result.ok) {
        toast.success(`Backup saved to ${result.path}`);
      } else {
        toast.error(`Backup completed with errors: ${result.errors.join(", ")}`);
      }
    } catch (err) {
      toast.error("Backup failed");
    } finally {
      setBackingUp(false);
    }
  }, [username]);

  const handleShutdown = useCallback(async () => {
    if (!username) return;
    if (!window.confirm("Are you sure you want to shut down the system? A backup will be created first.")) return;
    setShuttingDown(true);
    try {
      const result = await triggerShutdown(username);
      if (result.backup.ok) {
        toast.success("System is shutting down. Backup saved.");
      } else {
        toast.error(`Shutdown initiated but backup had errors: ${result.backup.errors.join(", ")}`);
      }
    } catch (err) {
      toast.error("Failed to initiate shutdown");
    } finally {
      setShuttingDown(false);
    }
  }, [username]);

  const handleOpenRestore = useCallback(async () => {
    if (!username) return;
    setRestoreDialogOpen(true);
    setLoadingBackups(true);
    setSelectedBackup(null);
    try {
      const list = await listBackups(username);
      setBackups(list);
    } catch (err) {
      toast.error("Failed to list backups");
      setBackups([]);
    } finally {
      setLoadingBackups(false);
    }
  }, [username]);

  const handleRestore = useCallback(async () => {
    if (!username || !selectedBackup) return;
    if (!window.confirm(`Restore from ${selectedBackup}? This will overwrite current data.`)) return;
    setRestoring(true);
    try {
      const result = await triggerRestore(username, selectedBackup);
      if (result.ok) {
        toast.success(`Restored from ${result.source}`);
        setRestoreDialogOpen(false);
      } else {
        toast.error(`Restore completed with errors: ${result.errors.join(", ")}`);
      }
    } catch (err) {
      toast.error("Restore failed");
    } finally {
      setRestoring(false);
    }
  }, [username, selectedBackup]);

  const handleLogout = () => {
    disconnect();
    setActiveSessionId(null);
    signOut({ callbackUrl: "/login" });
  };

  return (
    <>
      <DropdownMenu>
        <TooltipProvider delayDuration={300}>
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <Button variant='ghost' size='icon'>
                  <User className='h-4 w-4' />
                </Button>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent>Account menu</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <DropdownMenuContent align='end'>
          <DropdownMenuLabel>
            {authSession?.user?.name ?? "User"}
          </DropdownMenuLabel>
          <DropdownMenuSeparator />

          <DropdownMenuItem onClick={() => setEnvDialogOpen(true)}>
            <KeyRound className='mr-2 h-4 w-4' />
            Environment Variables
          </DropdownMenuItem>

          <DropdownMenuItem onClick={() => startTour()}>
            <BookOpen className='mr-2 h-4 w-4' />
            Start Tour
          </DropdownMenuItem>

          {isAdmin && (
            <>
              <DropdownMenuItem onClick={handleBackup} disabled={backingUp}>
                {backingUp ? (
                  <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                ) : (
                  <HardDrive className='mr-2 h-4 w-4' />
                )}
                {backingUp ? "Creating Backup..." : "System Backup"}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={handleOpenRestore}>
                <Upload className='mr-2 h-4 w-4' />
                Restore Backup
              </DropdownMenuItem>
              <DropdownMenuItem onClick={handleShutdown} disabled={shuttingDown} className='text-destructive focus:text-destructive'>
                {shuttingDown ? (
                  <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                ) : (
                  <Power className='mr-2 h-4 w-4' />
                )}
                {shuttingDown ? "Shutting Down..." : "Graceful Shutdown"}
              </DropdownMenuItem>
            </>
          )}

          <DropdownMenuSeparator />

          <DarkModeToggle className='mb-2 ms-1' />

          <DropdownMenuItem onClick={handleLogout}>
            <LogOut className='mr-2 h-4 w-4' />
            Log out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <EnvironmentDialog open={envDialogOpen} onOpenChange={setEnvDialogOpen} />

      <Dialog open={restoreDialogOpen} onOpenChange={setRestoreDialogOpen}>
        <DialogContent className='max-w-lg'>
          <DialogHeader>
            <DialogTitle>Restore from Backup</DialogTitle>
            <DialogDescription>
              Select a backup to restore. This overwrites Redis, docstore, Postgres, and Neo4j state.
            </DialogDescription>
          </DialogHeader>
          <div className='max-h-80 overflow-y-auto'>
            {loadingBackups ? (
              <div className='flex items-center justify-center py-8'>
                <Loader2 className='h-5 w-5 animate-spin' />
              </div>
            ) : backups.length === 0 ? (
              <p className='text-sm text-muted-foreground py-4 text-center'>No backups found.</p>
            ) : (
              <ul className='space-y-1'>
                {backups.map((b) => {
                  const ts = (b.manifest?.timestamp as string) ?? "";
                  const trig = (b.manifest?.triggered_by as string) ?? "";
                  return (
                    <li key={b.name}>
                      <button
                        type='button'
                        onClick={() => setSelectedBackup(b.name)}
                        className={`w-full text-left px-3 py-2 rounded text-sm border ${
                          selectedBackup === b.name ? "border-primary bg-accent" : "border-border hover:bg-accent/50"
                        }`}
                      >
                        <div className='font-mono'>{b.name}</div>
                        {(ts || trig) && (
                          <div className='text-xs text-muted-foreground mt-0.5'>
                            {ts}{ts && trig ? " • " : ""}{trig}
                          </div>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
          <DialogFooter>
            <Button variant='outline' onClick={() => setRestoreDialogOpen(false)} disabled={restoring}>
              Cancel
            </Button>
            <Button onClick={handleRestore} disabled={!selectedBackup || restoring}>
              {restoring ? (
                <>
                  <Loader2 className='mr-2 h-4 w-4 animate-spin' />
                  Restoring...
                </>
              ) : (
                "Restore"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
