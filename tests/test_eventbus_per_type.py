"""Tests for phase D: per-event-type worker pools in the subscriber.

Validates the key behaviours:
  - A slow handler on type X does not block type Y (parallel across types).
  - A handler that raises still XACKs, and the error counter increments.
  - Per-type stats are reported under ``stats().by_type``.
  - Workers_for_type resolves from env overrides.

The ``conftest.py`` stubs ``backend.*`` into sys.modules, so we load
the subscriber module via importlib (same pattern as the other eventbus
tests).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path


def _load(relpath: str):
    path = Path(__file__).resolve().parents[1] / relpath
    name = f"_mod_{os.urandom(4).hex()}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _inject_elastic_pool() -> object:
    """Load the real eventbus_elastic module and register it under the
    canonical ``backend.eventbus_elastic`` name so ``from backend.eventbus_elastic
    import ElasticPool`` inside the subscriber resolves through the test
    ``backend.*`` stub layer without a real package path."""
    path = Path(__file__).resolve().parents[1] / "backend" / "eventbus_elastic.py"
    spec = importlib.util.spec_from_file_location("backend.eventbus_elastic", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backend.eventbus_elastic"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


def _stub_backend_events(monkeypatch, handler_impl_per_type=None):
    """Install a fake ``backend.events`` module.

    ``handler_impl_per_type`` is {event_type: async_fn(event)} — the fake
    handler registry exposes it as ``handlers[type] = [fn]`` and
    ``_run_handler`` just awaits the bound handler.
    """
    fake = type(sys)("backend.events")

    class _E:
        def __init__(self, type, data):
            self.type = type
            self.data = data

    fake.Event = _E

    per_type = handler_impl_per_type or {}
    fake.handlers = {t: [fn] for t, fn in per_type.items()}

    async def run_handler(fn, event, *, source: str = "local"):
        # source kwarg mirrors the real signature so the subscriber's
        # ``source="subscriber"`` call matches. Tests don't assert on
        # it; the real events.py uses it to gate a counter bump.
        await fn(event)

    fake._run_handler = run_handler
    monkeypatch.setitem(sys.modules, "backend.events", fake)


def _stub_authoritative(monkeypatch, types):
    fake = type(sys)("backend.eventbus_authoritative")
    fake.is_authoritative = lambda t: t in types
    monkeypatch.setitem(sys.modules, "backend.eventbus_authoritative", fake)


def _stub_go_handled(monkeypatch, types=frozenset()):
    fake = type(sys)("backend.eventbus_go_handled")
    fake.is_go_handled = lambda t: t in types
    monkeypatch.setitem(sys.modules, "backend.eventbus_go_handled", fake)


class _FakeAckRedis:
    """Records XACKs so tests can verify they happened."""
    def __init__(self):
        self.acks: list = []

    async def xack(self, stream, group, entry_id):
        self.acks.append((stream, group, entry_id))
        return 1


# ---------------------------------------------------------------------------
# workers_for_type resolution
# ---------------------------------------------------------------------------

def test_workers_for_type_env_override(monkeypatch):
    monkeypatch.setenv("DORIAN_EVENTBUS_SUB_WORKERS_PER_TYPE", "1")
    monkeypatch.setenv("DORIAN_EVENTBUS_SUB_WORKERS_HotType", "8")
    sub = _load("backend/eventbus_subscriber.py")
    assert sub._workers_for_type("Normal") == 1
    assert sub._workers_for_type("HotType") == 8


def test_workers_for_type_rejects_zero(monkeypatch):
    monkeypatch.setenv("DORIAN_EVENTBUS_SUB_WORKERS_Bad", "0")
    monkeypatch.setenv("DORIAN_EVENTBUS_SUB_MIN_WORKERS", "2")
    sub = _load("backend/eventbus_subscriber.py")
    # Zero falls through to the global min.
    assert sub._workers_for_type("Bad") == 2


# ---------------------------------------------------------------------------
# Per-type isolation — a slow X must not block Y
# ---------------------------------------------------------------------------

def test_slow_type_does_not_block_other_type(monkeypatch):
    _inject_elastic_pool()
    sub = _load("backend/eventbus_subscriber.py")
    sub._redis = _FakeAckRedis()

    run_order: list[str] = []
    gate_x = asyncio.Event()

    async def slow_x(event):
        run_order.append("X:start")
        await gate_x.wait()  # held open until test releases
        run_order.append("X:done")

    async def fast_y(event):
        run_order.append("Y:done")

    _stub_backend_events(monkeypatch, {"X": slow_x, "Y": fast_y})
    _stub_authoritative(monkeypatch, {"X", "Y"})
    _stub_go_handled(monkeypatch)

    async def run():
        await sub._handle_entry("events:user", "0-1",
            {b"type": b"X", b"payload": b"{}"})
        await sub._handle_entry("events:user", "0-2",
            {b"type": b"Y", b"payload": b"{}"})

        # Y's pool should drain quickly; X's is blocked on gate_x.
        for _ in range(40):
            if "Y:done" in run_order:
                break
            await asyncio.sleep(0.05)

        # X must not have completed yet — it's gated.
        assert "Y:done" in run_order
        assert "X:done" not in run_order
        assert run_order.index("X:start") < run_order.index("Y:done")

        # Release X and let it finish before stop().
        gate_x.set()
        for _ in range(40):
            if "X:done" in run_order:
                break
            await asyncio.sleep(0.05)

        await sub.stop()

    _run(run())
    assert "X:done" in run_order

    s = sub.stats()["by_type"]
    assert s["X"]["dispatched"] == 1
    assert s["Y"]["dispatched"] == 1
    assert s["X"]["handler_errors"] == 0


# ---------------------------------------------------------------------------
# Handler error still XACKs + counts
# ---------------------------------------------------------------------------

def test_handler_error_still_xacks_and_counts(monkeypatch):
    _inject_elastic_pool()
    sub = _load("backend/eventbus_subscriber.py")
    redis = _FakeAckRedis()
    sub._redis = redis

    async def bad(event):
        raise RuntimeError("handler kaboom")

    _stub_backend_events(monkeypatch, {"Boom": bad})
    _stub_authoritative(monkeypatch, {"Boom"})
    _stub_go_handled(monkeypatch)

    async def run():
        await sub._handle_entry("events:bg", "0-9",
            {b"type": b"Boom", b"payload": b"{}"})
        for _ in range(40):
            if sub.stats()["by_type"].get("Boom", {}).get("handler_errors", 0) > 0:
                break
            await asyncio.sleep(0.05)
        await sub.stop()

    _run(run())

    # The poison event was ACK'd — Redis won't keep redelivering it.
    assert redis.acks == [("events:bg", sub._GROUP, "0-9")]
    by = sub.stats()["by_type"]["Boom"]
    assert by["handler_errors"] == 1
    assert "kaboom" in by["last_error"]


# ---------------------------------------------------------------------------
# Non-authoritative short-circuits without touching a type queue
# ---------------------------------------------------------------------------

def test_non_authoritative_short_circuits(monkeypatch):
    sub = _load("backend/eventbus_subscriber.py")
    redis = _FakeAckRedis()
    sub._redis = redis

    async def never_called(event):
        raise AssertionError("handler should not run for non-authoritative type")

    _stub_backend_events(monkeypatch, {"Meh": never_called})
    _stub_authoritative(monkeypatch, set())  # empty
    _stub_go_handled(monkeypatch)

    async def run():
        await sub._handle_entry("events:user", "0-3",
            {b"type": b"Meh", b"payload": b"{}"})
        # No pool should have spawned for Meh — it's non-authoritative.
        assert "Meh" not in sub._type_queues
        assert "Meh" not in sub._type_workers
        await sub.stop()

    _run(run())

    by = sub.stats()["by_type"]["Meh"]
    assert by["dedup_skipped"] == 1
    assert by["dispatched"] == 0
    # ACK always happens inline on short-circuit.
    assert redis.acks == [("events:user", sub._GROUP, "0-3")]
