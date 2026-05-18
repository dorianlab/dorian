"use client";

import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

type ValueMode = "literal" | "ref";

interface Props {
  value: unknown; // string | {ref, attr} | {concat: [...]}
  patternNodeIds: string[];
  onChange: (v: unknown) => void;
}

/**
 * Value-expression field for update_attribute transformations. Schema
 * supports three forms: literal string, {ref, attr} node-attr reference,
 * {concat: [...]} recursive concatenation. This component handles the
 * first two; concat falls back to a read-only JSON view (rare in practice).
 */
export function ValueExpressionField({ value, patternNodeIds, onChange }: Props) {
  const isRef = typeof value === "object" && value !== null && "ref" in (value as object);
  const isConcat = typeof value === "object" && value !== null && "concat" in (value as object);
  const mode: ValueMode | "concat" = isConcat ? "concat" : isRef ? "ref" : "literal";

  if (mode === "concat") {
    return (
      <div className="space-y-1">
        <pre className="font-mono text-[10px] p-2 bg-muted/30 rounded border overflow-auto">
          {JSON.stringify(value, null, 2)}
        </pre>
        <Button size="sm" variant="ghost" className="h-5 text-[10px] px-2"
          onClick={() => onChange("")}>
          Clear &amp; use literal
        </Button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1">
      <Select
        value={mode}
        onValueChange={(m) => {
          if (m === "literal") onChange("");
          else if (m === "ref") onChange({ ref: patternNodeIds[0] ?? "0", attr: "text" });
        }}
      >
        <SelectTrigger className="h-7 w-24 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="literal">literal</SelectItem>
          <SelectItem value="ref">ref</SelectItem>
        </SelectContent>
      </Select>

      {mode === "literal" ? (
        <Input
          className="h-7 text-xs flex-1"
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
          placeholder="literal value"
        />
      ) : (
        <>
          <Select
            value={(value as { ref: string }).ref}
            onValueChange={(ref) => onChange({ ...(value as object), ref })}
          >
            <SelectTrigger className="h-7 text-xs flex-1">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {patternNodeIds.map((id) => (
                <SelectItem key={id} value={id}>node {id}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={(value as { attr: string }).attr}
            onValueChange={(attr) => onChange({ ...(value as object), attr })}
          >
            <SelectTrigger className="h-7 w-20 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="type">type</SelectItem>
              <SelectItem value="text">text</SelectItem>
              <SelectItem value="language">lang</SelectItem>
            </SelectContent>
          </Select>
        </>
      )}
    </div>
  );
}
