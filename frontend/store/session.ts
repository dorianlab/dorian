import { create } from "zustand";
import { getSession } from "next-auth/react";
import type { Session } from "next-auth";
import type { SessionState, Eval } from "@/types/session";

declare module "next-auth" {
  interface User {
    id?: string;
  }
  interface Session {
    user?: User;
  }
}

export const useSessionStore = create<SessionState>((set) => ({
  sessions: [],
  activeSessionId: null,
  userId: "",

  evals: [],
  currentEvals: [],
  objectives: [],
  tasks: [],

  setUserId: (userId: string) => set({ userId }),
  setEvals: (evals: Eval[]) => set({ evals }),
  setCurrentEvals: (currentEvals: Eval[]) => set({ currentEvals }),

  addEval: (evaluation: Eval) =>
    set((s) => ({
      evals: [...s.evals, evaluation],
      currentEvals: [...s.currentEvals, evaluation],
    })),
  setSessions: (sessions) => set({ sessions }),
  addNewSession: (session) =>
    set((s) => ({ sessions: [...s.sessions, session] })),

  updateSession: (sessionId, key, value) =>
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.session_id === sessionId ? { ...s, [key]: value } : s,
      ),
    })),

  removeSession: (sessionId) =>
    set((state) => ({
      sessions: state.sessions.filter((s) => s.session_id !== sessionId),
    })),

  setActiveSessionId: (sessionId) => set({ activeSessionId: sessionId }),
  setObjectives: (objectives) => set({ objectives }),
  addObjective: (objective) =>
    set((state) => ({ objectives: [...state.objectives, objective] })),

  loadUser: async () => {
    const session: Session | null = await getSession();
    if (session?.user?.id) set({ userId: session.user.id });
  },
  setTasks: (tasks) => set({ tasks }),
  addTask: (task) =>
    set((state) => ({
      tasks: [...state.tasks, task],
    })),
}));
