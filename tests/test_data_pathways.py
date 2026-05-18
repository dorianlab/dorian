"""Tests for data pathway hardening: vault backup, dataset cleanup, feedback backup.

Covers:
  - Vault secrets backed to docstore on store
  - Vault secrets removed from docstore on delete
  - Vault recovery from docstore when Redis is empty
  - Dataset removal cleans up Redis keys + docstore
  - Feedback backed to docstore alongside Redis
  - Handler atomicity via session_meta_tx
"""
import asyncio
import json

from backend.envs import aioredis, expdb
from backend.events import Event


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Vault docstore backup
# ---------------------------------------------------------------------------

class TestVaultDocstoreBackup:

    def test_store_env_var_writes_to_both_redis_and_docstore(self):
        from dorian.vault.storage import store_env_var

        async def _test():
            envelope = {"ciphertext": "abc", "iv": "def", "salt": "ghi"}
            await store_env_var("user1", "API_KEY", envelope)

            raw = await aioredis.get("vault:user1:env:API_KEY")
            assert json.loads(raw) == envelope

            members = await aioredis.smembers("vault:user1:env:__index")
            assert "API_KEY" in members

            doc = await expdb.vault_secrets.find_one({"uid": "user1", "var_name": "API_KEY"})
            assert doc is not None
            assert doc["envelope"] == envelope

        _run(_test())

    def test_delete_env_var_removes_from_both(self):
        from dorian.vault.storage import store_env_var, delete_env_var

        async def _test():
            await store_env_var("user2", "SECRET", {"ciphertext": "x"})
            deleted = await delete_env_var("user2", "SECRET")

            assert deleted is True
            assert await aioredis.get("vault:user2:env:SECRET") is None

            doc = await expdb.vault_secrets.find_one({"uid": "user2", "var_name": "SECRET"})
            assert doc is None

        _run(_test())

    def test_recovery_restores_missing_redis_keys(self):
        from dorian.vault.storage import recover_vault_from_store

        async def _test():
            await expdb.vault_secrets.insert_one({
                "uid": "user3",
                "var_name": "DB_PASS",
                "envelope": {"ciphertext": "secret"},
            })

            recovered = await recover_vault_from_store()
            assert recovered == 1

            raw = await aioredis.get("vault:user3:env:DB_PASS")
            assert json.loads(raw) == {"ciphertext": "secret"}

            members = await aioredis.smembers("vault:user3:env:__index")
            assert "DB_PASS" in members

        _run(_test())

    def test_recovery_does_not_overwrite_existing_redis(self):
        from dorian.vault.storage import recover_vault_from_store

        async def _test():
            await aioredis.set("vault:user4:env:KEY", json.dumps({"ciphertext": "redis_value"}))
            await aioredis.sadd("vault:user4:env:__index", "KEY")

            await expdb.vault_secrets.insert_one({
                "uid": "user4",
                "var_name": "KEY",
                "envelope": {"ciphertext": "old_docstore_value"},
            })

            recovered = await recover_vault_from_store()
            assert recovered == 0

            raw = await aioredis.get("vault:user4:env:KEY")
            assert json.loads(raw)["ciphertext"] == "redis_value"

        _run(_test())

    def test_store_multiple_vars_same_user(self):
        from dorian.vault.storage import store_env_var, list_env_vars

        async def _test():
            await store_env_var("user5", "KEY_A", {"ciphertext": "a"})
            await store_env_var("user5", "KEY_B", {"ciphertext": "b"})

            names = await list_env_vars("user5")
            assert sorted(names) == ["KEY_A", "KEY_B"]

        _run(_test())


# Dataset removal cleanup tests retired with the rust port — see
# engine/backend/src/handlers/datasets.rs for the active code path.
# A live integration covering Redis key cleanup + postgres delete +
# file unlink should be added on the rust side before the next
# refactor; for now the unit-test gap mirrors the other ports
# (heartbeat, dataset_live, session, …) where python coverage was
# dropped at the same time as the python implementation.


# ---------------------------------------------------------------------------
# Feedback docstore backup
# ---------------------------------------------------------------------------

class TestFeedbackDocstoreBackup:

    def test_feedback_stored_in_redis_and_docstore(self):
        from dorian.event.handlers.lifecycle import handle_feedback

        async def _test():
            event = Event("FeedbackReceived", data={
                "uid": "u1", "session": "s1", "requestId": "r1",
                "answers": {"q1": "yes"},
                "pipelineId": "p1", "view": "canvas", "ts": 1234567890,
            })
            await handle_feedback(event)

            raw = await aioredis.get("feedback:u1:s1:r1")
            assert raw is not None
            entry = json.loads(raw)
            assert entry["answers"]["q1"] == "yes"

            history = await aioredis.lrange("feedback:u1:s1:history", 0, -1)
            assert len(history) == 1

            doc = await expdb.feedback.find_one({"uid": "u1", "session": "s1", "requestId": "r1"})
            assert doc is not None
            assert doc["answers"]["q1"] == "yes"

        _run(_test())

    def test_feedback_upsert_on_resubmit(self):
        from dorian.event.handlers.lifecycle import handle_feedback

        async def _test():
            for answer in ("first", "second"):
                event = Event("FeedbackReceived", data={
                    "uid": "u2", "session": "s2", "requestId": "r2",
                    "answers": {"q1": answer},
                })
                await handle_feedback(event)

            doc = await expdb.feedback.find_one({"uid": "u2", "session": "s2", "requestId": "r2"})
            assert doc["answers"]["q1"] == "second"

        _run(_test())


# PipelineSaved / PipelineRemoved / CustomOperatorAdded /
# EvaluationProcedureSelected handler tests retired with the rust
# port. Coverage moves to engine/backend/src/handlers/{pipeline,
# custom_nodes,session_meta,evaluation_procedure}.rs.
