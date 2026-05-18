"use client";

import { Fragment, useState } from "react";
import { usePipelineStats, type PipelineStat } from "@/hooks/useObservability";

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    running: "bg-sky-100 text-sky-700 border-sky-300 animate-pulse",
    completed: "bg-emerald-100 text-emerald-700 border-emerald-300",
    failed: "bg-rose-100 text-rose-700 border-rose-300",
    cancelled: "bg-amber-100 text-amber-700 border-amber-300",
  };
  return (
    <span
      className={`inline-block px-2 py-0.5 text-[10px] font-semibold rounded border ${
        styles[status] ?? "bg-muted text-muted-foreground border-border"
      }`}
    >
      {status}
    </span>
  );
}

function StageBadge({ stage }: { stage: string | null }) {
  if (!stage) return null;
  const styles: Record<string, string> = {
    parse: "bg-purple-100 text-purple-700 border-purple-300",
    validation: "bg-orange-100 text-orange-700 border-orange-300",
    expansion: "bg-yellow-100 text-yellow-700 border-yellow-300",
    post_expansion_validation: "bg-yellow-100 text-yellow-700 border-yellow-300",
    graph_build: "bg-red-100 text-red-700 border-red-300",
    execution: "bg-blue-100 text-blue-700 border-blue-300",
    vault_resolution: "bg-pink-100 text-pink-700 border-pink-300",
  };
  return (
    <span
      className={`inline-block px-1.5 py-0.5 text-[9px] font-medium rounded border ${
        styles[stage] ?? "bg-muted text-muted-foreground border-border"
      }`}
    >
      {stage.replace(/_/g, " ")}
    </span>
  );
}

function SourceBadge({ source }: { source: string | null }) {
  if (!source) return null;
  return (
    <span
      className={`inline-block px-1.5 py-0.5 text-[9px] font-medium rounded border ${
        source === "rl"
          ? "bg-indigo-100 text-indigo-700 border-indigo-300"
          : "bg-muted text-muted-foreground border-border"
      }`}
    >
      {source}
    </span>
  );
}

function formatTs(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString();
}

export default function PipelineTable() {
  const { data, loading } = usePipelineStats();
  const [expanded, setExpanded] = useState<string | null>(null);

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-4 h-48 animate-pulse" />
    );
  }

  const rows = data ?? [];
  const failedCount = rows.filter((p) => p.status === "failed").length;
  const rlCount = rows.filter((p) => p.source === "rl").length;

  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-foreground">
          Pipeline Executions
        </h3>
        <div className="flex gap-2 text-[10px]">
          <span className="text-muted-foreground">{rows.length} total</span>
          {failedCount > 0 && (
            <span className="text-rose-700">{failedCount} failed</span>
          )}
          {rlCount > 0 && (
            <span className="text-indigo-700">{rlCount} RL</span>
          )}
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 px-2 font-medium">Run ID</th>
              <th className="text-left py-2 px-2 font-medium">Source</th>
              <th className="text-left py-2 px-2 font-medium">Status</th>
              <th className="text-left py-2 px-2 font-medium">Stage</th>
              <th className="text-right py-2 px-2 font-medium">Duration</th>
              <th className="text-right py-2 px-2 font-medium">Nodes</th>
              <th className="text-left py-2 px-2 font-medium">Started</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p: PipelineStat) => (
              <Fragment key={p.run_id}>
                <tr
                  className="group border-b border-border/50 cursor-pointer hover:bg-muted/50"
                  onClick={() =>
                    setExpanded(expanded === p.run_id ? null : p.run_id)
                  }
                >
                  <td className="py-1.5 px-2 font-mono text-foreground truncate max-w-[120px]">
                    <span title={p.run_id}>{p.run_id.slice(0, 8)}...</span>
                  </td>
                  <td className="py-1.5 px-2">
                    <SourceBadge source={p.source} />
                  </td>
                  <td className="py-1.5 px-2">
                    <StatusBadge status={p.status} />
                  </td>
                  <td className="py-1.5 px-2">
                    <StageBadge stage={p.stage} />
                  </td>
                  <td className="py-1.5 px-2 text-right tabular-nums text-foreground">
                    {p.duration_s != null ? `${p.duration_s.toFixed(1)}s` : "-"}
                  </td>
                  <td className="py-1.5 px-2 text-right tabular-nums text-foreground">
                    {p.node_count}
                  </td>
                  <td className="py-1.5 px-2 text-muted-foreground">
                    {formatTs(p.start_ts)}
                  </td>
                </tr>
                {expanded === p.run_id && (
                  <tr key={`${p.run_id}-detail`} className="border-b border-border/50">
                    <td colSpan={7} className="px-3 py-2">
                      <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[10px]">
                        <span className="text-muted-foreground">Run ID</span>
                        <span className="font-mono text-foreground break-all">{p.run_id}</span>

                        <span className="text-muted-foreground">User</span>
                        <span className="text-foreground">{p.uid ?? "-"}</span>

                        <span className="text-muted-foreground">Session</span>
                        <span className="font-mono text-foreground">{p.session?.slice(0, 12)}...</span>

                        {p.pipeline_id && (
                          <>
                            <span className="text-muted-foreground">Pipeline ID</span>
                            <span className="font-mono text-foreground">{p.pipeline_id}</span>
                          </>
                        )}

                        {p.failed_node && (
                          <>
                            <span className="text-muted-foreground">Failed Node</span>
                            <span className="font-mono text-rose-700">{p.failed_node}</span>
                          </>
                        )}

                        {p.node_types && (
                          <>
                            <span className="text-muted-foreground">Operators</span>
                            <div className="flex flex-wrap gap-1">
                              {p.node_types.split(",").map((t, i) => (
                                <span
                                  key={i}
                                  className="inline-block px-1 py-0.5 bg-muted text-muted-foreground rounded text-[9px] font-mono"
                                >
                                  {t}
                                </span>
                              ))}
                            </div>
                          </>
                        )}

                        {p.error && (
                          <>
                            <span className="text-muted-foreground">Error</span>
                            <div className="text-rose-700 bg-rose-50 rounded px-2 py-1 break-all font-mono whitespace-pre-wrap max-h-32 overflow-y-auto">
                              {p.error}
                            </div>
                          </>
                        )}

                        {p.trace && (
                          <>
                            <span className="text-muted-foreground">Traceback</span>
                            <details className="group/trace">
                              <summary className="text-muted-foreground cursor-pointer hover:text-foreground text-[9px]">
                                Show full traceback
                              </summary>
                              <pre className="mt-1 text-rose-700 bg-rose-50 rounded px-2 py-1 break-all font-mono whitespace-pre-wrap text-[9px] max-h-64 overflow-y-auto">
                                {p.trace}
                              </pre>
                            </details>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="py-6 text-center text-muted-foreground">
                  No pipeline runs yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
