//! Cross-product trial scheduling — replaces
//! ``dorian/event/handlers/experiment.py::handle_dataset_upserted_trials``
//! and ``handle_pipeline_upserted_trials``. Pure orchestration:
//! query postgres for the gap (pipeline×dataset pairs not yet
//! evaluated), push one ``cross_product_trial`` job onto the queue
//! per gap. The python runner pops the queue and executes the
//! trial — operator runtime stays python.
//!
//! Kill switch: ``DISABLE_CROSS_PRODUCT_TRIALS=1`` short-circuits
//! both handlers to a single emit. Mirrors the python flag exactly.

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::{json, Value};
use std::env;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const TASK_QUEUE_KEY: &str = "task_queue";
/// Background priority — matches python ``Priority.BACKGROUND = -1``.
const BACKGROUND_PRIORITY: f64 = -1.0;

pub fn register(r: &mut Registry) {
    r.register("DatasetUpserted", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_dataset_upserted(state, event))
    });
    r.register("PipelineUpserted", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_pipeline_upserted(state, event))
    });
}

fn cross_product_disabled() -> bool {
    env::var("DISABLE_CROSS_PRODUCT_TRIALS")
        .ok()
        .map(|s| s.trim() == "1")
        .unwrap_or(false)
}

async fn submit_trial(
    conn: &mut redis::aio::ConnectionManager,
    pipeline_id: &str,
    session: &str,
) {
    let payload = json!({
        "uid": "system",
        "session": session,
        "pipelineId": pipeline_id,
        "source": "cross_product_trial",
        "_tier": "background",
    });
    let serialised = match serde_json::to_string(&payload) {
        Ok(s) => s,
        Err(_) => return,
    };
    let _: redis::RedisResult<()> = conn
        .zadd(TASK_QUEUE_KEY, &serialised, BACKGROUND_PRIORITY)
        .await;
}

async fn handle_dataset_upserted(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let did = match event.payload.get("did").and_then(|v| v.as_str()) {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => return Ok(()),
    };

    if cross_product_disabled() {
        let p = EmitPayload::new(
            "CrossProductTrialsDisabled",
            "rust-backend.handlers.cross_product_trials.dataset_upserted",
            json!({
                "trigger": "new_dataset",
                "dataset_id": did,
                "reason": "DISABLE_CROSS_PRODUCT_TRIALS=1",
            }),
        )
        .with_envelope(event.request_id.clone(), event.uid.clone(), event.session.clone());
        aemit(state, Lane::Bg, p).await?;
        return Ok(());
    }

    let Some(pool) = state.pg.as_ref() else {
        return Ok(());
    };
    let client = pool.get().await?;
    // Find every pipeline that has not yet been evaluated on this dataset.
    let rows = client
        .query(
            "SELECT p.id AS pipeline_id, p.session \
             FROM pipelines p \
             WHERE NOT EXISTS ( \
                 SELECT 1 FROM evaluations e \
                 WHERE e.pipeline_id = p.id AND e.dataset_id = $1 \
             )",
            &[&did],
        )
        .await
        .unwrap_or_default();

    let mut conn = state.redis.clone();
    let mut enqueued: i64 = 0;
    let pipelines_checked = rows.len() as i64;
    for row in rows {
        let pipeline_id: String = row.get("pipeline_id");
        let session: String = row.get("session");
        submit_trial(&mut conn, &pipeline_id, &session).await;
        enqueued += 1;
    }

    let p = EmitPayload::new(
        "CrossProductTrialsScheduled",
        "rust-backend.handlers.cross_product_trials.dataset_upserted",
        json!({
            "trigger": "new_dataset",
            "dataset_id": did,
            "trials_enqueued": enqueued,
            "pipelines_checked": pipelines_checked,
        }),
    )
    .with_envelope(event.request_id.clone(), event.uid.clone(), event.session.clone());
    aemit(state, Lane::Bg, p).await?;
    Ok(())
}

async fn handle_pipeline_upserted(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let pipeline_id = match event
        .payload
        .get("pipeline_id")
        .or_else(|| event.payload.get("pipelineId"))
        .and_then(|v| v.as_str())
    {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => return Ok(()),
    };

    if cross_product_disabled() {
        let p = EmitPayload::new(
            "CrossProductTrialsDisabled",
            "rust-backend.handlers.cross_product_trials.pipeline_upserted",
            json!({
                "trigger": "new_pipeline",
                "pipeline_id": pipeline_id,
                "reason": "DISABLE_CROSS_PRODUCT_TRIALS=1",
            }),
        )
        .with_envelope(event.request_id.clone(), event.uid.clone(), event.session.clone());
        aemit(state, Lane::Bg, p).await?;
        return Ok(());
    }

    let Some(pool) = state.pg.as_ref() else {
        return Ok(());
    };
    let client = pool.get().await?;
    // Get the pipeline's session — needed for the queue payload so
    // the runner can scope context (matches python).
    let pipeline_row = client
        .query_opt(
            "SELECT session FROM pipelines WHERE id = $1",
            &[&pipeline_id],
        )
        .await
        .unwrap_or(None);
    let session: String = match pipeline_row {
        Some(row) => row.get("session"),
        None => return Ok(()),
    };
    // Find every dataset that has not yet been evaluated with this pipeline.
    let rows = client
        .query(
            "SELECT d.id AS dataset_id \
             FROM datasets d \
             WHERE NOT EXISTS ( \
                 SELECT 1 FROM evaluations e \
                 WHERE e.pipeline_id = $1 AND e.dataset_id = d.id \
             )",
            &[&pipeline_id],
        )
        .await
        .unwrap_or_default();

    let mut conn = state.redis.clone();
    let mut enqueued: i64 = 0;
    let datasets_checked = rows.len() as i64;
    for _ in rows {
        // The runner uses pipeline_id + session to schedule one trial
        // per dataset in the gap (the runner internally iterates
        // datasets when processing this background payload). Match
        // python's ``submit_background`` which sends one record with
        // ``source="cross_product_trial"`` per gap row.
        submit_trial(&mut conn, &pipeline_id, &session).await;
        enqueued += 1;
    }

    let _ = Value::Null;
    let p = EmitPayload::new(
        "CrossProductTrialsScheduled",
        "rust-backend.handlers.cross_product_trials.pipeline_upserted",
        json!({
            "trigger": "new_pipeline",
            "pipeline_id": pipeline_id,
            "trials_enqueued": enqueued,
            "datasets_checked": datasets_checked,
        }),
    )
    .with_envelope(event.request_id.clone(), event.uid.clone(), event.session.clone());
    aemit(state, Lane::Bg, p).await?;
    Ok(())
}
