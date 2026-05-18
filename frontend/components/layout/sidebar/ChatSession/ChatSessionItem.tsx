"use client";

import { MoreVertical, Pencil, Trash2 } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import clsx from "clsx";
import moment from "moment";
import type { ChatSession } from "@/types/session";

interface ChatSessionItemProps {
  session: ChatSession;
  isActive: boolean;
  onSelect: () => void;
  onRename: () => void;
  onDelete: () => void;
}

export function ChatSessionItem({
  session,
  isActive,
  onSelect,
  onRename,
  onDelete,
}: ChatSessionItemProps) {
  return (
    <div
      className={clsx(
        "group relative flex items-center justify-between rounded-lg px-3 py-2 transition-colors cursor-pointer",
        isActive ? "bg-accent" : "hover:bg-accent/50"
      )}
    >
      <button
        onClick={onSelect}
        className='flex flex-1 flex-col items-start gap-1 text-left'
      >
        <span className='text-sm font-medium line-clamp-1'>{session.name}</span>
        <div className='flex items-center gap-2 text-xs text-muted-foreground'>
          <span>{moment(session.updated_at).fromNow()}</span>
        </div>
      </button>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant='ghost'
            size='icon'
            className='h-7 w-7 opacity-0 group-hover:opacity-100 transition-opacity'
          >
            <MoreVertical className='h-4 w-4' />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align='end'>
          <DropdownMenuItem onClick={onRename}>
            <Pencil className='h-4 w-4 mr-2' />
            Rename
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onClick={onDelete}
            className='text-red-600 focus:text-red-600'
          >
            <Trash2 className='h-4 w-4 mr-2' />
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
