//! Redis Streams consumer-group subscriber.
//!
//! Replaces ``backend/eventbus_subscriber.py``. Subscribes to the
//! ``events:user`` and ``events:bg`` streams via ``XREADGROUP``,
//! parses each entry's ``event`` field as an ``EventEnvelope``, and
//! dispatches through the ``Registry``. Each entry is ``XACK``-ed
//! after the handlers complete.
//!
//! Shutdown: a ``tokio::sync::watch`` channel signals "stop". The
//! loop polls it between blocking reads so SIGTERM gets noticed
//! within ~``block_ms`` (default 1 s).
//!
//! Backoff: a transient redis error pauses the loop for 1 s and
//! escalates to 5 s after 5 consecutive failures — same shape as
//! the python subscriber.

use anyhow::Result;
use redis::streams::{StreamReadOptions, StreamReadReply};
use redis::{AsyncCommands, RedisError};
use std::time::Duration;
use tokio::sync::watch;
use tokio::time::sleep;
use tracing::{debug, error, info, warn};

use crate::event::EventEnvelope;
use crate::registry::Registry;
use crate::state::AppState;

const STREAM_KEY: &str = "event";

/// Run the subscriber loop. Returns on a fatal redis error or when
/// the shutdown channel signals.
pub async fn run(
    state: AppState,
    registry: Registry,
    mut shutdown: watch::Receiver<bool>,
) -> Result<()> {
    info!(
        consumer = %state.config.consumer_name,
        group = %state.config.consumer_group,
        streams = ?[&state.config.stream_user, &state.config.stream_bg],
        "subscriber starting"
    );

    ensure_groups(&state).await?;

    let opts = StreamReadOptions::default()
        .group(&state.config.consumer_group, &state.config.consumer_name)
        .count(state.config.batch_count as usize)
        .block(state.config.block_ms as usize);

    let streams = [
        state.config.stream_user.clone(),
        state.config.stream_bg.clone(),
    ];
    let ids = [">", ">"]; // only entries the group hasn't delivered yet

    let mut consecutive_errors = 0u32;
    loop {
        if *shutdown.borrow() {
            info!("subscriber shutdown requested");
            break;
        }

        let mut conn = state.redis.clone();
        let result: Result<StreamReadReply, RedisError> = tokio::select! {
            biased;
            _ = shutdown.changed() => {
                info!("shutdown channel triggered during XREADGROUP");
                break;
            }
            r = conn.xread_options(&streams, &ids, &opts) => r,
        };

        match result {
            Ok(reply) => {
                consecutive_errors = 0;
                if reply.keys.is_empty() {
                    continue;
                }
                for stream in reply.keys {
                    let stream_name = stream.key.clone();
                    for entry in stream.ids {
                        process_entry(&state, &registry, &stream_name, &entry).await;
                    }
                }
            }
            Err(e) => {
                consecutive_errors = consecutive_errors.saturating_add(1);
                let backoff = if consecutive_errors > 5 { 5 } else { 1 };
                warn!(
                    err = %e,
                    consecutive = consecutive_errors,
                    "XREADGROUP failed, backing off {backoff}s"
                );
                sleep(Duration::from_secs(backoff)).await;
            }
        }
    }
    Ok(())
}

async fn process_entry(
    state: &AppState,
    registry: &Registry,
    stream: &str,
    entry: &redis::streams::StreamId,
) {
    let envelope = match parse_envelope(&entry.map) {
        Ok(e) => e,
        Err(reason) => {
            tracing::trace!(stream, id = %entry.id, reason, "skipping unrecognised entry");
            ack(state, stream, &entry.id).await;
            return;
        }
    };

    debug!(
        stream,
        id = %entry.id,
        event_type = %envelope.event_type,
        request_id = ?envelope.request_id,
        "dispatching",
    );
    registry.dispatch(state, &envelope).await;
    ack(state, stream, &entry.id).await;
}

/// Parse an event envelope from a stream entry's field map.
///
/// Two shapes are accepted:
///
///   1. **Gateway shape**: a single ``event`` field carrying the full
///      JSON envelope. This is what ``engine/gateway/src/eventbus.rs::emit``
///      and ``backend/events.py::aemit`` (when going via the gateway)
///      emit.
///   2. **Python eventbus shape**: separate ``type`` / ``ts`` /
///      ``payload`` (and optional ``source`` / ``request_id`` / ``uid``
///      / ``session``) fields. This is what
///      ``backend/eventbus_authoritative.py`` emits when bypassing the
///      gateway. Recognised so the Rust subscriber can co-consume the
///      same stream during the migration.
fn parse_envelope(
    map: &std::collections::HashMap<String, redis::Value>,
) -> Result<EventEnvelope, &'static str> {
    if let Some(v) = map.get(STREAM_KEY) {
        let raw = value_to_string(v).ok_or("non-utf8 event field")?;
        return EventEnvelope::from_stream_value(&raw).map_err(|_| "event JSON parse failed");
    }

    // Python-shape: build the envelope from individual fields.
    let event_type = map
        .get("type")
        .and_then(value_to_string)
        .ok_or("missing 'type' field")?;
    let payload = map
        .get("payload")
        .and_then(value_to_string)
        .map(|s| serde_json::from_str(&s).unwrap_or(serde_json::Value::String(s)))
        .unwrap_or(serde_json::Value::Null);
    let timestamp = map
        .get("ts")
        .and_then(value_to_string)
        .and_then(|s| s.parse::<f64>().ok());
    let source = map.get("source").and_then(value_to_string);
    let request_id = map.get("request_id").and_then(value_to_string);
    let uid = map.get("uid").and_then(value_to_string);
    let session = map.get("session").and_then(value_to_string);
    Ok(EventEnvelope {
        event_type,
        payload,
        source,
        timestamp,
        request_id,
        uid,
        session,
    })
}

fn value_to_string(v: &redis::Value) -> Option<String> {
    match v {
        redis::Value::BulkString(bytes) => std::str::from_utf8(bytes).ok().map(|s| s.to_string()),
        redis::Value::SimpleString(s) => Some(s.clone()),
        redis::Value::Int(i) => Some(i.to_string()),
        redis::Value::Double(f) => Some(f.to_string()),
        _ => None,
    }
}

async fn ack(state: &AppState, stream: &str, id: &str) {
    let mut conn = state.redis.clone();
    let res: redis::RedisResult<()> = conn
        .xack(stream, &state.config.consumer_group, &[id])
        .await;
    if let Err(e) = res {
        warn!(stream, id, "XACK failed: {e}");
    }
}

async fn ensure_groups(state: &AppState) -> Result<()> {
    for stream in [&state.config.stream_user, &state.config.stream_bg] {
        let mut conn = state.redis.clone();
        let res: redis::RedisResult<()> = redis::cmd("XGROUP")
            .arg("CREATE")
            .arg(stream)
            .arg(&state.config.consumer_group)
            .arg("0")
            .arg("MKSTREAM")
            .query_async(&mut conn)
            .await;
        match res {
            Ok(_) => {
                info!(%stream, group = %state.config.consumer_group, "consumer group created");
            }
            Err(e) => {
                let msg = e.to_string();
                if msg.contains("BUSYGROUP") {
                    debug!(%stream, "consumer group already exists");
                } else {
                    error!(%stream, "XGROUP CREATE failed: {msg}");
                    return Err(anyhow::anyhow!(
                        "XGROUP CREATE on {stream} failed: {msg}"
                    ));
                }
            }
        }
    }
    Ok(())
}
