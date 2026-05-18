"use client";

import type React from "react";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { cn } from "@/helpers/utils";

import { Upload, X, FileIcon, ImageIcon, FileTextIcon } from "lucide-react";

export type FileUploadProps = {
  /** Array of files currently uploaded */
  files: File[];
  /** Function to set files */
  setFiles: React.Dispatch<React.SetStateAction<File[]>>;
  /** Allowed file types (MIME types) */
  allowedTypes?: string[];
  /** Maximum file size in bytes */
  maxSize?: number;
  /** Allow multiple file uploads */
  multiple?: boolean;
  /** Label for the upload area */
  label?: string;
  /** Helper text for the upload area */
  helperText?: string;
  /** ID for the input element */
  id?: string;
  /** Class name for the container */
  className?: string;
};

export default function FileUpload({
  files,
  setFiles,
  allowedTypes = ["image/jpeg", "image/png", "image/gif", "application/pdf"],
  maxSize = 5 * 1024 * 1024, // 5MB default
  multiple = true,
  label = "Attachments",
  helperText = "Images (JPEG, PNG, GIF) or PDF (Max 5MB)",
  id = "file-upload",
  className = "",
}: FileUploadProps) {
  const [fileErrors, setFileErrors] = useState<string>("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} bytes`;
    if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1048576).toFixed(1)} MB`;
  };

  const validateFiles = (
    filesToValidate: File[],
  ): { valid: File[]; error: string } => {
    const validFiles: File[] = [];
    let error = "";

    for (const file of filesToValidate) {
      if (!allowedTypes.includes(file.type)) {
        error = `Only ${allowedTypes
          .map((type) => type.split("/")[1])
          .join(", ")} files are allowed.`;
        break;
      }
      if (file.size > maxSize) {
        error = `Files must be less than ${formatFileSize(maxSize)}.`;
        break;
      }
      validFiles.push(file);
    }

    return { valid: validFiles, error };
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = e.target.files;
    if (!selectedFiles || selectedFiles.length === 0) return;

    const filesArr = Array.from(selectedFiles);
    const filesToProcess = multiple ? filesArr : filesArr.slice(0, 1);

    const { valid, error } = validateFiles(filesToProcess);

    if (error) {
      setFileErrors(error);
    } else {
      setFiles((prev) => (multiple ? [...prev, ...valid] : valid));
      setFileErrors("");
    }

    // reset input to allow re-uploading same file
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const handleFileDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();

    if (!e.dataTransfer.files?.length) return;

    const droppedFiles = Array.from(e.dataTransfer.files);
    const filesToProcess = multiple ? droppedFiles : droppedFiles.slice(0, 1);

    const { valid, error } = validateFiles(filesToProcess);

    if (error) {
      setFileErrors(error);
    } else {
      setFiles((prev) => (multiple ? [...prev, ...valid] : valid));
      setFileErrors("");
    }
  };

  const removeFile = (index: number) => {
    setFiles(files.filter((_, i) => i !== index));
  };

  const getFileIcon = (fileType: string) => {
    if (fileType.startsWith("image/"))
      return <ImageIcon className='h-5 w-5 text-blue-500' />;
    if (fileType === "application/pdf")
      return <FileTextIcon className='h-5 w-5 text-red-500' />;
    return <FileIcon className='h-5 w-5 text-muted-foreground' />;
  };

  const hasError = Boolean(fileErrors);

  return (
    <div className={cn("space-y-2", className)}>
      <Label htmlFor={id}>{label}</Label>

      <div
        role='button'
        tabIndex={0}
        className={cn(
          "rounded-lg border-2 border-dashed p-6 text-center cursor-pointer transition-colors",
          "bg-muted/20 hover:bg-muted/30",
          hasError
            ? "border-destructive"
            : "border-muted-foreground/30 hover:border-muted-foreground/50",
        )}
        onDragOver={(e) => {
          e.preventDefault();
          e.stopPropagation();
        }}
        onDrop={handleFileDrop}
        onClick={() => fileInputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") fileInputRef.current?.click();
        }}
      >
        <input
          type='file'
          id={id}
          ref={fileInputRef}
          onChange={handleFileChange}
          className='hidden'
          accept={allowedTypes.join(",")}
          multiple={multiple}
        />

        <div className='flex flex-col items-center justify-center gap-2'>
          <Upload className='h-8 w-8 text-muted-foreground' />
          <p className='text-sm text-muted-foreground'>
            <span className='font-medium text-foreground'>Click to upload</span>{" "}
            or drag and drop
          </p>
          <p className='text-xs text-muted-foreground'>{helperText}</p>
        </div>
      </div>

      {hasError && <p className='text-sm text-destructive'>{fileErrors}</p>}

      {/* File Preview */}
      {files.length > 0 && (
        <div className='mt-4 space-y-2'>
          <Label>Uploaded Files</Label>

          <div className='space-y-2'>
            {files.map((file, index) => (
              <div
                key={`${file.name}-${file.size}-${index}`}
                className='flex items-center justify-between rounded-md border bg-muted/30 p-3'
              >
                <div className='flex items-center gap-3 min-w-0'>
                  {getFileIcon(file.type)}

                  <div className='flex flex-col min-w-0'>
                    <span className='text-sm font-medium truncate max-w-[240px]'>
                      {file.name}
                    </span>
                    <span className='text-xs text-muted-foreground'>
                      {formatFileSize(file.size)}
                    </span>
                  </div>
                </div>

                <Button
                  type='button'
                  variant='ghost'
                  size='icon'
                  onClick={() => removeFile(index)}
                  className='shrink-0'
                >
                  <X className='h-4 w-4' />
                  <span className='sr-only'>Remove file</span>
                </Button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
