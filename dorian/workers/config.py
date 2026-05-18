"""Worker supervisor configuration.

Reads from Dorian's dynaconf config when available, falls back to
environment variables for standalone deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class WorkerConfig:
    """All tunables for the worker supervisor.  Immutable after construction."""

    # Dask scheduler address — workers connect here.
    scheduler_address: str = "tcp://127.0.0.1:8786"

    # Worker pool bounds.
    min_workers: int = 1
    max_workers: int = 8

    # Resource watermarks (fractions 0..1).
    cpu_high: float = 0.85
    cpu_low: float = 0.30
    ram_high: float = 0.80
    ram_low: float = 0.30
    disk_high: float = 0.90

    # How many workers to add/remove per scaling decision.
    scale_step: int = 1

    # Minimum seconds between consecutive scale actions.
    cooldown_s: int = 30

    # Host monitor collection interval in seconds.
    monitor_interval_s: float = 5.0

    # Per-worker resource limits.
    worker_memory_limit: str = "4GB"
    worker_threads: int = 1
    worker_nanny: bool = True

    @classmethod
    def from_env(cls) -> WorkerConfig:
        """Build config purely from environment variables."""
        return cls(
            scheduler_address=_env_str("DORIAN_WORKERS_SCHEDULER", "tcp://127.0.0.1:8786"),
            min_workers=_env_int("DORIAN_WORKERS_MIN", 1),
            max_workers=_env_int("DORIAN_WORKERS_MAX", 8),
            cpu_high=_env_float("DORIAN_WORKERS_CPU_HIGH", 0.85),
            cpu_low=_env_float("DORIAN_WORKERS_CPU_LOW", 0.30),
            ram_high=_env_float("DORIAN_WORKERS_RAM_HIGH", 0.80),
            ram_low=_env_float("DORIAN_WORKERS_RAM_LOW", 0.30),
            disk_high=_env_float("DORIAN_WORKERS_DISK_HIGH", 0.90),
            scale_step=_env_int("DORIAN_WORKERS_SCALE_STEP", 1),
            cooldown_s=_env_int("DORIAN_WORKERS_COOLDOWN", 30),
            monitor_interval_s=_env_float("DORIAN_WORKERS_MONITOR_INTERVAL", 5.0),
            worker_memory_limit=_env_str("DORIAN_WORKERS_MEMORY_LIMIT", "4GB"),
            worker_threads=_env_int("DORIAN_WORKERS_THREADS", 1),
            worker_nanny=_env_str("DORIAN_WORKERS_NANNY", "true").lower() in ("1", "true", "yes"),
        )

    @classmethod
    def from_dorian_config(cls) -> WorkerConfig:
        """Build config from Dorian's dynaconf config, falling back to env vars."""
        try:
            from backend.config import config
            w = config.workers
            return cls(
                scheduler_address=str(getattr(w, "scheduler_address", None) or _env_str("DORIAN_WORKERS_SCHEDULER", "tcp://127.0.0.1:8786")),
                min_workers=int(getattr(w, "min_workers", 1)),
                max_workers=int(getattr(w, "max_workers", 8)),
                cpu_high=float(getattr(w, "cpu_high", 0.85)),
                cpu_low=float(getattr(w, "cpu_low", 0.30)),
                ram_high=float(getattr(w, "ram_high", 0.80)),
                ram_low=float(getattr(w, "ram_low", 0.30)),
                disk_high=float(getattr(w, "disk_high", 0.90)),
                scale_step=int(getattr(w, "scale_step", 1)),
                cooldown_s=int(getattr(w, "cooldown_s", 30)),
                monitor_interval_s=float(getattr(w, "monitor_interval_s", 5.0)),
                worker_memory_limit=str(getattr(w, "worker_memory_limit", "4GB")),
                worker_threads=int(getattr(w, "worker_threads", 1)),
                worker_nanny=bool(getattr(w, "worker_nanny", True)),
            )
        except Exception:
            return cls.from_env()
