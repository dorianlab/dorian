"""Tests for backend/eventbus_go_handled.py — Python-side toggle that
skips dispatch for event types whose handler lives in Go.

Pattern mirrors the authoritative-set tests (importlib load + fake
async Redis stub).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path


class _FakeRedis:
    def __init__(self):
        self.sets: dict[str, set[str]] = {}

    async def sadd(self, key, *members):
        s = self.sets.setdefault(key, set())
        c = 0
        for m in members:
            if m not in s:
                s.add(m); c += 1
        return c

    async def srem(self, key, *members):
        s = self.sets.get(key, set())
        c = 0
        for m in members:
            if m in s:
                s.remove(m); c += 1
        return c

    async def smembers(self, key):
        return {m.encode() for m in self.sets.get(key, set())}


def _load():
    path = Path(__file__).resolve().parents[1] / "backend" / "eventbus_go_handled.py"
    name = f"_go_{os.urandom(4).hex()}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


def test_empty_by_default():
    m = _load()
    assert m.snapshot() == []
    assert m.is_go_handled("X") is False


def test_add_marks_go_handled():
    m = _load()
    fr = _FakeRedis()

    async def run():
        await m.start(fr)
        await m.add("CancelPipeline")
        return m.is_go_handled("CancelPipeline"), m.snapshot()

    ok, snap = _run(run())
    assert ok is True
    assert snap == ["CancelPipeline"]


def test_remove_reverts():
    m = _load()
    fr = _FakeRedis()

    async def run():
        await m.start(fr)
        await m.add("A")
        await m.add("B")
        before = m.snapshot()
        await m.remove("A")
        after = m.snapshot()
        await m.stop()
        return before, after

    before, after = _run(run())
    assert sorted(before) == ["A", "B"]
    assert after == ["B"]


def test_redis_error_keeps_prior_snapshot():
    m = _load()
    fr = _FakeRedis()

    class Breaking:
        def __init__(self, inner):
            self.inner = inner
            self.broken = False

        async def sadd(self, *a, **k):
            return await self.inner.sadd(*a, **k)

        async def srem(self, *a, **k):
            return await self.inner.srem(*a, **k)

        async def smembers(self, *a, **k):
            if self.broken:
                raise RuntimeError("boom")
            return await self.inner.smembers(*a, **k)

    br = Breaking(fr)

    async def run():
        await m.start(br)
        await m.add("X")
        br.broken = True
        await m._refresh_once()
        return m.snapshot()

    assert _run(run()) == ["X"]
