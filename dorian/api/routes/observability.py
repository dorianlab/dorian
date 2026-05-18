"""
dorian/api/routes/observability.py
-----------------------------------
REST endpoints for the observability dashboard.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Query

from backend.events import handlers as _handler_registry
from backend.rate_limit import http_rate_limit
from dorian.observability.collector import collector

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/handlers")
async def handler_stats(
    since: float = Query(300),
    uid: Optional[str] = Query(None),
    _rl=http_rate_limit("observability"),
):
    stats = collector.get_handler_stats(since_s=since, uid=uid)
    return [asdict(s) for s in stats]


@router.get("/pipelines")
async def pipeline_stats(
    since: float = Query(300),
    uid: Optional[str] = Query(None),
    _rl=http_rate_limit("observability"),
):
    return collector.get_pipeline_stats(since_s=since, uid=uid)


@router.get("/throughput")
async def event_throughput(
    bucket: float = Query(10),
    since: float = Query(300),
    uid: Optional[str] = Query(None),
    _rl=http_rate_limit("observability"),
):
    return collector.get_event_throughput(bucket_s=bucket, since_s=since, uid=uid)


@router.get("/errors")
async def error_summary(
    since: float = Query(300),
    uid: Optional[str] = Query(None),
    _rl=http_rate_limit("observability"),
):
    return collector.get_error_summary(since_s=since, uid=uid)


@router.get("/system")
async def system_snapshot(_rl=http_rate_limit("observability")):
    return collector.get_system_snapshot()


@router.get("/workers")
async def worker_metrics(
    since: float = Query(300),
    bucket: float = Query(10),
    _rl=http_rate_limit("observability"),
):
    """Worker host metrics time series (CPU, RAM, disk, Dask workers)."""
    return collector.get_worker_metrics(since_s=since, bucket_s=bucket)


@router.get("/workers/latest")
async def worker_latest(_rl=http_rate_limit("observability")):
    """Most recent worker metrics snapshot."""
    return collector.get_worker_latest()


@router.get("/event-bus")
async def event_bus_stats(_rl=http_rate_limit("observability")):
    """Live event-bus state: queue sizes, workers, drop/enqueue counters.

    Use to diagnose RL-traffic overload. If ``bg_queue.size`` keeps saturating
    and ``drops_by_reason.bg_overflow`` climbs, RL is producing faster than
    the pool can consume — lower ``RL_MAX_CONCURRENT`` or raise pool size.
    If ``user_queue.size`` is non-trivial, user traffic is queuing — rare,
    indicates a slow handler.
    """
    from backend.events import events_bus_stats
    return events_bus_stats()


import os as _os
import time as _time

import httpx as _httpx


# ---------------------------------------------------------------------------
# Go-bus stats fetcher with a tiny in-process cache.
# ---------------------------------------------------------------------------
# The discrepancy endpoint is polled by dashboards at 5-10s cadence; we
# don't want each poll to make a separate HTTP call to the Go bus.
# 5-second cache is tight enough that "did we lose anything" stays live.
_EVENTBUS_URL = _os.environ.get("DORIAN_EVENTBUS_URL", "http://localhost:8081").rstrip("/")
_gobus_cache: dict[str, object] = {"ts": 0.0, "data": None, "error": ""}


async def _fetch_gobus_stats(timeout_s: float = 1.0) -> dict:
    now = _time.monotonic()
    if _gobus_cache["ts"] and (now - float(_gobus_cache["ts"])) < 5.0:
        d = _gobus_cache["data"]
        if isinstance(d, dict):
            return d
    try:
        async with _httpx.AsyncClient(timeout=timeout_s) as cli:
            resp = await cli.get(_EVENTBUS_URL + "/stats")
            if resp.status_code == 200:
                data = resp.json()
                _gobus_cache["ts"] = now
                _gobus_cache["data"] = data
                _gobus_cache["error"] = ""
                return data
            _gobus_cache["error"] = f"HTTP {resp.status_code}"
    except Exception as exc:
        _gobus_cache["error"] = str(exc)
    # Return last-good if any, else empty marker.
    last = _gobus_cache["data"]
    if isinstance(last, dict):
        return last
    return {"emitted": {}, "emitted_by_type": {}, "error": _gobus_cache["error"]}


@router.get("/eventbus-discrepancy")
async def eventbus_discrepancy(_rl=http_rate_limit("observability")):
    """Per-event-type cross-check of the Python bus vs the Go bus.

    Surfaced by the operator during the Go-bus cutover to see whether a
    given event type is safe to flip authoritative. For each type the
    backend has seen since startup, returns:

      * ``emitted``            — how many times Python accepted it on emit
      * ``local_dispatched``   — handler invocations completed via Python
      * ``subscriber_received`` — how many times the subscriber pulled it
                                   from the Go bus (shadow confirmation)
      * ``subscriber_dispatched`` — handler invocations via the subscriber
                                     (only non-zero for authoritative types)
      * ``subscriber_dedup_skipped`` — non-authoritative short-circuits
      * ``authoritative``      — current toggle state (bool)
      * ``workers`` / ``queued`` / ``queued_at_peak`` / ``handler_errors``
                                — per-type subscriber pool diagnostics

    Heuristic "ready to flip":
      * ``emitted == subscriber_received`` (no shadow loss)
      * ``handler_errors == 0`` when re-running through the subscriber
      * No ``shadow.dropped_*`` climbing for this type under typical load
    """
    from backend.events import events_bus_stats
    stats = events_bus_stats()

    emit = stats.get("emit_counts_by_type", {})
    local = stats.get("local_dispatch_counts_by_type", {})
    sub = stats.get("subscriber", {}).get("by_type", {})
    authoritative_types = set(stats.get("authoritative", {}).get("types", []))

    # Go-bus per-type counts are only meaningful for events entering
    # via HTTP POST /emit (non-Python producers). Python uses direct
    # aioredis XADD, bypassing the HTTP handler, so those events don't
    # register here. Surface as ``go_http_accepted`` to make the
    # semantics explicit for the dashboard.
    go_stats = await _fetch_gobus_stats()
    go_by_type = go_stats.get("emitted_by_type", {}) or {}
    go_overflow = go_stats.get("emitted_by_type_overflow", 0)

    all_types = sorted(set(emit) | set(local) | set(sub) | set(go_by_type))
    rows = []
    for t in all_types:
        sub_row = sub.get(t, {})
        py_emit = emit.get(t, 0)
        rows.append({
            "type": t,
            "authoritative": t in authoritative_types,
            "emitted": py_emit,
            # Events of this type accepted via Go bus HTTP /emit. Zero
            # for types emitted from Python (direct XADD path).
            "go_http_accepted": int(go_by_type.get(t, 0)),
            "local_dispatched": local.get(t, 0),
            "subscriber_received": sub_row.get("received", 0),
            "subscriber_dispatched": sub_row.get("dispatched", 0),
            "subscriber_dedup_skipped": sub_row.get("dedup_skipped", 0),
            # THE primary lost-event signal. For authoritative types,
            # the subscriber MUST see every emit; any persistent gap
            # indicates Redis-boundary loss (check ``shadow_dropped_*``).
            "emit_vs_received_gap": py_emit - sub_row.get("received", 0),
            "handler_errors": sub_row.get("handler_errors", 0),
            "workers": sub_row.get("workers", 0),
            "queued": sub_row.get("queued", 0),
            "queued_at_peak": sub_row.get("queued_at_peak", 0),
        })

    total_emitted = sum(emit.values())
    total_go_http_accepted = sum(int(v) for v in go_by_type.values())
    total_sub_received = sum(v.get("received", 0) for v in sub.values())
    summary = {
        "types_seen": len(all_types),
        "types_authoritative": len(authoritative_types),
        "total_emitted": total_emitted,
        "total_go_http_accepted": total_go_http_accepted,
        "total_subscriber_received": total_sub_received,
        # THE number to watch: Python-emit vs subscriber-received. For
        # a healthy system under steady state this should be 0 ± a
        # small in-flight band (events queued but not yet XREADGROUPed).
        "emit_minus_received": total_emitted - total_sub_received,
        "shadow_forwarded": stats.get("shadow", {}).get("forwarded", 0),
        "shadow_dropped_queue_full": stats.get("shadow", {}).get("dropped_queue_full", 0),
        # ``dropped_http_error`` is a legacy field name; under direct-
        # XADD it counts XADD failures (timeout / network / auth).
        "shadow_xadd_errors": stats.get("shadow", {}).get("dropped_http_error", 0),
        "go_type_overflow": go_overflow,
        "go_fetch_error": go_stats.get("error", ""),
    }
    return {"summary": summary, "by_type": rows}


@router.get("/event-map")
async def event_map(_rl=http_rate_limit("observability")):
    result: dict[str, list[str]] = {}
    for event_name, handler_list in _handler_registry.items():
        names = [
            getattr(fn, "__qualname__", getattr(fn, "__name__", "?"))
            for fn in handler_list
            if getattr(fn, "__qualname__", "") != "verbose"
        ]
        if names:
            result[event_name] = names
    return result
