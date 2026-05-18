//! Pattern-gated retry invalidation. Phase 2 of
//! (internal design note; not in public repo).
//!
//! When a curated rewrite lands as a successful mitigation
//! (``MitigationRewriteApplied`` from the AI Debugger,
//! ``RLMitigationApplied`` from the RL trainer), every
//! ``exception_patterns`` row that lists this rewrite in its
//! ``mitigations`` array gets ``active`` flipped to ``false``.
//! xproduct's gate JOIN sees the flipped row on the next tick and
//! re-enqueues every (pipeline, dataset) pair that previously
//! failed against this pattern.
//!
//! The inverse mapping (rewrite → patterns) lives on the patterns
//! themselves: each ``ExceptionPattern.mitigations[*].rewrite_id``
//! is the slug of a rewrite that's claimed to fix it. We query
//! that JSONB array directly — no additional schema is needed.

use anyhow::Result;
use serde_json::json;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

pub fn register(r: &mut Registry) {
    r.register("MitigationRewriteApplied", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle(state, event))
    })
    .register("RLMitigationApplied", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle(state, event))
    });
}

async fn handle(state: &AppState, event: &EventEnvelope) -> Result<()> {
    // Both events carry the rewrite slug in different field names —
    // accept either so the same handler covers AI-Debugger and RL
    // mitigations.
    //
    // ``MitigationRewriteApplied`` payload:
    //   { "mitigation": "<slug>", "operator": "<fqn>", ... }
    // ``RLMitigationApplied`` payload:
    //   { "mitigation_slug": "<slug>", "fix_type": "...", ... }
    let rewrite_id = event
        .payload
        .get("mitigation_slug")
        .and_then(|v| v.as_str())
        .or_else(|| event.payload.get("mitigation").and_then(|v| v.as_str()))
        .unwrap_or("");
    if rewrite_id.is_empty() {
        return Ok(());
    }

    let Some(pool) = state.pg.as_ref() else {
        return Ok(());
    };
    let client = pool.get().await?;

    // Postgres JSONB containment: ``mitigations @> '[{"rewrite_id": "<slug>"}]'``
    // matches any row whose mitigations array contains an object
    // with ``rewrite_id == <slug>``. We collect ids first so we can
    // emit one event per invalidated pattern (gives downstream
    // observability a per-row signal without re-querying).
    let needle = json!([{ "rewrite_id": rewrite_id }]);
    let needle_pg = tokio_postgres::types::Json(needle);
    let rows = client
        .query(
            "UPDATE exception_patterns \
             SET active = FALSE \
             WHERE active = TRUE \
               AND mitigations @> $1 \
             RETURNING id",
            &[&needle_pg],
        )
        .await?;

    if rows.is_empty() {
        return Ok(());
    }

    let pattern_ids: Vec<String> = rows
        .iter()
        .filter_map(|r| r.try_get::<_, String>("id").ok())
        .collect();
    tracing::info!(
        rewrite_id = %rewrite_id,
        n_patterns = pattern_ids.len(),
        "pattern-gating: invalidated patterns after rewrite applied"
    );

    // Emit a single PatternsInvalidated downstream event with the
    // full id list — observability + future Phase-4 late-binding
    // attribution can subscribe.
    let downstream = EmitPayload::new(
        "ExceptionPatternsInvalidated",
        "rust-backend.handlers.pattern_gating",
        json!({
            "rewrite_id": rewrite_id,
            "pattern_ids": pattern_ids,
            "trigger": event.event_type.clone(),
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
