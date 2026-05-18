"use client";

import ScopeToggle from "@/components/observability/scope-toggle";
import SystemCards from "@/components/observability/system-cards";
import ThroughputChart from "@/components/observability/throughput-chart";
import HandlerTable from "@/components/observability/handler-table";
import PipelineTable from "@/components/observability/pipeline-table";
import ErrorHotspots from "@/components/observability/error-hotspots";
import EventMapGraph from "@/components/observability/event-map";

export default function ObservabilityPage() {
  return (
    <div className="min-h-screen">
      {/* Header */}
      <header className="sticky top-0 z-30 bg-background/90 backdrop-blur border-b border-border px-6 py-3">
        <div className="flex items-center justify-between max-w-screen-2xl mx-auto">
          <div className="flex items-center gap-3">
            <div className="h-6 w-6 rounded bg-gradient-to-br from-orange-500 to-rose-600" />
            <h1 className="text-base font-bold tracking-tight text-foreground">
              Dorian Observability
            </h1>
          </div>
          <ScopeToggle />
        </div>
      </header>

      {/* Body */}
      <main className="max-w-screen-2xl mx-auto px-6 py-6 space-y-6">
        {/* Section 1: System overview */}
        <SystemCards />

        {/* Section 2: Event throughput */}
        <ThroughputChart />

        {/* Section 3 + 4: Handler perf & Pipeline executions (side by side on wide screens) */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
          <HandlerTable />
          <PipelineTable />
        </div>

        {/* Section 5: Error hotspots */}
        <ErrorHotspots />

        {/* Section 6: Event dependency map */}
        <EventMapGraph />
      </main>
    </div>
  );
}
