//! Session-lifecycle event handlers — currently just the
//! ``WebsocketDisconnected`` cleanup. Replaces
//! ``dorian.event.handlers.session.handle_websocket_disconnected``.
//!
//! Cleanup deletes only the transient session-scoped keys
//! (outbound stream, cursor, AI Debugger scope SET) — durable
//! state (``session:meta``, interactions log, feedback log)
//! is preserved so reconnection picks up where the user left off.

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::json;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const SOURCE: &str = "rust-backend.handlers.session.handle_websocket_disconnected";

pub fn register(r: &mut Registry) {
    r.register(
        "WebsocketDisconnected",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_disconnected(state, event))
        },
    );
}

fn payload_str(event: &EventEnvelope, key: &str) -> Option<String> {
    event
        .payload
        .get(key)
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty())
}

async fn handle_disconnected(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let uid = event
        .uid
        .clone()
        .or_else(|| payload_str(event, "uid"))
        .filter(|s| !s.is_empty());
    let session = event
        .session
        .clone()
        .or_else(|| payload_str(event, "session"))
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };

    let mut conn = state.redis.clone();
    let stream_key = keys::ws_stream(&uid, &session);
    let cursor_key = keys::cursor(&uid, &session);
    let canvas_key = keys::canvas_operators(&session);

    // Mirror python: UNLINK the stream (may be large), DEL cursor +
    // canvas-operators set. UNLINK is non-blocking; DEL is fine for
    // small keys. If any step fails, emit SessionCleanupFailed; the
    // happy path emits SessionCleanedUp.
    let result: Result<(), redis::RedisError> = async {
        let _: i64 = conn.unlink(&stream_key).await?;
        let _: i64 = conn.del(&[&cursor_key, &canvas_key]).await?;
        Ok(())
    }
    .await;

    match result {
        Ok(()) => {
            let payload = EmitPayload::new(
                "SessionCleanedUp",
                SOURCE,
                json!({
                    "source":  SOURCE,
                    "uid":     uid,
                    "session": session,
                }),
            )
            .with_envelope(event.request_id.clone(), Some(uid), Some(session));
            let _ = aemit(state, Lane::Bg, payload).await;
        }
        Err(exc) => {
            let payload = EmitPayload::new(
                "SessionCleanupFailed",
                SOURCE,
                json!({
                    "source":  SOURCE,
                    "uid":     uid,
                    "session": session,
                    "error":   format!("{exc}"),
                }),
            )
            .with_envelope(event.request_id.clone(), Some(uid), Some(session));
            let _ = aemit(state, Lane::Bg, payload).await;
        }
    }
    Ok(())
}
