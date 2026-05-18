// Browser UUID helper.
//
// `crypto.randomUUID()` is only defined in *secure contexts* (HTTPS or
// localhost). On a non-secure remote host (e.g. `http://<host>:3000`)
// it is `undefined`, so calling it as a function throws
// `TypeError: crypto.randomUUID is not a function` and the click handler
// crashes silently — the user clicked Compose and "nothing fired."
//
// Use this helper everywhere we need a client-side identifier. The
// `crypto.getRandomValues` path is widely available (works under HTTP
// too) and gives us RFC4122-compliant v4 UUIDs. The string fallback
// only triggers when `crypto` itself is missing (server-side rendering
// before hydration, very old browsers).
export function randomUUID(): string {
  if (typeof crypto !== "undefined") {
    if (typeof (crypto as Crypto).randomUUID === "function") {
      return (crypto as Crypto).randomUUID();
    }
    if (typeof crypto.getRandomValues === "function") {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      // RFC 4122 §4.4 — version 4, variant 10xx
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
      return (
        hex.slice(0, 4).join("") +
        "-" +
        hex.slice(4, 6).join("") +
        "-" +
        hex.slice(6, 8).join("") +
        "-" +
        hex.slice(8, 10).join("") +
        "-" +
        hex.slice(10, 16).join("")
      );
    }
  }
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`;
}
