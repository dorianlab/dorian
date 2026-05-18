"use client";

import { X, Plus } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ValueExpressionField } from "./ValueExpressionField";

export type Transformation = {
  type: string;
  [k: string]: unknown;
};

const TYPES = [
  "delete",
  "replace_operator",
  "update_attribute",
  "add_parameter",
  "insert_before",
  "insert_after",
  "add_edges",
] as const;

const DTYPES = ["int", "float", "string", "eval"] as const;
const ATTRS = ["type", "text", "language"] as const;

interface Props {
  t: Transformation;
  patternNodeIds: string[];
  onChange: (next: Transformation) => void;
  onDelete: () => void;
}

/**
 * Dispatches on the transformation's ``type`` discriminator and renders
 * a minimal form for each variant. Matches the JSON schema in
 * ``dorian/mcp/rule_schema.py`` — the server validates anything the UI
 * produces, so the UI stays lenient and fast.
 */
export function TransformationEditor({ t, patternNodeIds, onChange, onDelete }: Props) {
  const setField = (k: string, v: unknown) => onChange({ ...t, [k]: v });

  const changeType = (newType: string) => {
    // Keep overlapping fields; reset the rest. Schema's Pydantic layer
    // rejects extras (extra="forbid"), so we construct clean per-type
    // defaults here to avoid a backend schema error on save.
    const defaults: Record<string, Transformation> = {
      delete: { type: "delete", nodes: [], edges: [], mode: "isolated" },
      replace_operator: { type: "replace_operator", target: patternNodeIds[0] ?? "0", new_name: "" },
      update_attribute: { type: "update_attribute", target: patternNodeIds[0] ?? "0", attribute: "text", value: "" },
      add_parameter: { type: "add_parameter", target: patternNodeIds[0] ?? "0", param_name: "", param_value: "", param_dtype: "eval" },
      insert_before: { type: "insert_before", target: patternNodeIds[0] ?? "0", new_operator: "" },
      insert_after: { type: "insert_after", target: patternNodeIds[0] ?? "0", new_operator: "" },
      add_edges: { type: "add_edges", edges: [] },
    };
    onChange(defaults[newType] ?? { type: newType });
  };

  return (
    <div className="rounded border border-border bg-background/50 p-2 space-y-1.5">
      <div className="flex items-center gap-1">
        <Select value={t.type} onValueChange={changeType}>
          <SelectTrigger className="h-7 w-40 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {TYPES.map((tt) => (
              <SelectItem key={tt} value={tt}>{tt}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="flex-1" />
        <Button
          size="sm"
          variant="ghost"
          className="h-6 w-6 p-0 text-muted-foreground hover:text-destructive"
          onClick={onDelete}
          title="Delete transformation"
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>

      {/* Per-type fields */}
      {t.type === "delete" && <DeleteFields t={t} patternNodeIds={patternNodeIds} setField={setField} />}
      {t.type === "replace_operator" && <ReplaceOperatorFields t={t} patternNodeIds={patternNodeIds} setField={setField} />}
      {t.type === "update_attribute" && <UpdateAttributeFields t={t} patternNodeIds={patternNodeIds} setField={setField} />}
      {t.type === "add_parameter" && <AddParameterFields t={t} patternNodeIds={patternNodeIds} setField={setField} />}
      {(t.type === "insert_before" || t.type === "insert_after") && (
        <InsertFields t={t} patternNodeIds={patternNodeIds} setField={setField} />
      )}
      {t.type === "add_edges" && <AddEdgesFields t={t} setField={setField} />}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Per-type field renderers — keep each tiny; server validates on save
// ─────────────────────────────────────────────────────────────────────────────

type FieldProps = {
  t: Transformation;
  patternNodeIds: string[];
  setField: (k: string, v: unknown) => void;
};
type FieldPropsNoPattern = Omit<FieldProps, "patternNodeIds">;

function NodeRefSelect({ value, patternNodeIds, onChange, label = "target" }: {
  value: string; patternNodeIds: string[]; onChange: (s: string) => void; label?: string;
}) {
  return (
    <div className="flex items-center gap-1">
      <label className="text-[10px] text-muted-foreground w-14 shrink-0">{label}</label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger className="h-6 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {patternNodeIds.map((id) => (
            <SelectItem key={id} value={id}>node {id}</SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function DeleteFields({ t, patternNodeIds, setField }: FieldProps) {
  const nodes = (t.nodes as string[]) ?? [];
  const toggleNode = (id: string) => {
    setField("nodes", nodes.includes(id) ? nodes.filter((n) => n !== id) : [...nodes, id]);
  };
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap gap-1">
        <span className="text-[10px] text-muted-foreground">delete nodes:</span>
        {patternNodeIds.length === 0 ? (
          <span className="text-[10px] italic text-muted-foreground">(no pattern nodes)</span>
        ) : patternNodeIds.map((id) => (
          <Button
            key={id}
            size="sm"
            variant={nodes.includes(id) ? "secondary" : "outline"}
            className="h-5 text-[10px] px-1.5"
            onClick={() => toggleNode(id)}
          >
            {id}
          </Button>
        ))}
      </div>
      <div className="flex items-center gap-1">
        <label className="text-[10px] text-muted-foreground w-14">mode</label>
        <Select value={(t.mode as string) ?? "isolated"} onValueChange={(v) => setField("mode", v)}>
          <SelectTrigger className="h-6 text-xs w-28">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="isolated">isolated</SelectItem>
            <SelectItem value="cascade">cascade</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}

function ReplaceOperatorFields({ t, patternNodeIds, setField }: FieldProps) {
  return (
    <div className="space-y-1">
      <NodeRefSelect
        value={(t.target as string) ?? ""}
        patternNodeIds={patternNodeIds}
        onChange={(v) => setField("target", v)}
      />
      <div className="flex items-center gap-1">
        <label className="text-[10px] text-muted-foreground w-14">new name</label>
        <Input
          className="h-6 text-xs font-mono"
          placeholder="e.g. sklearn.preprocessing.StandardScaler"
          value={(t.new_name as string) ?? ""}
          onChange={(e) => setField("new_name", e.target.value)}
        />
      </div>
    </div>
  );
}

function UpdateAttributeFields({ t, patternNodeIds, setField }: FieldProps) {
  return (
    <div className="space-y-1">
      <NodeRefSelect
        value={(t.target as string) ?? ""}
        patternNodeIds={patternNodeIds}
        onChange={(v) => setField("target", v)}
      />
      <div className="flex items-center gap-1">
        <label className="text-[10px] text-muted-foreground w-14">attribute</label>
        <Select value={(t.attribute as string) ?? "text"} onValueChange={(v) => setField("attribute", v)}>
          <SelectTrigger className="h-6 text-xs w-28">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ATTRS.map((a) => <SelectItem key={a} value={a}>{a}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      <div className="flex items-center gap-1">
        <label className="text-[10px] text-muted-foreground w-14">value</label>
        <div className="flex-1">
          <ValueExpressionField
            value={t.value}
            patternNodeIds={patternNodeIds}
            onChange={(v) => setField("value", v)}
          />
        </div>
      </div>
    </div>
  );
}

function AddParameterFields({ t, patternNodeIds, setField }: FieldProps) {
  return (
    <div className="space-y-1">
      <NodeRefSelect
        value={(t.target as string) ?? ""}
        patternNodeIds={patternNodeIds}
        onChange={(v) => setField("target", v)}
      />
      <div className="flex items-center gap-1">
        <label className="text-[10px] text-muted-foreground w-14">name</label>
        <Input
          className="h-6 text-xs font-mono"
          placeholder="parameter name"
          value={(t.param_name as string) ?? ""}
          onChange={(e) => setField("param_name", e.target.value)}
        />
      </div>
      <div className="flex items-center gap-1">
        <label className="text-[10px] text-muted-foreground w-14">value</label>
        <Input
          className="h-6 text-xs font-mono flex-1"
          placeholder="parameter value"
          value={(t.param_value as string) ?? ""}
          onChange={(e) => setField("param_value", e.target.value)}
        />
        <Select
          value={(t.param_dtype as string) ?? "eval"}
          onValueChange={(v) => setField("param_dtype", v)}
        >
          <SelectTrigger className="h-6 text-xs w-20">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {DTYPES.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}

function InsertFields({ t, patternNodeIds, setField }: FieldProps) {
  return (
    <div className="space-y-1">
      <NodeRefSelect
        value={(t.target as string) ?? ""}
        patternNodeIds={patternNodeIds}
        onChange={(v) => setField("target", v)}
      />
      <div className="flex items-center gap-1">
        <label className="text-[10px] text-muted-foreground w-14">operator</label>
        <Input
          className="h-6 text-xs font-mono"
          placeholder="e.g. sklearn.preprocessing.StandardScaler"
          value={(t.new_operator as string) ?? ""}
          onChange={(e) => setField("new_operator", e.target.value)}
        />
      </div>
    </div>
  );
}

function AddEdgesFields({ t, setField }: FieldPropsNoPattern) {
  const edges = (t.edges as string[][]) ?? [];
  const setEdge = (i: number, idx: 0 | 1, v: string) => {
    const next = edges.map((e) => e.slice()) as string[][];
    next[i][idx] = v;
    setField("edges", next);
  };
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <label className="text-[10px] text-muted-foreground">edges to add</label>
        <Button
          size="sm" variant="ghost" className="h-5 text-[10px] px-1.5"
          onClick={() => setField("edges", [...edges, ["", ""]])}
        >
          <Plus className="h-3 w-3 mr-0.5" /> edge
        </Button>
      </div>
      {edges.length === 0 && (
        <p className="text-[10px] italic text-muted-foreground">no edges</p>
      )}
      {edges.map((e, i) => (
        <div key={i} className="flex items-center gap-1">
          <Input
            className="h-6 text-[11px] font-mono"
            placeholder="source"
            value={e[0] ?? ""}
            onChange={(ev) => setEdge(i, 0, ev.target.value)}
          />
          <span className="text-[10px] text-muted-foreground">→</span>
          <Input
            className="h-6 text-[11px] font-mono"
            placeholder="dest"
            value={e[1] ?? ""}
            onChange={(ev) => setEdge(i, 1, ev.target.value)}
          />
          <Button
            size="sm" variant="ghost"
            className="h-6 w-6 p-0 text-muted-foreground hover:text-destructive"
            onClick={() => setField("edges", edges.filter((_, j) => j !== i))}
          >
            <X className="h-3 w-3" />
          </Button>
        </div>
      ))}
    </div>
  );
}
