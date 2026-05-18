"""
dorian/code/extraction_store.py
-------------------------------
Persistence layer for pipeline extractions.

The docstore stores document blobs (source code, initial DAG, auto-extracted DAG,
user-corrected DAG).  PostgreSQL stores a relational index with IDs + rules
version hash for regression testing and cross-session querying.

This follows the project's existing storage pattern:
- the docstore for ``datasets`` / ``pipelines`` / ``sessions`` / ``snippets``
- PostgreSQL for ``pipelines`` / ``datasets`` / ``evaluations`` / ``interactions``
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from dorian.dag import DAG


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

async def persist_extraction(
    extraction_id: str,
    code: str,
    language: str,
    rules_version: str,
    initial_dag: DAG,
    auto_dag: DAG,
    session: str | None = None,
    uid: str | None = None,
    filename: str | None = None,
) -> None:
    """Write a new extraction record to the docstore + Postgres.

    Called fire-and-forget from the ``/extract`` endpoint after a successful
    parse.  Failures are logged but do not propagate to the caller.
    """
    from backend.envs import expdb, get_pg_pool

    now = datetime.now(timezone.utc)

    # 1. Docstore: full document blob
    await expdb.extractions.insert_one({
        "_id": extraction_id,
        "code": code,
        "language": language,
        "filename": filename,
        "initialDag": initial_dag.to_json_dict(),
        "autoDag": auto_dag.to_json_dict(),
        "correctedDag": None,
        "uid": uid,
        "session": session,
        "rulesVersion": rules_version,
        "status": "auto",
        "createdAt": now,
        "correctedAt": None,
    })

    # 2. Postgres: relational index
    code_hash = hashlib.sha256(code.encode()).hexdigest()[:16]
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO extractions
                (id, code_hash, auto_dag_id, rules_version, session, uid)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (id) DO NOTHING
            """,
            extraction_id,
            code_hash,
            extraction_id,   # auto_dag_id points to the same docstore doc
            rules_version,
            session,
            uid,
        )


async def record_correction(
    extraction_id: str,
    corrected_dag_json: dict[str, Any],
) -> None:
    """Update an extraction with the user-corrected DAG.

    Called from the ``ExtractionCorrected`` event handler when a user submits
    a corrected version of an extracted pipeline.
    """
    from backend.envs import expdb, get_pg_pool

    now = datetime.now(timezone.utc)

    # Docstore: set correctedDag field
    await expdb.extractions.update_one(
        {"_id": extraction_id},
        {"$set": {
            "correctedDag": corrected_dag_json,
            "status": "corrected",
            "correctedAt": now,
        }},
    )

    # Postgres: update status + corrected pointer
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE extractions
            SET corrected_dag_id = $1,
                status           = 'corrected',
                corrected_at     = NOW()
            WHERE id = $1
            """,
            extraction_id,
        )


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

async def get_extraction(extraction_id: str) -> dict | None:
    """Load a single extraction document from the docstore."""
    from backend.envs import expdb
    return await expdb.extractions.find_one({"_id": extraction_id})


async def get_regression_set() -> list[dict]:
    """Load all extraction documents for regression testing.

    Returns the full set, ordered by creation time (oldest first), so that
    regression tests replay in the same order the extractions were originally
    performed.
    """
    from backend.envs import expdb
    cursor = expdb.extractions.find({}).sort("createdAt", 1)
    return await cursor.to_list(length=None)
