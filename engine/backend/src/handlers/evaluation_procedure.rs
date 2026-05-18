//! ``EvaluationProcedureAdded`` handler — replaces python
//! ``handle_evaluation_procedure_added``. Same template as
//! ``ranking_objective.rs``: rust core owns the state writes
//! (session meta upsert + ``evaluation_procedures`` postgres
//! upsert), the user-code compile/exec validation runs as a
//! decoupled exec job (``eval_procedure:validate``).
//!
//! Wire format for the validation banner is byte-for-byte
//! identical to python (state/evaluation/validation).
//!
//! Why decouple: ``compile()`` + ``exec()`` are GIL-bound, and
//! running them inside the meta-tx made every "add a procedure"
//! click serialize on the python interpreter. Submitting the
//! validation as an exec job keeps the meta tx + WS xadd in rust,
//! GIL-free.

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

const PG_COLLECTION: &str = "evaluation_procedures";
const EXEC_JOBS_STREAM: &str = "exec:jobs";
const STREAM_MAXLEN_APPROX: usize = 10_000;

pub fn register(r: &mut Registry) {
    r.register(
        "EvaluationProcedureAdded",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_added(state, event))
        },
    );
    r.register(
        "EvalProcedureValidateCompleted",
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

    // Mirror python: payload IS the procedure (no "procedure" wrapper key).
    let procedure = &event.payload;
    let name = procedure
        .get("name")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(String::from);
    let Some(name) = name else { return Ok(()); };
    let proc_uuid = procedure
        .get("uuid")
        .and_then(|v| v.as_str())
        .or_else(|| procedure.get("id").and_then(|v| v.as_str()))
        .unwrap_or("")
        .to_string();
    let language = procedure
        .get("language")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("python")
        .to_string();
    let code = procedure
        .get("code")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let outputs = procedure
        .get("outputs")
        .cloned()
        .unwrap_or(Value::Array(Vec::new()));

    // -- session meta upsert ---------------------------------------------
    let entry = json!({
        "uuid":     proc_uuid,
        "name":     name,
        "language": language,
        "code":     code,
        "outputs":  outputs,
        // Frontend resolver looks up procedure code at runtime via
        // session meta, so embed the executable bits under "meta"
        // exactly like the python original does.
        "meta": {
            "code":     code,
            "language": language,
            "outputs":  outputs,
        },
    });
    let entry_for_meta = entry.clone();
    let proc_uuid_for_meta = proc_uuid.clone();
    let _ = with_session_meta(state, &session, |meta| async move {
        if meta.is_new {
            return Ok(None);
        }
        let mut data = meta.data;
        let obj_map = match &mut data {
            Value::Object(m) => m,
            _ => return Ok(None),
        };
        let existing = obj_map
            .get("EvaluationProcedures")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let mut found = false;
        let mut next: Vec<Value> = existing
            .into_iter()
            .map(|e| {
                if e.get("uuid").and_then(|v| v.as_str()) == Some(&proc_uuid_for_meta) {
                    found = true;
                    entry_for_meta.clone()
                } else {
                    e
                }
            })
            .collect();
        if !found {
            next.push(entry_for_meta.clone());
        }
        obj_map.insert("EvaluationProcedures".into(), Value::Array(next));
        Ok(Some(data))
    })
    .await;

    // -- postgres upsert -------------------------------------------------
    if let Some(pool) = state.pg.as_ref() {
        let id = format!("{session}:{uid}:{proc_uuid}");
        let doc = json!({
            "sessionId": session,
            "userId":    uid,
            "uuid":      proc_uuid,
            "name":      name,
            "language":  language,
            "code":      code,
            "outputs":   procedure.get("outputs").cloned().unwrap_or(Value::Array(Vec::new())),
        });
        if let Err(e) = pg::merge_set(pool, PG_COLLECTION, &id, &doc).await {
            tracing::warn!(name = %name, "evaluation_procedures merge_set failed: {e:#}");
        }
    }

    // -- decoupled compile/exec validation -------------------------------
    submit_validate_job(state, &uid, &session, &proc_uuid, &name, &code).await?;

    Ok(())
}

async fn submit_validate_job(
    state: &AppState,
    uid: &str,
    session: &str,
    proc_uuid: &str,
    name: &str,
    code: &str,
) -> Result<()> {
    let job_id = format!("{:x}", Uuid::new_v4().as_u128());
    let job_id = &job_id[..16.min(job_id.len())];
    let inputs = json!({
        "uid":     uid,
        "session": session,
        "uuid":    proc_uuid,
        "name":    name,
        "code":    code,
        "lane":    "user",
    });
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let fields: Vec<(&str, String)> = vec![
        ("kind",         "eval_procedure:validate".into()),
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

    let proc_uuid = inputs
        .get("uuid")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let name = inputs
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let valid = result
        .get("valid")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let error = result
        .get("error")
        .and_then(|v| v.as_str())
        .map(String::from)
        .or_else(|| {
            event
                .payload
                .get("error")
                .and_then(|v| v.as_str())
                .map(String::from)
        });

    let value_obj = json!({
        "uuid":  proc_uuid,
        "name":  name,
        "valid": valid,
        "error": error,
    });
    let stream_key = keys::ws_stream(&uid, &session);
    let payload: Vec<(&str, String)> = vec![
        ("event", "state/evaluation/validation".into()),
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
