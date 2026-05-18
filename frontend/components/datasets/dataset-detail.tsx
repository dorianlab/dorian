"use client";

import { useEffect, useState } from "react";
import {
  ArrowLeft,
  BarChart3,
  Database,
  Globe,
  Lock,
  ShieldCheck,
  Table2,
  Loader2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Separator } from "@/components/ui/separator";
import { getDatasetDetail, updateDatasetDescription } from "@/app/api/dataset";
import { LeaderboardTable } from "./leaderboard-table";
import { ProfilePanel } from "./profile-panel";
import { DatasetDescription } from "./dataset-description";
import type { AvailableDataset } from "@/types/dataset";
import { useSessionStore } from "@/store/session";
import { toast } from "sonner";

interface Props {
  dataset: AvailableDataset;
  onBack: () => void;
  /** Monotonically increasing counter — bumped by live WS events to trigger refetch. */
  liveVersion?: number;
}

export function DatasetDetail({ dataset: initial, onBack, liveVersion = 0 }: Props) {
  const [dataset, setDataset] = useState(initial);
  const [loading, setLoading] = useState(true);
  const { userId } = useSessionStore();
  const isOwner = !!userId && dataset.ownerId === userId;

  const handleSaveDescription = async (next: string) => {
    if (!dataset.id || !userId) return;
    try {
      const { description } = await updateDatasetDescription(dataset.id, userId, next);
      setDataset((prev) => ({ ...prev, description: description ?? undefined }));
      toast.success(description ? "Description updated" : "Description cleared");
    } catch {
      toast.error("Could not update description");
    }
  };

  // Fetch full detail (includes profile, features, targets, etc.)
  // Re-runs when liveVersion bumps (live WS event for this dataset).
  useEffect(() => {
    setLoading(true);
    getDatasetDetail(initial.id)
      .then((full) => setDataset((prev) => ({ ...prev, ...full })))
      .catch((err) => {
        console.error("Failed to load dataset detail", err);
        toast.error("Could not load dataset details");
      })
      .finally(() => setLoading(false));
  }, [initial.id, liveVersion]);

  const rows = dataset.itemCount ?? 0;
  const featureCount = dataset.features?.length ?? 0;
  const targetCount = dataset.targets?.length ?? 0;
  const profiled = dataset.profile && Object.keys(dataset.profile).length > 0;

  return (
    <div className="max-w-5xl mx-auto px-6 py-8 space-y-6">
      {/* Breadcrumb */}
      <Button
        variant="ghost"
        size="sm"
        className="gap-1.5 -ml-2 text-muted-foreground hover:text-foreground"
        onClick={onBack}
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        All datasets
      </Button>

      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <Database className="h-5 w-5 text-muted-foreground" />
            <h1 className="text-2xl font-semibold">{dataset.name}</h1>
            <Badge variant="outline" className="gap-1 text-xs">
              {dataset.isPublic ? (
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
        </div>
      </div>

      <DatasetDescription
        text={dataset.description ?? ""}
        editable={isOwner}
        onSave={isOwner ? handleSaveDescription : undefined}
      />

      {/* Stats row */}
      <div className="flex flex-wrap items-center gap-6 text-sm">
        {rows > 0 && (
          <Stat label="Rows" value={rows.toLocaleString()} />
        )}
        {featureCount > 0 && (
          <Stat label="Features" value={String(featureCount)} />
        )}
        {targetCount > 0 && (
          <Stat label="Targets" value={String(targetCount)} />
        )}
        {dataset.source?.type && (
          <Stat label="Source" value={dataset.source.type} />
        )}
        <div className="flex items-center gap-1.5">
          <span
            className={`h-2 w-2 rounded-full ${profiled ? "bg-emerald-500" : "bg-amber-500"}`}
          />
          <span className="text-xs text-muted-foreground">
            {profiled ? "Profiled" : "Pending profiling"}
          </span>
        </div>
      </div>

      <Separator />

      {/* Tabs */}
      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : (
        <Tabs defaultValue="leaderboard" className="space-y-4">
          <TabsList>
            <TabsTrigger value="leaderboard" className="gap-1.5 text-xs">
              <BarChart3 className="h-3.5 w-3.5" />
              Leaderboard
            </TabsTrigger>
            <TabsTrigger value="profile" className="gap-1.5 text-xs">
              <Table2 className="h-3.5 w-3.5" />
              Profile
            </TabsTrigger>
            <TabsTrigger value="quality" className="gap-1.5 text-xs">
              <ShieldCheck className="h-3.5 w-3.5" />
              Data Quality
            </TabsTrigger>
          </TabsList>

          <TabsContent value="leaderboard">
            <LeaderboardTable datasetId={dataset.id} liveVersion={liveVersion} />
          </TabsContent>

          <TabsContent value="profile">
            <ProfilePanel dataset={dataset} />
          </TabsContent>

          <TabsContent value="quality">
            <div className="text-center py-12 text-sm text-muted-foreground">
              Data quality checks will appear here once the dataset has been
              evaluated through the Debugger pipeline.
            </div>
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-lg font-semibold">{value}</span>
      <span className="text-xs text-muted-foreground">{label}</span>
    </div>
  );
}
