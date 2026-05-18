"use client";

import clsx from "clsx";
import { Check } from "lucide-react";

type SimpleLoaderSize = "sm" | "md";

interface SimpleLoaderProps {
  status: string;
  startTime?: Date;       // reserved for future elapsed-time display
  className?: string;
  showElapsedTime?: boolean;
  size?: SimpleLoaderSize;
}

const sizeClass: Record<SimpleLoaderSize, string> = {
  sm: "h-4 w-4",
  md: "h-6 w-6",
};

export function SimpleLoader({
  status,
  className,
  size = "md",
}: SimpleLoaderProps) {
  const getStatusColor = () => {
    switch (status) {
      case "computing": return "text-blue-500";
      case "computed":  return "text-green-500";
      case "error":     return "text-red-500";
      case "warning":   return "text-yellow-500";
      default:          return "text-gray-400";
    }
  };

  return (
    <div className={clsx("relative flex items-center", className)}>
      <div
        className={clsx(
          "relative flex items-center justify-center transition-all duration-300",
          sizeClass[size],
          getStatusColor(),
        )}
      >
        {/* Spinning arc — computing / pending */}
        {(status === "computing" || status === "pending") && (
          <svg
            className="w-full h-full"
            viewBox="0 0 24 24"
            xmlns="http://www.w3.org/2000/svg"
          >
            <circle
              className="opacity-25"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="2"
              fill="none"
            />
            <path
              className="opacity-75"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              d="M12 2a10 10 0 0 1 10 10"
            >
              <animateTransform
                attributeName="transform"
                type="rotate"
                from="0 12 12"
                to="360 12 12"
                dur="1s"
                repeatCount="indefinite"
              />
            </path>
          </svg>
        )}

        {/* Check mark — computed */}
        {status === "computed" && (
          <Check className="w-full h-full text-green-500 animate-fade-in" />
        )}

        {/* X — error */}
        {status === "error" && (
          <svg
            className="w-full h-full"
            viewBox="0 0 24 24"
            xmlns="http://www.w3.org/2000/svg"
          >
            <g
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              className="animate-simple-draw"
            >
              <path d="M18 6L6 18" />
              <path d="M6 6l12 12" />
            </g>
          </svg>
        )}

        {/* Triangle — warning */}
        {status === "warning" && (
          <svg
            className="w-full h-full"
            viewBox="0 0 24 24"
            xmlns="http://www.w3.org/2000/svg"
          >
            <path
              d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              className="animate-simple-draw"
            />
            <g
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              className="animate-simple-draw"
            >
              <path d="M12 8v5" />
              <path d="M12 16h.01" />
            </g>
          </svg>
        )}
      </div>
    </div>
  );
}
