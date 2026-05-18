//! Progress-update listener — the single python ``progress_update``
//! handler subscribed to seven metafeature/quality events. Replaces
//! ``dorian.event.handlers.listeners.progress_update``.
//!
//! Each event becomes one ``{event:progress, ...}`` line on the
//! per-(uid, session) Redis WS stream, with a status code derived
//! from the event type and the original ``value`` / ``error`` /
//! ``missing`` fields forwarded to the frontend's profiling view.
//!
//! No KB queries, no DB writes — purely a fan-out to the WS stream.
//! Suitable as one of the simple-handler proof points for #81.

use anyhow::Result;
use redis::streams::StreamMaxlen;
use redis::AsyncCommands;
use serde_json::Value;

use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 10_000;

const EVENT_TYPES: &[&str] = &[
    "ComputingMetafeature",
    "MetafeatureComputed",
    "MetafeatureError",
    "ComputingQualityMetric",
    "QualityMetricComputed",
    "QualityMetricError",
    "QualityMetricPendingInput",
];

pub fn register(r: &mut Registry) {
    for ty in EVENT_TYPES {
        r.register(ty, |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle(state, event))
        });
    }
}

fn payload_str(event: &EventEnvelope, key: &str) -> Option<String> {
    event
        .payload
        .get(key)
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty())
}

fn metric_name(event: &EventEnvelope) -> Option<String> {
    payload_str(event, "metafeature").or_else(|| payload_str(event, "metric"))
}

fn status_for(event_type: &str) -> &'static str {
    match event_type {
        "ComputingMetafeature" => "computing",
        "MetafeatureComputed" => "computed",
        "MetafeatureError" => "error",
        "ComputingQualityMetric" => "computing",
        "QualityMetricComputed" => "computed",
        "QualityMetricError" => "error",
        "QualityMetricPendingInput" => "pending",
        _ => "unknown",
    }
}

fn category_for(event_type: &str) -> &'static str {
    if event_type.starts_with("QualityMetric") {
        "data_quality"
    } else {
        "data_profiling"
    }
}

fn value_for(event: &EventEnvelope) -> String {
    let payload = &event.payload;
    match event.event_type.as_str() {
        "ComputingMetafeature" | "ComputingQualityMetric" => "None".into(),
        "MetafeatureComputed" | "QualityMetricComputed" => {
            // The event payload's ``value`` field is already JSON-
            // decoded; re-encode it so the stream carries a
            // self-describing string. Mirrors python's
            // ``json.dumps(_safe_json_value(...))`` minus the numpy
            // coercion (numpy types lose their identity in the JSON
            // round-trip on the producer side already).
            payload
                .get("value")
                .map(|v| serde_json::to_string(v).unwrap_or_else(|_| "None".into()))
                .unwrap_or_else(|| "\"None\"".into())
        }
        "MetafeatureError" | "QualityMetricError" => payload
            .get("error")
            .and_then(|v| v.as_str())
            .map(String::from)
            .unwrap_or_else(|| "None".into()),
        "QualityMetricPendingInput" => match payload.get("missing") {
            Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "[]".into()),
            None => "[]".into(),
        },
        _ => "None".into(),
    }
}

async fn handle(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let Some(uid) = event
        .uid
        .clone()
        .or_else(|| payload_str(event, "uid"))
        .filter(|s| !s.is_empty())
    else {
        return Ok(());
    };
    let Some(session) = event
        .session
        .clone()
        .or_else(|| payload_str(event, "session"))
        .filter(|s| !s.is_empty())
    else {
        return Ok(());
    };
    let Some(did) = payload_str(event, "did") else {
        return Ok(());
    };
    let Some(metric) = metric_name(event) else {
        return Ok(());
    };

    let stream_key = keys::ws_stream(&uid, &session);
    let value = value_for(event);
    let payload: Vec<(&str, String)> = vec![
        ("event", "progress".into()),
        ("uid", uid.clone()),
        ("session", session.clone()),
        ("did", did),
        ("metafeature", metric),
        ("category", category_for(&event.event_type).into()),
        ("status", status_for(&event.event_type).into()),
        ("value", value),
    ];

    let mut conn = state.redis.clone();
    let _: String = conn
        .xadd_maxlen(
            &stream_key,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &payload,
        )
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_envelope(ty: &str, payload: Value) -> EventEnvelope {
        EventEnvelope {
            event_type: ty.into(),
            uid: payload.get("uid").and_then(|v| v.as_str()).map(String::from),
            session: payload
                .get("session")
                .and_then(|v| v.as_str())
                .map(String::from),
            request_id: None,
            timestamp: None,
            source: None,
            payload,
        }
    }

    #[test]
    fn status_and_category_lookup() {
        assert_eq!(status_for("ComputingMetafeature"), "computing");
        assert_eq!(status_for("MetafeatureError"), "error");
        assert_eq!(status_for("QualityMetricPendingInput"), "pending");
        assert_eq!(category_for("ComputingMetafeature"), "data_profiling");
        assert_eq!(category_for("QualityMetricComputed"), "data_quality");
        assert_eq!(category_for("garbage"), "data_profiling");
    }

    #[test]
    fn value_for_computing_is_none_literal() {
        let env = make_envelope(
            "ComputingMetafeature",
            serde_json::json!({"uid":"u","session":"s","did":"d","metafeature":"m"}),
        );
        assert_eq!(value_for(&env), "None");
    }

    #[test]
    fn value_for_computed_round_trips_json() {
        let env = make_envelope(
            "MetafeatureComputed",
            serde_json::json!({
                "uid": "u", "session": "s", "did": "d", "metafeature": "m",
                "value": [1, 2, 3]
            }),
        );
        assert_eq!(value_for(&env), "[1,2,3]");
    }

    #[test]
    fn value_for_error_pulls_error_field() {
        let env = make_envelope(
            "MetafeatureError",
            serde_json::json!({
                "uid": "u", "session": "s", "did": "d", "metafeature": "m",
                "error": "boom"
            }),
        );
        assert_eq!(value_for(&env), "boom");
    }

    #[test]
    fn value_for_pending_serialises_missing_list() {
        let env = make_envelope(
            "QualityMetricPendingInput",
            serde_json::json!({
                "uid": "u", "session": "s", "did": "d", "metric": "m",
                "missing": ["target_columns"]
            }),
        );
        assert_eq!(value_for(&env), "[\"target_columns\"]");
    }

    #[test]
    fn metric_name_falls_back_to_metric_field() {
        let env = make_envelope(
            "QualityMetricComputed",
            serde_json::json!({"metric": "skew"}),
        );
        assert_eq!(metric_name(&env).as_deref(), Some("skew"));
    }
}
