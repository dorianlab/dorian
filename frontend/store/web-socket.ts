import { create } from "zustand";
import { encode } from "@msgpack/msgpack";

export type ConnectionStatus =
  | "idle"         // before first connection attempt
  | "connecting"   // initial handshake in progress
  | "connected"    // open and healthy
  | "reconnecting" // lost; retrying with exponential backoff
  | "offline"      // navigator.onLine is false
  | "error";       // socket error (transient; still retries)

type WebSocketStore = {
  socket: WebSocket | null;
  connectionStatus: ConnectionStatus;
  setSocket: (socket: WebSocket | null) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
  sendMessage: (msg: any) => void;
  disconnect: () => void;
};

const useWebSocketStore = create<WebSocketStore>((set, get) => ({
  socket: null,
  connectionStatus: "idle",

  setSocket: (socket) => set({ socket }),
  setConnectionStatus: (connectionStatus) => set({ connectionStatus }),

  sendMessage: (msg) => {
    const { socket } = get();
    // Guard on readyState so we never call send() on a CLOSING/CLOSED socket.
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(encode(msg));
    }
  },

  disconnect: () => {
    const socket = get().socket;
    try {
      socket?.close();
    } catch {}
    set({ socket: null, connectionStatus: "idle" });
  },
}));

export default useWebSocketStore;
