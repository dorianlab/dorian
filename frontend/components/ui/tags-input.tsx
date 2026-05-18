"use client";

import * as React from "react";
import { X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/helpers/utils";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandList,
} from "@/components/ui/command";

type TagsInputProps = {
  value: string[];
  onChange: (tags: string[]) => void;
  suggestions?: string[];
  placeholder?: string;
  maxTags?: number;
  className?: string;
};

function normalizeTag(tag: string) {
  return tag.trim().replace(/\s+/g, " ");
}

export default function TagsInput({
  value,
  onChange,
  suggestions = [],
  placeholder = "Type and press Enter…",
  maxTags,
  className,
}: TagsInputProps) {
  const [input, setInput] = React.useState("");
  const [open, setOpen] = React.useState(false);

  const tags = value ?? [];

  const canAddMore = maxTags ? tags.length < maxTags : true;

  const addTag = (raw: string) => {
    if (!canAddMore) return;

    const tag = normalizeTag(raw);
    if (!tag) return;

    // prevent duplicates (case-insensitive)
    const exists = tags.some((t) => t.toLowerCase() === tag.toLowerCase());
    if (exists) {
      setInput("");
      setOpen(false);
      return;
    }

    onChange([...tags, tag]);
    setInput("");
    setOpen(false);
  };

  const removeTag = (tag: string) => {
    onChange(tags.filter((t) => t !== tag));
  };

  const filteredSuggestions = suggestions
    .filter((s) => s.toLowerCase().includes(input.toLowerCase()))
    .filter((s) => !tags.some((t) => t.toLowerCase() === s.toLowerCase()))
    .slice(0, 8);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addTag(input);
      return;
    }

    if (e.key === "," || e.key === "Tab") {
      if (!input.trim()) return;
      e.preventDefault();
      addTag(input);
      return;
    }

    if (e.key === "Backspace" && !input && tags.length) {
      removeTag(tags[tags.length - 1]);
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLInputElement>) => {
    const text = e.clipboardData.getData("text");
    if (!text) return;

    // supports comma/newline separated paste
    const parts = text
      .split(/,|\n/)
      .map((p) => normalizeTag(p))
      .filter(Boolean);

    if (!parts.length) return;

    e.preventDefault();

    let next = [...tags];
    for (const p of parts) {
      if (maxTags && next.length >= maxTags) break;
      if (!next.some((t) => t.toLowerCase() === p.toLowerCase())) next.push(p);
    }
    onChange(next);
    setInput("");
    setOpen(false);
  };

  return (
    <div className={cn("space-y-2", className)}>
      {/* chips */}
      {tags.length > 0 && (
        <div className='flex flex-wrap gap-2'>
          {tags.map((tag) => (
            <Badge key={tag} variant='secondary' className='gap-1 pr-1'>
              <span>{tag}</span>
              <Button
                type='button'
                variant='ghost'
                size='icon'
                className='h-5 w-5'
                onClick={() => removeTag(tag)}
              >
                <X className='h-3 w-3' />
                <span className='sr-only'>Remove {tag}</span>
              </Button>
            </Badge>
          ))}
        </div>
      )}

      {/* input + suggestions */}
      <div className='relative'>
        <Input
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => {
            // small delay so clicks on suggestions register
            window.setTimeout(() => setOpen(false), 120);
          }}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={placeholder}
          disabled={!canAddMore}
        />

        {open && filteredSuggestions.length > 0 && (
          <div className='absolute z-50 mt-2 w-full rounded-md border bg-popover text-popover-foreground shadow'>
            <Command>
              <CommandList>
                <CommandEmpty>No results</CommandEmpty>
                <CommandGroup>
                  {filteredSuggestions.map((s) => (
                    <CommandItem key={s} value={s} onSelect={() => addTag(s)}>
                      {s}
                    </CommandItem>
                  ))}
                </CommandGroup>
              </CommandList>
            </Command>
          </div>
        )}
      </div>

      {maxTags && (
        <p className='text-xs text-muted-foreground'>
          {tags.length}/{maxTags} tags
        </p>
      )}
    </div>
  );
}
