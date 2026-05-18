//! ExecutePipeline → push onto the python execution worker's redis
//! priority queue. Replaces the dispatch slice of
//! ``backend.queue.submit_for_execution``.
//!
//! The runner that POPS from this queue stays python (the actual
//! pipeline execution = sklearn / pandas / Dask, which is operator
//! runtime and out of scope for the rust port).
//!
//! Minimum-viable port: ZADD with a fixed standard-tier score so
//! pipelines keep running. The original python handler also did:
//!   * user tier lookup → priority score
//!   * per-tier concurrency-limit check (xadd queue/concurrency-limit)
//!   * queue-depth status push to the SPA
//! Those are deferred until a tier-aware port lands. They're feature
//! polish, not blocking — without them all pipelines run at the same
//! priority and the SPA's "you're queued behind N pipelines" badge
//! doesn't appear, but Run still triggers an actual run.

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::{json, Value};
use std::time::SystemTime;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const TASK_QUEUE_KEY: &str = "task_queue";
/// Default priority — matches python's ``Priority.STANDARD = -20``.
/// Lower (more negative) = higher priority. Tier-driven scoring is a
/// follow-up port.
const STANDARD_TIER_SCORE: f64 = -20.0;

pub fn register(r: &mut Registry) {
    r.register("ExecutePipeline", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_execute_pipeline(state, event))
    });
}

async fn handle_execute_pipeline(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);

    // Mirror python's tiebreaker: fraction-of-day below the tier base.
    // Earlier submissions within the same tier run first.
    let tiebreaker = (now % 86_400.0) / 86_400.0;
    let score = STANDARD_TIER_SCORE + tiebreaker;

    // Python wraps the inbound event payload in
    // ``{**event.data, "_tier": ..., "_submit_ts": ...}``. Mirror so
    // the python runner sees the same shape.
    let mut wrapped: serde_json::Map<String, Value> = payload.clone();
    wrapped.insert("_tier".into(), Value::String("standard".into()));
    wrapped.insert("_submit_ts".into(), json!(now));
    let wrapped_str = serde_json::to_string(&Value::Object(wrapped))?;

    let mut conn = state.redis.clone();
    let _: redis::RedisResult<()> = conn.zadd(TASK_QUEUE_KEY, &wrapped_str, score).await;

    // Mirror python's ``aemit(PipelineQueued)`` so observability and
    // any rust subscriber sees the queued event.
    let p = EmitPayload::new(
        "PipelineQueued",
        "rust-backend.handlers.execute_pipeline",
        Value::Object(payload.clone()),
    )
    .with_envelope(
        event.request_id.clone(),
        event.uid.clone(),
        event.session.clone(),
    );
    aemit(state, Lane::Bg, p).await?;
    Ok(())
}
