"""Integration tests for the execution engine end-to-end.

Exercises the full chain inside a fakeredis instance (streams +
consumer groups supported as of fakeredis 2.x):

    submitter XADD exec:jobs
      → Worker XREADGROUP → dispatch kind → SET exec:result:{id}
      → XADD {Kind}Completed to events:bg or events:user
      → XACK

Uses fakeredis so no external Redis is required; the test is
entirely in-process and deterministic.

Each test body is ONE coroutine invoked via ``asyncio.run`` — running
the setup + worker + assertion in the same loop keeps fakeredis's
internal async Queues on a single loop (they bind to the first loop
that touches them).
"""
from __future__ import annotations

import asyncio
import json

import fakeredis
import fakeredis.aioredis as fake_aioredis

from dorian.exec import registry as reg_mod
from dorian.exec.worker import Worker


def _run(coro):
    return asyncio.run(coro)


def _make_redis():
    """Fresh fakeredis per test. Unique FakeServer so internal async
    Queues don't get bound to a stale event loop from a previous test."""
    server = fakeredis.FakeServer()
    return fake_aioredis.FakeRedis(server=server, decode_responses=True)


def _clear_registry():
    reg_mod._registry.clear()


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------

def test_exec_worker_round_trip_against_fakeredis():
    _clear_registry()

    @reg_mod.register("integration:echo")
    async def echo(inputs, *, job_id):
        return {"echoed": inputs, "job_id": job_id}

    async def body():
        rs = _make_redis()
        await rs.xadd("exec:jobs", {
            "kind": "integration:echo",
            "job_id": "j1",
            "inputs": json.dumps({"uid": "u1", "session": "s1", "foo": "bar"}),
            "submitted_at": "1700000000",
        })
        worker = Worker(rs, name="it-worker")
        task = asyncio.create_task(worker.run())
        for _ in range(40):
            if await rs.xlen("events:bg") > 0:
                break
            await asyncio.sleep(0.1)
        worker.stop()
        await asyncio.wait_for(task, timeout=2.0)

        assert worker.stats.succeeded == 1, f"stats={worker.stats}"

        raw = await rs.get("exec:result:j1")
        assert raw is not None
        blob = json.loads(raw)
        assert blob["result"]["echoed"]["foo"] == "bar"
        assert blob["error"] is None

        entries = await rs.xrange("events:bg", "-", "+")
        assert entries
        _, fields = entries[0]
        assert fields["type"] == "IntegrationEchoCompleted"
        payload = json.loads(fields["payload"])
        assert payload["job_id"] == "j1"
        assert payload["result"]["echoed"]["foo"] == "bar"

        pending = await rs.xpending("exec:jobs", "exec-workers")
        assert pending["pending"] == 0

    _run(body())


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------

def test_exec_worker_records_handler_failure_and_acks():
    _clear_registry()

    @reg_mod.register("integration:boom")
    async def boom(inputs, *, job_id):
        raise RuntimeError("handler went boom")

    async def body():
        rs = _make_redis()
        await rs.xadd("exec:jobs", {
            "kind": "integration:boom",
            "job_id": "jb",
            "inputs": "{}",
            "submitted_at": "0",
        })
        worker = Worker(rs, name="it-worker")
        task = asyncio.create_task(worker.run())
        for _ in range(40):
            if await rs.xlen("events:bg") > 0:
                break
            await asyncio.sleep(0.1)
        worker.stop()
        await asyncio.wait_for(task, timeout=2.0)

        assert worker.stats.failed == 1

        entries = await rs.xrange("events:bg", "-", "+")
        assert entries
        payload = json.loads(entries[0][1]["payload"])
        assert payload["error"] is not None
        assert "boom" in payload["error"]
        pending = await rs.xpending("exec:jobs", "exec-workers")
        assert pending["pending"] == 0

    _run(body())


# ---------------------------------------------------------------------------
# User-lane routing
# ---------------------------------------------------------------------------

def test_completion_goes_to_user_lane_when_inputs_request_it():
    _clear_registry()

    @reg_mod.register("integration:user_lane")
    async def f(inputs, *, job_id):
        return {"ok": True}

    async def body():
        rs = _make_redis()
        await rs.xadd("exec:jobs", {
            "kind": "integration:user_lane",
            "job_id": "ju",
            "inputs": json.dumps({"uid": "u", "session": "s", "lane": "user"}),
            "submitted_at": "0",
        })
        worker = Worker(rs, name="it-worker")
        task = asyncio.create_task(worker.run())
        for _ in range(40):
            if await rs.xlen("events:user") > 0:
                break
            await asyncio.sleep(0.1)
        worker.stop()
        await asyncio.wait_for(task, timeout=2.0)

        assert await rs.xlen("events:user") == 1
        assert await rs.xlen("events:bg") == 0

    _run(body())


# ---------------------------------------------------------------------------
# DLQ: poison entries stop redelivering after exceeding threshold
# ---------------------------------------------------------------------------

def test_claimer_moves_poison_entry_to_dlq(monkeypatch):
    _clear_registry()

    monkeypatch.setenv("DORIAN_EXEC_CLAIM_IDLE_S", "0")
    monkeypatch.setenv("DORIAN_EXEC_CLAIM_INTERVAL_S", "0.1")
    monkeypatch.setenv("DORIAN_EXEC_MAX_DELIVERIES", "2")

    async def body():
        from dorian.exec.claimer import run_claimer

        rs = _make_redis()

        # Prime one entry on the stream, force its delivery count past
        # the threshold by reading it repeatedly without ACKing, then
        # run a single claimer iteration to confirm the DLQ write.
        await rs.xadd("exec:jobs", {
            "kind": "integration:poison",
            "job_id": "jp",
            "inputs": "{}",
            "submitted_at": "0",
        })
        await rs.xgroup_create("exec:jobs", "exec-workers", id="0", mkstream=True)
        # New delivery via ">".
        await rs.xreadgroup(
            groupname="exec-workers", consumername="fake-stuck",
            streams={"exec:jobs": ">"}, count=10, block=None,
        )
        # Re-read pending list three more times to bump delivery_count
        # past MAX_DELIVERIES (=2). fakeredis increments the counter
        # on each re-read, matching real Redis semantics.
        for _ in range(3):
            await rs.xreadgroup(
                groupname="exec-workers", consumername="fake-stuck",
                streams={"exec:jobs": "0"}, count=10, block=None,
            )

        dl_calls: list = []

        async def on_deadletter(entry_id, fields, times):
            dl_calls.append((entry_id, times))

        async def noop_claim(entry_id, fields):
            # Shouldn't be called for poison entries.
            raise AssertionError(
                "on_claim called on a poison entry — DLQ should have pre-empted"
            )

        claim_task = asyncio.create_task(
            run_claimer(rs, "claimer-consumer", noop_claim, on_deadletter),
        )
        for _ in range(30):
            if await rs.xlen("exec:jobs:deadletter") > 0:
                break
            await asyncio.sleep(0.1)
        claim_task.cancel()
        try:
            await claim_task
        except asyncio.CancelledError:
            pass

        assert dl_calls, "on_deadletter callback never fired"

        dlq = await rs.xrange("exec:jobs:deadletter", "-", "+")
        assert len(dlq) >= 1
        _, fields = dlq[0]
        assert fields["kind"] == "integration:poison"
        assert fields["_original_id"]
        assert int(fields["_times_delivered"]) > 2

    _run(body())
