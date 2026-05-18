//! Generic interaction-log sink. Replaces
//! ``dorian/event/handlers/lifecycle.persist_interaction_event``
//! and the corresponding ``subscribe(_canvas_event,
//! persist_interaction_event)`` block in ``registry.py``.
//!
//! Each subscribed event type appends one JSON entry to the
//! per-session interaction log:
//!
//!     interactions:{uid}:{session}   (Redis LIST)
//!
//! Retention: ``LTRIM`` to the most recent ``CAP`` entries plus a
//! ``EXPIRE`` refresh to ``TTL_S`` seconds — same bounds as the
//! python handler so existing logs stay valid across the cutover.
//!
//! Wire format mirrors ``{"event": <type>, **payload}`` so existing
//! consumers (replay tooling, analytics) read it unchanged.

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::{Map, Value};

use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

/// Bounded retention — keep in sync with
/// ``dorian/event/handlers/lifecycle.py::_INTERACTIONS_CAP``.
const CAP: isize = 50_000;
/// Per-key TTL — refreshed on every push so an active session's log
/// stays alive while a stale one drops off.
const TTL_S: i64 = 7 * 24 * 3600;

/// Event types persisted to the interaction log. Order doesn't
/// matter; the registry loops over the slice.
const PERSISTED_EVENTS: &[&str] = &[
    // Canvas interactions
    "PipelineNodeAdded",
    "PipelineNodeRemoved",
    "PipelineNodeConfigured",
    "PipelineEdgeAdded",
    "PipelineEdgeRemoved",
    "PipelineComposed",
    "PipelineExportClicked",
    "PipelineShareClicked",
    // Extraction correction (also logged for analytics)
    "ExtractionCorrected",
    // Vault lifecycle (encrypted env vars)
    "VaultEnvVarStored",
    "VaultEnvVarDeleted",
    // Dataset lifecycle
    "DatasetUploaded",
    "DatasetImported",
    "DatasetRemoved",
];

pub fn register(r: &mut Registry) {
    for ev in PERSISTED_EVENTS {
        let event_type = *ev;
        r.register(event_type, move |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(persist(state, event))
        });
    }
}

async fn persist(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let uid = event
        .uid
        .clone()
        .or_else(|| {
            event
                .payload
                .get("uid")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty());
    let session = event
        .session
        .clone()
        .or_else(|| {
            event
                .payload
                .get("session")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };

    // Wire format: {"event": <type>, **payload} — same shape the
    // python handler emits so existing replay code reads unchanged.
    let mut entry: Map<String, Value> = match event.payload.clone() {
        Value::Object(m) => m,
        _ => Map::new(),
    };
    entry.insert("event".to_string(), Value::String(event.event_type.clone()));
    let serialised = serde_json::to_string(&Value::Object(entry))?;

    let key = format!("interactions:{uid}:{session}");
    let mut conn = state.redis.clone();
    let _: i64 = conn.rpush(&key, &serialised).await?;
    // LTRIM keeps only the most recent CAP entries; amortised O(1).
    let _: () = conn.ltrim(&key, -CAP, -1).await?;
    // Refresh TTL on every push.
    let _: bool = conn.expire(&key, TTL_S).await?;
    Ok(())
}
