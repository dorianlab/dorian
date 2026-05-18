"use client";

import React from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { useUIStore } from "@/store/ui";
import { ws, emitEvent } from "@/helpers/ws-events";
import type { Objective } from "@/types/session";

/**
 * Reconcile-objectives dialog.
 *
 * Opens whenever the rust ``recommendation::handle_pipeline_objectives_switch``
 * publishes ``state/objectives/conflict-prompt`` (the user has custom
 * ranking objectives AND a pipeline just landed).
 *
 * The user composes any subset of ``shared ∪ current_only ∪ suggested_only``
 * and submits — the final selection goes back as a normal
 * ``RankingObjectivesChanged`` emit, so the rust ``session_meta`` handler
 * persists it without a special-case path. The "Use suggested" shortcut
 * fires ``RankingObjectivesAcceptPipelineDefaults`` instead.
 *
 * Default-checked rule (matches rust payload doc):
 *   shared          — ✓ (user already had these AND pipeline suggests them)
 *   current_only    — ✓ (user picked these; preserve unless toggled off)
 *   suggested_only  — ✗ (opt-in: only land if the user ticks them)
 */
export function ObjectivesConflictDialog() {
  const conflict = useUIStore((s) => s.objectivesConflict);
  const setConflict = useUIStore((s) => s.setObjectivesConflict);

  // Reset checkbox state whenever a fresh conflict opens.
  const allItems = React.useMemo<Objective[]>(() => {
    if (!conflict) return [];
    const seen = new Set<string>();
    const out: Objective[] = [];
    for (const o of [
      ...conflict.shared,
      ...conflict.current_only,
      ...conflict.suggested_only,
    ]) {
      if (o?.uuid && !seen.has(o.uuid)) {
        seen.add(o.uuid);
        out.push(o);
      }
    }
    return out;
  }, [conflict]);

  const initialChecked = React.useMemo(() => {
    if (!conflict) return new Set<string>();
    const s = new Set<string>();
    for (const o of conflict.shared) o?.uuid && s.add(o.uuid);
    for (const o of conflict.current_only) o?.uuid && s.add(o.uuid);
    return s;
  }, [conflict]);

  const [checked, setChecked] = React.useState<Set<string>>(new Set());
  React.useEffect(() => {
    setChecked(new Set(initialChecked));
  }, [initialChecked]);

  if (!conflict) return null;

  const toggle = (uuid: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(uuid)) next.delete(uuid);
      else next.add(uuid);
      return next;
    });
  };

  const close = () => setConflict(null);

  const apply = () => {
    const final = allItems.filter((o) => checked.has(o.uuid));
    // RankingObjectivesChanged is the canonical "user picked these"
    // event. Order matches the dialog's render order; rust persists
    // ``rankingObjectives`` and flips ``objectiveMode`` to "custom".
    const payload = final.map((o, idx) => ({
      uuid: o.uuid,
      name: o.name,
      id: o.uuid,
      order: idx,
    }));
    ws.rankingObjectivesChanged({ objectives: payload });
    close();
  };

  const acceptSuggested = () => {
    // Backend shortcut for the common "yes, just use the pipeline
    // defaults" case. Same end result as ticking only the suggested
    // items + applying, but goes through the dedicated handler so the
    // ``objectiveMode = "pipeline_default"`` (not "custom") flag lands
    // — the auto-switch path on subsequent pipeline imports stays
    // active for this user.
    emitEvent("RankingObjectivesAcceptPipelineDefaults", {});
    close();
  };

  const renderRow = (o: Objective, hint?: string) => (
    <div
      key={o.uuid}
      className='flex items-start gap-3 py-2 border-b last:border-b-0'
    >
      <Checkbox
        id={`oconf-${o.uuid}`}
        checked={checked.has(o.uuid)}
        onCheckedChange={() => toggle(o.uuid)}
        className='mt-0.5'
      />
      <div className='flex-1'>
        <Label
          htmlFor={`oconf-${o.uuid}`}
          className='font-medium cursor-pointer'
        >
          {o.name}
        </Label>
        {hint && (
          <p className='text-xs text-muted-foreground mt-0.5'>{hint}</p>
        )}
      </div>
    </div>
  );

  const sharedItems = conflict.shared ?? [];
  const customOnlyItems = conflict.current_only ?? [];
  const suggestedOnlyItems = conflict.suggested_only ?? [];

  return (
    <Dialog open={!!conflict} onOpenChange={(v) => !v && close()}>
      <DialogContent className='max-w-lg'>
        <DialogHeader>
          <DialogTitle>Reconcile ranking objectives</DialogTitle>
          <DialogDescription>
            A pipeline just landed in this session. Its recommended ranking
            objectives differ from your customised list. Pick which ones to
            keep — your selection becomes the new active list.
          </DialogDescription>
        </DialogHeader>

        <div className='max-h-[55vh] overflow-y-auto pr-1 mt-2'>
          {sharedItems.length > 0 && (
            <section className='mb-4'>
              <h4 className='text-xs uppercase font-semibold text-muted-foreground mb-1'>
                In both lists
              </h4>
              {sharedItems.map((o) =>
                renderRow(
                  o,
                  "Already in your list and recommended by the pipeline.",
                ),
              )}
            </section>
          )}
          {customOnlyItems.length > 0 && (
            <section className='mb-4'>
              <h4 className='text-xs uppercase font-semibold text-muted-foreground mb-1'>
                Your custom objectives
              </h4>
              {customOnlyItems.map((o) =>
                renderRow(o, "You picked this earlier."),
              )}
            </section>
          )}
          {suggestedOnlyItems.length > 0 && (
            <section className='mb-1'>
              <h4 className='text-xs uppercase font-semibold text-muted-foreground mb-1'>
                Suggested by pipeline
              </h4>
              {suggestedOnlyItems.map((o) =>
                renderRow(o, "Recommended for sessions with a saved pipeline."),
              )}
            </section>
          )}
        </div>

        <DialogFooter className='flex flex-row justify-between gap-2 pt-3'>
          <div>
            <Button
              variant='ghost'
              size='sm'
              onClick={acceptSuggested}
              type='button'
            >
              Use suggested only
            </Button>
          </div>
          <div className='flex gap-2'>
            <Button variant='outline' size='sm' onClick={close} type='button'>
              Keep current
            </Button>
            <Button size='sm' onClick={apply} type='button'>
              Apply ({checked.size})
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default ObjectivesConflictDialog;
