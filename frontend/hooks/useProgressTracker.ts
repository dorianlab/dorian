"use client";

import { useState, useCallback } from "react";

export type ProcessStatus =
  | "pending"
  | "loading"
  | "success"
  | "error"
  | "warning";

export interface Process {
  id: string;
  name: string;
  description?: string;
  status: ProcessStatus;
  startTime: Date;
  endTime?: Date;
  progress?: number; // 0-100
  detail?: string;
}

export function useProcessTracker() {
  const [processes, setProcesses] = useState<Process[]>([]);
  const [currentProcessId, setCurrentProcessId] = useState<
    string | undefined
  >();

  // Start a new process
  const startProcess = useCallback(
    (name: string, description?: string): string => {
      const id = `process-${Date.now()}-${Math.random()
        .toString(36)
        .substring(2, 9)}`;
      const newProcess: Process = {
        id,
        name,
        description,
        status: "loading",
        startTime: new Date(),
      };

      setProcesses((prev) => [...prev, newProcess]);
      setCurrentProcessId(id);

      return id;
    },
    []
  );

  // Update a process
  const updateProcess = useCallback(
    (id: string, updates: Partial<Omit<Process, "id" | "startTime">>) => {
      setProcesses((prev) =>
        prev.map((process) =>
          process.id === id ? { ...process, ...updates } : process
        )
      );
    },
    []
  );

  // Complete a process
  const completeProcess = useCallback(
    (
      id: string,
      status: "success" | "error" | "warning" = "success",
      detail?: string
    ) => {
      setProcesses((prev) =>
        prev.map((process) =>
          process.id === id
            ? {
                ...process,
                status,
                endTime: new Date(),
                detail: detail || process.detail,
              }
            : process
        )
      );

      if (currentProcessId === id) {
        setCurrentProcessId(undefined);
      }
    },
    [currentProcessId]
  );

  // Update progress of a process
  const updateProgress = useCallback((id: string, progress: number) => {
    setProcesses((prev) =>
      prev.map((process) =>
        process.id === id
          ? { ...process, progress: Math.min(100, Math.max(0, progress)) }
          : process
      )
    );
  }, []);

  // Retry a failed process
  const retryProcess = useCallback((id: string) => {
    setProcesses((prev) => {
      const processToRetry = prev.find((p) => p.id === id);
      if (!processToRetry) return prev;

      const newId = `${id}-retry-${Date.now()}`;
      const newProcess: Process = {
        ...processToRetry,
        id: newId,
        status: "loading",
        startTime: new Date(),
        endTime: undefined,
        progress: undefined,
      };

      setCurrentProcessId(newId);
      return [...prev, newProcess];
    });
  }, []);

  // Clear all processes
  const clearProcesses = useCallback(() => {
    setProcesses([]);
    setCurrentProcessId(undefined);
  }, []);

  return {
    processes,
    currentProcessId,
    startProcess,
    updateProcess,
    completeProcess,
    updateProgress,
    retryProcess,
    clearProcesses,
  };
}
