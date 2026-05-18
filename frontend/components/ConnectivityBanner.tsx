"use client";

import { WifiOff, RefreshCw } from "lucide-react";
import useWebSocketStore, { ConnectionStatus } from "@/store/web-socket";
import { usePipelineRunStore } from "@/store/pipeline-run";

type BannerConfig = {
  bg: string;
  message: string;
  icon: React.ReactNode;
};

function getBannerConfig(
  status: ConnectionStatus,
  runningWhileDisconnected: boolean,
): BannerConfig | null {
  switch (status) {
    case "reconnecting":
      return {
        bg: "bg-amber-500",
        message: runningWhileDisconnected
          ? "Connection lost during pipeline execution — reconnecting…"
          : "Reconnecting to server…",
        icon: <RefreshCw className="h-3.5 w-3.5 animate-spin flex-shrink-0" />,
      };
    case "offline":
      return {
        bg: "bg-red-600",
        message: "You are offline. Will reconnect automatically when network is available.",
        icon: <WifiOff className="h-3.5 w-3.5 flex-shrink-0" />,
      };
    case "error":
      return {
        bg: "bg-red-600",
        message: "Connection error — retrying…",
        icon: <RefreshCw className="h-3.5 w-3.5 animate-spin flex-shrink-0" />,
      };
    default:
      return null;
  }
}

export default function ConnectivityBanner() {
  const status = useWebSocketStore((s) => s.connectionStatus);
  const pipelineRun = usePipelineRunStore((s) => s.pipelineRun);

  const runningWhileDisconnected =
    (status === "reconnecting" || status === "offline" || status === "error") &&
    pipelineRun?.status === "running";

  const cfg = getBannerConfig(status, runningWhileDisconnected);
  if (!cfg) return null;

  return (
    <div
      className={`shrink-0 flex items-center justify-center gap-2 px-4 py-1.5 text-xs font-medium text-white ${cfg.bg}`}
      role="status"
      aria-live="polite"
    >
      {cfg.icon}
      <span>{cfg.message}</span>
    </div>
  );
}
