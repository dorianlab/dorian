//! Recommendation interaction handlers — replaces the redis-I/O slice
//! of ``dorian/event/handlers/recommendations.py``. The orchestration
//! that fires here is pure I/O (interaction log update + selected
//! pipeline save + objective-default switch); the heavy
//! ``suggest_with_status`` call stays subscribed in python and runs as
//! a downstream job until the recommendation engine itself ports.
//!
//! Contract preserved verbatim:
//!
//!   * ``session:{session}:recommendations:interactions`` — JSON dict
//!     with ``upvoted/downvoted/selected/suggested`` lists. Rust
//!     appends one id per event.
//!   * ``RecommendationPipelineSaved`` — emitted from selected so
//!     ``risk_scope::sync_canvas_operators`` (already rust) refreshes
//!     the canvas operator SET against the new pipeline.
//!   * ``handle_pipeline_objectives_switch`` — switches to
//!     ``PIPELINE_DEFAULT_NAMES`` unless the user explicitly chose
//!     ``objectiveMode=custom``. Mirrors python
//!     ``set_pipeline_default_ranking`` exactly.
//!
//! KD/BKTree note: the rust ExperimentStore at
//! ``engine/optimizer/src/recommendation/store.rs`` is already loaded
//! by the rust-backend at startup — every objective scoring call from
//! here onward should resolve through that store, not the python
//! ``dorian/experiment/store.py`` (which becomes vestigial once
//! ``attempt_recommendations`` ports next).

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::{json, Map, Value};

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::session::with_session_meta;
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 100_000;

/// Ranking objectives to pre-select once a pipeline lands in the
/// session. Mirrors ``PIPELINE_DEFAULT_NAMES`` in
/// ``dorian/event/helpers/lifecycle.py`` — keep in sync if the
/// curated default list shifts.
const PIPELINE_DEFAULT_NAMES: &[&str] = &[
    "Previously Unseen",
    "Atomic Changes",
];

/// Ranking objectives to revert to when the user clears the active
/// pipeline. ``Previously Unseen`` / ``Atomic Changes`` are pipeline-
/// relative and can't be evaluated without one — leaving them
/// selected after a remove silently breaks the recommender. Mirrors
/// ``SCRATCH_DEFAULT_NAMES`` in
/// ``dorian/event/helpers/lifecycle.py``.
const SCRATCH_DEFAULT_NAMES: &[&str] = &[
    "Good Performance On Similar Data",
    "Good General Performance",
];

pub fn register(r: &mut Registry) {
    r.register("PipelineRecommendationSelected", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_interaction(state, event, "selected"))
    });
    r.register("PipelineRecommendationUpvoted", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_interaction(state, event, "upvoted"))
    });
    r.register("PipelineRecommendationDownvoted", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_interaction(state, event, "downvoted"))
    });
    r.register("RecommendationPipelineSaved", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_pipeline_objectives_switch(state, event))
    });
    r.register("PipelineImported", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_pipeline_objectives_switch(state, event))
    });
    // Reverse direction: when the active pipeline is cleared, the
    // pipeline-mode objectives (``Previously Unseen`` /
    // ``Atomic Changes``) become unevaluatable. Revert to scratch
    // defaults so the SPA shows a coherent selection.
    r.register("PipelineRemoved", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_pipeline_objectives_revert(state, event))
    });
    // Frontend's "Switch to pipeline defaults" button (shown after a
    // ``state/objectives/conflict-prompt`` arrives) emits this. The
    // handler completes the switch the original handler skipped to
    // avoid silently overwriting custom objectives.
    r.register(
        "RankingObjectivesAcceptPipelineDefaults",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_accept_pipeline_defaults(state, event))
        },
    );
}

/// Append one pipeline_id to the per-session interaction log under
/// ``kind`` (``selected`` / ``upvoted`` / ``downvoted``). Idempotent
/// from a redis-perspective: the entry list is overwritten with the
/// extended copy on every call (matches python ``_append_interactions``).
async fn record_interaction(
    conn: &mut redis::aio::ConnectionManager,
    session: &str,
    kind: &str,
    pipeline_id: &str,
) -> Result<()> {
    let key = format!("session:{session}:recommendations:interactions");
    let raw: Option<String> = conn.get(&key).await.ok().flatten();
    let mut data: Map<String, Value> = match raw {
        Some(s) => serde_json::from_str(&s).unwrap_or_default(),
        None => Map::new(),
    };
    for default_kind in &["upvoted", "downvoted", "selected", "suggested"] {
        data.entry(default_kind.to_string())
            .or_insert_with(|| Value::Array(Vec::new()));
    }
    if let Some(Value::Array(arr)) = data.get_mut(kind) {
        arr.push(Value::String(pipeline_id.to_string()));
    } else {
        data.insert(kind.into(), Value::Array(vec![Value::String(pipeline_id.to_string())]));
    }
    let serialised = serde_json::to_string(&Value::Object(data))?;
    let _: redis::RedisResult<()> = conn.set(&key, serialised).await;
    Ok(())
}

async fn handle_interaction(
    state: &AppState,
    event: &EventEnvelope,
    kind: &str,
) -> Result<()> {
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
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };

    // The frontend sometimes wraps the actual record under "payload";
    // mirror python's flexibility (``payload = event.data.get("payload",
    // event.data)``).
    let inner = payload
        .get("payload")
        .and_then(|v| v.as_object())
        .unwrap_or(payload);
    let pipeline_id = payload
        .get("pipelineId")
        .and_then(|v| v.as_str())
        .or_else(|| inner.get("pipelineId").and_then(|v| v.as_str()))
        .map(String::from)
        .filter(|s| !s.is_empty());

    let mut conn = state.redis.clone();
    if let Some(pid) = pipeline_id.as_deref() {
        if let Err(err) = record_interaction(&mut conn, &session, kind, pid).await {
            tracing::warn!(%err, kind, "record_interaction failed (best-effort)");
        }
    }

    // For ``selected``: persist the chosen pipeline body into session
    // meta so mitigation rewrites + execution can find it. Then emit
    // RecommendationPipelineSaved so risk_scope (canvas SET) +
    // handle_pipeline_objectives_switch (defaults flip) react.
    if kind == "selected" {
        let pipeline_body = inner.get("pipeline").cloned();
        if let Some(pipeline_body) = pipeline_body {
            let pipeline_for_meta = pipeline_body.clone();
            let _ = with_session_meta(state, &session, |meta| {
                let mut data = meta.data.clone();
                let is_new = meta.is_new;
                let pipeline_for_meta = pipeline_for_meta.clone();
                async move {
                    if is_new {
                        return Ok(None);
                    }
                    if let Some(obj) = data.as_object_mut() {
                        obj.insert("pipeline".into(), pipeline_for_meta);
                    }
                    Ok(Some(data))
                }
            })
            .await;

            let pid = pipeline_id.clone().unwrap_or_default();
            let payload = EmitPayload::new(
                "RecommendationPipelineSaved",
                "rust-backend.handlers.recommendation.selected",
                json!({
                    "pipeline_id": pid,
                    "session": session,
                    "uid": uid,
                    "value": pipeline_body,
                }),
            )
            .with_envelope(
                event.request_id.clone(),
                Some(uid.clone()),
                Some(session.clone()),
            );
            aemit(state, Lane::Bg, payload).await?;
        }
    }

    Ok(())
}

/// On any event that signals "pipeline now lives in this session"
/// (RecommendationPipelineSaved / PipelineImported), reconcile the
/// session's ranking objectives against the pipeline defaults.
///
/// Three cases:
///
///   1. **`objectiveMode == "scratch_default"`** (or unset): user has
///      not customized — auto-flip to PIPELINE_DEFAULTS, push
///      ``state/objectives/selected`` to the SPA. Same shape as
///      ``set_pipeline_default_ranking`` without the force branch.
///
///   2. **`objectiveMode == "pipeline_default"`** already: nothing to
///      do (already on pipeline defaults).
///
///   3. **`objectiveMode == "custom"`**: user picked their own
///      ranking objectives BEFORE the pipeline landed. Auto-overwrite
///      would silently discard their choice. Instead, emit
///      ``state/objectives/conflict-prompt`` so the SPA renders a
///      reconciliation dialog. The user resolves via
///      ``RankingObjectivesAcceptPipelineDefaults`` (switch) or by
///      doing nothing (keep custom).
async fn handle_pipeline_objectives_switch(
    state: &AppState,
    event: &EventEnvelope,
) -> Result<()> {
    let uid = event
        .uid
        .clone()
        .or_else(|| {
            event
                .payload
                .get("uid")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty());
    let session = event
        .session
        .clone()
        .or_else(|| {
            event
                .payload
                .get("session")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };

    let name_to_uuid = load_objective_name_to_uuid(state).await;
    if name_to_uuid.is_empty() {
        return Ok(());
    }

    // Build the pipeline-default list once.
    let mut new_ranking: Vec<Value> = Vec::new();
    let mut selected_pairs: Vec<String> = Vec::new();
    for &name in PIPELINE_DEFAULT_NAMES {
        if let Some(uuid) = name_to_uuid.get(name) {
            new_ranking.push(json!({"uuid": uuid, "name": name}));
            selected_pairs.push(format!("{}:{}", uuid, name));
        }
    }
    if new_ranking.is_empty() {
        return Ok(());
    }

    // Read meta first to decide which branch to take. Reading outside
    // with_session_meta keeps the conflict-prompt path lock-free.
    let mut conn = state.redis.clone();
    let raw: Option<String> = conn.get(keys::session_meta(&session)).await.ok().flatten();
    let Some(raw) = raw else {
        return Ok(());
    };
    let meta: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Ok(()),
    };
    let mode = meta
        .get("objectiveMode")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let current_ranking: Vec<Value> = meta
        .get("rankingObjectives")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    if mode == "custom" {
        // ── Conflict path: ask the user, don't silently overwrite ──
        //
        // The SPA renders a reconcile dialog where the user picks any
        // subset of (current ∪ suggested) — they're not constrained to
        // an "accept all defaults" / "keep all custom" binary.  Their
        // final composition is emitted as a normal
        // ``RankingObjectivesChanged`` (existing handler) so the
        // session_meta tx stays in one place. The
        // ``RankingObjectivesAcceptPipelineDefaults`` event is kept as
        // a convenience shortcut for the common "yes, use suggested"
        // case but the user is never forced through it.
        //
        // The payload pre-computes the set diff so the SPA can group
        // items into ``shared / current_only / suggested_only`` and
        // pre-tick the appropriate checkboxes:
        //
        //   * ``shared``         — already in the user's list AND in the
        //                          pipeline defaults; default-checked.
        //   * ``current_only``   — only in the user's list; default-checked.
        //   * ``suggested_only`` — only in the pipeline defaults;
        //                          default-unchecked (user opts in).
        let current_names: std::collections::HashSet<String> = current_ranking
            .iter()
            .filter_map(|v| v.get("name").and_then(|n| n.as_str()).map(String::from))
            .collect();
        let suggested_names: std::collections::HashSet<String> = PIPELINE_DEFAULT_NAMES
            .iter()
            .map(|s| s.to_string())
            .collect();
        if current_names == suggested_names {
            // Custom list already matches pipeline defaults — no
            // conflict to surface.
            return Ok(());
        }

        // Build {name → uuid+name dict} for every objective that
        // appears in either list, so the SPA can render uniformly.
        let mut entry_for = |name: &str| -> Value {
            let uuid = current_ranking
                .iter()
                .find(|v| v.get("name").and_then(|n| n.as_str()) == Some(name))
                .and_then(|v| v.get("uuid").and_then(|u| u.as_str()))
                .map(String::from)
                .or_else(|| name_to_uuid.get(name).cloned())
                .unwrap_or_else(|| stable_uuid(name));
            json!({"uuid": uuid, "name": name})
        };
        let shared: Vec<Value> = current_names
            .iter()
            .filter(|n| suggested_names.contains(n.as_str()))
            .map(|n| entry_for(n))
            .collect();
        let current_only: Vec<Value> = current_names
            .iter()
            .filter(|n| !suggested_names.contains(n.as_str()))
            .map(|n| entry_for(n))
            .collect();
        let suggested_only: Vec<Value> = suggested_names
            .iter()
            .filter(|n| !current_names.contains(n.as_str()))
            .map(|n| entry_for(n))
            .collect();

        let prompt_payload = json!({
            "current": current_ranking,
            "suggested": new_ranking,
            "shared": shared,
            "current_only": current_only,
            "suggested_only": suggested_only,
            "trigger": event.event_type,
            // Hint to the SPA that the user can compose any list,
            // not just accept/reject — the UI wires this through
            // ``RankingObjectivesChanged``.
            "resolve_via": "RankingObjectivesChanged",
        });
        let stream = keys::ws_stream(&uid, &session);
        let prompt_str = serde_json::to_string(&prompt_payload).unwrap_or_default();
        let _: redis::RedisResult<String> = conn
            .xadd_maxlen(
                &stream,
                StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
                "*",
                &[
                    ("event", "state/objectives/conflict-prompt"),
                    ("value", prompt_str.as_str()),
                    ("type", "json"),
                ],
            )
            .await;
        let payload = EmitPayload::new(
            "RankingObjectivesConflictRaised",
            "rust-backend.handlers.recommendation.objectives_switch",
            prompt_payload,
        )
        .with_envelope(event.request_id.clone(), Some(uid.clone()), Some(session.clone()));
        aemit(state, Lane::Bg, payload).await?;
        return Ok(());
    }

    // ── Auto-switch path (scratch_default or unset). ──
    let new_ranking_for_meta = new_ranking.clone();
    let switched = with_session_meta(state, &session, |meta| {
        let mut data = meta.data.clone();
        let is_new = meta.is_new;
        let new_ranking = new_ranking_for_meta.clone();
        async move {
            if is_new {
                return Ok(None);
            }
            let Some(obj) = data.as_object_mut() else {
                return Ok(None);
            };
            obj.insert("rankingObjectives".into(), Value::Array(new_ranking));
            obj.insert(
                "objectiveMode".into(),
                Value::String("pipeline_default".into()),
            );
            Ok(Some(data))
        }
    })
    .await?;

    if matches!(switched, crate::session::SessionMetaOutcome::Updated) {
        let stream = keys::ws_stream(&uid, &session);
        let value = selected_pairs.join(",");
        let _: redis::RedisResult<String> = conn
            .xadd_maxlen(
                &stream,
                StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
                "*",
                &[
                    ("event", "state/objectives/selected"),
                    ("value", value.as_str()),
                    ("type", "list"),
                ],
            )
            .await;
    }
    Ok(())
}

/// Counterpart to ``handle_pipeline_objectives_switch``. When the
/// user clears the active pipeline (``PipelineRemoved``), the
/// pipeline-relative objectives (``Previously Unseen`` /
/// ``Atomic Changes``) become unevaluatable. Revert to
/// ``SCRATCH_DEFAULT_NAMES`` and push the new selection to the SPA
/// — but only when the previous mode was the auto-flipped
/// ``pipeline_default``. Custom selections are preserved.
async fn handle_pipeline_objectives_revert(
    state: &AppState,
    event: &EventEnvelope,
) -> Result<()> {
    let uid = event
        .uid
        .clone()
        .or_else(|| {
            event
                .payload
                .get("uid")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty());
    let session = event
        .session
        .clone()
        .or_else(|| {
            event
                .payload
                .get("session")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };

    // Read meta to confirm the auto-flip is what's currently active.
    // Custom selections (mode == "custom") must NOT be touched.
    let mut conn = state.redis.clone();
    let raw: Option<String> = conn.get(keys::session_meta(&session)).await.ok().flatten();
    let Some(raw) = raw else {
        return Ok(());
    };
    let meta: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Ok(()),
    };
    let mode = meta
        .get("objectiveMode")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    // ``pipeline.rs::handle_removed`` runs first (registered earlier)
    // and resets the meta — at that point ``mode`` is already
    // ``scratch_default`` if it had been auto-flipped, or untouched
    // (``custom`` / unset) if the user picked their own list. Only
    // proceed in the auto-flip-revert case.
    if mode != "scratch_default" {
        return Ok(());
    }

    let name_to_uuid = load_objective_name_to_uuid(state).await;
    let mut new_ranking: Vec<Value> = Vec::new();
    let mut selected_pairs: Vec<String> = Vec::new();
    for &name in SCRATCH_DEFAULT_NAMES {
        if let Some(uuid) = name_to_uuid.get(name) {
            new_ranking.push(json!({"uuid": uuid, "name": name}));
            selected_pairs.push(format!("{}:{}", uuid, name));
        }
    }
    if new_ranking.is_empty() {
        return Ok(());
    }

    let new_ranking_for_meta = new_ranking.clone();
    let switched = with_session_meta(state, &session, |meta| {
        let mut data = meta.data.clone();
        let is_new = meta.is_new;
        let new_ranking = new_ranking_for_meta.clone();
        async move {
            if is_new {
                return Ok(None);
            }
            if let Some(obj) = data.as_object_mut() {
                obj.insert("rankingObjectives".into(), Value::Array(new_ranking));
            }
            Ok(Some(data))
        }
    })
    .await?;

    if matches!(switched, crate::session::SessionMetaOutcome::Updated) {
        let stream = keys::ws_stream(&uid, &session);
        let value = selected_pairs.join(",");
        let _: redis::RedisResult<String> = conn
            .xadd_maxlen(
                &stream,
                StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
                "*",
                &[
                    ("event", "state/objectives/selected"),
                    ("value", value.as_str()),
                    ("type", "list"),
                ],
            )
            .await;
    }
    Ok(())
}

/// Frontend resolves the conflict prompt by emitting
/// ``RankingObjectivesAcceptPipelineDefaults`` (user clicked
/// "Switch") or by doing nothing (user clicked "Keep" / dismissed).
/// This handler completes the switch when invoked.
async fn handle_accept_pipeline_defaults(
    state: &AppState,
    event: &EventEnvelope,
) -> Result<()> {
    let uid = event
        .uid
        .clone()
        .or_else(|| {
            event
                .payload
                .get("uid")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty());
    let session = event
        .session
        .clone()
        .or_else(|| {
            event
                .payload
                .get("session")
                .and_then(|v| v.as_str())
                .map(String::from)
        })
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };

    let name_to_uuid = load_objective_name_to_uuid(state).await;
    let mut new_ranking: Vec<Value> = Vec::new();
    let mut selected_pairs: Vec<String> = Vec::new();
    for &name in PIPELINE_DEFAULT_NAMES {
        if let Some(uuid) = name_to_uuid.get(name) {
            new_ranking.push(json!({"uuid": uuid, "name": name}));
            selected_pairs.push(format!("{}:{}", uuid, name));
        }
    }
    if new_ranking.is_empty() {
        return Ok(());
    }

    let new_ranking_for_meta = new_ranking.clone();
    let switched = with_session_meta(state, &session, |meta| {
        let mut data = meta.data.clone();
        let is_new = meta.is_new;
        let new_ranking = new_ranking_for_meta.clone();
        async move {
            if is_new {
                return Ok(None);
            }
            if let Some(obj) = data.as_object_mut() {
                obj.insert("rankingObjectives".into(), Value::Array(new_ranking));
                obj.insert(
                    "objectiveMode".into(),
                    Value::String("pipeline_default".into()),
                );
            }
            Ok(Some(data))
        }
    })
    .await?;

    if matches!(switched, crate::session::SessionMetaOutcome::Updated) {
        let stream = keys::ws_stream(&uid, &session);
        let mut conn = state.redis.clone();
        let value = selected_pairs.join(",");
        let _: redis::RedisResult<String> = conn
            .xadd_maxlen(
                &stream,
                StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
                "*",
                &[
                    ("event", "state/objectives/selected"),
                    ("value", value.as_str()),
                    ("type", "list"),
                ],
            )
            .await;
    }
    Ok(())
}

async fn load_objective_name_to_uuid(
    state: &AppState,
) -> std::collections::HashMap<String, String> {
    let mut name_to_uuid: std::collections::HashMap<String, String> =
        std::collections::HashMap::new();
    if let Some(pool) = state.pg.as_ref() {
        if let Ok(client) = pool.get().await {
            if let Ok(rows) = client
                .query("SELECT data FROM doc_ranking_objectives", &[])
                .await
            {
                for row in rows {
                    let data: tokio_postgres::types::Json<Value> = row.get(0);
                    if let Some(name) = data.0.get("name").and_then(|v| v.as_str()) {
                        let uuid = data
                            .0
                            .get("uuid")
                            .and_then(|v| v.as_str())
                            .map(String::from)
                            .unwrap_or_else(|| stable_uuid(name));
                        name_to_uuid.insert(name.to_string(), uuid);
                    }
                }
            }
        }
    }
    name_to_uuid
}

fn stable_uuid(name: &str) -> String {
    use uuid::Uuid;
    Uuid::new_v5(&Uuid::NAMESPACE_URL, name.as_bytes())
        .simple()
        .to_string()
}
