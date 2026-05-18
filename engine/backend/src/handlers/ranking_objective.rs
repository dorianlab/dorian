//! ``RankingObjectiveAdded`` handler — replaces python
//! ``handle_ranking_objectives_added``.
//!
//! Architecture: the rust core owns every state write (session meta
//! merge + ``ranking_objectives`` postgres upsert) and submits the
//! python compile/exec validation as a decoupled exec job. The
//! validation result returns as ``ObjectiveValidateCompleted`` —
//! a second rust handler in this module consumes that event and
//! pushes ``state/objectives/validation`` to the WS stream.
//!
//! Wire-format compatibility with the python original is preserved.
//! The user-perceived effect is unchanged; the only difference is
//! that validation is now async (a few hundred ms behind the meta
//! write) instead of synchronously blocking the meta tx.
//!
//! Hot-path GIL contention is now zero — pyo3 marshaling happens
//! once per validation job at the exec-stream boundary, not per
//! event. Same model as ``dq_check:*`` jobs.

use anyhow::Result;
use redis::streams::StreamMaxlen;
use redis::AsyncCommands;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::event::EventEnvelope;
use crate::keys;
use crate::pg;
use crate::registry::{BoxFuture, Registry};
use crate::session::with_session_meta;
use crate::state::AppState;

const PG_COLLECTION: &str = "ranking_objectives";
const EXEC_JOBS_STREAM: &str = "exec:jobs";
const STREAM_MAXLEN_APPROX: usize = 10_000;

pub fn register(r: &mut Registry) {
    r.register(
        "RankingObjectiveAdded",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_added(state, event))
        },
    );
    r.register(
        "ObjectiveValidateCompleted",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_validation_completed(state, event))
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

async fn handle_added(state: &AppState, event: &EventEnvelope) -> Result<()> {
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

    // payload.objective is the new objective record. Mirror python's
    // _normalize_objective — name is required, type ∈ {operator,
    // snippet}, language defaults to "python", code defaults to "".
    let objective = event
        .payload
        .get("objective")
        .cloned()
        .unwrap_or(Value::Null);
    let name = objective
        .get("name")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(String::from);
    let typ = objective
        .get("type")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| matches!(*s, "operator" | "snippet"))
        .map(String::from);
    let language = objective
        .get("language")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("python")
        .to_string();
    let code = objective
        .get("code")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    let (Some(name), Some(typ)) = (name, typ) else {
        // Same surface as python's ValueError — nothing to write.
        return Ok(());
    };

    // -- session meta merge ------------------------------------------------
    let merged_objective = json!({
        "name": name,
        "type": typ,
        "language": language,
        "code": code,
    });
    let name_for_meta = name.clone();
    let merged_for_meta = merged_objective.clone();
    let outcome = with_session_meta(state, &session, |meta| async move {
        if meta.is_new {
            return Ok(None);
        }
        let mut data = meta.data;
        let obj_map = match &mut data {
            Value::Object(m) => m,
            _ => return Ok(None),
        };

        // Merge into rankingObjectives keyed by name (last wins).
        let existing = obj_map
            .get("rankingObjectives")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let mut by_name: std::collections::BTreeMap<String, Value> = existing
            .iter()
            .filter_map(|v| {
                v.get("name")
                    .and_then(|n| n.as_str())
                    .map(|n| (n.to_string(), v.clone()))
            })
            .collect();
        // Preserve insertion order: pull existing names in order, then
        // append/replace with the new one.
        let mut order: Vec<String> = existing
            .iter()
            .filter_map(|v| v.get("name").and_then(|n| n.as_str()).map(String::from))
            .collect();
        if !by_name.contains_key(&name_for_meta) {
            order.push(name_for_meta.clone());
        }
        by_name.insert(name_for_meta.clone(), merged_for_meta.clone());

        let merged: Vec<Value> = order
            .into_iter()
            .filter_map(|n| by_name.remove(&n))
            .collect();
        obj_map.insert("rankingObjectives".into(), Value::Array(merged));
        obj_map.insert("objectiveMode".into(), Value::String("custom".into()));
        Ok(Some(data))
    })
    .await;

    if outcome.is_err() {
        return outcome.map(|_| ());
    }

    // -- postgres upsert ---------------------------------------------------
    if let Some(pool) = state.pg.as_ref() {
        let id = format!("{session}:{uid}:{name}");
        let doc = json!({
            "sessionId": session,
            "userId":    uid,
            "name":      name,
            "type":      typ,
            "language":  language,
            "code":      code,
        });
        if let Err(e) = pg::merge_set(pool, PG_COLLECTION, &id, &doc).await {
            tracing::warn!(name = %name, "ranking_objectives merge_set failed: {e:#}");
        }
    }

    // -- submit validation job (decoupled python compile/exec) ------------
    submit_validate_job(state, &uid, &session, &name, &language, &code).await?;

    Ok(())
}

/// XADD to ``exec:jobs`` so a python exec worker picks the
/// validation up. Result returns as ``ObjectiveValidateCompleted``.
async fn submit_validate_job(
    state: &AppState,
    uid: &str,
    session: &str,
    name: &str,
    language: &str,
    code: &str,
) -> Result<()> {
    let job_id = format!("{:x}", Uuid::new_v4().as_u128());
    let job_id = &job_id[..16.min(job_id.len())];
    let inputs = json!({
        "uid":      uid,
        "session":  session,
        "name":     name,
        "language": language,
        "code":     code,
        // Route completion on the user lane so the validation banner
        // surfaces with low latency (same convention as the dq_check
        // profile_and_quality submit).
        "lane":     "user",
    });
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let fields: Vec<(&str, String)> = vec![
        ("kind",         "objective:validate".into()),
        ("job_id",       job_id.to_string()),
        ("inputs",       serde_json::to_string(&inputs)?),
        ("submitted_at", format!("{now}")),
    ];
    let mut conn = state.redis.clone();
    let _: String = conn
        .xadd_maxlen(
            EXEC_JOBS_STREAM,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &fields,
        )
        .await?;
    Ok(())
}

/// ``ObjectiveValidateCompleted`` — the python exec worker emits this
/// when ``objective:validate`` finishes. Pull the result back out and
/// xadd ``state/objectives/validation`` so the frontend banner updates.
async fn handle_validation_completed(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let inputs = event.payload.get("inputs").cloned().unwrap_or(Value::Null);
    let result = event.payload.get("result").cloned().unwrap_or(Value::Null);

    let uid = inputs
        .get("uid")
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty());
    let session = inputs
        .get("session")
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };

    let name = result
        .get("name")
        .and_then(|v| v.as_str())
        .map(String::from)
        .or_else(|| inputs.get("name").and_then(|v| v.as_str()).map(String::from))
        .unwrap_or_default();
    let valid = result
        .get("valid")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let error = result
        .get("error")
        .and_then(|v| v.as_str())
        .map(String::from)
        // Surface job-level errors (e.g. unknown kind) the same way.
        .or_else(|| {
            event
                .payload
                .get("error")
                .and_then(|v| v.as_str())
                .map(String::from)
        });

    let value_obj = json!({
        "name":  name,
        "valid": valid,
        "error": error,
    });

    let stream_key = keys::ws_stream(&uid, &session);
    let payload: Vec<(&str, String)> = vec![
        ("event", "state/objectives/validation".into()),
        ("value", serde_json::to_string(&value_obj)?),
        ("type",  "json".into()),
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
