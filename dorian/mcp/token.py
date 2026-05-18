"""
dorian.mcp.token — short-lived token binding an MCP client to a session.

Two independent code paths, deliberately separated so the MCP
subprocess never has to import ``backend.envs`` (which would start
Dask etc. and either hang or corrupt the stdio channel):

    backend (async):   issue_token, revoke_token  — use aioredis via backend.envs
    MCP process (sync): resolve_token_sync        — uses dorian.mcp._backend_min

Both speak the same Redis keys (``mcp:token:{token}`` → JSON
``{uid, session, issued_at}``, 1-hour TTL).
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

_TOKEN_TTL_S = 3600  # 1 hour
_KEY_PREFIX = "mcp:token:"


def _key(token: str) -> str:
    return f"{_KEY_PREFIX}{token}"


# ────────────────────────────────────────────────────────────────────────────
# Async (backend-side: called from handle_create_mcp_token)
# ────────────────────────────────────────────────────────────────────────────

async def issue_token(uid: str, session: str, *, ttl_seconds: int = _TOKEN_TTL_S) -> str:
    """Mint a new token bound to ``(uid, session)``. Returns the token."""
    from backend.envs import aioredis
    token = secrets.token_hex(16)
    payload = json.dumps({
        "uid": uid,
        "session": session,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    })
    await aioredis.set(_key(token), payload, ex=ttl_seconds)
    return token


async def revoke_token(token: str) -> bool:
    from backend.envs import aioredis
    return bool(await aioredis.delete(_key(token)))


# ────────────────────────────────────────────────────────────────────────────
# Sync (MCP-process-side: called from session tools)
# ────────────────────────────────────────────────────────────────────────────

def resolve_token_sync(token: str) -> tuple[str, str] | None:
    """Look up ``(uid, session)`` bound to ``token``. Sync.

    Uses the minimal MCP Redis client (``_backend_min``) so the MCP
    subprocess never pulls in Dask/Postgres/etc. from ``backend.envs``.
    """
    from dorian.mcp._backend_min import mcp_sync_redis
    raw = mcp_sync_redis().get(_key(token))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    uid = data.get("uid")
    session = data.get("session")
    if not uid or not session:
        return None
    return uid, session


class McpAuthError(Exception):
    """Raised by MCP tools when token resolution fails."""
