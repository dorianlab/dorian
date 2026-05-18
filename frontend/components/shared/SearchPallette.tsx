"use client";

import React, { useState, useMemo } from "react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { VisuallyHidden } from "@radix-ui/react-visually-hidden";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { FileSearch, Users } from "lucide-react";

interface SearchPaletteProps<T> {
  open: boolean;
  setOpen: (open: boolean) => void;
  items: T[];
  selectedItems?: T[];
  onSelect: (item: T) => void;
  placeholder?: string;
  filterKey?: keyof T; // e.g. "name"
  titleKey?: keyof T; // display field, e.g. "name"
  emptyMessage?: string;
  footerAction?: React.ReactNode;
}

function SearchPalette<T extends Record<string, any>>({
  open,
  setOpen,
  items,
  selectedItems = [],
  onSelect,
  placeholder = "Search...",
  filterKey = "name",
  titleKey = "name",
  emptyMessage = "No results found.",
  footerAction,
}: SearchPaletteProps<T>) {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items as T[];
    return items.filter((i) => {
      const val = i[filterKey];
      return typeof val === "string" && val.toLowerCase().includes(q);
    });
  }, [items, query, filterKey]);

  const handleSelect = (item: T) => {
    onSelect(item);
    setOpen(false);
    setQuery("");
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        setOpen(v);
        if (!v) setQuery("");
      }}
    >
      <DialogContent
        className='p-0 gap-0 overflow-hidden'
        aria-describedby={undefined}
      >
        <VisuallyHidden>
          <DialogTitle>Search</DialogTitle>
        </VisuallyHidden>
        <Command shouldFilter={false} className='rounded-lg'>
          <div className='flex items-center w-full px-5 py-2 pt-7 '>
            <CommandInput
              autoFocus
              placeholder={placeholder}
              value={query}
              onValueChange={setQuery}
              className='w-full'
            />
          </div>
          <CommandList className='max-h-[min(18rem,50vh)] overflow-y-auto'>
            {!query && (
              <CommandEmpty>
                <div className='flex flex-col items-center py-6 text-muted-foreground text-norma'>
                  <FileSearch className='h-6 w-6 mb-2' />
                  Start typing to search.
                </div>
              </CommandEmpty>
            )}
            {query && filtered.length === 0 && (
              <CommandEmpty>
                <div className='flex flex-col items-center py-6 text-muted-foreground'>
                  <Users className='h-6 w-6 mb-2' />
                  {emptyMessage}
                </div>
              </CommandEmpty>
            )}

            {filtered?.length > 0 && (
              <CommandGroup heading='Results'>
                <ScrollArea>
                  {filtered.map((item, idx) => (
                    <CommandItem
                      key={idx}
                      value={String(item[titleKey])}
                      onSelect={() => handleSelect(item)}
                    >
                      {String(item[titleKey])}
                    </CommandItem>
                  ))}
                </ScrollArea>
              </CommandGroup>
            )}
          </CommandList>
          {footerAction && (
            <div className='px-5 mt-6 flex flex-col item-center justify-center w-full'>
              <div className='flex  overflow-hidden items-center justify-center w-full gap-5 text-muted-foreground '>
                <Separator />
                Or
                <Separator />
              </div>

              <div className='p-5 flex items-center justify-center '>
                {footerAction}
              </div>
            </div>
          )}
        </Command>
      </DialogContent>
    </Dialog>
  );
}

export default SearchPalette;
