import type { Task } from "@/types/pipeline";
import type { UUID } from "@/types/index";
export interface ChatSession {
  session_id: string;
  name: string;
  lastMessage?: string;
  created_at: string;
  updated_at: string;
  messageCount?: number;
}

export type SessionState = {
  sessions: ChatSession[];
  activeSessionId: string | null;
  userId: string;

  evals: Eval[];
  currentEvals: Eval[];
  objectives: Objective[];
  tasks: Task[];

  setUserId: (userId: string) => void;
  setEvals: (evals: Eval[]) => void;
  setCurrentEvals: (currentEvals: Eval[]) => void;
  addEval: (evaluation: Eval) => void;
  setSessions: (sessions: ChatSession[]) => void;
  addNewSession: (session: ChatSession) => void;
  updateSession: (sessionId: string, key: string, value: string) => void;
  removeSession: (sessionId: string) => void;

  setActiveSessionId: (sessionId: string | null) => void;
  setObjectives: (objectives: Objective[]) => void;
  addObjective: (objective: Objective) => void;
  setTasks: (tasks: Task[]) => void;
  addTask: (task: Task) => void;
  loadUser: () => Promise<void>;
};

export interface Eval {
  uuid: UUID;
  name: string;
}

export interface Objective {
  uuid: UUID;
  name: string;
  language?: string;
  code?: string;
  type?: "snippet" | "parameter" | "operator";
}
