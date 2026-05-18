import React from "react";
import SearchPalette from "@/components/shared/SearchPallette";
import SearchBar from "@/components/ui/search-bar";
import CustomEvaluationDialog from "@/components/evaluation/custom";
import { Eval } from "@/types/session";
import { ws } from "@/helpers/ws-events";
import { usePipelineStore } from "@/store/pipeline";
import { useSessionStore } from "@/store/session";
import { useUIStore } from "@/store/ui";

export default function EvaluationProcedure() {
  const [open, setOpen] = React.useState(false);
  const { selectedEval, setSelectedEval } = useUIStore();
  const { evals, currentEvals, addEval } = useSessionStore();

  const handleAddCustom = (o: Eval) => {
    addEval(o);
    setOpen(false);
  };

  const handleSelect = (_eval: Eval) => {
    setSelectedEval(_eval);

    ws.evaluationSelected({
      ..._eval,
      name: _eval.name,
    });
  };

  return (
    <div className='space-y-3'>
      <SearchPalette
        key='evaluation-procedures'
        open={!!open}
        setOpen={setOpen}
        items={evals}
        selectedItems={currentEvals}
        onSelect={handleSelect}
        placeholder='Search Evaluation Procedures...'
        footerAction={<CustomEvaluationDialog onAdd={handleAddCustom} />}
      />

      <div className='flex items-center gap-2'>
        {selectedEval ? (
          <div
            onClick={() => setOpen(true)}
            className='px-3 cursor-pointer py-2 mt-2 bg-card border border-input text-foreground overflow-hidden truncate shadow-sm rounded-md w-full text-sm'
          >
            {typeof selectedEval === "string"
              ? selectedEval
              : selectedEval.name
                ? selectedEval.name
                : "Selected Evaluation Procedure"}
          </div>
        ) : (
          <SearchBar onActivate={() => setOpen(true)} />
        )}
      </div>
    </div>
  );
}
