"use client";

/**
 * QueryResolver
 * -------------
 * Surfaces pending session-setup questions emitted by the backend
 * (`state/queries`) — specifically `:task_selection` and `:eval_selection`.
 *
 * Until the user answers these, `attempt_recommendations` returns no
 * suggestions, so the canvas area would otherwise sit blank.  This
 * component renders the questions inline as a select-and-submit panel,
 * emits the matching WS event on answer, and removes the resolved query
 * from the store.  The backend will re-enter `attempt_recommendations`
 * after `DataScienceTaskSelected`/`EvaluationProcedureSelected` and emit
 * `state/pipelines/recommendation`.
 */

import React, { useMemo, useState } from "react";
import { Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useUIStore } from "@/store/ui";
import { ws } from "@/helpers/ws-events";
import type { Question } from "@/types/ui";

type SelectQuestion = Extract<Question, { type: "select" | "multi-select" }>;

function isSetupQuery(q: Question): q is SelectQuestion {
  return (
    (q.id.endsWith(":task_selection") || q.id.endsWith(":eval_selection")) &&
    (q as any).type === "select"
  );
}

export function QueryResolver() {
  const queries = useUIStore((s) => s.queries);
  const removeQueries = useUIStore((s) => s.removeQueries);

  const setupQueries = useMemo(
    () => queries.filter(isSetupQuery),
    [queries],
  );

  const [answers, setAnswers] = useState<Record<string, string>>({});

  if (setupQueries.length === 0) return null;

  const handleSubmit = (q: SelectQuestion) => {
    const value = answers[q.id];
    if (!value) return;
    if (q.id.endsWith(":task_selection")) {
      ws.dataScienceTaskSelected({ name: value });
    } else if (q.id.endsWith(":eval_selection")) {
      ws.evaluationSelected({ name: value });
    }
    removeQueries([q.id]);
    setAnswers((prev) => {
      const next = { ...prev };
      delete next[q.id];
      return next;
    });
  };

  return (
    <div className='flex w-full h-full items-center justify-center p-6'>
      <div className='w-full max-w-md space-y-5 rounded-lg border border-border bg-card p-6 shadow-sm'>
        <div className='flex items-start gap-3'>
          <div className='rounded-full bg-primary/10 p-2 text-primary'>
            <Sparkles className='h-4 w-4' />
          </div>
          <div className='flex-1'>
            <h3 className='text-sm font-semibold'>A few quick choices</h3>
            <p className='text-xs text-muted-foreground'>
              Answer to unlock pipeline recommendations tailored to your dataset.
            </p>
          </div>
        </div>

        <div className='space-y-4'>
          {setupQueries.map((q) => {
            const value = answers[q.id] ?? "";
            return (
              <div key={q.id} className='space-y-2'>
                <label className='text-xs font-medium text-foreground'>
                  {q.question}
                </label>
                <div className='flex items-center gap-2'>
                  <Select
                    value={value}
                    onValueChange={(v) =>
                      setAnswers((prev) => ({ ...prev, [q.id]: v }))
                    }
                  >
                    <SelectTrigger className='flex-1 h-9 text-xs'>
                      <SelectValue placeholder='Choose…' />
                    </SelectTrigger>
                    <SelectContent>
                      {q.options.map((opt) => (
                        <SelectItem key={opt} value={opt} className='text-xs'>
                          {opt}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Button
                    size='sm'
                    disabled={!value}
                    onClick={() => handleSubmit(q)}
                    className='h-9 text-xs'
                  >
                    Save
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export default QueryResolver;
