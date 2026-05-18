//! Canvas-scope (operator SET) maintenance — replaces the
//! self-contained slices of ``dorian/event/handlers/risk_debugger.py``
//! that maintain ``session:{session}:canvas_operators``.
//!
//! Why split it from the rest of the AI Debugger chain: the SET is
//! pure Redis IO + pipeline-JSON traversal, both fast in rust. The
//! AI Debugger's ``identify_risks`` chain itself is staying in
//! python for now (its KB queries already go through the rust
//! snapshot via ``dorian_native``, but the orchestration is heavy).
//!
//! Handlers (this module):
//!   * ``PipelineComposed``      → wipe canvas SET + reset suggestions.
//!   * ``PipelineRetrieved``     → replace SET from pipeline JSON.
//!   * ``RecommendationPipelineSaved`` → same as PipelineRetrieved
//!     (the python registry subscribed both events to the same
//!     handler; mirror that here).
//!
//! After updating the SET we re-emit ``TaskIdentified`` per operator
//! so python's still-subscribed ``identify_risks`` re-runs the chain
//! against the fresh scope. Once the AI Debugger ports too, the
//! TaskIdentified hop becomes an internal call.

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::{json, Value};

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 100_000;

pub fn register(r: &mut Registry) {
    r.register("PipelineComposed", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_pipeline_composed(state, event))
    });
    r.register("PipelineRetrieved", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(sync_canvas_operators(state, event))
    });
    r.register(
        "RecommendationPipelineSaved",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(sync_canvas_operators(state, event))
        },
    );
    // PipelineNodeAdded → just the SADD onto canvas_operators (mirrors
    // the redis line of python ``identify_operator_risks``). The AI
    // Debugger chain itself (debounced risk analysis) stays python
    // until that ports.
    r.register("PipelineNodeAdded", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_node_added(state, event))
    });
    // PipelineNodeRemoved → SREM + suggestions/reset + emit
    // TaskIdentified per remaining operator + emit CanvasScopeUpdated
    // (with the ``affected_operators`` payload python's
    // ``_revalidate_data_checks`` subscribes to). Mirrors the python
    // ``handle_node_removed`` SET-mutation slice.
    r.register("PipelineNodeRemoved", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_node_removed(state, event))
    });
}

/// ``PipelineComposed`` (frontend-emitted Compose click) — clear the
/// entire canvas operator SET so the AI Debugger starts from a clean
/// slate, and tell the SPA to drop its rendered suggestions.
async fn handle_pipeline_composed(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let (uid, session) = match (uid_of(event), session_of(event)) {
        (Some(u), Some(s)) => (u, s),
        _ => return Ok(()),
    };
    let mut conn = state.redis.clone();

    let _: redis::RedisResult<()> = conn.del(keys::canvas_operators(&session)).await;

    push_suggestions_reset(&mut conn, &uid, &session).await;

    // Mirror python's terminal ``aemit(SuggestionsReset)`` so any
    // observer / analytics counter that listens for it still fires.
    let payload = EmitPayload::new(
        "SuggestionsReset",
        "rust-backend.handlers.risk_scope.pipeline_composed",
        json!({"session": session}),
    )
    .with_envelope(event.request_id.clone(), Some(uid), Some(session));
    aemit(state, Lane::Bg, payload).await?;
    Ok(())
}

/// ``PipelineRetrieved`` / ``RecommendationPipelineSaved`` — repopulate
/// the canvas operator SET from the pipeline JSON the SPA emitted in
/// ``payload.value``. After the SET is rewritten, fire one
/// ``TaskIdentified`` per operator so python's identify_risks chain
/// re-runs against the new scope.
async fn sync_canvas_operators(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let (uid, session) = match (uid_of(event), session_of(event)) {
        (Some(u), Some(s)) => (u, s),
        _ => return Ok(()),
    };
    let value = event.payload.get("value").cloned().unwrap_or(Value::Null);
    let pipeline = extract_pipeline(&value);
    let operators = extract_operators(&pipeline);

    let mut conn = state.redis.clone();
    let key = keys::canvas_operators(&session);
    let _: redis::RedisResult<()> = conn.del(&key).await;
    if !operators.is_empty() {
        let _: redis::RedisResult<()> = conn.sadd(&key, &operators).await;
    }

    push_suggestions_reset(&mut conn, &uid, &session).await;

    // Re-trigger the AI Debugger chain (still python-subscribed to
    // TaskIdentified). When the chain ports, this hop disappears.
    for op_name in &operators {
        let payload = EmitPayload::new(
            "TaskIdentified",
            "rust-backend.handlers.risk_scope.sync_canvas_operators",
            json!({
                "uid": uid,
                "session": session,
                "operator": op_name,
            }),
        )
        .with_envelope(
            event.request_id.clone(),
            Some(uid.clone()),
            Some(session.clone()),
        );
        aemit(state, Lane::Bg, payload).await?;
    }

    Ok(())
}

/// PipelineNodeAdded — add the operator FQN to ``canvas_operators``
/// SET. Skips Parameter / Snippet / custom nodes (names without
/// dots). The python ``identify_operator_risks`` keeps subscribing to
/// PipelineNodeAdded for the debounced risk-analysis run; both
/// handlers SADD the same value (idempotent).
async fn handle_node_added(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    // Frontend sometimes wraps the node fields under "payload"; mirror
    // python's `payload = event.data.get("payload", event.data)`.
    let inner = payload
        .get("payload")
        .and_then(|v| v.as_object())
        .unwrap_or(payload);
    let session = match session_of(event) {
        Some(s) => s,
        None => return Ok(()),
    };
    let op_name = inner
        .get("nodeName")
        .or_else(|| inner.get("name"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if op_name.is_empty() || !op_name.contains('.') {
        return Ok(());
    }
    let mut conn = state.redis.clone();
    let _: redis::RedisResult<()> = conn
        .sadd(keys::canvas_operators(&session), op_name)
        .await;
    Ok(())
}

/// PipelineNodeRemoved — SREM + suggestions/reset + emit
/// TaskIdentified for each remaining operator + emit
/// CanvasScopeUpdated. The python ``handle_node_removed`` originally
/// did all of this PLUS ``_revalidate_data_checks`` (CSV read +
/// chi-squared etc.) — that revalidation now subscribes to the
/// CanvasScopeUpdated event we emit below and runs as a python
/// "submitted job" until the AI Debugger chain ports.
async fn handle_node_removed(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let inner = payload
        .get("payload")
        .and_then(|v| v.as_object())
        .unwrap_or(payload);
    let (uid, session) = match (uid_of(event), session_of(event)) {
        (Some(u), Some(s)) => (u, s),
        _ => return Ok(()),
    };
    let op_name = inner
        .get("nodeName")
        .or_else(|| inner.get("name"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if op_name.is_empty() || !op_name.contains('.') {
        return Ok(());
    }

    let mut conn = state.redis.clone();
    let key = keys::canvas_operators(&session);
    let _: redis::RedisResult<()> = conn.srem(&key, &op_name).await;
    push_suggestions_reset(&mut conn, &uid, &session).await;

    // Re-fire identify_risks (python-subscribed to TaskIdentified) for
    // each operator still on the canvas, so the AI Debugger
    // suggestions narrow to the new scope.
    let remaining: Vec<String> = conn.smembers(&key).await.unwrap_or_default();
    for remaining_op in &remaining {
        let payload = EmitPayload::new(
            "TaskIdentified",
            "rust-backend.handlers.risk_scope.node_removed",
            json!({
                "uid": uid,
                "session": session,
                "operator": remaining_op,
            }),
        )
        .with_envelope(
            event.request_id.clone(),
            Some(uid.clone()),
            Some(session.clone()),
        );
        aemit(state, Lane::Bg, payload).await?;
    }

    // CanvasScopeUpdated → python ``_revalidate_data_checks`` runs the
    // CSV-based revalidation off this hop. ``affected_operators`` is
    // the new (smaller) scope.
    let scope_payload = EmitPayload::new(
        "CanvasScopeUpdated",
        "rust-backend.handlers.risk_scope.node_removed",
        json!({
            "removed": op_name,
            "remaining": remaining.len(),
            "affected_operators": remaining,
            "session": session,
        }),
    )
    .with_envelope(
        event.request_id.clone(),
        Some(uid.clone()),
        Some(session.clone()),
    );
    aemit(state, Lane::Bg, scope_payload).await?;
    Ok(())
}

async fn push_suggestions_reset(
    conn: &mut redis::aio::ConnectionManager,
    uid: &str,
    session: &str,
) {
    let stream = keys::ws_stream(uid, session);
    let res: redis::RedisResult<String> = conn
        .xadd_maxlen(
            &stream,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[("event", "suggestions/reset")],
        )
        .await;
    if let Err(err) = res {
        tracing::warn!(%err, stream, "suggestions/reset xadd failed");
    }
}

fn uid_of(event: &EventEnvelope) -> Option<String> {
    event
        .uid
        .clone()
        .or_else(|| {
            event
                .payload
                .get("uid")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty())
}

fn session_of(event: &EventEnvelope) -> Option<String> {
    event
        .session
        .clone()
        .or_else(|| {
            event
                .payload
                .get("session")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty())
}

/// Mirror of ``risk_pathways._extract_pipeline_from_event``.
/// The SPA can carry pipelineHistory ({headId, pipelines: [...]}),
/// a single version dict with ``pipeline`` key, a raw pipeline dict,
/// or a JSON string of any of those.
fn extract_pipeline(value: &Value) -> Value {
    fn parse(s: &str) -> Value {
        serde_json::from_str(s).unwrap_or(Value::Null)
    }
    let value = match value {
        Value::String(s) => parse(s),
        v => v.clone(),
    };
    if let Some(obj) = value.as_object() {
        // pipelineHistory → pick head pipeline
        if let Some(pipelines) = obj.get("pipelines").and_then(|v| v.as_array()) {
            let head_id = obj.get("headId").and_then(|v| v.as_str());
            let pick: Option<&Value> = head_id
                .and_then(|hid| pipelines.iter().find(|p| {
                    p.get("id").and_then(|v| v.as_str()) == Some(hid)
                }))
                .or_else(|| pipelines.first());
            if let Some(p) = pick {
                let inner = p.get("pipeline").cloned().unwrap_or_else(|| p.clone());
                return match inner {
                    Value::String(s) => parse(&s),
                    v => v,
                };
            }
            return Value::Null;
        }
        // version dict with "pipeline" key
        if let Some(inner) = obj.get("pipeline") {
            return match inner {
                Value::String(s) => parse(s),
                v => v.clone(),
            };
        }
    }
    value
}

/// Mirror of ``risk_pathways._extract_operators``: walk
/// ``pipeline.nodes`` (dict or list), keep operator FQNs (names that
/// contain ``.`` — Parameters / Snippets / custom nodes are skipped).
fn extract_operators(pipeline: &Value) -> Vec<String> {
    let body = pipeline
        .get("pipeline")
        .cloned()
        .unwrap_or_else(|| pipeline.clone());
    let body = match body {
        Value::String(s) => serde_json::from_str(&s).unwrap_or(Value::Null),
        v => v,
    };
    let Some(nodes) = body.get("nodes") else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let push_if_fqn = |out: &mut Vec<String>, node: &Value| {
        if let Some(name) = node.get("name").and_then(|v| v.as_str()) {
            if name.contains('.') {
                out.push(name.to_string());
            }
        }
    };
    match nodes {
        Value::Object(map) => {
            for v in map.values() {
                push_if_fqn(&mut out, v);
            }
        }
        Value::Array(arr) => {
            for v in arr {
                push_if_fqn(&mut out, v);
            }
        }
        _ => {}
    }
    out
}
