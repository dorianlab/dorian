"use client";

import { useEffect, useCallback } from "react";

import {
  fetchSessions,
  createSession,
  deleteSession,
  renameSession,
  fetchSession,
} from "@/app/api/sessions";
import { useSessionStore } from "@/store/session";
import { usePipelineStore } from "@/store/pipeline";

// Module-level dedup for concurrent /session/list calls. Multiple components
// mount useChatSessions on first page load; without this guard each one
// fires its own request, and before the backend lock was added they would
// each trigger a "First Session" auto-create. Keyed by uid so distinct
// users in the same tab (shouldn't happen, but) don't share a promise.
const _inflightFetchSessions = new Map<string, Promise<unknown>>();

export function useChatSessions() {
  const {
    activeSessionId,
    setActiveSessionId,
    userId,
    sessions,
    setSessions,
    addNewSession,
    updateSession,
    removeSession,
  } = useSessionStore();
  const { setTempPipeline } = usePipelineStore();

  const handleFetchSessions = async () => {
    if (!userId) return;
    // Dedup: if another caller is already fetching for this uid, await
    // their promise instead of firing a parallel request. The backend
    // lock already prevents duplicate "First Session" creation, but
    // avoiding the second request saves a roundtrip and keeps the
    // /session/list fast path uncontested.
    const existing = _inflightFetchSessions.get(userId);
    if (existing) {
      await existing;
      return;
    }
    const p = (async () => {
      try {
        const data = await fetchSessions(userId);
        setSessions(data);
      } finally {
        _inflightFetchSessions.delete(userId);
      }
    })();
    _inflightFetchSessions.set(userId, p);
    await p;
  };

  const handleFetchSession = async () => {
    try {
      if (!activeSessionId || !userId) return;
      const session = await fetchSession(activeSessionId, userId);

      if (session.pipeline) {
        const _pipeline = JSON.parse(session.pipeline as string);
        setTempPipeline(_pipeline);
      }
    } catch (error) {}
  };

  useEffect(() => {
    if (!userId) return;
    handleFetchSessions();
  }, [userId]);

  useEffect(() => {
    if (!activeSessionId || !userId) return;

    handleFetchSession();
  }, [activeSessionId, userId]);

  const handleCreateSession = async (title?: string) => {
    if (!userId) {
      console.warn("[useChatSessions] refusing to create session: userId not set yet");
      return "";
    }
    const { session_id, meta } = await createSession(userId, title);

    // The backend meta may not include every ChatSession field — normalise
    // so the Zustand store always receives a well-formed object.
    addNewSession({
      session_id: meta.session_id ?? session_id,
      name: meta.name ?? title ?? "New Chat",
      created_at: meta.created_at ?? new Date().toISOString(),
      updated_at: meta.updated_at ?? new Date().toISOString(),
    });

    // Auto-select the newly created session so it's immediately visible.
    setActiveSessionId(session_id);
    return session_id;
  };

  const handleDeleteSession = useCallback(async (sessionId: string) => {
    await deleteSession(sessionId, userId);
    const _updatedSessions = sessions.filter((s) => s.session_id !== sessionId);

    if (_updatedSessions.length === 0) {
      setActiveSessionId("");
    }

    removeSession(sessionId);
  }, []);

  const handleRenameSession = async (sessionId: string, newTitle: string) => {
    await renameSession(sessionId, newTitle);
    updateSession(sessionId, "name", newTitle);
  };

  const selectSession = (sessionId: string) => {
    setActiveSessionId(sessionId);
  };

  const updateSessionActivity = (sessionId: string, lastMessage: string) => {
    setSessions(sessions);
  };

  return {
    sessions,
    activeSessionId,
    handleCreateSession,
    handleDeleteSession,
    handleRenameSession,
    selectSession,
    updateSessionActivity,
    handleFetchSession,
  };
}
