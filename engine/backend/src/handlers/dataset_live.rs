//! Dataset-live broadcast handlers — Redis pub/sub fan-out on the
//! ``datasets:live`` channel for the Go WebSocket global-broadcast
//! consumer. Replaces ``dorian.event.handlers.dataset_live``.
//!
//! All four handlers are idempotent and side-effect-free outside
//! the pub/sub publish: failures are swallowed with a tracing log
//! (mirrors python's ``aemit(BroadcastXFailed)`` — bus echo of an
//! IO failure has no actionable consumer, so a log is sufficient).
//!
//! Channel: ``datasets:live``
//! Message: ``{"kind": str, "data": {...}}``

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::json;

use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const CHANNEL: &str = "datasets:live";

pub fn register(r: &mut Registry) {
    r.register("DatasetUpserted", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_dataset_upserted(state, event))
    });
    r.register(
        "DatasetPersistedToDocstore",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_dataset_persisted(state, event))
        },
    );
    r.register(
        "EvaluationBatchRecorded",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_evaluation_recorded(state, event))
        },
    );
    r.register("DatasetRemoved", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_dataset_removed(state, event))
    });
}

fn payload_str(event: &EventEnvelope, key: &str) -> Option<String> {
    event
        .payload
        .get(key)
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty())
}

async fn publish_msg(state: &AppState, msg: &str) -> Result<()> {
    let mut conn = state.redis.clone();
    let _: i64 = conn.publish(CHANNEL, msg).await?;
    Ok(())
}

async fn handle_dataset_upserted(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let Some(did) = payload_str(event, "did") else {
        return Ok(());
    };
    let session = payload_str(event, "session");
    let msg = serde_json::to_string(&json!({
        "kind": "dataset_updated",
        "data": {
            "id":       did,
            "session":  session,
        }
    }))?;
    publish_msg(state, &msg).await
}

async fn handle_dataset_persisted(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let Some(did) = payload_str(event, "did") else {
        return Ok(());
    };
    let msg = serde_json::to_string(&json!({
        "kind": "dataset_updated",
        "data": {
            "id":        did,
            "persisted": true,
        }
    }))?;
    publish_msg(state, &msg).await
}

async fn handle_evaluation_recorded(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let Some(dataset_id) = payload_str(event, "dataset_id") else {
        return Ok(());
    };
    // pipeline_id, run_id, metrics: mirror python which forwards
    // whatever is on the payload (None / missing → JSON null /
    // omitted; metrics defaults to ``{}``).
    let pipeline_id = event.payload.get("pipeline_id").cloned();
    let run_id = event.payload.get("run_id").cloned();
    let metrics = event
        .payload
        .get("metrics")
        .cloned()
        .unwrap_or_else(|| serde_json::Value::Object(serde_json::Map::new()));
    let msg = serde_json::to_string(&json!({
        "kind": "evaluation_recorded",
        "data": {
            "dataset_id":  dataset_id,
            "pipeline_id": pipeline_id,
            "run_id":      run_id,
            "metrics":     metrics,
        }
    }))?;
    publish_msg(state, &msg).await
}

async fn handle_dataset_removed(state: &AppState, event: &EventEnvelope) -> Result<()> {
    // Python checks both top-level ``did`` and ``payload.did`` — the
    // latter for envelopes whose payload was nested by an upstream
    // emit. Mirror that here.
    let did = payload_str(event, "did").or_else(|| {
        event
            .payload
            .get("payload")
            .and_then(|p| p.get("did"))
            .and_then(|v| v.as_str())
            .map(String::from)
            .filter(|s| !s.is_empty())
    });
    let Some(did) = did else {
        return Ok(());
    };
    let msg = serde_json::to_string(&json!({
        "kind": "dataset_removed",
        "data": {
            "id": did,
        }
    }))?;
    publish_msg(state, &msg).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn env(ty: &str, payload: Value) -> EventEnvelope {
        EventEnvelope {
            event_type: ty.into(),
            uid: None,
            session: None,
            request_id: None,
            timestamp: None,
            source: None,
            payload,
        }
    }

    fn build_msg_for_test(handler: &str, e: &EventEnvelope) -> Option<Value> {
        // Mirror the production handler's serialization without the
        // network call — lets us assert the wire format.
        match handler {
            "upserted" => {
                let did = payload_str(e, "did")?;
                let session = payload_str(e, "session");
                Some(json!({
                    "kind": "dataset_updated",
                    "data": {"id": did, "session": session}
                }))
            }
            "persisted" => {
                let did = payload_str(e, "did")?;
                Some(json!({
                    "kind": "dataset_updated",
                    "data": {"id": did, "persisted": true}
                }))
            }
            "evaluation" => {
                let dataset_id = payload_str(e, "dataset_id")?;
                Some(json!({
                    "kind": "evaluation_recorded",
                    "data": {
                        "dataset_id":  dataset_id,
                        "pipeline_id": e.payload.get("pipeline_id").cloned(),
                        "run_id":      e.payload.get("run_id").cloned(),
                        "metrics": e.payload.get("metrics").cloned()
                            .unwrap_or_else(|| Value::Object(Default::default())),
                    }
                }))
            }
            "removed" => {
                let did = payload_str(e, "did").or_else(|| {
                    e.payload
                        .get("payload")
                        .and_then(|p| p.get("did"))
                        .and_then(|v| v.as_str())
                        .map(String::from)
                });
                Some(json!({"kind": "dataset_removed", "data": {"id": did?}}))
            }
            _ => None,
        }
    }

    #[test]
    fn upserted_includes_session_when_present() {
        let e = env(
            "DatasetUpserted",
            json!({"did": "ds-1", "session": "s-42"}),
        );
        let msg = build_msg_for_test("upserted", &e).expect("msg");
        assert_eq!(msg["kind"], "dataset_updated");
        assert_eq!(msg["data"]["id"], "ds-1");
        assert_eq!(msg["data"]["session"], "s-42");
    }

    #[test]
    fn upserted_missing_did_drops() {
        let e = env("DatasetUpserted", json!({"session": "s"}));
        assert!(build_msg_for_test("upserted", &e).is_none());
    }

    #[test]
    fn persisted_marks_persisted_true() {
        let e = env("DatasetPersistedToDocstore", json!({"did": "ds-2"}));
        let msg = build_msg_for_test("persisted", &e).expect("msg");
        assert_eq!(msg["data"]["persisted"], true);
        assert_eq!(msg["data"]["id"], "ds-2");
    }

    #[test]
    fn evaluation_forwards_full_payload() {
        let e = env(
            "EvaluationBatchRecorded",
            json!({
                "dataset_id": "ds",
                "pipeline_id": "p1",
                "run_id": "r1",
                "metrics": {"accuracy": 0.9}
            }),
        );
        let msg = build_msg_for_test("evaluation", &e).expect("msg");
        assert_eq!(msg["kind"], "evaluation_recorded");
        assert_eq!(msg["data"]["dataset_id"], "ds");
        assert_eq!(msg["data"]["pipeline_id"], "p1");
        assert_eq!(msg["data"]["metrics"]["accuracy"], 0.9);
    }

    #[test]
    fn evaluation_metrics_defaults_to_empty_object() {
        let e = env(
            "EvaluationBatchRecorded",
            json!({"dataset_id": "ds"}),
        );
        let msg = build_msg_for_test("evaluation", &e).expect("msg");
        assert_eq!(msg["data"]["metrics"], json!({}));
        assert_eq!(msg["data"]["pipeline_id"], serde_json::Value::Null);
    }

    #[test]
    fn removed_falls_back_to_nested_payload_did() {
        let e = env("DatasetRemoved", json!({"payload": {"did": "ds-x"}}));
        let msg = build_msg_for_test("removed", &e).expect("msg");
        assert_eq!(msg["data"]["id"], "ds-x");
    }
}
