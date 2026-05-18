import { create } from "zustand";
import type {
  PipelineNotification,
  State as NotificationsState,
} from "@/types/notifications";
import { randomUUID } from "@/helpers/uuid";

export const useNotificationsStore = create<NotificationsState>((set, get) => ({
  items: [],

  push: (partial) => {
    const notification: PipelineNotification = {
      id: partial.id ?? randomUUID(),
      createdAt: partial.createdAt ?? Date.now(),
      kind: partial.kind,
      title: partial.title,
      message: partial.message,
      read: partial.read ?? false,
      meta: partial.meta,
    };
    set((state) => {
      // Deduplicate by id first (prevents double-display on reconnect replay)
      if (state.items.some((n) => n.id === notification.id)) {
        return state;
      }
      // Replace an existing unread notification of the same kind+title
      // instead of stacking duplicates.
      const idx = state.items.findIndex(
        (n) => !n.read && n.kind === notification.kind && n.title === notification.title,
      );
      if (idx !== -1) {
        const updated = [...state.items];
        updated[idx] = notification;
        return { items: updated };
      }
      return { items: [...state.items, notification] };
    });
  },

  pushBatch: (notifications) => {
    set((state) => {
      const existingIds = new Set(state.items.map((n) => n.id));
      const newItems = notifications.filter((n) => !existingIds.has(n.id));
      if (newItems.length === 0) return state;
      return { items: [...state.items, ...newItems] };
    });
  },

  markAllRead: () =>
    set((state) => ({
      items: state.items.map((n) => ({ ...n, read: true })),
    })),

  markRead: (id) =>
    set((state) => ({
      items: state.items.map((n) =>
        n.id === id ? { ...n, read: true } : n,
      ),
    })),

  clear: () => set({ items: [] }),

  unreadCount: () => get().items.filter((n) => !n.read).length,
}));
