"""Postgres-backed document store for the experiment database (``expdb``).

``Database`` / ``Collection`` / ``Cursor`` in ``pg_docstore`` expose the subset
of the async docstore API Dorian historically used, backed by per-collection
``doc_<name>`` JSONB tables (each keyed on ``id`` with a ``data`` column).

Entry point: ``await get_pg_db()`` returns a ``Database`` instance (also
the value ``backend.envs.expdb`` holds).
"""

from backend.db.pg_docstore import (
    Collection,
    Database,
    DeleteResult,
    InsertOneResult,
    UpdateResult,
    get_pg_db,
)

__all__ = [
    "Collection",
    "Database",
    "DeleteResult",
    "InsertOneResult",
    "UpdateResult",
    "get_pg_db",
]
