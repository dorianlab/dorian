// ---------------------------------------------------------------------------
// Value formatting helpers and quality check status rendering
// ---------------------------------------------------------------------------

import {
  CheckCircle2,
  XCircle,
  Clock3,
  CircleAlert,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Value formatting
// ---------------------------------------------------------------------------

export function formatValue(value: unknown): string {
  if (value === undefined || value === null || value === "None") return "";
  if (typeof value === "object" && value !== null) {
    if (Array.isArray(value)) return `[${value.length} items]`;
    return `{${Object.keys(value as Record<string, unknown>).length} entries}`;
  }
  if (typeof value === "number") {
    if (Number.isInteger(value)) return String(value);
    return Number(value).toPrecision(4);
  }
  const s = String(value);
  try {
    const parsed = JSON.parse(s);
    if (typeof parsed === "number") {
      return Number.isInteger(parsed)
        ? String(parsed)
        : Number(parsed).toPrecision(4);
    }
    if (typeof parsed === "object" && parsed !== null) {
      if (Array.isArray(parsed)) return `[${parsed.length} items]`;
      const keys = Object.keys(parsed);
      return `{${keys.length} entries}`;
    }
    return String(parsed);
  } catch {
    return s.length > 30 ? s.slice(0, 27) + "..." : s;
  }
}

export function renderExpandedValue(value: unknown) {
  if (value === undefined || value === null || value === "None") return null;

  if (typeof value === "object" && value !== null) {
    if (Array.isArray(value)) {
      return (
        <div className="space-y-1">
          {value.length === 0 ? (
            <p className="text-xs text-muted-foreground font-mono">[]</p>
          ) : (
            (value as unknown[]).map((item, index) => (
              <div
                key={index}
                className="rounded bg-muted/40 px-2 py-1 text-xs text-muted-foreground font-mono break-all"
              >
                {formatValue(item)}
              </div>
            ))
          )}
        </div>
      );
    }

    const entries = Object.entries(value as Record<string, unknown>);
    return (
      <div className="space-y-1">
        {entries.length === 0 ? (
          <p className="text-xs text-muted-foreground font-mono">{"{}"}</p>
        ) : (
          entries.map(([key, entryValue]) => (
            <div
              key={key}
              className="flex items-start justify-between gap-3 rounded bg-muted/40 px-2 py-1"
            >
              <span className="text-xs font-medium break-words">{key}</span>
              <span className="text-xs text-muted-foreground font-mono break-all text-right">
                {formatValue(entryValue)}
              </span>
            </div>
          ))
        )}
      </div>
    );
  }

  return (
    <p className="text-xs text-muted-foreground font-mono leading-relaxed break-all">
      {String(value)}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Quality check status helpers
// ---------------------------------------------------------------------------

export function qualityStatusIcon(status: string) {
  switch (status) {
    case "passed":
      return <CheckCircle2 className="h-3 w-3 text-emerald-600 flex-shrink-0" />;
    case "failed":
      return <XCircle className="h-3 w-3 text-rose-600 flex-shrink-0" />;
    case "pending":
      return <Clock3 className="h-3 w-3 text-amber-600 flex-shrink-0" />;
    default:
      return <CircleAlert className="h-3 w-3 text-slate-600 flex-shrink-0" />;
  }
}

export function qualityStatusClass(status: string): string {
  switch (status) {
    case "passed":
      return "text-green-700 bg-green-100 dark:text-green-300 dark:bg-green-900/30";
    case "failed":
      return "text-red-700 bg-red-100 dark:text-red-300 dark:bg-red-900/30";
    case "pending":
      return "text-amber-700 bg-amber-100 dark:text-amber-300 dark:bg-amber-900/30";
    default:
      return "text-muted-foreground bg-muted";
  }
}
