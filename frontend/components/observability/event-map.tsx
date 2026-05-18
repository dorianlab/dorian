"use client";

import { useMemo, useState } from "react";
import { useEventMap, type EventMap } from "@/hooks/useObservability";

/**
 * Simple SVG-based event -> handler dependency graph.
 * Events are on the left, handlers on the right, edges connect them.
 */
export default function EventMapGraph() {
  const { data, loading } = useEventMap();
  const [hovered, setHovered] = useState<string | null>(null);

  const layout = useMemo(() => {
    if (!data) return null;

    const events = Object.keys(data).sort();
    // Collect unique handlers
    const handlerSet = new Set<string>();
    for (const hList of Object.values(data)) {
      for (const h of hList) handlerSet.add(h);
    }
    const handlers = Array.from(handlerSet).sort();

    const ROW_H = 28;
    const EVENT_X = 20;
    const HANDLER_X = 520;
    const eventPositions = new Map<string, number>();
    const handlerPositions = new Map<string, number>();

    events.forEach((e, i) => eventPositions.set(e, 40 + i * ROW_H));
    handlers.forEach((h, i) => handlerPositions.set(h, 40 + i * ROW_H));

    const height = Math.max(
      events.length * ROW_H + 60,
      handlers.length * ROW_H + 60,
      200,
    );

    const edges: {
      event: string;
      handler: string;
      y1: number;
      y2: number;
    }[] = [];
    for (const [event, hs] of Object.entries(data)) {
      const y1 = eventPositions.get(event) ?? 0;
      for (const h of hs) {
        const y2 = handlerPositions.get(h) ?? 0;
        edges.push({ event, handler: h, y1, y2 });
      }
    }

    return { events, handlers, eventPositions, handlerPositions, edges, height };
  }, [data]);

  if (loading || !layout) {
    return (
      <div className="bg-card border border-border rounded-lg p-4 h-48 animate-pulse" />
    );
  }

  const { events, handlers, eventPositions, handlerPositions, edges, height } =
    layout;

  const isHighlighted = (event: string, handler: string) => {
    if (!hovered) return false;
    return hovered === event || hovered === handler;
  };

  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <h3 className="text-sm font-semibold text-foreground mb-3">
        Event &rarr; Handler Map
      </h3>
      <div className="overflow-x-auto">
        <svg
          width={760}
          height={height}
          className="text-xs"
          style={{ minWidth: 760 }}
        >
          {/* Edges */}
          {edges.map(({ event, handler, y1, y2 }, i) => {
            const highlighted = isHighlighted(event, handler);
            return (
              <path
                key={i}
                d={`M 280 ${y1} C 400 ${y1}, 400 ${y2}, 520 ${y2}`}
                fill="none"
                stroke={highlighted ? "#f97316" : "#d1d5db"}
                strokeWidth={highlighted ? 1.5 : 0.7}
                opacity={hovered && !highlighted ? 0.15 : 1}
                className="transition-all duration-150"
              />
            );
          })}

          {/* Event labels (left) */}
          {events.map((e) => {
            const y = eventPositions.get(e) ?? 0;
            const active = hovered === e;
            return (
              <g key={`ev-${e}`}>
                <rect
                  x={20}
                  y={y - 10}
                  width={250}
                  height={22}
                  rx={4}
                  fill={active ? "#fff7ed" : "#f9fafb"}
                  stroke={active ? "#f97316" : "#e5e7eb"}
                  className="cursor-pointer"
                  onMouseEnter={() => setHovered(e)}
                  onMouseLeave={() => setHovered(null)}
                />
                <text
                  x={30}
                  y={y + 4}
                  fill={active ? "#c2410c" : "#6b7280"}
                  fontSize={11}
                  fontFamily="monospace"
                  className="pointer-events-none select-none"
                >
                  {e}
                </text>
              </g>
            );
          })}

          {/* Handler labels (right) */}
          {handlers.map((h) => {
            const y = handlerPositions.get(h) ?? 0;
            const active = hovered === h;
            return (
              <g key={`h-${h}`}>
                <rect
                  x={520}
                  y={y - 10}
                  width={220}
                  height={22}
                  rx={4}
                  fill={active ? "#ecfeff" : "#f9fafb"}
                  stroke={active ? "#06b6d4" : "#e5e7eb"}
                  className="cursor-pointer"
                  onMouseEnter={() => setHovered(h)}
                  onMouseLeave={() => setHovered(null)}
                />
                <text
                  x={530}
                  y={y + 4}
                  fill={active ? "#0e7490" : "#6b7280"}
                  fontSize={11}
                  fontFamily="monospace"
                  className="pointer-events-none select-none"
                >
                  {h.length > 28 ? h.slice(0, 28) + "\u2026" : h}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
