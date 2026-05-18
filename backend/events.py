"""In-process event bus with worker-pool back-pressure.

Lifecycle:
    start_workers()   — call once during app startup  (from lifespan)
    stop_workers()    — call once during app shutdown (from lifespan)

When the pool is active, aemit() submits handler invocations to a bounded
asyncio.Queue and awaits their completion.  When inactive (startup, shutdown,
tests) handlers execute directly in the caller's coroutine.

sync emit() bridges from threads (Dask workers, asyncio.to_thread) to the
main event loop via run_coroutine_threadsafe.
"""

from __future__ import annotations

from typing import Dict, Callable, Any, TypeAlias
from termcolor import colored
from dataclasses import dataclass, field
from collections import defaultdict
from collections.abc import Coroutine
import asyncio
import logging
import os
import time
import traceback as _tb

# Shadow-mode tee to the Go event bus (phase B of the Go-bus migration).
# When DORIAN_EVENTBUS_SHADOW=1, every event dispatched through aemit /
# aemit_bg / emit is ALSO enqueued for forwarding to the Go event bus.
# Default is disabled — zero overhead when off.
from backend import eventbus_shadow as _shadow

# Authoritative-event toggle (phase C). When event.type is marked
# authoritative, local dispatch is SKIPPED — the subscriber pulls from
# the Go bus and runs handlers. Shadow still runs so the event reaches
# the Go bus in the first place.
from backend import eventbus_authoritative as _authz

# Go-handled types (phase G). When an event type is in this set, the
# Python side must NOT dispatch it — neither inline at emit time nor
# through the in-process subscriber. The Go subscriber inside the
# eventbus binary owns the handler. Forwarding to the shared Redis
# stream still happens so the Go side can read it.
from backend import eventbus_go_handled as _go_handled

_log = logging.getLogger(__name__)

import threading as _threading

import psutil as _psutil

_obs_process = _psutil.Process()

# ── Sampled RSS ─────────────────────────────────────────────────────
# Instead of calling psutil.memory_info() twice per handler (2–10ms
# of GIL-holding overhead each), we sample RSS once per second in a
# background thread.  Handlers read the cached value (~0 cost).
_sampled_rss: int = 0  # bytes


def _rss_sampler(interval: float = 1.0) -> None:
    """Background daemon thread that samples process RSS periodically."""
    global _sampled_rss
    while True:
        try:
            _sampled_rss = _obs_process.memory_info().rss
        except Exception:
            pass
        _threading.Event().wait(interval)


_rss_thread = _threading.Thread(target=_rss_sampler, daemon=True, name="rss-sampler")
_rss_thread.start()


# ===================================================================
# Event + Handler types
# ===================================================================
@dataclass
class Event:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str):
        if key not in self.data:
            raise KeyError(f"{key!r} not found in {self.type} event")
        return self.data[key]


Handler: TypeAlias = Callable[[Event], None] | Callable[[Event], Coroutine[None, None, None]]


# ===================================================================
# Handler registry
# ===================================================================
def color_of(event: str) -> str:
    if any(kw in event for kw in ('Error', 'Failed', 'Crash', 'NotFound', 'Exception', 'Unknown', 'Malformed', 'Irrelevant')):
        return 'red'
    return 'green'


def verbose(event: Event):
    print(f'{colored(event.type, color_of(event.type)):<30}{event.data}')


handlers: defaultdict[str, list[Handler]] = defaultdict(lambda: [verbose])


def subscribe(event: str, fn: Handler) -> None:
    """Register *fn* as a handler for *event*.

    *event* can be a plain string or an ``EventType`` enum member (which
    is itself a ``StrEnum`` and compares equal to its string value).
    """
    handlers[event].append(fn)


# ===================================================================
# Handler execution (shared by workers and direct fallback)
# ===================================================================
async def _run_handler(
    fn: Handler, event: Event, *, source: str = "local",
) -> None:
    """Execute one handler. Errors are logged locally (no bus re-entry).

    ``source`` identifies which dispatch path invoked us:
      * ``"local"`` — called from aemit / aemit_bg / _emit_direct /
        worker pool. Bumps ``_local_dispatch_counts_by_type``.
      * ``"subscriber"`` — called from the Go-bus subscriber's per-type
        worker. Does NOT bump the local counter (avoid double-counting
        in the discrepancy detector — the subscriber has its own
        ``by_type.dispatched`` count).
    Observability instrumentation runs for both sources so the
    per-handler dashboard keeps its full view.
    """
    # verbose is the built-in print-only fallback — skip observability
    # instrumentation for it to avoid high-frequency noise in the metrics log.
    _observe = fn is not verbose
    _wall_start = time.perf_counter()
    _error = False
    _error_msg: str | None = None
    try:
        if asyncio.iscoroutinefunction(fn):
            await fn(event)
        else:
            await asyncio.to_thread(fn, event)
    except Exception as e:
        _error = True
        _error_msg = str(e)
        try:
            verbose(Event('EventHandlerError', data={
                'source': f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', getattr(fn, '__name__', '?'))}",
                'event': event.type,
                'payload': event.data,
                'error': str(e),
                'trace': _tb.format_exc(),
            }))
        except Exception:
            pass
    finally:
        # Per-type local-dispatch counter for the phase D discrepancy
        # detector. Counts every completed handler invocation via the
        # Python-local path, whether it succeeded or raised. Bypassed
        # when the subscriber calls us via source="subscriber" — that
        # path has its own by-type dispatched counter so counting it
        # here would double-report the same run.
        if source == "local":
            _local_dispatch_counts_by_type[event.type] += 1

        if _observe:
            _wall_s = time.perf_counter() - _wall_start
            # Use background-sampled RSS (~0 cost) instead of calling
            # psutil.memory_info() per handler (2–10ms GIL overhead).
            _rss_now = _sampled_rss
            _fn_name = (
                f"{getattr(fn, '__module__', '?')}."
                f"{getattr(fn, '__qualname__', getattr(fn, '__name__', '?'))}"
            )
            from dorian.observability.collector import collector
            try:
                collector.record_handler(
                    fn_name=_fn_name,
                    event_type=event.type,
                    wall_s=_wall_s,
                    rss_mb=_rss_now / (1024 ** 2),
                    delta_mb=0.0,  # per-handler delta not meaningful with sampled RSS
                    error=_error,
                    error_msg=_error_msg,
                    uid=event.data.get("uid") or event.data.get("user"),
                    session=event.data.get("session") or event.data.get("sessionId"),
                )
            except Exception:
                pass  # never let collector errors break the bus


# ===================================================================
# Worker pool
# ===================================================================
def _load_bus_config() -> tuple[int, int, int, int]:
    """Read event bus sizing from Dynaconf config (safe at import time).

    Returns ``(pool_size, user_queue_capacity, bg_queue_capacity, reserved_user_workers)``.
    Env-var overrides: ``EVENT_BUS_POOL_SIZE``, ``EVENT_BUS_USER_CAP``,
    ``EVENT_BUS_BG_CAP``, ``EVENT_BUS_RESERVED_USER``.
    """
    pool, user_cap, bg_cap, reserved = 64, 4096, 2048, 16
    try:
        from backend.config import config
        bus = getattr(config, "event_bus", None)
        if bus is not None:
            pool = int(getattr(bus, "pool_size", pool))
            user_cap = int(getattr(bus, "user_capacity", getattr(bus, "queue_capacity", user_cap)))
            bg_cap = int(getattr(bus, "bg_capacity", bg_cap))
            reserved = int(getattr(bus, "reserved_user_workers", reserved))
    except Exception:
        pass
    # Env-var overrides (take precedence over config)
    pool = int(os.environ.get("EVENT_BUS_POOL_SIZE", pool))
    user_cap = int(os.environ.get("EVENT_BUS_USER_CAP", user_cap))
    bg_cap = int(os.environ.get("EVENT_BUS_BG_CAP", bg_cap))
    reserved = int(os.environ.get("EVENT_BUS_RESERVED_USER", reserved))
    return pool, user_cap, bg_cap, max(0, min(reserved, pool))


POOL_SIZE, USER_CAPACITY, BG_CAPACITY, RESERVED_USER_WORKERS = _load_bus_config()


# ── Event classification ──────────────────────────────────────────────────
#
# Every emit is routed to one of two queues:
#   * ``_user_queue`` — traffic that a human is waiting on (user sessions,
#     gateway responses, WS pushes). Large, unbounded for practical purposes
#     — if this ever fills, the backend is in real trouble and blocking is
#     the right behaviour.
#   * ``_bg_queue`` — RL generator, cross-product trials, anything tagged
#     ``uid=system`` + ``session startswith rl:``. Bounded, drop-OLDEST
#     on overflow so a runaway producer cannot starve the user queue,
#     and so the freshest state wins when the bus can't keep up.
#
# On top of that, a small set of extremely high-frequency tracing events
# (``NodeObservability``, ``NodeExecutionStarted``, ``NodeExecutionCompleted``)
# are SUPPRESSED at emit time when the payload is RL-sourced. These events
# only exist to paint node badges on the user's canvas; RL sessions have no
# canvas and emitting them is pure overhead.  See ``_DROP_FOR_RL``.

_DROP_FOR_RL: frozenset[str] = frozenset({
    "NodeObservability",
    "NodeExecutionStarted",
    "NodeExecutionCompleted",
    # High-frequency cache telemetry — only meaningful for the user canvas,
    # not for RL sessions that have no UI.
    "InstanceCacheHit",
    "InstanceCacheMiss",
})


def _is_rl_event(event: Event) -> bool:
    """True when the event's payload identifies an RL-generator run.

    RL runs always use ``uid="system"`` and ``session="rl:round-N:did..."``.
    Handlers and routing use this to keep user traffic isolated from RL
    pressure.
    """
    d = event.data
    if not isinstance(d, dict):
        return False
    if d.get("uid") != "system":
        return False
    session = d.get("session") or ""
    return isinstance(session, str) and session.startswith("rl:")


def _should_drop_at_emit(event: Event) -> bool:
    """True when the event is safe to drop entirely — never reaches any handler.

    We only drop the RL-side high-frequency canvas-tracing events.  Any event
    that feeds Postgres, the error corpus or the RL learning loop is
    allowed through regardless of volume.
    """
    return event.type in _DROP_FOR_RL and _is_rl_event(event)


# ── Queue state ───────────────────────────────────────────────────────────

_user_queue: asyncio.Queue | None = None
_bg_queue: asyncio.Queue | None = None
_workers: list[asyncio.Task] = []
_loop: asyncio.AbstractEventLoop | None = None

# ── Worker liveness heartbeat ─────────────────────────────────────────────
# Updated to ``time.monotonic()`` whenever a worker pops an item from a
# queue. ``eventbus_healthy()`` uses it to detect the silent-degradation
# pattern documented in
# ``project_python_eventbus_workers_degrade.md``: after enough lifespan
# reloads, the Python event-bus workers stop processing items even though
# the process keeps running and uvicorn keeps serving HTTP. The shadow
# forwarder still pushes events to Redis (so the rust subscriber keeps
# firing), but local handlers like ``seed_session`` never run — sidebar
# selectors stop populating, no ``state/queries`` events reach the SPA.
# The heartbeat lets the docker healthcheck observe the failure and
# recycle the container instead of leaving a half-dead backend in
# rotation.
_last_pop_ts: float = 0.0
# How long a non-empty queue is allowed to sit without a pop before we
# call the pool unhealthy. Tuned to be longer than any legitimate single
# handler (heavy seed_session phases run ~2s, KB warmup hits ~5s on a
# cold cache); 30s gives plenty of margin without papering over a real
# stall.
_HEARTBEAT_STALE_S: float = 30.0

# Counters for observability — exposed via ``events_bus_stats()``.
_drop_counts: dict[str, int] = defaultdict(int)
_enqueue_counts: dict[str, int] = defaultdict(int)

# Phase D — per-event-type counters for the shadow-discrepancy detector.
# ``_emit_counts_by_type``: number of events accepted (post-drop-filter)
#   per type, regardless of whether they were dispatched locally or
#   routed through Go.
# ``_local_dispatch_counts_by_type``: count of HANDLER INVOCATIONS that
#   completed through the Python-local path (worker-pool + direct
#   branches). Does NOT include handlers run via the Go subscriber —
#   those are counted in ``eventbus_subscriber._type_stats``.
# Joining both with the subscriber's per-type view gives the full
# picture: emitted vs python-dispatched vs go-dispatched vs skipped.
_emit_counts_by_type: dict[str, int] = defaultdict(int)
_local_dispatch_counts_by_type: dict[str, int] = defaultdict(int)


def events_bus_stats() -> dict[str, Any]:
    """Snapshot for monitoring. Called by the observability dashboard."""
    return {
        "user_queue": {
            "size": _user_queue.qsize() if _user_queue else 0,
            "maxsize": _user_queue.maxsize if _user_queue else 0,
        },
        "bg_queue": {
            "size": _bg_queue.qsize() if _bg_queue else 0,
            "maxsize": _bg_queue.maxsize if _bg_queue else 0,
        },
        "workers": len(_workers),
        "reserved_user_workers": RESERVED_USER_WORKERS,
        "drops_by_reason": dict(_drop_counts),
        "enqueues_by_lane": dict(_enqueue_counts),
        "shadow": _shadow.stats(),
        "authoritative": _authz.snapshot_meta() | {"types": _authz.snapshot()},
        "go_handled": _go_handled.snapshot_meta() | {"types": _go_handled.snapshot()},
        "subscriber": _eventbus_subscriber_stats(),
        "emit_counts_by_type": dict(_emit_counts_by_type),
        "local_dispatch_counts_by_type": dict(_local_dispatch_counts_by_type),
    }


def _eventbus_subscriber_stats() -> dict[str, Any]:
    """Lazy import — subscriber module imports back into events, so
    importing at module level would create a cycle at startup."""
    try:
        from backend import eventbus_subscriber as _sub
        return _sub.stats()
    except Exception:
        return {"enabled": False}


async def _worker(
    reserved_for_user: bool,
    user_q: asyncio.Queue,
    bg_q: asyncio.Queue,
) -> None:
    """Process (handler, event, done) items from queues.

    When ``reserved_for_user`` is True, this worker only pulls from the
    user queue — even if the user queue is empty and the bg queue is full.
    This guarantees that a runaway RL producer can never starve user
    traffic out of all worker threads.

    General workers prefer the user queue and fall back to the bg queue
    when it's empty.

    ``user_q`` and ``bg_q`` are passed by parameter (not read from
    globals) so that a subsequent lifespan hot-reload cycle that
    overwrites the module-level globals does not silently redirect this
    worker's queue access to the *new* cycle's queues.  Each worker is
    bound at creation time to the exact queue objects it was started with,
    guaranteeing that the sentinel ``None`` values sent by *this* cycle's
    ``stop_workers`` reach *this* cycle's workers — not a future cycle's.
    """
    while True:
        if reserved_for_user:
            item = await user_q.get()
            src_queue = user_q
        else:
            # Non-blocking peek at user queue first — preserves user priority.
            try:
                item = user_q.get_nowait()
                src_queue = user_q
            except asyncio.QueueEmpty:
                # Wait on whichever queue fires first; prefer user on ties.
                user_task = asyncio.create_task(user_q.get())
                bg_task = asyncio.create_task(bg_q.get())
                done, pending = await asyncio.wait(
                    {user_task, bg_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                # Resolve whichever completed first; cancel the other
                # without losing its item if it happened to complete too.
                if user_task in done:
                    item = user_task.result()
                    src_queue = user_q
                    if bg_task in done:
                        # Both completed — put the bg item back on the tail.
                        # Order loss within bg is acceptable (drop-oldest
                        # semantics already imply lenient ordering).
                        bg_item = bg_task.result()
                        bg_q.task_done()  # release the get() we're about to re-do
                        try:
                            bg_q.put_nowait(bg_item)
                        except asyncio.QueueFull:
                            # Lose the item rather than block — it's RL.
                            if bg_item is not None:
                                _, _, d = bg_item
                                d.set()
                            _drop_counts["bg_putback_full"] += 1
                    else:
                        bg_task.cancel()
                else:
                    item = bg_task.result()
                    src_queue = bg_q
                    user_task.cancel()

        # Liveness heartbeat — updated on every successful pop so a
        # stalled / crashed worker pool shows up in
        # ``eventbus_healthy()`` even if the Python process is otherwise
        # serving HTTP. Set BEFORE the None-sentinel branch so a clean
        # shutdown still bumps the timestamp (avoids a false-positive
        # unhealthy reading during stop_workers).
        global _last_pop_ts
        _last_pop_ts = time.monotonic()

        if item is None:
            src_queue.task_done()
            return
        fn, event, done = item
        try:
            await _run_handler(fn, event)
        finally:
            done.set()
            src_queue.task_done()


def eventbus_healthy() -> tuple[bool, dict]:
    """Worker-pool liveness check used by the docker healthcheck.

    Returns ``(ok, detail)`` where ``ok`` is False whenever the worker
    pool has stalled — either it was never started, or queues are
    backing up while workers haven't popped anything in
    ``_HEARTBEAT_STALE_S`` seconds.

    The check is intentionally generous: an idle backend with empty
    queues is healthy regardless of how stale the heartbeat is.
    Unhealthy means "items are waiting and nobody's processing them."
    """
    if _user_queue is None or _bg_queue is None:
        return False, {"reason": "workers_not_started"}
    user_size = _user_queue.qsize()
    bg_size = _bg_queue.qsize()
    if user_size == 0 and bg_size == 0:
        return True, {
            "user_queue": 0, "bg_queue": 0,
            "last_pop_age_s": (
                None if _last_pop_ts == 0.0
                else round(time.monotonic() - _last_pop_ts, 2)
            ),
        }
    age = time.monotonic() - _last_pop_ts if _last_pop_ts else float("inf")
    if age > _HEARTBEAT_STALE_S:
        return False, {
            "reason": "worker_pool_stalled",
            "user_queue": user_size,
            "bg_queue": bg_size,
            "last_pop_age_s": round(age, 2),
            "stale_threshold_s": _HEARTBEAT_STALE_S,
        }
    return True, {
        "user_queue": user_size,
        "bg_queue": bg_size,
        "last_pop_age_s": round(age, 2),
    }


async def start_workers(
    pool_size: int = POOL_SIZE,
    user_capacity: int = USER_CAPACITY,
    bg_capacity: int = BG_CAPACITY,
    reserved_user_workers: int = RESERVED_USER_WORKERS,
) -> None:
    """Start the worker pool.  Call once during app startup.

    Two queues are created: a user-priority queue (large capacity, never
    drops) and a background queue (bounded, drop-oldest on overflow).
    A subset of workers is reserved for user traffic so they never starve
    even under sustained RL-generator load.
    """
    global _user_queue, _bg_queue, _workers, _loop, _last_pop_ts
    _loop = asyncio.get_running_loop()
    # Seed the heartbeat so a freshly-started pool isn't classified as
    # stale before any item arrives.
    _last_pop_ts = time.monotonic()
    _user_queue = asyncio.Queue(maxsize=user_capacity)
    _bg_queue = asyncio.Queue(maxsize=bg_capacity)
    reserved = max(0, min(reserved_user_workers, pool_size))
    # Capture local refs BEFORE assigning to globals.  Workers receive the
    # queue objects by parameter so a subsequent hot-reload cycle that
    # overwrites the module-level globals does not silently redirect their
    # queue access to the new cycle's objects.
    _uq, _bq = _user_queue, _bg_queue
    _workers = [
        asyncio.create_task(
            _worker(reserved_for_user=(i < reserved), user_q=_uq, bg_q=_bq),
            name=f"event-worker-{i}{'-user' if i < reserved else ''}",
        )
        for i in range(pool_size)
    ]

    # Start the Go event-bus shadow forwarder. No-op when shadow mode
    # is disabled (DORIAN_EVENTBUS_SHADOW != 1).
    await _shadow.start()

    # Start the authoritative-type refresher + in-process subscriber.
    # Both are no-ops unless their respective env vars are set. We pull
    # aioredis lazily here so this module doesn't hard-depend on
    # backend.envs at import time (tests stub backend.envs separately).
    try:
        from backend.envs import aioredis as _aio
        await _authz.start(_aio)
        await _go_handled.start(_aio)
        from backend import eventbus_subscriber as _sub
        await _sub.start(_aio)
    except Exception as _exc:
        _log.warning("[eventbus] auth/subscriber startup skipped: %s", _exc)


async def stop_workers() -> None:
    """Drain the queues and shut down workers. Call once during app shutdown."""
    global _user_queue, _bg_queue, _loop
    if not _workers:
        return

    # Capture local refs now.  The globals are cleared below and must not
    # be touched between here and the final gather.
    user_q = _user_queue
    bg_q = _bg_queue

    # All sentinels go into the user queue.
    #
    # Every worker — reserved or general — reads from user_queue at some
    # point: reserved workers via `await user_q.get()`; general workers
    # via `user_q.get_nowait()` or the `asyncio.wait` user_task branch.
    #
    # The previous approach split sentinels across both queues
    # (16 → user_queue, 48 → bg_queue for a 64-worker pool).  General
    # workers prefer user_queue and could steal the 16 sentinels meant for
    # reserved workers.  A reserved worker that never received its sentinel
    # blocked the gather indefinitely, causing uvicorn to cancel
    # stop_workers, leaving globals un-cleared and old worker tasks
    # orphaned — the root of the silent-degradation pattern documented in
    # (Internal design note; documentation lives outside this repo.)
    n = len(_workers)
    for _ in range(n):
        try:
            user_q.put_nowait(None)
        except asyncio.QueueFull:
            await user_q.put(None)

    await asyncio.gather(*_workers, return_exceptions=True)

    # Unblock any aemit callers still waiting on stale items.
    for q in (user_q, bg_q):
        while q and not q.empty():
            try:
                item = q.get_nowait()
                if item is not None:
                    _, _, done = item
                    done.set()
            except asyncio.QueueEmpty:
                break

    _workers.clear()
    _user_queue = None
    _bg_queue = None
    _loop = None

    # Shadow forwarder flushes on shutdown; safe when disabled.
    await _shadow.stop()

    # Stop the authoritative refresher + subscriber.
    try:
        from backend import eventbus_subscriber as _sub
        await _sub.stop()
        await _go_handled.stop()
        await _authz.stop()
    except Exception:
        pass


def _bg_put_drop_oldest(item: tuple) -> bool:
    """Put an item on the bg queue; drop the oldest on overflow.

    Returns True if the item was enqueued, False if it had to replace
    an older one (which is still "enqueued" from the caller's POV,
    just at the cost of a dropped predecessor).
    """
    assert _bg_queue is not None
    try:
        _bg_queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        # Drop the oldest item to make room. Mark its ``done`` event so any
        # (unusual) awaiter on the dropped item unblocks.
        try:
            old = _bg_queue.get_nowait()
            if old is not None:
                _, _, done = old
                done.set()
            _bg_queue.task_done()
        except asyncio.QueueEmpty:
            pass
        _drop_counts["bg_overflow"] += 1
        try:
            _bg_queue.put_nowait(item)
        except asyncio.QueueFull:
            # Extremely unlikely — queue refilled in the gap above. Give up
            # on this item rather than blocking; the producer moves on.
            _, _, done = item
            done.set()
            _drop_counts["bg_overflow_hard"] += 1
            return False
        return True


# ===================================================================
# Shadow helper — forward accepted events to the Go event bus (phase B)
# ===================================================================

def _shadow_one(event: Event) -> None:
    """Best-effort tee of one event to the Go event bus.

    Only called for events that pass the local drop filter, so the Go
    side sees exactly the set Python dispatches. Lane classification
    mirrors ``_is_rl_event`` so the two buses agree on priority.
    Never raises; no-op when shadow mode is disabled.
    """
    if not _shadow.is_enabled():
        return
    try:
        lane = "bg" if _is_rl_event(event) else "user"
        data = event.data if isinstance(event.data, dict) else {}
        uid = str(data.get("uid", "")) if data else ""
        session = str(data.get("session", "")) if data else ""
        request_id = str(data.get("request_id", "")) if data else ""
        _shadow.shadow_emit(
            event.type, data, lane=lane,
            uid=uid, session=session, request_id=request_id,
        )
    except Exception:
        # Never let shadow bookkeeping affect production dispatch.
        pass


# ===================================================================
# Emit — async (primary interface)
# ===================================================================
async def aemit(*events: Event) -> None:
    """Dispatch events to all registered handlers and await completion.

    Events are classified by ``_is_rl_event``:
      * user events go on ``_user_queue`` — unbounded in practice, never drops
      * RL events go on ``_bg_queue`` — bounded, drop-oldest on overflow

    The caller awaits handler completion as before. On shutdown (queues
    gone) handlers execute directly.
    """
    if _user_queue is None or _bg_queue is None:
        # Workers not running — direct sequential execution
        for event in events:
            if _should_drop_at_emit(event):
                _drop_counts["rl_tracing"] += 1
                continue
            _shadow_one(event)
            # Authoritative types run via the subscriber, not locally.
            if _authz.is_authoritative(event.type):
                _drop_counts["authoritative_skip"] += 1
                continue
            for fn in handlers[event.type]:
                await _run_handler(fn, event)
        return

    pending: list[asyncio.Event] = []
    for event in events:
        if _should_drop_at_emit(event):
            _drop_counts["rl_tracing"] += 1
            continue
        _emit_counts_by_type[event.type] += 1
        _shadow_one(event)
        if _authz.is_authoritative(event.type):
            # Skip local enqueue — the Go bus is authoritative for this
            # event type. The subscriber will dispatch handlers once the
            # event has made the round-trip through Redis Streams.
            _drop_counts["authoritative_skip"] += 1
            continue
        is_bg = _is_rl_event(event)
        for fn in handlers[event.type]:
            done = asyncio.Event()
            item = (fn, event, done)
            if is_bg:
                _enqueue_counts["bg"] += 1
                _bg_put_drop_oldest(item)
            else:
                _enqueue_counts["user"] += 1
                await _user_queue.put(item)
            pending.append(done)

    if pending:
        try:
            await asyncio.gather(*(d.wait() for d in pending))
        except asyncio.CancelledError:
            # The caller was cancelled (e.g. client disconnected) but
            # items are already on a worker queue — handlers will still
            # run to completion. Safe to let this go.
            pass


async def aemit_bg(*events: Event) -> None:
    """Fire-and-forget variant of aemit — does NOT await handler completion.

    Use for non-critical events where the caller doesn't need to wait for
    all handlers to finish (e.g. analytics, canvas persistence). Event
    classification + drop-on-overflow rules are identical to ``aemit``.
    """
    if _user_queue is None or _bg_queue is None:
        # Workers not running — fire-and-forget via create_task
        for event in events:
            if _should_drop_at_emit(event):
                _drop_counts["rl_tracing"] += 1
                continue
            _shadow_one(event)
            if _authz.is_authoritative(event.type):
                _drop_counts["authoritative_skip"] += 1
                continue
            for fn in handlers[event.type]:
                asyncio.create_task(_run_handler(fn, event))
        return

    for event in events:
        if _should_drop_at_emit(event):
            _drop_counts["rl_tracing"] += 1
            continue
        _emit_counts_by_type[event.type] += 1
        _shadow_one(event)
        # Go-handled types: skip local dispatch. The shadow_one above
        # forwarded the event to Redis; the Go subscriber handles it.
        if _go_handled.is_go_handled(event.type):
            _drop_counts["go_handled_skip"] += 1
            continue
        if _authz.is_authoritative(event.type):
            _drop_counts["authoritative_skip"] += 1
            continue
        is_bg = _is_rl_event(event)
        for fn in handlers[event.type]:
            done = asyncio.Event()  # unused — we don't track completion
            item = (fn, event, done)
            if is_bg:
                _enqueue_counts["bg"] += 1
                _bg_put_drop_oldest(item)
            else:
                _enqueue_counts["user"] += 1
                try:
                    _user_queue.put_nowait(item)
                except asyncio.QueueFull:
                    # User queue full is a real problem — but fire-and-forget
                    # means we can't block. Spin up an escape-hatch task so
                    # the event is still processed, just outside the pool.
                    _log.warning(
                        "[event-bus] user queue full — overflow for %s",
                        event.type,
                    )
                    _drop_counts["user_overflow_to_task"] += 1
                    asyncio.create_task(_run_handler(fn, event))


# ===================================================================
# Emit — sync (for Dask workers / asyncio.to_thread / sync contexts)
# ===================================================================
def emit(*events: Event) -> None:
    """Blocking emit for sync contexts (no running event loop).

    Bridges to the main event loop via run_coroutine_threadsafe when the
    worker pool is active, so handlers run on the main loop alongside
    Redis/Postgres connections.  Falls back to a temporary loop otherwise.

    Fast-path: RL tracing events are dropped BEFORE the thread boundary
    crossing (run_coroutine_threadsafe is ~microsecond-scale but adds up
    at thousands of ``NodeObservability`` events per second under RL load).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # good — no running loop in this thread
    else:
        raise RuntimeError(
            "emit() was called from an async context. use: await aemit(...)."
        )

    # Pre-filter: drop RL tracing events without crossing into the main loop.
    keep = [e for e in events if not _should_drop_at_emit(e)]
    if len(keep) < len(events):
        _drop_counts["rl_tracing"] += len(events) - len(keep)
    if not keep:
        return

    if _loop is not None and _loop.is_running():
        # Main loop is alive — submit there (thread-safe)
        future = asyncio.run_coroutine_threadsafe(aemit(*keep), _loop)
        future.result()  # block this thread until handlers complete
    else:
        # No main loop yet (startup) or already stopped (shutdown)
        asyncio.run(_emit_direct(*keep))


async def _emit_direct(*events: Event) -> None:
    """Direct execution on a temporary loop — bypasses worker pool."""
    for event in events:
        if _should_drop_at_emit(event):
            _drop_counts["rl_tracing"] += 1
            continue
        _emit_counts_by_type[event.type] += 1
        _shadow_one(event)
        # Go-handled types: skip local dispatch. The shadow_one above
        # forwarded the event to Redis; the Go subscriber handles it.
        if _go_handled.is_go_handled(event.type):
            _drop_counts["go_handled_skip"] += 1
            continue
        if _authz.is_authoritative(event.type):
            _drop_counts["authoritative_skip"] += 1
            continue
        for fn in handlers[event.type]:
            await _run_handler(fn, event)


# ===================================================================
# Redis Pub/Sub bridge (opt-in for multi-process / cluster deployments)
# ===================================================================
import json as _json

_BRIDGE_CHANNEL = "dorian:events"
_bridge_task: asyncio.Task | None = None
_bridge_redis = None


async def start_bridge() -> None:
    """Start a Redis Pub/Sub subscriber that relays remote events locally.

    When enabled, ``aemit`` also publishes events to a Redis channel so
    that other processes (workers, replicas) can react.  Incoming messages
    from the channel are dispatched through the local handler registry.

    Enable by calling ``start_bridge()`` after ``start_workers()`` in the
    app lifespan.  Requires ``backend.envs.aioredis`` to be available.
    """
    global _bridge_task, _bridge_redis

    from backend.envs import aioredis as _aio

    _bridge_redis = _aio
    _bridge_task = asyncio.create_task(_bridge_listener(), name="event-bridge")


async def stop_bridge() -> None:
    """Stop the Redis Pub/Sub listener."""
    global _bridge_task, _bridge_redis
    if _bridge_task is not None:
        _bridge_task.cancel()
        try:
            await _bridge_task
        except asyncio.CancelledError:
            pass
        _bridge_task = None
    _bridge_redis = None


async def _bridge_listener() -> None:
    """Subscribe to ``dorian:events`` and dispatch incoming events locally."""
    from backend.envs import aioredis as _aio

    pubsub = _aio.pubsub()
    await pubsub.subscribe(_BRIDGE_CHANNEL)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                payload = _json.loads(message["data"])
                event = Event(type=payload["type"], data=payload.get("data", {}))
                # Skip if this event originated from this process (prevent loops)
                if payload.get("_origin") == id(_user_queue):
                    continue
                await aemit(event)
            except Exception:
                pass  # malformed message — skip silently
    finally:
        await pubsub.unsubscribe(_BRIDGE_CHANNEL)
        await pubsub.aclose()


async def publish_to_bridge(*events: Event) -> None:
    """Publish events to Redis Pub/Sub for cross-process relay.

    Call this alongside ``aemit`` when an event must reach other processes.
    No-op if the bridge is not started.
    """
    if _bridge_redis is None:
        return
    for event in events:
        payload = _json.dumps({
            "type": event.type,
            "data": event.data,
            "_origin": id(_user_queue),
        })
        await _bridge_redis.publish(_BRIDGE_CHANNEL, payload)
