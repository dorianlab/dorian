"use client";

import { useMemo, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { useHandlerStats, type HandlerStat } from "@/hooks/useObservability";

type SortKey = keyof Pick<
  HandlerStat,
  "count" | "avg_wall_s" | "max_wall_s" | "total_wall_s" | "error_rate"
>;

export default function HandlerTable() {
  const { data, loading } = useHandlerStats();
  const [sortKey, setSortKey] = useState<SortKey>("total_wall_s");
  const [sortAsc, setSortAsc] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const sorted = useMemo(() => {
    if (!data) return [];
    return [...data].sort((a, b) =>
      sortAsc ? a[sortKey] - b[sortKey] : b[sortKey] - a[sortKey],
    );
  }, [data, sortKey, sortAsc]);

  const top10 = useMemo(() => {
    return sorted.slice(0, 10).map((s) => ({
      name: s.fn_name.split(".").pop() ?? s.fn_name,
      avg_ms: Math.round(s.avg_wall_s * 1000),
    }));
  }, [sorted]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortAsc(!sortAsc);
    else {
      setSortKey(key);
      setSortAsc(false);
    }
  }

  const arrow = (key: SortKey) =>
    sortKey === key ? (sortAsc ? " \u25b2" : " \u25bc") : "";

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-4 h-72 animate-pulse" />
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-4">
      <h3 className="text-sm font-semibold text-foreground">
        Handler Performance
      </h3>

      {/* Bar chart — top 10 */}
      {top10.length > 0 && (
        <ResponsiveContainer width="100%" height={180}>
          <BarChart data={top10} layout="vertical">
            <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
            <XAxis
              type="number"
              tick={{ fill: "#6b7280", fontSize: 10 }}
              label={{
                value: "avg ms",
                position: "insideBottomRight",
                fill: "#9ca3af",
                fontSize: 10,
              }}
            />
            <YAxis
              type="category"
              dataKey="name"
              tick={{ fill: "#374151", fontSize: 10 }}
              width={160}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "#ffffff",
                border: "1px solid #e5e7eb",
                borderRadius: "0.375rem",
                fontSize: 11,
              }}
            />
            <Bar dataKey="avg_ms" fill="#f97316" radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      )}

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 px-2 font-medium">Handler</th>
              <th className="text-left py-2 px-2 font-medium">Event</th>
              <th
                className="text-right py-2 px-2 font-medium cursor-pointer select-none hover:text-foreground"
                onClick={() => toggleSort("count")}
              >
                Calls{arrow("count")}
              </th>
              <th
                className="text-right py-2 px-2 font-medium cursor-pointer select-none hover:text-foreground"
                onClick={() => toggleSort("avg_wall_s")}
              >
                Avg (ms){arrow("avg_wall_s")}
              </th>
              <th
                className="text-right py-2 px-2 font-medium cursor-pointer select-none hover:text-foreground"
                onClick={() => toggleSort("max_wall_s")}
              >
                Max (ms){arrow("max_wall_s")}
              </th>
              <th
                className="text-right py-2 px-2 font-medium cursor-pointer select-none hover:text-foreground"
                onClick={() => toggleSort("error_rate")}
              >
                Err %{arrow("error_rate")}
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((s) => {
              const key = `${s.fn_name}::${s.event_type}`;
              const isExpanded = expanded === key;
              return (
                <tr key={key} className="group">
                  <td className="py-1.5 px-2 font-mono text-foreground truncate max-w-[200px]">
                    <button
                      onClick={() =>
                        setExpanded(isExpanded ? null : key)
                      }
                      className="hover:underline text-left"
                      title={s.fn_name}
                    >
                      {s.fn_name.split(".").pop()}
                    </button>
                    {isExpanded && s.last_errors.length > 0 && (
                      <div className="mt-1 space-y-0.5">
                        {s.last_errors.map((msg, i) => (
                          <div
                            key={i}
                            className="text-[10px] text-rose-700 bg-rose-50 rounded px-1.5 py-0.5 break-all"
                          >
                            {msg}
                          </div>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="py-1.5 px-2 text-muted-foreground truncate max-w-[140px]">
                    {s.event_type}
                  </td>
                  <td className="py-1.5 px-2 text-right tabular-nums text-foreground">
                    {s.count}
                  </td>
                  <td className="py-1.5 px-2 text-right tabular-nums text-foreground">
                    {(s.avg_wall_s * 1000).toFixed(0)}
                  </td>
                  <td className="py-1.5 px-2 text-right tabular-nums text-foreground">
                    {(s.max_wall_s * 1000).toFixed(0)}
                  </td>
                  <td
                    className={`py-1.5 px-2 text-right tabular-nums ${
                      s.error_rate > 0.1
                        ? "text-rose-700 font-semibold"
                        : s.error_rate > 0
                          ? "text-amber-700"
                          : "text-muted-foreground"
                    }`}
                  >
                    {(s.error_rate * 100).toFixed(1)}%
                  </td>
                </tr>
              );
            })}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={6} className="py-6 text-center text-muted-foreground">
                  No handler data yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
