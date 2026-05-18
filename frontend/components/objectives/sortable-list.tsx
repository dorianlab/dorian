import React from "react";
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import SortableItem from "./sortable-item";
import type { SortableListProps, ObjectiveStatus } from "@/types/ui";

function SortableList({
  items,
  setItems,
  statusMap,
}: SortableListProps & { statusMap?: Map<string, ObjectiveStatus> }) {
  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!active?.id || !over?.id) return;
    if (active.id === over.id) return;

    const oldIndex = items.findIndex((i) => i.uuid === active.id);
    const newIndex = items.findIndex((i) => i.uuid === over.id);
    if (oldIndex === -1 || newIndex === -1) return;

    setItems(arrayMove(items, oldIndex, newIndex));
  }
  const handleDelete = (id: string) => {
    setItems(items.filter((item) => item.uuid !== id));
  };
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragEnd={handleDragEnd}
    >
      <SortableContext
        items={items.map((i) => ({ id: i.uuid }))}
        strategy={verticalListSortingStrategy}
      >
        {items.map((item) => (
          <SortableItem
            key={item.uuid}
            uuid={item.uuid}
            name={item.name}
            status={statusMap?.get(item.name)}
            onDelete={handleDelete}
          />
        ))}
      </SortableContext>
    </DndContext>
  );
}

export default SortableList;
