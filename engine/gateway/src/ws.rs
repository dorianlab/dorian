//! Native WebSocket endpoint. Replaces
//! ``dorian/api/websocket.py`` end-to-end — every browser WS now
//! terminates on the gateway, no python hop.
//!
//! Why this matters: the SPA reconnects on every navigation, every
//! tab switch, every reconnect attempt. The python WS endpoint sat
//! behind the python event-bus's local-dispatch worker pool, and
//! that pool has a recurring silent-stall pattern
//! (``project_python_eventbus_workers_degrade.md``) that left
//! sidebars empty. The native gateway handler doesn't depend on
//! the python pool at all — inbound msgpack frames go straight to
//! the redis ``events:user`` stream where the rust-backend
//! subscriber and (until they're all ported) the python subscriber
//! both consume; outbound goes straight from the per-session
//! redis stream.
//!
//! Wire format parity with the python endpoint:
//! - Inbound: msgpack-encoded objects matching one of:
//!     ``{event:"init", user:uid, session:sess}``
//!         → ``InitSession`` event
//!     ``{event:"feedback", user:uid, session:sess, answers:{...}}``
//!         → ``FeedbackReceived`` event
//!     ``{event: <PascalCase>, payload: {...}}``
//!         → emit the named event with the payload
//! - Outbound: each entry on the per-session redis stream is a
//!   map of ``(field, value)`` pairs; we relay the map as a
//!   msgpack object. ``type:"list"`` entries get ``value`` split
//!   on commas first to match the SPA's expectation (the python
//!   send loop did the same).
//!
//! HMAC: deliberately not enforced here. The browser can't sign a
//! WS upgrade because its ``Sec-WebSocket-*`` headers are
//! browser-controlled. Auth is via the signed query params /
//! NextAuth cookie that the SPA already attaches to the connect
//! URL — the original python handler did the same (it never
//! validated HMAC either).
//!
//! Rate limiting: connection-level only on this first cut. A
//! per-IP redis INCR with EX=60 caps to 20 new handshakes/min.
//! Per-event rate limiting (the python ``check_ws_event``) is
//! deferred — it required a config-driven map that's not
//! load-bearing for the user-facing path.

use std::collections::BTreeMap;
use std::time::Duration;

use axum::{
    extract::{
        ws::{Message as AxumMessage, WebSocket, WebSocketUpgrade},
        ConnectInfo, Query, State,
    },
    http::StatusCode,
    response::IntoResponse,
};
use futures_util::{sink::SinkExt, stream::StreamExt};
use redis::AsyncCommands;
use redis::streams::{StreamMaxlen, StreamReadOptions, StreamReadReply};
use rmpv::Value as MsgValue;
use serde::Deserialize;
use serde_json::{json, Value};
use std::net::SocketAddr;
use tokio::sync::mpsc;
use tracing::warn;

use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 100_000;
const ACTIVE_CONNECTIONS_KEY: &str = "dorian:active_connections";
const CONN_RATE_LIMIT_PER_MIN: i64 = 20;
const CONN_RATE_LIMIT_WINDOW_S: usize = 60;
/// 1 MiB cap on inbound frames — same as the python
/// ``MAX_MESSAGE_SIZE`` default. axum's ``WebSocketUpgrade::max_message_size``
/// would enforce this at framing time but the API there is awkward;
/// we check binary length before decoding instead.
const MAX_MESSAGE_BYTES: usize = 1_048_576;

#[derive(Debug, Deserialize)]
pub struct WsParams {
    pub uid: String,
    pub session: String,
}

/// Replace the old `bridge()` proxy. Path stays at ``/ws`` so the
/// SPA's ``NEXT_PUBLIC_WS_URL`` doesn't change.
pub async fn ws_proxy(
    State(state): State<AppState>,
    ws: WebSocketUpgrade,
    ConnectInfo(addr): ConnectInfo<SocketAddr>,
    Query(params): Query<WsParams>,
) -> impl IntoResponse {
    if params.uid.is_empty() || params.session.is_empty() {
        return (StatusCode::BAD_REQUEST, "uid and session query params required")
            .into_response();
    }

    // Per-IP connection rate-limit. INCR a 60s-window key; first
    // hit becomes 1 (set EX), subsequent hits within the window
    // increment. >20/min → reject the upgrade.
    let mut conn = state.inner.redis.clone();
    let ip_key = format!("ws:rl:conn:{}", addr.ip());
    let count: i64 = match conn.incr(&ip_key, 1).await {
        Ok(c) => c,
        Err(err) => {
            warn!(%err, "ws conn rate-limit incr failed; allowing");
            0
        }
    };
    if count == 1 {
        let _: redis::RedisResult<()> = conn.expire(&ip_key, CONN_RATE_LIMIT_WINDOW_S as i64).await;
    }
    if count > CONN_RATE_LIMIT_PER_MIN {
        return (StatusCode::TOO_MANY_REQUESTS, "Too many connections from this IP")
            .into_response();
    }

    let cfg = state.inner.config.clone();
    let uid = params.uid;
    let session = params.session;

    ws.on_upgrade(move |socket| async move {
        if let Err(err) = serve(socket, state, uid, session, cfg).await {
            warn!(%err, "ws session ended with error");
        }
    })
}

async fn serve(
    socket: WebSocket,
    state: AppState,
    uid: String,
    session: String,
    cfg: crate::config::GatewayConfig,
) -> anyhow::Result<()> {
    let mut conn = state.inner.redis.clone();
    let conn_id = format!("{uid}:{session}");
    let _: redis::RedisResult<()> = conn.sadd(ACTIVE_CONNECTIONS_KEY, &conn_id).await;

    // Emit InitSession via the event bus so the rust-backend's
    // session_seed handler (and any other InitSession subscriber)
    // sees it. This mirrors the python WS endpoint's
    // ``await aemit(Event("InitSession", ...))`` after-accept call.
    emit_event(
        &mut conn,
        &cfg.stream_user,
        cfg.stream_maxlen,
        "InitSession",
        json!({"uid": uid, "session": session}),
        Some(&uid),
        Some(&session),
    )
    .await;

    let (mut tx, mut rx) = socket.split();

    // Outbound pump (server → browser): XREAD per-session stream,
    // msgpack-encode each entry, send as binary frame. The python
    // send loop kept the cursor in ``{uid}:{session}:last`` redis
    // key with a 24h EX; mirror that so reconnects pick up where
    // the previous session left off.
    let stream_key = format!("{uid}:{session}:stream");
    let cursor_key = format!("{uid}:{session}:last");
    let last_id: String = conn.get(&cursor_key).await.unwrap_or_else(|_| "0".to_string());
    let (out_tx, mut out_rx) = mpsc::channel::<Vec<u8>>(64);

    let outbound_redis = state.inner.redis.clone();
    let outbound_session = session.clone();
    let outbound_uid = uid.clone();
    let outbound = tokio::spawn(async move {
        let mut conn = outbound_redis;
        let mut cursor = if last_id.is_empty() { "0".to_string() } else { last_id };
        let opts = StreamReadOptions::default().block(50).count(20);
        loop {
            let res: redis::RedisResult<StreamReadReply> = conn
                .xread_options(&[stream_key.as_str()], &[cursor.as_str()], &opts)
                .await;
            match res {
                Ok(reply) => {
                    for k in reply.keys {
                        for entry in k.ids {
                            // Build a msgpack-able map from the entry's
                            // ``(field, value)`` pairs. Redis returns
                            // them as ``HashMap<String, redis::Value>``
                            // — coerce values to strings (the SPA shape).
                            let mut map: BTreeMap<String, String> = BTreeMap::new();
                            for (k, v) in entry.map.iter() {
                                let s = match v {
                                    redis::Value::BulkString(b) => {
                                        String::from_utf8_lossy(b).to_string()
                                    }
                                    redis::Value::SimpleString(s) => s.clone(),
                                    redis::Value::Int(i) => i.to_string(),
                                    redis::Value::Double(f) => f.to_string(),
                                    other => format!("{other:?}"),
                                };
                                map.insert(k.clone(), s);
                            }
                            cursor = entry.id.clone();
                            let frame = redis_entry_to_msgpack(&map);
                            if out_tx.send(frame).await.is_err() {
                                return;
                            }
                            // Persist the cursor so reconnects don't
                            // re-flood the SPA with already-seen
                            // entries.
                            let _: redis::RedisResult<()> =
                                conn.set_ex(&cursor_key, &cursor, 86_400).await;
                        }
                    }
                }
                Err(err) => {
                    warn!(uid=%outbound_uid, session=%outbound_session, %err, "ws xread failed");
                    tokio::time::sleep(Duration::from_millis(200)).await;
                }
            }
        }
    });

    // Browser → events:user pump. Read msgpack frames, translate
    // to Event envelopes, XADD to the appropriate stream.
    let inbound_redis = state.inner.redis.clone();
    let inbound_uid = uid.clone();
    let inbound_session = session.clone();
    let stream_user = cfg.stream_user.clone();
    let stream_bg = cfg.stream_bg.clone();
    let stream_maxlen = cfg.stream_maxlen;
    let inbound = tokio::spawn(async move {
        let mut conn = inbound_redis;
        while let Some(msg) = rx.next().await {
            let m = match msg {
                Ok(m) => m,
                Err(_) => break,
            };
            let bytes = match m {
                AxumMessage::Binary(b) => b,
                AxumMessage::Close(_) => break,
                AxumMessage::Text(_) => continue,
                AxumMessage::Ping(_) | AxumMessage::Pong(_) => continue,
            };
            if bytes.len() > MAX_MESSAGE_BYTES {
                continue;
            }
            let json = match msgpack_to_json(&bytes) {
                Some(j) => j,
                None => continue,
            };
            let Some(obj) = json.as_object() else { continue };

            // Pattern-match the python contract.
            let event_field = obj.get("event").and_then(|v| v.as_str());
            match event_field {
                Some("init") => {
                    let user = obj
                        .get("user")
                        .and_then(|v| v.as_str())
                        .unwrap_or(&inbound_uid)
                        .to_string();
                    let sess = obj
                        .get("session")
                        .and_then(|v| v.as_str())
                        .unwrap_or(&inbound_session)
                        .to_string();
                    let init_payload = json!({"uid": user, "session": sess});
                    emit_event(
                        &mut conn,
                        &stream_user,
                        stream_maxlen,
                        "InitSession",
                        init_payload.clone(),
                        Some(&user),
                        Some(&sess),
                    )
                    .await;
                    publish_service_event(
                        &mut conn, "InitSession", &init_payload, &user, &sess, None,
                    )
                    .await;
                }
                Some("feedback") => {
                    let user = obj
                        .get("user")
                        .and_then(|v| v.as_str())
                        .unwrap_or(&inbound_uid)
                        .to_string();
                    let sess = obj
                        .get("session")
                        .and_then(|v| v.as_str())
                        .unwrap_or(&inbound_session)
                        .to_string();
                    let answers = obj.get("answers").cloned().unwrap_or(json!({}));
                    let fb_payload = json!({"uid": user, "session": sess, "answers": answers});
                    emit_event(
                        &mut conn,
                        &stream_user,
                        stream_maxlen,
                        "FeedbackReceived",
                        fb_payload.clone(),
                        Some(&user),
                        Some(&sess),
                    )
                    .await;
                    publish_service_event(
                        &mut conn, "FeedbackReceived", &fb_payload, &user, &sess, None,
                    )
                    .await;
                }
                Some(name) => {
                    let payload = obj.get("payload").cloned();
                    if let Some(payload) = payload {
                        let event_uid = payload
                            .get("uid")
                            .and_then(|v| v.as_str())
                            .unwrap_or(&inbound_uid)
                            .to_string();
                        let event_session = payload
                            .get("session")
                            .and_then(|v| v.as_str())
                            .unwrap_or(&inbound_session)
                            .to_string();
                        let request_id = payload
                            .get("requestId")
                            .and_then(|v| v.as_str())
                            .map(|s| s.to_string());
                        // Send to bg stream for Go/Rust subscribers.
                        emit_event(
                            &mut conn,
                            &stream_bg,
                            stream_maxlen,
                            name,
                            payload.clone(),
                            Some(&event_uid),
                            Some(&event_session),
                        )
                        .await;
                        // Also publish to the service bridge channel so
                        // Python's ``service_bridge`` receives ALL
                        // browser-originated events and dispatches them
                        // via ``aemit`` into the local handler registry.
                        // Without this, events like ``ExtractPipeline``,
                        // ``ExecutePipeline``, ``PipelineSaved``, etc. are
                        // dropped by the Python subscriber (they are not in
                        // ``eventbus:authoritative``).
                        publish_service_event(
                            &mut conn,
                            name,
                            &payload,
                            &event_uid,
                            &event_session,
                            request_id.as_deref(),
                        )
                        .await;
                    } else {
                        emit_event(
                            &mut conn,
                            &stream_user,
                            stream_maxlen,
                            "MalformedEvent",
                            json!({"raw": format!("{:?}", obj)}),
                            Some(&inbound_uid),
                            Some(&inbound_session),
                        )
                        .await;
                    }
                }
                None => {
                    emit_event(
                        &mut conn,
                        &stream_user,
                        stream_maxlen,
                        "MalformedEvent",
                        json!({"raw": format!("{:?}", obj)}),
                        Some(&inbound_uid),
                        Some(&inbound_session),
                    )
                    .await;
                }
            }
        }
        // Inbound disconnect — emit WebsocketDisconnected so the
        // python observability path (and any rust subscriber) sees
        // the lifecycle event. ``aemit`` in the python endpoint did
        // the same.
        emit_event(
            &mut conn,
            &stream_user,
            stream_maxlen,
            "WebsocketDisconnected",
            json!({
                "source": "ws.serve",
                "uid": inbound_uid,
                "session": inbound_session,
            }),
            Some(&inbound_uid),
            Some(&inbound_session),
        )
        .await;
    });

    // Pump out_rx → tx (browser).
    let writer = tokio::spawn(async move {
        while let Some(frame) = out_rx.recv().await {
            if tx.send(AxumMessage::Binary(frame.into())).await.is_err() {
                break;
            }
        }
    });

    // Wait for any task to finish (close happens cooperatively).
    tokio::select! {
        _ = inbound => {}
        _ = writer => {}
    }
    outbound.abort();

    // Cleanup tracking SET.
    let _: redis::RedisResult<()> = state
        .inner
        .redis
        .clone()
        .srem(ACTIVE_CONNECTIONS_KEY, &conn_id)
        .await;
    Ok(())
}

/// Build a ``EventBody``-shaped JSON envelope and XADD it to the
/// chosen stream. Same shape ``backend.events.aemit`` produces.
async fn emit_event(
    conn: &mut redis::aio::ConnectionManager,
    stream: &str,
    maxlen: u64,
    event_type: &str,
    payload: Value,
    uid: Option<&str>,
    session: Option<&str>,
) {
    let mut envelope = serde_json::Map::new();
    envelope.insert("type".into(), Value::String(event_type.to_string()));
    envelope.insert("payload".into(), payload);
    envelope.insert(
        "ts".into(),
        json!(chrono::Utc::now().timestamp_millis() as f64 / 1000.0),
    );
    if let Some(u) = uid {
        envelope.insert("uid".into(), Value::String(u.to_string()));
    }
    if let Some(s) = session {
        envelope.insert("session".into(), Value::String(s.to_string()));
    }
    let json_str = match serde_json::to_string(&Value::Object(envelope)) {
        Ok(s) => s,
        Err(err) => {
            warn!(%err, "ws emit serialise failed");
            return;
        }
    };
    let res: redis::RedisResult<String> = conn
        .xadd_maxlen(
            stream,
            StreamMaxlen::Approx(maxlen as usize),
            "*",
            &[("event", json_str.as_str())],
        )
        .await;
    if let Err(err) = res {
        warn!(%err, stream, "ws emit xadd failed");
    }
}

/// Publish an event envelope to the Redis pub/sub channel that the
/// Python ``service_bridge`` listens on (``events:service:{name}``).
///
/// The envelope shape matches what the old Go gateway published and
/// what ``dorian/event/service_bridge.py`` expects:
///
/// ```json
/// {"name":"ExtractPipeline","uid":"…","session":"…","payload":{…},
///  "request_id":"…","ts":1700000000.0,"source":"service"}
/// ```
///
/// Without this, browser-originated WS events (``ExtractPipeline``,
/// ``ExecutePipeline``, ``PipelineSaved``, etc.) only land in the
/// Redis stream where the Python subscriber drops them (they are not
/// in ``eventbus:authoritative``).  Publishing here is the missing
/// half of the Go-gateway contract.
async fn publish_service_event(
    conn: &mut redis::aio::ConnectionManager,
    name: &str,
    payload: &Value,
    uid: &str,
    session: &str,
    request_id: Option<&str>,
) {
    let mut envelope = serde_json::Map::new();
    envelope.insert("name".into(), Value::String(name.to_string()));
    envelope.insert("uid".into(), Value::String(uid.to_string()));
    envelope.insert("session".into(), Value::String(session.to_string()));
    envelope.insert("payload".into(), payload.clone());
    envelope.insert("source".into(), Value::String("service".to_string()));
    envelope.insert(
        "ts".into(),
        json!(chrono::Utc::now().timestamp_millis() as f64 / 1000.0),
    );
    if let Some(rid) = request_id {
        envelope.insert("request_id".into(), Value::String(rid.to_string()));
    }
    let json_str = match serde_json::to_string(&Value::Object(envelope)) {
        Ok(s) => s,
        Err(err) => {
            warn!(%err, "ws publish_service_event serialise failed");
            return;
        }
    };
    let channel = format!("events:service:{name}");
    let res: redis::RedisResult<i64> = conn.publish(&channel, json_str.as_str()).await;
    if let Err(err) = res {
        warn!(%err, channel, "ws publish_service_event failed");
    }
}

/// Build the msgpack frame the SPA expects from a redis stream
/// entry. Mirrors the python ``msgpack.packb(message)`` step:
/// the entry is a map of string→string fields. When ``type``
/// equals ``"list"``, ``value`` is split on commas and re-serialised
/// as an array — the python send loop did this in-place.
fn redis_entry_to_msgpack(entry: &BTreeMap<String, String>) -> Vec<u8> {
    let mut pairs: Vec<(MsgValue, MsgValue)> = Vec::with_capacity(entry.len());
    let is_list = entry.get("type").map(|s| s == "list").unwrap_or(false);
    for (k, v) in entry {
        let key = MsgValue::String(k.clone().into());
        let val = if is_list && k == "value" {
            // ``"".split(",")`` returns ``[""]`` in python and the
            // SPA's WS handler ``filter()``s those out. Match by
            // dropping empty trailing fragments.
            let arr: Vec<MsgValue> = v
                .split(',')
                .filter(|s| !s.is_empty())
                .map(|s| MsgValue::String(s.to_string().into()))
                .collect();
            MsgValue::Array(arr)
        } else {
            MsgValue::String(v.clone().into())
        };
        pairs.push((key, val));
    }
    let mut buf = Vec::new();
    if let Err(err) = rmpv::encode::write_value(&mut buf, &MsgValue::Map(pairs)) {
        warn!(%err, "msgpack encode failed");
    }
    buf
}

/// Decode the SPA's incoming msgpack frame into ``serde_json::Value``
/// so we can pattern-match payloads with the same shape the python
/// endpoint did. Returns ``None`` when the frame isn't a valid map.
fn msgpack_to_json(bytes: &[u8]) -> Option<Value> {
    let mut cursor = std::io::Cursor::new(bytes);
    let val = rmpv::decode::read_value(&mut cursor).ok()?;
    msgvalue_to_json(val)
}

fn msgvalue_to_json(v: MsgValue) -> Option<Value> {
    Some(match v {
        MsgValue::Nil => Value::Null,
        MsgValue::Boolean(b) => Value::Bool(b),
        MsgValue::Integer(i) => {
            if let Some(n) = i.as_i64() {
                json!(n)
            } else if let Some(n) = i.as_u64() {
                json!(n)
            } else if let Some(n) = i.as_f64() {
                json!(n)
            } else {
                return None;
            }
        }
        MsgValue::F32(f) => json!(f as f64),
        MsgValue::F64(f) => json!(f),
        MsgValue::String(s) => Value::String(s.into_str().unwrap_or_default()),
        MsgValue::Binary(b) => Value::String(String::from_utf8_lossy(&b).into_owned()),
        MsgValue::Array(arr) => Value::Array(
            arr.into_iter()
                .filter_map(msgvalue_to_json)
                .collect(),
        ),
        MsgValue::Map(pairs) => {
            let mut obj = serde_json::Map::with_capacity(pairs.len());
            for (k, v) in pairs {
                let key = match k {
                    MsgValue::String(s) => s.into_str().unwrap_or_default(),
                    other => format!("{other}"),
                };
                if let Some(val) = msgvalue_to_json(v) {
                    obj.insert(key, val);
                }
            }
            Value::Object(obj)
        }
        MsgValue::Ext(_, _) => return None,
    })
}
