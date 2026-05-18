"""
backend/cache.py
-----------------
Thread-safe, memory-aware LRU cache for DataFrames and intermediates.

Because the Dask cluster runs with ``processes=False``, all workers share
the same address space.  A single in-process cache is therefore accessible
from FastAPI handlers, event-bus handlers, and every Dask worker thread
without serialisation or IPC.

Usage in Dask task graphs
~~~~~~~~~~~~~~~~~~~~~~~~~
Replace ``pd.read_csv`` with :func:`cached_read_csv` — it has the same
single-argument signature (file path) and is a drop-in substitute::

    graph = {
        'fpath': '/data/user/iris.csv',
        'df': (cached_read_csv, 'fpath'),   # <-- instead of (pd.read_csv, 'fpath')
    }
"""
from __future__ import annotations

import hashlib
import sys
import threading
from collections import OrderedDict
from typing import Any, Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _estimate_size(value: Any) -> int:
    """Estimate in-memory size of *value* in bytes."""
    if isinstance(value, pd.DataFrame):
        return int(value.memory_usage(deep=True).sum())
    if isinstance(value, pd.Series):
        return int(value.memory_usage(deep=True))
    if hasattr(value, "nbytes"):          # numpy arrays
        return int(value.nbytes)
    return sys.getsizeof(value)


def _file_content_hash(fpath: str) -> str:
    """Fast content hash of a file using blake2b (16-byte digest, 64 KB chunks)."""
    h = hashlib.blake2b(digest_size=16)
    with open(fpath, "rb") as f:
        while chunk := f.read(65_536):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------

class _CacheEntry:
    __slots__ = ("key", "value", "mem_bytes", "content_hash")

    def __init__(self, key: str, value: Any, mem_bytes: int, content_hash: str = ""):
        self.key = key
        self.value = value
        self.mem_bytes = mem_bytes
        self.content_hash = content_hash


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------

class MemoryLRUCache:
    """Thread-safe, memory-aware LRU cache.

    Parameters
    ----------
    max_bytes : int
        Upper bound on total cached data (bytes).  When exceeded the
        least-recently-used entries are evicted until there is room.
    """

    def __init__(self, max_bytes: int = 2 * 1024 ** 3):
        self._lock = threading.Lock()
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max_bytes = max_bytes
        self._current_bytes = 0
        # Reverse index: file path -> content_hash (for invalidation)
        self._path_to_hash: dict[str, str] = {}
        # Simple counters
        self._hits = 0
        self._misses = 0

    # -- read --

    def get(self, key: str) -> Optional[Any]:
        """Return cached value or ``None`` on miss.  Promotes to MRU on hit."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return entry.value

    # -- write --

    def put(self, key: str, value: Any, *, content_hash: str = "") -> None:
        """Insert *value* under *key*, evicting LRU entries as needed."""
        mem_bytes = _estimate_size(value)
        with self._lock:
            # Replace existing entry
            if key in self._store:
                self._current_bytes -= self._store[key].mem_bytes
                del self._store[key]
            # Evict until there is room
            while self._current_bytes + mem_bytes > self._max_bytes and self._store:
                self._evict_one()
            # If value alone exceeds the budget, skip caching
            if mem_bytes > self._max_bytes:
                return
            entry = _CacheEntry(key, value, mem_bytes, content_hash)
            self._store[key] = entry
            self._store.move_to_end(key)
            self._current_bytes += mem_bytes

    # -- invalidation --

    def invalidate(self, key: str) -> bool:
        """Remove *key*.  Returns ``True`` if it existed."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            self._current_bytes -= entry.mem_bytes
            del self._store[key]
            return True

    def invalidate_by_path(self, fpath: str) -> bool:
        """Remove the DataFrame cached for *fpath* (used on re-upload)."""
        with self._lock:
            content_hash = self._path_to_hash.pop(fpath, None)
            if content_hash is None:
                return False
            key = f"df:{content_hash}"
            entry = self._store.get(key)
            if entry is None:
                return False
            self._current_bytes -= entry.mem_bytes
            del self._store[key]
            return True

    def register_path(self, fpath: str, content_hash: str) -> None:
        """Record *fpath* → *content_hash* mapping, evicting stale entries."""
        with self._lock:
            old_hash = self._path_to_hash.get(fpath)
            if old_hash and old_hash != content_hash:
                old_key = f"df:{old_hash}"
                if old_key in self._store:
                    self._current_bytes -= self._store[old_key].mem_bytes
                    del self._store[old_key]
            self._path_to_hash[fpath] = content_hash

    # -- introspection --

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._store),
                "current_bytes": self._current_bytes,
                "max_bytes": self._max_bytes,
                "utilization": round(self._current_bytes / self._max_bytes, 3) if self._max_bytes else 0,
                "hits": self._hits,
                "misses": self._misses,
            }

    # -- internal --

    def _evict_one(self) -> None:
        """Pop the LRU entry.  *Must* be called with ``_lock`` held."""
        _key, entry = self._store.popitem(last=False)
        self._current_bytes -= entry.mem_bytes


# ---------------------------------------------------------------------------
# Convenience: cached CSV reader
# ---------------------------------------------------------------------------

def _arrow_to_numpy(df: pd.DataFrame) -> pd.DataFrame:
    """Convert PyArrow-backed columns to plain numpy dtypes.

    pandas ≥3.0 defaults to ``ArrowStringArray`` for string columns.
    sklearn and many downstream consumers cannot index into Arrow arrays
    (``"only integer scalar arrays can be converted to a scalar index"``).

    ``astype(object)`` is used instead of ``df[c] = df[c].to_numpy()``
    because pandas 3.0 type inference re-wraps numpy string arrays as Arrow
    on assignment.  ``astype(object)`` explicitly sets dtype=object which
    prevents the re-wrapping.
    """
    arrow_cols = [
        c for c in df.columns
        if "Arrow" in type(df[c].array).__name__
    ]
    if not arrow_cols:
        return df
    df = df.copy()
    for c in arrow_cols:
        df[c] = df[c].astype(object)
    return df


def cached_read_csv(fpath: str) -> pd.DataFrame:
    """Drop-in replacement for ``pd.read_csv`` that checks the LRU cache first.

    Content-addressable: identical file content at different paths shares a
    single cache entry.  Thread-safe.

    Eagerly converts Arrow-backed columns to numpy so all downstream
    consumers (sklearn, profiler, fairness checks) receive compatible dtypes.

    Internally parses via ``pyarrow.csv.read_csv`` (via
    :func:`cached_read_table`) so every call through either entry
    point pays for the CSV parse at most once per content hash.
    Arrow-first parsing is consistently faster than ``pd.read_csv``
    on typical tabular datasets. The derived pandas DataFrame is
    cached too, so repeat calls return the same object without
    re-running ``to_pandas``.
    """
    from backend.envs import cache

    fpath_str = str(fpath)
    content_hash = _file_content_hash(fpath_str)
    key = f"df:{content_hash}"

    cached = cache.get(key)
    if cached is not None:
        return cached

    df = _arrow_to_numpy(cached_read_table(fpath).to_pandas())
    cache.put(key, df, content_hash=content_hash)
    cache.register_path(fpath_str, content_hash)
    return df


def cached_read_table(fpath: str):
    """Arrow-native CSV reader — returns ``pa.Table`` with the shared cache.

    Complementary to :func:`cached_read_csv`: same LRU, same content
    hash, but the cached artefact is an ``pa.Table`` so Arrow-native
    consumers (profiling metafeatures, DQ checks once they migrate)
    can skip the pandas materialisation step entirely. Used as the
    single-parse entry point in
    ``dorian.exec.profile.profile_and_quality``'s Dask graph, where
    downstream nodes derive the pandas view from this one table.
    """
    import pyarrow.csv as pacsv

    from backend.envs import cache

    fpath_str = str(fpath)
    content_hash = _file_content_hash(fpath_str)
    key = f"table:{content_hash}"

    cached = cache.get(key)
    if cached is not None:
        return cached

    table = pacsv.read_csv(fpath_str)
    cache.put(key, table, content_hash=content_hash)
    cache.register_path(fpath_str, content_hash)
    return table
