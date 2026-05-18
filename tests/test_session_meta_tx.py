"""Tests for the atomic session meta transaction context manager.

Covers:
  - Basic read-modify-write atomicity
  - Lock acquisition and release
  - Concurrent updates don't overwrite each other
  - Empty/missing session meta
"""
import asyncio
import json

from backend.envs import aioredis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


async def _set_meta(session: str, meta: dict):
    await aioredis.set(f"session:{session}:meta", json.dumps(meta))


async def _get_meta(session: str) -> dict | None:
    raw = await aioredis.get(f"session:{session}:meta")
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSessionMetaTx:

    def test_basic_read_modify_write(self):
        from dorian.event.helpers.lifecycle import session_meta_tx

        async def _test():
            await _set_meta("s1", {"name": "Session 1", "pipeline": None})

            async with session_meta_tx("s1") as meta:
                meta["pipeline"] = {"id": "p1", "nodes": []}

            result = await _get_meta("s1")
            assert result["pipeline"] == {"id": "p1", "nodes": []}
            assert result["name"] == "Session 1"

        _run(_test())

    def test_creates_meta_if_missing(self):
        from dorian.event.helpers.lifecycle import session_meta_tx

        async def _test():
            async with session_meta_tx("new-session") as meta:
                meta["created"] = True

            result = await _get_meta("new-session")
            assert result == {"created": True}

        _run(_test())

    def test_lock_released_after_success(self):
        from dorian.event.helpers.lifecycle import session_meta_tx

        async def _test():
            await _set_meta("s2", {"x": 1})

            async with session_meta_tx("s2") as meta:
                meta["x"] = 2

            lock_exists = await aioredis.exists("session:s2:meta:lock")
            assert not lock_exists

        _run(_test())

    def test_lock_released_after_exception(self):
        from dorian.event.helpers.lifecycle import session_meta_tx

        async def _test():
            await _set_meta("s3", {"x": 1})

            try:
                async with session_meta_tx("s3") as meta:
                    raise ValueError("boom")
            except ValueError:
                pass

            lock_exists = await aioredis.exists("session:s3:meta:lock")
            assert not lock_exists

        _run(_test())

    def test_concurrent_updates_serialize(self):
        """Two concurrent updates to different fields should both persist."""
        from dorian.event.helpers.lifecycle import session_meta_tx

        async def _test():
            await _set_meta("s4", {"a": 0, "b": 0})

            async def update_a():
                async with session_meta_tx("s4") as meta:
                    meta["a"] = 1

            async def update_b():
                async with session_meta_tx("s4") as meta:
                    meta["b"] = 2

            await asyncio.gather(update_a(), update_b())

            result = await _get_meta("s4")
            assert result["a"] == 1
            assert result["b"] == 2

        _run(_test())

    def test_nested_dict_mutation(self):
        from dorian.event.helpers.lifecycle import session_meta_tx

        async def _test():
            await _set_meta("s5", {"dataset": {"did": "d1", "fpath": "/data/d1.csv"}})

            async with session_meta_tx("s5") as meta:
                meta["dataset"]["fpath"] = "/data/updated.csv"

            result = await _get_meta("s5")
            assert result["dataset"]["fpath"] == "/data/updated.csv"
            assert result["dataset"]["did"] == "d1"

        _run(_test())

    def test_list_append_in_meta(self):
        from dorian.event.helpers.lifecycle import session_meta_tx

        async def _test():
            await _set_meta("s6", {"customOperators": [{"uuid": "op1", "name": "A"}]})

            async with session_meta_tx("s6") as meta:
                ops = meta.get("customOperators") or []
                ops.append({"uuid": "op2", "name": "B"})
                meta["customOperators"] = ops

            result = await _get_meta("s6")
            assert len(result["customOperators"]) == 2
            assert result["customOperators"][1]["uuid"] == "op2"

        _run(_test())
