"""Tests for backend/eventbus_authoritative.py — Redis-backed allow-list.

Phase C of the Go-bus migration. We load the module via importlib (the
conftest stubs backend.*) and back it with a minimal async fake that
mimics the SADD / SREM / SMEMBERS interface we use.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path


class _FakeRedis:
    """Minimal async Redis stub for the authoritative module."""

    def __init__(self):
        self.sets: dict[str, set[str]] = {}

    async def sadd(self, key, *members):
        s = self.sets.setdefault(key, set())
        added = 0
        for m in members:
            if m not in s:
                s.add(m)
                added += 1
        return added

    async def srem(self, key, *members):
        s = self.sets.get(key, set())
        removed = 0
        for m in members:
            if m in s:
                s.remove(m)
                removed += 1
        return removed

    async def smembers(self, key):
        # Simulate aioredis returning bytes — the module must decode.
        return {m.encode() for m in self.sets.get(key, set())}


def _load(module_name: str = "backend/eventbus_authoritative.py") -> object:
    path = Path(__file__).resolve().parents[1] / module_name
    name = f"_authz_{os.urandom(4).hex()}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


def test_snapshot_empty_before_start():
    az = _load()
    assert az.snapshot() == []
    assert az.is_authoritative("X") is False


def test_add_then_is_authoritative():
    az = _load()
    fr = _FakeRedis()

    async def run():
        await az.start(fr)
        await az.add("NodeObservability")
        return az.is_authoritative("NodeObservability"), az.snapshot()

    is_auth, snap = _run(run())
    assert is_auth is True
    assert snap == ["NodeObservability"]


def test_remove_reverts():
    az = _load()
    fr = _FakeRedis()

    async def run():
        await az.start(fr)
        await az.add("ProcessSample")
        await az.add("NodeObservability")
        before = az.snapshot()
        await az.remove("ProcessSample")
        after = az.snapshot()
        await az.stop()
        return before, after

    before, after = _run(run())
    assert sorted(before) == ["NodeObservability", "ProcessSample"]
    assert after == ["NodeObservability"]


def test_redis_error_keeps_prior_snapshot():
    """If Redis SMEMBERS blows up, the cached snapshot must survive —
    fail-open, don't lose routing decisions on a transient glitch."""
    az = _load()
    fr = _FakeRedis()

    class BreakingRedis:
        def __init__(self, inner):
            self.inner = inner
            self.broken = False

        async def sadd(self, *a, **k):
            return await self.inner.sadd(*a, **k)

        async def srem(self, *a, **k):
            return await self.inner.srem(*a, **k)

        async def smembers(self, *a, **k):
            if self.broken:
                raise RuntimeError("redis ghosted us")
            return await self.inner.smembers(*a, **k)

    br = BreakingRedis(fr)

    async def run():
        await az.start(br)
        await az.add("X")
        # Now break Redis and trigger a refresh.
        br.broken = True
        await az._refresh_once()
        return az.snapshot()

    snap = _run(run())
    assert snap == ["X"], "prior snapshot should survive a refresh error"


def test_snapshot_meta_exposes_age():
    az = _load()
    fr = _FakeRedis()

    async def run():
        await az.start(fr)
        await az.add("Y")
        meta = az.snapshot_meta()
        await az.stop()
        return meta

    meta = _run(run())
    assert meta["size"] == 1
    assert meta["loaded_at"] > 0
    assert meta["age_s"] is not None and meta["age_s"] >= 0
    assert meta["refresh_interval_s"] == 1.0
