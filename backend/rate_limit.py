"""
backend/rate_limit.py
---------------------
HTTP rate limiting via FastAPI Depends + Redis fixed-window counters.

Intentionally does NOT use slowapi — slowapi 0.1.9 has a bug where it
accesses request.state.view_rate_limit even when headers_enabled=False,
causing an AttributeError on Python 3.13 / newer Starlette.

Usage in routes:
    from backend.rate_limit import http_rate_limit, HTTP_LIMITS

    @router.post("/upload")
    async def upload_data(
        file: UploadFile = File(...),
        _rl = http_rate_limit("upload"),   # ← Depends injected automatically
    ):
        ...

No `request: Request` parameter needed in the route — the dependency
captures the request internally via FastAPI's DI.
"""
from __future__ import annotations

import logging
import re

from fastapi import Depends, HTTPException, Request

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helper
# ---------------------------------------------------------------------------

def _cfg(path: str, default):
    try:
        from backend.config import config
        obj = config
        for part in path.split("."):
            obj = getattr(obj, part)
        return obj
    except (AttributeError, KeyError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Limit string parser  "10/minute" → (10, 60)
# ---------------------------------------------------------------------------

_LIMIT_RE = re.compile(r"(\d+)\s*/\s*(second|minute|hour|day)", re.IGNORECASE)
_WINDOW_SECS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def _parse(limit_str: str) -> tuple[int, int]:
    m = _LIMIT_RE.match(limit_str.strip())
    if not m:
        _log.warning("[rate_limit] bad limit string %r — defaulting to 60/minute", limit_str)
        return 60, 60
    return int(m.group(1)), _WINDOW_SECS[m.group(2).lower()]


# ---------------------------------------------------------------------------
# Identity key  uid param → IP fallback
# ---------------------------------------------------------------------------

def _identity(request: Request) -> str:
    uid = (
        request.query_params.get("uid")
        or request.query_params.get("user_id")
    )
    if uid:
        return uid
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Per-endpoint limit strings (read from config, sensible defaults)
# ---------------------------------------------------------------------------

HTTP_LIMITS: dict[str, str] = {
    "upload":           _cfg("rate_limit.http.upload",           "10/minute"),
    "session_create":   _cfg("rate_limit.http.session_create",   "20/minute"),
    "session_read":     _cfg("rate_limit.http.session_read",     "60/minute"),
    "session_write":    _cfg("rate_limit.http.session_write",    "30/minute"),
    "datasets":         _cfg("rate_limit.http.datasets",         "30/minute"),
    "extraction_rules": _cfg("rate_limit.http.extraction_rules", "60/minute"),
    "observability":    _cfg("rate_limit.http.observability",    "120/minute"),
    "contact":          _cfg("rate_limit.http.contact",          "5/minute"),
    "vault":            _cfg("rate_limit.http.vault",            "20/minute"),
    "catalog":          _cfg("rate_limit.http.catalog",          "60/minute"),
}


# ---------------------------------------------------------------------------
# Dependency factory
# ---------------------------------------------------------------------------

def http_rate_limit(limit_key: str):
    """
    Return a FastAPI Depends that enforces a Redis fixed-window rate limit.

    Raises HTTP 429 with Retry-After + X-RateLimit-* headers when exceeded.
    Swallows Redis errors silently so a Redis outage never blocks requests.
    """
    limit_str = HTTP_LIMITS.get(limit_key, "60/minute")
    max_count, window = _parse(limit_str)

    async def _check(request: Request) -> None:
        try:
            from backend.envs import aioredis
            identity = _identity(request)
            key = f"rl:http:{identity}:{limit_key}"

            count = await aioredis.incr(key)
            if count == 1:
                await aioredis.expire(key, window)

            if count > max_count:
                ttl = await aioredis.ttl(key)
                retry_after = max(int(ttl), 1)
                _log.warning(
                    "[rate_limit] 429 identity=%s endpoint=%s count=%d limit=%s",
                    identity, limit_key, count, limit_str,
                )
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "rate_limit_exceeded",
                        "detail": limit_str,
                        "retryAfter": retry_after,
                    },
                    headers={
                        "Retry-After":           str(retry_after),
                        "X-RateLimit-Limit":     limit_str,
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset":     str(retry_after),
                    },
                )
        except HTTPException:
            raise
        except Exception as exc:
            # Redis down / network error — let the request through
            _log.warning("[rate_limit] Redis error, skipping limit: %r", exc)

    return Depends(_check)
