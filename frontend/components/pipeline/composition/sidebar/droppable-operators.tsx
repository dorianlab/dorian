import React, { useRef, useState } from "react";
import { useDnD } from "@/components/pipeline/composition/DndContext";

import { Button } from "@/components/ui/button";
import { Operator } from "@/types/pipeline";
import clsx from "clsx";
import SearchBar from "@/components/ui/search-input";
import ParameterForm from "@/components/pipeline/composition/Forms/CustomParameter";
import SnippetForm from "@/components/pipeline/composition/Forms/CustomSnippet";
import OperatorForm from "@/components/pipeline/composition/Forms/CustomOperator";
import useWebSocketStore from "@/store/web-socket";
import { ws } from "@/helpers/ws-events";
import { Code2, Boxes, Variable } from "lucide-react";
import { Separator } from "@/components/ui/separator";

// ✅ shadcn/ui
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import {
  TooltipProvider,
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from "@/components/ui/tooltip";
import { usePipelineStore } from "@/store/pipeline";
import { useSessionStore } from "@/store/session";

function shrinkDots(name: string): string {
  if (!name) return "";
  const length = 28;
  const short = `${name?.split(".").at(0)}...${name?.split(".").at(-1)}`;
  return short.length > length ? `${short.slice(0, length)}...` : short;
}

export default function OperatorsSidebar() {
  const [modalOpen, setModalOpen] = useState(false);
  const [formType, setFormType] = useState<
    "parameter" | "snippet" | "operator" | null
  >("operator");

  const sidebarRef = useRef<HTMLDivElement>(null);
  const [searchQuery, setSearchQuery] = useState("");

  const { sendMessage } = useWebSocketStore(); // keeping in case used elsewhere
  const { operators, customOperators, addCustomOperator, setDraggingNode } =
    usePipelineStore();
  const { userId, activeSessionId } = useSessionStore();

  const [_, setType] = useDnD();

  const onDragStart = (
    event: React.DragEvent<HTMLButtonElement>,
    nodeType: Operator,
  ) => {
    setDraggingNode(nodeType);
    if (setType) setType(nodeType);

    event.dataTransfer.setData(
      "application/reactflow",
      JSON.stringify(nodeType),
    );
    event.dataTransfer.effectAllowed = "move";
  };

  const handleRecordInteraction = (payload: any) => {
    try {
      if (!userId || !activeSessionId) return;

      if (payload.type === "Parameter") return ws.customParameterAdded(payload);
      if (payload.type === "Snippet") return ws.customSnippetAdded(payload);
      if (payload.type === "Operator") return ws.customOperatorAdded(payload);
    } catch (error) {
      console.error("Error recording interaction:", error);
    }
  };

  const title = formType ? `Add ${formType}` : "Add";

  return (
    <aside
      ref={sidebarRef}
      className={clsx(
        "w-full flex-1 px-3 pe-2 small-scrollbar overflow-y-scroll text-xs ease-in-out transition-transform duration-300",
      )}
    >
      {/* ✅ shadcn Dialog */}
      <Dialog open={modalOpen} onOpenChange={setModalOpen}>
        <DialogContent className='p-0' aria-describedby={undefined}>
          <DialogHeader className='px-6 pt-6'>
            <DialogTitle className='capitalize'>{title}</DialogTitle>
          </DialogHeader>

          <div className=' '>
            {formType === "parameter" && (
              <ParameterForm
                onSubmit={(data: any) => {
                  const payload = {
                    ...data,
                    uuid: data.name.toLowerCase().replace(/\s+/g, "-"),
                    type: "Parameter",
                    dtype: data.type,
                    isNewNode: true,
                  };

                  addCustomOperator(payload);
                  handleRecordInteraction(payload);
                  setModalOpen(false);
                }}
                onCancel={() => setModalOpen(false)}
              />
            )}

            {formType === "snippet" && (
              <SnippetForm
                onSubmit={(data: any) => {
                  const payload = {
                    ...data,
                    uuid: data.name.toLowerCase().replace(/\s+/g, "-"),
                    type: "Snippet",
                    isNewNode: true,
                  };

                  addCustomOperator(payload);
                  handleRecordInteraction(payload);
                  setModalOpen(false);
                }}
                onCancel={() => setModalOpen(false)}
              />
            )}

            {formType === "operator" && (
              <OperatorForm
                onSubmit={(data: any) => {
                  const payload = {
                    ...data,
                    uuid: data.name.toLowerCase().replace(/\s+/g, "-"),
                    type: "Operator",
                    dtype: "str",
                    isNewNode: true,
                  };

                  addCustomOperator(payload);
                  handleRecordInteraction(payload);
                  setModalOpen(false);
                }}
                onCancel={() => setModalOpen(false)}
              />
            )}
          </div>
        </DialogContent>
      </Dialog>

      {operators.length > 0 && (
        <h1 className='text-[14px] font-medium mb-4'>Operators</h1>
      )}

      <SearchBar
        className='mb-2'
        placeholder='Search operators...'
        value={searchQuery}
        onChange={(value) => setSearchQuery(value)}
      />

      <Button
        className='my-1 w-full cursor-pointer text-pretty text-xs text-left'
        variant='outline'
        onClick={() => {
          setFormType("parameter");
          setModalOpen(true);
        }}
      >
        <Variable className='h-4 w-4' strokeWidth={1.3} />
        Custom Parameter
      </Button>

      <Button
        className='my-1 w-full cursor-pointer text-pretty text-xs text-left'
        variant='outline'
        onClick={() => {
          setFormType("snippet");
          setModalOpen(true);
        }}
      >
        <Code2 className='h-4 w-4' strokeWidth={1.3} />
        Custom Snippet
      </Button>

      <Button
        className='my-1 w-full cursor-pointer text-pretty text-xs text-left'
        variant='outline'
        onClick={() => {
          setFormType("operator");
          setModalOpen(true);
        }}
      >
        <Boxes className='h-4 w-4' strokeWidth={1.3} />
        Custom Operator
      </Button>

      <div className='flex my-5 text-gray-500 items-center justify-center gap-2'>
        <Separator className='w-20' />
        OR
        <Separator className='w-20' />
      </div>

      {/* ✅ shadcn Tooltip */}
      <TooltipProvider delayDuration={150}>
        {[...operators, ...customOperators]
          .sort((a, b) => (a.name ?? "").localeCompare(b.name ?? ""))
          .filter((op) =>
            searchQuery
              ? op.name.toLowerCase().includes(searchQuery.toLowerCase())
              : true,
          )
          .map((op) => (
            <Tooltip key={op.uuid}>
              <TooltipTrigger asChild>
                <Button
                  className='my-1 cursor-grab w-full text-pretty text-xs text-left'
                  variant='outline'
                  id={op.uuid}
                  onDragStart={(event) => onDragStart(event, op)}
                  draggable
                >
                  {shrinkDots(op.name)}
                </Button>
              </TooltipTrigger>
              <TooltipContent
                side='right'
                className='max-w-[280px] break-words'
              >
                {op.name}
              </TooltipContent>
            </Tooltip>
          ))}
      </TooltipProvider>
    </aside>
  );
}
