"use client";

import { useEffect, useState, useCallback } from "react";
import { useSession } from "next-auth/react";
import { useRouter } from "next/navigation";
import Image from "next/image";
import Logo from "@/app/logo.svg";
import { ArrowLeft, Database } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DarkModeToggle } from "@/components/ui/dark-mode-toggle";
import { DatasetList } from "@/components/datasets/dataset-list";
import { DatasetDetail } from "@/components/datasets/dataset-detail";
import { listAvailableDatasets, getDatasetDetail } from "@/app/api/dataset";
import { useDatasetLive, type DatasetLiveEvent } from "@/hooks/useDatasetLive";
import type { AvailableDataset } from "@/types/dataset";

export default function DatasetsPage() {
  const router = useRouter();
  const { data: session, status } = useSession();
  const [datasets, setDatasets] = useState<AvailableDataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Track a monotonically-increasing version so detail/leaderboard components
  // know when to refetch after a live event.
  const [liveVersion, setLiveVersion] = useState(0);

  useEffect(() => {
    if (status === "unauthenticated") router.push("/login");
  }, [status, router]);

  useEffect(() => {
    if (status !== "authenticated" || !session?.user?.id) return;
    setLoading(true);
    listAvailableDatasets(session.user.id)
      .then(setDatasets)
      .catch(() => setDatasets([]))
      .finally(() => setLoading(false));
  }, [status, session?.user?.id]);

  // ---------------------------------------------------------------------------
  // Live updates via WebSocket (session=__global__)
  // ---------------------------------------------------------------------------
  const handleLiveEvent = useCallback(
    (evt: DatasetLiveEvent) => {
      switch (evt.kind) {
        case "dataset_updated": {
          const did = evt.data.id as string;
          if (!did) return;

          // Refetch the updated dataset document from the API to get full data.
          getDatasetDetail(did)
            .then((full) => {
              setDatasets((prev) => {
                const idx = prev.findIndex((d) => d.id === did);
                const updated: AvailableDataset = {
                  id: full.id,
                  name: full.name,
                  description: full.description,
                  isPublic: full.isPublic,
                  ownerId: full.ownerId,
                  itemCount: full.itemCount,
                  source: full.source,
                  profile: full.profile,
                  features: full.features,
                  targets: full.targets,
                  createdAt: full.createdAt,
                };
                if (idx >= 0) {
                  const next = [...prev];
                  next[idx] = updated;
                  return next;
                }
                return [updated, ...prev];
              });
            })
            .catch((err) => {
              console.error("Failed to refresh dataset on live update", err);
            });

          // Bump version so the detail view refetches if this dataset is selected.
          if (did === selectedId) setLiveVersion((v) => v + 1);
          break;
        }

        case "dataset_removed": {
          const did = evt.data.id as string;
          if (!did) return;
          setDatasets((prev) => prev.filter((d) => d.id !== did));
          if (did === selectedId) setSelectedId(null);
          break;
        }

        case "evaluation_recorded": {
          // Bump version so the leaderboard refetches.
          const did = evt.data.dataset_id as string;
          if (did && did === selectedId) setLiveVersion((v) => v + 1);
          break;
        }
      }
    },
    [selectedId],
  );

  useDatasetLive(handleLiveEvent);

  if (status === "loading" || status === "unauthenticated") return null;

  const selected = datasets.find((d) => d.id === selectedId) ?? null;

  return (
    <div className="flex flex-col h-full bg-background">
      {/* Header */}
      <header className="flex items-center h-14 shrink-0 px-4 gap-3 border-b border-border bg-card">
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={() => (selectedId ? setSelectedId(null) : router.push("/"))}
          title="Back"
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <Image className="h-6 w-auto shrink-0" src={Logo} alt="Dorian" priority />
        <div className="flex items-center gap-2 text-sm font-medium text-foreground">
          <Database className="h-4 w-4 text-muted-foreground" />
          Public Datasets
        </div>
        <div className="ml-auto">
          <DarkModeToggle />
        </div>
      </header>

      {/* Body */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {selected ? (
          <DatasetDetail
            dataset={selected}
            onBack={() => setSelectedId(null)}
            liveVersion={liveVersion}
          />
        ) : (
          <DatasetList
            datasets={datasets}
            loading={loading}
            onSelect={(id) => setSelectedId(id)}
          />
        )}
      </div>
    </div>
  );
}
