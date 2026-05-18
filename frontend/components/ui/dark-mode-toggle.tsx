"use client";

import { useTheme } from "next-themes";
import { Moon, Sun } from "lucide-react";
import { cn } from "@/helpers/utils";

export function DarkModeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const isDark = theme === "dark";

  return (
    <button
      onClick={() => setTheme(isDark ? "light" : "dark")}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      className={cn(
        "flex   items-center gap-1 rounded-full border border-border bg-muted p-1 transition-colors hover:bg-accent",
        className,
      )}
    >
      <span
        className={`flex h-6 w-6 items-center justify-center rounded-full transition-all ${
          !isDark ? "bg-background shadow-sm" : "text-muted-foreground"
        }`}
      >
        <Sun className='h-3.5 w-3.5' />
      </span>
      <span
        className={`flex h-6 w-6 items-center justify-center rounded-full transition-all ${
          isDark ? "bg-background shadow-sm" : "text-muted-foreground"
        }`}
      >
        <Moon className='h-3.5 w-3.5' />
      </span>
    </button>
  );
}
