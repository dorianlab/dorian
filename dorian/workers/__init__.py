"""Dorian Workers — bus-native Dask worker supervisor with auto-scaling."""

from dorian.workers.events import (
    WORKER_METRICS_COLLECTED,
    WORKER_SCALE_UP,
    WORKER_SCALE_DOWN,
    WORKER_SPAWNED,
    WORKER_RETIRED,
    WORKER_SUPERVISOR_STARTED,
    WORKER_SUPERVISOR_STOPPED,
)

__all__ = [
    "WORKER_METRICS_COLLECTED",
    "WORKER_SCALE_UP",
    "WORKER_SCALE_DOWN",
    "WORKER_SPAWNED",
    "WORKER_RETIRED",
    "WORKER_SUPERVISOR_STARTED",
    "WORKER_SUPERVISOR_STOPPED",
]
