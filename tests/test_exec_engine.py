"""Tests for the dorian/exec execution engine.

Scope:
  * registry: register / get / double-register rejection
  * completion-event name derivation (kind → PascalCase event type)
  * decode_job: tolerant of str/bytes stream field representations
  * Worker end-to-end (registered kind → emit completion + store result)
    using a stub aioredis that records XADD / SET / XACK.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


def _load(relpath: str, module_name: str | None = None):
    path = Path(__file__).resolve().parents[1] / relpath
    name = module_name or f"_mod_{os.urandom(4).hex()}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_register_and_get():
    reg = _load("dorian/exec/registry.py")

    async def fn(inputs, *, job_id):
        return {"ok": True}

    reg.register("sample:kind")(fn)
    assert reg.get("sample:kind") is fn
    snap = reg.get_registry()
    assert "sample:kind" in snap


def test_registry_rejects_double_registration():
    reg = _load("dorian/exec/registry.py")

    @reg.register("dup:kind")
    async def one(inputs, *, job_id):
        return {}

    with pytest.raises(ValueError):
        @reg.register("dup:kind")
        async def two(inputs, *, job_id):
            return {}


# ---------------------------------------------------------------------------
# Event-name derivation
# ---------------------------------------------------------------------------

def test_completion_event_name_basic():
    # Worker module self-registers nothing at import time, safe to load
    # under a unique name per call.
    w = _load("dorian/exec/worker.py")
    assert w._completion_event_name("dq_check:missing_values") == "DQCheckMissingValuesCompleted"
    assert w._completion_event_name("ranking_objective:score") == "RankingObjectiveScoreCompleted"
    # Short tokens in the allow-list stay uppercase.
    assert w._completion_event_name("kb:lookup") == "KBLookupCompleted"
    assert w._completion_event_name("llm:chat") == "LLMChatCompleted"


def test_completion_event_name_edge_cases():
    w = _load("dorian/exec/worker.py")
    assert w._completion_event_name("simple") == "SimpleCompleted"
    assert w._completion_event_name("A_b_c") == "ABCCompleted"  # short tokens → upper


# ---------------------------------------------------------------------------
# Job decoding
# ---------------------------------------------------------------------------

def test_decode_job_mixed_bytes_and_str():
    w = _load("dorian/exec/worker.py")
    fields = {
        b"kind": b"dq_check:missing_values",
        "job_id": "abc123",
        b"inputs": b'{"dataset_id":"d1","fpath":"/p.csv"}',
        "submitted_at": "1700000000.5",
    }
    kind, inputs, job_id, ts = w._decode_job(fields)
    assert kind == "dq_check:missing_values"
    assert job_id == "abc123"
    assert inputs["dataset_id"] == "d1"
    assert ts == 1700000000.5


def test_decode_job_missing_kind_returns_empty():
    w = _load("dorian/exec/worker.py")
    kind, _, _, _ = w._decode_job({"job_id": "x"})
    assert kind == ""


def test_decode_job_bad_inputs_json_falls_back_to_raw():
    w = _load("dorian/exec/worker.py")
    _, inputs, _, _ = w._decode_job({"kind": "K", "inputs": "not json"})
    assert inputs == {"_raw": "not json"}


# ---------------------------------------------------------------------------
# Worker end-to-end with stub Redis
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Async Redis stub matching the surface the worker actually uses."""

    def __init__(self, jobs: list[tuple[str, dict]]):
        self._jobs = list(jobs)
        self._reads = 0
        self.xadds: list[tuple[str, dict]] = []
        self.sets: dict[str, tuple[str, int | None]] = {}
        self.xacks: list[tuple[str, str, str]] = []

    async def xgroup_create(self, *a, **kw):
        return True

    async def xreadgroup(self, *, groupname, consumername, streams, count, block):
        # First call returns all jobs as one batch; subsequent calls
        # return nothing (simulates empty stream + block timeout).
        if self._reads > 0 or not self._jobs:
            # Approximate a block timeout by sleeping briefly so the
            # worker's cancel loop has a chance to tear down.
            await asyncio.sleep(0.05)
            return []
        self._reads += 1
        entries = [(eid, fields) for eid, fields in self._jobs]
        return [(list(streams.keys())[0], entries)]

    async def xadd(self, stream, fields, *, maxlen=None, approximate=False):
        self.xadds.append((stream, dict(fields)))
        return f"0-{len(self.xadds)}"

    async def set(self, key, value, *, ex=None):
        self.sets[key] = (value, ex)
        return True

    async def xack(self, stream, group, entry_id):
        self.xacks.append((stream, group, entry_id))
        return 1


def test_worker_dispatches_and_emits_completion():
    reg = _load("dorian/exec/registry.py")
    w = _load("dorian/exec/worker.py")

    received: list[dict] = []

    @reg.register("test:echo")
    async def echo(inputs, *, job_id):
        received.append(inputs)
        return {"echoed": inputs}

    # The worker imports backend/exec registry by module path; we need
    # the worker's ``_registry`` reference to point at OUR freshly-loaded
    # registry module, not a separate import. Point them at the same.
    w._registry = reg  # type: ignore

    rs = _FakeRedis(jobs=[(
        "0-1",
        {
            "kind": "test:echo",
            "job_id": "j1",
            "inputs": json.dumps({"uid": "u1", "session": "s1", "lane": "user"}),
            "submitted_at": "1700000000.0",
        },
    )])

    worker = w.Worker(rs, name="test-worker")

    async def run():
        task = asyncio.create_task(worker.run())
        # Give the slot a moment to pick up the job + emit.
        for _ in range(30):
            if received and rs.xadds and rs.xacks:
                break
            await asyncio.sleep(0.05)
        worker.stop()
        await asyncio.wait_for(task, timeout=2.0)

    _run(run())

    # Handler received the decoded inputs.
    assert received and received[0]["uid"] == "u1"
    # Completion event XADDed to the user lane (because inputs.lane=user).
    assert rs.xadds and rs.xadds[0][0] == "events:user"
    # Event type was derived correctly.
    assert rs.xadds[0][1]["type"] == "TestEchoCompleted"
    # Result was stored under exec:result:{job_id}.
    assert "exec:result:j1" in rs.sets
    # The job was ACKed.
    assert rs.xacks and rs.xacks[0][2] == "0-1"


def test_worker_unknown_kind_emits_error_completion():
    reg = _load("dorian/exec/registry.py")
    w = _load("dorian/exec/worker.py")
    w._registry = reg  # type: ignore

    rs = _FakeRedis(jobs=[(
        "0-1",
        {"kind": "never_registered", "job_id": "j9", "inputs": "{}"},
    )])
    worker = w.Worker(rs, name="test-worker")

    async def run():
        task = asyncio.create_task(worker.run())
        for _ in range(30):
            if rs.xadds and rs.xacks:
                break
            await asyncio.sleep(0.05)
        worker.stop()
        await asyncio.wait_for(task, timeout=2.0)

    _run(run())

    assert rs.xadds, "expected a completion emit even on unknown kind"
    body = json.loads(rs.xadds[0][1]["payload"])
    assert "unknown kind" in (body.get("error") or "")
    # Still ACKed so Redis doesn't redeliver forever.
    assert rs.xacks


def test_worker_handler_exception_counted_and_acked():
    reg = _load("dorian/exec/registry.py")
    w = _load("dorian/exec/worker.py")
    w._registry = reg  # type: ignore

    @reg.register("boom:kind")
    async def boom(inputs, *, job_id):
        raise RuntimeError("handler went boom")

    rs = _FakeRedis(jobs=[(
        "0-1",
        {"kind": "boom:kind", "job_id": "jb", "inputs": "{}"},
    )])
    worker = w.Worker(rs, name="test-worker")

    async def run():
        task = asyncio.create_task(worker.run())
        for _ in range(30):
            if rs.xadds and rs.xacks:
                break
            await asyncio.sleep(0.05)
        worker.stop()
        await asyncio.wait_for(task, timeout=2.0)

    _run(run())

    assert worker.stats.failed == 1
    assert rs.xacks  # poison job still ACKed
    body = json.loads(rs.xadds[0][1]["payload"])
    assert body.get("error") and "boom" in body["error"]
