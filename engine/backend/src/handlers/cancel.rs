//! ``CancelPipeline`` handler. Replaces
//! ``dorian/event/handlers/cancel.py::handle_cancel_pipeline``.
//!
//! Sets the cancel flag in Redis (5-min TTL) and emits a
//! ``PipelineCancelRequested`` downstream event. The flag is
//! checked cooperatively by the pipeline runner before each node
//! starts; the downstream event is what observability + RL
//! mitigation listen for.

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::json;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const CANCEL_TTL_SECS: u64 = 300;

pub fn register(r: &mut Registry) {
    r.register("CancelPipeline", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle(state, event))
    });
}

async fn handle(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let run_id = event
        .payload
        .get("runId")
        .and_then(|v| v.as_str())
        .or_else(|| event.payload.get("run_id").and_then(|v| v.as_str()));
    let run_id = match run_id {
        Some(r) if !r.is_empty() => r.to_string(),
        _ => return Ok(()), // python equivalent silently no-ops on missing runId
    };

    let mut conn = state.redis.clone();
    let _: () = conn
        .set_ex(keys::cancel_run(&run_id), "1", CANCEL_TTL_SECS)
        .await?;

    let downstream = EmitPayload::new(
        "PipelineCancelRequested",
        "rust-backend.handlers.cancel",
        json!({
            "run_id": run_id,
            "uid": event.uid.clone(),
            "session": event.session.clone(),
        }),
    )
    .with_envelope(
        event.request_id.clone(),
        event.uid.clone(),
        event.session.clone(),
    );
    aemit(state, Lane::Bg, downstream).await?;
    Ok(())
}
