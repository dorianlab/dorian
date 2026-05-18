/** @type {import('next').NextConfig} */
//
// Next.js rewrites turn the frontend container into a same-origin
// reverse proxy for the rust gateway's HTTP surface. The SPA hits
// :3000 for every REST call and these rewrites hand the request off
// to the gateway over the docker network (``http://gateway:8080``)
// without changing the browser-visible origin.
//
// WebSockets do NOT go through this proxy: ``rewrites()`` is HTTP
// only and would 404 the upgrade. The SPA connects WS directly to
// the gateway's external port (:8000) — see deploy-manual.sh's
// NEXT_PUBLIC_WS_URL derivation.
//
// The internal target is whatever ``DORIAN_GATEWAY_INTERNAL`` resolves
// to inside the docker network (default ``http://gateway:8080`` —
// gateway's container-internal listener). At build time the value is
// baked into the rewrite table; at runtime Next.js uses it for every
// proxied request.
//
// Path-collision rule: API rewrites here use ``beforeFiles`` so they
// take precedence over file-system routes. Same-named SPA pages
// (``app/<name>/page.tsx``) MUST live at non-API paths — e.g. the
// dataset browser is mounted at ``/library`` precisely because
// ``/datasets`` is the python API. Don't add ``app/datasets/page.tsx``
// or any file route whose path also exists in this rewrite table; it
// will be silently shadowed and the corresponding ``apiClient`` call
// will receive whatever the gateway serves at that path.
const GATEWAY_INTERNAL =
  process.env.DORIAN_GATEWAY_INTERNAL || 'http://gateway:8080';

const nextConfig = {
  reactStrictMode: false,
  async rewrites() {
    return {
      beforeFiles: [
        // Public endpoints (no HMAC, no auth):
        { source: '/stats',          destination: `${GATEWAY_INTERNAL}/stats` },
        { source: '/openapi.json',   destination: `${GATEWAY_INTERNAL}/openapi.json` },
        { source: '/docs/:path*',    destination: `${GATEWAY_INTERNAL}/docs/:path*` },
        { source: '/redoc/:path*',   destination: `${GATEWAY_INTERNAL}/redoc/:path*` },
        // Worker-pool liveness probe — reachable through the same
        // origin as the SPA so external monitors don't need to hop
        // through a separate firewall rule. The route is owned by the
        // python backend (``main.py::healthz``); the gateway forwards
        // it like any other API path.
        { source: '/healthz',        destination: `${GATEWAY_INTERNAL}/healthz` },

        // Authenticated REST surface — gateway natively serves
        // ``/session/*``, ``/eventbus/*`` and proxies ``/api/*`` etc.
        // to the python backend. ``/api/auth/*`` is intentionally NOT
        // proxied — that's NextAuth's own provider/callback handler
        // running in the frontend container itself.
        { source: '/session/:path*', destination: `${GATEWAY_INTERNAL}/session/:path*` },
        { source: '/eventbus/:path*',destination: `${GATEWAY_INTERNAL}/eventbus/:path*` },
        { source: '/emit',           destination: `${GATEWAY_INTERNAL}/emit` },
        // /api proxy with NextAuth carve-out: route everything except
        // ``/api/auth/*`` to the gateway. Negative lookahead in the
        // path pattern keeps NextAuth local.
        {
          source: '/api/:path((?!auth(?:/|$)).*)',
          destination: `${GATEWAY_INTERNAL}/api/:path*`,
        },
        { source: '/datasets',       destination: `${GATEWAY_INTERNAL}/datasets` },
        { source: '/datasets/:path*',destination: `${GATEWAY_INTERNAL}/datasets/:path*` },
        { source: '/extract/:path*', destination: `${GATEWAY_INTERNAL}/extract/:path*` },
        { source: '/rules',          destination: `${GATEWAY_INTERNAL}/rules` },
        { source: '/rules/:path*',   destination: `${GATEWAY_INTERNAL}/rules/:path*` },
        { source: '/upload',         destination: `${GATEWAY_INTERNAL}/upload` },
        { source: '/import',         destination: `${GATEWAY_INTERNAL}/import` },
        // Catalog: SPA hits ``/catalog/*`` (see frontend/app/api/catalog.ts).
        // Gateway natively serves ``/catalog/*`` from the rust KbSnapshot.
        { source: '/catalog',        destination: `${GATEWAY_INTERNAL}/catalog` },
        { source: '/catalog/:path*', destination: `${GATEWAY_INTERNAL}/catalog/:path*` },
        { source: '/contact/:path*', destination: `${GATEWAY_INTERNAL}/contact/:path*` },
        { source: '/vault/:path*',   destination: `${GATEWAY_INTERNAL}/vault/:path*` },
        { source: '/admin/:path*',   destination: `${GATEWAY_INTERNAL}/admin/:path*` },
        // Bare ``/observability`` is the SPA dashboard page; the python
        // backend only exposes sub-paths (``/observability/handlers``
        // etc.). ``:path+`` (one-or-more) instead of ``:path*`` so the
        // bare path falls through to the file route — otherwise Next.js
        // RSC prefetches of ``<Link href="/observability">`` get
        // rewritten to the gateway and HMAC-rejected with 401.
        { source: '/observability/:path+',
                                     destination: `${GATEWAY_INTERNAL}/observability/:path+` },

        // No /ws rewrite. Next.js's ``rewrites()`` only proxies HTTP —
        // a WS upgrade request would 404 the upgrade. The SPA connects
        // its WebSocket directly to the gateway at ``${gateway}:8000/ws``
        // (NEXT_PUBLIC_WS_URL is baked at build time by deploy-manual.sh).
      ],
    };
  },
}

module.exports = nextConfig
