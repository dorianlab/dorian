"use client";

import { useErrorSummary, type ErrorEntry } from "@/hooks/useObservability";

export default function ErrorHotspots() {
  const { data, loading } = useErrorSummary();

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-4 h-48 animate-pulse" />
    );
  }

  if (!data || data.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6 text-center text-muted-foreground text-sm">
        No errors recorded
      </div>
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <h3 className="text-sm font-semibold text-foreground mb-3">
        Error Hotspots
      </h3>
      <div className="space-y-2">
        {data.map((entry: ErrorEntry) => {
          const highRate = entry.error_rate > 0.1;
          return (
            <div
              key={entry.fn_name}
              className={`rounded-lg border p-3 ${
                highRate
                  ? "border-rose-300 bg-rose-50"
                  : "border-border bg-card"
              }`}
            >
              {/* Header */}
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-xs font-mono font-semibold text-foreground truncate max-w-[240px]">
                  {entry.fn_name.split(".").pop()}
                </span>
                <div className="flex items-center gap-3 text-xs">
                  <span className="text-muted-foreground">
                    {entry.error_count}/{entry.total_count} calls
                  </span>
                  <span
                    className={`font-bold ${
                      highRate ? "text-rose-700" : "text-amber-700"
                    }`}
                  >
                    {(entry.error_rate * 100).toFixed(1)}%
                  </span>
                </div>
              </div>

              {/* Event types */}
              <div className="flex flex-wrap gap-1 mb-2">
                {entry.event_types.map((ev) => (
                  <span
                    key={ev}
                    className="text-[10px] bg-muted text-muted-foreground px-1.5 py-0.5 rounded"
                  >
                    {ev}
                  </span>
                ))}
              </div>

              {/* Recent errors */}
              {entry.last_errors.length > 0 && (
                <div className="space-y-0.5">
                  {entry.last_errors.map((msg, i) => (
                    <div
                      key={i}
                      className="text-[10px] text-rose-700 bg-rose-50 rounded px-2 py-0.5 break-all font-mono"
                    >
                      {msg}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
