"use client";

import { AboutDorianButton } from "@/components/AboutDorianDialog";

export default function AlphaBanner() {
  return (
    <div className='flex items-center justify-center gap-2 bg-primary/90 text-primary-foreground px-4 py-1.5 text-xs font-medium shadow-sm shrink-0'>
      <span>
        Alpha Release — You are using an early preview of Dorian. Feedback is
        very welcome!
      </span>
      <AboutDorianButton />
    </div>
  );
}
