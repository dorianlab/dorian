//! Helper for pushing python-execution jobs onto the exec-worker
//! stream. Mirrors what
//! ``dorian/exec/worker.py::_decode_job`` consumes:
//!
//!   field      | content
//!   -----------+----------------------------------------------
//!   kind       | logical job kind (``"dq_check:missing_values"``)
//!   job_id     | client-generated id (uuid hex truncated, like python)
//!   submitted_at | unix-seconds float as repr-style string
//!   inputs     | JSON-encoded dict
//!
//! Completion comes back as ``{Kind}Completed`` events on
//! ``events:user`` (or ``events:bg`` if the caller passed
//! ``lane = "bg"``); the corresponding rust handler subscribes to that
//! event type via the standard registry.

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::Value;
use std::time::SystemTime;
use uuid::Uuid;

use crate::state::AppState;

const JOBS_STREAM: &str = "exec:jobs";
const STREAM_MAXLEN_APPROX: usize = 100_000;

/// Submit one job to the exec-worker stream and return the generated
/// ``job_id``. The python worker pops, dispatches via the ``@register``
/// table, and emits ``{Kind}Completed`` with the result on the
/// caller-specified lane.
pub async fn submit_exec_job(
    state: &AppState,
    kind: &str,
    inputs: &Value,
) -> Result<String> {
    let job_id = Uuid::new_v4().simple().to_string()[..16].to_string();
    let submitted_at = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    // python uses ``repr(time.time())`` so the precision matches.
    let submitted_at_str = format!("{}", submitted_at);
    let inputs_json = serde_json::to_string(inputs)?;

    let mut conn = state.redis.clone();
    let _: redis::RedisResult<String> = conn
        .xadd_maxlen(
            JOBS_STREAM,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[
                ("kind", kind),
                ("job_id", job_id.as_str()),
                ("submitted_at", submitted_at_str.as_str()),
                ("inputs", inputs_json.as_str()),
            ],
        )
        .await;
    Ok(job_id)
}
