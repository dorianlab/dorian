"use client";

import { Plus, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

interface PatternNode {
  type?: string;
  text?: string;
  language?: string;
}

interface PatternEdge {
  source: string;
  destination: string;
}

interface PatternShape {
  nodes?: Record<string, PatternNode>;
  edges?: PatternEdge[];
}

interface Props {
  pattern: PatternShape | undefined;
  onChange: (next: PatternShape) => void;
}

/**
 * Editor for a rule pattern — the LHS matched against the target DAG.
 * Each node has three regex fields (type / text / language). Each edge
 * references two existing node IDs. Minimal UI — the schema enforces
 * regex safety and size limits server-side.
 */
export function PatternEditor({ pattern, onChange }: Props) {
  const nodes = pattern?.nodes ?? {};
  const edges = pattern?.edges ?? [];
  const nodeIds = Object.keys(nodes);

  const setNode = (id: string, field: keyof PatternNode, val: string) => {
    onChange({
      ...pattern,
      nodes: { ...nodes, [id]: { ...nodes[id], [field]: val } },
    });
  };

  const addNode = () => {
    const nextId = String(nodeIds.length);
    onChange({
      ...pattern,
      nodes: { ...nodes, [nextId]: { type: ".*", text: ".*", language: "python" } },
      edges,
    });
  };

  const removeNode = (id: string) => {
    const { [id]: _drop, ...rest } = nodes;
    onChange({
      ...pattern,
      nodes: rest,
      edges: edges.filter((e) => e.source !== id && e.destination !== id),
    });
  };

  const setEdge = (i: number, field: keyof PatternEdge, val: string) => {
    const next = edges.slice();
    next[i] = { ...next[i], [field]: val };
    onChange({ ...pattern, edges: next });
  };

  const addEdge = () => {
    if (nodeIds.length < 2) return;
    onChange({
      ...pattern,
      edges: [...edges, { source: nodeIds[0], destination: nodeIds[1] }],
    });
  };

  const removeEdge = (i: number) => {
    onChange({ ...pattern, edges: edges.filter((_, j) => j !== i) });
  };

  return (
    <div className="space-y-2">
      <div className="space-y-1">
        <div className="flex items-center justify-between">
          <label className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">
            Pattern nodes ({nodeIds.length})
          </label>
          <Button size="sm" variant="ghost" className="h-5 text-[10px] px-1.5" onClick={addNode}>
            <Plus className="h-3 w-3 mr-0.5" />
            Add node
          </Button>
        </div>
        {nodeIds.length === 0 ? (
          <p className="text-[10px] text-muted-foreground italic">No pattern nodes</p>
        ) : (
          <div className="space-y-1">
            {nodeIds.map((id) => {
              const n = nodes[id];
              return (
                <div key={id} className="flex items-center gap-1">
                  <code className="text-[10px] font-mono w-6 shrink-0 text-muted-foreground">
                    {id}
                  </code>
                  <Input
                    className="h-6 text-[11px] font-mono"
                    placeholder="type regex"
                    value={n.type ?? ""}
                    onChange={(e) => setNode(id, "type", e.target.value)}
                  />
                  <Input
                    className="h-6 text-[11px] font-mono"
                    placeholder="text regex"
                    value={n.text ?? ""}
                    onChange={(e) => setNode(id, "text", e.target.value)}
                  />
                  <Input
                    className="h-6 text-[11px] font-mono w-20"
                    placeholder="lang"
                    value={n.language ?? ""}
                    onChange={(e) => setNode(id, "language", e.target.value)}
                  />
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-6 w-6 p-0 shrink-0 text-muted-foreground hover:text-destructive"
                    onClick={() => removeNode(id)}
                    title="Remove node"
                  >
                    <X className="h-3 w-3" />
                  </Button>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="space-y-1">
        <div className="flex items-center justify-between">
          <label className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">
            Pattern edges ({edges.length})
          </label>
          <Button
            size="sm"
            variant="ghost"
            className="h-5 text-[10px] px-1.5"
            onClick={addEdge}
            disabled={nodeIds.length < 2}
          >
            <Plus className="h-3 w-3 mr-0.5" />
            Add edge
          </Button>
        </div>
        {edges.length === 0 ? (
          <p className="text-[10px] text-muted-foreground italic">No pattern edges</p>
        ) : (
          <div className="space-y-1">
            {edges.map((e, i) => (
              <div key={i} className="flex items-center gap-1">
                <Input
                  className="h-6 text-[11px] font-mono"
                  placeholder="source id"
                  value={e.source}
                  onChange={(ev) => setEdge(i, "source", ev.target.value)}
                />
                <span className="text-[10px] text-muted-foreground">→</span>
                <Input
                  className="h-6 text-[11px] font-mono"
                  placeholder="dest id"
                  value={e.destination}
                  onChange={(ev) => setEdge(i, "destination", ev.target.value)}
                />
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 w-6 p-0 shrink-0 text-muted-foreground hover:text-destructive"
                  onClick={() => removeEdge(i)}
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
