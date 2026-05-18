"""Event handlers for worker supervisor events.

Subscribes to WorkerMetricsCollected and feeds data into the observability
collector so worker host metrics appear in the /observability/workers API.
"""

from __future__ import annotations

from dorian.observability.collector import collector


async def record_worker_metrics(event) -> None:
    """Feed worker metrics into the observability collector."""
    collector.record_worker_metrics(event.data)
