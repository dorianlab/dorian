"use client";

import { useEffect, useState } from "react";
import { Trophy, Loader2, ChevronDown } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  getDatasetLeaderboard,
  getDatasetMetrics,
  type LeaderboardEntry,
  type MetricInfo,
} from "@/app/api/dataset";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface Props {
  datasetId: string;
  /** Monotonically increasing counter — bumped by live WS events to trigger refetch. */
  liveVersion?: number;
}

export function LeaderboardTable({ datasetId, liveVersion = 0 }: Props) {
  const [metrics, setMetrics] = useState<MetricInfo[]>([]);
  const [selectedMetric, setSelectedMetric] = useState("accuracy");
  const [entries, setEntries] = useState<LeaderboardEntry[]>([]);
  const [loading, setLoading] = useState(true);

  // Load available metrics
  useEffect(() => {
    getDatasetMetrics(datasetId).then((m) => {
      setMetrics(m);
      if (m.length > 0 && !m.some((x) => x.name === selectedMetric)) {
        setSelectedMetric(m[0].name);
      }
    });
  }, [datasetId]);

  // Load leaderboard when metric changes or live update arrives
  useEffect(() => {
    setLoading(true);
    getDatasetLeaderboard(datasetId, selectedMetric)
      .then((lb) => setEntries(lb.entries))
      .catch(() => setEntries([]))
      .finally(() => setLoading(false));
  }, [datasetId, selectedMetric, liveVersion]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Metric selector */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Trophy className="h-4 w-4 text-amber-500" />
          Pipeline Leaderboard
        </div>
        {metrics.length > 0 && (
          <Select value={selectedMetric} onValueChange={setSelectedMetric}>
            <SelectTrigger className="w-48 h-8 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {metrics.map((m) => (
                <SelectItem key={m.name} value={m.name} className="text-xs">
                  {m.name} ({m.count} eval{m.count !== 1 ? "s" : ""})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>

      {entries.length === 0 ? (
        <div className="text-center py-12 text-sm text-muted-foreground">
          No evaluations recorded for this dataset yet.
        </div>
      ) : (
        <div className="rounded-lg border border-border overflow-hidden">
          {/* Header */}
          <div className="grid grid-cols-[3rem_1fr_8rem_6rem_6rem] gap-2 px-4 py-2.5 bg-muted/50 text-xs font-medium text-muted-foreground border-b border-border">
            <span>#</span>
            <span>Pipeline</span>
            <span className="text-right">{selectedMetric}</span>
            <span className="text-right">Source</span>
            <span className="text-right">Task</span>
          </div>

          {/* Rows */}
          {entries.map((e) => (
            <div
              key={e.pipeline_id}
              className="grid grid-cols-[3rem_1fr_8rem_6rem_6rem] gap-2 px-4 py-2.5 text-sm border-b border-border last:border-0 hover:bg-accent/30 transition-colors"
            >
              <span className="font-mono text-xs text-muted-foreground">
                {e.rank <= 3 ? (
                  <span className={e.rank === 1 ? "text-amber-500 font-bold" : e.rank === 2 ? "text-slate-400 font-bold" : "text-amber-700 font-bold"}>
                    {e.rank}
                  </span>
                ) : (
                  e.rank
                )}
              </span>
              <div className="flex flex-wrap items-center gap-1 min-w-0">
                {(e.operators ?? []).slice(0, 4).map((op, i) => (
                  <Badge
                    key={`${op}-${i}`}
                    variant="secondary"
                    className="text-[10px] h-5 max-w-[120px] truncate"
                  >
                    {op.split(".").pop()}
                  </Badge>
                ))}
                {(e.operators ?? []).length > 4 && (
                  <span className="text-[10px] text-muted-foreground">
                    +{e.operators.length - 4}
                  </span>
                )}
              </div>
              <span className="text-right font-mono text-xs">
                {typeof e.metric_value === "number"
                  ? e.metric_value.toFixed(4)
                  : "-"}
              </span>
              <span className="text-right">
                <Badge variant="outline" className="text-[10px] h-5">
                  {e.provenance ?? "user"}
                </Badge>
              </span>
              <span className="text-right text-xs text-muted-foreground truncate">
                {e.task ?? "-"}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
