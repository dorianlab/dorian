// app/components/pipeline/composition/composition-modal/Index.tsx
import React from "react";
import DroppableSidebar from "@/components/pipeline/composition/sidebar/droppable-operators";
import { cn } from "@/helpers/utils";

/**
 * Composition sidebar — operator/snippet/parameter palette.
 *
 * Pipeline changes are auto-saved (see `usePipelineAutoSave`), so there is
 * no explicit Save button.  The sidebar is purely the drag-source panel.
 */
export default function CompositionSidebar() {
  const containerRef = React.useRef<HTMLDivElement>(null);

  return (
    <div
      className={cn(
        "h-full flex flex-col py-4 gap-0 transition-all duration-1000 ease-in-out w-72 bg-card shadow-xl border-l border-border",
      )}
      ref={containerRef}
    >
      <DroppableSidebar />
    </div>
  );
}
