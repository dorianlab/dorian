//! Pipeline-event handlers. Replaces
//! ``dorian/event/handlers/pipeline.py``.
//!
//! Refactor over the python design:
//!
//!   * The python had two ``handle_pipeline_*`` functions that
//!     were entirely no-op (``pipeline_created``,
//!     ``pipeline_updated``) — registered against events but
//!     immediately returning. They're dead code from a debounce-
//!     style design that never landed; not ported.
//!   * The python had four ``handle_pipeline_*_clicked`` /
//!     ``handle_pipeline_*_restored`` functions that re-emitted the
//!     same event-type they subscribed to. That's a feedback loop
//!     unless the eventbus has dedup-by-request-id (it does, via
//!     ``processed`` tracking) — but the loop is structurally
//!     fragile. Not ported; their downstream consumers should
//!     subscribe to the original event directly.
//!   * The python ``handle_pipeline_run_clicked`` bridges into the
//!     pipeline runner (``dorian.pipeline.execution``). That entry
//!     point is python-side because the runner is python (Dask + the
//!     operator runtime). Stays in python until the runner itself
//!     ports to rust.
//!
//! What we port: the three handlers that actually mutate
//! ``session:meta`` — ``PipelineSaved``, ``PipelineRemoved``,
//! ``PipelineCanvasChanged``.

use anyhow::Result;
use serde_json::{json, Value};

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::session::{with_session_meta, SessionMetaOutcome};
use crate::state::AppState;

pub fn register(r: &mut Registry) {
    r.register("PipelineSaved", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_saved(state, event))
    });
    r.register("PipelineRemoved", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_removed(state, event))
    });
    r.register("PipelineCanvasChanged", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_canvas_changed(state, event))
    });
    // PipelineExists / PipelineImported → read the file at ``payload.fpath``,
    // initialise pipelineHistory in session meta, push state/pipeline to the
    // SPA stream, emit PipelineRetrieved. python ``read_pipeline`` ported
    // verbatim except the file read is tokio-native (no asyncio.to_thread
    // bridge) and the python downstream (start_debugging) keeps subscribing
    // to PipelineRetrieved as a downstream job.
    r.register("PipelineExists", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_read_pipeline(state, event))
    });
    r.register("PipelineImported", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_read_pipeline(state, event))
    });
}

async fn handle_read_pipeline(state: &AppState, event: &EventEnvelope) -> Result<()> {
    use redis::AsyncCommands;
    use redis::streams::StreamMaxlen;

    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let uid = payload
        .get("uid")
        .and_then(|v| v.as_str())
        .map(String::from)
        .or_else(|| event.uid.clone())
        .filter(|s| !s.is_empty());
    let session = payload
        .get("session")
        .and_then(|v| v.as_str())
        .map(String::from)
        .or_else(|| event.session.clone())
        .filter(|s| !s.is_empty());
    let fpath = payload
        .get("fpath")
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session), Some(fpath)) = (uid, session, fpath) else {
        return Ok(());
    };

    let mut conn = state.redis.clone();
    let meta_key = crate::keys::session_meta(&session);
    let raw: Option<String> = conn.get(&meta_key).await.ok().flatten();
    let Some(raw) = raw else {
        let payload = EmitPayload::new(
            "SessionNotFound",
            "rust-backend.handlers.pipeline.read_pipeline",
            json!({"uid": uid, "session": session}),
        )
        .with_envelope(event.request_id.clone(), Some(uid), Some(session));
        aemit(state, Lane::Bg, payload).await?;
        return Ok(());
    };

    let pipeline_text = match tokio::fs::read_to_string(&fpath).await {
        Ok(s) => s,
        Err(_err) => {
            let payload = EmitPayload::new(
                "PipelineFileNotFound",
                "rust-backend.handlers.pipeline.read_pipeline",
                json!({"fpath": fpath}),
            )
            .with_envelope(event.request_id.clone(), Some(uid), Some(session));
            aemit(state, Lane::Bg, payload).await?;
            return Ok(());
        }
    };

    let mut meta: Value = serde_json::from_str(&raw)?;
    let pipeline_id = uuid::Uuid::new_v4().to_string();
    let now = chrono::Utc::now().to_rfc3339();
    let pipeline_version = json!({
        "id": pipeline_id,
        "parentPipelineId": session,
        "createdAt": now,
        "message": "Imported",
        "pipeline": pipeline_text,
    });
    let pipeline_history = json!({
        "uuid": session,
        "headId": pipeline_id,
        "pipelines": [pipeline_version],
    });
    if let Some(obj) = meta.as_object_mut() {
        obj.insert("pipeline".into(), pipeline_version.clone());
        obj.insert("pipelineHistory".into(), pipeline_history.clone());
    }

    // Persist meta + state/pipeline xadd in a non-blocking pair.
    let meta_str = serde_json::to_string(&meta)?;
    let _: redis::RedisResult<()> = conn.set(&meta_key, meta_str).await;

    let stream = crate::keys::ws_stream(&uid, &session);
    let history_str = serde_json::to_string(&pipeline_history)?;
    let _: redis::RedisResult<String> = conn
        .xadd_maxlen(
            &stream,
            StreamMaxlen::Approx(100_000),
            "*",
            &[
                ("event", "state/pipeline"),
                ("uid", uid.as_str()),
                ("session", session.as_str()),
                ("value", history_str.as_str()),
                ("type", "json"),
            ],
        )
        .await;

    // Downstream: start_debugging (still python, subscribed via aemit
    // bridge). Emit PipelineRetrieved with the same payload shape.
    let retrieved_payload = json!({
        "event": "state/pipeline",
        "uid": uid,
        "session": session,
        "value": pipeline_history,
    });
    let payload = EmitPayload::new(
        "PipelineRetrieved",
        "rust-backend.handlers.pipeline.read_pipeline",
        retrieved_payload,
    )
    .with_envelope(event.request_id.clone(), Some(uid), Some(session));
    aemit(state, Lane::Bg, payload).await?;

    Ok(())
}

async fn handle_saved(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let session = match event.session.clone() {
        Some(s) if !s.is_empty() => s,
        _ => return Ok(()),
    };
    let head_id = event
        .payload
        .get("headId")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let Some(head_id) = head_id else {
        // Re-emitted completion or malformed — same as python.
        return Ok(());
    };

    let pipelines: Vec<Value> = event
        .payload
        .get("pipelines")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let uuid_field = event.payload.get("uuid").cloned().unwrap_or(Value::Null);

    let head_pipeline = pipelines
        .iter()
        .find(|p| p.get("id").and_then(|v| v.as_str()) == Some(head_id.as_str()))
        .cloned()
        .or_else(|| pipelines.last().cloned());
    let Some(head_pipeline) = head_pipeline else {
        let payload = EmitPayload::new(
            "PipelineSaveError",
            "rust-backend.handlers.pipeline.saved",
            json!({
                "source": "handlers.pipeline.handle_pipeline_saved",
                "error": "missing_head_pipeline",
            }),
        )
        .with_envelope(
            event.request_id.clone(),
            event.uid.clone(),
            Some(session),
        );
        aemit(state, Lane::Bg, payload).await?;
        return Ok(());
    };

    let outcome = with_session_meta(state, &session, |meta| {
        let mut data = meta.data.clone();
        let is_new = meta.is_new;
        async move {
            if is_new {
                return Ok(None);
            }
            if let Some(obj) = data.as_object_mut() {
                obj.insert(
                    "pipelineHistory".to_string(),
                    json!({
                        "uuid": uuid_field,
                        "headId": head_id,
                        "pipelines": pipelines,
                    }),
                );
                obj.insert("pipeline".to_string(), head_pipeline);
            }
            Ok(Some(data))
        }
    })
    .await?;

    if matches!(outcome, SessionMetaOutcome::Missing) {
        emit_session_not_found(
            state,
            event,
            &session,
            "handlers.pipeline.handle_pipeline_saved",
        )
        .await?;
    }
    Ok(())
}

async fn handle_removed(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let session = match event.session.clone() {
        Some(s) if !s.is_empty() => s,
        _ => return Ok(()),
    };
    let outcome = with_session_meta(state, &session, |meta| {
        let mut data = meta.data.clone();
        let is_new = meta.is_new;
        async move {
            if is_new {
                return Ok(None);
            }
            if let Some(obj) = data.as_object_mut() {
                obj.insert("pipelineHistory".to_string(), Value::Object(Default::default()));
                obj.insert("pipeline".to_string(), Value::Object(Default::default()));
                // ``Previously Unseen`` / ``Atomic Changes`` are
                // pipeline-relative — they can't be computed without
                // a pipeline reference. When the user clears the
                // pipeline, drop the auto-flipped pipeline-mode
                // objectives so the next seed re-picks SCRATCH
                // defaults. Custom selections are preserved (only
                // wipe when the previous mode was the auto-flip).
                let mode = obj
                    .get("objectiveMode")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if mode == "pipeline_default" {
                    obj.insert("rankingObjectives".to_string(), Value::Array(Vec::new()));
                    obj.insert(
                        "objectiveMode".to_string(),
                        Value::String("scratch_default".to_string()),
                    );
                }
            }
            Ok(Some(data))
        }
    })
    .await?;

    if matches!(outcome, SessionMetaOutcome::Missing) {
        emit_session_not_found(
            state,
            event,
            &session,
            "handlers.pipeline.handle_pipeline_removed",
        )
        .await?;
    }
    // Note: ``recommendation::handle_pipeline_objectives_revert`` also
    // subscribes to PipelineRemoved and pushes the SCRATCH_DEFAULTS
    // pair to the SPA stream. We just cleared rankingObjectives above
    // so that handler picks the fresh defaults instead of seeing the
    // stale pipeline-mode list.
    Ok(())
}

async fn handle_canvas_changed(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let session = match event.session.clone() {
        Some(s) if !s.is_empty() => s,
        _ => return Ok(()),
    };
    let nodes = event.payload.get("nodes").cloned();
    let edges = event.payload.get("edges").cloned();
    let (Some(nodes), Some(edges)) = (nodes, edges) else {
        // Frontend may emit empty/malformed canvas events — fail silently
        // (same as python; this fires often).
        return Ok(());
    };
    if !nodes.is_object() || !edges.is_array() {
        return Ok(());
    }

    let _ = with_session_meta(state, &session, |meta| {
        let mut data = meta.data.clone();
        let is_new = meta.is_new;
        async move {
            if is_new {
                return Ok(None);
            }
            if let Some(obj) = data.as_object_mut() {
                obj.insert("pipeline".to_string(), json!({"nodes": nodes, "edges": edges}));
            }
            Ok(Some(data))
        }
    })
    .await?;
    // Canvas-changed never emits SessionNotFound — too chatty, the client
    // can fire it before the session is fully provisioned.
    Ok(())
}

async fn emit_session_not_found(
    state: &AppState,
    event: &EventEnvelope,
    session: &str,
    source: &str,
) -> Result<()> {
    let payload = EmitPayload::new(
        "SessionNotFound",
        source,
        json!({"session": session, "uid": event.uid.clone()}),
    )
    .with_envelope(
        event.request_id.clone(),
        event.uid.clone(),
        Some(session.to_string()),
    );
    aemit(state, Lane::Bg, payload).await?;
    Ok(())
}

