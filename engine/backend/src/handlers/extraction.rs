//! ``ExtractPipeline`` handler — replaces
//! ``dorian/event/handlers/extraction.py::handle_extract_pipeline``.
//!
//! Wire flow:
//!
//! 1. Validate envelope (uid, session present; payload has `code`).
//! 2. Load JSON-spec extraction rules from
//!    ``doc_extraction_rule_versions`` (postgres docstore) — the
//!    user's saved customisations stack on top of the curated
//!    ruleset on disk.
//! 3. Parse the source code via the ``extractor`` crate
//!    (tree-sitter → ``Model`` + match-rewrite-fixpoint).
//! 4. Persist the extraction record (postgres ``doc_extractions`` —
//!    rolled into the same docstore as everything else).
//! 5. Set the per-session ``extraction:active`` redis key so
//!    downstream tools can resolve the user's current extraction.
//! 6. XADD the projected payload onto the user's WS stream so the
//!    canvas renders the new pipeline. The wire format mirrors what
//!    the python handler emitted (a flat ``{nodes, edges}`` shape)
//!    so the frontend doesn't have to migrate in lockstep — the
//!    Model→wire projection lives ONLY here, never in storage or
//!    handlers downstream of this one.
//! 7. Emit ``ExtractionDone`` so observability + downstream rust
//!    handlers see the extraction landed.
//!
//! The python rule-suggestion / accept / save-rules / cancel-suggest
//! / mcp-token handlers stay python for now — those are
//! application-specific (LLM-driven, embedding retrieval). They'll
//! migrate when their dependencies port. The extractor's CORE path
//! lives in rust as of this commit.

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::{json, Value};
use std::path::PathBuf;
use uuid::Uuid;

use extractor::engine::{extract, load_rules_dir};
use extractor::model::{ActorKind, Model};
use extractor::rule::RuleSpec;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 100_000;

pub fn register(r: &mut Registry) {
    r.register("ExtractPipeline", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle(state, event))
    });
}

async fn handle(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let uid = match event.uid.as_deref() {
        Some(u) if !u.is_empty() => u.to_string(),
        _ => {
            invalid_envelope(state, "missing uid").await;
            return Ok(());
        }
    };
    let session = match event.session.as_deref() {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => {
            invalid_envelope(state, "missing session").await;
            return Ok(());
        }
    };

    let code = event
        .payload
        .get("code")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if code.is_empty() {
        ws_error(state, &uid, &session, "Missing code").await;
        return Ok(());
    }
    let language = event
        .payload
        .get("language")
        .and_then(|v| v.as_str())
        .unwrap_or("python")
        .to_string();
    let filename = event
        .payload
        .get("filename")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    if language != "python" {
        ws_error(
            state,
            &uid,
            &session,
            &format!("rust extractor only supports python (got {language:?})"),
        )
        .await;
        return Ok(());
    }

    let extraction_id = Uuid::new_v4().simple().to_string();
    let _ = aemit(
        state,
        Lane::Bg,
        EmitPayload::new(
            "ExtractionStarted",
            "rust-backend.handlers.extraction",
            json!({
                "uid": uid,
                "session": session,
                "filename": filename,
                "code_len": code.len(),
            }),
        )
        .with_envelope(
            event.request_id.clone(),
            Some(uid.clone()),
            Some(session.clone()),
        ),
    )
    .await;

    // ── Load rules ─────────────────────────────────────────────
    let mut rules = Vec::new();
    let rules_dir = state.config.extractor_rules_dir.as_str();
    if !rules_dir.is_empty() {
        match load_rules_dir(PathBuf::from(rules_dir)) {
            Ok(loaded) => rules.extend(loaded),
            Err(err) => {
                tracing::warn!(%err, dir = rules_dir, "extraction: curated rules dir load failed");
            }
        }
    }
    let custom_count = match load_user_json_specs(state, &uid).await {
        Ok(specs) => {
            let n = specs.len();
            for spec in specs {
                rules.push(spec.compile());
            }
            n
        }
        Err(err) => {
            tracing::warn!(%err, "extraction: user json_specs load failed");
            0
        }
    };
    let _ = aemit(
        state,
        Lane::Bg,
        EmitPayload::new(
            "ExtractionRulesLoaded",
            "rust-backend.handlers.extraction",
            json!({
                "count": rules.len(),
                "json_custom_count": custom_count,
            }),
        )
        .with_envelope(
            event.request_id.clone(),
            Some(uid.clone()),
            Some(session.clone()),
        ),
    )
    .await;

    // ── Run the extractor ──────────────────────────────────────
    let model = match extract(&code, rules) {
        Ok(m) => m,
        Err(err) => {
            let msg = format!("Parse error: {err}");
            ws_error(state, &uid, &session, &msg).await;
            let _ = aemit(
                state,
                Lane::Bg,
                EmitPayload::new(
                    "ExtractionParseError",
                    "rust-backend.handlers.extraction",
                    json!({ "error": err.to_string() }),
                )
                .with_envelope(
                    event.request_id.clone(),
                    Some(uid.clone()),
                    Some(session.clone()),
                ),
            )
            .await;
            return Ok(());
        }
    };

    // ── Persist + active-key + WS push ─────────────────────────
    persist_extraction(state, &extraction_id, &uid, &session, &code, filename.as_deref(), &model).await;

    let mut conn = state.redis.clone();
    let _: redis::RedisResult<()> = conn
        .set(keys::active_extraction(&session), extraction_id.clone())
        .await;

    let stream = keys::ws_stream(&uid, &session);
    let result = project_model_to_wire(&model, &extraction_id);
    let payload = serde_json::to_string(&result).unwrap_or_else(|_| "{}".to_string());
    let _: redis::RedisResult<String> = conn
        .xadd_maxlen(
            &stream,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[
                ("event", "extraction/result"),
                ("value", payload.as_str()),
                ("type", "json"),
            ],
        )
        .await;

    let _ = aemit(
        state,
        Lane::Bg,
        EmitPayload::new(
            "ExtractionDone",
            "rust-backend.handlers.extraction",
            json!({
                "extraction_id": extraction_id,
                "actors": model.root.actors.len(),
                "relations": model.root.relations.len(),
            }),
        )
        .with_envelope(
            event.request_id.clone(),
            Some(uid),
            Some(session),
        ),
    )
    .await;
    Ok(())
}

/// Load the user's saved JSON-spec rules from
/// ``doc_extraction_rule_versions``. Reads the latest valid
/// ``json_specs`` document for the user; returns an empty list
/// when none exists or postgres is unreachable.
async fn load_user_json_specs(state: &AppState, uid: &str) -> Result<Vec<RuleSpec>> {
    let pool = match state.pg.as_ref() {
        Some(p) => p,
        None => return Ok(Vec::new()),
    };
    let client = pool.get().await?;
    let row = client
        .query_opt(
            "SELECT data->>'content' AS content \
             FROM doc_extraction_rule_versions \
             WHERE data->>'uid' = $1 \
               AND data->>'isValid' = 'true' \
               AND data->>'format' = 'json_specs' \
             ORDER BY created_at DESC LIMIT 1",
            &[&uid],
        )
        .await?;
    let content = match row.and_then(|r| r.try_get::<_, Option<String>>("content").ok().flatten()) {
        Some(s) if !s.is_empty() => s,
        _ => return Ok(Vec::new()),
    };
    let specs: Vec<RuleSpec> = match serde_json::from_str(&content) {
        Ok(v) => v,
        Err(err) => {
            tracing::warn!(%err, "extraction: user json_specs parse failed");
            return Ok(Vec::new());
        }
    };
    Ok(specs)
}

/// Insert one row into ``doc_extractions`` so the rule-suggestion
/// flow + the regression-replay corpus can resolve the user's
/// recent extractions by id. Best-effort — postgres unavailable
/// means we just don't persist.
async fn persist_extraction(
    state: &AppState,
    extraction_id: &str,
    uid: &str,
    session: &str,
    code: &str,
    filename: Option<&str>,
    model: &Model,
) {
    let pool = match state.pg.as_ref() {
        Some(p) => p,
        None => return,
    };
    let client = match pool.get().await {
        Ok(c) => c,
        Err(err) => {
            tracing::warn!(%err, "extraction: pg client unavailable for persist");
            return;
        }
    };
    let model_json = serde_json::to_value(model).unwrap_or(Value::Null);
    let doc = json!({
        "_id": extraction_id,
        "uid": uid,
        "session": session,
        "filename": filename,
        "code": code,
        "language": "python",
        "model": model_json,
    });
    let doc_pg = tokio_postgres::types::Json(doc);
    if let Err(err) = client
        .execute(
            "INSERT INTO doc_extractions (id, data, created_at, updated_at) \
             VALUES ($1, $2, NOW(), NOW()) \
             ON CONFLICT (id) DO UPDATE SET \
                data = EXCLUDED.data, \
                updated_at = NOW()",
            &[&extraction_id, &doc_pg],
        )
        .await
    {
        tracing::warn!(%err, "extraction: doc_extractions upsert failed");
    }
}

/// Project a [`Model`] into the WS payload shape the existing
/// frontend expects: ``{uuid, nodes: {id: {type, name, ...}},
/// edges: [{source, destination, position, output}], extractionId}``.
///
/// This projection lives ONLY here — the storage layer holds the
/// full Model, downstream rust orchestration reads the Model. The
/// projection is the seam at the WS boundary so the React canvas
/// doesn't have to migrate in lockstep with this PR.
fn project_model_to_wire(model: &Model, extraction_id: &str) -> Value {
    let mut nodes_obj = serde_json::Map::new();
    for actor in &model.root.actors {
        let kind_str = match actor.kind {
            ActorKind::Operator => "Operator",
            ActorKind::Snippet => "Snippet",
            ActorKind::Parameter => "Parameter",
            ActorKind::Composite => "Composite",
            ActorKind::ParserLeaf => "Node",
        };
        let mut entry = serde_json::Map::new();
        entry.insert("type".into(), Value::String(kind_str.to_string()));
        let display_name = if !actor.name.is_empty() {
            actor.name.clone()
        } else if !actor.parser.text.is_empty() {
            actor.parser.text.clone()
        } else {
            actor.id.clone()
        };
        entry.insert("name".into(), Value::String(display_name));
        if matches!(actor.kind, ActorKind::Parameter) {
            if let Some(p) = actor.parameters.first() {
                entry.insert("value".into(), Value::String(p.value.clone()));
                entry.insert("dtype".into(), Value::String(p.dtype.clone()));
            }
        }
        if matches!(actor.kind, ActorKind::Snippet) {
            entry.insert("code".into(), Value::String(actor.code.clone()));
            entry.insert("language".into(), Value::String(actor.language.clone()));
        }
        let outputs: Vec<Value> = actor
            .outputs
            .iter()
            .map(|p| json!({ "name": p.name }))
            .collect();
        let inputs: Vec<Value> = actor
            .inputs
            .iter()
            .map(|p| json!({ "name": p.name }))
            .collect();
        entry.insert("outputs".into(), Value::Array(outputs));
        entry.insert("inputs".into(), Value::Array(inputs));
        nodes_obj.insert(actor.id.clone(), Value::Object(entry));
    }

    // Each Relation flattens into one or more legacy edges. For
    // point-to-point relations (the dominant shape) it's exactly
    // one. The wire format's `position` carries the destination
    // port name; `output` is the integer index of the source port
    // in the producing actor's output list (legacy compat for the
    // canvas's slice keying).
    let mut edges: Vec<Value> = Vec::new();
    for rel in &model.root.relations {
        for src in &rel.sources {
            for dst in &rel.destinations {
                let output_idx = model
                    .root
                    .actor(&src.actor)
                    .and_then(|a| a.outputs.iter().position(|p| p.name == src.port))
                    .unwrap_or(0);
                edges.push(json!({
                    "source": src.actor,
                    "destination": dst.actor,
                    "position": dst.port,
                    "output": output_idx,
                }));
            }
        }
    }

    json!({
        "uuid": Uuid::new_v4().simple().to_string(),
        "extractionId": extraction_id,
        "nodes": Value::Object(nodes_obj),
        "edges": edges,
    })
}

async fn invalid_envelope(state: &AppState, reason: &str) {
    let _ = aemit(
        state,
        Lane::Bg,
        EmitPayload::new(
            "ExtractionEnvelopeInvalid",
            "rust-backend.handlers.extraction",
            json!({ "reason": reason }),
        ),
    )
    .await;
}

async fn ws_error(state: &AppState, uid: &str, session: &str, msg: &str) {
    let mut conn = state.redis.clone();
    let stream = keys::ws_stream(uid, session);
    let payload = json!({ "error": msg }).to_string();
    let _: redis::RedisResult<String> = conn
        .xadd_maxlen(
            &stream,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[
                ("event", "extraction/error"),
                ("value", payload.as_str()),
                ("type", "json"),
            ],
        )
        .await;
}
