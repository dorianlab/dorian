"""
RL Pipeline Generator — generates pipeline candidates via reinforcement
learning and submits them for background execution.

Pipelines are submitted through the SAME execution path as end-users but at
BACKGROUND priority, so user and system traffic always takes precedence.

Datasets are processed small-to-large.  Before any generation starts, the
scheduler waits for all cross-product trials (deterministic evaluation of
every existing (pipeline, dataset) pair) to complete.

Usage::

    # Continuous mode — runs until Ctrl+C
    uv run python scripts/generate_pipelines.py

    # Single pass across all datasets
    uv run python scripts/generate_pipelines.py --once

    # Custom batch size and max rounds
    uv run python scripts/generate_pipelines.py --batch-size 20 --max-rounds 5

    # Single dataset (by docstore _id)
    uv run python scripts/generate_pipelines.py --dataset-id abc123 --batch-size 5

    # With seed for reproducibility
    uv run python scripts/generate_pipelines.py --seed 42
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

# Ensure project root is on sys.path for both direct and -m invocation
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graceful shutdown — Linux signals with Windows fallback
# ---------------------------------------------------------------------------

import contextlib
from typing import Callable


@contextlib.asynccontextmanager
async def _graceful_shutdown(cancel_fn: Callable[[], None]):
    """Register signal handlers for graceful cancellation.

    On Linux/macOS the loop's ``add_signal_handler`` wires SIGINT/SIGTERM
    directly to *cancel_fn* so the scheduler finishes its current batch
    before exiting.

    On Windows ``add_signal_handler`` is not implemented — the fallback
    lets ``KeyboardInterrupt`` propagate naturally (caught by the caller's
    ``try/except`` or ``asyncio.run``'s own handler).
    """
    loop = asyncio.get_running_loop()
    registered: list[signal.Signals] = []

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, cancel_fn)
            registered.append(sig)
        except NotImplementedError:
            # Windows — signal handlers not supported on ProactorEventLoop
            pass

    if not registered:
        _log.debug("Signal handlers unavailable (Windows); Ctrl+C will raise KeyboardInterrupt.")

    try:
        yield
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)


async def _run_single_dataset(args):
    """Generate pipelines for a single dataset."""
    import numpy as np
    from backend.envs import expdb
    from dorian.experiment.store import init_experiment_store
    from dorian.pipeline.generation.engine import GenerationEngine

    # Initialize ExperimentStore (Postgres + indices)
    await init_experiment_store()

    # Fetch dataset — TEXT _id in the Postgres document store.
    doc = await expdb.datasets.find_one({"_id": args.dataset_id})
    if not doc:
        _log.error("Dataset %s not found.", args.dataset_id)
        sys.exit(1)

    did = str(doc["_id"])
    task_info = doc.get("task")
    task = None
    if isinstance(task_info, dict):
        task = task_info.get("type", "").capitalize() or None

    # Build metafeature vector
    profile = doc.get("profile")
    if profile:
        try:
            from dorian.experiment.kdtree import profile_to_vector
            metafeatures = profile_to_vector(profile).astype(np.float32)
        except Exception:
            metafeatures = None
    else:
        metafeatures = None

    _log.info(
        "Generating %d pipelines for dataset '%s' (%s, %d rows, task=%s)...",
        args.batch_size, doc.get("name", did), did[:8],
        doc.get("itemCount", 0), task,
    )

    engine = GenerationEngine(
        task=task,
        metafeatures=metafeatures,
        max_steps=15,
        seed=args.seed,
        mode="model_free",
    )

    submitted = await engine.generate_and_submit(
        dataset_id=did,
        n=args.batch_size,
        metafeatures=metafeatures,
        session=f"rl:cli:{did[:8]}",
        source="rl_generator",
    )

    _log.info("Done. %d pipelines submitted for execution.", len(submitted))
    for pid in submitted:
        _log.info("  pipeline_id: %s", pid)


async def _run_scheduler(args):
    """Run the generation scheduler (continuous or single pass)."""
    from dorian.experiment.store import init_experiment_store
    from dorian.pipeline.generation.scheduler import GenerationScheduler

    # Initialize ExperimentStore (Postgres + indices)
    await init_experiment_store()

    scheduler = GenerationScheduler(
        batch_size=args.batch_size,
        cooldown=args.cooldown,
        max_rounds=args.max_rounds,
        seed=args.seed,
    )

    async with _graceful_shutdown(scheduler.cancel):
        if args.once:
            total = await scheduler.run_once()
            _log.info("Single pass complete. %d pipelines submitted.", total)
        else:
            await scheduler.run()


async def async_main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="RL Pipeline Generator — generate and execute pipeline candidates",
    )
    parser.add_argument(
        "--batch-size", type=int, default=10,
        help="Pipelines to generate per dataset per round (default: 10)",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=None,
        help="Max rounds across all datasets (default: unlimited)",
    )
    parser.add_argument(
        "--cooldown", type=float, default=30.0,
        help="Seconds between batches (default: 30)",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single pass across all datasets then exit",
    )
    parser.add_argument(
        "--dataset-id", type=str, default=None,
        help="Generate pipelines for a single dataset (docstore _id)",
    )
    args = parser.parse_args()

    # Start the event bus workers (needed for aemit)
    from backend.events import start_workers, stop_workers
    await start_workers()

    # Standalone mode: execute pipelines directly (no queue bridge running)
    from dorian.pipeline.generation.executor import set_standalone_mode
    set_standalone_mode(True)

    try:
        if args.dataset_id:
            await _run_single_dataset(args)
        else:
            await _run_scheduler(args)
    finally:
        await stop_workers()

        # Cleanup
        from dorian.experiment.store import shutdown_experiment_store
        from backend.envs import close_pg_pool
        await shutdown_experiment_store()
        await close_pg_pool()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
