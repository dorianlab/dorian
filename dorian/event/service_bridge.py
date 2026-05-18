"""Service event bridge — receive WS-originated events from the Go gateway.

The Go WebSocket proxy (gateway/internal/ws/proxy.go) decodes inbound msgpack
frames from the frontend and republishes each one as a JSON envelope on the
Redis pub/sub channel ``events:service:{name}``. The envelope shape matches
``gateway/internal/events/bridge.go::Envelope``::

    {
      "name": "InitSession",
      "uid":  "...",
      "session": "...",
      "payload": { ... },
      "request_id": "...",
      "ts": 1700000000.0,
      "source": "service"
    }

This module subscribes to that pattern, decodes each envelope, and re-emits
it into the local Python event bus via ``aemit`` so the existing handlers
(``InitSession`` → ``seed_session``, ``FeedbackReceived`` → ``handle_feedback``,
canvas events → ``persist_interaction_event``, ...) keep working unchanged.

Without this bridge the Go gateway publishes inbound events to a namespace
nothing in the Python backend listens on, and ``seed_session`` never fires —
the symptom observed in the Phase 3.3 performance recording session where
the tooltip tour never started despite the WebSocket staying connected.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from redis.asyncio import Redis as RedisAsync

from backend.events import Event, aemit

logger = logging.getLogger(__name__)

_PATTERN = "events:service:*"


async def _run(client: RedisAsync) -> None:
    pubsub = client.pubsub()
    await pubsub.psubscribe(_PATTERN)
    logger.info("service event bridge subscribed pattern=%s", _PATTERN)
    try:
        async for msg in pubsub.listen():
            if not msg or msg.get("type") != "pmessage":
                continue
            await _handle(msg.get("data"))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("service event bridge crashed")
        raise
    finally:
        try:
            await pubsub.punsubscribe(_PATTERN)
            await pubsub.close()
        except Exception:
            pass


async def _handle(raw: Any) -> None:
    if raw is None:
        return
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning("service bridge: bad JSON envelope: %s", e)
        return

    name = envelope.get("name")
    if not name:
        logger.warning("service bridge: envelope missing 'name' field")
        return

    payload = envelope.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {"value": payload}

    data: dict[str, Any] = dict(payload)
    uid = envelope.get("uid")
    session = envelope.get("session")
    if uid is not None:
        data.setdefault("uid", uid)
        data.setdefault("user", uid)
    if session is not None:
        data.setdefault("session", session)
    request_id = envelope.get("request_id")
    if request_id:
        data.setdefault("requestId", request_id)

    try:
        await aemit(Event(name, data=data))
    except Exception:
        logger.exception("service bridge: handler failed for event %s", name)


async def start_service_bridge(client: RedisAsync) -> asyncio.Task:
    """Start the bridge as a background task. Cancel the task to stop."""
    return asyncio.create_task(_run(client), name="service-event-bridge")
