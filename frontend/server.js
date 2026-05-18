/**
 * Custom Next.js entrypoint that adds a WebSocket reverse-proxy
 * for ``/ws`` alongside the regular HTTP server.
 *
 * Why this file exists at all: Next.js ``rewrites()`` is HTTP-only
 * (it 404s the upgrade) and there is no Route Handler / middleware
 * primitive that fires on the raw ``upgrade`` event — the canonical
 * answer in the Next docs is a custom server. The ``serv-7101``
 * deploy needs WS to enter through ``:3000`` (firewall blocks the
 * gateway's external ``:8000``), so the SPA points its WebSocket
 * URL at this server and we forward the upgrade to the gateway
 * over the docker network.
 *
 * Why no http-proxy: 30 lines of raw ``net.connect`` + bidirectional
 * pipe is enough; pulling in an extra dep would obscure the actual
 * shape of the handshake.
 *
 * Knobs:
 *   * ``DORIAN_GATEWAY_HOST`` / ``DORIAN_GATEWAY_PORT`` — upstream
 *     target (default ``gateway:8080`` — the gateway's
 *     docker-internal listener).
 *   * ``DORIAN_DISABLE_WS_PROXY=1`` — bypass this layer and let the
 *     SPA reach the gateway directly (only useful when the deploy
 *     exposes the gateway externally).
 */
const http = require("http");
const net = require("net");
const { parse } = require("url");
const next = require("next");

const PORT = parseInt(process.env.PORT || "3000", 10);
const HOSTNAME = process.env.HOSTNAME || "0.0.0.0";
const dev = process.env.NODE_ENV !== "production";
const GATEWAY_HOST = process.env.DORIAN_GATEWAY_HOST || "gateway";
const GATEWAY_PORT = parseInt(process.env.DORIAN_GATEWAY_PORT || "8080", 10);
const WS_PROXY_DISABLED = process.env.DORIAN_DISABLE_WS_PROXY === "1";

const app = next({ dev, hostname: HOSTNAME, port: PORT });
const handle = app.getRequestHandler();

function proxyUpgrade(req, clientSocket, head) {
  const upstream = net.connect({ host: GATEWAY_HOST, port: GATEWAY_PORT });

  const teardown = (err) => {
    if (err) console.error("[ws-proxy]", err.message || err);
    try { clientSocket.destroy(); } catch (_) { /* swallow */ }
    try { upstream.destroy(); } catch (_) { /* swallow */ }
  };
  upstream.on("error", teardown);
  clientSocket.on("error", teardown);

  upstream.on("connect", () => {
    // Rebuild the HTTP/1.1 upgrade request. Node has already parsed
    // and consumed the headers off the wire — they're in ``req.headers``.
    // Rewrite Host so it matches the upstream's vhost / Origin checks,
    // and tack on X-Forwarded-* for parity with a real reverse proxy.
    const headers = { ...req.headers };
    headers["host"] = `${GATEWAY_HOST}:${GATEWAY_PORT}`;
    const xff = req.socket.remoteAddress || "";
    headers["x-forwarded-for"] = headers["x-forwarded-for"]
      ? `${headers["x-forwarded-for"]}, ${xff}`
      : xff;
    headers["x-forwarded-proto"] = "http";
    headers["x-forwarded-host"] = req.headers["host"] || "";

    const lines = [`${req.method} ${req.url} HTTP/1.1`];
    for (const [k, v] of Object.entries(headers)) {
      if (Array.isArray(v)) for (const vv of v) lines.push(`${k}: ${vv}`);
      else lines.push(`${k}: ${v}`);
    }
    upstream.write(lines.join("\r\n") + "\r\n\r\n");
    if (head && head.length) upstream.write(head);

    // Bidirectional pipe. ``pipe(end: true)`` (default) means closing
    // either side cleanly tears down the other.
    clientSocket.pipe(upstream);
    upstream.pipe(clientSocket);
  });
}

app.prepare().then(() => {
  const server = http.createServer((req, res) => {
    handle(req, res, parse(req.url, true));
  });

  if (!WS_PROXY_DISABLED) {
    server.on("upgrade", (req, socket, head) => {
      if ((req.url || "").startsWith("/ws")) {
        proxyUpgrade(req, socket, head);
        return;
      }
      // Next.js owns all other upgrade paths (HMR in dev, etc.).
      // Pass the upgrade through to its handler so we don't accidentally
      // close a legit Next.js socket.
      handle(req, socket, parse(req.url, true));
    });
  }

  server.listen(PORT, HOSTNAME, (err) => {
    if (err) throw err;
    console.log(
      `[server] ready on http://${HOSTNAME}:${PORT} ` +
      `(ws-proxy ${WS_PROXY_DISABLED ? "disabled" : `→ ${GATEWAY_HOST}:${GATEWAY_PORT}/ws`})`
    );
  });
});
