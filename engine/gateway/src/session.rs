//! Session-management HTTP routes. Replaces ``dorian/api/routes/session.py``.
//!
//! Six routes — create / list / rename / delete / get / state. All
//! pure redis ops + the occasional eventbus emit; the python
//! versions land here without losing behaviour. The gateway already
//! has redis on ``AppState``, so adding routes is a one-file
//! extension.
//!
//! Refactor over python:
//!
//!   * The python had two ``aemit`` variants (``aemit`` blocking +
//!     ``aemit_bg`` fire-and-forget); both ultimately hit the same
//!     stream. The rust port uses one path — every emit is
//!     fire-and-forget from the route's perspective because the
//!     gateway's eventbus emit is already non-blocking against the
//!     consumer side.
//!   * The python's ``/session/list`` race-safe lock used a fixed
//!     50 ms × 20 = 1 s poll; here it's exponential 5 → 100 ms,
//!     same total budget but fewer Redis ops under contention.
//!   * The python's ``get_session_state`` raised ``HTTPException``
//!     with two different "not found" messages depending on
//!     whether the session existed but had no pipeline. We collapse
//!     to one ``404 Not Found``; the frontend's only branch is
//!     "is the response 200 or not".

use axum::{
    extract::{Form, Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post, MethodRouter},
    Json, Router,
};
use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::time::Duration;
use tokio::time::sleep;
use tracing::{debug, warn};
use uuid::Uuid;

use crate::eventbus::EventBody;
use crate::state::AppState;

pub fn routes() -> Router<AppState> {
    let by_id: MethodRouter<AppState> = MethodRouter::new()
        .get(get_session)
        .delete(delete_session);
    Router::new()
        .route("/session/create", post(create_session))
        .route("/session/list", get(list_sessions))
        .route("/session/rename", post(rename_session))
        .route("/session/:session_id", by_id)
        .route("/session/:session_id/state", get(get_session_state))
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn meta_key(session_id: &str) -> String {
    format!("session:{session_id}:meta")
}

fn user_sessions_key(uid: &str) -> String {
    format!("user:{uid}:sessions")
}

fn first_session_lock_key(uid: &str) -> String {
    format!("user:{uid}:first_session_lock")
}

fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339()
}

/// Best-effort eventbus emit — same shape ``aemit`` produces in
/// python. Gateway already has the ``events:bg`` stream maxlen
/// config; reuse the eventbus producer instead of duplicating the
/// XADD here.
async fn emit_event(state: &AppState, lane: &str, body: EventBody) {
    use redis::streams::StreamMaxlen;
    let cfg = &state.inner.config;
    let stream = match lane {
        "user" => &cfg.stream_user,
        _ => &cfg.stream_bg,
    };
    let json = match serde_json::to_string(&body) {
        Ok(s) => s,
        Err(e) => {
            warn!("eventbus emit serialise failed: {e}");
            return;
        }
    };
    let mut conn = state.inner.redis.clone();
    let res: redis::RedisResult<String> = conn
        .xadd_maxlen(
            stream,
            StreamMaxlen::Approx(cfg.stream_maxlen as usize),
            "*",
            &[("event", json.as_str())],
        )
        .await;
    if let Err(e) = res {
        warn!("eventbus emit failed: {e}");
    }
}

// ---------------------------------------------------------------------------
// POST /session/create
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct CreateForm {
    uid: String,
    name: String,
}

#[derive(Serialize)]
struct CreateResponse {
    session_id: String,
    meta: Value,
}

async fn create_session(
    State(state): State<AppState>,
    Form(form): Form<CreateForm>,
) -> impl IntoResponse {
    let session_id = Uuid::new_v4().to_string();
    let now = now_iso();
    let meta = json!({
        "session_id": session_id,
        "name": form.name,
        "uid": form.uid,
        "created_at": now,
        "updated_at": now,
        "dataset": Value::Null,
        "pipeline": Value::Null,
    });

    let mut conn = state.inner.redis.clone();
    let raw = match serde_json::to_string(&meta) {
        Ok(s) => s,
        Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    };
    if let Err(e) = conn.set::<_, _, ()>(meta_key(&session_id), &raw).await {
        return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
    }
    if let Err(e) = conn
        .rpush::<_, _, ()>(user_sessions_key(&form.uid), &session_id)
        .await
    {
        return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
    }

    emit_event(
        &state,
        "bg",
        EventBody {
            event_type: "SessionCreated".into(),
            payload: meta.clone(),
            source: Some("rust-gateway.session.create".into()),
            timestamp: Some(unix_secs()),
            request_id: None,
        },
    )
    .await;

    (
        StatusCode::OK,
        Json(CreateResponse {
            session_id,
            meta,
        }),
    )
        .into_response()
}

fn unix_secs() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ---------------------------------------------------------------------------
// GET /session/list?uid=X
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct ListQuery {
    uid: String,
}

async fn list_sessions(
    State(state): State<AppState>,
    Query(q): Query<ListQuery>,
) -> impl IntoResponse {
    let mut conn = state.inner.redis.clone();
    let session_ids: Vec<String> = match conn.lrange(user_sessions_key(&q.uid), 0, -1).await {
        Ok(v) => v,
        Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    };

    let session_ids = if session_ids.is_empty() {
        // Race-safe first-session create — same shape as the python.
        let lock_key = first_session_lock_key(&q.uid);
        let acquired: Option<String> = redis::cmd("SET")
            .arg(&lock_key)
            .arg(Uuid::new_v4().to_string())
            .arg("NX")
            .arg("EX")
            .arg(10)
            .query_async(&mut conn)
            .await
            .unwrap_or(None);
        if acquired.is_some() {
            // Re-check inside the lock — another request might have
            // RPUSH-ed between our LRANGE and our SETNX.
            let inside: Vec<String> =
                conn.lrange(user_sessions_key(&q.uid), 0, -1).await.unwrap_or_default();
            let result = if inside.is_empty() {
                let session_id = Uuid::new_v4().to_string();
                let now = now_iso();
                let meta = json!({
                    "session_id": session_id,
                    "name": "First Session",
                    "uid": q.uid,
                    "created_at": now,
                    "updated_at": now,
                    "dataset": Value::Null,
                    "pipeline": Value::Null,
                });
                let raw = serde_json::to_string(&meta).unwrap_or_default();
                let _ = conn.set::<_, _, ()>(meta_key(&session_id), &raw).await;
                let _ = conn
                    .rpush::<_, _, ()>(user_sessions_key(&q.uid), &session_id)
                    .await;
                vec![session_id]
            } else {
                inside
            };
            // Best-effort lock release; TTL covers crashes.
            let _: redis::RedisResult<i32> = conn.del(&lock_key).await;
            result
        } else {
            // Lock loser — exponential backoff up to ~1 s waiting for
            // the winner to RPUSH.
            let mut waited = Duration::ZERO;
            let cap = Duration::from_secs(1);
            let mut backoff = Duration::from_millis(5);
            let mut found = Vec::new();
            while waited < cap {
                sleep(backoff).await;
                waited += backoff;
                backoff = std::cmp::min(backoff * 2, Duration::from_millis(100));
                let v: Vec<String> = conn
                    .lrange(user_sessions_key(&q.uid), 0, -1)
                    .await
                    .unwrap_or_default();
                if !v.is_empty() {
                    found = v;
                    break;
                }
            }
            found
        }
    } else {
        session_ids
    };

    if session_ids.is_empty() {
        return (StatusCode::OK, Json(Vec::<Value>::new())).into_response();
    }

    // MGET all metas in one round-trip — same optimisation as python.
    let keys: Vec<String> = session_ids.iter().map(|sid| meta_key(sid)).collect();
    let raws: Vec<Option<String>> = conn.mget(&keys).await.unwrap_or_default();
    let mut metas: Vec<Value> = Vec::with_capacity(session_ids.len());
    for raw in raws {
        let Some(raw) = raw else { continue };
        match serde_json::from_str::<Value>(&raw) {
            Ok(v) => metas.push(v),
            Err(_) => {
                emit_event(
                    &state,
                    "bg",
                    EventBody {
                        event_type: "SessionMetaCorrupt".into(),
                        payload: json!({"uid": q.uid}),
                        source: Some("rust-gateway.session.list".into()),
                        timestamp: Some(unix_secs()),
                        request_id: None,
                    },
                )
                .await;
            }
        }
    }
    (StatusCode::OK, Json(metas)).into_response()
}

// ---------------------------------------------------------------------------
// POST /session/rename
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct RenameForm {
    session_id: String,
    new_title: String,
}

async fn rename_session(
    State(state): State<AppState>,
    Form(form): Form<RenameForm>,
) -> impl IntoResponse {
    let mut conn = state.inner.redis.clone();
    let raw: Option<String> = conn.get(meta_key(&form.session_id)).await.unwrap_or(None);
    let Some(raw) = raw else {
        return (StatusCode::NOT_FOUND, Json(json!({"error": "Session not found"})))
            .into_response();
    };
    let mut meta: Value = serde_json::from_str(&raw).unwrap_or_else(|_| json!({}));
    if let Some(obj) = meta.as_object_mut() {
        obj.insert("name".into(), Value::String(form.new_title));
        obj.insert("updated_at".into(), Value::String(now_iso()));
    }
    let new_raw = serde_json::to_string(&meta).unwrap_or_default();
    if let Err(e) = conn.set::<_, _, ()>(meta_key(&form.session_id), &new_raw).await {
        return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
    }
    (StatusCode::OK, Json(json!({"status": "renamed", "meta": meta}))).into_response()
}

// ---------------------------------------------------------------------------
// DELETE /session/{session_id}?uid=X
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct DeleteQuery {
    uid: String,
}

async fn delete_session(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
    Query(q): Query<DeleteQuery>,
) -> impl IntoResponse {
    let mut conn = state.inner.redis.clone();
    let _: redis::RedisResult<i32> = conn.del(meta_key(&session_id)).await;
    let _: redis::RedisResult<i32> = conn.lrem(user_sessions_key(&q.uid), 0, &session_id).await;
    (StatusCode::OK, Json(json!({"status": "deleted"}))).into_response()
}

// ---------------------------------------------------------------------------
// GET /session/{session_id}
// ---------------------------------------------------------------------------

async fn get_session(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
) -> impl IntoResponse {
    let mut conn = state.inner.redis.clone();
    debug!(session_id = %session_id, "GET /session/{session_id}");
    let raw: Option<String> = conn.get(meta_key(&session_id)).await.unwrap_or(None);
    let Some(raw) = raw else {
        return (StatusCode::NOT_FOUND, Json(json!({"error": "Session not found"})))
            .into_response();
    };
    let meta: Value = serde_json::from_str(&raw).unwrap_or_else(|_| json!({}));
    (StatusCode::OK, Json(meta)).into_response()
}

// ---------------------------------------------------------------------------
// GET /session/{session_id}/state
// ---------------------------------------------------------------------------

async fn get_session_state(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
) -> impl IntoResponse {
    let mut conn = state.inner.redis.clone();
    let raw: Option<String> = conn.get(meta_key(&session_id)).await.unwrap_or(None);
    let Some(raw) = raw else {
        return (StatusCode::NOT_FOUND, Json(json!({"error": "Session not found"})))
            .into_response();
    };
    let meta: Value = serde_json::from_str(&raw).unwrap_or_else(|_| json!({}));

    let pipeline = meta.get("pipeline").cloned().unwrap_or(Value::Null);
    let dataset = meta.get("dataset").cloned().unwrap_or(Value::Null);
    if pipeline.is_null() && dataset.is_null() {
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "Session has no state yet"})),
        )
            .into_response();
    }

    let mut last_run = Value::Null;
    if let Some(run_id) = meta.get("last_run_id").and_then(|v| v.as_str()) {
        let run_raw: Option<String> = conn.get(format!("execution:{run_id}")).await.unwrap_or(None);
        if let Some(run_raw) = run_raw {
            last_run = serde_json::from_str(&run_raw).unwrap_or(Value::Null);
        }
    }

    (
        StatusCode::OK,
        Json(json!({
            "pipeline": pipeline,
            "lastRun": last_run,
            "selectedTask": meta.get("selected_task").cloned().unwrap_or(Value::Null),
            "selectedEval": meta.get("selected_eval").cloned().unwrap_or(Value::Null),
            "selectedObjectives": meta.get("selected_objectives").cloned()
                .unwrap_or(Value::Array(vec![])),
        })),
    )
        .into_response()
}
