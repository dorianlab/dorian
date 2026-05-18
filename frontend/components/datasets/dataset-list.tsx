"use client";

import { useMemo, useState } from "react";
import {
  Database,
  Globe,
  Lock,
  Search,
  Table2,
  Loader2,
} from "lucide-react";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import type { AvailableDataset } from "@/types/dataset";

interface Props {
  datasets: AvailableDataset[];
  loading: boolean;
  onSelect: (id: string) => void;
}

export function DatasetList({ datasets, loading, onSelect }: Props) {
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    if (!search.trim()) return datasets;
    const q = search.toLowerCase();
    return datasets.filter(
      (d) =>
        d.name.toLowerCase().includes(q) ||
        d.description?.toLowerCase().includes(q) ||
        d.source?.type?.toLowerCase().includes(q),
    );
  }, [datasets, search]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto px-6 py-8 space-y-6">
      {/* Title + search */}
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Datasets</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {(() => {
              const publicCount = datasets.filter((d) => d.isPublic).length;
              const privateCount = datasets.length - publicCount;
              if (privateCount === 0) {
                return `${publicCount} public dataset${publicCount !== 1 ? "s" : ""}`;
              }
              return `${publicCount} public + ${privateCount} yours`;
            })()}
          </p>
        </div>
        <div className="relative w-72">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search datasets..."
            className="pl-9"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      {/* Grid */}
      {filtered.length === 0 ? (
        <div className="text-center py-20 text-muted-foreground">
          <Database className="h-12 w-12 mx-auto mb-4 opacity-40" />
          <p>{search ? "No datasets match your search." : "No datasets available yet."}</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filtered.map((d) => (
            <DatasetCard key={d.id} dataset={d} onSelect={onSelect} />
          ))}
        </div>
      )}
    </div>
  );
}

function DatasetCard({
  dataset: d,
  onSelect,
}: {
  dataset: AvailableDataset;
  onSelect: (id: string) => void;
}) {
  const rows = d.itemCount ?? 0;
  const cols = d.features?.length ?? 0;

  return (
    <Card
      className="p-4 hover:bg-accent/50 cursor-pointer transition-colors group"
      onClick={() => onSelect(d.id)}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Table2 className="h-4 w-4 text-muted-foreground shrink-0" />
          <span className="font-medium truncate text-sm">{d.name}</span>
        </div>
        <Badge
          variant="outline"
          className="shrink-0 text-[10px] gap-1"
        >
          {d.isPublic ? (
            <>
              <Globe className="h-3 w-3" /> Public
            </>
          ) : (
            <>
              <Lock className="h-3 w-3" /> Private
            </>
          )}
        </Badge>
      </div>

      {d.description && (
        <p className="text-xs text-muted-foreground mt-2 line-clamp-2">
          {d.description}
        </p>
      )}

      <div className="flex items-center gap-4 mt-3 text-xs text-muted-foreground">
        {rows > 0 && (
          <span>{rows.toLocaleString()} rows</span>
        )}
        {cols > 0 && (
          <span>{cols} feature{cols !== 1 ? "s" : ""}</span>
        )}
        {d.source?.type && (
          <Badge variant="secondary" className="text-[10px] h-5">
            {d.source.type}
          </Badge>
        )}
      </div>

      {/* Profile status indicator */}
      <div className="mt-3">
        {d.profile && Object.keys(d.profile).length > 0 ? (
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-emerald-500" />
            <span className="text-[10px] text-muted-foreground">Profiled</span>
          </div>
        ) : (
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-amber-500" />
            <span className="text-[10px] text-muted-foreground">Pending</span>
          </div>
        )}
      </div>
    </Card>
  );
}
