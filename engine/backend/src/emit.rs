//! Emit downstream events back to the bus. Mirrors python
//! ``backend.events.aemit`` semantics but skips the
//! sync-vs-async dance — there's no Dask worker pool here, every
//! handler is async, ``aemit`` is the only emit path. The legacy
//! ``emit()`` distinction is one of the python anti-patterns this
//! port retires.

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde::Serialize;
use serde_json::Value;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::state::AppState;

/// Lane selector — matches the gateway's ``lane_for`` semantics.
/// User-impacting events go to ``events:user`` for priority/back-
/// pressure-aware delivery; everything else (observability,
/// internal coordination) goes to ``events:bg``.
#[derive(Debug, Clone, Copy)]
pub enum Lane {
    User,
    Bg,
}

#[derive(Debug, Clone, Serialize)]
pub struct EmitPayload {
    #[serde(rename = "type")]
    pub event_type: String,
    pub payload: Value,
    pub source: String,
    pub timestamp: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub uid: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session: Option<String>,
}

impl EmitPayload {
    /// Build a payload with auto-filled timestamp + ``source`` tag.
    /// ``source`` is how observability traces find which handler
    /// emitted what — keep it specific (``rust-backend.handlers.cancel``,
    /// not just ``backend``).
    pub fn new(event_type: impl Into<String>, source: impl Into<String>, payload: Value) -> Self {
        Self {
            event_type: event_type.into(),
            payload,
            source: source.into(),
            timestamp: now_secs_f64(),
            request_id: None,
            uid: None,
            session: None,
        }
    }

    pub fn with_envelope(
        mut self,
        request_id: Option<String>,
        uid: Option<String>,
        session: Option<String>,
    ) -> Self {
        self.request_id = request_id;
        self.uid = uid;
        self.session = session;
        self
    }
}

/// Emit a payload to the chosen lane. Returns the assigned XADD id.
pub async fn aemit(state: &AppState, lane: Lane, payload: EmitPayload) -> Result<String> {
    let stream = match lane {
        Lane::User => &state.config.stream_user,
        Lane::Bg => &state.config.stream_bg,
    };
    let json = serde_json::to_string(&payload)?;
    let mut conn = state.redis.clone();
    let id: String = conn
        .xadd_maxlen(
            stream,
            StreamMaxlen::Approx(100_000),
            "*",
            &[("event", json.as_str())],
        )
        .await?;
    Ok(id)
}

fn now_secs_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}
