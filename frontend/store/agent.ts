import { create } from "zustand";

export type AgentState = {
  /** When true, all outbound WS events include agentDriven: true */
  agentMode: boolean;
  /** Controls the Agent Panel dialog open/close */
  panelOpen: boolean;

  setAgentMode: (on: boolean) => void;
  setPanelOpen: (open: boolean) => void;
};

export const useAgentStore = create<AgentState>((set) => ({
  agentMode: false,
  panelOpen: false,

  setAgentMode: (on) => set({ agentMode: on }),
  setPanelOpen: (open) => set({ panelOpen: open }),
}));
