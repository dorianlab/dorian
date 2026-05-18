"""
tests/conftest.py
-----------------
Shared backend stubs for all tests.

Inserts mock modules into ``sys.modules`` so tests can import ``dorian.*``
without a running Redis, Neo4j, Dask, or docstore.  Runs once per session
before any test module is collected.
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock, AsyncMock


# ---------------------------------------------------------------------------
# Stateful Redis mock — tracks keys so write-then-read works in tests.
# ---------------------------------------------------------------------------

class _StatefulRedisMock:
    """In-memory dict-backed Redis mock supporting get/set/delete/exists/etc.

    Async variants (aioredis) use the same store so handlers that write
    via aioredis and read via sync redis share state.
    """

    def __init__(self):
        self._store: dict[str, str | bytes] = {}
        self._lists: dict[str, list] = {}
        self._sets: dict[str, set] = {}
        self._streams: dict[str, list[dict]] = {}

    # --- string ops ---
    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self._store:
            return False
        self._store[key] = value
        return True

    def delete(self, *keys):
        count = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                count += 1
        return count

    def exists(self, key):
        return key in self._store

    # --- list ops ---
    def rpush(self, key, *values):
        lst = self._lists.setdefault(key, [])
        lst.extend(values)
        return len(lst)

    def lrange(self, key, start, stop):
        lst = self._lists.get(key, [])
        if stop == -1:
            return lst[start:]
        return lst[start:stop + 1]

    def lrem(self, key, count, value):
        lst = self._lists.get(key, [])
        removed = 0
        while value in lst and (count == 0 or removed < abs(count)):
            lst.remove(value)
            removed += 1
        return removed

    def ltrim(self, key, start, stop):
        lst = self._lists.get(key, [])
        if not lst:
            return True
        if stop == -1:
            new = lst[start:]
        else:
            new = lst[start:stop + 1 if stop >= 0 else len(lst) + stop + 1]
        self._lists[key] = new
        return True

    def llen(self, key):
        return len(self._lists.get(key, []))

    def expire(self, key, seconds):
        # Mock: ignore TTL semantics but report success like real Redis.
        return 1 if key in self._store or key in self._lists or key in self._sets or key in self._streams else 0

    # --- set ops ---
    def sadd(self, key, *members):
        s = self._sets.setdefault(key, set())
        before = len(s)
        s.update(members)
        return len(s) - before

    def srem(self, key, *members):
        s = self._sets.get(key, set())
        before = len(s)
        s -= set(members)
        return before - len(s)

    def smembers(self, key):
        return self._sets.get(key, set()).copy()

    def sismember(self, key, member):
        return member in self._sets.get(key, set())

    # --- stream ops ---
    def xadd(self, key, fields, *, maxlen=None, approximate=True):
        stream = self._streams.setdefault(key, [])
        stream.append(fields)
        # Honour MAXLEN trimming (approximate flag ignored in mock).
        if maxlen is not None and len(stream) > maxlen:
            del stream[: len(stream) - maxlen]
        return f"{len(stream)}-0"

    # --- scan ---
    def scan(self, cursor=0, match=None, count=100):
        import fnmatch
        keys = list(self._store.keys())
        if match:
            keys = [k for k in keys if fnmatch.fnmatch(k, match)]
        return (0, keys)

    # --- utility ---
    def close(self):
        pass

    def reset(self):
        """Clear all state between tests."""
        self._store.clear()
        self._lists.clear()
        self._sets.clear()
        self._streams.clear()

    # --- pipeline (no-op for tests) ---
    def pipeline(self, **kwargs):
        return self

    def execute(self):
        return []


class _AsyncRedisMock(_StatefulRedisMock):
    """Wraps _StatefulRedisMock to return coroutines for all methods."""

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name.startswith("_") or name in ("reset", "close"):
            return attr
        if callable(attr) and not asyncio.iscoroutinefunction(attr):
            async def _async_wrapper(*args, **kwargs):
                return attr(*args, **kwargs)
            _async_wrapper.__name__ = name
            return _async_wrapper
        return attr


# Shared store so sync (redis) and async (aioredis) see the same data.
_redis_store = _StatefulRedisMock()
_aioredis_store = _AsyncRedisMock()
_aioredis_store._store = _redis_store._store
_aioredis_store._lists = _redis_store._lists
_aioredis_store._sets = _redis_store._sets
_aioredis_store._streams = _redis_store._streams


# ---------------------------------------------------------------------------
# Stateful docstore mock — dict-backed collections for write/read tests.
# ---------------------------------------------------------------------------

class _DocCollection:
    """Minimal async docstore collection mock with dict-based storage."""

    def __init__(self):
        self._docs: dict[str, dict] = {}

    async def insert_one(self, doc):
        _id = doc.get("_id") or str(id(doc))
        self._docs[str(_id)] = dict(doc)
        return MagicMock(inserted_id=_id)

    async def find_one(self, filter_dict=None, sort=None):
        if not filter_dict:
            return next(iter(self._docs.values()), None)
        for doc in self._docs.values():
            if all(doc.get(k) == v for k, v in filter_dict.items()):
                return dict(doc)
        return None

    async def update_one(self, filter_dict, update, upsert=False):
        target = None
        for doc in self._docs.values():
            if all(doc.get(k) == v for k, v in filter_dict.items()):
                target = doc
                break
        if target is None and upsert:
            target = dict(filter_dict)
            insert_fields = update.get("$setOnInsert", {})
            target.update(insert_fields)
            _id = target.get("_id") or str(id(target))
            target["_id"] = _id
            self._docs[str(_id)] = target
        if target is not None:
            set_fields = update.get("$set", {})
            target.update(set_fields)
        return MagicMock(modified_count=1 if target else 0, upserted_id=target.get("_id") if target else None)

    async def delete_one(self, filter_dict):
        to_remove = None
        for key, doc in self._docs.items():
            if all(doc.get(k) == v for k, v in filter_dict.items()):
                to_remove = key
                break
        if to_remove:
            del self._docs[to_remove]
            return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    def find(self, filter_dict=None):
        return _DocCursor(self._docs, filter_dict)

    def reset(self):
        self._docs.clear()


class _DocCursor:
    """Minimal async cursor for iteration with sort/limit support."""

    def __init__(self, docs, filter_dict=None):
        self._items = list(docs.values())
        if filter_dict:
            self._items = [
                d for d in self._items
                if all(d.get(k) == v for k, v in filter_dict.items())
            ]
        self._index = 0

    def sort(self, key, direction=-1):
        """Sort items (best-effort — compares raw values)."""
        try:
            self._items.sort(key=lambda d: d.get(key, ""), reverse=(direction == -1))
        except TypeError:
            pass
        return self

    def limit(self, n):
        """Limit the number of returned items."""
        self._items = self._items[:n]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        doc = self._items[self._index]
        self._index += 1
        return doc


class _docstoreMock:
    """Database mock that auto-creates collections on attribute access."""

    def __init__(self):
        self._collections: dict[str, _DocCollection] = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._collections:
            self._collections[name] = _DocCollection()
        return self._collections[name]

    def __getitem__(self, name):
        """Support bracket notation: expdb["collection_name"]."""
        if name not in self._collections:
            self._collections[name] = _DocCollection()
        return self._collections[name]

    async def list_collection_names(self):
        """Return the names of all collections that have documents."""
        return list(self._collections.keys())

    def reset(self):
        for col in self._collections.values():
            col.reset()
        self._collections.clear()


_docstore_mock = _docstoreMock()


# ---------------------------------------------------------------------------
# backend.* module stubs
# ---------------------------------------------------------------------------

_backend = types.ModuleType("backend")
_backend.__package__ = "backend"
sys.modules["backend"] = _backend

_envs = types.ModuleType("backend.envs")
_envs.aioredis = _aioredis_store
_envs.redis = _redis_store
_envs.executor = MagicMock()
_envs.expdb = _docstore_mock
sys.modules["backend.envs"] = _envs
_backend.envs = _envs

_events_mod = types.ModuleType("backend.events")


class _Event:
    """Minimal stub for backend.events.Event."""
    def __init__(self, type, data=None):
        self.type = type
        self.data = data or {}


_events_mod.Event = _Event
_events_mod.emit = MagicMock()
_events_mod.aemit = AsyncMock()
_events_mod.aemit_bg = AsyncMock()
_events_mod.verbose = MagicMock()
_events_mod.subscribe = MagicMock()
_events_mod.handlers = {}
sys.modules["backend.events"] = _events_mod
_backend.events = _events_mod

_cfg = types.ModuleType("backend.config")
_cfg.base = "."
_cfg.config = MagicMock()
sys.modules["backend.config"] = _cfg
_backend.config = _cfg

_repo = types.ModuleType("backend.repository")
_repo_doc = types.ModuleType("backend.repository.document")
_repo_doc.Document = type("Document", (), {})
sys.modules["backend.repository"] = _repo
sys.modules["backend.repository.document"] = _repo_doc
_backend.repository = _repo
_repo.document = _repo_doc

_rate_limit = types.ModuleType("backend.rate_limit")
_rate_limit.rate_limit = MagicMock()
_rate_limit.http_rate_limit = MagicMock(return_value=MagicMock())
sys.modules["backend.rate_limit"] = _rate_limit
_backend.rate_limit = _rate_limit

_cache = types.ModuleType("backend.cache")
_cache.cached_read_csv = MagicMock()
sys.modules["backend.cache"] = _cache
_backend.cache = _cache

# backend.hmac_auth stub
_hmac = types.ModuleType("backend.hmac_auth")
_hmac.HMACAuthMiddleware = MagicMock()
sys.modules["backend.hmac_auth"] = _hmac
_backend.hmac_auth = _hmac

# backend.queue stub
_queue = types.ModuleType("backend.queue")
_queue.submit_for_execution = MagicMock()
_queue.bridge_logic = AsyncMock()
sys.modules["backend.queue"] = _queue
_backend.queue = _queue

# backend.ws_rate_limit stub
_ws_rl = types.ModuleType("backend.ws_rate_limit")
_ws_rl.ws_rate_limit = MagicMock(return_value=MagicMock())
sys.modules["backend.ws_rate_limit"] = _ws_rl
_backend.ws_rate_limit = _ws_rl

# backend.utils stub
_utils = types.ModuleType("backend.utils")
_utils.sanitize_floats = lambda x: x
sys.modules["backend.utils"] = _utils
_backend.utils = _utils

# backend.infra stubs
_infra = types.ModuleType("backend.infra")
_infra.__package__ = "backend.infra"
sys.modules["backend.infra"] = _infra
_backend.infra = _infra

_infra_init = types.ModuleType("backend.infra.init")
sys.modules["backend.infra.init"] = _infra_init

_infra_dbs = types.ModuleType("backend.infra.dbs")
_infra_dbs.__package__ = "backend.infra.dbs"
sys.modules["backend.infra.dbs"] = _infra_dbs

_infra_docstore = types.ModuleType("backend.infra.dbs.expdb")
_infra_docstore.__package__ = "backend.infra.dbs.expdb"
sys.modules["backend.infra.dbs.expdb"] = _infra_docstore

_ranking_objs = types.ModuleType("backend.infra.dbs.expdb.ranking_objectives")
_ranking_objs._upsert_objectives = AsyncMock(return_value=[])
sys.modules["backend.infra.dbs.expdb.ranking_objectives"] = _ranking_objs

_eval_procs = types.ModuleType("backend.infra.dbs.expdb.evaluation_procedures")
_eval_procs.upsert_evaluation_procedure = AsyncMock(return_value={})
_eval_procs.get_evaluation_procedures = AsyncMock(return_value=[])
sys.modules["backend.infra.dbs.expdb.evaluation_procedures"] = _eval_procs


# ---------------------------------------------------------------------------
# classes (typeclass) stub
# ---------------------------------------------------------------------------


class _FakeTypeclass:
    """Stub for classes.typeclass that supports .instance() decorator."""
    def __init__(self, fn):
        self._fn = fn

    def instance(self, *args, **kwargs):
        return lambda fn: fn

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


_classes = types.ModuleType("classes")
_classes.typeclass = _FakeTypeclass
sys.modules["classes"] = _classes


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_stores():
    """Reset in-memory Redis + docstore state between every test."""
    _redis_store.reset()
    _docstore_mock.reset()
    _events_mod.aemit.reset_mock()
    _events_mod.emit.reset_mock()
    yield
    _redis_store.reset()
    _docstore_mock.reset()
