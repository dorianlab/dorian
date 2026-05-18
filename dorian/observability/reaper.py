"""
dorian/observability/reaper.py
-------------------------------
Periodic cleanup of stale pipeline result files.

Results larger than the inline limit are pickled to DORIAN_RESULT_DIR
(default /tmp/dorian_results).  Redis references to them expire after
result_ttl seconds, but the files themselves remain.  This reaper
deletes orphaned files whose mtime exceeds the TTL.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from backend.events import Event, aemit

_task: asyncio.Task | None = None


async def _reap_loop(result_dir: Path, ttl_s: int, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            now = time.time()
            removed = 0
            empty_dirs: list[Path] = []

            for pkl in result_dir.glob("*/*.pkl"):
                try:
                    if (now - pkl.stat().st_mtime) > ttl_s:
                        pkl.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    pass

            # Clean up empty run directories.
            for d in result_dir.iterdir():
                if d.is_dir():
                    try:
                        d.rmdir()  # only succeeds if empty
                    except OSError:
                        pass

            if removed:
                await aemit(Event("ResultsReaped", {"removed": removed}))
        except Exception:
            pass  # best-effort, never crash the loop


async def start_reaper(
    result_dir: str | Path = "/tmp/dorian_results",
    ttl_s: int = 86400,
    interval_s: float = 3600,
) -> None:
    """Start the background reaper task.

    Args:
        result_dir: Directory containing run_id/node_id.pkl files.
        ttl_s: Max file age in seconds (default 24h, matches result_ttl).
        interval_s: Seconds between sweeps (default 1h).
    """
    global _task
    if _task is not None and not _task.done():
        return
    rd = Path(result_dir)
    _task = asyncio.create_task(
        _reap_loop(rd, ttl_s, interval_s), name="result-reaper"
    )


async def stop_reaper() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
