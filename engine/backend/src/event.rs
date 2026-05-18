//! Event envelope shape — mirrors what python ``backend/events.py``
//! reads off the stream. Field names follow the gateway's
//! ``EventBody`` so a single emitter shape works for both consumers.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct EventEnvelope {
    /// Event-type name, ``PascalCase`` past-tense
    /// (``DatasetUploaded``, ``PipelineSaved``, ...).
    #[serde(rename = "type")]
    pub event_type: String,
    /// Payload as opaque JSON — handlers down-cast as needed.
    #[serde(default)]
    pub payload: serde_json::Value,
    /// Optional source identifier; the python emitter usually fills
    /// ``source = "<module>.<fn>"`` so failure traces can locate the
    /// emit site.
    #[serde(default)]
    pub source: Option<String>,
    /// Wall-clock timestamp set by the producer (or by the gateway
    /// emit endpoint when the producer doesn't).
    #[serde(default)]
    pub timestamp: Option<f64>,
    /// Optional correlation id — matches ``request_id`` in the python
    /// envelope so cross-service traces stitch together.
    #[serde(default)]
    pub request_id: Option<String>,
    /// User id, optional. Threaded through the whole event-handler
    /// pipeline for session-scoped operations.
    #[serde(default)]
    pub uid: Option<String>,
    /// Session id — the canvas / RL run / mcp tool invocation. Most
    /// handlers key state by this.
    #[serde(default)]
    pub session: Option<String>,
}

impl EventEnvelope {
    /// Parse an event from the stream entry's ``event`` field. The
    /// gateway emits the whole envelope as a JSON string under
    /// ``event``; the python emitter uses the same shape.
    pub fn from_stream_value(raw: &str) -> Result<Self, serde_json::Error> {
        serde_json::from_str(raw)
    }
}
