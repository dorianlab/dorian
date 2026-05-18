"""
dorian/infra/cache.py
---------------------
Two-tier caching layer: in-process LRU + Redis TTL.

Provides a unified ``@cached`` decorator and explicit ``get``/``set``/
``invalidate`` functions that work across both tiers.

Tier 1 — In-process LRU (``functools.lru_cache``)
    Hot-path, zero-latency.  Already used by ``dorian.knowledge.queries``.
    Tier 1 entries are *per-process* and lost on restart.

Tier 2 — Redis TTL cache
    Cross-process, shared between Dask workers and the API server.
    Keys live under the ``cache:`` namespace with configurable TTL.
    Falls back silently if Redis is unavailable (treat cache as optional).

Usage::

    from dorian.infra.cache import cached, invalidate_prefix

    # Decorator — caches the return value in both tiers
    @cached(prefix="kb:operator_interface", ttl=300)
    async def get_operator_interface(name: str) -> dict:
        ...

    # Explicit invalidation (e.g. after KB reload)
    await invalidate_prefix("kb:")
"""
from __future__ import annotations

import json
import functools
import hashlib
from typing import Any, Callable, Optional

from dorian.infra.keys import RedisKeys


# ---------------------------------------------------------------------------
# Redis key helpers
# ---------------------------------------------------------------------------

class _CacheKeys:
    """Redis key patterns for the cache namespace."""

    @staticmethod
    def entry(prefix: str, key_hash: str) -> str:
        return f"cache:{prefix}:{key_hash}"

    @staticmethod
    def prefix_pattern(prefix: str) -> str:
        return f"cache:{prefix}:*"


def _hash_args(*args, **kwargs) -> str:
    """Deterministic hash of function arguments for cache key derivation."""
    raw = json.dumps({"a": [repr(a) for a in args], "k": {k: repr(v) for k, v in sorted(kwargs.items())}}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Tier 2: Redis get / set / invalidate
# ---------------------------------------------------------------------------

async def cache_get(prefix: str, key_hash: str) -> Optional[Any]:
    """Retrieve a cached value from Redis (Tier 2).

    Returns None on cache miss or Redis error (fail-open).
    """
    try:
        from backend.envs import aioredis
        raw = await aioredis.get(_CacheKeys.entry(prefix, key_hash))
        if raw is not None:
            return json.loads(raw)
    except Exception:
        pass
    return None


async def cache_set(prefix: str, key_hash: str, value: Any, ttl: int = 300) -> None:
    """Store a value in Redis with TTL (Tier 2).

    Silently ignores Redis errors (fail-open).
    """
    try:
        from backend.envs import aioredis
        serialized = json.dumps(value)
        await aioredis.set(_CacheKeys.entry(prefix, key_hash), serialized, ex=ttl)
    except Exception:
        pass


async def invalidate_prefix(prefix: str) -> int:
    """Delete all cache entries matching a prefix.

    Uses SCAN to avoid blocking Redis.  Returns the number of keys deleted.

    Example::

        await invalidate_prefix("kb:")  # clear all KB caches
        await invalidate_prefix("rec:")  # clear recommendation caches
    """
    try:
        from backend.envs import aioredis
        pattern = _CacheKeys.prefix_pattern(prefix)
        deleted = 0
        cursor = 0
        while True:
            cursor, keys = await aioredis.scan(cursor, match=pattern, count=200)
            if keys:
                await aioredis.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        return deleted
    except Exception:
        return 0


async def invalidate_key(prefix: str, *args, **kwargs) -> bool:
    """Delete a specific cache entry by reconstructing its key from arguments."""
    try:
        from backend.envs import aioredis
        key_hash = _hash_args(*args, **kwargs)
        result = await aioredis.delete(_CacheKeys.entry(prefix, key_hash))
        return bool(result)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tier 1 + 2: ``@cached`` decorator
# ---------------------------------------------------------------------------

def cached(
    prefix: str,
    ttl: int = 300,
    lru_maxsize: int = 256,
):
    """Two-tier cache decorator for async functions.

    Parameters
    ----------
    prefix : str
        Redis key prefix (e.g. ``"kb:operator_interface"``).
    ttl : int
        Redis TTL in seconds (default 5 minutes).
    lru_maxsize : int
        In-process LRU cache size (default 256).

    The decorated function must be async and return JSON-serializable data.

    Tier 1 (LRU) is checked first.  On miss, Tier 2 (Redis) is checked.
    On double-miss, the original function runs and both tiers are populated.
    """
    def decorator(fn: Callable) -> Callable:
        # Tier 1: in-process LRU (keyed by a string hash to avoid unhashable args)
        _lru: dict[str, Any] = {}
        _lru_order: list[str] = []

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            key_hash = _hash_args(*args, **kwargs)

            # --- Tier 1: in-process dict ---
            if key_hash in _lru:
                return _lru[key_hash]

            # --- Tier 2: Redis ---
            cached_val = await cache_get(prefix, key_hash)
            if cached_val is not None:
                # Promote to Tier 1
                _lru_put(key_hash, cached_val)
                return cached_val

            # --- Miss: compute ---
            result = await fn(*args, **kwargs)

            # Populate both tiers
            _lru_put(key_hash, result)
            await cache_set(prefix, key_hash, result, ttl)

            return result

        def _lru_put(key: str, value: Any) -> None:
            """Simple bounded dict-based LRU insert."""
            if key not in _lru:
                if len(_lru_order) >= lru_maxsize:
                    evict = _lru_order.pop(0)
                    _lru.pop(evict, None)
                _lru_order.append(key)
            _lru[key] = value

        # Expose cache control on the wrapper
        wrapper.cache_clear = lambda: (_lru.clear(), _lru_order.clear())  # type: ignore[attr-defined]
        wrapper.cache_prefix = prefix  # type: ignore[attr-defined]

        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Convenience: clear all in-process LRU caches (e.g. on KB reload)
# ---------------------------------------------------------------------------

def clear_all_lru_caches() -> None:
    """Clear every ``functools.lru_cache`` on ``dorian.knowledge.queries``.

    Call this after KB graph changes (rare — typically after dev reloads).
    """
    try:
        import dorian.knowledge.queries as q
        for name in dir(q):
            obj = getattr(q, name, None)
            if hasattr(obj, "cache_clear"):
                obj.cache_clear()
    except ImportError:
        pass


async def invalidate_all() -> int:
    """Clear both Tier 1 (LRU) and Tier 2 (Redis) caches.

    Useful during development or after KB schema changes.
    Returns the number of Redis keys deleted.
    """
    clear_all_lru_caches()
    return await invalidate_prefix("")
