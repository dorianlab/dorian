//! ``RustBackendHeartbeat`` handler — the proof-of-life event the
//! rust backend emits and consumes. Doubles as the integration test
//! for the subscriber + registry: emit one of these, see it logged,
//! see an ``ack`` published back to the bus. New handler ports
//! follow the same template.
//!
//! When this lands, the python ``backend/eventbus_subscriber.py`` is
//! left untouched — both consumer groups read the same stream, so a
//! ``RustBackendHeartbeat`` event is delivered to one of them.
//! Production-impact-free: nothing in the python backend listens for
//! the heartbeat type.

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::json;
use tracing::info;

use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

pub fn register(r: &mut Registry) {
    r.register("RustBackendHeartbeat", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle(state, event))
    });
}

async fn handle(state: &AppState, event: &EventEnvelope) -> Result<()> {
    info!(
        request_id = ?event.request_id,
        source = ?event.source,
        payload = %event.payload,
        "RustBackendHeartbeat received"
    );

    // Emit the ack back so end-to-end tests can assert delivery.
    let ack_event = json!({
        "type": "RustBackendHeartbeatAck",
        "payload": {
            "ts": std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0),
            "consumer": state.config.consumer_name,
            "echo_request_id": event.request_id,
        },
        "source": "rust-backend.handlers.heartbeat",
        "timestamp": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0),
        "request_id": event.request_id,
    });

    let mut conn = state.redis.clone();
    let payload = serde_json::to_string(&ack_event)?;
    // Match the gateway's lane semantics: heartbeats are bg.
    let _: String = conn
        .xadd_maxlen(
            &state.config.stream_bg,
            StreamMaxlen::Approx(100_000),
            "*",
            &[("event", payload.as_str())],
        )
        .await?;
    Ok(())
}
