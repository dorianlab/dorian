"use client";

import * as React from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { PipelineDraft } from "@/types/pipeline";
import { Separator } from "@/components/ui/separator";
import PipelineRenderer from "./pipeline-renderer";
import { Button } from "@/components/ui/button";
import { ws } from "../../helpers/ws-events";

// Column showing a pipeline graph with a "Select this" button in the header.
// The 4 recommendation-feed action buttons (upvote/downvote/view/select) are
// hidden via showActions={false} — they are irrelevant in comparison mode.
function PipelineColumn({
  title,
  pipeline,
  onSelect,
}: {
  title: string;
  pipeline?: PipelineDraft | null;
  onSelect?: () => void;
}) {
  if (!pipeline) {
    return (
      <div className='flex items-center justify-center h-full text-sm text-muted-foreground'>
        No pipeline
      </div>
    );
  }

  return (
    <Card className='h-full'>
      <CardHeader className='pb-2'>
        <div className='flex items-center justify-between min-w-0'>
          <div className='min-w-0'>
            <h3 className='text-sm font-medium'>{title}</h3>
            {pipeline.uuid && (
              <Badge variant='secondary' className='text-xs mt-1'>
                {pipeline.uuid}
              </Badge>
            )}
          </div>
          {onSelect && (
            <Button size='sm' onClick={onSelect} className='ml-2 flex-shrink-0'>
              Select this
            </Button>
          )}
        </div>
      </CardHeader>

      <Separator />

      <CardContent className='p-0'>
        <ScrollArea className='h-[70vh] px-4 py-3'>
          <PipelineRenderer data={pipeline} className='h-[65vh]' showActions={false} />
        </ScrollArea>
      </CardContent>
    </Card>
  );
}

export function PairwiseComparison({
  open,
  onOpenChange,
  leftPipeline,
  rightPipeline,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  leftPipeline?: PipelineDraft | null;
  rightPipeline?: PipelineDraft | null;
}) {
  const handleVote = (selected: PipelineDraft) => {
    onOpenChange(false);
    ws.pipelinePairwiseVoted({ selectedPipeline: selected });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className='w-[70vw]! max-w-[1400px]!'
        aria-describedby={undefined}
      >
        <DialogHeader>
          <DialogTitle>Compare Pipelines</DialogTitle>
        </DialogHeader>

        <div className='grid grid-cols-1 md:grid-cols-2 gap-4'>
          <PipelineColumn
            title='Pipeline A'
            pipeline={leftPipeline}
            onSelect={leftPipeline ? () => handleVote(leftPipeline) : undefined}
          />
          <PipelineColumn
            title='Pipeline B'
            pipeline={rightPipeline}
            onSelect={rightPipeline ? () => handleVote(rightPipeline) : undefined}
          />
        </div>

        <DialogFooter className='mt-3 flex flex-col-reverse gap-2 sm:flex-row sm:justify-between'>
          <Button variant='outline' onClick={() => onOpenChange(false)}>
            Close
          </Button>
          <Button variant='ghost' onClick={() => onOpenChange(false)}>
            No preference
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
