"""Storage backend protocol and factory.

The ``StorageBackend`` protocol defines the minimal contract for reading,
writing, and resolving dataset/pipeline files.  Implementations live in
sibling modules (``local.py``, future ``s3.py``, etc.).

The factory ``get_backend()`` reads ``config.storage.backend`` (default:
``"local"``) and returns a singleton instance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

_BACKENDS: dict[str, type["StorageBackend"]] = {}


def _register(name: str):
    """Class decorator that registers a backend implementation."""
    def decorator(cls):
        _BACKENDS[name] = cls
        return cls
    return decorator


@runtime_checkable
class StorageBackend(Protocol):
    """Minimal file-storage contract.

    All keys are *logical* paths relative to the storage root, e.g.
    ``"<session_id>/dataset.csv"``.  Backends translate these into their
    own addressing scheme (local filesystem path, S3 key, NFS mount, …).
    """

    async def write(self, key: str, data: bytes) -> None:
        """Persist *data* under *key*, creating parent directories/prefixes."""
        ...

    async def read(self, key: str) -> bytes:
        """Return the full contents of *key*.  Raise ``FileNotFoundError``
        if the key does not exist."""
        ...

    async def exists(self, key: str) -> bool:
        """Return ``True`` if *key* exists in the store."""
        ...

    def resolve(self, key: str) -> str:
        """Return a resolvable reference for *key*.

        For local storage this is an absolute filesystem path.
        For remote stores this could be a presigned URL or URI.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove *key* from the store.  No-op if it doesn't exist."""
        ...


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------
_instance: StorageBackend | None = None


def get_backend() -> StorageBackend:
    """Return the configured storage backend (singleton)."""
    global _instance
    if _instance is not None:
        return _instance

    # Lazy import to avoid circular dependency with backend.config
    try:
        from backend.config import config
        backend_name = getattr(getattr(config, "storage", None), "backend", "local")
        root = getattr(getattr(config, "storage", None), "root", None)
    except Exception:
        backend_name = "local"
        root = None

    # Import implementations so they register themselves
    import dorian.storage.local  # noqa: F401

    cls = _BACKENDS.get(backend_name)
    if cls is None:
        raise ValueError(
            f"Unknown storage backend {backend_name!r}. "
            f"Available: {', '.join(_BACKENDS)}"
        )

    _instance = cls(root=root) if root else cls()
    return _instance
