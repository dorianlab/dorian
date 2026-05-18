/**
 * frontend/lib/vault-crypto.ts
 * ----------------------------
 * Client-side AES-256-GCM encryption for user environment variables.
 *
 * All crypto runs in the browser via the Web Crypto API.
 * The server never sees plaintext values — only ciphertext envelopes.
 *
 * Flow:
 *   1. User enters a vault passphrase (kept in sessionStorage, tab-scoped)
 *   2. PBKDF2 derives a 256-bit AES key from passphrase + random salt
 *   3. AES-256-GCM encrypts the env var value with a random 12-byte IV
 *   4. The {ciphertext, iv, salt} envelope is sent to the server for storage
 *   5. At execution time, the passphrase is sent via a nonce so the backend
 *      can decrypt in-memory, inject into the pipeline, and forget
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PBKDF2_ITERATIONS = 600_000;
const SALT_BYTES = 16;
const IV_BYTES = 12;
const KEY_LENGTH_BITS = 256;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Encrypted envelope — all fields are base64-encoded byte strings. */
export interface EncryptedEnvelope {
  ciphertext: string; // base64
  iv: string; // base64 (12 bytes)
  salt: string; // base64 (16 bytes)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function toBase64(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function fromBase64(b64: string): Uint8Array<ArrayBuffer> {
  const binary = atob(b64);
  const buf = new ArrayBuffer(binary.length);
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

// ---------------------------------------------------------------------------
// Key derivation
// ---------------------------------------------------------------------------

/**
 * Derive an AES-256-GCM key from a passphrase and salt using PBKDF2.
 *
 * @param passphrase  User-entered vault passphrase
 * @param salt        Random 16-byte salt (unique per encryption)
 * @returns           CryptoKey suitable for AES-GCM encrypt/decrypt
 */
export async function deriveKey(
  passphrase: string,
  salt: Uint8Array<ArrayBuffer>,
): Promise<CryptoKey> {
  const encoder = new TextEncoder();
  const keyMaterial = await crypto.subtle.importKey(
    "raw",
    encoder.encode(passphrase),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    {
      name: "PBKDF2",
      salt,
      iterations: PBKDF2_ITERATIONS,
      hash: "SHA-256",
    },
    keyMaterial,
    { name: "AES-GCM", length: KEY_LENGTH_BITS },
    false,
    ["encrypt", "decrypt"],
  );
}

// ---------------------------------------------------------------------------
// Encrypt / Decrypt
// ---------------------------------------------------------------------------

/**
 * Encrypt a plaintext value using AES-256-GCM with a passphrase-derived key.
 *
 * A fresh random salt and IV are generated for every call, so encrypting
 * the same value twice produces different ciphertext (semantic security).
 */
export async function encrypt(
  plaintext: string,
  passphrase: string,
): Promise<EncryptedEnvelope> {
  const encoder = new TextEncoder();
  const salt = new Uint8Array(crypto.getRandomValues(new Uint8Array(SALT_BYTES)));
  const iv = new Uint8Array(crypto.getRandomValues(new Uint8Array(IV_BYTES)));
  const key = await deriveKey(passphrase, salt);

  const ciphertextBuf = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    encoder.encode(plaintext),
  );

  return {
    ciphertext: toBase64(ciphertextBuf),
    iv: toBase64(iv),
    salt: toBase64(salt),
  };
}

/**
 * Decrypt an encrypted envelope back to plaintext.
 *
 * Used client-side only (e.g. for a future "reveal" feature).
 * The backend has its own decryption in `dorian/vault/crypto.py`.
 */
export async function decrypt(
  envelope: EncryptedEnvelope,
  passphrase: string,
): Promise<string> {
  const salt = fromBase64(envelope.salt);
  const iv = fromBase64(envelope.iv);
  const ciphertext = fromBase64(envelope.ciphertext);
  const key = await deriveKey(passphrase, salt);

  const plaintextBuf = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv },
    key,
    ciphertext,
  );

  return new TextDecoder().decode(plaintextBuf);
}

// ---------------------------------------------------------------------------
// Passphrase session management
// ---------------------------------------------------------------------------

const PASSPHRASE_KEY = "dorian:vault:passphrase";

/** True when running in the browser (sessionStorage is available). */
const _isBrowser = typeof window !== "undefined";

/** Store passphrase in sessionStorage (tab-scoped, cleared on tab close). */
export function storePassphrase(passphrase: string): void {
  if (_isBrowser) sessionStorage.setItem(PASSPHRASE_KEY, passphrase);
}

/** Retrieve passphrase from sessionStorage. Returns null if not set. */
export function getPassphrase(): string | null {
  return _isBrowser ? sessionStorage.getItem(PASSPHRASE_KEY) : null;
}

/** Clear passphrase from sessionStorage (lock the vault). */
export function clearPassphrase(): void {
  if (_isBrowser) sessionStorage.removeItem(PASSPHRASE_KEY);
}

/** Check whether the vault is currently unlocked (passphrase in session). */
export function isVaultUnlocked(): boolean {
  return _isBrowser && sessionStorage.getItem(PASSPHRASE_KEY) !== null;
}
