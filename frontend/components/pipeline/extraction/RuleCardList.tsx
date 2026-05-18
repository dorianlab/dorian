"use client";

import { useState, useCallback, useEffect } from "react";
import {
  DndContext, closestCenter, KeyboardSensor, PointerSensor, useSensor, useSensors,
  DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove, SortableContext, sortableKeyboardCoordinates, verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { Plus, Save } from "lucide-react";
import { Button } from "@/components/ui/button";
import { RuleCard, RuleSpec } from "./RuleCard";

interface Props {
  specs: RuleSpec[];
  /** Per-spec schema errors from the last save attempt, indexed. */
  errorsByIndex?: Record<number, string[]>;
  onChange: (next: RuleSpec[]) => void;
  onSave: () => void;
  saving?: boolean;
  dirty?: boolean;
}

const DEFAULT_NEW_RULE: RuleSpec = {
  description: "New rule",
  pattern: { nodes: { "0": { type: ".*", text: ".*", language: "python" } }, edges: [] },
  transformations: [],
};

/**
 * Sortable list of rule cards. Canonical storage is the JSON-spec array.
 * Reorder via drag, add via button, delete / duplicate from each card.
 * Save emits SaveExtractionRuleSpecs; the backend runs schema
 * validation + backward-compat replay before persisting.
 */
export function RuleCardList({
  specs, errorsByIndex, onChange, onSave, saving, dirty,
}: Props) {
  // Stable ids for dnd-kit. Regenerated only when length changes
  // (external load / add / delete); internal reorder shifts ids via
  // arrayMove so the same id stays attached to the same rule.
  const [ids, setIds] = useState<string[]>(() =>
    specs.map((_, i) => `rule-${i}-${Math.random().toString(36).slice(2, 8)}`),
  );
  useEffect(() => {
    setIds((prev) => {
      if (prev.length === specs.length) return prev;
      return specs.map((_, i) => `rule-${i}-${Math.random().toString(36).slice(2, 8)}`);
    });
  }, [specs.length]);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      if (!over || active.id === over.id) return;
      const oldIndex = ids.indexOf(String(active.id));
      const newIndex = ids.indexOf(String(over.id));
      if (oldIndex < 0 || newIndex < 0) return;
      setIds((prev) => arrayMove(prev, oldIndex, newIndex));
      onChange(arrayMove(specs, oldIndex, newIndex));
    },
    [ids, specs, onChange],
  );

  const handleCardChange = (idx: number) => (next: RuleSpec) => {
    const copy = specs.slice();
    copy[idx] = next;
    onChange(copy);
  };

  const handleDelete = (idx: number) => () => {
    onChange(specs.filter((_, i) => i !== idx));
    setIds((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleDuplicate = (idx: number) => () => {
    const copy = specs.slice();
    copy.splice(idx + 1, 0, structuredClone(specs[idx]));
    onChange(copy);
    setIds((prev) => {
      const n = prev.slice();
      n.splice(idx + 1, 0, `rule-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`);
      return n;
    });
  };

  const handleAdd = () => {
    onChange([...specs, structuredClone(DEFAULT_NEW_RULE)]);
    setIds((prev) => [...prev, `rule-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`]);
  };

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <div className="shrink-0 flex items-center justify-between px-3 py-2 border-b bg-muted/30">
        <div className="text-xs font-medium text-muted-foreground">
          Rules ({specs.length})
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={handleAdd}>
            <Plus className="h-3.5 w-3.5 mr-1" />
            Add rule
          </Button>
          <Button
            size="sm"
            variant={dirty ? "default" : "secondary"}
            className="h-7 text-xs"
            disabled={!dirty || saving}
            onClick={onSave}
          >
            <Save className="h-3.5 w-3.5 mr-1" />
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto p-2 space-y-1.5">
        {specs.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center text-xs text-muted-foreground">
            <p>No rules yet.</p>
            <Button variant="link" size="sm" onClick={handleAdd}>
              Add your first rule
            </Button>
          </div>
        ) : (
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
            <SortableContext items={ids} strategy={verticalListSortingStrategy}>
              {specs.map((spec, idx) => (
                <RuleCard
                  key={ids[idx] ?? idx}
                  id={ids[idx] ?? `rule-${idx}`}
                  index={idx}
                  spec={spec}
                  errors={errorsByIndex?.[idx]}
                  onChange={handleCardChange(idx)}
                  onDelete={handleDelete(idx)}
                  onDuplicate={handleDuplicate(idx)}
                />
              ))}
            </SortableContext>
          </DndContext>
        )}
      </div>
    </div>
  );
}
