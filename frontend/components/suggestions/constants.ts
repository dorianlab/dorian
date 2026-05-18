export const SEVERITY_COLORS: Record<string, string> = {
  high: "bg-red-500",
  medium: "bg-amber-500",
  low: "bg-green-500",
};

export const SEVERITY_BADGE_STYLES: Record<string, string> = {
  high: "bg-red-500/20 text-red-700",
  medium: "bg-amber-500/20 text-amber-700",
  low: "bg-green-500/20 text-green-700",
};

export const STATUS_LABEL: Record<string, string> = {
  actionable: "Confirmed on data",
  potential: "Potential risk",
};

/** Parse a JSON-encoded string array that arrives from Redis stream fields. */
export function parseJsonArray(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw;
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }
  return [];
}

/** Return the operator FQN as-is for display.
 *  e.g. "sklearn.svm.SVC" → "sklearn.svm.SVC" */
export function operatorDisplayName(fqn: string | undefined): string {
  if (!fqn) return "";
  return fqn;
}
