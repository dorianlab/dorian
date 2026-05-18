"""
dorian/observability/__init__.py
---------------------------------
Public API for the observability package.

Usage (in main.py lifespan):
    from dorian.observability import start_sampler, stop_sampler

    async with lifespan():
        await start_sampler()
        ...
        await stop_sampler()
"""
from .sampler import start_sampler, stop_sampler
from .collector import collector

__all__ = ["start_sampler", "stop_sampler", "collector"]
