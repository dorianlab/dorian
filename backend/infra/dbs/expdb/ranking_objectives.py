from datetime import datetime, timezone
from typing import Any, Dict, List

from backend.envs import expdb

def _utcnow():
    return datetime.now(timezone.utc)

def _normalize_objective(obj: Dict[str, Any]) -> Dict[str, Any]:
    name = str(obj.get("name", "")).strip()
    if not name:
        raise ValueError("Objective 'name' is required")

    typ = str(obj.get("type", "")).strip()
    if typ not in ("operator", "snippet"):
        raise ValueError("Objective 'type' must be 'operator' or 'snippet'")

    language = str(obj.get("language", "")).strip() or "python"
    code = obj.get("code", "")
    if code is None:
        code = ""
    code = str(code)

    return {
        "name": name,
        "type": typ,
        "language": language,
        "code": code,
    }

async def _upsert_objectives(session: str, uid: str, objs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Upsert by (sessionId, userId, name) so reorder/resend doesn't create duplicates.
    Returns the same objects but with docId attached.
    """
    col = expdb.ranking_objectives
    now = _utcnow()
    out: List[Dict[str, Any]] = []


    for raw in objs:
        norm = _normalize_objective(raw)

        q = {"sessionId": session, "userId": uid, "name": norm["name"]}
        update = {
            "$set": {
                "type": norm["type"],
                "language": norm["language"],
                "code": norm.get("code", ""),
                "updatedAt": now,
            },
            "$setOnInsert": {
                "sessionId": session,
                "userId": uid,
                "createdAt": now,
            },
        }

        res = await col.update_one(q, update, upsert=True)

        # Determine _id
        if res.upserted_id:
            _id = res.upserted_id
        else:
            doc = await col.find_one(q, {"_id": 1})
            _id = doc["_id"] if doc else None

        merged = dict(raw)
        if _id:
            merged["docId"] = str(_id)

        out.append(merged)

    return out