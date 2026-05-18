"use client";

/**
 * DatasetUploadDialog
 * -------------------
 * Prompts the user for an optional description right after they pick a CSV
 * file. The actual upload is deferred to the caller's ``onConfirm`` handler so
 * this component stays agnostic of progress tracking / store wiring.
 */

import { useEffect, useState } from "react";
import { Upload } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";

interface Props {
  file: File | null;
  onCancel: () => void;
  onConfirm: (description: string) => Promise<void> | void;
}

export function DatasetUploadDialog({ file, onCancel, onConfirm }: Props) {
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // Reset draft whenever a new file is picked.
  useEffect(() => {
    setDescription("");
    setSubmitting(false);
  }, [file]);

  if (!file) return null;

  const commit = async () => {
    setSubmitting(true);
    try {
      await onConfirm(description.trim());
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={!!file}
      onOpenChange={(open) => {
        if (!open && !submitting) onCancel();
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Upload className="h-4 w-4" />
            Upload dataset
          </DialogTitle>
          <DialogDescription>
            <span className="font-medium text-foreground">{file.name}</span> —{" "}
            {(file.size / 1024).toFixed(1)} KB
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          <Label htmlFor="dataset-description" className="text-xs">
            Description <span className="text-muted-foreground">(optional)</span>
          </Label>
          <Textarea
            id="dataset-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What is this dataset? Source, columns, provenance, caveats…"
            className="min-h-[120px] text-sm"
            disabled={submitting}
            autoFocus
          />
          <p className="text-[11px] text-muted-foreground">
            You can edit this later on the dataset detail page.
          </p>
        </div>

        <DialogFooter>
          <Button
            variant="ghost"
            size="sm"
            onClick={onCancel}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button size="sm" onClick={commit} disabled={submitting}>
            {submitting ? "Uploading…" : "Upload"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
