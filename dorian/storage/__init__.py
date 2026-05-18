"""Pluggable storage abstraction for dataset and pipeline files.

Usage::

    from dorian.storage import get_backend

    backend = get_backend()           # configured via config.yaml
    await backend.write(key, data)    # key = "session/dataset.csv"
    data = await backend.read(key)
    url  = backend.resolve(key)       # local path or presigned URL
"""

from dorian.storage.backend import StorageBackend, get_backend

__all__ = ["StorageBackend", "get_backend"]
