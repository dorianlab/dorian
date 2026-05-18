"""
dorian/observability/collector.py
----------------------------------
In-memory metrics collector for handler invocations and pipeline executions.

Stores structured records in bounded ring buffers (deques) and exposes
query methods that aggregate over a configurable time window, optionally
filtered by user id.

Thread-safe: all mutations go through a threading.Lock because handler
instrumentation may run from asyncio.to_thread workers.

Usage:
    from dorian.observability.collector import collector

    collector.record_handler(...)
    stats = collector.get_handler_stats(since_s=300, uid=None)
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

# ── CPU sampler state ──────────────────────────────────────────────────
# psutil.Process.cpu_percent() returns the % used since the LAST call to
# cpu_percent() on the same Process object (or 0.0 on the first call).
# Creating a fresh Process every request means every call returns 0.0.
# A module-level Process object keeps the reference point across requests.
# First call primes it with interval=None (returns 0.0, starts the clock);
# subsequent get_system_snapshot calls see the interval elapsed since the
# prior call and return a real percentage.
try:
    import psutil as _psutil
    _cpu_process = _psutil.Process()
    _cpu_process.cpu_percent(interval=None)  # prime — first result is always 0.0
except Exception:
    _cpu_process = None


# -----------------------------------------------------------------------
# Record types
# -----------------------------------------------------------------------
@dataclass(slots=True)
class HandlerRecord:
    fn_name: str
    event_type: str
    wall_s: float
    rss_mb: float
    delta_mb: float
    error: bool
    error_msg: str | None
    ts: float                    # time.time()
    uid: str | None = None
    session: str | None = None


@dataclass(slots=True)
class PipelineRecord:
    run_id: str
    uid: str
    session: str
    status: str                  # running | completed | failed | cancelled
    start_ts: float
    end_ts: float | None = None
    node_count: int = 0
    error: str | None = None
    # --- extended metadata ---
    stage: str = ""              # parse | validation | expansion | graph_build | execution
    trace: str | None = None     # full traceback for failures
    source: str = ""             # "user" | "rl" — who triggered the run
    pipeline_id: str | None = None  # pipeline UUID (from session meta)
    node_types: str | None = None   # comma-separated operator FQNs in the pipeline
    failed_node: str | None = None  # node_id of the first node that failed
    duration_s: float | None = None # pre-computed duration


@dataclass(slots=True)
class WorkerMetricsRecord:
    ts: float
    cpu_percent: float
    ram_used: int
    ram_total: int
    ram_percent: float
    disk_used: int
    disk_total: int
    disk_percent: float
    dask_workers: int
    dask_processing: int


@dataclass
class HandlerStats:
    fn_name: str
    event_type: str
    count: int = 0
    total_wall_s: float = 0.0
    avg_wall_s: float = 0.0
    max_wall_s: float = 0.0
    error_count: int = 0
    error_rate: float = 0.0
    last_errors: list[str] = field(default_factory=list)


# -----------------------------------------------------------------------
# Collector
# -----------------------------------------------------------------------
_HANDLER_BUFFER = 5_000
_PIPELINE_BUFFER = 2_000   # raised to hold RL batch history
_WORKER_BUFFER = 1_000


class MetricsCollector:
    """Singleton-style collector — instantiated once at module level."""

    def __init__(
        self,
        handler_maxlen: int = _HANDLER_BUFFER,
        pipeline_maxlen: int = _PIPELINE_BUFFER,
        worker_maxlen: int = _WORKER_BUFFER,
    ) -> None:
        self._lock = threading.Lock()
        self._handler_records: deque[HandlerRecord] = deque(maxlen=handler_maxlen)
        self._pipeline_records: deque[PipelineRecord] = deque(maxlen=pipeline_maxlen)
        self._worker_records: deque[WorkerMetricsRecord] = deque(maxlen=worker_maxlen)

    # -------------------------------------------------------------------
    # Mutation
    # -------------------------------------------------------------------
    def record_handler(
        self,
        fn_name: str,
        event_type: str,
        wall_s: float,
        rss_mb: float,
        delta_mb: float,
        error: bool,
        error_msg: str | None = None,
        uid: str | None = None,
        session: str | None = None,
    ) -> None:
        rec = HandlerRecord(
            fn_name=fn_name,
            event_type=event_type,
            wall_s=wall_s,
            rss_mb=rss_mb,
            delta_mb=delta_mb,
            error=error,
            error_msg=error_msg,
            ts=time.time(),
            uid=uid,
            session=session,
        )
        with self._lock:
            self._handler_records.append(rec)

    def record_pipeline(
        self,
        run_id: str,
        uid: str,
        session: str,
        status: str,
        start_ts: float,
        end_ts: float | None = None,
        node_count: int = 0,
        error: str | None = None,
        *,
        stage: str = "",
        trace: str | None = None,
        source: str = "",
        pipeline_id: str | None = None,
        node_types: str | None = None,
        failed_node: str | None = None,
    ) -> None:
        # duration is only well-defined once the run is finished, i.e. when
        # a real end_ts is provided. Running pipelines (end_ts is None) keep
        # duration_s=None so the UI renders "-" instead of a fake "0.0s".
        duration_s = (
            round(end_ts - start_ts, 3)
            if (end_ts is not None and start_ts)
            else None
        )
        rec = PipelineRecord(
            run_id=run_id,
            uid=uid,
            session=session,
            status=status,
            start_ts=start_ts,
            end_ts=end_ts,
            node_count=node_count,
            error=error,
            stage=stage,
            trace=trace,
            source=source,
            pipeline_id=pipeline_id,
            node_types=node_types,
            failed_node=failed_node,
            duration_s=duration_s,
        )
        with self._lock:
            # Upsert: if a record with the same run_id exists, replace it
            for i, existing in enumerate(self._pipeline_records):
                if existing.run_id == run_id:
                    self._pipeline_records[i] = rec
                    return
            self._pipeline_records.append(rec)

    def record_worker_metrics(self, data: dict[str, Any]) -> None:
        rec = WorkerMetricsRecord(
            ts=data.get("ts", time.time()),
            cpu_percent=data.get("cpu_percent", 0.0),
            ram_used=data.get("ram_used", 0),
            ram_total=data.get("ram_total", 0),
            ram_percent=data.get("ram_percent", 0.0),
            disk_used=data.get("disk_used", 0),
            disk_total=data.get("disk_total", 0),
            disk_percent=data.get("disk_percent", 0.0),
            dask_workers=data.get("dask_workers", 0),
            dask_processing=data.get("dask_processing", 0),
        )
        with self._lock:
            self._worker_records.append(rec)

    # -------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------
    def _filter_handlers(
        self, since_s: float, uid: str | None,
    ) -> list[HandlerRecord]:
        cutoff = time.time() - since_s
        with self._lock:
            records = list(self._handler_records)
        out = [r for r in records if r.ts >= cutoff]
        if uid:
            out = [r for r in out if r.uid == uid]
        return out

    def _filter_pipelines(
        self, since_s: float, uid: str | None,
    ) -> list[PipelineRecord]:
        cutoff = time.time() - since_s
        with self._lock:
            records = list(self._pipeline_records)
        out = [r for r in records if r.start_ts >= cutoff]
        if uid:
            out = [r for r in out if r.uid == uid]
        return out

    def get_handler_stats(
        self, since_s: float = 300, uid: str | None = None,
    ) -> list[HandlerStats]:
        """Aggregate handler records into per-(fn_name, event_type) stats."""
        records = self._filter_handlers(since_s, uid)
        groups: dict[tuple[str, str], list[HandlerRecord]] = defaultdict(list)
        for r in records:
            groups[(r.fn_name, r.event_type)].append(r)

        stats: list[HandlerStats] = []
        for (fn, ev), recs in groups.items():
            count = len(recs)
            total = sum(r.wall_s for r in recs)
            errors = [r for r in recs if r.error]
            error_msgs = [r.error_msg for r in errors if r.error_msg]
            stats.append(HandlerStats(
                fn_name=fn,
                event_type=ev,
                count=count,
                total_wall_s=round(total, 4),
                avg_wall_s=round(total / count, 4) if count else 0.0,
                max_wall_s=round(max((r.wall_s for r in recs), default=0.0), 4),
                error_count=len(errors),
                error_rate=round(len(errors) / count, 4) if count else 0.0,
                last_errors=error_msgs[-5:],
            ))
        return sorted(stats, key=lambda s: s.total_wall_s, reverse=True)

    def get_pipeline_stats(
        self, since_s: float = 300, uid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return pipeline records as serialisable dicts."""
        records = self._filter_pipelines(since_s, uid)
        return [
            {
                "run_id": r.run_id,
                "uid": r.uid,
                "session": r.session,
                "status": r.status,
                "start_ts": r.start_ts,
                "end_ts": r.end_ts,
                "duration_s": r.duration_s if r.duration_s is not None else (
                    round(r.end_ts - r.start_ts, 3) if r.end_ts else None
                ),
                "node_count": r.node_count,
                "error": r.error,
                "stage": r.stage or None,
                "trace": r.trace,
                "source": r.source or None,
                "pipeline_id": r.pipeline_id,
                "node_types": r.node_types,
                "failed_node": r.failed_node,
            }
            for r in sorted(records, key=lambda r: r.start_ts, reverse=True)
        ]

    def get_event_throughput(
        self,
        bucket_s: float = 10,
        since_s: float = 300,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return events-per-second time series, bucketed."""
        records = self._filter_handlers(since_s, uid)
        if not records:
            return []

        now = time.time()
        start = now - since_s
        buckets: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for r in records:
            idx = int((r.ts - start) / bucket_s)
            buckets[idx][r.event_type] += 1

        num_buckets = int(since_s / bucket_s) + 1
        result: list[dict[str, Any]] = []
        for i in range(num_buckets):
            bucket_start = start + i * bucket_s
            entry: dict[str, Any] = {
                "ts": round(bucket_start, 1),
                "total": 0,
            }
            if i in buckets:
                for ev, cnt in buckets[i].items():
                    entry[ev] = cnt
                    entry["total"] += cnt
            result.append(entry)
        return result

    def get_error_summary(
        self, since_s: float = 300, uid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Group errors by handler, sorted by count descending."""
        records = self._filter_handlers(since_s, uid)
        errors = [r for r in records if r.error]

        groups: dict[str, list[HandlerRecord]] = defaultdict(list)
        for r in errors:
            groups[r.fn_name].append(r)

        summary: list[dict[str, Any]] = []
        for fn, recs in groups.items():
            # find total invocations for this handler (not just errors)
            all_for_fn = [r for r in records if r.fn_name == fn]
            unique_msgs = list(dict.fromkeys(
                r.error_msg for r in recs if r.error_msg
            ))
            summary.append({
                "fn_name": fn,
                "error_count": len(recs),
                "total_count": len(all_for_fn),
                "error_rate": round(len(recs) / len(all_for_fn), 4) if all_for_fn else 0.0,
                "event_types": list(set(r.event_type for r in recs)),
                "last_errors": unique_msgs[-5:],
            })
        return sorted(summary, key=lambda s: s["error_count"], reverse=True)

    def get_worker_metrics(
        self, since_s: float = 300, bucket_s: float = 10,
    ) -> list[dict[str, Any]]:
        """Return worker host metrics time series, bucketed by interval."""
        cutoff = time.time() - since_s
        with self._lock:
            records = [r for r in self._worker_records if r.ts >= cutoff]

        if not records:
            return []

        start = cutoff
        buckets: dict[int, list[WorkerMetricsRecord]] = defaultdict(list)
        for r in records:
            idx = int((r.ts - start) / bucket_s)
            buckets[idx].append(r)

        num_buckets = int(since_s / bucket_s) + 1
        result: list[dict[str, Any]] = []
        for i in range(num_buckets):
            if i not in buckets:
                continue
            recs = buckets[i]
            n = len(recs)
            result.append({
                "ts": round(start + i * bucket_s, 1),
                "cpu_percent": round(sum(r.cpu_percent for r in recs) / n, 1),
                "ram_percent": round(sum(r.ram_percent for r in recs) / n * 100, 1),
                "disk_percent": round(sum(r.disk_percent for r in recs) / n * 100, 1),
                "dask_workers": recs[-1].dask_workers,
                "dask_processing": max(r.dask_processing for r in recs),
            })
        return result

    def get_worker_latest(self) -> dict[str, Any] | None:
        """Return the most recent worker metrics snapshot, or None."""
        with self._lock:
            if not self._worker_records:
                return None
            r = self._worker_records[-1]
        return {
            "ts": r.ts,
            "cpu_percent": r.cpu_percent,
            "ram_used_gb": round(r.ram_used / (1024 ** 3), 2),
            "ram_total_gb": round(r.ram_total / (1024 ** 3), 2),
            "ram_percent": round(r.ram_percent * 100, 1),
            "disk_used_gb": round(r.disk_used / (1024 ** 3), 2),
            "disk_total_gb": round(r.disk_total / (1024 ** 3), 2),
            "disk_percent": round(r.disk_percent * 100, 1),
            "dask_workers": r.dask_workers,
            "dask_processing": r.dask_processing,
        }

    def _disk_usage(self) -> list[dict[str, Any]]:
        """Best-effort disk-usage snapshot for the host's data partitions.

        Which paths to report is configurable via ``observability.disk_paths``
        in config.yaml; defaults cover the canonical deployment layout
        (``/scratch``) plus ``DORIAN_ROOT`` if set.

        Result is cached for 5s so repeated polls from the dashboard don't
        syscall on every tick. Missing paths are silently skipped; NFS
        paths that time out return a best-effort zero rather than blocking
        the whole snapshot.
        """
        import os
        import shutil
        now = time.time()
        cached = getattr(self, "_disk_cache", None)
        if cached and (now - cached[0]) < 5.0:
            return cached[1]

        try:
            from backend.config import config
            configured = list(getattr(config, "observability", None).disk_paths or [])
        except Exception:
            configured = []

        # Inside the container we see host disks as /host/<name> (bind-
        # mounted read-only by the compose override). Outside (dev),
        # we see them directly. Probe both forms. ``/app/data`` is the
        # named volume where uploaded datasets / cached results live,
        # so it's the most relevant disk metric for a dorian operator.
        # ``/`` falls back to the container's root partition (which on
        # overlayfs reflects the underlying host disk) so the dashboard
        # always renders at least one card even when nothing else is
        # mounted.
        defaults = [
            "/host/scratch",
            "/scratch",
            os.environ.get("DORIAN_ROOT") or "",
            "/app/data",
            "/",
        ]
        candidates: list[str] = []
        seen: set[str] = set()
        for p in list(configured) + defaults:
            if p and p not in seen and os.path.isdir(p):
                seen.add(p)
                candidates.append(p)

        out: list[dict[str, Any]] = []
        for path in candidates:
            # Dashboard label: strip the container-only /host prefix so
            # users see the logical mount name.
            display = path[len("/host"):] if path.startswith("/host/") else path
            try:
                u = shutil.disk_usage(path)
                out.append({
                    "path": display,
                    "total_gb": round(u.total / (1024 ** 3), 2),
                    "used_gb":  round(u.used  / (1024 ** 3), 2),
                    "free_gb":  round(u.free  / (1024 ** 3), 2),
                    "used_pct": round(u.used * 100.0 / u.total, 1) if u.total else 0.0,
                })
            except OSError:
                out.append({
                    "path": display,
                    "total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0, "used_pct": 0.0,
                    "error": "unreachable",
                })

        self._disk_cache = (now, out)
        return out

    def get_system_snapshot(self) -> dict[str, Any]:
        """Return current system-level metrics.

        Imported lazily to avoid circular imports at module level.
        """
        import psutil
        from backend.events import (
            _user_queue, _bg_queue, _workers, POOL_SIZE,
            USER_CAPACITY, BG_CAPACITY, events_bus_stats,
        )

        # Process-local CPU uses the module-level primed Process so the
        # interval-since-last-call is meaningful across HTTP requests.
        if _cpu_process is not None:
            try:
                cpu = _cpu_process.cpu_percent(interval=None)
            except Exception:
                cpu = 0.0
            try:
                rss = _cpu_process.memory_info().rss / (1024 ** 2)
            except Exception:
                rss = 0.0
        else:
            proc = psutil.Process()
            cpu = proc.cpu_percent(interval=0.1)  # fallback: 100ms sample
            rss = proc.memory_info().rss / (1024 ** 2)

        user_depth = _user_queue.qsize() if _user_queue is not None else 0
        bg_depth = _bg_queue.qsize() if _bg_queue is not None else 0
        bus = events_bus_stats()

        # RL pipeline inflight count — the actual "what's running" number
        # for this codebase. Dask distributed scheduler is almost always
        # idle because pipelines execute via dask.threaded.get, not through
        # the cluster; the RL semaphore is the authoritative gate.
        rl_limit = 0
        rl_inflight = 0
        try:
            from backend.queue import get_rl_concurrency
            r = get_rl_concurrency()
            rl_limit = int(r.get("limit", 0))
            rl_inflight = int(r.get("inflight", 0))
        except Exception:
            pass

        # Dask scheduler stats — retained for completeness but usually 0.
        # When DORIAN_USE_RUST_RUNNER=1 (default) ``executor`` is None
        # and the block is skipped entirely.
        dask_inflight = 0
        dask_workers = 0
        try:
            from backend.envs import executor
            if executor is not None:
                info = executor.scheduler_info()
                workers = info.get("workers", {})
                dask_workers = len(workers)
                dask_inflight = sum(
                    len(w.get("processing", {})) for w in workers.values()
                )
        except Exception:
            pass

        return {
            "cpu_percent": round(cpu, 1),
            "rss_mb": round(rss, 1),
            "event_bus": {
                "pool_size": POOL_SIZE,
                "active_workers": len(_workers),
                # Preserve legacy single-queue field names for existing
                # frontend widgets while we're transitioning: ``queue_depth``
                # and ``queue_capacity`` report the USER lane (what a human
                # is waiting on). New fields expose the full two-lane state.
                "queue_depth": user_depth,
                "queue_capacity": USER_CAPACITY,
                "user_queue": {"size": user_depth, "capacity": USER_CAPACITY},
                "bg_queue": {"size": bg_depth, "capacity": BG_CAPACITY},
                "drops_by_reason": bus.get("drops_by_reason", {}),
                "enqueues_by_lane": bus.get("enqueues_by_lane", {}),
            },
            "rl": {
                "inflight": rl_inflight,
                "limit": rl_limit,
            },
            "dask": {
                "workers": dask_workers,
                "inflight": dask_inflight,
            },
            "disk": self._disk_usage(),
            "ts": time.time(),
        }

    def get_pipeline_duration_estimate(self, since_s: float = 1800) -> dict[str, Any]:
        """Return pipeline duration statistics for time estimation.

        Used by the queue system to estimate wait times for queued users.
        Returns median, p75, p95 durations from completed pipelines.
        """
        records = self._filter_pipelines(since_s, uid=None)
        durations = [
            r.end_ts - r.start_ts
            for r in records
            if r.status == "completed" and r.end_ts is not None
        ]

        if not durations:
            return {
                "sample_size": 0,
                "median_s": 30.0,
                "p75_s": 45.0,
                "p95_s": 120.0,
            }

        durations.sort()
        n = len(durations)

        def percentile(pct: float) -> float:
            idx = int(n * pct / 100)
            return round(durations[min(idx, n - 1)], 2)

        return {
            "sample_size": n,
            "median_s": percentile(50),
            "p75_s": percentile(75),
            "p95_s": percentile(95),
            "min_s": round(durations[0], 2),
            "max_s": round(durations[-1], 2),
            "mean_s": round(sum(durations) / n, 2),
        }


# Module-level singleton
collector = MetricsCollector()
