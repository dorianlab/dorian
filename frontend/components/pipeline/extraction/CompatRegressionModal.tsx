"use client";

import { useExtractionStore } from "@/store/extraction";
import type { CompatRegression } from "@/store/extraction";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AlertTriangle } from "lucide-react";

interface Props {
  /** Called when user chooses to override and retry the save. Implementer
   *  should re-emit the save event with ``skipCompatCheck: true``. */
  onOverride: () => void;
  /** Called when user chooses to edit rules further. Closes the modal
   *  and leaves the user on the rules pane. */
  onEdit: () => void;
}

/**
 * Displays the list of past extractions that would regress under the
 * candidate rules list. Modal is driven by the store — the hook that
 * receives ``extraction/rules-compat-regressions`` pushes a
 * ``CompatRegressionReport`` into the store and this component renders
 * iff that field is non-null.
 */
export function CompatRegressionModal({ onOverride, onEdit }: Props) {
  const report = useExtractionStore((s) => s.compatRegressionReport);
  const setReport = useExtractionStore((s) => s.setCompatRegressionReport);

  if (!report) return null;

  const close = () => setReport(null);

  return (
    <Dialog open={!!report} onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-amber-500" />
            Save blocked — backward-compatibility regression
          </DialogTitle>
          <DialogDescription>
            The candidate rules list would change{" "}
            <span className="font-mono">{report.regressions.length}</span> past
            extraction{report.regressions.length === 1 ? "" : "s"} (checked{" "}
            <span className="font-mono">{report.checked}</span> of{" "}
            <span className="font-mono">{report.corpus_size}</span> in{" "}
            {report.elapsed_ms.toFixed(0)} ms
            {report.capped && " — capped at recent 500"}
            ). Every past extraction should stay pinned to the DAG the user
            accepted. Review what would change before overriding.
          </DialogDescription>
        </DialogHeader>

        <ScrollArea className="max-h-80 border rounded-md divide-y">
          {report.regressions.map((r: CompatRegression) => (
            <div key={r.extraction_id} className="p-3 space-y-1.5">
              <div className="flex items-start justify-between gap-2">
                <code className="text-xs font-mono text-muted-foreground truncate">
                  {r.extraction_id}
                </code>
                <Badge variant="outline" className="shrink-0 text-[10px]">
                  {r.diff_summary}
                </Badge>
              </div>
              {r.missing_ops.length > 0 && (
                <div className="text-xs">
                  <span className="text-muted-foreground">missing: </span>
                  {r.missing_ops.slice(0, 5).map((op, i) => (
                    <code key={i} className="font-mono mx-0.5 px-1 bg-red-500/10 text-red-700 dark:text-red-400 rounded">
                      {op}
                    </code>
                  ))}
                  {r.missing_ops.length > 5 && (
                    <span className="text-muted-foreground">
                      +{r.missing_ops.length - 5} more
                    </span>
                  )}
                </div>
              )}
              {r.extra_ops.length > 0 && (
                <div className="text-xs">
                  <span className="text-muted-foreground">extra: </span>
                  {r.extra_ops.slice(0, 5).map((op, i) => (
                    <code key={i} className="font-mono mx-0.5 px-1 bg-amber-500/10 text-amber-700 dark:text-amber-400 rounded">
                      {op}
                    </code>
                  ))}
                  {r.extra_ops.length > 5 && (
                    <span className="text-muted-foreground">
                      +{r.extra_ops.length - 5} more
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </ScrollArea>

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={close}>
            Abandon change
          </Button>
          <Button variant="outline" onClick={() => { close(); onEdit(); }}>
            Edit rules
          </Button>
          <Button
            variant="destructive"
            onClick={() => { close(); onOverride(); }}
          >
            Override &amp; save anyway
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
