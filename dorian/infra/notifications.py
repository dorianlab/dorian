"""Batch notification accumulator for offline / be-right-back users.

When a user's WebSocket connection is down, important notifications are
stored in a Redis LIST.  On reconnect, the full batch is flushed to the
user's stream so they see everything that happened while they were away.

Usage::

    from dorian.infra.notifications import push_notification, flush_pending

    # Backend handler emits a notification (works whether user is online or not)
    await push_notification(uid, session, {
        "kind": "info",
        "title": "Pipeline completed",
        "message": "Your pipeline finished in 12.3s",
    })

    # On reconnect (seed_session), flush accumulated notifications
    await flush_pending(uid, session)
"""

from __future__ import annotations

import json
import time

from dorian.infra.keys import RedisKeys, STREAM_MAXLEN


async def push_notification(
    uid: str,
    session: str,
    notification: dict,
    *,
    always_stream: bool = True,
) -> None:
    """Send a notification to the user, with offline fallback.

    If ``always_stream`` is True (default), the notification is both pushed
    to the WS stream (for immediate delivery) AND stored in the pending list
    (for reconnect replay).  The frontend dedup logic prevents double display.

    If ``always_stream`` is False, the notification is only stored in the
    pending list — useful for low-priority background events that should
    only appear when the user returns.
    """
    from backend.envs import aioredis

    # Ensure required fields
    notification.setdefault("id", f"n-{int(time.time()*1000)}-{uid[:8]}")
    notification.setdefault("createdAt", str(int(time.time() * 1000)))
    notification.setdefault("kind", "info")

    # Always store in pending list for reconnect replay
    pending_key = RedisKeys.pending_notifications(uid, session)
    await aioredis.rpush(pending_key, json.dumps(notification))
    await aioredis.expire(pending_key, 86400)  # 24h TTL

    if always_stream:
        # Also push to the WS stream for immediate delivery
        stream_key = RedisKeys.stream(uid, session)
        payload = json.dumps({
            "type": "notification",
            "value": json.dumps(notification),
            # Redundant top-level fields for the generic WS handler
            **notification,
        })
        await aioredis.xadd(
            stream_key,
            {"data": payload},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )


async def flush_pending(uid: str, session: str) -> int:
    """Flush all pending notifications to the user's WS stream.

    Called during session reconnect (``seed_session``).  Returns the number
    of notifications flushed.

    The pending list is deleted after flushing — notifications that were
    already delivered via the stream will be deduped by the frontend.
    """
    from backend.envs import aioredis

    pending_key = RedisKeys.pending_notifications(uid, session)
    items = await aioredis.lrange(pending_key, 0, -1)

    if not items:
        return 0

    stream_key = RedisKeys.stream(uid, session)

    # Send a batch notification event with all accumulated items
    batch = []
    for raw in items:
        try:
            batch.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue

    if batch:
        batch_payload = json.dumps({
            "type": "notifications/batch",
            "value": json.dumps(batch),
        })
        await aioredis.xadd(
            stream_key,
            {"data": batch_payload},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )

    # Clear the pending list
    await aioredis.delete(pending_key)
    return len(batch)


async def get_pending_count(uid: str, session: str) -> int:
    """Return the number of pending notifications without consuming them."""
    from backend.envs import aioredis

    pending_key = RedisKeys.pending_notifications(uid, session)
    return await aioredis.llen(pending_key)
