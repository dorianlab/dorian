"""Tests for the Go event-bus shadow forwarder (backend/eventbus_shadow.py).

Phase E rewrite: the shadow now XADDs directly to Redis instead of
going through an HTTP drain queue. Tests stub aioredis with a fake
that records every XADD.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


def _load_shadow_module():
    path = Path(__file__).resolve().parents[1] / "backend" / "eventbus_shadow.py"
    name = f"_eventbus_shadow_{os.urandom(4).hex()}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


class _FakeRedis:
    """Minimal aioredis stub recording every XADD call."""
    def __init__(self, *, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    async def xadd(self, stream, fields, *, maxlen=None, approximate=False):
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append((stream, dict(fields), maxlen, approximate))
        return f"0-{len(self.calls)}"


# ---------------------------------------------------------------------------
# Config + noop
# ---------------------------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DORIAN_EVENTBUS_SHADOW", raising=False)
    sh = _load_shadow_module()
    assert sh.is_enabled() is False


def test_shadow_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("DORIAN_EVENTBUS_SHADOW", "0")
    sh = _load_shadow_module()

    async def run():
        await sh.start(redis_client=_FakeRedis())
        sh.shadow_emit("X", {"a": 1}, lane="bg")
        await sh.stop()

    _run(run())
    assert sh.stats()["forwarded"] == 0
    assert sh.stats()["enqueued"] == 0


# ---------------------------------------------------------------------------
# Direct XADD behaviour
# ---------------------------------------------------------------------------

def test_shadow_xadds_with_correct_fields(monkeypatch):
    monkeypatch.setenv("DORIAN_EVENTBUS_SHADOW", "1")
    sh = _load_shadow_module()
    fake = _FakeRedis()

    async def run():
        await sh.start(redis_client=fake)
        sh.shadow_emit(
            "PipelineRunCompleted", {"run_id": "r1", "nodes": 12},
            lane="bg", uid="u1", session="s1",
        )
        # The task runs on the loop — yield so it gets a chance to execute.
        for _ in range(20):
            if fake.calls:
                break
            await asyncio.sleep(0.02)
        await sh.stop()

    _run(run())

    assert len(fake.calls) == 1, f"want 1 xadd, got {fake.calls}"
    stream, fields, maxlen, approx = fake.calls[0]
    assert stream == "events:bg"
    assert fields["type"] == "PipelineRunCompleted"
    assert fields["uid"] == "u1"
    assert fields["session"] == "s1"
    # Payload round-trip: stored as a JSON string on the stream.
    assert json.loads(fields["payload"]) == {"run_id": "r1", "nodes": 12}
    assert approx is True


def test_shadow_routes_by_lane(monkeypatch):
    monkeypatch.setenv("DORIAN_EVENTBUS_SHADOW", "1")
    sh = _load_shadow_module()
    fake = _FakeRedis()

    async def run():
        await sh.start(redis_client=fake)
        sh.shadow_emit("A", {}, lane="user")
        sh.shadow_emit("B", {}, lane="bg")
        for _ in range(20):
            if len(fake.calls) >= 2:
                break
            await asyncio.sleep(0.02)
        await sh.stop()

    _run(run())

    streams = [c[0] for c in fake.calls]
    assert "events:user" in streams
    assert "events:bg" in streams


def test_shadow_counts_xadd_errors(monkeypatch):
    monkeypatch.setenv("DORIAN_EVENTBUS_SHADOW", "1")
    sh = _load_shadow_module()
    fake = _FakeRedis(fail=True)

    async def run():
        await sh.start(redis_client=fake)
        for _ in range(3):
            sh.shadow_emit("X", {}, lane="bg")
        for _ in range(20):
            if sh.stats()["dropped_http_error"] >= 3:
                break
            await asyncio.sleep(0.02)
        await sh.stop()

    _run(run())

    s = sh.stats()
    assert s["dropped_http_error"] >= 3, f"expected errors counted: {s}"
    assert s["forwarded"] == 0
    assert "redis down" in s["last_error"]


def test_shadow_handles_unserialisable_payload(monkeypatch):
    """Unserialisable payload bump the error counter; no XADD issued."""
    monkeypatch.setenv("DORIAN_EVENTBUS_SHADOW", "1")
    sh = _load_shadow_module()
    fake = _FakeRedis()

    class NotJsonable:
        def __repr__(self):
            # Even str() / repr() must fail so _json_default's fallback
            # doesn't save it — force an explicit TypeError instead.
            raise TypeError("cannot stringify")

    async def run():
        await sh.start(redis_client=fake)
        sh.shadow_emit("X", NotJsonable(), lane="bg")
        await asyncio.sleep(0.05)
        await sh.stop()

    _run(run())
    assert len(fake.calls) == 0
    assert sh.stats()["dropped_http_error"] >= 1


def test_shadow_stats_shape(monkeypatch):
    monkeypatch.setenv("DORIAN_EVENTBUS_SHADOW", "1")
    sh = _load_shadow_module()
    s = sh.stats()
    # Keys the /observability dashboard relies on must remain stable.
    for k in ("enabled", "transport", "stream_user", "stream_bg",
              "forwarded", "dropped_queue_full", "dropped_http_error",
              "breaker_open", "last_error"):
        assert k in s, f"missing key: {k}"
    assert s["transport"] == "redis-xadd"
