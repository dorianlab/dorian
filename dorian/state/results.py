"""
dorian/state/results.py
------------------------
Result storage for operator outputs.

Strategy:
  • Small, JSON-serialisable values  → stored directly in Redis
    key: result:{run_id}:{node_id}   TTL: 24 h
  • Large / non-serialisable values  → pickled to local filesystem
    path: /tmp/dorian_results/{run_id}/{node_id}.pkl
    result_ref stored in Redis is a "file:<path>" string so callers can
    retrieve the blob without rerunning.

The ResultStore is intentionally kept simple and swappable: swap the
"large" backend to MinIO/S3 by replacing _store_large / _load_large.
"""
from __future__ import annotations

import json
import os
import pickle
from pathlib import Path
from typing import Any, Optional

from backend.envs import aioredis

_RESULT_DIR = Path(os.environ.get("DORIAN_RESULT_DIR", "/tmp/dorian_results"))
_TTL = 60 * 60 * 24  # 24 hours
_INLINE_SIZE_LIMIT = 4096  # bytes; results smaller than this go straight to Redis


def _redis_key(run_id: str, node_id: str) -> str:
    return f"result:{run_id}:{node_id}"


def _file_path(run_id: str, node_id: str) -> Path:
    return _RESULT_DIR / run_id / f"{node_id}.pkl"


def _is_json_small(value: Any) -> tuple[bool, Optional[bytes]]:
    """Try to serialise `value` to JSON; return (ok, bytes) or (False, None)."""
    try:
        encoded = json.dumps(value).encode()
        if len(encoded) <= _INLINE_SIZE_LIMIT:
            return True, encoded
    except (TypeError, ValueError):
        pass
    return False, None


class ResultStore:

    @staticmethod
    async def store(run_id: str, node_id: str, value: Any) -> str:
        """
        Persist `value` and return a result_ref string that can be used with
        ResultStore.load() to retrieve it later.
        """
        ok, encoded = _is_json_small(value)
        key = _redis_key(run_id, node_id)

        if ok:
            await aioredis.set(key, encoded, ex=_TTL)
            return f"redis:{key}"
        else:
            # Fall back to file
            path = _file_path(run_id, node_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                pickle.dump(value, f)
            ref = f"file:{path}"
            # Store the ref in Redis so the state tracker can surface it
            await aioredis.set(key, ref.encode(), ex=_TTL)
            return ref

    @staticmethod
    async def load(result_ref: str) -> Any:
        """Load a result back using the opaque ref returned by store()."""
        if result_ref.startswith("redis:"):
            redis_key = result_ref[len("redis:"):]
            raw = await aioredis.get(redis_key)
            if raw is None:
                raise KeyError(f"Result not found in Redis: {redis_key}")
            return json.loads(raw)
        elif result_ref.startswith("file:"):
            path = Path(result_ref[len("file:"):])
            with open(path, "rb") as f:
                return pickle.load(f)
        else:
            raise ValueError(f"Unknown result_ref scheme: {result_ref!r}")

    @staticmethod
    async def delete(run_id: str, node_id: str) -> None:
        """Clean up stored results for a node (e.g. after pipeline completes)."""
        key = _redis_key(run_id, node_id)
        raw = await aioredis.get(key)
        await aioredis.delete(key)
        if raw and raw.startswith("file:"):
            path = Path(raw[len("file:"):])
            if path.exists():
                path.unlink(missing_ok=True)
