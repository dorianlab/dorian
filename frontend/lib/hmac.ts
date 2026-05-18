/**
 * frontend/lib/hmac.ts
 * --------------------
 * HMAC-SHA256 request signing utilities.
 *
 * Preferred path: Web Crypto API (``crypto.subtle``). Native, fast,
 * constant-time. Selected whenever ``crypto.subtle`` is defined — the
 * typical case for HTTPS / localhost / service-worker contexts.
 *
 * Fallback path: the pure-TypeScript ``js-sha256`` library. Selected
 * when ``crypto.subtle`` is missing (plain-HTTP deployments — browsers
 * only expose ``crypto.subtle`` in secure contexts) or when the native
 * path throws at runtime. The fallback produces the same hex output so
 * the backend's HMAC middleware can't tell them apart.
 *
 * The switch is automatic at every call — page loads on HTTPS pick the
 * native implementation, the same bundle loaded over HTTP uses the
 * fallback, and a transient native failure degrades to the fallback
 * without a full reload.
 *
 * Every outgoing HTTP request is signed with:
 *   X-HMAC-Signature:  hex(HMAC-SHA256(secret, canonical_request))
 *   X-HMAC-Timestamp:  Unix epoch seconds
 *   X-HMAC-Nonce:      Random 32-char hex string
 *
 * Canonical request format:
 *   {METHOD}\n{PATH}\n{TIMESTAMP}\n{NONCE}\n{BODY_SHA256}
 */
import { sha256 } from "js-sha256";

const HMAC_SECRET = process.env.NEXT_PUBLIC_HMAC_SECRET ?? "";

/** True when HMAC signing is configured. The signer picks the right
 *  implementation (native ``crypto.subtle`` vs. pure-TS fallback) at
 *  every call, so this flag does not depend on the runtime context. */
export const hmacEnabled = HMAC_SECRET.length > 0;


// ---------------------------------------------------------------------------
// Runtime detection — recomputed on every call so a context change (e.g.
// a service worker attaching crypto.subtle late) is picked up without a
// page reload. The check is a cheap property access.
// ---------------------------------------------------------------------------

function hasSubtle(): boolean {
  const g: any = globalThis as any;
  return !!(g && g.crypto && g.crypto.subtle);
}


// ---------------------------------------------------------------------------
// Native path (crypto.subtle) — preferred under HTTPS / localhost
// ---------------------------------------------------------------------------

let _cachedKey: CryptoKey | null = null;

async function getSubtleKey(): Promise<CryptoKey> {
  if (_cachedKey) return _cachedKey;
  const encoder = new TextEncoder();
  _cachedKey = await crypto.subtle.importKey(
    "raw",
    encoder.encode(HMAC_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return _cachedKey;
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function subtleDigestHex(data: Uint8Array): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", data.buffer as ArrayBuffer);
  return toHex(hash);
}

async function subtleHmacHex(message: Uint8Array): Promise<string> {
  const key = await getSubtleKey();
  const sig = await crypto.subtle.sign("HMAC", key, message.buffer as ArrayBuffer);
  return toHex(sig);
}


// ---------------------------------------------------------------------------
// Fallback path (js-sha256) — used when crypto.subtle is unavailable
// ---------------------------------------------------------------------------
// js-sha256's TypeScript typings only surface the ``sha256`` function;
// ``sha256.hmac(key, message)`` is available on its namespace at runtime
// but doesn't have a typed signature. A local type assertion keeps the
// TS compiler happy without adding ``@types/js-sha256``.
//
// The functions accept Uint8Array (same as the native path) so the two
// code paths have identical preconditions.

type Sha256Ns = ((msg: Uint8Array | string) => string) & {
  hmac: (key: string, message: Uint8Array | string) => string;
};

const _sha256 = sha256 as unknown as Sha256Ns;

function fallbackDigestHex(data: Uint8Array): string {
  return _sha256(data);
}

function fallbackHmacHex(message: Uint8Array): string {
  return _sha256.hmac(HMAC_SECRET, message);
}


// ---------------------------------------------------------------------------
// Nonce generation
// ---------------------------------------------------------------------------

function generateNonce(): string {
  const bytes = new Uint8Array(16);
  const g: any = globalThis as any;
  if (g.crypto && typeof g.crypto.getRandomValues === "function") {
    g.crypto.getRandomValues(bytes);
  } else {
    // Insecure fallback — only reachable in exotic test environments.
    // Every browser + node ≥15 exposes crypto.getRandomValues.
    for (let i = 0; i < bytes.length; i++) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}


// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export interface HMACHeaders {
  "X-HMAC-Signature": string;
  "X-HMAC-Timestamp": string;
  "X-HMAC-Nonce": string;
}

/**
 * Compute HMAC-SHA256 signing headers for a request.
 *
 * Uses ``crypto.subtle`` when available (native, preferred) and transparently
 * falls back to the pure-TypeScript implementation when it isn't (e.g. on
 * plain HTTP) or when it throws. Callers don't need to care which path ran.
 *
 * @param method - HTTP method (GET, POST, etc.)
 * @param path   - URL path (e.g., "/session/create")
 * @param body   - Raw request body (string or undefined for bodyless requests)
 */
export async function signRequest(
  method: string,
  path: string,
  body?: string | null,
): Promise<HMACHeaders> {
  const encoder = new TextEncoder();
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const nonce = generateNonce();
  const bodyBytes = encoder.encode(body ?? "");
  const upper = method.toUpperCase();

  if (hasSubtle()) {
    try {
      const bodyHash = await subtleDigestHex(bodyBytes);
      const canonical = `${upper}\n${path}\n${timestamp}\n${nonce}\n${bodyHash}`;
      const signatureHex = await subtleHmacHex(encoder.encode(canonical));
      return {
        "X-HMAC-Signature": signatureHex,
        "X-HMAC-Timestamp": timestamp,
        "X-HMAC-Nonce": nonce,
      };
    } catch {
      // Native path failed (rare — misconfigured CSP, exotic runtime).
      // Fall through to the pure-TS implementation so signing doesn't
      // silently break the app.
    }
  }

  const bodyHash = fallbackDigestHex(bodyBytes);
  const canonical = `${upper}\n${path}\n${timestamp}\n${nonce}\n${bodyHash}`;
  const signatureHex = fallbackHmacHex(encoder.encode(canonical));
  return {
    "X-HMAC-Signature": signatureHex,
    "X-HMAC-Timestamp": timestamp,
    "X-HMAC-Nonce": nonce,
  };
}
