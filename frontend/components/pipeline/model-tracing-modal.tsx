"use client";

import * as React from "react";
import { useState, useEffect, useMemo } from "react";
import { ScanSearch } from "lucide-react";
import envConfig from "@/env.config";
import { useModelTracingStore } from "@/store/model-tracing";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";

export default function ModelTracingModal() {
  const { activeNodeId, traceOutput, close } = useModelTracingStore();
  const isOpen = activeNodeId !== null && traceOutput !== null;

  const images = useMemo(
    () =>
      (traceOutput?.images ?? []).map(
        (p) => `${envConfig.backend}${p}`,
      ),
    [traceOutput],
  );
  const logs = traceOutput?.logs ?? [];

  const [activeImage, setActiveImage] = useState(0);
  useEffect(() => {
    setActiveImage(0);
  }, [images]);

  return (
    <Dialog open={isOpen} onOpenChange={(open) => { if (!open) close(); }}>
      <DialogContent className="w-[90vw] sm:max-w-[90vw] max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ScanSearch className="h-5 w-5" />
            Model Tracing
          </DialogTitle>
          <DialogDescription>
            Inspect model tracing output. Supports decision tree visualizations,
            feature importance plots, and log files.
          </DialogDescription>
        </DialogHeader>

        <Separator />

        {images.length === 0 && logs.length === 0 ? (
          <div className="rounded-md border border-dashed border-muted-foreground/30 bg-muted/20 p-8 text-center text-sm text-muted-foreground">
            No trace output yet — run the pipeline to see results here.
          </div>
        ) : (
          <div className="flex flex-col gap-6">
            {/* Image gallery */}
            {images.length > 0 && (
              <div className="flex flex-col gap-3">
                {/* Thumbnail strip */}
                <div className="flex gap-2 overflow-x-auto pb-1">
                  {images.map((src, i) => (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      key={i}
                      src={src}
                      alt={`Trace ${i}`}
                      onClick={() => setActiveImage(i)}
                      className={`h-16 w-24 object-cover rounded cursor-pointer border-2 shrink-0 transition-colors ${
                        activeImage === i
                          ? "border-primary"
                          : "border-transparent hover:border-muted-foreground/40"
                      }`}
                    />
                  ))}
                </div>
                {/* Main image viewer */}
                <div
                  className="rounded-md border bg-muted/30 flex items-center justify-center overflow-hidden"
                  style={{ height: logs.length > 0 ? "50vh" : "65vh" }}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={images[activeImage]}
                    alt={`Trace ${activeImage}`}
                    className="max-h-full max-w-full object-contain"
                  />
                </div>
              </div>
            )}

            {/* Log files */}
            {logs.map((log, i) => (
              <div key={i}>
                <p className="text-xs text-muted-foreground mb-1 font-mono">
                  {log.path}
                </p>
                <ScrollArea className="h-48 rounded-md border p-3 bg-muted/30">
                  <pre className="whitespace-pre-wrap break-all text-xs">
                    {log.content}
                  </pre>
                </ScrollArea>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
