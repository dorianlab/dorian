"use client";

import { useState, useMemo } from "react";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  GripVertical, ChevronDown, ChevronRight, Copy, Trash2, AlertTriangle, Plus,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { PatternEditor } from "./PatternEditor";
import { TransformationEditor, Transformation } from "./TransformationEditor";

export type RuleSpec = {
  description?: string;
  pattern?: {
    nodes?: Record<string, { type?: string; text?: string; language?: string }>;
    edges?: Array<{ source: string; destination: string }>;
  };
  transformations?: Array<{ type: string; [k: string]: unknown }>;
  [k: string]: unknown;
};

interface Props {
  /** Stable id used by dnd-kit. Position in the list is implicit. */
  id: string;
  index: number;
  spec: RuleSpec;
  /** Schema errors surfaced by the backend on the last save attempt. */
  errors?: string[];
  onChange: (next: RuleSpec) => void;
  onDelete: () => void;
  onDuplicate: () => void;
}

/**
 * One rule-card. Collapsed: short summary (description, transformation
 * kinds, badges). Expanded: editable description input + JSON textarea
 * for the full spec. A per-transformation-type form editor is follow-up
 * work; the textarea is the escape hatch until then.
 */
export function RuleCard({
  id, index, spec, errors, onChange, onDelete, onDuplicate,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [draftJson, setDraftJson] = useState<string | null>(null);
  const [jsonError, setJsonError] = useState<string | null>(null);

  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  const description = spec.description ?? "(untitled rule)";
  const tfKinds = useMemo(
    () => (spec.transformations ?? []).map((t) => t.type),
    [spec.transformations],
  );
  const patternNodeCount = Object.keys(spec.pattern?.nodes ?? {}).length;
  const patternEdgeCount = (spec.pattern?.edges ?? []).length;

  const applyJson = () => {
    if (draftJson === null) return;
    try {
      const next = JSON.parse(draftJson) as RuleSpec;
      onChange(next);
      setJsonError(null);
      setDraftJson(null);
    } catch (exc) {
      setJsonError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`group border border-border rounded-md bg-card ${errors?.length ? "border-red-500/50" : ""}`}
    >
      <div className="flex items-center gap-2 px-2 py-1.5">
        <button
          {...attributes}
          {...listeners}
          className="shrink-0 p-1 text-muted-foreground hover:text-foreground cursor-grab active:cursor-grabbing"
          aria-label="Drag to reorder"
        >
          <GripVertical className="h-4 w-4" />
        </button>
        <button
          className="flex-1 min-w-0 flex items-center gap-1.5 text-left"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          )}
          <span className="text-xs font-mono text-muted-foreground shrink-0">
            {String(index).padStart(2, "0")}
          </span>
          <span className="text-xs font-medium truncate">{description}</span>
        </button>
        <div className="flex items-center gap-1 shrink-0">
          {tfKinds.slice(0, 3).map((k, i) => (
            <Badge key={i} variant="outline" className="text-[9px] px-1 h-4">
              {k}
            </Badge>
          ))}
          {tfKinds.length > 3 && (
            <span className="text-[10px] text-muted-foreground">+{tfKinds.length - 3}</span>
          )}
          <span className="text-[10px] text-muted-foreground ml-1">
            {patternNodeCount}n/{patternEdgeCount}e
          </span>
          {errors?.length ? (
            <AlertTriangle className="h-3.5 w-3.5 text-red-500" aria-label="Invalid" />
          ) : null}
          <Button
            variant="ghost"
            size="sm"
            className="h-6 w-6 p-0 opacity-0 group-hover:opacity-100 transition-opacity"
            onClick={onDuplicate}
            title="Duplicate rule"
          >
            <Copy className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 w-6 p-0 text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100 transition-opacity"
            onClick={onDelete}
            title="Delete rule"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {expanded && (
        <RuleCardBody
          spec={spec}
          errors={errors}
          draftJson={draftJson}
          jsonError={jsonError}
          setDraftJson={setDraftJson}
          setJsonError={setJsonError}
          applyJson={applyJson}
          onChange={onChange}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// RuleCardBody — form editors + JSON fallback; split out so the card wrapper
// stays readable and the body can grow without nesting depth.
// ─────────────────────────────────────────────────────────────────────────────

function RuleCardBody({
  spec, errors, draftJson, jsonError,
  setDraftJson, setJsonError, applyJson, onChange,
}: {
  spec: RuleSpec;
  errors?: string[];
  draftJson: string | null;
  jsonError: string | null;
  setDraftJson: (v: string | null) => void;
  setJsonError: (v: string | null) => void;
  applyJson: () => void;
  onChange: (next: RuleSpec) => void;
}) {
  const [jsonOpen, setJsonOpen] = useState(false);
  const patternNodeIds = Object.keys(spec.pattern?.nodes ?? {});
  const transformations: Transformation[] =
    (spec.transformations as Transformation[]) ?? [];

  const setTransformations = (next: Transformation[]) =>
    onChange({ ...spec, transformations: next as RuleSpec["transformations"] });

  const setTransformation = (i: number, t: Transformation) => {
    const next = transformations.slice();
    next[i] = t;
    setTransformations(next);
  };

  const addTransformation = () => {
    const defaultT = patternNodeIds.length > 0
      ? { type: "delete", nodes: [], edges: [], mode: "isolated" }
      : { type: "add_edges", edges: [] };
    setTransformations([...transformations, defaultT as Transformation]);
  };

  return (
    <div className="px-3 pb-3 pt-1 border-t bg-muted/20 space-y-3">
      <div className="space-y-1">
        <label className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">
          Description
        </label>
        <Input
          className="h-7 text-xs"
          value={spec.description ?? ""}
          onChange={(e) => onChange({ ...spec, description: e.target.value })}
          placeholder="Short description"
        />
      </div>

      {errors && errors.length > 0 && (
        <div className="text-[11px] text-red-600 dark:text-red-400 space-y-0.5">
          {errors.map((err, i) => (
            <div key={i}>· {err}</div>
          ))}
        </div>
      )}

      <PatternEditor
        pattern={spec.pattern}
        onChange={(next) => onChange({ ...spec, pattern: next })}
      />

      <div className="space-y-1">
        <div className="flex items-center justify-between">
          <label className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">
            Transformations ({transformations.length})
          </label>
          <Button size="sm" variant="ghost" className="h-5 text-[10px] px-1.5"
            onClick={addTransformation}>
            <Plus className="h-3 w-3 mr-0.5" /> Add
          </Button>
        </div>
        {transformations.length === 0 ? (
          <p className="text-[10px] text-muted-foreground italic">No transformations</p>
        ) : (
          <div className="space-y-1.5">
            {transformations.map((t, i) => (
              <TransformationEditor
                key={i}
                t={t}
                patternNodeIds={patternNodeIds}
                onChange={(next) => setTransformation(i, next)}
                onDelete={() =>
                  setTransformations(transformations.filter((_, j) => j !== i))
                }
              />
            ))}
          </div>
        )}
      </div>

      {/* Collapsible JSON view — escape hatch when the form can't express
          something (rare: deeply-nested concat values, future schema
          additions). Server validates either way. */}
      <div className="space-y-1">
        <button
          className="flex items-center gap-1 text-[10px] font-medium text-muted-foreground uppercase tracking-wide hover:text-foreground"
          onClick={() => setJsonOpen((v) => !v)}
        >
          {jsonOpen ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
          Raw JSON (advanced)
        </button>
        {jsonOpen && (
          <div className="space-y-1">
            <div className="flex items-center justify-end gap-1">
              {draftJson !== null && (
                <>
                  <Button size="sm" variant="ghost" className="h-5 text-[10px] px-2"
                    onClick={() => { setDraftJson(null); setJsonError(null); }}>
                    Cancel
                  </Button>
                  <Button size="sm" variant="secondary" className="h-5 text-[10px] px-2" onClick={applyJson}>
                    Apply
                  </Button>
                </>
              )}
            </div>
            <Textarea
              className="font-mono text-[11px] min-h-[140px]"
              value={draftJson ?? JSON.stringify(spec, null, 2)}
              onChange={(e) => setDraftJson(e.target.value)}
              onFocus={() => draftJson === null && setDraftJson(JSON.stringify(spec, null, 2))}
              spellCheck={false}
            />
            {jsonError && (
              <p className="text-[10px] text-red-600 dark:text-red-400">{jsonError}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
