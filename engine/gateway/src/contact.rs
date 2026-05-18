//! `/contact/*` endpoints. Replaces the form-only handlers in
//! `dorian/api/routes/contact.py`:
//!
//!   * `POST /contact/feedback` — survey/feedback form
//!   * `POST /contact/us` — contact-us form (name + email + message)
//!   * `GET  /contact/submissions` — list submissions filtered by
//!     `uid` and / or `type`
//!
//! The bug-report endpoint `POST /contact/bug` stays python because
//! it accepts `multipart/form-data` with file attachments — porting
//! the multipart-with-files path is its own slice. The gateway's
//! reverse-proxy fallback keeps it reachable without a
//! native-route entry.
//!
//! All three native routes:
//!   1. Generate a UUID submission id.
//!   2. Insert a JSONB document into postgres
//!      `doc_contact_submissions` (same shape the python handler
//!      wrote — `_id`, `type`, `uid`, `submitted_at`, plus the
//!      type-specific fields).
//!   3. Emit a `ContactFormSubmitted` event onto `events:user` so
//!      the slack handler (and any future subscriber) sees it.

use axum::{
    extract::{Form, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use uuid::Uuid;

use crate::eventbus::EventBody;
use crate::state::AppState;

const COLLECTION: &str = "doc_contact_submissions";

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/contact/feedback", post(submit_feedback))
        .route("/contact/us", post(submit_contact_us))
        .route("/contact/submissions", get(list_submissions))
}

// ---------------------------------------------------------------------------
// /contact/feedback
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct FeedbackForm {
    pub uid: String,
    #[serde(default)]
    pub name: String,
    pub feedback_type: String,
    pub subject: String,
    pub details: String,
    #[serde(default = "default_rating")]
    pub rating: String,
}

fn default_rating() -> String {
    "5".to_string()
}

#[derive(Debug, Serialize)]
struct SubmitResponse {
    status: &'static str,
    submission_id: String,
}

async fn submit_feedback(
    State(state): State<AppState>,
    Form(form): Form<FeedbackForm>,
) -> impl IntoResponse {
    let submission_id = Uuid::new_v4().to_string();
    let doc = json!({
        "_id": submission_id,
        "type": "feedback",
        "uid": form.uid,
        "name": form.name,
        "submitted_at": chrono::Utc::now().to_rfc3339(),
        "feedback_type": form.feedback_type,
        "subject": form.subject,
        "details": form.details,
        "rating": form.rating,
    });
    persist_and_emit(&state, &submission_id, &doc).await
}

// ---------------------------------------------------------------------------
// /contact/us
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct ContactUsForm {
    pub uid: String,
    pub first_name: String,
    pub last_name: String,
    pub email: String,
    pub subject: String,
    pub message: String,
}

async fn submit_contact_us(
    State(state): State<AppState>,
    Form(form): Form<ContactUsForm>,
) -> impl IntoResponse {
    let submission_id = Uuid::new_v4().to_string();
    let doc = json!({
        "_id": submission_id,
        "type": "contact",
        "uid": form.uid,
        "submitted_at": chrono::Utc::now().to_rfc3339(),
        "first_name": form.first_name,
        "last_name": form.last_name,
        "email": form.email,
        "subject": form.subject,
        "message": form.message,
    });
    persist_and_emit(&state, &submission_id, &doc).await
}

// ---------------------------------------------------------------------------
// /contact/submissions
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct ListQuery {
    #[serde(default)]
    pub uid: Option<String>,
    #[serde(default, rename = "type")]
    pub type_: Option<String>,
    #[serde(default = "default_list_limit")]
    pub limit: i64,
}

fn default_list_limit() -> i64 {
    50
}

async fn list_submissions(
    State(state): State<AppState>,
    Query(q): Query<ListQuery>,
) -> impl IntoResponse {
    let Some(pool) = pool_or_503(&state) else {
        return (StatusCode::SERVICE_UNAVAILABLE, "postgres unavailable").into_response();
    };
    let client = match pool.get().await {
        Ok(c) => c,
        Err(err) => {
            return (StatusCode::SERVICE_UNAVAILABLE, format!("pg get: {err}"))
                .into_response();
        }
    };
    let limit = q.limit.clamp(1, 200);

    // Filter on the JSONB ``uid`` and ``type`` keys; default to no filter.
    // Order by ``data->>submitted_at`` descending so newest-first matches
    // the python handler.
    let rows = match (q.uid.as_deref(), q.type_.as_deref()) {
        (Some(uid), Some(t)) => {
            client
                .query(
                    "SELECT data FROM doc_contact_submissions \
                     WHERE data->>'uid' = $1 AND data->>'type' = $2 \
                     ORDER BY data->>'submitted_at' DESC \
                     LIMIT $3",
                    &[&uid, &t, &limit],
                )
                .await
        }
        (Some(uid), None) => {
            client
                .query(
                    "SELECT data FROM doc_contact_submissions \
                     WHERE data->>'uid' = $1 \
                     ORDER BY data->>'submitted_at' DESC \
                     LIMIT $2",
                    &[&uid, &limit],
                )
                .await
        }
        (None, Some(t)) => {
            client
                .query(
                    "SELECT data FROM doc_contact_submissions \
                     WHERE data->>'type' = $1 \
                     ORDER BY data->>'submitted_at' DESC \
                     LIMIT $2",
                    &[&t, &limit],
                )
                .await
        }
        (None, None) => {
            client
                .query(
                    "SELECT data FROM doc_contact_submissions \
                     ORDER BY data->>'submitted_at' DESC \
                     LIMIT $1",
                    &[&limit],
                )
                .await
        }
    };

    match rows {
        Ok(rows) => {
            let docs: Vec<Value> = rows
                .into_iter()
                .map(|r| {
                    let data: tokio_postgres::types::Json<Value> = r.get(0);
                    data.0
                })
                .collect();
            (StatusCode::OK, Json(docs)).into_response()
        }
        Err(err) => (StatusCode::INTERNAL_SERVER_ERROR, format!("query: {err}"))
            .into_response(),
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// AppState's pg field is currently absent — gateway uses redis +
/// the http_client. Postgres-backed routes need a pool. We piggyback
/// on the same env vars `dorian-rust-backend` reads (`POSTGRES_*`)
/// via a lazy connection per-request — bounded by the deadpool the
/// rust-backend owns. For now: route through a small helper that
/// builds a single connection on demand. If the gateway grows more
/// pg-backed routes, factor a deadpool into AppState.
fn pool_or_503(_state: &AppState) -> Option<&'static deadpool_postgres::Pool> {
    static POOL: std::sync::OnceLock<Option<deadpool_postgres::Pool>> =
        std::sync::OnceLock::new();
    POOL.get_or_init(|| build_pg_pool().ok()).as_ref()
}

fn build_pg_pool() -> anyhow::Result<deadpool_postgres::Pool> {
    use deadpool_postgres::{Config, Runtime};
    use std::env;
    let mut cfg = Config::new();
    cfg.host = Some(env::var("POSTGRES_HOST").unwrap_or_else(|_| "postgres".into()));
    cfg.port = env::var("POSTGRES_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .or(Some(5432));
    cfg.user = Some(env::var("POSTGRES_USER").unwrap_or_else(|_| "dorian".into()));
    cfg.password = env::var("POSTGRES_PASSWORD").ok();
    cfg.dbname = Some(env::var("POSTGRES_DB").unwrap_or_else(|_| "dorian".into()));
    let pool = cfg.create_pool(Some(Runtime::Tokio1), tokio_postgres::NoTls)?;
    Ok(pool)
}

async fn persist_and_emit(
    state: &AppState,
    submission_id: &str,
    doc: &Value,
) -> axum::response::Response {
    let Some(pool) = pool_or_503(state) else {
        return (StatusCode::SERVICE_UNAVAILABLE, "postgres unavailable").into_response();
    };
    let client = match pool.get().await {
        Ok(c) => c,
        Err(err) => {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                format!("pg get: {err}"),
            )
                .into_response();
        }
    };
    if let Err(err) = client
        .execute(
            "INSERT INTO doc_contact_submissions (id, data) \
             VALUES ($1, $2::jsonb)",
            &[
                &submission_id,
                &tokio_postgres::types::Json(doc),
            ],
        )
        .await
    {
        return (StatusCode::INTERNAL_SERVER_ERROR, format!("insert: {err}"))
            .into_response();
    }

    // Emit ContactFormSubmitted onto events:user. Same shape the
    // python ``aemit(Event(...))`` produces — slack handler reads
    // it and posts to webhook.
    let body = EventBody {
        event_type: "ContactFormSubmitted".to_string(),
        payload: doc.clone(),
        source: Some("gateway.contact".to_string()),
        timestamp: Some(chrono::Utc::now().timestamp_millis() as f64 / 1000.0),
        request_id: None,
    };
    crate::eventbus::xadd_user(state, &body).await;

    (
        StatusCode::OK,
        Json(SubmitResponse {
            status: "ok",
            submission_id: submission_id.to_string(),
        }),
    )
        .into_response()
}

#[allow(dead_code)]
fn _silence_collection_warning() -> &'static str {
    COLLECTION
}
