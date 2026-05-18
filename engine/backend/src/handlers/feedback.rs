//! Feedback persistence — replaces
//! ``dorian/event/handlers/lifecycle.handle_feedback`` and
//! ``handle_feedback_edit_requested``. Pure I/O (redis + postgres);
//! no python compute on the hot path.
//!
//! Wire contract preserved verbatim:
//!
//!   * Redis SET ``feedback:{uid}:{session}:{requestId}`` — full entry
//!   * Redis RPUSH ``feedback:{uid}:{session}:history`` — append log
//!   * Per-answer Redis SET ``{question_id} = answer`` — callback keys
//!   * Postgres ``doc_feedback`` upsert (uid+session+requestId is the
//!     dedup key — matches the unique index in
//!     ``backend/infra/__init__.py``)
//!   * Session-meta transaction: task / eval prompt-dismissal flags,
//!     ``_pendingQueryIds`` filter, dataset-target sync + state/target
//!     re-emit
//!   * Re-profile triggers: ``DataExists`` emit when DQ-review or
//!     quality-input answers are present
//!   * Terminal ``FeedbackStored`` event for downstream consumers

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::{json, Map, Value};
use uuid::Uuid;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::session::{with_session_meta, SessionMetaOutcome};
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 100_000;

/// Suffixes of dataset-config answers that, once provided, justify a
/// re-profile. Mirrors the python ``quality_input_suffixes`` tuple
/// in lifecycle.py — keep in sync when new HITL fields land.
const QUALITY_INPUT_SUFFIXES: &[&str] = &[
    ":value_occurrence_expectations",
    ":syntactic_allowed_values",
    ":semantic_accuracy_rules",
    ":inaccuracy_columns",
    ":range_rules",
    ":quality_threshold_mode",
    ":quality_threshold_override",
    ":feature_columns",
    ":target_columns",
    ":category_column",
    ":sensitive_columns",
    ":balance_target_labels",
    ":compliance_rules",
    ":consistency_label_threshold",
    ":format_schema",
    ":semantic_consistency_rules",
    ":feature_effectiveness_rules",
    ":category_size_threshold",
    ":label_effectiveness_rules",
    ":target_size",
    ":precision_requirements",
    ":relevant_features",
    ":record_relevance_condition",
    ":required_attributes",
];

/// Optional dataset-quality fields the UI omits when the user skips
/// them. Default to empty containers so rule-driven quality metrics
/// don't sit pending forever (mirrors the python defaults exactly).
fn optional_dataset_defaults(did: &str) -> [(String, Value); 6] {
    [
        (format!("dataset:{did}:value_occurrence_expectations"), json!([])),
        (format!("dataset:{did}:syntactic_allowed_values"),       json!({})),
        (format!("dataset:{did}:semantic_accuracy_rules"),        json!([])),
        (format!("dataset:{did}:inaccuracy_columns"),             json!([])),
        (format!("dataset:{did}:range_rules"),                    json!({})),
        (format!("dataset:{did}:sensitive_columns"),              json!([])),
    ]
}

pub fn register(r: &mut Registry) {
    r.register("FeedbackReceived", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_feedback(state, event))
    });
    r.register("FeedbackEditRequested", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_feedback_edit_requested(state, event))
    });
}

async fn handle_feedback(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let uid = string_of(payload, "uid").or_else(|| event.uid.clone());
    let session = string_of(payload, "session").or_else(|| event.session.clone());
    let request_id = string_of(payload, "requestId").unwrap_or_default();
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };
    if uid.is_empty() || session.is_empty() {
        return Ok(());
    }

    // ── Build the answers map ────────────────────────────────────────
    let mut answers: Map<String, Value> = payload
        .get("answers")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();

    // Backfill optional dataset-config defaults for every did mentioned.
    let mut dids: std::collections::HashSet<String> = std::collections::HashSet::new();
    for key in answers.keys() {
        let parts: Vec<&str> = key.split(':').collect();
        if parts.len() >= 3 && parts[0] == "dataset" {
            dids.insert(parts[1].to_string());
        }
    }
    for did in &dids {
        for (k, default_v) in optional_dataset_defaults(did) {
            answers.entry(k).or_insert(default_v);
        }
    }

    // Full record persisted to Redis + Postgres.
    let full_entry = json!({
        "uid":        uid,
        "session":    session,
        "requestId":  request_id,
        "answers":    Value::Object(answers.clone()),
        "pipelineId": payload.get("pipelineId").cloned().unwrap_or(Value::Null),
        "view":       payload.get("view").cloned().unwrap_or(Value::Null),
        "ts":         payload.get("ts").cloned().unwrap_or(Value::Null),
    });
    let full_entry_str = serde_json::to_string(&full_entry)?;

    let mut conn = state.redis.clone();

    // 1. Primary Redis blob (one entry per submission, never overwritten).
    let primary_key = format!("feedback:{uid}:{session}:{request_id}");
    let _: redis::RedisResult<()> = conn.set(&primary_key, &full_entry_str).await;

    // 2. Append-only history list.
    let history_key = format!("feedback:{uid}:{session}:history");
    let _: redis::RedisResult<()> = conn.rpush(&history_key, &full_entry_str).await;

    // 3. Postgres durable backup. Best-effort — Redis is authoritative.
    if let Err(err) = upsert_feedback_pg(state, &uid, &session, &request_id, &full_entry).await {
        tracing::warn!(%err, "feedback pg upsert failed (best-effort, ignoring)");
    }

    // 4. Per-answer callback keys — unblock python's blocking polls
    //    in the profiling pipeline (get_features, get_targets, …).
    for (key, value) in &answers {
        let payload_str: String = match value {
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            Value::Bool(b) => b.to_string(),
            Value::Null => "null".to_string(),
            other => serde_json::to_string(other).unwrap_or_default(),
        };
        let _: redis::RedisResult<()> = conn.set(key.as_str(), payload_str).await;
    }

    // 5. Session-meta transaction: prompt dismissal + pending-query
    //    cleanup + dataset target sync.
    //
    // Extract the dataset.did from meta BEFORE entering the tx so the
    // closure stays Send (no shared-state captures across awaits).
    let pre_meta: Option<Value> = {
        let raw: Option<String> = conn.get(keys::session_meta(&session)).await.ok().flatten();
        raw.as_deref().and_then(|s| serde_json::from_str(s).ok())
    };
    let target_did: Option<String> = pre_meta
        .as_ref()
        .and_then(|m| m.get("dataset"))
        .and_then(|d| d.get("did"))
        .and_then(|v| v.as_str())
        .map(String::from);
    let target_xadd: Option<(String, String)> = target_did.as_ref().and_then(|did| {
        let target_key = format!("dataset:{did}:target_columns");
        answers.get(&target_key).map(|v| {
            (
                did.clone(),
                serde_json::to_string(v).unwrap_or_else(|_| "null".to_string()),
            )
        })
    });

    let answers_for_meta = answers.clone();
    let session_for_meta = session.clone();
    let target_did_for_meta = target_did.clone();
    let _ = with_session_meta(state, &session, move |meta| {
        let mut data = meta.data.clone();
        let is_new = meta.is_new;
        let answers = answers_for_meta.clone();
        let session = session_for_meta.clone();
        let target_did = target_did_for_meta.clone();
        async move {
            if is_new {
                return Ok(None);
            }
            let Some(obj) = data.as_object_mut() else {
                return Ok(None);
            };

            let task_key = format!("session:{session}:task_selection");
            if let Some(v) = answers.get(&task_key) {
                let dismissed = matches!(v, Value::Null)
                    || v.as_str().map(|s| s.is_empty() || s == "__skip__").unwrap_or(false);
                if dismissed {
                    obj.insert("_taskPromptDismissed".into(), Value::Bool(true));
                } else {
                    obj.remove("_taskPromptDismissed");
                }
            }

            let eval_key = format!("session:{session}:eval_selection");
            if let Some(v) = answers.get(&eval_key) {
                let dismissed = matches!(v, Value::Null)
                    || v.as_str().map(|s| s.is_empty() || s == "__skip__").unwrap_or(false);
                if dismissed {
                    obj.insert("_evalPromptDismissed".into(), Value::Bool(true));
                } else {
                    obj.remove("_evalPromptDismissed");
                }
            }

            if let Some(Value::Array(pending)) = obj.get("_pendingQueryIds").cloned() {
                let answered: std::collections::HashSet<&str> =
                    answers.keys().map(|k| k.as_str()).collect();
                let filtered: Vec<Value> = pending
                    .into_iter()
                    .filter(|qid| qid
                        .as_str()
                        .map(|s| !answered.contains(s))
                        .unwrap_or(true))
                    .collect();
                obj.insert("_pendingQueryIds".into(), Value::Array(filtered));
            }

            // Dataset target sync — only writes the target back into meta.
            // The state/target xadd happens outside the tx.
            if let Some(did) = target_did {
                let target_key = format!("dataset:{did}:target_columns");
                if let Some(target_value) = answers.get(&target_key) {
                    if let Some(dataset_obj) = obj
                        .get_mut("dataset")
                        .and_then(|d| d.as_object_mut())
                    {
                        dataset_obj.insert("target".into(), target_value.clone());
                    }
                }
            }
            Ok(Some(data))
        }
    })
    .await;

    if let Some((_did, target_str)) = target_xadd {
        let stream = keys::ws_stream(&uid, &session);
        let _: redis::RedisResult<String> = conn
            .xadd_maxlen(
                &stream,
                StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
                "*",
                &[
                    ("event", "state/target"),
                    ("value", target_str.as_str()),
                    ("type", "json"),
                ],
            )
            .await;
    }

    // 6. DQ-review answers → DataExists re-profile trigger.
    let has_dq_review = answers
        .keys()
        .any(|k| k.starts_with("dq:") && k.contains(":review:"));
    let has_quality_input = answers.keys().any(|k| {
        k.starts_with("dataset:") && QUALITY_INPUT_SUFFIXES.iter().any(|s| k.ends_with(s))
    });

    if has_dq_review || has_quality_input {
        // Read the dataset {did, fpath} from session meta to fill the
        // DataExists payload. Only emit when both are present.
        let meta_key = keys::session_meta(&session);
        let raw: Option<String> = conn.get(&meta_key).await.ok().flatten();
        if let Some(raw) = raw {
            if let Ok(meta) = serde_json::from_str::<Value>(&raw) {
                let did = meta
                    .get("dataset")
                    .and_then(|d| d.get("did"))
                    .and_then(|v| v.as_str())
                    .map(String::from);
                let fpath = meta
                    .get("dataset")
                    .and_then(|d| d.get("fpath"))
                    .and_then(|v| v.as_str())
                    .map(String::from);
                if let (Some(did), Some(fpath)) = (did, fpath) {
                    let payload = EmitPayload::new(
                        "DataExists",
                        "rust-backend.handlers.feedback.handle_feedback",
                        json!({
                            "uid": uid,
                            "session": session,
                            "did": did,
                            "fpath": fpath,
                        }),
                    )
                    .with_envelope(
                        event.request_id.clone(),
                        Some(uid.clone()),
                        Some(session.clone()),
                    );
                    aemit(state, Lane::Bg, payload).await?;
                }
            }
        }
    }

    // 7. Terminal FeedbackStored — downstream observers latch on this.
    let payload = EmitPayload::new(
        "FeedbackStored",
        "rust-backend.handlers.feedback.handle_feedback",
        full_entry,
    )
    .with_envelope(
        event.request_id.clone(),
        Some(uid.clone()),
        Some(session.clone()),
    );
    aemit(state, Lane::Bg, payload).await?;

    Ok(())
}

/// Postgres upsert into ``doc_feedback``. Uses the JSONB-filter
/// pattern the python ``Collection.update_one(filter=…, upsert=True)``
/// produces, so a unique (uid, session, requestId) keeps a single row
/// across retries — same dedup contract as the index in
/// ``backend/infra/__init__.py::create_index([uid,session,requestId], unique=True)``.
async fn upsert_feedback_pg(
    state: &AppState,
    uid: &str,
    session: &str,
    request_id: &str,
    full_entry: &Value,
) -> Result<()> {
    let Some(pool) = state.pg.as_ref() else {
        return Ok(());
    };
    let client = pool.get().await?;
    let new_id = Uuid::new_v4().to_string();
    let data_json = tokio_postgres::types::Json(full_entry.clone());
    // Bind String args explicitly so tokio-postgres sees TEXT, not the
    // ambiguous &str-with-anonymous-lifetime; also bind data via the
    // typed Json wrapper so postgres receives JSONB directly (avoids
    // the `$N::jsonb` text-cast that flagged "error serializing
    // parameter" on this version of the driver).
    let uid_s: String = uid.to_string();
    let session_s: String = session.to_string();
    let req_s: String = request_id.to_string();
    let updated = client
        .execute(
            "UPDATE doc_feedback \
                SET data = $4, updated_at = NOW() \
              WHERE data->>'uid' = $1 \
                AND data->>'session' = $2 \
                AND data->>'requestId' = $3",
            &[&uid_s, &session_s, &req_s, &data_json],
        )
        .await?;
    if updated == 0 {
        let _ = client
            .execute(
                "INSERT INTO doc_feedback (id, data, created_at, updated_at) \
                 VALUES ($1, $2, NOW(), NOW()) \
                 ON CONFLICT (id) DO NOTHING",
                &[&new_id, &data_json],
            )
            .await?;
    }
    Ok(())
}

async fn handle_feedback_edit_requested(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let uid = string_of(payload, "uid").or_else(|| event.uid.clone());
    let session = string_of(payload, "session").or_else(|| event.session.clone());
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };

    let mut conn = state.redis.clone();
    let meta_key = keys::session_meta(&session);
    let raw: Option<String> = conn.get(&meta_key).await.ok().flatten();
    let Some(raw) = raw else {
        return Ok(());
    };
    let meta: Value = serde_json::from_str(&raw)?;

    let did = meta
        .get("dataset")
        .and_then(|d| d.get("did"))
        .and_then(|v| v.as_str())
        .map(String::from);
    let columns: Vec<Value> = meta
        .get("dataset")
        .and_then(|d| d.get("columns"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let Some(did) = did else {
        // No dataset bound → emit FeedbackEditNoDataset and stop.
        let payload = EmitPayload::new(
            "FeedbackEditNoDataset",
            "rust-backend.handlers.feedback.edit_requested",
            json!({"uid": uid, "session": session}),
        )
        .with_envelope(event.request_id.clone(), Some(uid), Some(session));
        aemit(state, Lane::Bg, payload).await?;
        return Ok(());
    };
    if columns.is_empty() {
        let payload = EmitPayload::new(
            "FeedbackEditNoDataset",
            "rust-backend.handlers.feedback.edit_requested",
            json!({"uid": uid, "session": session}),
        )
        .with_envelope(event.request_id.clone(), Some(uid), Some(session));
        aemit(state, Lane::Bg, payload).await?;
        return Ok(());
    }

    let feat_key = format!("dataset:{did}:feature_columns");
    let target_key = format!("dataset:{did}:target_columns");

    let feat_raw: Option<String> = conn.get(&feat_key).await.ok().flatten();
    let target_raw: Option<String> = conn.get(&target_key).await.ok().flatten();

    let current_features: Value = feat_raw
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or_else(|| json!([]));
    let current_targets: Value = target_raw
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or_else(|| json!([]));

    let queries = json!([
        {
            "id": feat_key,
            "type": "multi-select",
            "question": "Select the feature columns of the dataset.",
            "options": columns,
            "defaultValue": current_features,
        },
        {
            "id": target_key,
            "type": "multi-select",
            "question": "Select the target column of the dataset.",
            "options": columns,
            "defaultValue": current_targets,
        },
    ]);

    // Clear callback keys so the subsequent FeedbackReceived overwrites.
    let _: redis::RedisResult<()> = conn.del(&feat_key).await;
    let _: redis::RedisResult<()> = conn.del(&target_key).await;

    // Push state/queries to the SPA.
    let stream = keys::ws_stream(&uid, &session);
    let queries_str = serde_json::to_string(&queries)?;
    let _: redis::RedisResult<String> = conn
        .xadd_maxlen(
            &stream,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[
                ("event", "state/queries"),
                ("value", queries_str.as_str()),
                ("type", "json"),
            ],
        )
        .await;

    let payload = EmitPayload::new(
        "FeedbackEditEmitted",
        "rust-backend.handlers.feedback.edit_requested",
        json!({"uid": uid, "session": session, "questionCount": 2}),
    )
    .with_envelope(event.request_id.clone(), Some(uid), Some(session));
    aemit(state, Lane::Bg, payload).await?;

    let _ = matches!(SessionMetaOutcome::Updated, SessionMetaOutcome::Updated);
    Ok(())
}

fn string_of(map: &Map<String, Value>, key: &str) -> Option<String> {
    map.get(key)
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty())
}
