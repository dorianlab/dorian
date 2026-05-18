"""Tests for backend/eventbus_elastic.py — auto-scaling worker pool.

Goals:
  * Pool starts at min_workers.
  * Under sustained queue pressure it scales up to max_workers.
  * Once the queue drains it scales back down toward min_workers.
  * Peak workers recorded; scale_ups / scale_downs counters move.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path


def _load(relpath: str):
    path = Path(__file__).resolve().parents[1] / relpath
    name = f"_mod_{os.urandom(4).hex()}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Starts at min
# ---------------------------------------------------------------------------

def test_pool_starts_at_min_workers():
    m = _load("backend/eventbus_elastic.py")

    async def idle_worker(pool, idx):
        # Block forever until cancelled.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    async def run():
        q = asyncio.Queue()
        p = m.ElasticPool(
            name="t", queue=q, worker_fn=idle_worker,
            min_workers=2, max_workers=5,
        )
        await p.start()
        assert p.stats()["workers"] == 2
        assert p.stats()["workers_min"] == 2
        assert p.stats()["workers_max"] == 5
        await p.stop()

    _run(run())


# ---------------------------------------------------------------------------
# Scale up under sustained pressure
# ---------------------------------------------------------------------------

def test_pool_scales_up_when_queue_stays_high():
    m = _load("backend/eventbus_elastic.py")

    # Workers are deliberately slow so the queue stays above threshold.
    async def slow_worker(pool, idx):
        q = pool.queue
        try:
            while True:
                item = await q.get()
                # Pretend-slow work: 200ms per item.
                await asyncio.sleep(0.2)
                q.task_done()
        except asyncio.CancelledError:
            return

    async def run():
        q = asyncio.Queue(maxsize=1000)
        p = m.ElasticPool(
            name="scale-up", queue=q, worker_fn=slow_worker,
            min_workers=1, max_workers=4,
            scale_up_threshold=5,
            scale_up_cooldown_s=0.1,
            scale_down_cooldown_s=10.0,
            poll_interval_s=0.05,
        )
        await p.start()

        # Flood the queue.
        for i in range(200):
            q.put_nowait(i)

        # Wait for the pool to climb.
        for _ in range(80):
            if p.stats()["workers"] >= 4:
                break
            await asyncio.sleep(0.1)

        s = p.stats()
        assert s["workers"] == 4, f"expected cap reached, got {s}"
        assert s["scale_ups"] >= 3, f"scale_ups={s['scale_ups']}"
        assert s["workers_peak"] >= 4
        await p.stop()

    _run(run())


# ---------------------------------------------------------------------------
# Scale down after idleness
# ---------------------------------------------------------------------------

def test_pool_scales_down_when_idle():
    m = _load("backend/eventbus_elastic.py")

    # Slow-enough workers so the queue stays above threshold long enough
    # to trigger scale-up. 50ms per item × 50 items is 2.5s worth of
    # serial work — plenty for the autoscaler to climb to the cap.
    async def slowish_worker(pool, idx):
        q = pool.queue
        try:
            while True:
                item = await q.get()
                await asyncio.sleep(0.05)
                q.task_done()
        except asyncio.CancelledError:
            return

    async def run():
        q = asyncio.Queue(maxsize=200)
        p = m.ElasticPool(
            name="scale-down", queue=q, worker_fn=slowish_worker,
            min_workers=1, max_workers=4,
            scale_up_threshold=3,
            scale_up_cooldown_s=0.05,
            scale_down_cooldown_s=0.3,
            poll_interval_s=0.05,
        )
        await p.start()

        # Drive to max. With 1 worker at 50ms/item, 60 items pile up far
        # above threshold=3 immediately — scale-up fires repeatedly.
        for i in range(60):
            q.put_nowait(i)
        for _ in range(100):
            if p.stats()["workers"] >= 4:
                break
            await asyncio.sleep(0.05)
        assert p.stats()["workers"] == 4, f"never reached cap: {p.stats()}"

        # Let the queue drain and idle period elapse.
        for _ in range(200):
            if p.stats()["workers"] == 1 and q.qsize() == 0:
                break
            await asyncio.sleep(0.1)

        s = p.stats()
        assert s["workers"] == 1, f"expected min reached, got {s}"
        assert s["scale_downs"] >= 3, f"scale_downs={s['scale_downs']}"
        await p.stop()

    _run(run())


# ---------------------------------------------------------------------------
# Peak is recorded even after scale-down
# ---------------------------------------------------------------------------

def test_workers_peak_persists_after_scale_down():
    m = _load("backend/eventbus_elastic.py")

    async def slowish_worker(pool, idx):
        q = pool.queue
        try:
            while True:
                item = await q.get()
                await asyncio.sleep(0.05)
                q.task_done()
        except asyncio.CancelledError:
            return

    async def run():
        q = asyncio.Queue(maxsize=200)
        p = m.ElasticPool(
            name="peak", queue=q, worker_fn=slowish_worker,
            min_workers=1, max_workers=3,
            scale_up_threshold=3,
            scale_up_cooldown_s=0.05,
            scale_down_cooldown_s=0.2,
            poll_interval_s=0.05,
        )
        await p.start()
        for i in range(40):
            q.put_nowait(i)
        for _ in range(80):
            if p.stats()["workers"] >= 3:
                break
            await asyncio.sleep(0.05)
        assert p.stats()["workers"] == 3, f"never reached cap: {p.stats()}"

        # Drain + idle.
        for _ in range(200):
            if p.stats()["workers"] == 1:
                break
            await asyncio.sleep(0.05)

        s = p.stats()
        assert s["workers"] == 1
        assert s["workers_peak"] == 3, f"peak not preserved: {s}"
        await p.stop()

    _run(run())
