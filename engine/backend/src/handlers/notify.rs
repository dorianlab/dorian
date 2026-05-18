//! In-app notification dispatcher — replaces python ``notify`` in
//! ``dorian.event.handlers.listeners``. Maps a small set of backend
//! event types to structured ``{event:notification, ...}`` lines on
//! the per-(uid, session) Redis WS stream so the frontend's bell
//! center can render them.
//!
//! Each spec is a (kind, title, message_fn). Failures (no uid /
//! no session) are silent — same behaviour as the python original.

use anyhow::Result;
use redis::streams::StreamMaxlen;
use redis::AsyncCommands;
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 10_000;

struct Spec {
    kind: &'static str,
    title: &'static str,
    message: fn(&EventEnvelope) -> String,
}

fn spec_for(event_type: &str) -> Option<&'static Spec> {
    match event_type {
        "PipelineRunCompleted" => Some(&Spec {
            kind: "success",
            title: "Pipeline completed",
            message: |_| "Your pipeline run finished successfully.".into(),
        }),
        "PipelineRunFailed" => Some(&Spec {
            kind: "error",
            title: "Pipeline failed",
            message: |event| {
                event
                    .payload
                    .get("error")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty())
                    .map(String::from)
                    .unwrap_or_else(|| "Your pipeline run encountered an error.".into())
            },
        }),
        "RecommendationsFetched" => Some(&Spec {
            kind: "info",
            title: "Recommendations ready",
            message: |_| "New pipeline recommendations are available.".into(),
        }),
        "DataProfiled" => Some(&Spec {
            kind: "success",
            title: "Dataset ready",
            message: |_| "Your dataset has been profiled and is ready to use.".into(),
        }),
        _ => None,
    }
}

const EVENT_TYPES: &[&str] = &[
    "PipelineRunCompleted",
    "PipelineRunFailed",
    "RecommendationsFetched",
    "DataProfiled",
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

async fn handle(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let Some(spec) = spec_for(&event.event_type) else {
        return Ok(());
    };
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

    let message = (spec.message)(event);
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);

    let stream_key = keys::ws_stream(&uid, &session);
    let payload: Vec<(&str, String)> = vec![
        ("event", "notification".into()),
        ("id", Uuid::new_v4().to_string()),
        ("kind", spec.kind.into()),
        ("title", spec.title.into()),
        ("message", message),
        ("createdAt", now_ms.to_string()),
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
    use serde_json::json;

    fn env(ty: &str, payload: serde_json::Value) -> EventEnvelope {
        EventEnvelope {
            event_type: ty.into(),
            uid: payload.get("uid").and_then(|v| v.as_str()).map(String::from),
            session: payload.get("session").and_then(|v| v.as_str()).map(String::from),
            request_id: None,
            timestamp: None,
            source: None,
            payload,
        }
    }

    #[test]
    fn known_event_types_have_specs() {
        for ty in EVENT_TYPES {
            assert!(spec_for(ty).is_some(), "spec missing for {ty}");
        }
    }

    #[test]
    fn unknown_event_type_returns_no_spec() {
        assert!(spec_for("RandomOtherEvent").is_none());
    }

    #[test]
    fn run_failed_uses_payload_error_when_present() {
        let e = env(
            "PipelineRunFailed",
            json!({"uid": "u", "session": "s", "error": "boom"}),
        );
        let spec = spec_for("PipelineRunFailed").unwrap();
        assert_eq!((spec.message)(&e), "boom");
    }

    #[test]
    fn run_failed_falls_back_to_default_message() {
        let e = env("PipelineRunFailed", json!({"uid": "u", "session": "s"}));
        let spec = spec_for("PipelineRunFailed").unwrap();
        assert_eq!(
            (spec.message)(&e),
            "Your pipeline run encountered an error."
        );
    }

    #[test]
    fn recommendations_uses_static_info_message() {
        let e = env("RecommendationsFetched", json!({"uid": "u", "session": "s"}));
        let spec = spec_for("RecommendationsFetched").unwrap();
        assert_eq!(spec.kind, "info");
        assert_eq!(spec.title, "Recommendations ready");
    }
}
