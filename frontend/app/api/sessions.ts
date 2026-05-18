// services/session.ts
import { createApiClient, isNetworkError } from "@/lib/api-client";
import type { ChatSession } from "@/types/session";
import env from "@/env.config";

const sessionApi = createApiClient({
  baseURL: `${env.backend}/session`,
});

// Use sessionApi here
export async function fetchSessions(uid: string): Promise<ChatSession[]> {
  try {
    const res = await sessionApi.get<ChatSession[]>("/list", { params: { uid } });
    return res.data;
  } catch (error) {
    // Backend unreachable: api-client already raised the global
    // toast; suppress the noisy console.error so the devtools
    // stack trace doesn't drown out the rest of the page. Real
    // server errors (5xx) still log normally for debugging.
    if (!isNetworkError(error)) {
      console.error("Error fetching sessions:", error);
    }
    return [];
  }
}

// Use URLSearchParams (application/x-www-form-urlencoded) for small
// key-value form posts. FormData (multipart) defeats HMAC signing —
// the frontend signs ``body=null`` for FormData (per api-client.ts)
// because the browser-generated multipart boundary is unknowable
// pre-send, while the gateway hashes the actual wire bytes. The
// resulting signature mismatch produces 401s on every form post.
// URLSearchParams is signable, deterministic, and FastAPI accepts
// it for these routes (``Form(...)`` reads either content-type).
export async function createSession(uid: string, name = "New Chat") {
  const form = new URLSearchParams();
  form.append("uid", uid);
  form.append("name", name);

  const res = await sessionApi.post<{ session_id: string; meta: Record<string, string> }>(
    "/create",
    form,
    { headers: { "Content-Type": "application/x-www-form-urlencoded" } },
  );

  return res.data;
}

export async function renameSession(session_id: string, new_title: string) {
  const form = new URLSearchParams();
  form.append("session_id", session_id);
  form.append("new_title", new_title);

  const res = await sessionApi.post(
    "/rename",
    form,
    { headers: { "Content-Type": "application/x-www-form-urlencoded" } },
  );
  return res.data;
}

export async function deleteSession(session_id: string, uid: string) {
  await sessionApi.delete(`/${session_id}`, {
    params: { uid },
  });
}

export async function fetchSession(session_id: string, uid: string) {
  if (!uid) throw new Error("fetchSession called without uid");
  const res = await sessionApi.get<Record<string, unknown>>(`/${session_id}`, {
    params: { uid },
  });
  return res.data;
}

export interface SessionState {
  pipeline?: Record<string, unknown>;
  lastRun?: Record<string, unknown>;
  selectedTask?: string;
  selectedEval?: string;
  selectedObjectives?: { uuid: string; name: string }[];
}

export async function fetchSessionState(session_id: string) {
  const res = await sessionApi.get<SessionState>(`/${session_id}/state`);
  return res.data;
}
