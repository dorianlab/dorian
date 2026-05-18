"""
backend/ws_rate_limit.py
------------------------
WebSocket event rate limiting using Redis fixed-window counters.

Each inbound WS event is checked before it reaches the event bus.

Key format : rl:ws:{uid}:{event_name}
TTL        : window_seconds (default 60)
Algorithm  : fixed window  — one INCR + conditional EXPIRE per event

Why fixed window?
  Simple, O(1) Redis ops, one key per (uid, event_type).
  The 2× burst edge case (across a window boundary) is acceptable for
  this workload — expensive events (LLM, Dask) have small enough limits
  that even 2× is not dangerous.

Connection-level limit (new WS handshakes per IP):
  Key format : rl:ws:conn:{ip}
  Checked in websocket_endpoint before websocket.accept().
"""
from __future__ import annotations

import logging

from backend.config import config

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _cfg(path: str, default):
    try:
        obj = config
        for part in path.split("."):
            obj = getattr(obj, part)
        return obj
    except (AttributeError, KeyError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Per-event limits and window
# ---------------------------------------------------------------------------

_WINDOW: int = int(_cfg("rate_limit.ws.window_seconds", 60))

_LIMITS: dict[str, int] = {
    "SuggestExtractionRules": int(_cfg("rate_limit.ws.SuggestExtractionRules", 3)),
    "ExtractPipeline":        int(_cfg("rate_limit.ws.ExtractPipeline",        10)),
    "RunPipeline":            int(_cfg("rate_limit.ws.RunPipeline",            5)),
    "SaveExtractionRules":    int(_cfg("rate_limit.ws.SaveExtractionRules",    20)),
    "AcceptExtractionRule":   int(_cfg("rate_limit.ws.AcceptExtractionRule",   30)),
    "RejectExtractionRule":   int(_cfg("rate_limit.ws.RejectExtractionRule",   30)),
}

_DEFAULT_LIMIT: int = int(_cfg("rate_limit.ws.default", 60))

# Max new WS connections per minute per IP
_MAX_CONNECTIONS: int = int(_cfg("rate_limit.connection.max_per_minute", 120))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def check_ws_event(
    redis,
    uid: str,
    event_name: str,
) -> tuple[bool, int, int]:
    """
    Check whether uid may send event_name right now.

    Returns:
        (allowed, current_count, limit)

    Side-effects:
        Increments the Redis counter; sets TTL on the first hit.
    """
    limit = _LIMITS.get(event_name, _DEFAULT_LIMIT)
    key = f"rl:ws:{uid}:{event_name}"

    count = await redis.incr(key)
    if count == 1:
        # First request in this window — arm the expiry
        await redis.expire(key, _WINDOW)

    allowed = count <= limit

    if not allowed:
        _log.warning(
            "[ws_rate_limit] BLOCKED uid=%s event=%s count=%d limit=%d",
            uid, event_name, count, limit,
        )

    return allowed, count, limit


async def ttl_for(redis, uid: str, event_name: str) -> int:
    """Seconds remaining in the current window for this uid+event."""
    ttl = await redis.ttl(f"rl:ws:{uid}:{event_name}")
    return max(int(ttl), 1)


async def check_ws_connection(redis, client_ip: str) -> bool:
    """
    Connection-level gate: allow at most _MAX_CONNECTIONS new WS
    handshakes per minute from a single IP.

    Returns True if the connection should be allowed.
    """
    key = f"rl:ws:conn:{client_ip}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)

    allowed = count <= _MAX_CONNECTIONS
    if not allowed:
        _log.warning(
            "[ws_rate_limit] CONNECTION BLOCKED ip=%s count=%d limit=%d",
            client_ip, count, _MAX_CONNECTIONS,
        )
    return allowed
