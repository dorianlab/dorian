"""Tests for the observability collector and result reaper.

Covers:
  - Handler record ingestion and aggregation
  - Pipeline record upsert
  - Worker metrics recording and bucketed retrieval
  - Event throughput bucketing
  - Error summary grouping
  - Result reaper file cleanup
"""
import asyncio
import tempfile
import time
from pathlib import Path

from dorian.observability.collector import MetricsCollector


# ---------------------------------------------------------------------------
# Handler stats
# ---------------------------------------------------------------------------

class TestHandlerStats:

    def test_record_and_retrieve(self):
        c = MetricsCollector()
        c.record_handler(
            fn_name="handle_feedback",
            event_type="FeedbackReceived",
            wall_s=0.123,
            rss_mb=200.0,
            delta_mb=1.5,
            error=False,
        )
        stats = c.get_handler_stats(since_s=60)
        assert len(stats) == 1
        assert stats[0].fn_name == "handle_feedback"
        assert stats[0].count == 1
        assert stats[0].avg_wall_s == 0.123
        assert stats[0].error_count == 0

    def test_error_stats(self):
        c = MetricsCollector()
        c.record_handler("fn", "Ev", 0.1, 100, 0, error=True, error_msg="boom")
        c.record_handler("fn", "Ev", 0.1, 100, 0, error=False)

        stats = c.get_handler_stats(since_s=60)
        assert stats[0].error_count == 1
        assert stats[0].error_rate == 0.5
        assert stats[0].last_errors == ["boom"]

    def test_multiple_handlers_sorted_by_total_wall(self):
        c = MetricsCollector()
        c.record_handler("fast", "Ev", 0.01, 100, 0, error=False)
        c.record_handler("slow", "Ev", 1.0, 100, 0, error=False)

        stats = c.get_handler_stats(since_s=60)
        assert stats[0].fn_name == "slow"
        assert stats[1].fn_name == "fast"

    def test_uid_filter(self):
        c = MetricsCollector()
        c.record_handler("fn", "Ev", 0.1, 100, 0, error=False, uid="alice")
        c.record_handler("fn", "Ev", 0.1, 100, 0, error=False, uid="bob")

        alice = c.get_handler_stats(since_s=60, uid="alice")
        assert len(alice) == 1
        assert alice[0].count == 1

    def test_since_filter(self):
        c = MetricsCollector()
        c.record_handler("fn", "Ev", 0.1, 100, 0, error=False)
        # A stat recorded now should appear for since=60 but not since=0.
        assert len(c.get_handler_stats(since_s=60)) == 1
        assert len(c.get_handler_stats(since_s=0)) == 0


# ---------------------------------------------------------------------------
# Pipeline stats
# ---------------------------------------------------------------------------

class TestPipelineStats:

    def test_record_and_retrieve(self):
        c = MetricsCollector()
        c.record_pipeline("run1", "u1", "s1", "running", start_ts=time.time())

        stats = c.get_pipeline_stats(since_s=60)
        assert len(stats) == 1
        assert stats[0]["run_id"] == "run1"
        assert stats[0]["status"] == "running"
        assert stats[0]["duration_s"] is None

    def test_upsert_replaces_existing(self):
        c = MetricsCollector()
        now = time.time()
        c.record_pipeline("run1", "u1", "s1", "running", start_ts=now)
        c.record_pipeline("run1", "u1", "s1", "completed", start_ts=now, end_ts=now + 5)

        stats = c.get_pipeline_stats(since_s=60)
        assert len(stats) == 1
        assert stats[0]["status"] == "completed"
        assert stats[0]["duration_s"] == 5.0

    def test_error_pipeline(self):
        c = MetricsCollector()
        now = time.time()
        c.record_pipeline("run2", "u1", "s1", "failed", start_ts=now, error="OOM")

        stats = c.get_pipeline_stats(since_s=60)
        assert stats[0]["error"] == "OOM"


# ---------------------------------------------------------------------------
# Worker metrics
# ---------------------------------------------------------------------------

class TestWorkerMetrics:

    def test_record_and_latest(self):
        c = MetricsCollector()
        c.record_worker_metrics({
            "ts": time.time(),
            "cpu_percent": 45.2,
            "ram_used": 8 * 1024**3,
            "ram_total": 16 * 1024**3,
            "ram_percent": 0.5,
            "disk_used": 100 * 1024**3,
            "disk_total": 500 * 1024**3,
            "disk_percent": 0.2,
            "dask_workers": 4,
            "dask_processing": 2,
        })

        latest = c.get_worker_latest()
        assert latest is not None
        assert latest["cpu_percent"] == 45.2
        assert latest["ram_total_gb"] == 16.0
        assert latest["dask_workers"] == 4

    def test_empty_latest(self):
        c = MetricsCollector()
        assert c.get_worker_latest() is None

    def test_time_series_bucketing(self):
        c = MetricsCollector()
        now = time.time()
        for i in range(5):
            c.record_worker_metrics({
                "ts": now - (4 - i),
                "cpu_percent": 10 * (i + 1),
                "ram_used": 0, "ram_total": 1, "ram_percent": 0.0,
                "disk_used": 0, "disk_total": 1, "disk_percent": 0.0,
                "dask_workers": 1, "dask_processing": 0,
            })

        ts = c.get_worker_metrics(since_s=10, bucket_s=5)
        assert len(ts) >= 1  # at least one non-empty bucket


# ---------------------------------------------------------------------------
# Event throughput
# ---------------------------------------------------------------------------

class TestEventThroughput:

    def test_empty_returns_empty(self):
        c = MetricsCollector()
        assert c.get_event_throughput(since_s=60) == []

    def test_buckets_created(self):
        c = MetricsCollector()
        c.record_handler("fn", "EvA", 0.1, 100, 0, error=False)
        c.record_handler("fn", "EvB", 0.1, 100, 0, error=False)

        buckets = c.get_event_throughput(bucket_s=10, since_s=60)
        total = sum(b["total"] for b in buckets)
        assert total == 2


# ---------------------------------------------------------------------------
# Error summary
# ---------------------------------------------------------------------------

class TestErrorSummary:

    def test_groups_by_handler(self):
        c = MetricsCollector()
        c.record_handler("fn_a", "Ev", 0.1, 100, 0, error=True, error_msg="err1")
        c.record_handler("fn_a", "Ev", 0.1, 100, 0, error=True, error_msg="err2")
        c.record_handler("fn_b", "Ev", 0.1, 100, 0, error=True, error_msg="err3")

        summary = c.get_error_summary(since_s=60)
        assert len(summary) == 2
        # Sorted by error count desc.
        assert summary[0]["fn_name"] == "fn_a"
        assert summary[0]["error_count"] == 2
        assert summary[1]["fn_name"] == "fn_b"

    def test_no_errors_returns_empty(self):
        c = MetricsCollector()
        c.record_handler("fn", "Ev", 0.1, 100, 0, error=False)
        assert c.get_error_summary(since_s=60) == []


# ---------------------------------------------------------------------------
# Result reaper
# ---------------------------------------------------------------------------

class TestResultReaper:

    def test_reaps_old_files(self):
        import os
        from dorian.observability.reaper import _reap_loop

        async def _test():
            with tempfile.TemporaryDirectory() as tmpdir:
                run_dir = Path(tmpdir) / "run1"
                run_dir.mkdir()

                old_file = run_dir / "node1.pkl"
                old_file.write_bytes(b"stale data")
                # Backdate mtime by 2 days.
                old_time = time.time() - 172800
                os.utime(old_file, (old_time, old_time))

                fresh_file = run_dir / "node2.pkl"
                fresh_file.write_bytes(b"fresh data")

                # Create a task that runs one sweep then we cancel it.
                task = asyncio.create_task(_reap_loop(Path(tmpdir), ttl_s=86400, interval_s=0.01))
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                assert not old_file.exists(), "Stale file should be removed"
                assert fresh_file.exists(), "Fresh file should be kept"

        asyncio.run(_test())

    def test_cleans_empty_dirs(self):
        import os
        from dorian.observability.reaper import _reap_loop

        async def _test():
            with tempfile.TemporaryDirectory() as tmpdir:
                empty_dir = Path(tmpdir) / "run_empty"
                empty_dir.mkdir()

                task = asyncio.create_task(_reap_loop(Path(tmpdir), ttl_s=86400, interval_s=0.01))
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                assert not empty_dir.exists(), "Empty run dir should be removed"

        asyncio.run(_test())
