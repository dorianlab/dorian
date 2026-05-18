/**
 * A single tooltip entry as delivered by the backend via the `ui/tooltips`
 * WebSocket event.  The shape must match `TOOLTIPS` in dorian/ui/tooltips.py.
 */
export interface TooltipEntry {
  /** Short heading shown at the top of the tooltip. */
  title: string;
  /** Full explanatory text. */
  content: string;
  /**
   * Suggested position in a step-through onboarding tour (1 = first).
   * 0 means "show at any time, not part of the sequential tour".
   */
  step: number;
}

/**
 * Map of target-id → tooltip entry, as received from the backend.
 * Keys match the `data-tooltip-id` attributes on frontend elements.
 */
export type TooltipMap = Record<string, TooltipEntry>;
