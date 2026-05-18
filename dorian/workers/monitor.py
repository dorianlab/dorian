"""Host health monitor — collects CPU, RAM, disk, and compute queue metrics.

Runs as a periodic async loop and emits WorkerMetricsCollected on each cycle.
Backend-agnostic: uses WorkerBackend.worker_info() for queue depth.

Reliability:
  - Each metric (CPU, RAM, disk) is collected independently — a failure in one
    does not prevent the others from reporting.
  - psutil.cpu_percent() uses a module-level lock to prevent data races when
    called from asyncio.to_thread() (its internal counters are global state).
  - Inside Docker containers, RAM limits are read from cgroup v2/v1 files when
    available, falling back to psutil.virtual_memory() (which reports host RAM).
  - Disk path is platform-aware (Windows: C:\\, Unix: /).
  - Consecutive collection failures trigger exponential backoff on the monitor
    interval (capped at 60s) to avoid log spam on persistent errors.  A single
    success resets the backoff.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import psutil

from dorian.workers.backend import WorkerBackend
from dorian.workers.bus import aemit
from dorian.workers.config import WorkerConfig
from dorian.workers.events import WORKER_METRICS_COLLECTED

# ---------------------------------------------------------------------------
# Thread-safety: psutil.cpu_percent(interval=None) mutates global counters.
# Guard with a lock so concurrent to_thread() calls don't race.
# ---------------------------------------------------------------------------
_cpu_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Platform-aware disk path
# ---------------------------------------------------------------------------
_DISK_PATH: str = "C:\\" if sys.platform == "win32" else "/"

# ---------------------------------------------------------------------------
# Cgroup-aware RAM limits (Docker / Kubernetes)
# ---------------------------------------------------------------------------
_CGROUP_V2_LIMIT = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
_CGROUP_V2_USAGE = Path("/sys/fs/cgroup/memory.current")
_CGROUP_V1_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")

# Very large sentinel used by cgroup when there is no limit (2^63-1 page-aligned).
_NO_LIMIT = 2**62


def _read_cgroup_int(path: Path) -> int | None:
    """Read a cgroup file and return its integer value, or None on failure."""
    try:
        text = path.read_text().strip()
        if text == "max":
            return None  # cgroup v2 "max" = unlimited
        val = int(text)
        return val if val < _NO_LIMIT else None
    except (OSError, ValueError):
        return None


def _container_ram() -> tuple[int, int] | None:
    """Return (used_bytes, total_bytes) from cgroup, or None if not in a container."""
    # cgroup v2
    limit = _read_cgroup_int(_CGROUP_V2_LIMIT)
    usage = _read_cgroup_int(_CGROUP_V2_USAGE)
    if limit is not None and usage is not None:
        return (usage, limit)

    # cgroup v1
    limit = _read_cgroup_int(_CGROUP_V1_LIMIT)
    usage = _read_cgroup_int(_CGROUP_V1_USAGE)
    if limit is not None and usage is not None:
        return (usage, limit)

    return None


# ---------------------------------------------------------------------------
# Metric collection — each metric isolated with its own try/except
# ---------------------------------------------------------------------------

def _sample_cpu() -> float | None:
    """Return CPU utilization percent (0–100), or None on failure."""
    try:
        with _cpu_lock:
            return psutil.cpu_percent(interval=None)
    except Exception:
        return None


def _sample_ram() -> dict | None:
    """Return RAM metrics dict, or None on failure.

    Prefers cgroup limits when running inside a container so the monitor
    reports the container's memory ceiling, not the host's total RAM.
    """
    try:
        cg = _container_ram()
        if cg is not None:
            used, total = cg
            return {
                "ram_used": used,
                "ram_total": total,
                "ram_percent": used / total if total > 0 else 0.0,
            }

        vm = psutil.virtual_memory()
        return {
            "ram_used": vm.used,
            "ram_total": vm.total,
            "ram_percent": vm.percent / 100.0,
        }
    except Exception:
        return None


def _sample_disk() -> dict | None:
    """Return disk metrics dict, or None on failure."""
    try:
        disk = psutil.disk_usage(_DISK_PATH)
        return {
            "disk_used": disk.used,
            "disk_total": disk.total,
            "disk_percent": disk.percent / 100.0,
        }
    except Exception:
        return None


def _collect_host_metrics() -> dict:
    """Gather all host metrics.  Individual failures degrade gracefully."""
    metrics: dict = {"ts": time.time()}

    cpu = _sample_cpu()
    metrics["cpu_percent"] = cpu if cpu is not None else 0.0

    ram = _sample_ram()
    if ram is not None:
        metrics.update(ram)
    else:
        metrics.update({"ram_used": 0, "ram_total": 0, "ram_percent": 0.0})

    disk = _sample_disk()
    if disk is not None:
        metrics.update(disk)
    else:
        metrics.update({"disk_used": 0, "disk_total": 0, "disk_percent": 0.0})

    return metrics


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def collect_metrics(cfg: WorkerConfig, backend: WorkerBackend | None = None) -> dict:
    """Snapshot host + compute metrics.

    Host metrics run in a thread (psutil syscalls can block ~1ms each).
    Backend metrics are collected async via the protocol.
    """
    metrics = await asyncio.to_thread(_collect_host_metrics)

    # Compute queue depth + active worker count via backend protocol.
    if backend is not None:
        try:
            info = await backend.worker_info()
            processing = sum(
                w.get("processing", 0) for w in info.values()
            )
            metrics.update({
                "dask_workers": len(info),
                "dask_processing": processing,
            })
        except Exception:
            metrics.update({"dask_workers": 0, "dask_processing": 0})
    else:
        metrics.update({"dask_workers": 0, "dask_processing": 0})

    return metrics


# ---------------------------------------------------------------------------
# Backoff constants
# ---------------------------------------------------------------------------
_MAX_BACKOFF_S = 60.0
_BACKOFF_FACTOR = 2.0


async def monitor_loop(
    cfg: WorkerConfig,
    backend: WorkerBackend | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Periodic collection loop.  Runs until stop is set or cancelled.

    On consecutive collection failures, the interval backs off exponentially
    (capped at 60s) to avoid log spam.  A single success resets the backoff.
    """
    # Prime the CPU percent counter — first call always returns 0.0.
    with _cpu_lock:
        psutil.cpu_percent(interval=None)

    consecutive_failures = 0
    interval = cfg.monitor_interval_s

    while not (stop and stop.is_set()):
        try:
            metrics = await collect_metrics(cfg, backend)
            await aemit(WORKER_METRICS_COLLECTED, metrics)
            # Success — reset backoff.
            consecutive_failures = 0
            interval = cfg.monitor_interval_s
        except Exception as exc:
            consecutive_failures += 1
            interval = min(
                cfg.monitor_interval_s * (_BACKOFF_FACTOR ** consecutive_failures),
                _MAX_BACKOFF_S,
            )
            # Log locally — don't emit a broken metrics event.
            print(f"[workers] monitor collection failed ({consecutive_failures}x): {exc}")

        # Sleep for the interval, waking early if stop is set.
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break  # stop was set
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)
