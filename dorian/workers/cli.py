"""CLI entry point for the worker supervisor.

Usage (from repo root):
    uv run python -m dorian.workers                          # uses Dorian config if available
    uv run python -m dorian.workers --scheduler tcp://host:8786 --max-workers 4
    DORIAN_WORKERS_MAX=4 uv run python -m dorian.workers     # env-var config
"""

from __future__ import annotations

import asyncio
import signal

import click

from dorian.workers.config import WorkerConfig


@click.command()
@click.option("--scheduler", default=None, help="Dask scheduler address (tcp://host:port)")
@click.option("--min-workers", default=None, type=int, help="Minimum worker count")
@click.option("--max-workers", default=None, type=int, help="Maximum worker count")
@click.option("--cpu-high", default=None, type=float, help="CPU scale-up watermark (0..1)")
@click.option("--cpu-low", default=None, type=float, help="CPU scale-down watermark (0..1)")
@click.option("--ram-high", default=None, type=float, help="RAM scale-up watermark (0..1)")
@click.option("--ram-low", default=None, type=float, help="RAM scale-down watermark (0..1)")
@click.option("--cooldown", default=None, type=int, help="Cooldown between scale actions (seconds)")
@click.option("--standalone", is_flag=True, help="Skip Dorian config, use env vars only")
def main(
    scheduler: str | None,
    min_workers: int | None,
    max_workers: int | None,
    cpu_high: float | None,
    cpu_low: float | None,
    ram_high: float | None,
    ram_low: float | None,
    cooldown: int | None,
    standalone: bool,
) -> None:
    """Start the Dorian worker supervisor."""
    if standalone:
        cfg = WorkerConfig.from_env()
    else:
        cfg = WorkerConfig.from_dorian_config()

    # CLI overrides take precedence.
    overrides = {}
    if scheduler is not None:
        overrides["scheduler_address"] = scheduler
    if min_workers is not None:
        overrides["min_workers"] = min_workers
    if max_workers is not None:
        overrides["max_workers"] = max_workers
    if cpu_high is not None:
        overrides["cpu_high"] = cpu_high
    if cpu_low is not None:
        overrides["cpu_low"] = cpu_low
    if ram_high is not None:
        overrides["ram_high"] = ram_high
    if ram_low is not None:
        overrides["ram_low"] = ram_low
    if cooldown is not None:
        overrides["cooldown_s"] = cooldown

    if overrides:
        from dataclasses import asdict
        merged = {**asdict(cfg), **overrides}
        cfg = WorkerConfig(**merged)

    print(f"[workers] supervisor starting — scheduler={cfg.scheduler_address} "
          f"workers={cfg.min_workers}..{cfg.max_workers}")

    asyncio.run(_run(cfg))


async def _run(cfg: WorkerConfig) -> None:
    from dorian.workers.supervisor import Supervisor

    sup = Supervisor(cfg)
    loop = asyncio.get_running_loop()

    # Graceful shutdown on SIGINT/SIGTERM.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(sup.stop()))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler — fall back to
            # signal.signal which is less clean but functional.
            signal.signal(sig, lambda s, f: asyncio.create_task(sup.stop()))

    await sup.start()

    # Block until stopped.
    await sup._stop_event.wait()


if __name__ == "__main__":
    main()
