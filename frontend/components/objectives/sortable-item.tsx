import React from "react";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { SortableItemProps, ObjectiveStatus } from "@/types/ui";
import { GripVertical, X, AlertTriangle } from "lucide-react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/** Human-readable labels for objective dependency fields. */
const DEPENDENCY_LABELS: Record<string, string> = {
  dataset_profile: "Dataset profile",
  current_pipeline: "A pipeline on the canvas",
  task: "Data science task selection",
};

function SortableItem({
  uuid,
  name,
  status,
  onDelete,
}: SortableItemProps & {
  status?: ObjectiveStatus;
  onDelete?: (id: string) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition } =
    useSortable({ id: uuid });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  const isDegraded = status?.status === "degraded";
  const missingLabels = (status?.missing ?? []).map(
    (dep) => DEPENDENCY_LABELS[dep] ?? dep,
  );

  return (
    <TooltipProvider>
      <div
        ref={setNodeRef}
        style={style}
        className={`flex items-center justify-between gap-2 mb-2 border rounded-md px-3 py-2 ${
          isDegraded ? "bg-muted/50 border-amber-500/30" : "bg-background"
        }`}
      >
        <div className='flex items-center gap-2 flex-1 overflow-hidden'>
          <GripVertical
            {...listeners}
            {...attributes}
            className='h-4 w-4 text-muted-foreground cursor-grab active:cursor-grabbing flex-shrink-0'
          />

          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className={`truncate text-sm cursor-default ${
                  isDegraded ? "text-muted-foreground" : ""
                }`}
              >
                {name}
              </span>
            </TooltipTrigger>
            <TooltipContent>
              <p>{name}</p>
              {isDegraded && (
                <p className='text-amber-400 text-xs mt-1'>
                  Needs: {missingLabels.join(", ")}
                </p>
              )}
            </TooltipContent>
          </Tooltip>

          {isDegraded && (
            <Tooltip>
              <TooltipTrigger asChild>
                <AlertTriangle className='h-3.5 w-3.5 text-amber-500 flex-shrink-0' />
              </TooltipTrigger>
              <TooltipContent>
                <p className='text-xs'>
                  Degraded — needs: {missingLabels.join(", ")}
                </p>
              </TooltipContent>
            </Tooltip>
          )}
        </div>

        <button
          type='button'
          onClick={() => onDelete?.(uuid)}
          className='text-muted-foreground hover:text-red-500 transition-colors'
          aria-label='Delete'
          title='Delete'
          onPointerDown={(e) => e.stopPropagation()}
        >
          <X className='h-4 w-4' />
        </button>
      </div>
    </TooltipProvider>
  );
}

export default SortableItem;
