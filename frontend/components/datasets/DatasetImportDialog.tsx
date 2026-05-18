"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Database,
  Globe,
  Loader2,
  Search,
  Table2,
  User,
  CheckCircle2,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { isRateLimitError } from "@/lib/api-client";

import {
  listAvailableDatasets,
  importExistingDataset,
} from "@/app/api/dataset";
import { useSessionStore } from "@/store/session";
import { useDatasetStore } from "@/store/dataset";
import type { AvailableDataset } from "@/types/dataset";

interface Props {
  open: boolean;
  setOpen: (v: boolean) => void;
}

export default function DatasetImportDialog({ open, setOpen }: Props) {
  const { activeSessionId, userId } = useSessionStore();
  const { addDatasets } = useDatasetStore();

  const [datasets, setDatasets] = useState<AvailableDataset[]>([]);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<"mine" | "public">("mine");

  // Fetch datasets when dialog opens
  useEffect(() => {
    if (!open || !userId) return;
    let cancelled = false;

    setLoading(true);
    listAvailableDatasets(userId)
      .then((res) => {
        if (!cancelled) setDatasets(res);
      })
      .catch((err) => {
        if (isRateLimitError(err)) return;
        console.error("Failed to fetch datasets", err);
        toast.error("Could not load available datasets");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open, userId]);

  // Filter & partition
  const filtered = useMemo(() => {
    const q = query.toLowerCase().trim();
    return datasets.filter(
      (d) =>
        !q ||
        d.name?.toLowerCase().includes(q) ||
        d.description?.toLowerCase().includes(q) ||
        d.source?.type?.toLowerCase().includes(q),
    );
  }, [datasets, query]);

  const mine = useMemo(
    () => filtered.filter((d) => d.ownerId === userId),
    [filtered, userId],
  );
  const pub = useMemo(
    () => filtered.filter((d) => d.isPublic),
    [filtered],
  );

  const handleImport = useCallback(
    async (ds: AvailableDataset) => {
      if (!activeSessionId || !userId) return;
      setImporting(ds.id);
      try {
        const result = await importExistingDataset(
          ds.id,
          activeSessionId,
          userId,
        );
        addDatasets([
          {
            uuid: result.did,
            filename: result.name || ds.name,
            size: 0,
            hasLabels: !!(ds.targets && ds.targets.length > 0),
            did: result.did,
            isPublic: ds.isPublic,
          },
        ]);
        toast.success(`Imported "${ds.name}"`);
        setOpen(false);
      } catch (err: any) {
        if (isRateLimitError(err)) return;
        const msg =
          err?.response?.data?.detail ?? err?.message ?? "Import failed";
        toast.error(msg);
      } finally {
        setImporting(null);
      }
    },
    [activeSessionId, userId, addDatasets, setOpen],
  );

  const visibleList = tab === "mine" ? mine : pub;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        className="max-w-lg max-h-[80vh] flex flex-col"
        aria-describedby={undefined}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Database className="h-5 w-5" />
            Import Dataset
          </DialogTitle>
        </DialogHeader>

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search datasets..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="pl-9"
          />
        </div>

        {/* Tabs */}
        <Tabs
          value={tab}
          onValueChange={(v) => setTab(v as "mine" | "public")}
          className="flex-1 min-h-0 flex flex-col"
        >
          <TabsList className="w-full">
            <TabsTrigger value="mine" className="flex-1 gap-1.5">
              <User className="h-3.5 w-3.5" />
              My Datasets
              {!loading && (
                <Badge variant="secondary" className="ml-1 px-1.5 py-0 text-[10px]">
                  {mine.length}
                </Badge>
              )}
            </TabsTrigger>
            <TabsTrigger value="public" className="flex-1 gap-1.5">
              <Globe className="h-3.5 w-3.5" />
              Public
              {!loading && (
                <Badge variant="secondary" className="ml-1 px-1.5 py-0 text-[10px]">
                  {pub.length}
                </Badge>
              )}
            </TabsTrigger>
          </TabsList>

          <TabsContent value={tab} className="flex-1 overflow-y-auto mt-2 space-y-1.5 small-scrollbar">
            {loading ? (
              <div className="flex items-center justify-center py-12 text-muted-foreground text-sm gap-2">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading datasets...
              </div>
            ) : visibleList.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 text-muted-foreground text-sm gap-1">
                <Database className="h-8 w-8 opacity-40" />
                <p>No datasets found</p>
                {query && (
                  <p className="text-xs">
                    Try a different search term
                  </p>
                )}
              </div>
            ) : (
              visibleList.map((ds) => (
                <DatasetCard
                  key={ds.id}
                  dataset={ds}
                  userId={userId}
                  importing={importing === ds.id}
                  onImport={() => handleImport(ds)}
                />
              ))
            )}
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Dataset Card
// ---------------------------------------------------------------------------

function DatasetCard({
  dataset,
  userId,
  importing,
  onImport,
}: {
  dataset: AvailableDataset;
  userId: string;
  importing: boolean;
  onImport: () => void;
}) {
  const isOwner = dataset.ownerId === userId;
  const sourceType = dataset.source?.type ?? "unknown";
  const hasProfile = !!(dataset.profile && Object.keys(dataset.profile).length > 0);

  return (
    <div
      role="button"
      tabIndex={0}
      aria-disabled={importing || undefined}
      onClick={() => !importing && onImport()}
      onKeyDown={(e) => { if (!importing && (e.key === "Enter" || e.key === " ")) onImport(); }}
      className="w-full text-left rounded-md border bg-card px-3 py-2.5 hover:bg-accent/50 transition-colors aria-disabled:opacity-60 cursor-pointer group"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <Table2 className="h-4 w-4 text-muted-foreground flex-shrink-0 mt-0.5" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium truncate">{dataset.name}</p>
            <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
              {dataset.itemCount != null && (
                <span className="text-[11px] text-muted-foreground">
                  {dataset.itemCount.toLocaleString()} rows
                </span>
              )}
              {hasProfile && (
                <span className="inline-flex items-center gap-0.5 text-[11px] text-green-600 dark:text-green-400">
                  <CheckCircle2 className="h-3 w-3" /> profiled
                </span>
              )}
              <Badge
                variant="outline"
                className="text-[10px] px-1.5 py-0 h-4"
              >
                {sourceType === "user-upload" ? (isOwner ? "mine" : "user") : sourceType}
              </Badge>
              {dataset.isPublic && (
                <Badge
                  variant="secondary"
                  className="text-[10px] px-1.5 py-0 h-4"
                >
                  public
                </Badge>
              )}
            </div>
          </div>
        </div>

        <div className="flex-shrink-0">
          {importing ? (
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs opacity-0 group-hover:opacity-100 transition-opacity"
              tabIndex={-1}
            >
              Import
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
