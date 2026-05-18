# dorian/events/helpers.py
import asyncio
import json
from contextlib import asynccontextmanager

from backend.events import Event, aemit
from backend.envs import aioredis

SCRATCH_DEFAULT_NAMES = [
    "Good Performance On Similar Data",
    "Good General Performance",
]

PIPELINE_DEFAULT_NAMES = [
    # update when you know the real “incremental improvements” defaults
    "Previously Unseen",
    "Atomic Changes",
]

def _normalize_ranking_objectives(raw):
    """Ensure meta['rankingObjectives'] is a list of {uuid,name}."""
    if not raw or not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if isinstance(it, dict) and it.get("uuid") and it.get("name"):
            out.append({"uuid": str(it["uuid"]), "name": str(it["name"])})
    return out

def _select_defaults_by_name(all_objectives, names):
    name_set = set(names)
    return [{"uuid": o.uuid, "name": o.name} for o in all_objectives if o.name in name_set]

def _encode_selected(items):
    # [{uuid,name}] -> "uuid:name,uuid:name"
    return ",".join(f'{i["uuid"]}:{i["name"]}' for i in items if i.get("uuid") and i.get("name"))


async def _xadd(uid: str, session: str, message: dict):
    """Push a message to the user/session stream and await aemit a 'sent' event."""
    from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
    stream = RedisKeys.stream(uid, session)
    await aioredis.xadd(stream, message, maxlen=STREAM_MAXLEN, approximate=True)
    await aemit(Event("WebsocketEventEnqueued", data={"stream": stream, **message}))

async def _set_json(key: str, value):
    await aioredis.set(key, json.dumps(value))



def _normalize_operator(payload: dict) -> dict:
    """Coerce UI payload into a canonical operator shape."""
    return {
        "uuid": payload.get("uuid") or (payload.get("name", "").lower().replace(" ", "-")),
        "name": payload.get("name"),
        "type": payload.get("type"),         # "Parameter" | "Snippet" | "Operator"
        "dtype": payload.get("dtype"),       # present for Parameter/Operator
        "code": payload.get("code"),         # present for Snippet
        "inputs": payload.get("inputs") or [],
        "outputs": payload.get("outputs") or [],
        "meta": {k: v for k, v in payload.items() if k not in {
            "uuid", "name", "type", "dtype", "code", "inputs", "outputs",
            "uid", "user", "session", "event", "isNewNode"
        }},
    }



_ENVELOPE_KEYS = {"uid", "user", "session", "sessionId", "ts", "requestId", "request_id", "event"}


def _envelope(d: dict):
    # If payload exists and is a dict, use it as the primary source.
    # Otherwise treat the flat event data (minus envelope fields) as payload
    # so handlers always see the domain fields regardless of WS unwrap depth.
    if isinstance(d.get("payload"), dict):
        payload = d["payload"]
    else:
        payload = {k: v for k, v in d.items() if k not in _ENVELOPE_KEYS}

    # Helper to read from payload first, then root
    def read(key, alt_keys=None):
        alt_keys = alt_keys or []
        return (
            payload.get(key)
            or next((payload.get(k) for k in alt_keys if payload.get(k)), None)
            or d.get(key)
            or next((d.get(k) for k in alt_keys if d.get(k)), None)
        )

    uid = read("uid", ["user"])
    session = read("session", ["sessionId"])
    request_id = read("requestId", ["request_id"])
    ts = read("ts")

    return uid, session, payload, request_id, ts


def with_envelope(handler):
    """Decorator: unpack envelope fields from event.data and pass as kwargs.

    Allows handlers with signature ``(event, uid, session, payload, request_id, ts)``
    to be registered directly with ``subscribe()``, which only passes ``Event``.
    """
    async def wrapper(event):
        uid, session, payload, request_id, ts = _envelope(event.data)
        await handler(event, uid=uid, session=session, payload=payload, request_id=request_id, ts=ts)
    wrapper.__name__ = handler.__name__
    wrapper.__qualname__ = handler.__qualname__
    return wrapper


async def _get_session_meta(session: str) -> dict | None:
    raw = await aioredis.get(f"session:{session}:meta")
    return json.loads(raw) if raw else None


async def _save_session_meta(session: str, meta: dict):
    await aioredis.set(f"session:{session}:meta", json.dumps(meta))


# ---------------------------------------------------------------------------
# Atomic session meta update — prevents concurrent read-modify-write races.
# Uses a Redis lock (SET NX EX) so concurrent handlers on the same session
# serialize their updates instead of overwriting each other.
# ---------------------------------------------------------------------------

_META_LOCK_TTL = 5  # seconds — auto-expire if holder crashes
_META_LOCK_POLL = 0.05  # seconds between retries
_META_LOCK_RETRIES = 60  # 60 × 50ms = 3 seconds max wait


@asynccontextmanager
async def session_meta_tx(session: str):
    """Context manager that yields (meta: dict) under a per-session lock.

    On exit the modified ``meta`` is written back atomically.  If the
    session doesn't exist yet, yields an empty dict and creates it.

    Usage::

        async with session_meta_tx(session) as meta:
            meta["field"] = "value"
        # meta is saved automatically on exit
    """
    lock_key = f"session:{session}:meta:lock"
    acquired = False

    for _ in range(_META_LOCK_RETRIES):
        acquired = await aioredis.set(lock_key, "1", nx=True, ex=_META_LOCK_TTL)
        if acquired:
            break
        await asyncio.sleep(_META_LOCK_POLL)

    if not acquired:
        # Fallback: proceed without lock rather than dropping the update.
        pass

    try:
        meta = await _get_session_meta(session) or {}
        yield meta
        await _save_session_meta(session, meta)
    finally:
        if acquired:
            await aioredis.delete(lock_key)



def _upsert_by_key(
    items: list[dict],
    item: dict,
    key: str = "uuid",
) -> list[dict]:
    if not item:
        return items

    v = item.get(key)
    if not v:
        # If no key, just append
        return items + [item]

    out = []
    replaced = False

    for x in items:
        if x.get(key) == v:
            out.append(item)
            replaced = True
        else:
            out.append(x)

    if not replaced:
        out.append(item)

    return out



async def ensure_ranking_objectives(
    session: str,
    objectives,
    has_pipeline: bool,
    meta: dict | None,
):
    """
    Ensures meta['rankingObjectives'] exists.
    - If already present -> keep (only normalized).
    - If missing/empty -> choose defaults based on has_pipeline and persist.
    Returns (meta, ranking, selected_objectives, did_update_meta)
    """
    if not isinstance(meta, dict):
        meta = {}

    ranking = _normalize_ranking_objectives(meta.get("rankingObjectives"))
    did_update = False

    if not ranking:
        default_names = PIPELINE_DEFAULT_NAMES if has_pipeline else SCRATCH_DEFAULT_NAMES
        ranking = _select_defaults_by_name(objectives, default_names)

        meta["rankingObjectives"] = ranking
        meta["objectiveMode"] = "pipeline_default" if has_pipeline else "scratch_default"
        did_update = True

    if did_update:
        await _save_session_meta(session, meta)

    return meta, ranking, _encode_selected(ranking), did_update


async def set_pipeline_default_ranking(
    uid: str,
    session: str,
    objectives,
    mode_if_set: str = "pipeline_default",
    force: bool = False,
):
    """
    Sets pipeline default ranking objectives for the session and notifies UI.
    If force=False, it will NOT override when objectiveMode == 'custom'.
    """
    async with session_meta_tx(session) as meta:
        current_mode = meta.get("objectiveMode")
        if (not force) and current_mode == "custom":
            # user customized; don't override
            return meta, _normalize_ranking_objectives(meta.get("rankingObjectives"))

        ranking = _select_defaults_by_name(objectives, PIPELINE_DEFAULT_NAMES)
        meta["rankingObjectives"] = ranking
        meta["objectiveMode"] = mode_if_set

    # notify UI immediately
    await _xadd(uid, session, {
        "event": "state/objectives/selected",
        "value": _encode_selected(ranking),
        "type": "list"
    })

    return meta, ranking