import React from "react";
import SearchPalette from "@/components/shared/SearchPallette";
import SearchBar from "@/components/ui/search-bar";
import { ws } from "@/helpers/ws-events";
import { useUIStore } from "@/store/ui";
import { Task } from "@/types/pipeline";
import { useSessionStore } from "@/store/session";

export default function TaskSelector() {
  const [open, setOpen] = React.useState(false);
  const { setSelectedTask, selectedTask } = useUIStore();
  const { tasks, addTask } = useSessionStore();
  const handleSelect = (task: Task) => {
    // Update local/state
    addTask(task);
    setSelectedTask(task as any);
    // Normalize payload for WS
    const payload =
      typeof task === "string" ? { name: task } : { id: task.id, ...task };

    // Fire WS event
    ws.dataScienceTaskSelected({
      ...payload,
    });
  };

  return (
    <div className='space-y-3'>
      <SearchPalette
        key='task-selector'
        open={!!open}
        setOpen={setOpen}
        items={tasks}
        selectedItems={[]}
        onSelect={handleSelect}
        placeholder={`Select a task...`}
      />

      <div className='flex flex-col gap-1'>
        {selectedTask ? (
          <>
            <div
              onClick={() => setOpen(true)}
              className='px-3 cursor-pointer py-2 mt-2 bg-card border border-input text-foreground overflow-hidden truncate shadow-sm rounded-md w-full text-sm flex items-center justify-between gap-2'
            >
              <span className='truncate'>
                {typeof selectedTask === "string"
                  ? selectedTask
                  : selectedTask.name
                    ? selectedTask.name
                    : "Selected Task"}
              </span>
              {typeof selectedTask !== "string" && selectedTask.auto ? (
                <span
                  title={selectedTask.reason || "Auto-detected from the dataset profile"}
                  className='shrink-0 text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-primary/10 text-primary border border-primary/20'
                >
                  auto-detected
                </span>
              ) : null}
            </div>
            {typeof selectedTask !== "string" && selectedTask.auto && selectedTask.reason ? (
              <p className='text-[11px] text-muted-foreground px-1'>
                {selectedTask.reason} — click to change.
              </p>
            ) : null}
          </>
        ) : (
          <SearchBar onActivate={() => setOpen(true)} />
        )}
      </div>
    </div>
  );
}
