"""
dorian.mcp._backend_min — minimal sync Redis + Postgres handles for the MCP
subprocess.

Importing ``backend.envs`` from the MCP server subprocess is a trap:
it starts a Dask LocalCluster, initialises the executor, and sets up
PostgreSQL's async pool lazy-init. All of that is inappropriate for a
short-lived sync tool-call process and the Dask start can hang or
print to stdout, corrupting the MCP JSON-RPC stream.

This module reads the same ``config/config.yaml`` via dynaconf and
exposes ONLY sync clients:

    mcp_sync_redis()  — redis.Redis bound to config.redis
    mcp_sync_expdb()  — sync document-store handle (Postgres-backed)

Both are lazy — connections open on first use, not on module import.

The ``mcp_sync_expdb`` name is a historical alias — see the note below.
Returns a sync wrapper around the Postgres
document-store facade (``backend.db.pg_docstore``). The wrapper runs each
async operation in its own asyncio loop — acceptable for MCP's low call
volume but NOT safe to use from async request handlers. For the main
backend path use ``backend.envs.expdb`` (native async) instead.
"""
from __future__ import annotations

import asyncio
from typing import Any


_REDIS: Any = None
_PG_SYNC: Any = None


def mcp_sync_redis():
    """Return a lazily-initialised sync redis.Redis client."""
    global _REDIS
    if _REDIS is not None:
        return _REDIS
    from backend.config import config
    from redis import Redis
    r = config.redis
    _REDIS = Redis(
        host=r.host,
        port=r.port,
        username=r.user,
        password=r.password,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )
    return _REDIS


# ---------------------------------------------------------------------------
# Sync wrapper over the async Postgres document-store facade
# ---------------------------------------------------------------------------


class _SyncCursor:
    """Chainable sync cursor: ``col.find(...).sort(...).limit(...)`` → list.

    Collection-level find/sort/limit are buffered here and the async facade
    is driven once when the cursor is iterated or materialised (``list(cur)``).
    """

    def __init__(self, coll: "_SyncCollection", filt, projection=None):
        self._coll = coll
        self._filt = filt or {}
        self._projection = projection
        self._sort_spec: list[tuple[str, int]] = []
        self._limit: int | None = None
        self._skip: int = 0

    def sort(self, spec, direction=None):
        if isinstance(spec, str):
            self._sort_spec.append((spec, direction if direction is not None else 1))
        else:
            for item in spec:
                if isinstance(item, tuple):
                    self._sort_spec.append(item)
                else:
                    self._sort_spec.append((item, 1))
        return self

    def limit(self, n: int) -> "_SyncCursor":
        self._limit = n
        return self

    def skip(self, n: int) -> "_SyncCursor":
        self._skip = n
        return self

    def _materialise(self) -> list[dict]:
        async def _f():
            cur = self._coll._async_coll.find(self._filt, projection=self._projection)
            if self._sort_spec:
                cur = cur.sort(self._sort_spec)
            if self._skip:
                cur = cur.skip(self._skip)
            if self._limit is not None:
                cur = cur.limit(self._limit)
            return await cur.to_list(self._limit)
        return self._coll._run(_f())

    def __iter__(self):
        return iter(self._materialise())


class _SyncCollection:
    def __init__(self, sync_db: "_SyncDatabase", name: str):
        self._sync_db = sync_db
        self._name = name

    def _run(self, coro):
        """Execute ``coro`` in a fresh event loop. Acceptable for MCP's
        one-call-per-invocation pattern; do NOT use from async handlers.
        """
        return asyncio.run(coro)

    @property
    def _async_coll(self):
        return self._sync_db._async_db[self._name]

    def find_one(self, filter=None, projection=None, sort=None):
        async def _f():
            return await self._async_coll.find_one(filter, projection, sort=sort)
        return self._run(_f())

    def find(self, filter=None, projection=None) -> _SyncCursor:
        return _SyncCursor(self, filter, projection)

    def insert_one(self, doc):
        async def _f():
            return await self._async_coll.insert_one(doc)
        return self._run(_f())

    def update_one(self, filter, update, upsert=False):
        async def _f():
            return await self._async_coll.update_one(filter, update, upsert=upsert)
        return self._run(_f())

    def delete_one(self, filter):
        async def _f():
            return await self._async_coll.delete_one(filter)
        return self._run(_f())

    def count_documents(self, filter=None):
        async def _f():
            return await self._async_coll.count_documents(filter or {})
        return self._run(_f())


class _SyncDatabase:
    def __init__(self):
        # Lazy — build the async facade only on first use so importing this
        # module stays side-effect-free.
        self._async_db_obj = None

    @property
    def _async_db(self):
        if self._async_db_obj is None:
            from backend.db.pg_docstore import Database
            self._async_db_obj = Database()
        return self._async_db_obj

    def __getitem__(self, name: str) -> _SyncCollection:
        return _SyncCollection(self, name)

    def __getattr__(self, name: str) -> _SyncCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return _SyncCollection(self, name)


def mcp_sync_expdb():
    """Return a sync document-store handle.

    Legacy name kept so MCP callsites don't change. Internally this is the
    Postgres facade wrapped in a sync adapter.
    """
    global _PG_SYNC
    if _PG_SYNC is None:
        _PG_SYNC = _SyncDatabase()
    return _PG_SYNC
