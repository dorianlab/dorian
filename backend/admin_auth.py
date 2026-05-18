"""
backend/admin_auth.py
---------------------
FastAPI dependency that gates admin-mutation endpoints behind a
shared-secret bearer token PLUS an audit-trail username.

Motivation — the earlier scheme (``?username=<login>``) was trivially
bypassable: anyone observing a URL (access logs, proxy, browser
history, network trace) could replay the admin request from their own
machine. A bearer token carried in an HTTP header, compared with
``hmac.compare_digest``, closes that hole. It's not a substitute for
per-request HMAC signing (``backend/hmac_auth.py``), which handles
replay prevention + payload integrity across the whole API surface —
this is a narrowly-scoped belt for admin routes that works even when
the global HMAC middleware is disabled (e.g. local dev).

Configuration resolution order (first non-empty wins):

    1. ``DORIAN_ADMIN_TOKEN``      — env var (preferred in prod / compose)
    2. ``config.admin.token``      — config.yaml fallback

If no token is configured, the dependency refuses ALL admin mutations
with HTTP 503 — fail-closed by design.

Usage:

    from fastapi import Depends
    from backend.admin_auth import require_admin

    @router.post("/admin/do-thing")
    async def do_thing(caller: str = Depends(require_admin)):
        ...  # caller holds the audit-trail username

Clients must send:

    X-Admin-Token:    <shared secret>
    X-Admin-Username: <github login, case-sensitive, in config.admin.usernames>

``_assert_admin`` (the plain username check used by read-only admin
endpoints) is unchanged; public reads stay ungated.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import Header, HTTPException, status

from backend.config import config

_log = logging.getLogger(__name__)


def _configured_token() -> Optional[str]:
    """Resolve the shared admin token. Empty string → not configured."""
    env_tok = os.environ.get("DORIAN_ADMIN_TOKEN", "").strip()
    if env_tok:
        return env_tok
    try:
        cfg_tok = str(getattr(config.admin, "token", "") or "").strip()
        return cfg_tok or None
    except (AttributeError, KeyError):
        return None


def _admin_usernames() -> list[str]:
    try:
        return list(config.admin.usernames)
    except (AttributeError, KeyError):
        return []


def _is_admin_username(username: str | None) -> bool:
    if not username:
        return False
    stripped = username.strip()
    if not stripped:
        return False
    # Demo/sandbox identifiers are never admin regardless of membership.
    low = stripped.lower()
    if low == "demo" or low.startswith("demo-"):
        return False
    return stripped in _admin_usernames()


def require_admin(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    x_admin_username: str | None = Header(default=None, alias="X-Admin-Username"),
) -> str:
    """FastAPI dependency: verify the admin token + audit username.

    Returns the validated username (string) so handlers can log it.
    Raises:
        * 503 — server has no admin token configured (fail-closed).
        * 401 — token missing or mismatch.
        * 403 — username missing or not in admin allow-list.
    """
    configured = _configured_token()
    if not configured:
        _log.warning(
            "[admin-auth] refused admin mutation — DORIAN_ADMIN_TOKEN not set"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin endpoint disabled: server has no admin token configured",
        )

    if not x_admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Admin-Token header",
        )

    # Constant-time comparison — avoids timing side-channel on token length.
    if not hmac.compare_digest(x_admin_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin token",
        )

    if not _is_admin_username(x_admin_username):
        # 403 (not 401) — token was valid, identity was not.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="X-Admin-Username missing or not in admin allow-list",
        )

    return (x_admin_username or "").strip()
