//! Onboarding handlers — tour completion, tooltip feedback, and
//! the per-session state replay sent on ``InitSession``. Replaces
//! ``dorian/event/handlers/onboarding.py``.
//!
//! Persistence shape (postgres ``onboarding`` collection):
//!
//! ```json
//! {
//!     "uid": "...",
//!     "tour_completed": false,
//!     "tooltip_votes": {
//!         "<tooltip_id>": {"vote": "up"|"down", "dwell_ms": int, "ts": iso8601}
//!     }
//! }
//! ```

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::json;

use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

// Per-collection table is ``doc_onboarding`` — one ``doc_<name>``
// table per document collection (see ``backend/db/pg_docstore.py``).
const STREAM_MAXLEN_APPROX: usize = 100_000;

pub fn register(r: &mut Registry) {
    r.register("OnboardingTourCompleted", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_tour_completed(state, event))
    });
    r.register("OnboardingTooltipFeedback", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_tooltip_feedback(state, event))
    });
    r.register("InitSession", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(send_onboarding_state(state, event))
    });
}

/// Helper: pull a string field out of the envelope payload, falling
/// back to the envelope's top-level ``uid`` for the common case.
fn payload_str(event: &EventEnvelope, key: &str) -> Option<String> {
    event
        .payload
        .get(key)
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty())
}

async fn handle_tour_completed(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let uid = event
        .uid
        .clone()
        .or_else(|| payload_str(event, "uid"))
        .filter(|s| !s.is_empty());
    let Some(uid) = uid else {
        return Ok(());
    };

    let Some(pool) = state.pg.as_ref() else {
        tracing::warn!(uid, "postgres unavailable, skipping TourCompleted");
        return Ok(());
    };

    let client = pool.get().await?;
    // CREATE TABLE IF NOT EXISTS — first rust write may beat python's
    // lazy ``Collection._ensure_table`` to the punch on a fresh
    // deploy; idempotent so it's cheap on repeat hits.
    client
        .execute(
            "CREATE TABLE IF NOT EXISTS doc_onboarding ( \
                id TEXT PRIMARY KEY, \
                data JSONB NOT NULL, \
                created_at TIMESTAMPTZ DEFAULT NOW(), \
                updated_at TIMESTAMPTZ DEFAULT NOW())",
            &[],
        )
        .await?;
    let existing: Option<String> = client
        .query_opt(
            "SELECT id FROM doc_onboarding WHERE data->>'uid' = $1 LIMIT 1",
            &[&uid],
        )
        .await?
        .map(|r| r.get(0));

    if let Some(id) = existing {
        client
            .execute(
                "UPDATE doc_onboarding SET data = data || $2::jsonb, updated_at = NOW() \
                 WHERE id = $1",
                &[
                    &id,
                    &tokio_postgres::types::Json(json!({"tour_completed": true})),
                ],
            )
            .await?;
    } else {
        let id = uuid::Uuid::new_v4().simple().to_string();
        client
            .execute(
                "INSERT INTO doc_onboarding (id, data) VALUES ($1, $2::jsonb)",
                &[
                    &id,
                    &tokio_postgres::types::Json(
                        json!({"uid": uid, "tour_completed": true, "tooltip_votes": {}}),
                    ),
                ],
            )
            .await?;
    }
    Ok(())
}

async fn handle_tooltip_feedback(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let uid = event
        .uid
        .clone()
        .or_else(|| payload_str(event, "uid"))
        .filter(|s| !s.is_empty());
    let Some(uid) = uid else { return Ok(()) };

    let Some(tooltip_id) = payload_str(event, "tooltip_id") else {
        return Ok(());
    };
    let Some(vote) = payload_str(event, "vote") else {
        return Ok(());
    };
    if vote != "up" && vote != "down" {
        return Ok(());
    }
    let dwell_ms: i64 = event
        .payload
        .get("dwell_ms")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);

    let Some(pool) = state.pg.as_ref() else {
        tracing::warn!(uid, "postgres unavailable, skipping TooltipFeedback");
        return Ok(());
    };

    let ts = chrono::Utc::now().to_rfc3339();
    let vote_entry = json!({
        "vote": vote,
        "dwell_ms": dwell_ms,
        "ts": ts,
    });
    let client = pool.get().await?;
    client
        .execute(
            "CREATE TABLE IF NOT EXISTS doc_onboarding ( \
                id TEXT PRIMARY KEY, \
                data JSONB NOT NULL, \
                created_at TIMESTAMPTZ DEFAULT NOW(), \
                updated_at TIMESTAMPTZ DEFAULT NOW())",
            &[],
        )
        .await?;
    let existing: Option<String> = client
        .query_opt(
            "SELECT id FROM doc_onboarding WHERE data->>'uid' = $1 LIMIT 1",
            &[&uid],
        )
        .await?
        .map(|r| r.get(0));
    if let Some(id) = existing {
        // ``||`` shallow-merges — for nested ``tooltip_votes`` we
        // need a deep merge so other tooltip slots survive. Use
        // jsonb_set on the specific path.
        client
            .execute(
                "UPDATE doc_onboarding \
                 SET data = jsonb_set(data, ARRAY['tooltip_votes', $2], $3::jsonb, true), \
                     updated_at = NOW() WHERE id = $1",
                &[
                    &id,
                    &tooltip_id,
                    &tokio_postgres::types::Json(vote_entry),
                ],
            )
            .await?;
    } else {
        let id = uuid::Uuid::new_v4().simple().to_string();
        let mut tooltip_votes = serde_json::Map::new();
        tooltip_votes.insert(tooltip_id.clone(), vote_entry);
        client
            .execute(
                "INSERT INTO doc_onboarding (id, data) VALUES ($1, $2::jsonb)",
                &[
                    &id,
                    &tokio_postgres::types::Json(json!({
                        "uid": uid,
                        "tour_completed": false,
                        "tooltip_votes": serde_json::Value::Object(tooltip_votes),
                    })),
                ],
            )
            .await?;
    }
    Ok(())
}

async fn send_onboarding_state(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let uid = event.uid.clone().or_else(|| payload_str(event, "uid"));
    let session = event
        .session
        .clone()
        .or_else(|| payload_str(event, "session"));
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };
    if uid.is_empty() || session.is_empty() {
        return Ok(());
    }

    let mut tour_completed = false;
    let mut tooltip_votes = json!({});

    if let Some(pool) = state.pg.as_ref() {
        let client = pool.get().await?;
        // Read-only path — table may not exist on a fresh deploy if
        // no writer has touched it yet. Treat that as "no record".
        let row_opt = client
            .query_opt(
                "SELECT data FROM doc_onboarding WHERE data->>'uid' = $1 LIMIT 1",
                &[&uid],
            )
            .await
            .ok()
            .flatten();
        if let Some(row) = row_opt {
            let data: tokio_postgres::types::Json<serde_json::Value> = row.get(0);
            if let Some(b) = data.0.get("tour_completed").and_then(|v| v.as_bool()) {
                tour_completed = b;
            }
            if let Some(v) = data.0.get("tooltip_votes").cloned() {
                tooltip_votes = v;
            }
        }
    }

    let payload = json!({
        "tour_completed": tour_completed,
        "tooltip_votes": tooltip_votes,
    });
    let mut conn = state.redis.clone();
    let stream_key = keys::ws_stream(&uid, &session);
    let _: String = conn
        .xadd_maxlen(
            stream_key,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[
                ("event", "ui/onboarding"),
                ("value", &serde_json::to_string(&payload)?),
                ("type", "json"),
            ],
        )
        .await?;
    Ok(())
}
