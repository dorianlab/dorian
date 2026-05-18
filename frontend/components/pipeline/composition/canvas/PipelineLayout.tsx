"use client";

import clsx from "clsx";
import { useUIStore } from "@/store/ui";

export function PipelineLayout({ className, header, sidebar, children }: any) {
  const { sidebarCollapsed } = useUIStore();

  return (
    <div
      className={clsx(
        "dndflow h-full w-full flex flex-col overflow-hidden",
        className,
      )}
    >
      {/* Header — sticky at top, part of document flow */}
      {header && <div className="shrink-0">{header}</div>}

      {/* Body — sidebar + main content, fills remaining space */}
      <div className="flex flex-1 min-h-0">
        {/* Sidebar — width animates, overflow clips the full-width inner content */}
        <div
          className={clsx(
            "relative flex-shrink-0 h-full overflow-hidden transition-[width] duration-300 ease-in-out",
            sidebarCollapsed ? "w-14" : "w-72",
          )}
        >
          {sidebar}
        </div>

        {/* Main content */}
        <div className="flex-1 min-w-0 h-full overflow-hidden">
          {children}
        </div>
      </div>
    </div>
  );
}
