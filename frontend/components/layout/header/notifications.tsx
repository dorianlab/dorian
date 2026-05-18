"use client";

import * as React from "react";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/helpers/utils";
import type { PipelineNotification, NotificationKind } from "@/types/notifications";
import {
  Bell,
  CheckCheck,
  Trash2,
  Info,
  CheckCircle2,
  AlertTriangle,
  XCircle,
} from "lucide-react";
function timeAgo(ts: number) {
  const diff = Date.now() - ts;
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

const kindConfig: Record<
  NotificationKind,
  { icon: React.ElementType; color: string; bg: string }
> = {
  info: {
    icon: Info,
    color: "text-blue-500",
    bg: "bg-blue-500/10",
  },
  success: {
    icon: CheckCircle2,
    color: "text-emerald-500",
    bg: "bg-emerald-500/10",
  },
  warning: {
    icon: AlertTriangle,
    color: "text-amber-500",
    bg: "bg-amber-500/10",
  },
  error: {
    icon: XCircle,
    color: "text-red-500",
    bg: "bg-red-500/10",
  },
};

export default function NotificationsPopover({
  items,
  onItemClick,
  onMarkAllRead,
  onClear,
}: {
  items: PipelineNotification[];
  onItemClick?: (item: PipelineNotification) => void;
  onMarkAllRead?: () => void;
  onClear?: () => void;
}) {
  const unreadCount = useMemo(
    () => items.filter((n) => !n.read).length,
    [items],
  );

  const sorted = useMemo(() => {
    return [...items].sort((a, b) => b.createdAt - a.createdAt);
  }, [items]);

  return (
    <Popover>
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild>
            <PopoverTrigger asChild>
              <Button variant='ghost' size='icon' className='relative'>
                <Bell className='h-4 w-4' />

                {unreadCount > 0 && (
                  <span className='absolute -top-1 -right-1'>
                    <Badge
                      variant='destructive'
                      className='h-5 min-w-5 text-white px-1.5 text-[10px] leading-5 rounded-full'
                    >
                      {unreadCount > 99 ? "99+" : unreadCount}
                    </Badge>
                  </span>
                )}
              </Button>
            </PopoverTrigger>
          </TooltipTrigger>
          <TooltipContent>Notifications</TooltipContent>
        </Tooltip>
      </TooltipProvider>

      <PopoverContent
        className='w-[400px] max-h-[480px] p-0 overflow-hidden'
        align='end'
      >
        <div className='px-4 py-3'>
          <div className='flex items-center justify-between gap-3'>
            <div>
              <div className='font-semibold text-sm'>Notifications</div>
              <div className='text-xs text-muted-foreground'>
                Stay updated with your latest activity
              </div>
            </div>

            <TooltipProvider delayDuration={300}>
              <div className='flex items-center gap-1'>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant='ghost'
                      size='icon'
                      className='h-8 w-8'
                      onClick={onMarkAllRead}
                      disabled={!sorted.length || unreadCount === 0}
                    >
                      <CheckCheck className='h-4 w-4' />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Mark all as read</TooltipContent>
                </Tooltip>

                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant='ghost'
                      size='icon'
                      className='h-8 w-8'
                      onClick={onClear}
                      disabled={!sorted.length}
                    >
                      <Trash2 className='h-4 w-4' />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Clear all notifications</TooltipContent>
                </Tooltip>
              </div>
            </TooltipProvider>
          </div>
        </div>

        <Separator />

        {!sorted.length ? (
          <div className='px-4 py-16 text-center'>
            <Bell className='h-10 w-10 mx-auto text-muted-foreground/40 mb-3' />
            <p className='text-sm text-muted-foreground'>
              No notifications yet
            </p>
            <p className='text-xs text-muted-foreground/70 mt-1'>
              {"We'll notify you when something arrives"}
            </p>
          </div>
        ) : (
          <ScrollArea className='h-[360px]'>
            <div className='p-2 space-y-1'>
              {sorted.map((n) => {
                const config = kindConfig[n.kind];
                const Icon = config.icon;

                return (
                  <button
                    key={n.id}
                    type='button'
                    onClick={() => onItemClick?.(n)}
                    className={cn(
                      "w-full text-left rounded-lg border p-3 transition-all duration-200",
                      "hover:bg-accent/50 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1",
                      n.read
                        ? "border-border bg-transparent opacity-80"
                        : "border-border bg-accent/80",
                    )}
                  >
                    <div className='flex gap-3'>
                      <div
                        className={cn(
                          "shrink-0 h-8 w-8 rounded-full flex items-center justify-center",
                          config.bg,
                        )}
                      >
                        <Icon className={cn("h-4 w-4", config.color)} />
                      </div>

                      <div className='min-w-0 flex-1'>
                        <div className='flex items-center justify-between gap-2'>
                          <div
                            className={cn(
                              "text-sm truncate",
                              !n.read && "font-base",
                            )}
                          >
                            {n.title}
                          </div>
                          <div className='text-[11px] text-muted-foreground shrink-0'>
                            {timeAgo(n.createdAt)}
                          </div>
                        </div>

                        {n.message && (
                          <div className='mt text-[11px]  text-muted-foreground line-clamp-2'>
                            {n.message}
                          </div>
                        )}
                      </div>

                      {!n.read && (
                        <div className='shrink-0 self-center'>
                          <div className='h-2 w-2 rounded-full bg-primary' />
                        </div>
                      )}
                    </div>
                  </button>
                );
              })}
            </div>
          </ScrollArea>
        )}
      </PopoverContent>
    </Popover>
  );
}
