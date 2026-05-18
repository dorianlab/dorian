"""
dorian/observability/sampler.py
---------------------------------
Background process-level sampler using psutil.

Periodically logs CPU utilisation and RSS memory of the current process
to the ``dorian.observability.sampler`` logger.  Runs as a lightweight
asyncio Task alongside the main event loop — zero impact on handlers.

Metrics emitted every `interval` seconds:
    PROCESS  cpu=<float>%  rss=<float>MB
"""
from __future__ import annotations

import asyncio

import psutil

from backend.events import Event, aemit
_process = psutil.Process()

_task: asyncio.Task | None = None


async def _sample_loop(interval: float) -> None:
    # Prime the CPU percent measurement — first call always returns 0.0 by
    # design (psutil computes delta since the *previous* call or process start).
    _process.cpu_percent()
    while True:
        await asyncio.sleep(interval)
        try:
            cpu = _process.cpu_percent()                    # % since last call
            rss = _process.memory_info().rss / (1024 ** 2)  # MB
            await aemit(Event("ProcessSample", {"cpu_percent": round(cpu, 1), "rss_mb": round(rss, 1)}))
        except psutil.NoSuchProcess:
            # Should never happen (we are measuring ourselves), but be safe.
            await aemit(Event("SamplerProcessLost", {}))
            break


async def start_sampler(interval: float = 5.0) -> None:
    """Start the background sampling task.

    Safe to call multiple times — subsequent calls are no-ops if the task is
    already running.

    Args:
        interval: Seconds between samples.  Defaults to 5 s.
    """
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_sample_loop(interval), name="obs-sampler")
    await aemit(Event("SamplerStarted", {"interval": interval}))


async def stop_sampler() -> None:
    """Cancel the sampling task and await its completion.

    Safe to call even if the sampler was never started.
    """
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
    await aemit(Event("SamplerStopped", {}))
