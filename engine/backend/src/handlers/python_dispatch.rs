//! Rust subscribers that forward events to python-side job workers.
//!
//! Each entry here mirrors a former ``subscribe(E.X, handle_X)`` call
//! in ``dorian/event/registry.py``. The rust handler subscribes to
//! the original event, packages the payload, and submits a
//! ``post:NAME`` job onto the exec-worker stream. The python wrapper
//! at ``dorian/exec/post_handlers.py::post_NAME`` pops + runs the
//! existing handler body, which keeps the python-resident logic
//! (KB-query helpers, rule-rewrite engine, recommendation engine,
//! LLM extractor, RL trainer integration) addressable from the
//! event-bus without a python ``subscribe()`` registration.
//!
//! Hot-path orchestration that should NOT round-trip through the
//! worker (canvas SET maintenance, suggestion-card render, feedback
//! persistence, recommendation-interaction redis log, etc.) is in
//! its own dedicated handler module — those don't show up here.
//!
//! This module is the migration glue. Rewriting an entry as a native
//! rust handler retires the corresponding ``post:NAME`` wrapper +
//! its python imports.

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::{json, Value};

use crate::event::EventEnvelope;
use crate::exec_jobs::submit_exec_job;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

pub fn register(r: &mut Registry) {
    // ── DQ check chain ──────────────────────────────────────────────
    // DataExists / DataWritten → take the python idempotency lock and
    // submit the profiling job. The completion handler subscribes to
    // DQCheckProfileAndQualityCompleted and runs the post-processing
    // wrapper.
    r.register("DataExists", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_check_data(state, event))
    });
    r.register("DataWritten", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_check_data(state, event))
    });
    r.register("DQCheckProfileAndQualityCompleted",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(forward_post(state, event, "post:dq_check_profile_and_quality_completed"))
        });
    r.register("CanvasScopeUpdated", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(forward_post(state, event, "post:canvas_scope_revalidate"))
    });
    r.register("RecommendationsFetched", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(forward_post(state, event, "post:debug_recommended_pipelines"))
    });

    // PipelineRecommendation* / DataExists / DataProfiled / etc. →
    // attempt_recommendations + recommendation interaction re-rank
    // are now NATIVE rust handlers in
    // ``handlers::recommendations_engine``. Do not also dispatch via
    // python or every event would re-rank twice (once natively, once
    // through the python wrapper).
    //
    // DQ orchestration helpers (``run_data_checks`` /
    // ``evaluate_pathways``) STILL need the python wrappers — they
    // call CSV-backed toolbox checks. Keep them on the dispatch
    // until those native ports land.
    r.register("DataProfiled", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(forward_post(state, event, "post:run_data_checks"))
    });
    r.register("DataProfiled", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(forward_post(state, event, "post:evaluate_pathways"))
    });

    // ── DAG rewrite chain ───────────────────────────────────────────
    for (ev, kind) in [
        ("SuggestionAccepted", "post:apply_mitigation"),
        ("DataMitigationReset", "post:apply_mitigation"),
        ("DataMitigationFinish", "post:apply_mitigation"),
        ("MetafeatureError", "post:encoding_metafeature_error"),
        ("NodeExecutionFailed", "post:node_execution_failed"),
    ] {
        r.register(ev, move |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(forward_post_with_trigger(state, event, kind))
        });
    }

    // ── Group builder (compound DAG expansion) ──────────────────────
    r.register("PipelineNodeAdded", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(forward_post(state, event, "post:operator_dropped"))
    });

    // ── RL chain ────────────────────────────────────────────────────
    r.register("PipelineRunFailed", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(forward_post(state, event, "post:rl_pipeline_run_failed"))
    });
    for ev in ["PipelineRunCompleted", "PipelineRunFailed"] {
        r.register(ev, |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(forward_post_with_trigger(state, event, "post:rl_mitigation_child_completed"))
        });
    }

    // ── Extraction chain ────────────────────────────────────────────
    //
    // ``ExtractPipeline`` is handled natively in rust by
    // ``handlers::extraction`` — no python forwarding. The remaining
    // events still bridge to python until each ports: rule-suggestion
    // / accept / save-rules are LLM-driven (application-specific) and
    // ``ExtractionCorrected`` is bookkeeping that follows a
    // ``handle_extract_pipeline`` call.
    for (ev, kind) in [
        ("ExtractionCorrected", "post:extraction_corrected"),
        ("SaveExtractionRules", "post:save_extraction_rules"),
        ("SaveExtractionRuleSpecs", "post:save_extraction_rule_specs"),
        ("LoadExtractionRules", "post:load_extraction_rules"),
        ("SuggestExtractionRules", "post:suggest_extraction_rules"),
        ("CancelSuggestExtractionRules", "post:cancel_suggest_extraction_rules"),
        ("AcceptExtractionRule", "post:accept_extraction_rule"),
        ("RejectExtractionRule", "post:reject_extraction_rule"),
        ("CreateMcpToken", "post:create_mcp_token"),
    ] {
        r.register(ev, move |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(forward_post(state, event, kind))
        });
    }

    // ── Session init Phase 3 ────────────────────────────────────────
    // Phase 1+2 (state replay) is rust ``session_seed``; Phase 3
    // (tooltips, recommendations stub, dataset-profile verification)
    // stays python via the wrapper.
    r.register("InitSession", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(forward_post(state, event, "post:session_init_phase3"))
    });
}

// ---------------------------------------------------------------------------
// Generic forwarders
// ---------------------------------------------------------------------------

fn build_payload(event: &EventEnvelope) -> Value {
    let mut p = match event.payload.clone() {
        Value::Object(_) => event.payload.clone(),
        other => json!({"value": other}),
    };
    if let Some(obj) = p.as_object_mut() {
        if let Some(uid) = event.uid.as_deref() {
            obj.entry("uid".to_string())
                .or_insert(Value::String(uid.to_string()));
        }
        if let Some(session) = event.session.as_deref() {
            obj.entry("session".to_string())
                .or_insert(Value::String(session.to_string()));
        }
        if let Some(req) = event.request_id.as_deref() {
            obj.entry("requestId".to_string())
                .or_insert(Value::String(req.to_string()));
        }
    }
    p
}

/// Forward the event payload to a fixed ``post:NAME`` worker job.
/// No-op when the payload can't be JSON-serialised.
async fn forward_post(state: &AppState, event: &EventEnvelope, kind: &str) -> Result<()> {
    let payload = build_payload(event);
    let _ = submit_exec_job(state, kind, &payload).await?;
    Ok(())
}

/// Forward the event payload to a fixed ``post:NAME`` worker job,
/// adding ``_trigger`` so the python wrapper can re-dispatch through
/// the original handler with the right event.type. Used for handlers
/// whose body branches on ``event.type``.
async fn forward_post_with_trigger(
    state: &AppState,
    event: &EventEnvelope,
    kind: &str,
) -> Result<()> {
    let mut payload = build_payload(event);
    if let Some(obj) = payload.as_object_mut() {
        obj.insert(
            "_trigger".to_string(),
            Value::String(event.event_type.clone()),
        );
    }
    let _ = submit_exec_job(state, kind, &payload).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Specialised handlers
// ---------------------------------------------------------------------------

/// DataExists / DataWritten — take the profiling lock + submit the
/// existing ``dq_check:profile_and_quality`` job. The python worker
/// pops the job, runs the Dask graph, emits
/// ``DQCheckProfileAndQualityCompleted`` with the result. Lock release
/// happens in the post-completion wrapper.
async fn handle_check_data(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let did = payload
        .get("did")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let session = payload
        .get("session")
        .and_then(|v| v.as_str())
        .or(event.session.as_deref())
        .unwrap_or("")
        .to_string();
    let uid = payload
        .get("uid")
        .and_then(|v| v.as_str())
        .or(event.uid.as_deref())
        .unwrap_or("")
        .to_string();
    let fpath = payload
        .get("fpath")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if did.is_empty() || session.is_empty() || fpath.is_empty() {
        return Ok(());
    }

    let lock_key = format!("profiling:lock:{did}:{session}");
    let rerun_key = format!("{lock_key}:rerun");

    let mut conn = state.redis.clone();
    // SET NX EX 600 — same idempotency lock the python handler took.
    let acquired: Option<String> = redis::cmd("SET")
        .arg(&lock_key)
        .arg("1")
        .arg("NX")
        .arg("EX")
        .arg(600)
        .query_async(&mut conn)
        .await
        .unwrap_or(None);
    if acquired.is_none() {
        // Job in flight — queue exactly one rerun so post-completion
        // re-fires the chain after the current run releases the lock.
        let _: redis::RedisResult<()> = conn.set_ex(&rerun_key, "1", 600).await;
        return Ok(());
    }

    // Lock held; submit the compute job and return. The rerun key + lock
    // release are owned by the completion handler.
    let inputs = json!({
        "uid": uid,
        "session": session,
        "did": did,
        "fpath": fpath,
        "lock_key": lock_key,
        "rerun_key": rerun_key,
        "lane": "user",
    });
    let _ = submit_exec_job(state, "dq_check:profile_and_quality", &inputs).await?;
    let _ = keys::session_meta; // keep the helper imported (other handlers in this module use it)
    Ok(())
}

// handle_recommendation_interaction / handle_attempt_recommendations
// retired — superseded by the native ``recommendations_engine`` rust
// handlers. The python wrappers (``post:recommendation_interaction``
// / ``post:attempt_recommendations``) in
// ``dorian/exec/post_handlers.py`` are dead code and can be removed
// when no test depends on them.
