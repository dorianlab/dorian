"""
backend/eventbus_elastic.py
---------------------------
Shared elastic worker-pool primitive used by the Go-bus shadow forwarder
(producer side) and the subscriber per-type pools (consumer side).

Design:
  * Default worker count is 1 — the same shape we shipped in phase C/D,
    so a fresh deploy adds a pool *without* adding concurrency.
  * A background ticker observes the queue depth. If it stays above
    ``scale_up_threshold`` for ``scale_up_cooldown_s``, we spawn one
    more worker, up to ``max_workers``.
  * If the queue stays at zero for ``scale_down_cooldown_s`` AND worker
    count > ``min_workers``, we remove one worker (cancel a task).
  * Peak worker count is recorded for observability.

Why hand-rolled and not e.g. a thread-pool executor? Because:
  * The workers are async (asyncio.Queue + async handler), so a thread
    pool wouldn't help.
  * We need a SINGLE shared code path for both the subscriber (per-type)
    and the shadow drain (per-bus) so ops can tune one concept.
  * The pool grows/shrinks in response to a signal specific to the
    producer/consumer at hand; generic executors expose no knob for this.

This module is transport-agnostic — ``worker_fn`` is any async callable
that takes (pool, worker_idx) and runs until cancelled. Pool metadata
is supplied by the caller.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning defaults — can be overridden per pool instance.
# ---------------------------------------------------------------------------

DEFAULT_MIN_WORKERS = 1
DEFAULT_MAX_WORKERS = 8
DEFAULT_SCALE_UP_THRESHOLD = 32   # queue-depth trigger
DEFAULT_SCALE_UP_COOLDOWN_S = 1.0
DEFAULT_SCALE_DOWN_COOLDOWN_S = 30.0
DEFAULT_POLL_INTERVAL_S = 0.5


@dataclass
class ElasticPoolStats:
    """Observability snapshot for one pool."""
    name: str
    workers: int = 0
    workers_min: int = DEFAULT_MIN_WORKERS
    workers_max: int = DEFAULT_MAX_WORKERS
    workers_peak: int = 0
    scale_ups: int = 0
    scale_downs: int = 0
    last_scale_reason: str = ""
    last_scale_at: float = 0.0


@dataclass
class ElasticPool:
    """An auto-scaling pool of asyncio worker tasks backed by a queue.

    The caller supplies:
      * ``queue`` — an asyncio.Queue the workers consume from.
      * ``worker_fn`` — a coroutine function ``async def f(pool, idx)``
        that loops pulling from ``queue`` and processes items. It MUST
        be cancellable — it's stopped via ``.cancel()`` during scale-down.
      * ``name`` — stable identifier used in logs and stats.

    Lifecycle: ``start()`` spawns the minimum number of workers + the
    scaler task; ``stop()`` cancels everything.
    """
    name: str
    queue: asyncio.Queue
    worker_fn: Callable[["ElasticPool", int], Awaitable[None]]

    min_workers: int = DEFAULT_MIN_WORKERS
    max_workers: int = DEFAULT_MAX_WORKERS
    scale_up_threshold: int = DEFAULT_SCALE_UP_THRESHOLD
    scale_up_cooldown_s: float = DEFAULT_SCALE_UP_COOLDOWN_S
    scale_down_cooldown_s: float = DEFAULT_SCALE_DOWN_COOLDOWN_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S

    # Internal state — initialised in __post_init__.
    _workers: list[asyncio.Task] = field(default_factory=list)
    _next_worker_idx: int = 0
    _scaler_task: Optional[asyncio.Task] = None
    _stats: ElasticPoolStats = field(init=False)

    def __post_init__(self) -> None:
        self._stats = ElasticPoolStats(
            name=self.name,
            workers_min=self.min_workers,
            workers_max=self.max_workers,
        )

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    async def start(self) -> None:
        if self._workers:
            return
        for _ in range(self.min_workers):
            self._spawn_one()
        self._scaler_task = asyncio.create_task(
            self._scaler_loop(), name=f"elastic-scaler-{self.name}"
        )
        _log.info(
            "[elastic-pool:%s] started min=%d max=%d threshold=%d",
            self.name, self.min_workers, self.max_workers, self.scale_up_threshold,
        )

    async def stop(self) -> None:
        if self._scaler_task is not None:
            self._scaler_task.cancel()
            try:
                await self._scaler_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scaler_task = None
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    # -----------------------------------------------------------------
    # Observability
    # -----------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        self._stats.workers = len(self._workers)
        return {
            "workers": self._stats.workers,
            "workers_min": self._stats.workers_min,
            "workers_max": self._stats.workers_max,
            "workers_peak": self._stats.workers_peak,
            "scale_ups": self._stats.scale_ups,
            "scale_downs": self._stats.scale_downs,
            "last_scale_reason": self._stats.last_scale_reason,
            "last_scale_at": self._stats.last_scale_at,
            "queue_depth": self.queue.qsize(),
        }

    # -----------------------------------------------------------------
    # Scaling logic
    # -----------------------------------------------------------------
    def _spawn_one(self) -> None:
        idx = self._next_worker_idx
        self._next_worker_idx += 1
        t = asyncio.create_task(
            self.worker_fn(self, idx),
            name=f"elastic-{self.name}-{idx}",
        )
        self._workers.append(t)
        if len(self._workers) > self._stats.workers_peak:
            self._stats.workers_peak = len(self._workers)

    def _retire_one(self) -> Optional[asyncio.Task]:
        if len(self._workers) <= self.min_workers:
            return None
        t = self._workers.pop()
        t.cancel()
        return t

    async def _scaler_loop(self) -> None:
        """Periodically observe queue depth and scale workers up or down.

        Scale-up fires once ``scale_up_threshold`` is exceeded for
        ``scale_up_cooldown_s`` continuously — we don't overreact to a
        single burst. Scale-down requires the queue to stay empty for
        ``scale_down_cooldown_s`` so we don't flap under choppy load.
        """
        high_since: float = 0.0
        idle_since: float = 0.0
        while True:
            try:
                await asyncio.sleep(self.poll_interval_s)
            except asyncio.CancelledError:
                return

            depth = self.queue.qsize()
            now = time.monotonic()

            # Scale up pressure.
            if depth >= self.scale_up_threshold:
                if high_since == 0.0:
                    high_since = now
                elif now - high_since >= self.scale_up_cooldown_s and len(self._workers) < self.max_workers:
                    self._spawn_one()
                    self._stats.scale_ups += 1
                    self._stats.last_scale_reason = (
                        f"scale-up depth={depth} workers={len(self._workers)}"
                    )
                    self._stats.last_scale_at = time.time()
                    high_since = now  # reset so we don't spawn every tick
                    _log.info("[elastic-pool:%s] %s", self.name, self._stats.last_scale_reason)
            else:
                high_since = 0.0

            # Scale down on sustained idleness.
            if depth == 0:
                if idle_since == 0.0:
                    idle_since = now
                elif now - idle_since >= self.scale_down_cooldown_s and len(self._workers) > self.min_workers:
                    t = self._retire_one()
                    if t is not None:
                        self._stats.scale_downs += 1
                        self._stats.last_scale_reason = (
                            f"scale-down idle>{self.scale_down_cooldown_s:.0f}s "
                            f"workers={len(self._workers)}"
                        )
                        self._stats.last_scale_at = time.time()
                        idle_since = now  # reset so we retire one at a time
                        _log.info(
                            "[elastic-pool:%s] %s", self.name, self._stats.last_scale_reason,
                        )
            else:
                idle_since = 0.0
