"""
dorian/infra/redis_utils.py
----------------------------
Thin async helpers for common Redis-JSON patterns.

Eliminates the recurring ``raw = await aioredis.get(key); json.loads(raw)``
boilerplate and centralises error handling so callers never crash on corrupt
or missing values.
"""
from __future__ import annotations

import json
from typing import Any


async def redis_get_json(
    redis,
    key: str,
    *,
    default: Any = None,
) -> Any:
    """Read a Redis key and return the JSON-decoded value.

    Returns *default* when the key is missing **or** the value is not
    valid JSON.  Never raises on bad data.
    """
    raw = await redis.get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


async def redis_set_json(
    redis,
    key: str,
    value: Any,
    *,
    ex: int | None = None,
) -> None:
    """Encode *value* as JSON and SET it in Redis.

    Passes *ex* (seconds) through to ``redis.set()`` when provided.
    """
    payload = json.dumps(value)
    if ex is not None:
        await redis.set(key, payload, ex=ex)
    else:
        await redis.set(key, payload)


def safe_json_loads(raw: str | bytes | None, *, default: Any = None) -> Any:
    """``json.loads`` with a safe fallback.

    Useful for values already fetched from Redis (or any other source)
    where you just need the decode step with a guaranteed fallback.
    """
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
