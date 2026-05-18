//! Observability sinks. Replaces the in-process python collector
//! (``dorian/observability/collector.py::record_worker_metrics``).
//!
//! Worker hosts emit ``WorkerMetricsCollected`` periodically; rust
//! pushes one JSON-encoded record per event onto a bounded redis
//! list (``observability:worker_metrics``). Whatever endpoint reads
//! the list (an upcoming rust observability route) returns the most
//! recent records.
//!
//! Bounded: ``LTRIM 0 199`` after every push so the list stays small
//! even on a long-running deployment.

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::{json, Value};
use std::time::SystemTime;

use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const WORKER_METRICS_KEY: &str = "observability:worker_metrics";
const WORKER_METRICS_CAP: isize = 200;

pub fn register(r: &mut Registry) {
    r.register("WorkerMetricsCollected", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_worker_metrics(state, event))
    });
}

async fn handle_worker_metrics(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let payload = event.payload.as_object().cloned().unwrap_or_default();
    // Build a normalised record. Mirrors python's
    // ``WorkerMetricsRecord`` shape so the observability endpoint can
    // deserialise without per-source schema drift.
    let now_ts = payload
        .get("ts")
        .and_then(|v| v.as_f64())
        .unwrap_or_else(|| {
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0)
        });
    let record = json!({
        "ts": now_ts,
        "cpu_percent": payload.get("cpu_percent").cloned().unwrap_or(json!(0.0)),
        "ram_used": payload.get("ram_used").cloned().unwrap_or(json!(0)),
        "ram_total": payload.get("ram_total").cloned().unwrap_or(json!(0)),
        "ram_percent": payload.get("ram_percent").cloned().unwrap_or(json!(0.0)),
        "disk_used": payload.get("disk_used").cloned().unwrap_or(json!(0)),
        "disk_total": payload.get("disk_total").cloned().unwrap_or(json!(0)),
        "disk_percent": payload.get("disk_percent").cloned().unwrap_or(json!(0.0)),
        "dask_workers": payload.get("dask_workers").cloned().unwrap_or(json!(0)),
        "dask_processing": payload.get("dask_processing").cloned().unwrap_or(json!(0)),
    });
    let serialised = serde_json::to_string(&Value::Object(
        record.as_object().cloned().unwrap_or_default(),
    ))?;
    let mut conn = state.redis.clone();
    let _: redis::RedisResult<i64> = conn.lpush(WORKER_METRICS_KEY, &serialised).await;
    let _: redis::RedisResult<()> = conn.ltrim(WORKER_METRICS_KEY, 0, WORKER_METRICS_CAP - 1).await;
    Ok(())
}
