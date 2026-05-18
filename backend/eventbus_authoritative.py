"""
backend/eventbus_authoritative.py
---------------------------------
Redis-backed allow-list of event types whose authoritative dispatch path
is the Go event bus (phase C of the migration).

Semantics:

* An event type X is "authoritative in Go" iff its name is a member of
  the Redis SET ``eventbus:authoritative``.
* On emit: if X is authoritative → the Python bus SKIPS local enqueue
  and relies on the Go bus + in-process subscriber to dispatch handlers.
  Shadow forwarding is still performed (that's how the event reaches the
  Go bus in the first place).
* If X is NOT authoritative → Python keeps its current local dispatch
  path unchanged; shadow is still additive.

Operational goal: flip individual event types' transport without a
redeploy, so we can cut over high-volume-low-risk types
(``NodeObservability``, ``ProcessSample``) first and watch them before
moving on to the next batch.

Design:

* The hot emit path checks ``is_authoritative(event_type)`` on every
  event — it MUST be fast. We keep a local dict snapshot, refreshed
  every ``_REFRESH_INTERVAL_S`` seconds by a background task.
* Zero Redis round-trips on the hot path; the cache is read with a
  single dict lookup.
* Writes (add/remove) are rare admin actions; they update Redis and
  immediately invalidate the local cache so observers see changes
  within ~100ms in the worst case.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)


# Redis SET where membership marks "this event type is authoritative in Go."
_REDIS_KEY: str = os.environ.get("DORIAN_EVENTBUS_AUTH_KEY", "eventbus:authoritative")

# How often the local snapshot refreshes from Redis. Short enough that
# admin changes propagate within a second; long enough that Redis load
# is negligible.
_REFRESH_INTERVAL_S: float = float(
    os.environ.get("DORIAN_EVENTBUS_AUTH_REFRESH_S", "1.0")
)
_DEFAULTS: frozenset[str] = frozenset(
    item.strip()
    for item in os.environ.get("DORIAN_EVENTBUS_AUTHORITATIVE_DEFAULTS", "").split(",")
    if item.strip()
)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_snapshot: frozenset[str] = frozenset()
_snapshot_loaded_at: float = 0.0
_refresh_task: Optional[asyncio.Task] = None
_redis = None  # set by start()


def is_authoritative(event_type: str) -> bool:
    """Hot-path check: is this event type currently routed through Go?

    Read from the local snapshot — no Redis call. The snapshot may be up
    to ``_REFRESH_INTERVAL_S`` seconds stale; that's acceptable for a
    migration toggle.
    """
    return event_type in _snapshot


def snapshot() -> list[str]:
    """Return a sorted copy of the current authoritative set."""
    return sorted(_snapshot)


def snapshot_meta() -> dict[str, Any]:
    """Diagnostic metadata for observability."""
    return {
        "size": len(_snapshot),
        "loaded_at": _snapshot_loaded_at,
        "age_s": max(0.0, time.time() - _snapshot_loaded_at) if _snapshot_loaded_at else None,
        "refresh_interval_s": _REFRESH_INTERVAL_S,
        "redis_key": _REDIS_KEY,
    }


# ---------------------------------------------------------------------------
# Admin mutations
# ---------------------------------------------------------------------------

async def add(event_type: str) -> None:
    """Mark ``event_type`` as authoritative in Go. Idempotent."""
    if _redis is None:
        raise RuntimeError("eventbus_authoritative not started")
    await _redis.sadd(_REDIS_KEY, event_type)
    await _refresh_once()


async def remove(event_type: str) -> None:
    """Unmark ``event_type`` — local Python dispatch resumes for it."""
    if _redis is None:
        raise RuntimeError("eventbus_authoritative not started")
    await _redis.srem(_REDIS_KEY, event_type)
    await _refresh_once()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start(redis_client) -> None:
    """Initialise the snapshot + start the background refresher.

    Must be called during app startup, AFTER aioredis is ready. Safe to
    call multiple times — subsequent calls are a no-op.
    """
    global _redis, _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    _redis = redis_client
    if _DEFAULTS:
        await _redis.sadd(_REDIS_KEY, *_DEFAULTS)
    await _refresh_once()
    _refresh_task = asyncio.create_task(
        _refresh_loop(), name="eventbus-authoritative-refresh"
    )
    _log.info(
        "[eventbus-auth] refresher started key=%s interval=%.1fs initial=%d",
        _REDIS_KEY, _REFRESH_INTERVAL_S, len(_snapshot),
    )


async def stop() -> None:
    """Cancel the refresher. Safe to call when not started."""
    global _refresh_task
    if _refresh_task is None:
        return
    _refresh_task.cancel()
    try:
        await asyncio.wait_for(_refresh_task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    _refresh_task = None


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

async def _refresh_once() -> None:
    """Re-read the set from Redis into the local snapshot.

    Swallows any Redis error — a transient failure keeps the previous
    snapshot (fail-open: events keep flowing through their previous
    route rather than disappearing). Logs at warning level so we notice.
    """
    global _snapshot, _snapshot_loaded_at
    if _redis is None:
        return
    try:
        members = await _redis.smembers(_REDIS_KEY)
        # aioredis returns bytes by default; normalise to str.
        decoded = frozenset(
            m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
            for m in members
        )
        _snapshot = decoded
        _snapshot_loaded_at = time.time()
    except Exception as exc:  # pragma: no cover — exercised via breakage
        _log.warning("[eventbus-auth] refresh failed, keeping prior snapshot: %s", exc)


async def _refresh_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_REFRESH_INTERVAL_S)
        except asyncio.CancelledError:
            return
        await _refresh_once()
