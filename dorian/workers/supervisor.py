"""Worker supervisor — manages compute workers on this host.

Backend-agnostic: delegates spawn/retire to a WorkerBackend (Dask, Ray, etc.).
Subscribes to WorkerScaleUp / WorkerScaleDown and emits WorkerSpawned /
WorkerRetired after each action.

Lifecycle:
    backend = DaskBackend(scheduler_address)
    supervisor = Supervisor(cfg, backend)
    await supervisor.start()
    ...
    await supervisor.stop()
"""

from __future__ import annotations

import asyncio

from dorian.workers.backend import WorkerBackend, DaskBackend
from dorian.workers.bus import aemit, subscribe
from dorian.workers.config import WorkerConfig
from dorian.workers.events import (
    WORKER_SCALE_UP,
    WORKER_SCALE_DOWN,
    WORKER_SPAWNED,
    WORKER_RETIRED,
    WORKER_SUPERVISOR_STARTED,
    WORKER_SUPERVISOR_STOPPED,
)
from dorian.workers.monitor import monitor_loop
from dorian.workers.scaling import ScalingPolicy


class Supervisor:
    """Master process that manages compute workers on the local host."""

    def __init__(self, cfg: WorkerConfig, backend: WorkerBackend | None = None) -> None:
        self.cfg = cfg
        self._backend: WorkerBackend = backend or DaskBackend(cfg.scheduler_address)
        self._worker_addrs: list[str] = []
        self._monitor_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._policy = ScalingPolicy(cfg)
        self._scale_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect backend, spawn initial workers, start monitor."""
        # DaskBackend needs an explicit connect step; other backends may not.
        if hasattr(self._backend, "connect"):
            await self._backend.connect()

        # Spawn initial pool up to min_workers.
        for _ in range(self.cfg.min_workers):
            await self._spawn_one()

        # Wire scaling policy into the bus.
        self._policy.register()
        subscribe(WORKER_SCALE_UP, self._handle_scale_up)
        subscribe(WORKER_SCALE_DOWN, self._handle_scale_down)

        # Start monitor loop.
        self._monitor_task = asyncio.create_task(
            monitor_loop(self.cfg, self._backend, self._stop_event),
            name="worker-monitor",
        )

        await aemit(WORKER_SUPERVISOR_STARTED, {
            "scheduler": self.cfg.scheduler_address,
            "min_workers": self.cfg.min_workers,
            "max_workers": self.cfg.max_workers,
            "initial_workers": len(self._worker_addrs),
        })

    async def stop(self) -> None:
        """Gracefully retire all workers and disconnect."""
        self._stop_event.set()

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # Retire all workers.
        for addr in list(self._worker_addrs):
            await self._retire_one(addr)

        await self._backend.close()
        await aemit(WORKER_SUPERVISOR_STOPPED, {})

    # ------------------------------------------------------------------
    # Worker management
    # ------------------------------------------------------------------

    async def _spawn_one(self) -> str | None:
        """Start a single worker via the backend."""
        if len(self._worker_addrs) >= self.cfg.max_workers:
            return None

        addr = await self._backend.spawn(
            memory_limit=self.cfg.worker_memory_limit,
            threads=self.cfg.worker_threads,
        )
        self._worker_addrs.append(addr)

        await aemit(WORKER_SPAWNED, {
            "worker_address": addr,
            "total_workers": len(self._worker_addrs),
        })
        return addr

    async def _retire_one(self, address: str | None = None) -> None:
        """Retire a single worker via the backend."""
        if not self._worker_addrs:
            return

        target = address or self._worker_addrs[-1]
        retired = await self._backend.retire(target)

        if retired and retired in self._worker_addrs:
            self._worker_addrs.remove(retired)

        await aemit(WORKER_RETIRED, {
            "worker_address": retired,
            "total_workers": len(self._worker_addrs),
        })

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _handle_scale_up(self, event) -> None:
        """Respond to WorkerScaleUp — spawn N workers."""
        async with self._scale_lock:
            count = event.data.get("count", 1) if hasattr(event, "data") else 1
            reason = event.data.get("reason", "") if hasattr(event, "data") else ""
            spawned = 0
            for _ in range(count):
                if await self._spawn_one():
                    spawned += 1
            if spawned:
                print(f"[workers] scaled up +{spawned} ({reason})")

    async def _handle_scale_down(self, event) -> None:
        """Respond to WorkerScaleDown — retire N workers."""
        async with self._scale_lock:
            count = event.data.get("count", 1) if hasattr(event, "data") else 1
            reason = event.data.get("reason", "") if hasattr(event, "data") else ""
            retired = 0
            for _ in range(count):
                if len(self._worker_addrs) > self.cfg.min_workers:
                    await self._retire_one()
                    retired += 1
            if retired:
                print(f"[workers] scaled down -{retired} ({reason})")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def worker_count(self) -> int:
        return len(self._worker_addrs)
