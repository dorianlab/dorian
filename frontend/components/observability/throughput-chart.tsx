"use client";

import { useMemo } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import { useThroughput, type ThroughputBucket } from "@/hooks/useObservability";

const COLORS = [
  "#f97316", // orange
  "#06b6d4", // cyan
  "#a78bfa", // violet
  "#34d399", // emerald
  "#fb923c", // amber
  "#f472b6", // pink
  "#60a5fa", // blue
  "#fbbf24", // yellow
];

function formatTime(ts: number) {
  const d = new Date(ts * 1000);
  return `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}:${d.getSeconds().toString().padStart(2, "0")}`;
}

export default function ThroughputChart() {
  const { data, loading } = useThroughput();

  // Discover the top event types by volume across all buckets
  const { chartData, eventKeys } = useMemo(() => {
    if (!data || data.length === 0) return { chartData: [], eventKeys: [] };

    // Sum totals per event type
    const totals = new Map<string, number>();
    for (const bucket of data) {
      for (const [k, v] of Object.entries(bucket)) {
        if (k === "ts" || k === "total") continue;
        totals.set(k, (totals.get(k) ?? 0) + (v as number));
      }
    }

    // Top 6 event types; rest grouped as "other"
    const sorted = Array.from(totals.entries()).sort((a, b) => b[1] - a[1]);
    const top = sorted.slice(0, 6).map(([k]) => k);
    const topSet = new Set(top);

    const chartData = data.map((bucket: ThroughputBucket) => {
      const row: Record<string, number | string> = {
        time: formatTime(bucket.ts),
      };
      let other = 0;
      for (const [k, v] of Object.entries(bucket)) {
        if (k === "ts" || k === "total") continue;
        if (topSet.has(k)) {
          row[k] = v as number;
        } else {
          other += v as number;
        }
      }
      if (other > 0) row["other"] = other;
      return row;
    });

    const eventKeys = [...top, ...(sorted.length > 6 ? ["other"] : [])];
    return { chartData, eventKeys };
  }, [data]);

  if (loading) {
    return (
      <div className="bg-card border border-border rounded-lg p-4 h-64 animate-pulse" />
    );
  }

  if (chartData.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-6 text-center text-muted-foreground text-sm">
        No event data yet
      </div>
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <h3 className="text-sm font-semibold text-foreground mb-3">
        Event Throughput
      </h3>
      <ResponsiveContainer width="100%" height={240}>
        <AreaChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis
            dataKey="time"
            tick={{ fill: "#6b7280", fontSize: 10 }}
            interval="preserveStartEnd"
          />
          <YAxis tick={{ fill: "#6b7280", fontSize: 10 }} />
          <Tooltip
            contentStyle={{
              backgroundColor: "#ffffff",
              border: "1px solid #e5e7eb",
              borderRadius: "0.375rem",
              fontSize: 11,
            }}
            labelStyle={{ color: "#374151" }}
          />
          <Legend
            wrapperStyle={{ fontSize: 10 }}
            iconSize={8}
          />
          {eventKeys.map((key, i) => (
            <Area
              key={key}
              type="monotone"
              dataKey={key}
              stackId="1"
              fill={COLORS[i % COLORS.length]}
              stroke={COLORS[i % COLORS.length]}
              fillOpacity={0.5}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
