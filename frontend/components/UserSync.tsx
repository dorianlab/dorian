"use client";

import { useEffect } from "react";
import { useSession } from "next-auth/react";
import { useSessionStore } from "@/store/session";

/**
 * Layout-level component that syncs the NextAuth session user ID into the
 * Zustand `useSessionStore`.  Previously this sync only happened in
 * `app/page.tsx` (Home), so any other page visited directly (e.g. /library)
 * would have `userId === ""` and all WS connections (useDatasetLive,
 * usePipelineSocket) would refuse to connect.
 *
 * Mount this inside `<AuthProvider>` in the root layout.
 */
export default function UserSync() {
  const { data: session, status } = useSession();
  const userId = useSessionStore((s) => s.userId);
  const setUserId = useSessionStore((s) => s.setUserId);

  useEffect(() => {
    if (status === "authenticated" && session?.user?.id && !userId) {
      setUserId(session.user.id);
    }
  }, [status, session, userId, setUserId]);

  return null;
}
