import asyncio
import json

from backend.envs import aioredis
from dorian.infra.keys import RedisKeys


def _run(coro):
    return asyncio.run(coro)


def test_bind_existing_profiled_dataset_updates_session_and_stream():
    from dorian.api.routes.file import _bind_dataset_to_session

    async def _test():
        has_profile = await _bind_dataset_to_session(
            did="ds1",
            session_id="sess1",
            user_id="user1",
            fpath="/tmp/data.csv",
            doc={
                "_id": "ds1",
                "profile": {"NumberOfInstances": 12},
                "features": ["a", "b"],
                "targets": ["y"],
            },
        )

        assert has_profile is True

        raw = await aioredis.get(RedisKeys.session_meta("sess1"))
        meta = json.loads(raw)
        assert meta["dataset"]["did"] == "ds1"
        assert meta["dataset"]["profile"]["NumberOfInstances"] == 12

        assert await aioredis.get(RedisKeys.dataset_fpath("ds1")) == "/tmp/data.csv"
        assert json.loads(await aioredis.get(RedisKeys.dataset_feature_columns("ds1"))) == ["a", "b"]
        assert json.loads(await aioredis.get(RedisKeys.dataset_target_columns("ds1"))) == ["y"]

        stream = aioredis._streams[RedisKeys.stream("user1", "sess1")]
        assert stream[-1]["event"] == "state/dataset"
        assert json.loads(stream[-1]["value"])["did"] == "ds1"

    _run(_test())


def test_bind_existing_unprofiled_dataset_reports_not_ready():
    from dorian.api.routes.file import _bind_dataset_to_session

    async def _test():
        has_profile = await _bind_dataset_to_session(
            did="ds2",
            session_id="sess2",
            user_id="user2",
            fpath="/tmp/raw.csv",
            fallback_meta={"description": "raw upload"},
        )

        raw = await aioredis.get(RedisKeys.session_meta("sess2"))
        meta = json.loads(raw)
        assert has_profile is False
        assert meta["dataset"]["did"] == "ds2"
        assert meta["dataset"]["description"] == "raw upload"
        assert "profile" not in meta["dataset"]

    _run(_test())
