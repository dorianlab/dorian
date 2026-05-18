"use client";

import React from "react";
import { ChevronDownIcon, ChevronUpIcon } from "lucide-react";

// ---------------------------------------------------------------------------
// Category section header (collapsible)
// ---------------------------------------------------------------------------

interface CategoryHeaderProps {
  label: string;
  count: number;
  completedCount: number;
  errorCount: number;
  collapsed: boolean;
  onToggle: () => void;
}

function CategoryHeaderInner({
  label,
  count,
  completedCount,
  errorCount,
  collapsed,
  onToggle,
}: CategoryHeaderProps) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className="flex w-full items-center gap-2 px-3 py-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider hover:bg-muted/40 rounded"
    >
      {collapsed ? (
        <ChevronDownIcon className="h-3 w-3" />
      ) : (
        <ChevronUpIcon className="h-3 w-3" />
      )}
      <span className="flex-1 text-left">{label}</span>
      <span className="text-[10px] font-normal normal-case tracking-normal">
        {completedCount}/{count}
        {errorCount > 0 && (
          <span className="text-red-500 ml-1">{errorCount} err</span>
        )}
      </span>
    </button>
  );
}

export const CategoryHeader = React.memo(CategoryHeaderInner);
