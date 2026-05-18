// Public env vars baked into the Next.js bundle at build time.
//
// NEXT_PUBLIC_* vars are inlined by Next.js's webpack pass during
// ``pnpm build``. If a var is unset at build time, the literal
// ``undefined`` ships in the bundle — and the fallback ``||`` pattern
// silently substitutes a hardcoded ``localhost``, which on a remote
// box (browser at a non-localhost host) points at the user's own
// loopback and fails with ERR_CONNECTION_REFUSED.
//
// We've been bitten by this three times in a row. So: fail loudly in
// production builds when any required NEXT_PUBLIC_* is missing. In
// dev (``NODE_ENV=development``, e.g. ``pnpm dev``) keep the
// localhost defaults so the local-first workflow doesn't need an
// .env.local. The deploy envelope for a remote host must set these vars explicitly. See
const isDev = process.env.NODE_ENV === 'development';

function required(name: string, value: string | undefined, devFallback: string): string {
  if (value && value.length > 0 && value !== 'undefined') return value;
  if (isDev) return devFallback;
  throw new Error(
    `${name} is not set. NEXT_PUBLIC_* vars must be baked at build time — ` +
    `set ${name} in the build environment (.env, docker compose) before running \`pnpm build\`.`
  );
}

function optional(value: string | undefined): string {
  // GitHub OAuth credentials are optional. The login UI hides the
  // GitHub button when this is empty, so an unset value is perfectly
  // valid for a deploy that doesn't wire up OAuth — no ``required``
  // throw needed.
  return value && value !== 'undefined' ? value : '';
}

export default {
    ws:       required('NEXT_PUBLIC_WS_URL',       process.env.NEXT_PUBLIC_WS_URL,       'ws://127.0.0.1:8000/ws'),
    backend:  required('NEXT_PUBLIC_BACKEND_URL',  process.env.NEXT_PUBLIC_BACKEND_URL,  'http://127.0.0.1:8000'),
    frontend: required('NEXT_PUBLIC_FRONTEND_URL', process.env.NEXT_PUBLIC_FRONTEND_URL, 'http://127.0.0.1:3000'),
    GITHUB_ID:     optional(process.env.NEXT_PUBLIC_GITHUB_ID),
    GITHUB_SECRET: optional(process.env.NEXT_PUBLIC_GITHUB_SECRET),
}
