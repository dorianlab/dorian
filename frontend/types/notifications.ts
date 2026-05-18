export type State = {
  items: PipelineNotification[];
  push: (
    n: Omit<PipelineNotification, "id" | "createdAt"> &
      Partial<Pick<PipelineNotification, "id" | "createdAt">>,
  ) => void;
  /** Push a batch of notifications (reconnect replay). Deduplicates by id. */
  pushBatch: (notifications: PipelineNotification[]) => void;
  markAllRead: () => void;
  markRead: (id: string) => void;
  clear: () => void;
  /** Number of unread notifications. */
  unreadCount: () => number;
};

export type NotificationKind = "success" | "warning" | "error" | "info";

export type PipelineNotification = {
  id: string;
  kind: NotificationKind;
  title: string;
  message?: string;
  createdAt: number; // Date.now()
  read?: boolean;
  meta?: Record<string, any>; // optional (nodeId, runId, etc.)
};
