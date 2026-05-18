"""Scaling policy — evaluates host metrics and emits scale decisions.

Subscribes to WorkerMetricsCollected and emits WorkerScaleUp / WorkerScaleDown
when watermarks are breached, respecting cooldown and pool bounds.
"""

from __future__ import annotations

import time

from dorian.workers.bus import aemit, subscribe
from dorian.workers.config import WorkerConfig
from dorian.workers.events import (
    WORKER_METRICS_COLLECTED,
    WORKER_SCALE_UP,
    WORKER_SCALE_DOWN,
)


class ScalingPolicy:
    """Stateful policy engine — tracks cooldown and current worker count."""

    def __init__(self, cfg: WorkerConfig) -> None:
        self.cfg = cfg
        self._last_scale_time: float = 0.0

    def _in_cooldown(self) -> bool:
        return (time.time() - self._last_scale_time) < self.cfg.cooldown_s

    async def evaluate(self, event) -> None:
        """Called on every WorkerMetricsCollected event."""
        data = event.data if hasattr(event, "data") else event

        if self._in_cooldown():
            return

        cpu = data.get("cpu_percent", 0) / 100.0
        ram = data.get("ram_percent", 0.0)
        disk = data.get("disk_percent", 0.0)
        current_workers = data.get("dask_workers", 0)
        processing = data.get("dask_processing", 0)

        # --- SCALE UP ---
        if current_workers < self.cfg.max_workers:
            need_up = (
                cpu >= self.cfg.cpu_high
                or ram >= self.cfg.ram_high
                or (processing > 0 and processing >= current_workers)
            )
            if need_up:
                count = min(
                    self.cfg.scale_step,
                    self.cfg.max_workers - current_workers,
                )
                if count > 0:
                    self._last_scale_time = time.time()
                    await aemit(WORKER_SCALE_UP, {
                        "count": count,
                        "reason": f"cpu={cpu:.0%} ram={ram:.0%} workers={current_workers} processing={processing}",
                    })
                    return

        # --- SCALE DOWN ---
        if current_workers > self.cfg.min_workers:
            idle = (
                cpu <= self.cfg.cpu_low
                and ram <= self.cfg.ram_low
                and processing == 0
            )
            if idle:
                count = min(
                    self.cfg.scale_step,
                    current_workers - self.cfg.min_workers,
                )
                if count > 0:
                    self._last_scale_time = time.time()
                    await aemit(WORKER_SCALE_DOWN, {
                        "count": count,
                        "reason": f"cpu={cpu:.0%} ram={ram:.0%} workers={current_workers} processing={processing}",
                    })
                    return

        # --- DISK SAFETY ---
        if disk >= self.cfg.disk_high and current_workers > self.cfg.min_workers:
            self._last_scale_time = time.time()
            await aemit(WORKER_SCALE_DOWN, {
                "count": max(1, current_workers - self.cfg.min_workers),
                "reason": f"disk={disk:.0%} — critical, scaling to minimum",
            })

    def register(self) -> None:
        """Wire this policy into the event bus."""
        subscribe(WORKER_METRICS_COLLECTED, self.evaluate)
