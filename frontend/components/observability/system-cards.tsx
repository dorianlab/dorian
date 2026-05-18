"use client";

import { useSystemSnapshot } from "@/hooks/useObservability";

function Card({
  label,
  value,
  sub,
  color = "text-foreground",
}: {
  label: string;
  value: string | number;
  sub?: string;
  color?: string;
}) {
  return (
    <div className="bg-card border border-border rounded-lg p-4 flex flex-col gap-1">
      <span className="text-xs text-muted-foreground uppercase tracking-wide">
        {label}
      </span>
      <span className={`text-2xl font-bold tabular-nums ${color}`}>
        {value}
      </span>
      {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
    </div>
  );
}

export default function SystemCards() {
  const { data, loading } = useSystemSnapshot();

  if (loading || !data) {
    return (
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {Array.from({ length: 5 }).map((_, i) => (
          <div
            key={i}
            className="bg-card border border-border rounded-lg p-4 h-24 animate-pulse"
          />
        ))}
      </div>
    );
  }

  const cpuColor =
    data.cpu_percent > 80
      ? "text-rose-600"
      : data.cpu_percent > 50
        ? "text-amber-600"
        : "text-emerald-600";

  // Two-lane bus stats. Fall back to legacy fields if the backend is older.
  const userDepth =
    data.event_bus.user_queue?.size ?? data.event_bus.queue_depth;
  const userCap =
    data.event_bus.user_queue?.capacity ?? data.event_bus.queue_capacity;
  const bgDepth = data.event_bus.bg_queue?.size ?? 0;
  const bgCap = data.event_bus.bg_queue?.capacity ?? 0;

  const bgPct =
    bgCap > 0 ? Math.round((bgDepth / bgCap) * 100) : 0;
  const bgColor =
    bgPct > 80
      ? "text-rose-600"
      : bgPct > 50
        ? "text-amber-600"
        : "text-emerald-600";

  const drops = data.event_bus.drops_by_reason ?? {};
  // Split filter-drops (intended: RL tracing events that never enter the
  // queue) from overflow-drops (the bad kind: producer > consumer).
  const filterDrops = drops.rl_tracing ?? 0;
  const overflowDrops =
    (drops.bg_overflow ?? 0) +
    (drops.bg_overflow_hard ?? 0) +
    (drops.bg_putback_full ?? 0) +
    (drops.user_overflow_to_task ?? 0);

  // RL inflight — the authoritative "running pipelines" number.
  const rlInflight = data.rl?.inflight ?? 0;
  const rlLimit = data.rl?.limit ?? 0;
  const rlPct = rlLimit > 0 ? Math.round((rlInflight / rlLimit) * 100) : 0;
  const rlColor =
    rlPct > 80
      ? "text-rose-600"
      : rlPct > 50
        ? "text-amber-600"
        : "text-emerald-600";

  const diskColor = (pct: number) =>
    pct > 90
      ? "text-rose-600"
      : pct > 75
        ? "text-amber-600"
        : "text-emerald-600";

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Card
          label="CPU"
          value={`${data.cpu_percent}%`}
          color={cpuColor}
        />
        <Card
          label="RSS Memory"
          value={`${data.rss_mb.toFixed(0)} MB`}
          color="text-sky-600"
        />
        <Card
          label="Event Bus (BG)"
          value={bgDepth}
          sub={
            overflowDrops > 0
              ? `${bgPct}% of ${bgCap} · ${overflowDrops} overflow`
              : filterDrops > 0
                ? `${bgPct}% of ${bgCap} · ${filterDrops.toLocaleString()} filtered`
                : `${bgPct}% of ${bgCap} · user:${userDepth}/${userCap}`
          }
          color={overflowDrops > 0 ? "text-rose-600" : bgColor}
        />
        <Card
          label="Workers"
          value={data.event_bus.active_workers}
          sub={`pool size ${data.event_bus.pool_size}`}
          color="text-violet-600"
        />
        <Card
          label="RL Inflight"
          value={rlInflight}
          sub={rlLimit > 0 ? `limit ${rlLimit}` : "—"}
          color={rlColor}
        />
      </div>
      {data.disk && data.disk.length > 0 && (
        <div
          className={`grid gap-3 grid-cols-1 md:grid-cols-${Math.min(data.disk.length, 4)}`}
        >
          {data.disk.map((d) => (
            <Card
              key={d.path}
              label={`Disk ${d.path}`}
              value={d.error ? "?" : `${d.used_pct}%`}
              sub={
                d.error
                  ? d.error
                  : `${d.used_gb.toFixed(0)} / ${d.total_gb.toFixed(0)} GB · ${d.free_gb.toFixed(0)} free`
              }
              color={d.error ? "text-muted-foreground" : diskColor(d.used_pct)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
