"""Local filesystem storage backend.

This is the default backend for single-machine deployments.  Files are
stored under a configurable root directory (defaults to ``data/``).
"""

from __future__ import annotations

from pathlib import Path

import aiofiles

from dorian.storage.backend import _register


@_register("local")
class LocalStorageBackend:
    """Store files on the local filesystem."""

    def __init__(self, root: str | None = None) -> None:
        if root is None:
            try:
                from backend.config import config
                root = getattr(config.fs, "data", "data/")
            except Exception:
                root = "data/"
        self._root = Path(root).resolve()

    async def write(self, key: str, data: bytes) -> None:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)

    async def read(self, key: str) -> bytes:
        path = self._root / key
        if not path.exists():
            raise FileNotFoundError(f"Storage key not found: {key}")
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def exists(self, key: str) -> bool:
        return (self._root / key).exists()

    def resolve(self, key: str) -> str:
        return (self._root / key).resolve().as_posix()

    async def delete(self, key: str) -> None:
        path = self._root / key
        if path.exists():
            path.unlink()
