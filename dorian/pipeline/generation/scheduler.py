"""GenerationScheduler — background continuous pipeline generation.

The scheduler runs as a long-lived async task that:

1. Waits for cross-product trials to complete (all (pipeline, dataset) pairs
   evaluated) before starting any generation.
2. Loads public datasets from the docstore sorted by itemCount ascending (small first).
3. For each dataset, generates a batch of pipelines via GenerationEngine.
4. Pipelines are submitted at BACKGROUND priority — user and system traffic
   always takes precedence.
5. After each batch, checks for new datasets/pipelines and schedules any
   missing cross-product trials before continuing.

Usage::

    scheduler = GenerationScheduler()
    await scheduler.run()              # blocking — runs until cancelled
    await scheduler.run_once()         # single pass across all datasets

Integration:
    The scheduler is started by the CLI (``scripts/generate_pipelines.py``)
    or can be embedded in the FastAPI lifespan for always-on generation.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone

import numpy as np

from backend.events import Event, aemit

_log = logging.getLogger(__name__)

# Defaults
DEFAULT_BATCH_SIZE = 10
DEFAULT_COOLDOWN_SECONDS = 30
DEFAULT_TRIAL_POLL_SECONDS = 10


class GenerationScheduler:
    """Background scheduler for continuous pipeline generation.

    Parameters
    ----------
    batch_size : int
        Pipelines to generate per dataset per round.
    cooldown : float
        Seconds to wait between batches (prevents resource starvation).
    max_rounds : int or None
        Maximum rounds across all datasets (None = unlimited).
    seed : int or None
        Base seed for reproducibility.
    """

    def __init__(
        self,
        batch_size: int = DEFAULT_BATCH_SIZE,
        cooldown: float = DEFAULT_COOLDOWN_SECONDS,
        max_rounds: int | None = None,
        seed: int | None = None,
    ):
        self.batch_size = batch_size
        self.cooldown = cooldown
        self.max_rounds = max_rounds
        self.seed = seed
        self._round = 0
        self._total_submitted = 0
        self._cancelled = False

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run continuous generation until cancelled or max_rounds reached."""
        _log.info(
            "Generation scheduler starting (batch=%d, cooldown=%.0fs, max_rounds=%s).",
            self.batch_size, self.cooldown,
            self.max_rounds if self.max_rounds else "unlimited",
        )

        await aemit(Event("GenerationSchedulerStarted", {
            "batch_size": self.batch_size,
            "cooldown": self.cooldown,
            "max_rounds": self.max_rounds,
        }))

        while not self._cancelled:
            if self.max_rounds and self._round >= self.max_rounds:
                _log.info("Max rounds (%d) reached. Stopping.", self.max_rounds)
                break

            # Wait for cross-product trials to complete
            await self._wait_for_trials()

            # ── Adaptive backoff: watch the RL execution gate ───────────
            # If the gate is saturated, a batch we submit now just piles
            # into the Redis task queue and amplifies the event-bus
            # pressure without any throughput gain. Poll until inflight
            # drops below a fraction of the limit, then run the batch.
            await self._wait_for_headroom()

            # Run one pass
            submitted = await self.run_once()
            self._round += 1
            self._total_submitted += submitted

            if submitted == 0:
                _log.info("No pipelines generated this round. Sleeping longer.")
                await asyncio.sleep(self.cooldown * 3)
            else:
                await asyncio.sleep(self.cooldown)

        await aemit(Event("GenerationSchedulerStopped", {
            "rounds": self._round,
            "total_submitted": self._total_submitted,
        }))

    async def run_once(self) -> int:
        """Single pass: generate pipelines for all available datasets.

        Returns total number of pipelines submitted.
        """
        datasets = await self._load_datasets()
        if not datasets:
            _log.info("No datasets available for generation.")
            return 0

        _log.info(
            "Round %d: %d datasets available (sorted by size ascending).",
            self._round, len(datasets),
        )

        total = 0

        for ds in datasets:
            if self._cancelled:
                break

            did = str(ds["_id"])
            name = ds.get("name", did)
            task_info = ds.get("task")
            task = None
            if isinstance(task_info, dict):
                task = task_info.get("type", "").capitalize() or None

            # Build metafeature vector from profile
            metafeatures = self._profile_to_vector(ds.get("profile"))

            session = f"rl:round-{self._round}:{did[:8]}"

            _log.info(
                "  Dataset '%s' (%s, %d rows) — generating %d pipelines...",
                name, did[:8], ds.get("itemCount", 0), self.batch_size,
            )

            try:
                from dorian.pipeline.generation.engine import GenerationEngine

                engine = GenerationEngine(
                    task=task,
                    metafeatures=metafeatures,
                    max_steps=15,
                    seed=(self.seed + self._round * 1000 + total) if self.seed else None,
                    mode="model_free",
                )

                submitted = await engine.generate_and_submit(
                    dataset_id=did,
                    n=self.batch_size,
                    metafeatures=metafeatures,
                    session=session,
                    source="rl_generator",
                )
                total += len(submitted)

                await aemit(Event("GenerationBatchCompleted", {
                    "dataset_id": did,
                    "dataset_name": name,
                    "round": self._round,
                    "generated": len(submitted),
                }))

            except Exception:
                _log.error(
                    "  Generation failed for dataset %s: %s",
                    did, traceback.format_exc(),
                )
                await aemit(Event("GenerationBatchFailed", {
                    "dataset_id": did,
                    "error": traceback.format_exc(),
                }))

        _log.info(
            "Round %d complete: %d pipelines submitted across %d datasets.",
            self._round, total, len(datasets),
        )
        return total

    def cancel(self) -> None:
        """Signal the scheduler to stop after the current batch."""
        self._cancelled = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load_datasets(self) -> list[dict]:
        """Load public datasets from the docstore sorted by itemCount ascending."""
        from backend.envs import expdb

        cursor = expdb.datasets.find(
            {"isPublic": True, "profile": {"$ne": None}},
            projection={
                "_id": 1, "name": 1, "itemCount": 1, "task": 1,
                "profile": 1, "columns": 1,
            },
        ).sort("itemCount", 1).limit(500)

        return [doc async for doc in cursor]

    async def _wait_for_headroom(
        self,
        high_watermark: float = 0.9,
        poll_every: float = 2.0,
        max_wait: float = 300.0,
    ) -> None:
        """Block while the RL execution gate is saturated.

        The execution side is capped by the semaphore behind
        ``get_rl_concurrency()`` (``RL_MAX_CONCURRENT``). When inflight
        is at or near the limit, any pipelines we submit now just queue
        up in Redis and amplify event-bus pressure without throughput
        gain. Wait until inflight drops below ``high_watermark * limit``
        before letting the scheduler enqueue another batch. This is the
        missing feedback loop that makes the scheduler actually elastic.

        If we can't read the gate (env mismatch, import error) or the
        limit is zero, skip the wait — the static scheduler cooldown is
        still in place as a safety net.

        ``max_wait`` caps how long we block so a permanently-jammed
        executor doesn't halt the scheduler forever; after the cap we
        let the batch run and rely on the gate itself to absorb.
        """
        try:
            from backend.queue import get_rl_concurrency
        except Exception:
            return

        import time
        deadline = time.monotonic() + max_wait
        first_check = True

        while not self._cancelled:
            try:
                snap = get_rl_concurrency()
            except Exception:
                return
            limit = int(snap.get("limit") or 0)
            inflight = int(snap.get("inflight") or 0)
            if limit <= 0:
                return
            if inflight < int(limit * high_watermark):
                if not first_check:
                    _log.info(
                        "Scheduler resuming — RL gate headroom restored "
                        "(inflight=%d / limit=%d).", inflight, limit,
                    )
                return
            if time.monotonic() >= deadline:
                _log.warning(
                    "Headroom wait timed out after %.0fs — proceeding "
                    "despite saturated gate (inflight=%d / limit=%d).",
                    max_wait, inflight, limit,
                )
                return
            if first_check:
                _log.info(
                    "Scheduler pausing — RL gate saturated "
                    "(inflight=%d / limit=%d, watermark=%.0f%%).",
                    inflight, limit, high_watermark * 100,
                )
                await aemit(Event("GenerationSchedulerBackpressure", {
                    "inflight": inflight,
                    "limit": limit,
                    "watermark": high_watermark,
                }))
                first_check = False
            await asyncio.sleep(poll_every)

    async def _wait_for_trials(self, timeout: float = 120) -> None:
        """Wait for cross-product trials to complete, with timeout.

        Parameters
        ----------
        timeout : float
            Maximum seconds to wait.  If trials are still pending after this
            duration, proceed anyway — the scheduler should not block forever
            when no execution workers are consuming background tasks.
        """
        try:
            from dorian.experiment.trials import get_pending_trial_count
            import time

            deadline = time.monotonic() + timeout
            first_check = True

            while not self._cancelled:
                pending = await get_pending_trial_count()
                if pending == 0:
                    if not first_check:
                        _log.info("All cross-product trials complete.")
                    return

                elapsed = time.monotonic() - (deadline - timeout)
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    _log.warning(
                        "Timeout after %.0fs waiting for %d pending trials. "
                        "Proceeding with generation (trials may still be running).",
                        timeout, pending,
                    )
                    return

                _log.info(
                    "Waiting for %d cross-product trials to complete "
                    "(%.0fs elapsed, %.0fs until timeout)...",
                    pending, elapsed, remaining,
                )
                first_check = False
                await asyncio.sleep(min(DEFAULT_TRIAL_POLL_SECONDS, remaining))

        except Exception:
            # If trials module fails (e.g. no Postgres), proceed anyway
            _log.warning(
                "Could not check pending trials: %s", traceback.format_exc(),
            )

    @staticmethod
    def _profile_to_vector(profile: dict | None) -> np.ndarray:
        """Convert a profile dict to the numpy vector expected by PipelineGenEnv."""
        if not profile:
            return np.zeros(48, dtype=np.float32)

        try:
            from dorian.experiment.kdtree import profile_to_vector
            vec = profile_to_vector(profile)
            return vec.astype(np.float32)
        except Exception:
            return np.zeros(48, dtype=np.float32)
