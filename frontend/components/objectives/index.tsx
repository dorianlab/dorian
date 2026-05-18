import React from "react";
import SearchPalette from "@/components/shared/SearchPallette";
import SearchBar from "@/components/ui/search-bar";
import CustomObjectiveDialog from "@/components/objectives/custom";
import { Objective } from "@/types/session";
import SortableList from "@/components/objectives/sortable-list";
import { ws } from "@/helpers/ws-events";
import { useSessionStore } from "@/store/session";
import { useUIStore } from "@/store/ui";
import type { ObjectiveStatus } from "@/types/ui";

export default function RankingObjectives() {
  const [open, setOpen] = React.useState(false);
  const { objectives, addObjective } = useSessionStore();

  const { selectedObjectives, setSelectedObjectives, objectiveStatus } =
    useUIStore();

  // Build a Map<name, ObjectiveStatus> for efficient lookup by SortableItem
  const statusMap = React.useMemo(() => {
    const m = new Map<string, ObjectiveStatus>();
    for (const s of objectiveStatus) {
      m.set(s.name, s);
    }
    return m;
  }, [objectiveStatus]);

  const handleSelect = (objective: Objective) => {
    // Dedup by uuid (fall back to name for legacy items lacking uuid).
    // Re-clicking the same objective in the search palette must not
    // produce a duplicate entry — the selected list is rendered as a
    // sortable, and ReactFlow-style key collisions corrupt the drag
    // state. The catalog query on the backend already returns unique
    // names; this guard covers the multi-source merge in the SPA.
    const key = (o: Objective) => (o as any).uuid ?? (o as any).id ?? o.name;
    const existing = new Set(selectedObjectives.map(key));
    if (existing.has(key(objective))) return;
    commitObjectives([...selectedObjectives, objective]);
  };

  const handleAddCustom = (o: Objective) => {
    addObjective(o);
    ws.rankingObjectiveAdded({ objective: o });
    setOpen(false);
  };

  const emitRankingChanged = React.useCallback((list: Objective[]) => {
    const payload = list.map((o, index) => ({
      ...o,
      id: (o as any).id ?? (o as any).uuid ?? index.toString(),
      name: (o as any).name ?? (o as any).title ?? `Objective ${index + 1}`,
      order: index,
    }));
    ws.rankingObjectivesChanged({ objectives: payload });
  }, []);

  const commitObjectives = React.useCallback(
    (next: Objective[]) => {
      // Defensive dedup — the only well-formed selected-objectives
      // list has unique uuids (the rust backend's
      // ``RankingObjectivesChanged`` handler doesn't dedup either).
      const seen = new Set<string>();
      const deduped: Objective[] = [];
      for (const o of next) {
        const key = (o as any).uuid ?? (o as any).id ?? o.name;
        if (seen.has(key)) continue;
        seen.add(key);
        deduped.push(o);
      }
      setSelectedObjectives(deduped);
      emitRankingChanged(deduped);
    },
    [setSelectedObjectives, emitRankingChanged],
  );
  return (
    <div className='space-y-3'>
      <SearchPalette
        key='ranking-objectives'
        open={!!open}
        setOpen={setOpen}
        items={objectives}
        selectedItems={selectedObjectives}
        onSelect={handleSelect}
        placeholder='Search objectives...'
        footerAction={<CustomObjectiveDialog onAdd={handleAddCustom} />}
      />

      <div className='flex items-center gap-2'>
        <SearchBar onActivate={() => setOpen(true)} />
      </div>

      <SortableList
        items={selectedObjectives}
        setItems={commitObjectives as any}
        statusMap={statusMap}
      />
    </div>
  );
}
