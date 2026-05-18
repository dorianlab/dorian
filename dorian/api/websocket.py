"""
dorian/api/websocket.py
-----------------------
WebSocket endpoint: msgpack-encoded bidirectional channel between the
React frontend and the Python event bus.

Inbound:  msgpack → pattern-match → Event → aemit
Outbound: Redis Stream → msgpack → WebSocket binary frame
"""

from fastapi import Query, WebSocket, WebSocketDisconnect
import asyncio
import logging
import msgpack
import traceback
from backend.events import Event, aemit, aemit_bg, verbose
from backend.config import config
from backend.envs import aioredis
from backend.ws_rate_limit import check_ws_event, check_ws_connection, ttl_for
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits (read from config with sensible defaults)
# ---------------------------------------------------------------------------
_ws_cfg = getattr(config, "websocket", None)
MAX_MESSAGE_SIZE = int(getattr(_ws_cfg, "max_message_size", 1_048_576))
_STREAM_BLOCK_MS = int(getattr(_ws_cfg, "stream_block_ms", 50))
_STREAM_BATCH_SIZE = int(getattr(_ws_cfg, "stream_batch_size", 20))
_MSGPACK_MAX_STR_LEN = 262_144   # 256 KB max for any single string field
_MSGPACK_MAX_BIN_LEN = 1_048_576
_MSGPACK_MAX_ARRAY_LEN = 10_000
_MSGPACK_MAX_MAP_LEN = 1_000

shutdown = asyncio.Event()


async def websocket_endpoint(websocket: WebSocket, uid: str = Query(...), session: str = Query(...)):
    # ── Connection-level rate limit (20 new handshakes/min per IP) ──────────
    client_ip = (websocket.client.host if websocket.client else "unknown")
    if not await check_ws_connection(aioredis, client_ip):
        await websocket.accept()
        await websocket.close(code=1008, reason="Too many connections from this IP")
        return

    await websocket.accept()
    _log.info("[WS] accepted connection uid=%s session=%s ip=%s", uid, session, client_ip)
    # NOTE: InitSession is deferred until AFTER the send loop starts (below).
    # Previously it blocked here, which meant the send loop couldn't push
    # Phase 1 events to the client until seed_session fully completed (~2s).

    # Track this connection so KB invalidation can push updates to all
    # active sessions (see handle_kb_changed in handlers/session.py).
    conn_key = f"{uid}:{session}"
    await aioredis.sadd(RedisKeys.ACTIVE_CONNECTIONS, conn_key)

    async def receive_messages():
        try:
            while not shutdown.is_set():
                message = await websocket.receive_bytes()

                # --- size guard ---
                if len(message) > MAX_MESSAGE_SIZE:
                    await aemit(Event("WebsocketPayloadTooLarge", data={
                        "source": "websocket.receive_messages",
                        "uid": uid,
                        "session": session,
                        "size": len(message),
                        "limit": MAX_MESSAGE_SIZE,
                    }))
                    continue

                try:
                    payload = msgpack.unpackb(
                        message,
                        raw=False,
                        max_str_len=_MSGPACK_MAX_STR_LEN,
                        max_bin_len=_MSGPACK_MAX_BIN_LEN,
                        max_array_len=_MSGPACK_MAX_ARRAY_LEN,
                        max_map_len=_MSGPACK_MAX_MAP_LEN,
                    )
                except Exception as err:
                    await aemit(Event("WebsocketMalformedPayload", data={
                        "source": "websocket.receive_messages",
                        "error": repr(err),
                        "raw": message[:200].hex(),
                    }))
                    continue

                try:
                    if not isinstance(payload, dict) or "event" not in payload:
                        await aemit(Event("MalformedEvent", data={"raw": str(payload)[:200]}))
                        continue

                    match payload:
                        case {'event': 'init', 'user': uid_, 'session': sess_}:
                            await aemit(Event("InitSession", data={'uid': uid_, 'session': sess_}))
                        case {'event': 'feedback', 'user': uid_, 'session': sess_, 'answers': answers}:
                            await aemit(Event("FeedbackReceived", data={'uid': uid_, 'session': sess_, 'answers': answers}))
                        case {'event': str() as ev, 'payload': dict() as inner}:
                            # ── Per-event rate limit ─────────────────────────
                            event_uid = inner.get("uid") or uid
                            allowed, count, limit = await check_ws_event(
                                aioredis, event_uid, ev
                            )
                            if not allowed:
                                retry = await ttl_for(aioredis, event_uid, ev)
                                await aioredis.xadd(
                                    RedisKeys.stream(uid, session),
                                    {
                                        "event": "error/rate-limited",
                                        "eventName": ev,
                                        "limit": str(limit),
                                        "retryAfter": str(retry),
                                    },
                                    maxlen=STREAM_MAXLEN, approximate=True,
                                )
                                continue
                            # Diagnostic: trace AI-Debugger suggestion clicks
                            # end-to-end. The "Apply" button flows as a
                            # SuggestionInteraction; users have reported
                            # clicks that never reach apply_mitigation. Log
                            # arrival so we can tell the frontend/WS hop
                            # from the handler-pipeline apart.
                            if ev in ("SuggestionInteraction", "SuggestionAccepted"):
                                await aemit(Event("SuggestionClickReceived", data={
                                    "event": ev,
                                    "uid": inner.get("uid"),
                                    "session": inner.get("session"),
                                    "type": inner.get("type"),
                                    "sid": inner.get("suggestion_id"),
                                }))
                            await aemit_bg(Event(ev, data=inner))
                        case _:
                            await aemit(Event("MalformedEvent", data=payload))
                except Exception as err:
                    verbose(Event("EventBusError", data={
                        "source": "websocket.receive_messages",
                        "error": repr(err),
                    }))

        except WebSocketDisconnect:
            await aemit(Event("WebsocketDisconnected", data={
                "source": "websocket.receive_messages",
                "uid": uid,
                "session": session,
            }))
            return

        except Exception as exc:
            tb = traceback.format_exc()
            try:
                await aemit(Event("WebsocketOnReceiveError", data={
                    "source": "websocket.receive_messages",
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "trace": tb,
                }))
            except Exception as err:
                verbose(Event("EventBusError", data={
                    "source": "websocket.receive_messages",
                    "error": repr(err),
                }))


    async def send_messages():
        from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

        stream_key = RedisKeys.stream(uid, session)
        cursor_key = RedisKeys.cursor(uid, session)
        try:
            last = (await aioredis.get(cursor_key)) or 0
        except (RedisConnectionError, RedisTimeoutError):
            last = 0  # transient — start from stream head, reconcile on next set

        # Loop body wraps its own try/except so a transient Redis blip
        # (DNS hiccup, brief disconnect) doesn't kill the entire send task
        # and silently strand the client until reconnect.
        backoff = 0.1
        while not shutdown.is_set():
            try:
                messages = await aioredis.xread({stream_key: last}, block=_STREAM_BLOCK_MS, count=_STREAM_BATCH_SIZE)
                for _, message_list in messages:
                    for _id, message in message_list:
                        if isinstance(message, dict) and message.get('type') == 'list':
                            message['value'] = message.get('value', '').split(',')
                        await websocket.send_bytes(msgpack.packb(message, use_bin_type=True))
                        last = _id
                await aioredis.set(cursor_key, last, ex=86400)
                backoff = 0.1  # reset on successful tick
            except (RedisConnectionError, RedisTimeoutError) as exc:
                # Transient Redis problem — back off and retry without
                # tearing down the WS send loop.
                try:
                    await aemit(Event("WebsocketRedisTransientError", data={
                        "source": "websocket.send_messages",
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "backoff": backoff,
                    }))
                except Exception:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
            except Exception as exc:
                tb = traceback.format_exc()
                try:
                    await aemit(Event("WebsocketOnSendError", data={
                        "source": "websocket.send_messages",
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "trace": tb,
                    }))
                except Exception:
                    pass
                # Unknown failure — bail; the outer task supervisor will
                # close the WS and the client will reconnect.
                break

    receive = asyncio.create_task(receive_messages())
    send = asyncio.create_task(send_messages())

    # Emit InitSession AFTER the send loop is running so that Phase 1
    # events (state/pipeline, state/dataset) are pushed to the client
    # immediately instead of waiting for the full seed_session (~2s).
    #
    # We use aemit (not aemit_bg) because InitSession is critical — if
    # dropped, the session never seeds and the user sees no data.  The
    # aemit call blocks until handlers complete, but that's fine: the
    # send/receive tasks are already running as asyncio tasks, so the WS
    # is active.  We wrap it in create_task so it doesn't delay reaching
    # asyncio.wait below (which keeps the WS alive).
    _log.info("[WS] emitting InitSession uid=%s session=%s", uid, session)
    init_task = asyncio.create_task(
        aemit(Event("InitSession", data={'uid': uid, 'session': session}))
    )

    try:
        await asyncio.wait([receive, send], return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Untrack this connection.
        await aioredis.srem(RedisKeys.ACTIVE_CONNECTIONS, conn_key)
        # Cancel surviving tasks and wait for them to finish so we don't
        # leak coroutines or hold references to a closed WebSocket.
        init_task.cancel()
        receive.cancel()
        send.cancel()
        await asyncio.gather(init_task, receive, send, return_exceptions=True)
