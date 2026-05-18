"""
backend/eventbus_subscriber.py
------------------------------
In-process consumer that reads events from the Go event bus's Redis
Streams and dispatches them through the local Python handler registry.

Phase C: introduced; one consume loop per lane, serial dispatch.
Phase D: per-event-type worker pools. The consume loop no longer runs
handlers inline — it routes decoded events to a per-type bounded queue
drained by a dedicated worker pool. A slow handler for type X can no
longer stall events of types Y, Z, … on the same lane.

When an event type is marked authoritative (see eventbus_authoritative),
Python ``aemit`` skips local enqueue — so the event reaches handlers
ONLY via this subscriber. For non-authoritative types the subscriber
still consumes them (shadow confirmation), but dispatch is deduplicated:
local Python already dispatched, so the subscriber short-circuits to
XACK without re-running handlers.

The subscriber uses Redis Streams consumer groups, one per lane
(``user`` and ``bg``). Consumer name is derived from host+pid so
multiple backend replicas on the same Redis share the load.

Per-type worker sizing:
  - Default: ``DORIAN_EVENTBUS_SUB_WORKERS_PER_TYPE`` (default 1) —
    serial per type, parallel across types. Matches the previous
    single-loop semantics for any ONE event type.
  - Per-type override via env: ``DORIAN_EVENTBUS_SUB_WORKERS_<TYPE>``
    (e.g. ``DORIAN_EVENTBUS_SUB_WORKERS_NodeObservability=4``) —
    bump concurrency for types with slow or I/O-heavy handlers.
  - Per-type queue cap: ``DORIAN_EVENTBUS_SUB_QCAP_PER_TYPE`` (default
    256). Backpressure is NOT propagated to Redis — the consume loop
    simply awaits queue.put, which means Redis reads slow down when a
    type is saturated. Redis itself continues to hold unread entries.

Cross-process competing-consumer correctness under partial failure
(XPENDING replay + deadletter) remains out of scope — handlers are
idempotent by construction.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_STREAM_USER = os.environ.get("DORIAN_EVENTBUS_STREAM_USER", "events:user")
_STREAM_BG = os.environ.get("DORIAN_EVENTBUS_STREAM_BG", "events:bg")
_GROUP = os.environ.get("DORIAN_EVENTBUS_GROUP", "python-backend")
_BLOCK_MS = int(os.environ.get("DORIAN_EVENTBUS_BLOCK_MS", "5000"))
_BATCH = int(os.environ.get("DORIAN_EVENTBUS_BATCH", "32"))

# Per-type worker pool tuning. Phase E upgrades the static pool (one
# fixed worker count per type) to an elastic ElasticPool that starts at
# ``min`` and grows up to ``max`` under queue pressure. Default min=1
# matches the phase D behaviour for deploys that don't override.
_DEFAULT_MIN_WORKERS = int(os.environ.get("DORIAN_EVENTBUS_SUB_MIN_WORKERS", "1"))
_DEFAULT_MAX_WORKERS = int(os.environ.get("DORIAN_EVENTBUS_SUB_MAX_WORKERS", "8"))
_DEFAULT_QCAP_PER_TYPE = int(
    os.environ.get("DORIAN_EVENTBUS_SUB_QCAP_PER_TYPE", "256")
)
# Queue depth that triggers a scale-up. Small enough that a bursty
# event type grows workers quickly; large enough to avoid flapping on a
# single spike.
_DEFAULT_SCALE_UP_THRESHOLD = int(
    os.environ.get("DORIAN_EVENTBUS_SUB_SCALE_UP_AT", "32")
)


def _workers_for_type(event_type: str) -> int:
    """Resolve the MIN worker count for a type.

    Retained for backwards compatibility with phase C/D env overrides:
      DORIAN_EVENTBUS_SUB_WORKERS_<TYPE>=N forces BOTH min and max to N
      (behaves like a static pool — use when you want predictable sizing).
      Otherwise: DORIAN_EVENTBUS_SUB_MIN_WORKERS (default 1).
    """
    key = f"DORIAN_EVENTBUS_SUB_WORKERS_{event_type}"
    v = os.environ.get(key, "").strip()
    if v:
        try:
            n = int(v)
            if n > 0:
                return n
        except ValueError:
            pass
    return _DEFAULT_MIN_WORKERS


def _bounds_for_type(event_type: str) -> tuple[int, int]:
    """Return (min, max) worker counts for this type.

    A per-type override via DORIAN_EVENTBUS_SUB_WORKERS_<TYPE> pins both
    min and max to the same value (static pool). Otherwise use the
    global min/max. Max is always >= min.
    """
    override_key = f"DORIAN_EVENTBUS_SUB_WORKERS_{event_type}"
    v = os.environ.get(override_key, "").strip()
    if v:
        try:
            n = int(v)
            if n > 0:
                return n, n
        except ValueError:
            pass
    lo = _DEFAULT_MIN_WORKERS
    hi = max(_DEFAULT_MAX_WORKERS, lo)
    return lo, hi


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


_ENABLED: bool = _env_bool("DORIAN_EVENTBUS_SUBSCRIBER", default=False)


def is_enabled() -> bool:
    return _ENABLED


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class _Stats:
    received: int = 0
    dispatched: int = 0
    # Short-circuited because event type is NOT authoritative — local
    # Python already dispatched so re-running here would duplicate.
    dedup_skipped: int = 0
    decode_errors: int = 0
    handler_errors: int = 0
    last_error: str = ""
    last_id_user: str = ""
    last_id_bg: str = ""


@dataclass
class _TypeStats:
    """Per-event-type subscriber metrics."""
    received: int = 0           # count arriving from stream
    dispatched: int = 0         # count of handler invocations completed
    dedup_skipped: int = 0      # non-authoritative, short-circuited
    handler_errors: int = 0     # handler raised during dispatch
    queued_at_peak: int = 0     # high-water mark of the per-type queue
    last_error: str = ""


_stats = _Stats()
_type_stats: dict[str, _TypeStats] = {}

_tasks: list[asyncio.Task] = []
_redis = None
_consumer_name: str = ""
# Set at ``start()`` — used to compute since-start rates in ``stats()``.
_start_ts: float = 0.0

# Per-type worker pools — populated lazily on first sight of each type.
# Phase E: each type's pool is an ElasticPool that can grow up to its
# configured max under queue pressure. ``_type_workers`` is retained
# for test-introspection compatibility; it mirrors the pool's internal
# worker list.
_type_queues: dict[str, asyncio.Queue] = {}
_type_pools: dict[str, Any] = {}
_type_workers: dict[str, list[asyncio.Task]] = {}


def stats() -> dict[str, Any]:
    by_type: dict[str, dict[str, Any]] = {}
    for t, ts in _type_stats.items():
        q = _type_queues.get(t)
        pool = _type_pools.get(t)
        pool_stats = pool.stats() if pool is not None else {}
        by_type[t] = {
            "received": ts.received,
            "dispatched": ts.dispatched,
            "dedup_skipped": ts.dedup_skipped,
            "handler_errors": ts.handler_errors,
            "queued": q.qsize() if q is not None else 0,
            "queued_at_peak": ts.queued_at_peak,
            "workers": pool_stats.get("workers", 0),
            "workers_min": pool_stats.get("workers_min", 0),
            "workers_max": pool_stats.get("workers_max", 0),
            "workers_peak": pool_stats.get("workers_peak", 0),
            "scale_ups": pool_stats.get("scale_ups", 0),
            "scale_downs": pool_stats.get("scale_downs", 0),
            "last_error": ts.last_error,
        }
    uptime_s = max(0.001, time.time() - _start_ts) if _start_ts else 0.0
    def _rate(v: int) -> float:
        return round(v / uptime_s, 2) if uptime_s else 0.0

    return {
        "enabled": _ENABLED,
        "group": _GROUP,
        "consumer": _consumer_name,
        "uptime_s": round(uptime_s, 2),
        "received": _stats.received,
        "received_rate": _rate(_stats.received),
        "dispatched": _stats.dispatched,
        "dispatched_rate": _rate(_stats.dispatched),
        "dedup_skipped": _stats.dedup_skipped,
        "decode_errors": _stats.decode_errors,
        "handler_errors": _stats.handler_errors,
        "handler_errors_rate": _rate(_stats.handler_errors),
        "last_error": _stats.last_error,
        "last_id_user": _stats.last_id_user,
        "last_id_bg": _stats.last_id_bg,
        "by_type": by_type,
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start(redis_client) -> None:
    """Start one consumer per stream lane. No-op when disabled."""
    global _redis, _consumer_name, _tasks, _start_ts

    if not _ENABLED:
        return
    if _tasks:
        return
    _start_ts = time.time()

    _redis = redis_client
    _consumer_name = f"{socket.gethostname()}-{os.getpid()}"

    # Create consumer groups (idempotent — BUSYGROUP errors are ignored).
    for stream in (_STREAM_USER, _STREAM_BG):
        try:
            await _redis.xgroup_create(stream, _GROUP, id="0", mkstream=True)
        except Exception as exc:  # pragma: no cover — relies on Redis behaviour
            # Redis replies BUSYGROUP when the group already exists.
            if "BUSYGROUP" not in str(exc):
                _log.warning("[eventbus-sub] xgroup_create(%s) failed: %s", stream, exc)

    _tasks = [
        asyncio.create_task(_consume_loop(_STREAM_USER), name="eventbus-sub-user"),
        asyncio.create_task(_consume_loop(_STREAM_BG), name="eventbus-sub-bg"),
    ]
    _log.info(
        "[eventbus-sub] started group=%s consumer=%s streams=[%s,%s]",
        _GROUP, _consumer_name, _STREAM_USER, _STREAM_BG,
    )


async def stop() -> None:
    """Cancel consumer tasks + all per-type worker pools. Safe when not started."""
    global _tasks, _type_workers, _type_queues, _type_pools
    if not _tasks and not _type_pools:
        return
    # Cancel consume loops first so no new events get routed.
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks = []
    # Stop per-type elastic pools (each internally cancels its workers +
    # its autoscaler).
    for pool in list(_type_pools.values()):
        await pool.stop()
    _type_pools.clear()
    _type_workers.clear()
    _type_queues.clear()


# ---------------------------------------------------------------------------
# Consume loop
# ---------------------------------------------------------------------------

async def _consume_loop(stream: str) -> None:
    """XREADGROUP → dispatch → XACK in a loop. Cancels cleanly."""
    assert _redis is not None
    while True:
        try:
            resp = await _redis.xreadgroup(
                groupname=_GROUP,
                consumername=_consumer_name,
                streams={stream: ">"},
                count=_BATCH,
                block=_BLOCK_MS,
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _stats.last_error = f"XREADGROUP: {exc}"
            _log.warning("[eventbus-sub] %s read failed: %s (backing off 1s)", stream, exc)
            await asyncio.sleep(1.0)
            continue

        if not resp:
            # Block timeout with no new entries — loop around.
            continue

        # Response shape: [(stream_name, [(id, {field: value, ...}), ...])]
        for _stream_name, entries in resp:
            for entry_id, fields in entries:
                await _handle_entry(stream, entry_id, fields)


async def _handle_entry(stream: str, entry_id: Any, fields: Any) -> None:
    """Decode one stream entry and enqueue it to its per-type worker pool.

    The consume loop MUST stay fast — its only job is to feed the
    per-type queues. All actual dispatch happens in ``_type_worker``,
    which does the authoritativeness check, calls handlers, and XACKs.

    Malformed records and records we decide to short-circuit (dedup
    skip) are XACKed inline here; they never enter a type queue.
    """
    global _stats

    _stats.received += 1
    entry_id_str = _maybe_str(entry_id)
    if stream == _STREAM_USER:
        _stats.last_id_user = entry_id_str
    else:
        _stats.last_id_bg = entry_id_str

    event_type, event_obj = _decode_fields(fields)
    if event_obj is None:
        _stats.decode_errors += 1
        await _safe_xack(stream, entry_id)
        return

    # Per-type counters are initialised lazily — the first event of a
    # type creates its stats row, its queue, and its worker pool.
    ts = _type_stats.setdefault(event_type, _TypeStats())
    ts.received += 1

    # Check authoritativeness up front so non-auth events never touch a
    # per-type queue — keeps queue depths meaningful for cutover telemetry.
    try:
        from backend.eventbus_authoritative import is_authoritative
        from backend.eventbus_go_handled import is_go_handled
    except Exception as exc:  # pragma: no cover
        _stats.handler_errors += 1
        _stats.last_error = f"import: {exc}"
        await _safe_xack(stream, entry_id)
        return

    # If Go owns the handler, Python must NOT dispatch. The Go-side
    # subscriber (gateway/internal/eventbus/subscriber) will pull the
    # same entry under its own consumer group and run the handler.
    if is_go_handled(event_type):
        _stats.dedup_skipped += 1
        ts.dedup_skipped += 1
        await _safe_xack(stream, entry_id)
        return

    if not is_authoritative(event_type):
        _stats.dedup_skipped += 1
        ts.dedup_skipped += 1
        await _safe_xack(stream, entry_id)
        return

    # Lazily create the per-type elastic pool the first time we see
    # this type. ``min_workers`` defaults to 1 (serial per-type), and
    # the pool grows on its own up to ``max_workers`` under queue
    # pressure (see backend/eventbus_elastic.ElasticPool).
    q = _type_queues.get(event_type)
    if q is None:
        from backend.eventbus_elastic import ElasticPool

        q = asyncio.Queue(maxsize=_DEFAULT_QCAP_PER_TYPE)
        _type_queues[event_type] = q
        lo, hi = _bounds_for_type(event_type)

        async def _worker_adapter(pool, idx, *, _t=event_type, _q=q):
            # The ElasticPool worker contract is ``(pool, idx)``; our
            # existing ``_type_worker`` takes ``(event_type, queue)``.
            # Close over the type/queue captured at spawn time.
            await _type_worker(_t, _q)

        pool = ElasticPool(
            name=f"sub/{event_type}",
            queue=q,
            worker_fn=_worker_adapter,
            min_workers=lo,
            max_workers=hi,
            scale_up_threshold=_DEFAULT_SCALE_UP_THRESHOLD,
        )
        await pool.start()
        _type_pools[event_type] = pool
        _type_workers[event_type] = list(pool._workers)
        _log.info(
            "[eventbus-sub] spawned elastic pool type=%s min=%d max=%d qcap=%d",
            event_type, lo, hi, _DEFAULT_QCAP_PER_TYPE,
        )

    # Back-pressure into the consume loop: ``put`` blocks when the queue
    # is full, which slows down XREADGROUP and keeps entries in Redis
    # until the pool catches up. Preferable to silently dropping.
    await q.put((stream, entry_id, event_obj))
    depth = q.qsize()
    if depth > ts.queued_at_peak:
        ts.queued_at_peak = depth


async def _type_worker(event_type: str, q: asyncio.Queue) -> None:
    """Drain one type's queue, dispatch handlers, XACK.

    Runs N instances per type. A handler raising an exception is logged
    + counted but never takes the worker down — XACK still happens so
    Redis doesn't keep redelivering a poison-pill event forever.
    """
    # Local alias + import hoisted out of the hot loop.
    from backend.events import handlers, _run_handler
    ts = _type_stats[event_type]

    while True:
        try:
            stream, entry_id, event_obj = await q.get()
        except asyncio.CancelledError:
            return
        try:
            fn_list = handlers.get(event_type) or []
            for fn in fn_list:
                try:
                    # source="subscriber" → tells _run_handler not to bump
                    # the local-dispatch counter; this path is tracked
                    # separately in ts.dispatched.
                    await _run_handler(fn, event_obj, source="subscriber")
                    _stats.dispatched += 1
                    ts.dispatched += 1
                except Exception as exc:
                    _stats.handler_errors += 1
                    ts.handler_errors += 1
                    _stats.last_error = f"{event_type}: {exc}"
                    ts.last_error = str(exc)
                    _log.exception(
                        "[eventbus-sub] handler %s(%s) failed", fn, event_type
                    )
        finally:
            # Always ACK — even if every handler failed. A poison event
            # that keeps failing would otherwise hold up redelivery to
            # every replica forever. The handler_errors counter is the
            # signal to investigate.
            await _safe_xack(stream, entry_id)
            q.task_done()


async def _safe_xack(stream: str, entry_id: Any) -> None:
    try:
        await _redis.xack(stream, _GROUP, entry_id)
    except Exception as exc:  # pragma: no cover
        _log.warning("[eventbus-sub] XACK %s/%s failed: %s", stream, entry_id, exc)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def _maybe_str(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    return str(v)


def _decode_fields(fields: Any):
    """Convert a Redis stream field-dict into (event_type, Event).

    Returns (type, None) on malformed input so the caller knows to skip
    dispatch but can still ACK. Field keys/values may be bytes or str
    depending on aioredis decode options; handle both.
    """
    from backend.events import Event

    try:
        # Normalise keys and values to str.
        flat: dict[str, str] = {}
        if isinstance(fields, dict):
            iter_items = fields.items()
        else:
            iter_items = fields
        for k, v in iter_items:
            flat[_maybe_str(k)] = _maybe_str(v)
    except Exception:
        return "", None

    event_type = flat.get("type", "")
    if not event_type:
        return "", None

    raw_payload = flat.get("payload", "")
    data: dict
    if raw_payload:
        try:
            parsed = json.loads(raw_payload)
            data = parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            data = {"_raw": raw_payload}
    else:
        data = {}

    # Re-inject envelope fields so handlers that read data["uid"] etc.
    # continue to work transparently.
    for k in ("uid", "session", "request_id"):
        if flat.get(k):
            data.setdefault(k, flat[k])
    if flat.get("ts"):
        try:
            data.setdefault("ts", float(flat["ts"]))
        except ValueError:
            pass

    return event_type, Event(type=event_type, data=data)
