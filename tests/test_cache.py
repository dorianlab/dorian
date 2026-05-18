"""
tests/test_cache.py
--------------------
Unit tests for backend.cache (MemoryLRUCache and cached_read_csv).

The cache module has no hard dependency on the backend at import time
(cached_read_csv uses a deferred import), so tests run without Redis/Dask.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

# Backend stubs are in conftest.py (loaded automatically by pytest).
# Load backend.cache directly from file to get the real implementation.
import importlib.util

_project_root = str(Path(__file__).resolve().parents[1])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

_cache_path = os.path.join(_project_root, "backend", "cache.py")
_spec = importlib.util.spec_from_file_location("backend.cache", _cache_path)
_cache_mod = importlib.util.module_from_spec(_spec)
sys.modules["backend.cache"] = _cache_mod
_spec.loader.exec_module(_cache_mod)

MemoryLRUCache = _cache_mod.MemoryLRUCache
_estimate_size = _cache_mod._estimate_size
_file_content_hash = _cache_mod._file_content_hash
cached_read_csv = _cache_mod.cached_read_csv
cached_read_table = _cache_mod.cached_read_table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _small_df(nrows: int = 10, ncols: int = 3) -> pd.DataFrame:
    import numpy as np
    return pd.DataFrame(
        np.random.randn(nrows, ncols),
        columns=[f"col_{i}" for i in range(ncols)],
    )


def _write_csv(tmp: str, df: pd.DataFrame) -> str:
    path = os.path.join(tmp, "data.csv")
    df.to_csv(path, index=False)
    return path


# ---------------------------------------------------------------------------
# Tests: MemoryLRUCache core
# ---------------------------------------------------------------------------

class TestLRUCache(unittest.TestCase):

    def test_put_and_get(self):
        cache = MemoryLRUCache(max_bytes=10 * 1024 ** 2)
        cache.put("k1", "hello")
        self.assertEqual(cache.get("k1"), "hello")

    def test_get_miss_returns_none(self):
        cache = MemoryLRUCache()
        self.assertIsNone(cache.get("nonexistent"))

    def test_put_updates_existing_key(self):
        cache = MemoryLRUCache()
        cache.put("k", "v1")
        cache.put("k", "v2")
        self.assertEqual(cache.get("k"), "v2")
        self.assertEqual(cache.stats()["entries"], 1)

    def test_lru_eviction_order(self):
        """Least-recently-used entry should be evicted first."""
        import numpy as np
        # Use numpy arrays for predictable sizing (nbytes is exact)
        a = np.zeros(100)   # 800 bytes
        b = np.zeros(100)   # 800 bytes
        c = np.zeros(100)   # 800 bytes
        # Budget fits 2 arrays but not 3
        cache = MemoryLRUCache(max_bytes=1700)
        cache.put("old", a)
        cache.put("new", b)
        # Access "old" so it becomes MRU
        cache.get("old")
        # Insert third entry — must evict one; "new" is LRU
        cache.put("big", c)
        self.assertIsNone(cache.get("new"))
        self.assertIsNotNone(cache.get("old"))

    def test_oversized_item_not_cached(self):
        """An item larger than max_bytes should not be stored."""
        cache = MemoryLRUCache(max_bytes=10)
        cache.put("huge", "x" * 1000)
        self.assertIsNone(cache.get("huge"))
        self.assertEqual(cache.stats()["entries"], 0)

    def test_invalidate(self):
        cache = MemoryLRUCache()
        cache.put("k", "v")
        self.assertTrue(cache.invalidate("k"))
        self.assertIsNone(cache.get("k"))
        # Second invalidate returns False
        self.assertFalse(cache.invalidate("k"))

    def test_invalidate_by_path(self):
        cache = MemoryLRUCache()
        cache.put("df:abc123", "dataframe", content_hash="abc123")
        cache.register_path("/data/file.csv", "abc123")
        self.assertTrue(cache.invalidate_by_path("/data/file.csv"))
        self.assertIsNone(cache.get("df:abc123"))

    def test_register_path_evicts_stale_hash(self):
        cache = MemoryLRUCache()
        cache.put("df:old_hash", "old_df", content_hash="old_hash")
        cache.register_path("/data/file.csv", "old_hash")
        # Re-register with a new hash — old entry should be evicted
        cache.register_path("/data/file.csv", "new_hash")
        self.assertIsNone(cache.get("df:old_hash"))

    def test_stats(self):
        cache = MemoryLRUCache(max_bytes=1024)
        cache.put("a", "hello")
        cache.get("a")       # hit
        cache.get("missing")  # miss
        s = cache.stats()
        self.assertEqual(s["entries"], 1)
        self.assertEqual(s["hits"], 1)
        self.assertEqual(s["misses"], 1)
        self.assertEqual(s["max_bytes"], 1024)
        self.assertGreater(s["current_bytes"], 0)

    def test_memory_tracking_dataframe(self):
        cache = MemoryLRUCache(max_bytes=10 * 1024 ** 2)
        df = _small_df(100, 5)
        expected_size = int(df.memory_usage(deep=True).sum())
        cache.put("df", df)
        self.assertEqual(cache.stats()["current_bytes"], expected_size)


# ---------------------------------------------------------------------------
# Tests: thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety(unittest.TestCase):

    def test_concurrent_put_get(self):
        """Hammer the cache from multiple threads and check for crashes."""
        cache = MemoryLRUCache(max_bytes=1024 * 1024)
        errors = []

        def writer(tid: int):
            try:
                for i in range(200):
                    cache.put(f"t{tid}_{i}", f"val_{i}")
            except Exception as exc:
                errors.append(exc)

        def reader(tid: int):
            try:
                for i in range(200):
                    cache.get(f"t{tid}_{i}")
            except Exception as exc:
                errors.append(exc)

        threads = []
        for t in range(8):
            threads.append(threading.Thread(target=writer, args=(t,)))
            threads.append(threading.Thread(target=reader, args=(t,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")


# ---------------------------------------------------------------------------
# Tests: helpers
# ---------------------------------------------------------------------------

class TestEstimateSize(unittest.TestCase):

    def test_dataframe(self):
        df = _small_df(10, 3)
        size = _estimate_size(df)
        self.assertGreater(size, 0)
        self.assertEqual(size, int(df.memory_usage(deep=True).sum()))

    def test_series(self):
        s = pd.Series([1, 2, 3])
        size = _estimate_size(s)
        self.assertGreater(size, 0)

    def test_numpy_array(self):
        import numpy as np
        arr = np.zeros((100, 100))
        self.assertEqual(_estimate_size(arr), arr.nbytes)

    def test_plain_python(self):
        size = _estimate_size("hello")
        self.assertGreater(size, 0)


class TestFileContentHash(unittest.TestCase):

    def test_same_content_same_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path1 = os.path.join(tmp, "a.csv")
            path2 = os.path.join(tmp, "b.csv")
            content = b"col1,col2\n1,2\n3,4\n"
            for p in (path1, path2):
                with open(p, "wb") as f:
                    f.write(content)
            self.assertEqual(_file_content_hash(path1), _file_content_hash(path2))

    def test_different_content_different_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path1 = os.path.join(tmp, "a.csv")
            path2 = os.path.join(tmp, "b.csv")
            with open(path1, "wb") as f:
                f.write(b"col1\n1\n")
            with open(path2, "wb") as f:
                f.write(b"col1\n2\n")
            self.assertNotEqual(_file_content_hash(path1), _file_content_hash(path2))


# ---------------------------------------------------------------------------
# Tests: cached_read_csv
# ---------------------------------------------------------------------------

class TestCachedReadCsv(unittest.TestCase):

    def test_read_and_cache(self):
        """First call parses once, second call hits both caches.

        After the Arrow-first refactor, the pandas path goes through
        the table cache first: a cold call misses on both ``df:`` and
        ``table:`` and materialises each; a warm call hits the df
        cache and short-circuits before re-entering the table path.
        """
        df = _small_df(5, 2)
        cache = MemoryLRUCache(max_bytes=10 * 1024 ** 2)

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_csv(tmp, df)

            # Patch the deferred import to use our local cache
            with patch("backend.cache.cache", cache, create=True), \
                 patch.dict(sys.modules, {"backend.envs": MagicMock(cache=cache)}):
                # Ensure backend.envs.cache resolves to our cache
                sys.modules["backend.envs"].cache = cache

                result1 = cached_read_csv(path)
                self.assertEqual(cache.stats()["misses"], 2)
                self.assertEqual(cache.stats()["hits"], 0)

                result2 = cached_read_csv(path)
                self.assertEqual(cache.stats()["hits"], 1)
                self.assertEqual(cache.stats()["misses"], 2)

            pd.testing.assert_frame_equal(result1, result2)

    def test_content_addressable_dedup(self):
        """Identical files at different paths share the cache.

        Arrow + pandas views of the same file content get two
        distinct cache entries (one ``table:<hash>``, one
        ``df:<hash>``) but two files with identical content still
        collapse to the same two entries — not four.
        """
        df = _small_df(5, 2)
        cache = MemoryLRUCache(max_bytes=10 * 1024 ** 2)

        with tempfile.TemporaryDirectory() as tmp:
            path_a = os.path.join(tmp, "a.csv")
            path_b = os.path.join(tmp, "b.csv")
            df.to_csv(path_a, index=False)
            df.to_csv(path_b, index=False)

            with patch.dict(sys.modules, {"backend.envs": MagicMock(cache=cache)}):
                sys.modules["backend.envs"].cache = cache

                cached_read_csv(path_a)
                cached_read_csv(path_b)

                # Two entries (table + df) rather than four — the
                # second path is a content-hash hit on both caches.
                self.assertEqual(cache.stats()["entries"], 2)

    def test_table_and_csv_share_parse(self):
        """``cached_read_table`` and ``cached_read_csv`` share the CSV parse.

        After ``cached_read_table`` has populated ``table:<hash>``, a
        subsequent ``cached_read_csv`` on the same content only needs
        to materialise the pandas view — it does NOT re-parse the
        CSV from disk (the table cache hits).
        """
        df = _small_df(5, 2)
        cache = MemoryLRUCache(max_bytes=10 * 1024 ** 2)

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_csv(tmp, df)

            with patch.dict(sys.modules, {"backend.envs": MagicMock(cache=cache)}):
                sys.modules["backend.envs"].cache = cache

                # Cold: populates table cache.
                cached_read_table(path)
                table_entries_after_first = cache.stats()["entries"]

                # Follow-up pandas call reuses the table cache; only
                # the ``df:`` entry is added.
                cached_read_csv(path)

                self.assertEqual(
                    cache.stats()["entries"],
                    table_entries_after_first + 1,
                )


if __name__ == "__main__":
    unittest.main()
