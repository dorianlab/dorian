"""
backend/envs.py
----------------
Global infrastructure singletons: Redis (sync + async), the Postgres
document store (``expdb``), the in-memory LRU cache, and (legacy) the
Dask cluster.

All connection pools are sized and configured with explicit timeouts so the
app fails fast on infrastructure outages rather than hanging indefinitely.

Dask gating
-----------
``DORIAN_USE_RUST_RUNNER=1`` (the default) skips the Dask
``LocalCluster``/``Client`` boot entirely. Pipeline execution lives in
the Rust runner (``dorian/pipeline/execution.py::_run_via_rust_runner``)
and the Dask client was only used by ``backend.queue.bridge_logic``'s
backpressure check — that consumer's ``scheduler_info`` path is gated
on the same flag. Setting the env var to ``0`` restores the legacy
LocalCluster path for fallback / debugging.
"""
import os

from backend.config import config
from backend.cache import MemoryLRUCache

from redis.asyncio import Redis as RedisAsync
from redis import Redis

# ---------------------------------------------------------------------------
# Dask cluster (legacy, gated)
# ---------------------------------------------------------------------------

_USE_RUST_RUNNER = os.environ.get("DORIAN_USE_RUST_RUNNER", "1").lower() in (
    "1", "true", "yes", "on",
)

cluster = None
executor = None
worker_client = None

if not _USE_RUST_RUNNER:
    import dask
    from dask.distributed import (
        Client,
        LocalCluster,
        worker_client as _worker_client,
    )
    worker_client = _worker_client

    # Disable worker memory management entirely: with processes=False the
    # workers share the main process RSS, which is dominated by sklearn
    # models loaded by the threaded scheduler — the distributed
    # pacer/spill logic cannot manage that memory and only causes
    # misleading "pausing worker" warnings on Windows.
    dask.config.set({
        "distributed.worker.memory.target": False,
        "distributed.worker.memory.spill": False,
        "distributed.worker.memory.pause": False,
        "distributed.worker.memory.terminate": False,
    })

    _dask_mode = getattr(config.dask, "mode", "local")
    if _dask_mode == "external":
        executor = Client(config.dask.scheduler_address)
    else:
        cluster = LocalCluster(**config.dask.cluster)
        executor = Client(cluster)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

cache = MemoryLRUCache(max_bytes=int(config.cache.max_bytes))

# ---------------------------------------------------------------------------
# Redis  (both sync and async clients share the same pool defaults)
# ---------------------------------------------------------------------------
_redis_max_conns = int(getattr(config.redis, "max_connections", 50))

_REDIS_POOL = {
    "host": config.redis.host,
    # Env vars take precedence over config.yaml so deploy-generated secrets
    # (written to .env.local) are always authoritative. config.yaml values
    # are dev-only fallbacks for bare ``uv run uvicorn`` without compose.
    "port": int(os.environ.get("DORIAN_REDIS_PORT") or config.redis.port),
    "username": config.redis.user,
    "password": os.environ.get("DORIAN_REDIS_PASSWORD") or config.redis.password,
    "decode_responses": True,
    "max_connections": _redis_max_conns,  # bounded pool — prevents fd exhaustion
    # ``socket_timeout`` must exceed any blocking read (XREADGROUP block=5s)
    # — see backend/eventbus_subscriber.py::_BLOCK_MS. Setting it equal to
    # the block window (the prior 5s value) raced the timeout against the
    # block return and produced 60+ false ``Timeout reading from redis``
    # warnings/min on idle streams. 30s gives a 25s grace beyond the
    # default block length and still trips on actually-hung connections.
    "socket_timeout": 30,           # seconds — fail fast on hung connections
    "socket_connect_timeout": 5,    # seconds — connection establishment
    "socket_keepalive": True,       # detect dead TCP connections early
    "retry_on_timeout": True,       # auto-retry on transient timeouts
    "health_check_interval": 30,    # seconds — periodic PING on idle connections
}

aioredis = RedisAsync(**_REDIS_POOL)

redis = Redis(**_REDIS_POOL)

# ---------------------------------------------------------------------------
# Document store (Postgres-backed) — ``expdb``
# ---------------------------------------------------------------------------
# ``expdb`` is the canonical experiment-database handle. Every collection
# (pipelines, datasets, rewrites, vault_secrets, …) lives in its own
# Postgres ``doc_<name>`` JSONB table served by the async facade in
# ``backend.db.pg_docstore``. Callsites use ``expdb.<collection>.<operation>``
# with the docstore subset the facade exposes (find_one, find, sort,
# insert_one, insert_many, update_one with upsert, delete_one, aggregate
# with $match/$sort/$sample, etc.).
from backend.db import Database as _ExpDatabase

expdb = _ExpDatabase()

# ---------------------------------------------------------------------------
# PostgreSQL (async) — lazy-init pool (needs running event loop)
# ---------------------------------------------------------------------------
import asyncio
import asyncpg

_pg_pool: asyncpg.Pool | None = None


def _pool_is_stale(pool: asyncpg.Pool) -> bool:
    """True when the cached pool can't serve the running event loop.

    asyncpg pools bind connections to the loop that created them.
    The RL trainer drives writes via repeated ``asyncio.run(...)``
    calls, each of which creates and tears down a fresh loop — after
    the first call, the cached pool's connections reference a closed
    loop and subsequent acquires raise "Event loop is closed".
    """
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        return False
    pool_loop = getattr(pool, "_loop", None)
    if pool_loop is None:
        return False
    return pool_loop is not current or pool_loop.is_closed()


async def get_pg_pool() -> asyncpg.Pool:
    """Return (and lazily create) the asyncpg connection pool."""
    global _pg_pool
    if _pg_pool is not None and _pool_is_stale(_pg_pool):
        try:
            _pg_pool.terminate()
        except Exception:
            pass
        _pg_pool = None
    if _pg_pool is None:
        pg = config.postgresql
        pg_password = os.environ.get("DORIAN_POSTGRES_PASSWORD") or pg.password
        _pg_pool = await asyncpg.create_pool(
            host=pg.host,
            port=int(pg.port),
            user="dorian",
            password=pg_password,
            database="dorian",
            min_size=4,
            max_size=50,
            command_timeout=30,
        )
    return _pg_pool


async def close_pg_pool() -> None:
    """Gracefully close the asyncpg pool (call from lifespan shutdown)."""
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None
