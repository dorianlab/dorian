# =============================================================================
# Dorian Frontend — Multi-stage build with pnpm
# =============================================================================

# ---- Stage 1: install dependencies ----
FROM docker.io/library/node:22-alpine AS deps

# Bump npm 10.9.7 (bundled with node:22-alpine) → 11.13.0.
#
# ``npm install -g npm@11`` from npm 10 fails in the middle of reify
# with ``Cannot find module 'promise-retry'`` because npm 10's
# in-process arborist still loads the old dependency tree while the
# new npm 11 files have already replaced the on-disk module
# (npm 11 dropped ``promise-retry``). Self-upgrades across the major
# version boundary are unreliable.
#
# Workaround: drop the bundled npm and extract the 11.13.0 tarball in
# its place. The bin shims at /usr/local/bin/npm still resolve to
# node_modules/npm/bin/npm-cli.js, so npm --version reports 11.13.0
# afterwards. Then install pnpm normally.
RUN apk add --no-cache curl \
    && rm -rf /usr/local/lib/node_modules/npm \
    && mkdir -p /tmp/npm-pkg \
    && curl -fsSL https://registry.npmjs.org/npm/-/npm-11.13.0.tgz \
       | tar -xz -C /tmp/npm-pkg \
    && mv /tmp/npm-pkg/package /usr/local/lib/node_modules/npm \
    && rm -rf /tmp/npm-pkg \
    && npm --version \
    && npm install -g pnpm@latest

WORKDIR /app

COPY package.json pnpm-lock.yaml ./

# --no-frozen-lockfile: pnpm-lock.yaml may lag behind package.json
# in development; the lock is updated in-container and discarded.
#
# Concurrency is dialled down to avoid EMFILE (worker init failure) on hosts
# with a low per-process file-descriptor ceiling — pnpm's default fan-out of
# network=16/child=5 workers blows past `ulimit -n=1024` on most Debian/
# Ubuntu defaults. Bump the soft FD limit inside the RUN as a belt-and-
# braces measure (the `ulimit` only affects the RUN's shell).
#
# The retry loop ACTUALLY fails if all attempts fail — the previous
# `&& break || sleep 10` swallowed errors and let a broken install through
# (symptom: `next: not found` in the builder stage because node_modules
# was empty).
ENV npm_config_network_concurrency=4
ENV npm_config_child_concurrency=2
ENV npm_config_fetch_retries=5
ENV npm_config_fetch_retry_mintimeout=20000
ENV npm_config_fetch_retry_maxtimeout=120000
# Force pnpm's extraction worker pool to a single thread. pnpm 10 spawns
# one @pnpm/worker per CPU core for tarball extraction, and each worker
# keeps a clutch of file descriptors open — that pool is what blew past
# ulimit -n on the deploy host. There is no CLI flag for this (the flags
# only govern network/script fan-out), but the internal WORKERS env var
# is honoured. libuv's UV_THREADPOOL_SIZE bounds the underlying fs pool.
ENV PNPM_WORKERS=1
ENV UV_THREADPOOL_SIZE=4
# Force Node to prefer IPv4 in DNS resolution. pnpm workers hang and
# crash with `(undefined)` errors when the host is dual-stack but IPv6
# to registry.npmjs.org is broken/slow (common behind corporate NAT).
ENV NODE_OPTIONS="--dns-result-order=ipv4first"
# Give the fetcher a long leash so a worker mid-download isn't killed
# by a 30s default timeout.
ENV npm_config_fetch_timeout=600000
# Print pnpm's reason for failure instead of the "(undefined)" placeholder.
ENV npm_config_loglevel=info
RUN apk add --no-cache curl ca-certificates; \
    ulimit -n 65536 2>/dev/null || ulimit -n 16384 2>/dev/null || true; \
    echo "==> DIAGNOSTICS"; \
    echo "    ulimit -n:        $(ulimit -n)"; \
    echo "    /proc limits:"; \
    grep -E '^(Max open files|Max processes)' /proc/self/limits 2>/dev/null || true; \
    echo "    node:             $(node --version)"; \
    echo "    pnpm:             $(pnpm --version)"; \
    echo "    registry reach:   $(curl -4 -sS -o /dev/null -w '%{http_code} (%{time_total}s)' https://registry.npmjs.org/ || echo 'FAILED')"; \
    echo "    dns lookup:       $(getent hosts registry.npmjs.org || echo 'FAILED')"; \
    echo ""; \
    set -e; \
    for i in 1 2 3; do \
      echo "==> pnpm install attempt $i/3"; \
      if pnpm install --no-frozen-lockfile \
           --ignore-scripts \
           --prefer-offline \
           --network-concurrency=2 \
           --child-concurrency=1; then \
        echo "==> pnpm install succeeded"; \
        break; \
      fi; \
      echo "==> attempt $i failed, sleeping 15s"; \
      sleep 15; \
    done; \
    if [ ! -x node_modules/.bin/next ]; then \
      echo "==> pnpm failed to install a usable node_modules — falling back to npm"; \
      rm -rf node_modules; \
      npm config set fetch-retries 5; \
      npm config set fetch-retry-maxtimeout 600000; \
      npm install --no-audit --no-fund --legacy-peer-deps --prefer-offline \
        || ( echo "!! npm install also failed"; exit 1 ); \
    fi; \
    if [ ! -x node_modules/.bin/next ]; then \
      echo "!! node_modules/.bin/next missing after install"; \
      ls -la node_modules/.bin/ 2>/dev/null | head -20; \
      exit 1; \
    fi; \
    echo "==> node_modules/.bin/next: OK"

# ---- Stage 2: build Next.js application ----
FROM docker.io/library/node:22-alpine AS builder

# Bump npm 10.9.7 (bundled with node:22-alpine) → 11.13.0.
#
# ``npm install -g npm@11`` from npm 10 fails in the middle of reify
# with ``Cannot find module 'promise-retry'`` because npm 10's
# in-process arborist still loads the old dependency tree while the
# new npm 11 files have already replaced the on-disk module
# (npm 11 dropped ``promise-retry``). Self-upgrades across the major
# version boundary are unreliable.
#
# Workaround: drop the bundled npm and extract the 11.13.0 tarball in
# its place. The bin shims at /usr/local/bin/npm still resolve to
# node_modules/npm/bin/npm-cli.js, so npm --version reports 11.13.0
# afterwards. Then install pnpm normally.
RUN apk add --no-cache curl \
    && rm -rf /usr/local/lib/node_modules/npm \
    && mkdir -p /tmp/npm-pkg \
    && curl -fsSL https://registry.npmjs.org/npm/-/npm-11.13.0.tgz \
       | tar -xz -C /tmp/npm-pkg \
    && mv /tmp/npm-pkg/package /usr/local/lib/node_modules/npm \
    && rm -rf /tmp/npm-pkg \
    && npm --version \
    && npm install -g pnpm@latest

WORKDIR /app

# Copy installed node_modules from deps stage
COPY --from=deps /app/node_modules ./node_modules

# Copy all source files
COPY . .

# Build-time environment variables (baked into the JS bundle by Next.js).
# No defaults — env.config.ts throws at module load if any required
# NEXT_PUBLIC_* is empty in a non-dev build, so a forgotten build-arg
# fails the ``pnpm build`` step instead of shipping a localhost-baked
# bundle to a remote box. ``deploy-manual.sh`` exports each var before
# ``docker build`` so the deploy path stays unchanged.
ARG NEXT_PUBLIC_WS_URL
ARG NEXT_PUBLIC_BACKEND_URL
ARG NEXT_PUBLIC_FRONTEND_URL
ARG NEXT_PUBLIC_HMAC_SECRET
ARG NEXT_PUBLIC_GITHUB_ID
ARG NEXT_PUBLIC_GITHUB_SECRET

ENV NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL
ENV NEXT_PUBLIC_BACKEND_URL=$NEXT_PUBLIC_BACKEND_URL
ENV NEXT_PUBLIC_FRONTEND_URL=$NEXT_PUBLIC_FRONTEND_URL
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_HMAC_SECRET=$NEXT_PUBLIC_HMAC_SECRET
ENV NEXT_PUBLIC_GITHUB_ID=$NEXT_PUBLIC_GITHUB_ID
ENV NEXT_PUBLIC_GITHUB_SECRET=$NEXT_PUBLIC_GITHUB_SECRET

# Cap Next.js build workers.  `next build` (Turbopack) spawns one Node
# process per page for "Collecting page data" — with a large route tree
# that can be 300+ workers, each holding IPC pipes + module FDs.  Even
# with ulimit -n raised to 65536, the sheer process fan-out triggers
# EMFILE on `spawn /usr/local/bin/node`.  NEXT_PRIVATE_WORKER_THREADS
# limits the build worker pool (undocumented but stable since Next 14).
ENV NEXT_PRIVATE_WORKER_THREADS=8
RUN pnpm build

# ---- Stage 3: production runner ----
FROM docker.io/library/node:22-alpine AS runner

# Bump npm 10.9.7 (bundled with node:22-alpine) → 11.13.0.
#
# ``npm install -g npm@11`` from npm 10 fails in the middle of reify
# with ``Cannot find module 'promise-retry'`` because npm 10's
# in-process arborist still loads the old dependency tree while the
# new npm 11 files have already replaced the on-disk module
# (npm 11 dropped ``promise-retry``). Self-upgrades across the major
# version boundary are unreliable.
#
# Workaround: drop the bundled npm and extract the 11.13.0 tarball in
# its place. The bin shims at /usr/local/bin/npm still resolve to
# node_modules/npm/bin/npm-cli.js, so npm --version reports 11.13.0
# afterwards. Then install pnpm normally.
RUN apk add --no-cache curl \
    && rm -rf /usr/local/lib/node_modules/npm \
    && mkdir -p /tmp/npm-pkg \
    && curl -fsSL https://registry.npmjs.org/npm/-/npm-11.13.0.tgz \
       | tar -xz -C /tmp/npm-pkg \
    && mv /tmp/npm-pkg/package /usr/local/lib/node_modules/npm \
    && rm -rf /tmp/npm-pkg \
    && npm --version \
    && npm install -g pnpm@latest

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Copy only what's needed to run
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./
COPY --from=builder /app/next.config.js ./
# Custom server.js — adds a ``/ws`` upgrade reverse-proxy on top of
# Next.js. ``pnpm start`` invokes ``node server.js`` (see
# ``package.json::scripts.start``). Without this file in the image
# the container would fail to start.
COPY --from=builder /app/server.js ./
# Static assets at the site root (e.g. /dorian-logo.svg referenced
# by the login screen). Next.js serves these directly from
# ``public/`` without any explicit route — but only if the directory
# actually ships in the image. Was missing pre-2026-04-28; the
# login screen 404'd on the logo.
COPY --from=builder /app/public ./public

EXPOSE 3000

CMD ["pnpm", "start"]
