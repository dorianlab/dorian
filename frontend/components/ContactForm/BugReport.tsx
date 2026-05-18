"use client";

import type React from "react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import FileUpload from "./FileInput";
import { submitBugReport } from "@/app/api/contact";
import { useSessionStore } from "@/store/session";
import { toast } from "sonner";
import { isRateLimitError } from "@/lib/api-client";

export default function BugReport() {
  const userId = useSessionStore((s) => s.userId);
  const [bugFormData, setBugFormData] = useState({
    name: "",
    title: "",
    description: "",
    steps: "",
    expected: "",
    device: "",
    severity: "low",
  });

  const [files, setFiles] = useState<File[]>([]);
  const [loading, setLoading] = useState(false);

  const handleBugSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await submitBugReport(userId, bugFormData, files);
      toast.success("Bug report submitted", {
        description: "Thanks for reporting! We'll look into it.",
      });
      setBugFormData({
        name: "",
        title: "",
        description: "",
        steps: "",
        expected: "",
        device: "",
        severity: "low",
      });
      setFiles([]);
    } catch (err) {
      if (isRateLimitError(err)) return;
      toast.error("Submission failed", {
        description: "Failed to submit bug report. Please try again.",
      });
    } finally {
      setLoading(false);
    }
  };

  const allowSubmission = Boolean(bugFormData.title && bugFormData.description);

  return (
    <form onSubmit={handleBugSubmit} className='space-y-5 pt-4'>
      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='bug-name'>Your name <span className='text-muted-foreground font-normal'>(optional)</span></Label>
        <Input
          id='bug-name'
          placeholder='How should we address you?'
          value={bugFormData.name}
          onChange={(e) =>
            setBugFormData({ ...bugFormData, name: e.target.value })
          }
        />
      </div>

      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='bug-title'>Title</Label>
        <Input
          id='bug-title'
          placeholder='Brief description of the issue'
          value={bugFormData.title}
          onChange={(e) =>
            setBugFormData({ ...bugFormData, title: e.target.value })
          }
          required
        />
      </div>

      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='bug-description'>Description</Label>
        <Textarea
          id='bug-description'
          placeholder='Detailed description of the bug'
          className='min-h-[100px]'
          value={bugFormData.description}
          onChange={(e) =>
            setBugFormData({ ...bugFormData, description: e.target.value })
          }
          required
        />
      </div>

      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='bug-steps'>Steps to Reproduce</Label>
        <Textarea
          id='bug-steps'
          placeholder='1. Go to... 2. Click on...'
          className='min-h-[80px]'
          value={bugFormData.steps}
          onChange={(e) =>
            setBugFormData({ ...bugFormData, steps: e.target.value })
          }
        />
      </div>

      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='bug-expected'>Expected vs. Actual Behavior</Label>
        <Textarea
          id='bug-expected'
          placeholder='What you expected to happen and what actually happened'
          className='min-h-[80px]'
          value={bugFormData.expected}
          onChange={(e) =>
            setBugFormData({ ...bugFormData, expected: e.target.value })
          }
        />
      </div>

      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='bug-device'>Device/Browser Information</Label>
        <Input
          id='bug-device'
          placeholder='e.g., Chrome 98 on Windows 10'
          value={bugFormData.device}
          onChange={(e) =>
            setBugFormData({ ...bugFormData, device: e.target.value })
          }
        />
      </div>

      <div className='flex flex-col gap-2.5'>
        <Label>Severity</Label>
        <Select
          value={bugFormData.severity}
          onValueChange={(val) =>
            setBugFormData({ ...bugFormData, severity: val })
          }
        >
          <SelectTrigger>
            <SelectValue placeholder='Select severity' />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value='low'>Low - Minor issue</SelectItem>
            <SelectItem value='medium'>
              Medium - Affects functionality but has workaround
            </SelectItem>
            <SelectItem value='high'>
              High - Major functionality broken
            </SelectItem>
            <SelectItem value='critical'>
              Critical - System crash or data loss
            </SelectItem>
          </SelectContent>
        </Select>
      </div>

      <FileUpload
        files={files}
        setFiles={setFiles}
        label='Attachments (Optional)'
        helperText='Images (JPEG, PNG, GIF) or PDF (Max 5MB)'
        allowedTypes={[
          "image/jpeg",
          "image/png",
          "image/gif",
          "application/pdf",
        ]}
        maxSize={5 * 1024 * 1024}
        multiple
      />

      <Button disabled={!allowSubmission || loading} type='submit' className='w-full'>
        {loading ? "Submitting..." : "Submit Bug Report"}
      </Button>
    </form>
  );
}
