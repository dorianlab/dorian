"use client";

import { useEffect, useState } from "react";
import { useObservabilityStore, useSessionStore } from "@/store";

const LOOKBACK_OPTIONS = [
  { label: "1m", value: 60 },
  { label: "5m", value: 300 },
  { label: "15m", value: 900 },
  { label: "1h", value: 3600 },
];

export default function ScopeToggle() {
  const { scope, since, setScope, setSince } = useObservabilityStore();
  const userId = useSessionStore((s) => s.userId);
  const [uidInput, setUidInput] = useState("");

  // Default to the signed-in user's scope on first mount with a known
  // userId — the dashboard's intuitive default is "what I submitted",
  // not "every pipeline across every account". Manual override (Global
  // button or typing another uid) keeps working unchanged.
  useEffect(() => {
    if (userId && scope === "global") {
      setScope(userId);
    }
  }, [userId]);  // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex items-center gap-4 text-sm">
      {/* Scope */}
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground font-medium">Scope:</span>
        <button
          onClick={() => userId && setScope(userId)}
          disabled={!userId}
          className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
            scope === userId && userId
              ? "bg-emerald-600 text-white"
              : "bg-muted text-foreground hover:bg-muted/70 disabled:opacity-50"
          }`}
          title={userId ? `Filter to ${userId}` : "Sign in to filter to your runs"}
        >
          Mine
        </button>
        <button
          onClick={() => setScope("global")}
          className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
            scope === "global"
              ? "bg-orange-600 text-white"
              : "bg-muted text-foreground hover:bg-muted/70"
          }`}
        >
          Global
        </button>
        <div className="flex items-center gap-1">
          <input
            type="text"
            placeholder="uid..."
            value={uidInput}
            onChange={(e) => setUidInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && uidInput.trim()) {
                setScope(uidInput.trim());
              }
            }}
            className="w-32 px-2 py-1 rounded bg-muted border border-border text-xs text-foreground
                       placeholder:text-muted-foreground focus:outline-none focus:border-orange-500"
          />
          {scope !== "global" && scope !== userId && (
            <span className="text-xs text-orange-600 font-mono truncate max-w-[120px]">
              {scope}
            </span>
          )}
        </div>
      </div>

      {/* Lookback */}
      <div className="flex items-center gap-1.5">
        <span className="text-muted-foreground font-medium">Window:</span>
        {LOOKBACK_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            onClick={() => setSince(opt.value)}
            className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
              since === opt.value
                ? "bg-sky-600 text-white"
                : "bg-muted text-foreground hover:bg-muted/70"
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
    </div>
  );
}
