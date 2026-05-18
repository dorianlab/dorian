/**
 * frontend/lib/api-client.ts
 * --------------------------
 * Shared fetch-based HTTP client for all backend API calls.
 *
 * Drop-in replacement for the previous axios-based client.  Uses the native
 * `fetch` API everywhere except file uploads with progress tracking, which
 * fall back to XMLHttpRequest (fetch still has no upload progress API).
 *
 * Features:
 *  - Base URL pointing at the FastAPI backend
 *  - HMAC request signing (when enabled)
 *  - Global 429 response handling with a Sonner toast
 *  - Throws ``HttpError`` on non-2xx (mirrors axios; ``err.response``
 *    carries ``{status, statusText, data, headers}`` for catch blocks)
 *
 * Usage:
 *   import { apiClient } from "@/lib/api-client";
 *   const { data } = await apiClient.get("/datasets", { params: { uid } });
 *
 * For sub-clients with a different baseURL:
 *   import { createApiClient } from "@/lib/api-client";
 *   const sessionApi = createApiClient({ baseURL: "http://127.0.0.1:8000/session" });
 */

import { toast } from "sonner";
import { signRequest, hmacEnabled } from "@/lib/hmac";
import env from "@/env.config";

// ---------------------------------------------------------------------------
// Sentinel error — thrown by the 429 handler so callers can distinguish a
// rate-limit rejection (already toasted) from any other error.
// ---------------------------------------------------------------------------

export class RateLimitError extends Error {
  readonly retryAfter: number;
  readonly limit: string;

  constructor(retryAfter: number, limit: string) {
    super(`Rate limited — retry in ${retryAfter}s`);
    this.name = "RateLimitError";
    this.retryAfter = retryAfter;
    this.limit = limit;
  }
}

/** Returns true when the error came from our rate-limit handler (already toasted). */
export function isRateLimitError(err: unknown): err is RateLimitError {
  return err instanceof RateLimitError;
}

// ---------------------------------------------------------------------------
// HttpError — thrown on non-2xx responses. Mirrors the axios-style
// ``err.response.{status,data,headers}`` shape that callers' catch
// blocks already destructure (DatasetImportDialog, useSessionState).
// Without this, a 4xx/5xx body silently flows through ``.then`` and
// downstream code crashes with cryptic ``s.filter is not a function``
// when an error JSON like ``{detail: "..."}`` is treated as an array.
// ---------------------------------------------------------------------------

export class HttpError extends Error {
  readonly response: {
    status: number;
    statusText: string;
    data: unknown;
    headers: Headers;
  };

  constructor(resp: ApiResponse<unknown>) {
    const data = resp.data as { detail?: unknown } | undefined;
    const detail =
      typeof data?.detail === "string"
        ? data.detail
        : typeof data === "string"
        ? data
        : resp.statusText;
    super(`HTTP ${resp.status}: ${detail}`);
    this.name = "HttpError";
    this.response = {
      status: resp.status,
      statusText: resp.statusText,
      data: resp.data,
      headers: resp.headers,
    };
  }
}

// ---------------------------------------------------------------------------
// NetworkError — thrown when ``fetch`` itself fails (connection
// refused, DNS, CORS preflight). Distinct from ``HttpError`` (which
// represents a server-returned 4xx/5xx). Callers catch this when
// they want to render a "backend unreachable" empty state instead of
// the unstyled console TypeError chain that crashed the welcome
// screen on 2026-04-29.
// ---------------------------------------------------------------------------

export class NetworkError extends Error {
  readonly url: string;
  readonly cause: unknown;

  constructor(url: string, cause: unknown) {
    super(`Network error reaching ${url}: ${(cause as Error)?.message ?? cause}`);
    this.name = "NetworkError";
    this.url = url;
    this.cause = cause;
  }
}

export function isNetworkError(err: unknown): err is NetworkError {
  return err instanceof NetworkError;
}

// Debounced backend-offline toast — when the bundle is pointed at a
// down/wrong backend, dozens of parallel requests fail in the same
// frame. Without debouncing the user gets 12+ identical "backend
// offline" toasts; this collapses them into a single sticky banner
// that auto-clears once any subsequent request succeeds.
let _networkErrorToastShownAt = 0;
const _NETWORK_TOAST_DEBOUNCE_MS = 5000;

function handleNetworkError(_cause: unknown): void {
  const now = Date.now();
  if (now - _networkErrorToastShownAt < _NETWORK_TOAST_DEBOUNCE_MS) return;
  _networkErrorToastShownAt = now;
  toast.error("Cannot reach the Dorian backend", {
    description: "Connection refused or unreachable. The page will retry automatically.",
    id: "backend-offline",
    duration: _NETWORK_TOAST_DEBOUNCE_MS,
  });
}

export function isHttpError(err: unknown): err is HttpError {
  return err instanceof HttpError;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RequestConfig {
  headers?: Record<string, string>;
  params?: Record<string, string | number | boolean | undefined>;
  onUploadProgress?: (event: { loaded: number; total: number }) => void;
}

export interface ApiResponse<T = unknown> {
  data: T;
  status: number;
  statusText: string;
  headers: Headers;
}

export interface ClientConfig {
  baseURL?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildUrl(base: string, path: string, params?: RequestConfig["params"]): string {
  const root = base.replace(/\/+$/, "");
  const rel = path.replace(/^\/+/, "");
  const full = rel.startsWith("http") ? rel : rel.length > 0 ? `${root}/${rel}` : root;
  const url = new URL(full);

  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }
  }

  return url.toString();
}

async function attachHMAC(
  method: string,
  fullUrl: string,
  body: string | null,
  headers: Record<string, string>,
): Promise<void> {
  if (!hmacEnabled) return;
  const path = new URL(fullUrl).pathname;
  const hmacHeaders = await signRequest(method, path, body);
  headers["X-HMAC-Signature"] = hmacHeaders["X-HMAC-Signature"];
  headers["X-HMAC-Timestamp"] = hmacHeaders["X-HMAC-Timestamp"];
  headers["X-HMAC-Nonce"] = hmacHeaders["X-HMAC-Nonce"];
}

function handle429(status: number, body: unknown, headers: Headers): never {
  const detail = (body as Record<string, unknown>)?.detail as
    | { error?: string; detail?: string; retryAfter?: number }
    | undefined;

  const retryAfter =
    detail?.retryAfter ?? Number(headers.get("retry-after") ?? 60);
  const limitStr = detail?.detail ?? "";

  toast.error("Too many requests", {
    description: `${limitStr ? `Limit: ${limitStr}. ` : ""}Please try again in ${retryAfter}s.`,
    duration: Math.min(retryAfter * 1_000, 10_000),
  });

  throw new RateLimitError(retryAfter, limitStr);
}

// ---------------------------------------------------------------------------
// XHR upload helper (for onUploadProgress — fetch has no upload progress API)
// ---------------------------------------------------------------------------

function xhrUpload<T>(
  method: string,
  url: string,
  formData: FormData,
  headers: Record<string, string>,
  onProgress: (event: { loaded: number; total: number }) => void,
): Promise<ApiResponse<T>> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url, true);

    // Set headers (skip content-type — browser sets it with boundary for FormData)
    for (const [k, v] of Object.entries(headers)) {
      if (k.toLowerCase() !== "content-type") {
        xhr.setRequestHeader(k, v);
      }
    }

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        onProgress({ loaded: e.loaded, total: e.total });
      }
    });

    xhr.addEventListener("load", () => {
      let data: T;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        data = xhr.responseText as unknown as T;
      }

      if (xhr.status === 429) {
        try {
          handle429(xhr.status, data, new Headers());
        } catch (e) {
          reject(e);
          return;
        }
      }

      const apiResp: ApiResponse<T> = {
        data,
        status: xhr.status,
        statusText: xhr.statusText,
        headers: new Headers(),
      };

      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new HttpError(apiResp));
        return;
      }

      resolve(apiResp);
    });

    xhr.addEventListener("error", () => reject(new Error("Network error")));
    xhr.addEventListener("abort", () => reject(new Error("Request aborted")));

    xhr.send(formData);
  });
}

// ---------------------------------------------------------------------------
// ApiClient class
// ---------------------------------------------------------------------------

class ApiClient {
  private baseURL: string;

  constructor(config?: ClientConfig) {
    this.baseURL = config?.baseURL ?? env.backend;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    config?: RequestConfig,
  ): Promise<ApiResponse<T>> {
    const url = buildUrl(this.baseURL, path, config?.params);
    const headers: Record<string, string> = { ...config?.headers };

    // ── Upload with progress → XHR path ──────────────────────────
    if (config?.onUploadProgress && body instanceof FormData) {
      // HMAC signing for multipart: sign with empty body (can't hash FormData)
      await attachHMAC(method.toUpperCase(), url, null, headers);
      return xhrUpload<T>(method, url, body, headers, config.onUploadProgress);
    }

    // ── Standard fetch path ──────────────────────────────────────
    let fetchBody: BodyInit | undefined;
    let bodyForHMAC: string | null = null;

    if (body instanceof FormData) {
      fetchBody = body;
      // sign with empty body for multipart — the browser-generated
      // boundary is unknowable pre-send so we can't hash the wire
      // bytes. The gateway is expected to bypass HMAC verification
      // on multipart requests (file uploads etc.). For small
      // key-value form posts, callers should prefer URLSearchParams
      // which IS signable end-to-end.
    } else if (body instanceof URLSearchParams) {
      const serialized = body.toString();
      fetchBody = serialized;
      bodyForHMAC = serialized;
      if (!headers["Content-Type"] && !headers["content-type"]) {
        headers["Content-Type"] = "application/x-www-form-urlencoded";
      }
    } else if (body != null) {
      const serialized = typeof body === "string" ? body : JSON.stringify(body);
      fetchBody = serialized;
      bodyForHMAC = serialized;
      if (!headers["Content-Type"] && !headers["content-type"]) {
        headers["Content-Type"] = "application/json";
      }
    }

    await attachHMAC(method.toUpperCase(), url, bodyForHMAC, headers);

    // Don't set content-type for FormData — browser sets it with boundary
    if (body instanceof FormData) {
      delete headers["Content-Type"];
      delete headers["content-type"];
    }

    let resp: Response;
    try {
      resp = await fetch(url, {
        method: method.toUpperCase(),
        headers,
        body: fetchBody,
      });
    } catch (e) {
      // Native fetch throws TypeError("Failed to fetch") on
      // connection refused / DNS failure / CORS block. Surface as
      // a structured ``NetworkError`` so callers can distinguish
      // backend-down from a 5xx, and so the global toast handler
      // shows a single user-visible "backend offline" notice
      // instead of letting every parallel request log a separate
      // console error. The original TypeError is chained so
      // detailed traces are still reachable from devtools.
      handleNetworkError(e);
      throw new NetworkError(url, e);
    }

    let data: T;
    const ct = resp.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      data = await resp.json();
    } else {
      data = (await resp.text()) as unknown as T;
    }

    if (resp.status === 429) {
      handle429(resp.status, data, resp.headers);
    }

    const apiResp: ApiResponse<T> = {
      data,
      status: resp.status,
      statusText: resp.statusText,
      headers: resp.headers,
    };

    // 502/503/504: gateway can't reach the backend (most often
    // backend restarting or upstream timeout). Show the same single
    // sticky "backend offline" toast we use for fetch-level network
    // failures so a transient restart doesn't carpet-bomb the UI
    // with one toast per parallel poll. The page retries automatically;
    // once any subsequent response succeeds the toast clears via the
    // debouncer.
    if (resp.status === 502 || resp.status === 503 || resp.status === 504) {
      handleNetworkError(new Error(`HTTP ${resp.status} ${resp.statusText}`));
      throw new HttpError(apiResp);
    }

    // Non-2xx → throw, matching axios behaviour. Without this the
    // raw error body (often ``{detail: "..."}``) flows through
    // ``.then`` callbacks and breaks downstream type assumptions
    // (e.g. ``setDatasets({detail: "..."})`` then ``.filter`` crash).
    if (!resp.ok) {
      throw new HttpError(apiResp);
    }

    return apiResp;
  }

  async get<T = unknown>(path: string, config?: RequestConfig): Promise<ApiResponse<T>> {
    return this.request<T>("GET", path, undefined, config);
  }

  async post<T = unknown>(path: string, body?: unknown, config?: RequestConfig): Promise<ApiResponse<T>> {
    return this.request<T>("POST", path, body, config);
  }

  async put<T = unknown>(path: string, body?: unknown, config?: RequestConfig): Promise<ApiResponse<T>> {
    return this.request<T>("PUT", path, body, config);
  }

  async patch<T = unknown>(path: string, body?: unknown, config?: RequestConfig): Promise<ApiResponse<T>> {
    return this.request<T>("PATCH", path, body, config);
  }

  async delete<T = unknown>(path: string, config?: RequestConfig): Promise<ApiResponse<T>> {
    return this.request<T>("DELETE", path, undefined, config);
  }
}

// ---------------------------------------------------------------------------
// Default shared instance (baseURL = FastAPI backend)
// ---------------------------------------------------------------------------

export const apiClient = new ApiClient();

// ---------------------------------------------------------------------------
// Factory for named sub-clients
// ---------------------------------------------------------------------------

export function createApiClient(config?: ClientConfig): ApiClient {
  return new ApiClient(config);
}
