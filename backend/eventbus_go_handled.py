"""
backend/eventbus_go_handled.py
------------------------------
Redis-backed set of event types owned by a Go handler.

When a type is in this set:
  * Python's in-process subscriber SKIPS dispatch for it (the Go
    subscriber runs the handler instead).
  * Python emits still happen normally — the event lands on the same
    Redis streams both subscribers read from.
  * Python emit-side local dispatch is orthogonal (controlled by the
    ``eventbus_authoritative`` module); for Go-handled types it is
    typically also set to authoritative so local dispatch is skipped.

Cache + refresh pattern mirrors backend/eventbus_authoritative.py so
the hot-path check is a single dict lookup with no Redis round-trip.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)

_REDIS_KEY: str = os.environ.get("DORIAN_EVENTBUS_GO_HANDLED_KEY", "eventbus:go_handled")
_REFRESH_INTERVAL_S: float = float(
    os.environ.get("DORIAN_EVENTBUS_GO_HANDLED_REFRESH_S", "1.0")
)

_snapshot: frozenset[str] = frozenset()
_snapshot_loaded_at: float = 0.0
_refresh_task: Optional[asyncio.Task] = None
_redis = None


def is_go_handled(event_type: str) -> bool:
    """Hot-path check — single dict lookup, no Redis call."""
    return event_type in _snapshot


def snapshot() -> list[str]:
    """Sorted list of types currently marked Go-handled."""
    return sorted(_snapshot)


def snapshot_meta() -> dict[str, Any]:
    return {
        "size": len(_snapshot),
        "loaded_at": _snapshot_loaded_at,
        "age_s": max(0.0, time.time() - _snapshot_loaded_at) if _snapshot_loaded_at else None,
        "refresh_interval_s": _REFRESH_INTERVAL_S,
        "redis_key": _REDIS_KEY,
    }


async def add(event_type: str) -> None:
    if _redis is None:
        raise RuntimeError("eventbus_go_handled not started")
    await _redis.sadd(_REDIS_KEY, event_type)
    await _refresh_once()


async def remove(event_type: str) -> None:
    if _redis is None:
        raise RuntimeError("eventbus_go_handled not started")
    await _redis.srem(_REDIS_KEY, event_type)
    await _refresh_once()


async def start(redis_client) -> None:
    global _redis, _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    _redis = redis_client
    await _refresh_once()
    _refresh_task = asyncio.create_task(
        _refresh_loop(), name="eventbus-go-handled-refresh",
    )
    _log.info(
        "[eventbus-go-handled] refresher started key=%s interval=%.1fs initial=%d",
        _REDIS_KEY, _REFRESH_INTERVAL_S, len(_snapshot),
    )


async def stop() -> None:
    global _refresh_task
    if _refresh_task is None:
        return
    _refresh_task.cancel()
    try:
        await asyncio.wait_for(_refresh_task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    _refresh_task = None


async def _refresh_once() -> None:
    global _snapshot, _snapshot_loaded_at
    if _redis is None:
        return
    try:
        members = await _redis.smembers(_REDIS_KEY)
        decoded = frozenset(
            m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
            for m in members
        )
        _snapshot = decoded
        _snapshot_loaded_at = time.time()
    except Exception as exc:  # pragma: no cover
        _log.warning("[eventbus-go-handled] refresh failed, keeping prior snapshot: %s", exc)


async def _refresh_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_REFRESH_INTERVAL_S)
        except asyncio.CancelledError:
            return
        await _refresh_once()
