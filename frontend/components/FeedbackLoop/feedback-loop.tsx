"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Check,
  ChevronDown,
  ChevronRight,
  ChevronsUpDown,
  Save,
  X,
} from "lucide-react";

import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";

import { Checkbox } from "@/components/ui/checkbox";
import TagsInput from "@/components/ui/tags-input";
import { Question } from "@/types/ui";
import { cn } from "@/helpers/utils";
import ColumnTableEditor from "./column-table-editor";

export type AnswerValue = string | string[] | Record<string, unknown>;

// ---------------------------------------------------------------------------
// Section metadata
// ---------------------------------------------------------------------------

const SECTION_META: Record<string, { title: string; description?: string }> = {
  _default: {
    title: "General",
  },
  columns: {
    title: "Column Selection",
    description: "Choose which columns to use as features and target.",
  },
  thresholds: {
    title: "Quality Thresholds",
    description: "Configure global quality acceptance thresholds.",
  },
  accuracy: {
    title: "Data Accuracy",
    description:
      "Define allowed values, valid ranges, and outlier detection rules.",
  },
  consistency: {
    title: "Data Consistency",
    description:
      "Set expected types, formats, compliance rules, and precision.",
  },
  effectiveness: {
    title: "Feature & Label Effectiveness",
    description: "Configure balance, diversity, and effectiveness checks.",
  },
  relevance: {
    title: "Relevance & Representativeness",
    description: "Specify size targets, relevant features, and sampling rules.",
  },
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

type FeedbackLoopProps = {
  questions: Question[];
  onSubmit: (answers: Record<string, AnswerValue>) => void;
  /** Called when a single section is saved incrementally. */
  onSaveSection?: (
    sectionKey: string,
    answers: Record<string, AnswerValue>,
  ) => void;
  onCancel: () => void;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function FeedbackLoop({
  questions,
  onSubmit,
  onSaveSection,
  onCancel,
}: FeedbackLoopProps) {
  // ── Answer state ────────────────────────────────────────────────
  const [answers, setAnswers] = useState<Record<string, AnswerValue>>(() => {
    const init: Record<string, AnswerValue> = {};
    for (const q of questions) {
      if ("defaultValue" in q && q.defaultValue != null) {
        init[q.id] = q.defaultValue;
      }
    }
    return init;
  });
  const [openPopovers, setOpenPopovers] = useState<Record<string, boolean>>(
    {},
  );

  // Track which sections have been saved incrementally
  const [savedSections, setSavedSections] = useState<Set<string>>(new Set());

  // Track which sections are collapsed (all start expanded)
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(
    new Set(),
  );

  useEffect(() => {
    const initialAnswers: Record<string, AnswerValue> = {};
    for (const q of questions) {
      if ("initialValue" in q && q.initialValue !== undefined) {
        initialAnswers[q.id] = q.initialValue;
      }
    }
    setAnswers(initialAnswers);
    setSavedSections(new Set());
  }, [questions]);

  const updateAnswer = (id: string, value: AnswerValue) => {
    setAnswers((prev) => ({ ...prev, [id]: value }));
    // Mark section as unsaved when any answer in it changes
    // (We could find the section key here, but it's simpler to
    // just clear savedSections for the section on change below.)
  };

  const toggleMulti = (id: string, opt: string, checked: boolean) => {
    setAnswers((prev) => {
      const arr = (prev[id] as string[]) || [];
      return {
        ...prev,
        [id]: checked ? [...arr, opt] : arr.filter((v) => v !== opt),
      };
    });
  };

  const setPopoverOpen = (id: string, open: boolean) => {
    setOpenPopovers((prev) => ({ ...prev, [id]: open }));
  };

  const toggleSection = (key: string) => {
    setCollapsedSections((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // ── Group questions by section ──────────────────────────────────
  const sections = useMemo(() => {
    const map = new Map<string, Question[]>();
    for (const q of questions) {
      const key = q.section ?? "_default";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(q);
    }
    return map;
  }, [questions]);

  const sectionKeys = useMemo(() => Array.from(sections.keys()), [sections]);

  // ── Incremental save for a single section ───────────────────────
  const handleSaveSection = useCallback(
    (sectionKey: string) => {
      const qs = sections.get(sectionKey);
      if (!qs) return;
      const sectionAnswers: Record<string, AnswerValue> = {};
      for (const q of qs) {
        if (answers[q.id] !== undefined) {
          sectionAnswers[q.id] = answers[q.id];
        }
      }
      onSaveSection?.(sectionKey, sectionAnswers);
      setSavedSections((prev) => new Set(prev).add(sectionKey));
    },
    [sections, answers, onSaveSection],
  );

  // ── Render a single question ────────────────────────────────────
  const renderQuestion = (q: Question) => (
      <div key={q.id} className="space-y-2">
        <Label className="text-sm">{q.question}</Label>

        {q.type === "text" &&
          (q.multiline ? (
            <Textarea
              value={(answers[q.id] as string) ?? ""}
              onChange={(e) => updateAnswer(q.id, e.target.value)}
              placeholder="Type your answer..."
              className="min-h-28"
            />
          ) : (
            <Input
              value={(answers[q.id] as string) ?? ""}
              onChange={(e) => updateAnswer(q.id, e.target.value)}
              placeholder="Type your answer..."
            />
          ))}

        {q.type === "yesno" && (
          <Select
            value={(answers[q.id] as string) ?? ""}
            onValueChange={(val) => updateAnswer(q.id, val)}
          >
            <SelectTrigger className="mt-1">
              <SelectValue placeholder="Select" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="yes">Yes</SelectItem>
              <SelectItem value="no">No</SelectItem>
            </SelectContent>
          </Select>
        )}

        {q.type === "select" && "options" in q && (
          <Popover
            open={openPopovers[q.id] ?? false}
            onOpenChange={(open) => setPopoverOpen(q.id, open)}
          >
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                role="combobox"
                aria-expanded={openPopovers[q.id] ?? false}
                className="mt-1 w-full justify-between font-normal"
              >
                <span className="truncate">
                  {(answers[q.id] as string) || "Select..."}
                </span>
                <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
              </Button>
            </PopoverTrigger>
            <PopoverContent
              className="w-[--radix-popover-trigger-width] p-0"
              align="start"
            >
              <Command>
                <CommandInput placeholder="Search..." />
                <CommandList>
                  <CommandEmpty>No results found.</CommandEmpty>
                  <CommandGroup>
                    {q.options.map((opt) => (
                      <CommandItem
                        key={`${q.id}:${opt}`}
                        value={opt}
                        onSelect={() => {
                          updateAnswer(q.id, opt);
                          setPopoverOpen(q.id, false);
                        }}
                      >
                        <Check
                          className={cn(
                            "mr-2 h-4 w-4",
                            answers[q.id] === opt
                              ? "opacity-100"
                              : "opacity-0",
                          )}
                        />
                        {opt}
                      </CommandItem>
                    ))}
                  </CommandGroup>
                </CommandList>
              </Command>
            </PopoverContent>
          </Popover>
        )}

        {q.type === "multi-select" && "options" in q && (
          <div className="max-h-60 space-y-2 overflow-y-auto pt-1">
            {q.options.map((opt) => {
              const checked = ((answers[q.id] as string[]) || []).includes(opt);
              return (
                <div key={opt} className="flex items-center gap-2">
                  <Checkbox
                    id={`${q.id}-${opt}`}
                    checked={checked}
                    onCheckedChange={(v) =>
                      toggleMulti(q.id, opt, Boolean(v))
                    }
                  />
                  <Label
                    htmlFor={`${q.id}-${opt}`}
                    className="font-normal cursor-pointer"
                  >
                    {opt}
                  </Label>
                </div>
              );
            })}
          </div>
        )}

        {q.type === "tag-list" && (
          <TagsInput
            value={
              Array.isArray(answers[q.id])
                ? (answers[q.id] as string[])
                : []
            }
            onChange={(tags) => updateAnswer(q.id, tags)}
            suggestions={"suggestions" in q ? q.suggestions : undefined}
            placeholder={
              "placeholder" in q ? q.placeholder : "Type and press Enter..."
            }
          />
        )}

        {q.type === "column-table" && "rows" in q && (
          <ColumnTableEditor
            rows={q.rows}
            fields={q.fields}
            profiles={q.profiles}
            values={
              (answers[q.id] as Record<string, unknown>) ??
              q.initialValue ??
              {}
            }
            onChange={(cellKey, cellValue) => {
              const prev =
                (answers[q.id] as Record<string, unknown>) ??
                { ...(q.initialValue ?? {}) };
              updateAnswer(q.id, { ...prev, [cellKey]: cellValue });
            }}
          />
        )}
      </div>
  );

  // ── Main render ─────────────────────────────────────────────────
  const hasSections = sectionKeys.some((k) => k !== "_default" && k !== "general");

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(answers);
      }}
      className="flex h-full min-h-0 flex-1 flex-col"
    >
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xl font-semibold">Feedback Required</h2>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onCancel}
          aria-label="Close"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex-1 min-h-0 space-y-4 overflow-y-auto pb-2">
        {hasSections
          ? sectionKeys.map((sectionKey) => {
              const qs = sections.get(sectionKey)!;
              const meta = SECTION_META[sectionKey];
              const isCollapsed = collapsedSections.has(sectionKey);
              const isSaved = savedSections.has(sectionKey);

              return (
                <div
                  key={sectionKey}
                  className="rounded-lg border bg-card"
                >
                  {/* Section header */}
                  <button
                    type="button"
                    className="flex w-full items-center gap-2 px-4 py-3 text-left hover:bg-muted/50 transition-colors"
                    onClick={() => toggleSection(sectionKey)}
                  >
                    {isCollapsed ? (
                      <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
                    ) : (
                      <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-sm">
                          {meta?.title ?? sectionKey}
                        </span>
                        {isSaved && (
                          <span className="inline-flex items-center gap-1 text-xs text-green-600">
                            <Check className="h-3 w-3" />
                            Saved
                          </span>
                        )}
                      </div>
                      {meta?.description && !isCollapsed && (
                        <p className="text-xs text-muted-foreground mt-0.5">
                          {meta.description}
                        </p>
                      )}
                    </div>
                    <span className="text-xs text-muted-foreground shrink-0">
                      {qs.length} {qs.length === 1 ? "field" : "fields"}
                    </span>
                  </button>

                  {/* Section body */}
                  {!isCollapsed && (
                    <div className="border-t px-4 py-4 space-y-5">
                      {qs.map(renderQuestion)}

                      {/* Per-section save button */}
                      {onSaveSection && (
                        <div className="flex justify-end pt-2">
                          <Button
                            type="button"
                            variant={isSaved ? "outline" : "secondary"}
                            size="sm"
                            onClick={() => handleSaveSection(sectionKey)}
                            className="gap-1.5"
                          >
                            {isSaved ? (
                              <>
                                <Check className="h-3.5 w-3.5" />
                                Saved
                              </>
                            ) : (
                              <>
                                <Save className="h-3.5 w-3.5" />
                                Save section
                              </>
                            )}
                          </Button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })
          : // Flat list fallback (no sections)
            questions.map(renderQuestion)}
      </div>

      <div className="shrink-0 flex items-end justify-end gap-3 border-t pt-4 pb-1">
        <Button variant="outline" type="button" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit">Save all</Button>
      </div>
    </form>
  );
}
