import json
import math
import pendulum
from typing import Any, Dict
from redis.asyncio import Redis


def sanitize_floats(obj: Any) -> Any:
    """Recursively replace nan/inf float values with None for JSON compliance."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_floats(v) for v in obj]
    return obj

async def update_session_meta(redis: Redis, session_id: str, updates: Dict[str, Any]):
    raw = await redis.get(f"session:{session_id}:meta")
    if not raw:
        return None

    session = json.loads(raw)
    session.update(updates)
    session["updated_at"] = str(pendulum.now())

    await redis.set(f"session:{session_id}:meta", json.dumps(session))
    return session
