"use client";

import { useCallback, useMemo } from "react";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import TagsInput from "@/components/ui/tags-input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type {
  CellType,
  ColumnProfile,
  ColumnTableField,
} from "@/types/ui";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

type ColumnTableEditorProps = {
  /** Row keys = column names from the dataset. */
  rows: string[];
  /** Column definitions for the inline editor table. */
  fields: ColumnTableField[];
  /** Per-row column profiles (optional, used for badges + suggestions). */
  profiles?: Record<string, ColumnProfile>;
  /** Current cell values keyed as "row:field_key". */
  values: Record<string, unknown>;
  /** Fires on every cell edit with the composite key + new value. */
  onChange: (key: string, value: unknown) => void;
};

// ---------------------------------------------------------------------------
// Cell renderers
// ---------------------------------------------------------------------------

function CellTagList({
  cellKey,
  value,
  onChange,
  suggestions,
  placeholder,
}: {
  cellKey: string;
  value: string[];
  onChange: (key: string, val: string[]) => void;
  suggestions?: string[];
  placeholder?: string;
}) {
  return (
    <TagsInput
      value={value}
      onChange={(v) => onChange(cellKey, v)}
      suggestions={suggestions}
      placeholder={placeholder ?? "Add values..."}
      className="min-w-[180px]"
    />
  );
}

function CellRange({
  cellKey,
  value,
  onChange,
  placeholder,
}: {
  cellKey: string;
  value: [number | string, number | string] | null;
  onChange: (key: string, val: [number, number] | null) => void;
  placeholder?: string;
}) {
  const min = value?.[0] ?? "";
  const max = value?.[1] ?? "";

  const update = (idx: 0 | 1, raw: string) => {
    const n = raw === "" ? NaN : Number(raw);
    const cur: [number, number] = [
      Number(value?.[0] ?? NaN),
      Number(value?.[1] ?? NaN),
    ];
    cur[idx] = n;
    if (isNaN(cur[0]) && isNaN(cur[1])) {
      onChange(cellKey, null);
    } else {
      onChange(cellKey, cur);
    }
  };

  return (
    <div className="flex items-center gap-1">
      <Input
        type="number"
        value={String(min)}
        onChange={(e) => update(0, e.target.value)}
        placeholder="min"
        className="w-20 h-8 text-xs"
      />
      <span className="text-muted-foreground text-xs">&ndash;</span>
      <Input
        type="number"
        value={String(max)}
        onChange={(e) => update(1, e.target.value)}
        placeholder="max"
        className="w-20 h-8 text-xs"
      />
    </div>
  );
}

function CellTypeSelect({
  cellKey,
  value,
  onChange,
}: {
  cellKey: string;
  value: string;
  onChange: (key: string, val: string) => void;
}) {
  return (
    <Select value={value || ""} onValueChange={(v) => onChange(cellKey, v)}>
      <SelectTrigger className="h-8 w-[120px] text-xs">
        <SelectValue placeholder="Type..." />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="int">int</SelectItem>
        <SelectItem value="float">float</SelectItem>
        <SelectItem value="str">str</SelectItem>
        <SelectItem value="bool">bool</SelectItem>
        <SelectItem value="datetime">datetime</SelectItem>
      </SelectContent>
    </Select>
  );
}

function CellNumber({
  cellKey,
  value,
  onChange,
  placeholder,
}: {
  cellKey: string;
  value: number | string;
  onChange: (key: string, val: number | null) => void;
  placeholder?: string;
}) {
  return (
    <Input
      type="number"
      value={value === null || value === undefined ? "" : String(value)}
      onChange={(e) => {
        const n = e.target.value === "" ? null : Number(e.target.value);
        onChange(cellKey, n);
      }}
      placeholder={placeholder ?? "0"}
      className="w-20 h-8 text-xs"
    />
  );
}

function CellPredicate({
  cellKey,
  value,
  onChange,
  placeholder,
}: {
  cellKey: string;
  value: Record<string, unknown> | null;
  onChange: (key: string, val: Record<string, unknown> | null) => void;
  placeholder?: string;
}) {
  // Simplified predicate: op + value display with inline text editing
  const display = value
    ? `${value.op ?? ""} ${JSON.stringify(value.value ?? "")}`
    : "";

  return (
    <Input
      value={display}
      onChange={(e) => {
        const raw = e.target.value.trim();
        if (!raw) {
          onChange(cellKey, null);
          return;
        }
        // Try to parse "between 18, 100" or "in [0, 1]"
        const betweenMatch = raw.match(/^between\s+([^\s,]+)\s*,\s*(.+)$/i);
        if (betweenMatch) {
          onChange(cellKey, {
            op: "between",
            value: [Number(betweenMatch[1]), Number(betweenMatch[2])],
          });
          return;
        }
        const inMatch = raw.match(/^in\s+\[(.+)\]$/i);
        if (inMatch) {
          const vals = inMatch[1].split(",").map((s) => s.trim());
          onChange(cellKey, { op: "in", value: vals });
          return;
        }
        // Fallback: store as raw text
        onChange(cellKey, { op: "raw", value: raw });
      }}
      placeholder={placeholder ?? "e.g. between 18, 100"}
      className="h-8 text-xs min-w-[160px]"
    />
  );
}

function CellText({
  cellKey,
  value,
  onChange,
  placeholder,
}: {
  cellKey: string;
  value: string;
  onChange: (key: string, val: string) => void;
  placeholder?: string;
}) {
  return (
    <Input
      value={value ?? ""}
      onChange={(e) => onChange(cellKey, e.target.value)}
      placeholder={placeholder}
      className="h-8 text-xs"
    />
  );
}

function CellYesNo({
  cellKey,
  value,
  onChange,
}: {
  cellKey: string;
  value: string;
  onChange: (key: string, val: string) => void;
}) {
  return (
    <Select value={value || ""} onValueChange={(v) => onChange(cellKey, v)}>
      <SelectTrigger className="h-8 w-[80px] text-xs">
        <SelectValue placeholder="-" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="yes">Yes</SelectItem>
        <SelectItem value="no">No</SelectItem>
      </SelectContent>
    </Select>
  );
}

// Map cellType → renderer
function renderCell(
  cellType: CellType,
  cellKey: string,
  value: unknown,
  onChange: (key: string, val: unknown) => void,
  profile?: ColumnProfile,
  placeholder?: string,
) {
  switch (cellType) {
    case "tag-list":
      return (
        <CellTagList
          cellKey={cellKey}
          value={Array.isArray(value) ? value : []}
          onChange={onChange}
          suggestions={profile?.sample_values?.map(String)}
          placeholder={placeholder}
        />
      );
    case "range":
      return (
        <CellRange
          cellKey={cellKey}
          value={Array.isArray(value) ? (value as [number, number]) : null}
          onChange={onChange}
          placeholder={placeholder}
        />
      );
    case "type-select":
      return (
        <CellTypeSelect
          cellKey={cellKey}
          value={typeof value === "string" ? value : ""}
          onChange={onChange}
        />
      );
    case "number":
      return (
        <CellNumber
          cellKey={cellKey}
          value={value as number | string}
          onChange={onChange}
          placeholder={placeholder}
        />
      );
    case "predicate":
      return (
        <CellPredicate
          cellKey={cellKey}
          value={
            value && typeof value === "object" && !Array.isArray(value)
              ? (value as Record<string, unknown>)
              : null
          }
          onChange={onChange}
          placeholder={placeholder}
        />
      );
    case "condition-list":
      // Fallback to text for complex condition lists (can be enhanced later)
      return (
        <CellText
          cellKey={cellKey}
          value={typeof value === "string" ? value : JSON.stringify(value ?? "")}
          onChange={onChange}
          placeholder={placeholder}
        />
      );
    case "yesno":
      return (
        <CellYesNo
          cellKey={cellKey}
          value={typeof value === "string" ? value : ""}
          onChange={onChange}
        />
      );
    case "text":
    default:
      return (
        <CellText
          cellKey={cellKey}
          value={typeof value === "string" ? value : ""}
          onChange={onChange}
          placeholder={placeholder}
        />
      );
  }
}

// ---------------------------------------------------------------------------
// Profile badge (shown next to column name)
// ---------------------------------------------------------------------------

function ProfileBadge({ profile }: { profile?: ColumnProfile }) {
  if (!profile) return null;
  return (
    <div className="flex items-center gap-1">
      <Badge variant="outline" className="text-[10px] px-1 py-0">
        {profile.inferred_type}
      </Badge>
      <Badge variant="outline" className="text-[10px] px-1 py-0">
        {profile.scale}
      </Badge>
      {profile.null_pct > 0 && (
        <Badge variant="destructive" className="text-[10px] px-1 py-0">
          {(profile.null_pct * 100).toFixed(1)}% null
        </Badge>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function ColumnTableEditor({
  rows,
  fields,
  profiles,
  values,
  onChange,
}: ColumnTableEditorProps) {
  const colCount = fields.length;

  // Memoize the grid template to avoid re-computing on every render
  const gridCols = useMemo(
    () => `minmax(140px, 1fr) ${"minmax(120px, 2fr) ".repeat(colCount)}`.trim(),
    [colCount],
  );

  const handleChange = useCallback(
    (key: string, val: unknown) => onChange(key, val),
    [onChange],
  );

  if (!rows.length) {
    return (
      <p className="text-sm text-muted-foreground italic">
        No columns to configure.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded border">
      {/* Header */}
      <div
        className="grid gap-2 border-b bg-muted/50 px-3 py-2 text-xs font-medium text-muted-foreground"
        style={{ gridTemplateColumns: gridCols }}
      >
        <span>Column</span>
        {fields.map((f) => (
          <span key={f.key}>{f.label}</span>
        ))}
      </div>

      {/* Rows */}
      {rows.map((row) => {
        const profile = profiles?.[row];
        return (
          <div
            key={row}
            className="grid items-center gap-2 border-b px-3 py-2 last:border-0"
            style={{ gridTemplateColumns: gridCols }}
          >
            {/* Column name + profile badge */}
            <div className="space-y-0.5">
              <span className="text-sm font-medium">{row}</span>
              <ProfileBadge profile={profile} />
            </div>

            {/* Cells */}
            {fields.map((field) => {
              const cellKey = `${row}:${field.key}`;
              return (
                <div key={cellKey}>
                  {renderCell(
                    field.cellType,
                    cellKey,
                    values[cellKey],
                    handleChange,
                    profile,
                    field.placeholder,
                  )}
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
