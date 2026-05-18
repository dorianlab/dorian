"""Tests for backend/eventbus_subscriber.py — Redis Streams consumer.

Phase C of the Go-bus migration. The subscriber's ``_decode_fields`` and
authoritative-gating logic are exercised via importlib (bypassing the
conftest ``backend.*`` stubs).

The XREADGROUP consume loop against a real Redis is integration-level
and covered by the end-to-end test in this file, which is skipped when
Redis isn't available (DORIAN_TEST_REDIS_URL not set).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock


def _load(relpath: str) -> object:
    path = Path(__file__).resolve().parents[1] / relpath
    name = f"_mod_{os.urandom(4).hex()}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _inject_elastic_pool() -> None:
    """Load backend/eventbus_elastic.py under its canonical module name
    so the subscriber's ``from backend.eventbus_elastic import ElasticPool``
    succeeds even though conftest stubs the ``backend`` package."""
    path = Path(__file__).resolve().parents[1] / "backend" / "eventbus_elastic.py"
    spec = importlib.util.spec_from_file_location("backend.eventbus_elastic", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backend.eventbus_elastic"] = mod
    spec.loader.exec_module(mod)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _decode_fields — round-trip a Go-produced stream entry back into an Event
# ---------------------------------------------------------------------------

def test_decode_fields_basic(monkeypatch):
    sub = _load("backend/eventbus_subscriber.py")

    # Subscriber imports backend.events inside _decode_fields; stub it.
    fake_events = type(sys)("backend.events")
    class _E:
        def __init__(self, type, data):
            self.type = type
            self.data = data
    fake_events.Event = _E
    monkeypatch.setitem(sys.modules, "backend.events", fake_events)

    fields = {
        b"type": b"NodeObservability",
        b"uid": b"u1",
        b"session": b"s1",
        b"ts": b"1729000000.5",
        b"payload": b'{"node_id":"n1","wall_s":0.5}',
    }
    event_type, ev = sub._decode_fields(fields)
    assert event_type == "NodeObservability"
    assert ev is not None
    assert ev.type == "NodeObservability"
    assert ev.data["node_id"] == "n1"
    assert ev.data["wall_s"] == 0.5
    # Envelope fields should be injected into data so handlers still
    # find ``data["uid"]`` and friends.
    assert ev.data["uid"] == "u1"
    assert ev.data["session"] == "s1"
    assert ev.data["ts"] == 1729000000.5


def test_decode_fields_missing_type_yields_none(monkeypatch):
    sub = _load("backend/eventbus_subscriber.py")
    fake_events = type(sys)("backend.events")
    class _E:
        def __init__(self, type, data):
            self.type = type
            self.data = data
    fake_events.Event = _E
    monkeypatch.setitem(sys.modules, "backend.events", fake_events)

    event_type, ev = sub._decode_fields({b"uid": b"u1"})
    assert event_type == ""
    assert ev is None


def test_decode_fields_bad_json_payload_degrades(monkeypatch):
    sub = _load("backend/eventbus_subscriber.py")
    fake_events = type(sys)("backend.events")
    class _E:
        def __init__(self, type, data):
            self.type = type
            self.data = data
    fake_events.Event = _E
    monkeypatch.setitem(sys.modules, "backend.events", fake_events)

    fields = {b"type": b"X", b"payload": b"not json at all"}
    event_type, ev = sub._decode_fields(fields)
    assert event_type == "X"
    # Payload preserved under _raw so operators can see what came in.
    assert ev.data["_raw"] == "not json at all"


# ---------------------------------------------------------------------------
# Authoritative gating — dispatched vs short-circuited to dedup_skipped
# ---------------------------------------------------------------------------

def test_handle_entry_dispatches_only_authoritative(monkeypatch):
    _inject_elastic_pool()
    sub = _load("backend/eventbus_subscriber.py")

    # Stub backend.events.handlers / _run_handler
    dispatched: list[str] = []

    async def fake_run_handler(fn, event, *, source="local"):
        dispatched.append(event.type)

    fake_events = type(sys)("backend.events")

    class _E:
        def __init__(self, type, data):
            self.type = type
            self.data = data

    fake_events.Event = _E
    fake_events.handlers = {"AuthEvent": [lambda e: None]}
    fake_events._run_handler = fake_run_handler
    monkeypatch.setitem(sys.modules, "backend.events", fake_events)

    # Stub authoritative lookup: only "AuthEvent" is authoritative.
    fake_auth = type(sys)("backend.eventbus_authoritative")
    fake_auth.is_authoritative = lambda t: t == "AuthEvent"
    monkeypatch.setitem(sys.modules, "backend.eventbus_authoritative", fake_auth)

    fake_go = type(sys)("backend.eventbus_go_handled")
    fake_go.is_go_handled = lambda t: False
    monkeypatch.setitem(sys.modules, "backend.eventbus_go_handled", fake_go)

    # Fake redis with an XACK we can observe.
    acks: list[tuple] = []

    class _R:
        async def xack(self, stream, group, entry_id):
            acks.append((stream, group, entry_id))
            return 1

    sub._redis = _R()

    async def run():
        # Authoritative → enqueued to AuthEvent's worker pool (phase D).
        await sub._handle_entry("events:bg", "0-1",
            {b"type": b"AuthEvent", b"payload": b'{}'})
        # Non-authoritative → dedup_skipped, acked inline.
        await sub._handle_entry("events:bg", "0-2",
            {b"type": b"NonAuth", b"payload": b'{}'})
        # Give the AuthEvent worker time to drain + dispatch + ack.
        for _ in range(40):
            if dispatched and len(acks) == 2:
                break
            await asyncio.sleep(0.05)
        await sub.stop()

    _run(run())

    assert dispatched == ["AuthEvent"], f"only auth should dispatch, got {dispatched}"
    assert sub._stats.dispatched == 1
    assert sub._stats.dedup_skipped == 1
    # Both entries must be XACK'd so Redis doesn't keep redelivering them.
    assert len(acks) == 2
    assert sub._stats.last_id_bg == "0-2"


def test_handle_entry_handler_error_counted_and_acked(monkeypatch):
    _inject_elastic_pool()
    sub = _load("backend/eventbus_subscriber.py")

    async def boom(fn, event, *, source="local"):
        raise RuntimeError("handler went boom")

    fake_events = type(sys)("backend.events")

    class _E:
        def __init__(self, type, data):
            self.type = type
            self.data = data

    fake_events.Event = _E
    fake_events.handlers = {"E": [lambda e: None]}
    fake_events._run_handler = boom
    monkeypatch.setitem(sys.modules, "backend.events", fake_events)

    fake_auth = type(sys)("backend.eventbus_authoritative")
    fake_auth.is_authoritative = lambda t: True
    monkeypatch.setitem(sys.modules, "backend.eventbus_authoritative", fake_auth)

    fake_go = type(sys)("backend.eventbus_go_handled")
    fake_go.is_go_handled = lambda t: False
    monkeypatch.setitem(sys.modules, "backend.eventbus_go_handled", fake_go)

    acks: list = []

    class _R:
        async def xack(self, stream, group, entry_id):
            acks.append(entry_id)

    sub._redis = _R()

    async def run():
        await sub._handle_entry("events:bg", "0-9",
            {b"type": b"E", b"payload": b'{}'})
        # Wait for per-type worker to drain, invoke boom, count error, ack.
        for _ in range(40):
            if acks and sub._stats.handler_errors > 0:
                break
            await asyncio.sleep(0.05)
        await sub.stop()

    _run(run())

    # Handler errored → counted, entry still acked (phase D policy
    # unchanged from C: poison events are never redelivered).
    assert sub._stats.handler_errors == 1
    assert "boom" in sub._stats.last_error
    assert acks == ["0-9"]
