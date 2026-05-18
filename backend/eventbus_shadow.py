"""
backend/eventbus_shadow.py
--------------------------
Shadow forwarder: mirrors every ``aemit`` / ``aemit_bg`` / ``emit``
call into the Go-bus stream format on Redis.

Architecture (phase E rewrite, supersedes phase B's HTTP indirection):

    Python emit  →  Redis XADD  →  (shared Redis Streams)

The Go event-bus binary writes to exactly the same streams
(``events:user`` / ``events:bg``) with the same field layout. Python
bypasses the HTTP hop because it already has an aioredis client — the
HTTP indirection that phases B/D carried was paper over a redundancy,
and the drain-queue it required was the only real Python-side
blocker. Removing it collapses the producer path to a single Redis
round-trip per emit, and deletes the drain pool entirely.

The Go binary remains the entry point for NON-Python producers (the
Rust engine, future Go-gateway-originated events, curl-based tests)
and for the admin /stats surface.

Opt-in via env:

    DORIAN_EVENTBUS_SHADOW=1          # enable
    DORIAN_EVENTBUS_STREAM_USER=…     # default events:user
    DORIAN_EVENTBUS_STREAM_BG=…       # default events:bg
    DORIAN_EVENTBUS_STREAM_MAXLEN=…   # default 100000 (approx MAXLEN trim)

Contract:
  * Never blocks the caller.  XADD is fire-and-forget via
    ``asyncio.create_task`` — the await happens on the loop, not in the
    producer.  The task runs inline on the main loop so serialisation
    order matches emit order within a given producer.
  * Never raises into the caller.  Every exception is swallowed and
    counted.
  * No internal queue, no drain workers, no HTTP breaker.  Redis
    backpressure surfaces as a slow ``XADD`` call; a dead Redis shows
    up as timeout → counted in ``dropped_redis_error``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


_ENABLED: bool = _env_bool("DORIAN_EVENTBUS_SHADOW", default=False)

_STREAM_USER = os.environ.get("DORIAN_EVENTBUS_STREAM_USER", "events:user")
_STREAM_BG = os.environ.get("DORIAN_EVENTBUS_STREAM_BG", "events:bg")
_STREAM_MAXLEN = int(os.environ.get("DORIAN_EVENTBUS_STREAM_MAXLEN", "100000"))

# XADD timeout for one forward attempt. We're fire-and-forget on a
# dedicated task so this does NOT block the caller — we can afford a
# generous timeout to absorb aioredis pool contention under bursts
# without turning a normal slow-pool wait into a counted failure.
_XADD_TIMEOUT_S = float(os.environ.get("DORIAN_EVENTBUS_XADD_TIMEOUT_S", "2.0"))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class _Stats:
    enqueued: int = 0
    forwarded: int = 0
    # Kept for backwards-compat with the phase B/D stats shape; always 0
    # under direct-XADD because there's no drain queue to overflow.
    dropped_queue_full: int = 0
    dropped_breaker_open: int = 0
    # The real failure mode now: XADD failed (timeout / network / auth).
    dropped_http_error: int = 0   # legacy name; now counts XADD errors
    last_error: str = ""
    # The circuit-breaker state is vestigial; always False under direct
    # XADD. Kept so the observability dashboard doesn't need to change
    # shape in one release.
    breaker_open: bool = False
    consecutive_failures: int = 0


_stats: _Stats = _Stats()
_redis = None  # aioredis client, set by start()


def is_enabled() -> bool:
    return _ENABLED


def stats() -> dict[str, Any]:
    """Snapshot for /observability/event-bus."""
    return {
        "enabled": _ENABLED,
        "transport": "redis-xadd",
        "stream_user": _STREAM_USER,
        "stream_bg": _STREAM_BG,
        "stream_maxlen": _STREAM_MAXLEN,
        "enqueued": _stats.enqueued,
        "forwarded": _stats.forwarded,
        "dropped_queue_full": _stats.dropped_queue_full,
        "dropped_breaker_open": _stats.dropped_breaker_open,
        "dropped_http_error": _stats.dropped_http_error,   # now: XADD errors
        "breaker_open": _stats.breaker_open,
        "consecutive_failures": _stats.consecutive_failures,
        "last_error": _stats.last_error,
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start(redis_client=None) -> None:
    """Bind the Redis client. No-op when shadow mode is disabled.

    ``redis_client`` is the aioredis/redis.asyncio client used for XADD.
    When None, we import the backend's canonical client lazily.
    """
    global _redis
    if not _ENABLED:
        return
    if _redis is not None:
        return
    if redis_client is not None:
        _redis = redis_client
    else:
        try:
            from backend.envs import aioredis as _aio
            _redis = _aio
        except Exception as exc:
            _log.error("[eventbus-shadow] cannot acquire aioredis: %s", exc)
            return
    _log.info(
        "[eventbus-shadow] direct-XADD mode user=%s bg=%s maxlen=%d",
        _STREAM_USER, _STREAM_BG, _STREAM_MAXLEN,
    )


async def stop() -> None:
    """No persistent state to shut down under direct-XADD. Kept as a
    lifecycle hook for future transports."""
    global _redis
    _redis = None


# ---------------------------------------------------------------------------
# Producer — called from backend/events.py's emit / aemit / aemit_bg
# ---------------------------------------------------------------------------

def shadow_emit(event_type: str, payload: Any, *, lane: str,
                uid: str = "", session: str = "",
                request_id: str = "", ts: float = 0.0) -> None:
    """Fire-and-forget XADD of one event into the Go-bus stream.

    No-op when shadow mode is disabled. Never blocks, never raises.
    Schedules the XADD as an asyncio task on the current loop — returns
    immediately to the producer. Errors are counted; a broken Redis
    surfaces as ``dropped_http_error`` climbing (the field is named for
    backwards compat with the HTTP-era dashboards).
    """
    if not _ENABLED:
        return

    _stats.enqueued += 1
    stream = _STREAM_USER if lane == "user" else _STREAM_BG
    ts_eff = ts or time.time()

    # Build the stream field dict once per emit. Keys + values are
    # strings; payload round-trips as a JSON blob so the subscriber's
    # decoder matches the Go-bus-produced entries verbatim.
    try:
        payload_str = json.dumps(payload, default=_json_default)
    except (TypeError, ValueError) as exc:
        _stats.dropped_http_error += 1
        _stats.last_error = f"payload-encode: {exc}"
        return

    fields = {"type": event_type}
    if uid:
        fields["uid"] = uid
    if session:
        fields["session"] = session
    if request_id:
        fields["request_id"] = request_id
    if ts_eff:
        fields["ts"] = repr(ts_eff)
    fields["payload"] = payload_str

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # No running loop (rare — called from a non-async context that
        # hasn't set one up). Skip silently; emit()/aemit() themselves
        # funnel sync calls through the main loop.
        _stats.dropped_http_error += 1
        _stats.last_error = "no running loop"
        return

    loop.create_task(_xadd(stream, fields))


async def _xadd(stream: str, fields: dict[str, str]) -> None:
    """Perform the XADD with a short timeout. Counts success/failure."""
    if _redis is None:
        _stats.dropped_http_error += 1
        _stats.last_error = "redis not started"
        return
    try:
        await asyncio.wait_for(
            _redis.xadd(stream, fields, maxlen=_STREAM_MAXLEN, approximate=True),
            timeout=_XADD_TIMEOUT_S,
        )
        _stats.forwarded += 1
        _stats.consecutive_failures = 0
    except asyncio.TimeoutError:
        _stats.dropped_http_error += 1
        _stats.consecutive_failures += 1
        _stats.last_error = "xadd timeout"
    except Exception as exc:
        _stats.dropped_http_error += 1
        _stats.consecutive_failures += 1
        _stats.last_error = f"xadd: {exc}"


def _json_default(o: Any) -> Any:
    """Fallback encoder for non-JSON-native types appearing in payloads."""
    try:
        import datetime
        if isinstance(o, (datetime.datetime, datetime.date)):
            return o.isoformat()
    except Exception:
        pass
    return str(o)
