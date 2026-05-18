"""document-store persistence (expdb) for custom evaluation procedures.

Mirrors the ranking_objectives pattern: upsert by (sessionId, userId, name)
so re-sends don't create duplicates.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.envs import expdb


def _utcnow():
    return datetime.now(timezone.utc)


def _normalize_procedure(obj: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a procedure document."""
    name = str(obj.get("name", "")).strip()
    if not name:
        raise ValueError("Evaluation procedure 'name' is required")

    uuid = str(obj.get("uuid", obj.get("id", ""))).strip()
    language = str(obj.get("language", "python")).strip() or "python"
    code = str(obj.get("code", ""))
    outputs = obj.get("outputs") or []

    return {
        "uuid": uuid,
        "name": name,
        "language": language,
        "code": code,
        "outputs": outputs,
    }


async def upsert_evaluation_procedure(
    session: str,
    uid: str,
    procedure: dict[str, Any],
) -> dict[str, Any]:
    """Upsert a single evaluation procedure by (sessionId, userId, uuid).

    Returns the procedure dict with ``docId`` attached.
    """
    col = expdb.evaluation_procedures
    now = _utcnow()
    norm = _normalize_procedure(procedure)

    query = {"sessionId": session, "userId": uid, "uuid": norm["uuid"]}
    update = {
        "$set": {
            "name": norm["name"],
            "language": norm["language"],
            "code": norm["code"],
            "outputs": norm["outputs"],
            "updatedAt": now,
        },
        "$setOnInsert": {
            "sessionId": session,
            "userId": uid,
            "uuid": norm["uuid"],
            "createdAt": now,
        },
    }

    res = await col.update_one(query, update, upsert=True)

    if res.upserted_id:
        _id = res.upserted_id
    else:
        doc = await col.find_one(query, {"_id": 1})
        _id = doc["_id"] if doc else None

    merged = dict(procedure)
    if _id:
        merged["docId"] = str(_id)
    return merged


async def get_evaluation_procedures(
    session: str,
    uid: str,
) -> list[dict[str, Any]]:
    """Fetch all evaluation procedures for a (session, uid) pair."""
    col = expdb.evaluation_procedures
    cursor = col.find(
        {"sessionId": session, "userId": uid},
        {"_id": 1, "uuid": 1, "name": 1, "language": 1, "code": 1, "outputs": 1},
    ).sort("createdAt", 1)

    results = []
    async for doc in cursor:
        doc["docId"] = str(doc.pop("_id"))
        results.append(doc)
    return results
