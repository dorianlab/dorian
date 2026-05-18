//! Native rust recommendation engine — replaces
//! ``dorian/event/handlers/recommendations.py::attempt_recommendations``
//! and ``_handle_interaction``. Calls the rust scoring engine
//! (``optimizer::recommendation::{score_candidates, ranking::rank}``)
//! directly; the python KDTree/BKTree go vestigial.
//!
//! Subscribes to:
//!
//!   * ``DataExists``, ``DataProfiled``, ``DataScienceTaskSelected``,
//!     ``EvaluationProcedureCommitted``, ``RankingObjectivesCommitted``,
//!     ``RankingObjectiveAdded`` → ``handle_attempt_recommendations``.
//!   * ``PipelineRecommendationSelected/Upvoted/Downvoted`` →
//!     ``handle_recommendation_interaction`` (re-rank slice, after the
//!     redis-I/O slice in ``recommendation.rs`` already ran).
//!
//! Hot-path policy: the orchestration is rust; the score/rank kernels
//! are rust (parallel via rayon for >64-candidate pools). The
//! candidates list is fetched from ``doc_pipelines`` via tokio-
//! postgres in the same task. The python wrappers in
//! ``post_handlers.py::post_attempt_recommendations`` get superseded
//! by this module — those wrappers are retired in
//! ``python_dispatch::register`` once this handler is wired in.

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use optimizer::recommendation::{
    objectives::{
        check_dependencies, create_builtin_objective, create_builtin_objective_with_store,
        extract_operator_names, score_candidates, Candidate, RecommendationContext,
    },
    ExperimentStore,
};
use std::sync::Arc;
use optimizer::ranking::rank;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

/// Default ranking strategy. Mirrors python's
/// ``config.development.recommendation.strategy = "nds_lex"``. Pareto
/// fronts with lex tie-break by user objective order — the right
/// default for the interactive UX.
const RANKING_STRATEGY: &str = "nds_lex";
/// Top-K rendered to the SPA. Matches python ``DEFAULT_LIMIT``.
const DEFAULT_LIMIT: usize = 5;
/// Candidate-pool size pulled from ``doc_pipelines`` for scoring.
/// Matches python ``_RETRIEVAL_POOL`` (1000 by default).
const RETRIEVAL_POOL: i64 = 1000;
/// If a task-filtered random sample yields fewer than this, retry
/// without the task filter so cold sessions still get a pool.
const TASK_FALLBACK_THRESHOLD: usize = 3;
const STREAM_MAXLEN_APPROX: usize = 100_000;
/// Per-session debounce window so a session-init burst (DataExists +
/// DataProfiled + DataScienceTaskSelected back-to-back) collapses
/// into one re-rank instead of three. Matches python.
const DEBOUNCE_S: f64 = 1.0;

pub fn register(r: &mut Registry) {
    for ev in [
        "DataExists",
        "DataProfiled",
        "DataScienceTaskSelected",
        "EvaluationProcedureCommitted",
        "RankingObjectivesCommitted",
        "RankingObjectiveAdded",
        // Reconnect path: re-emit recommendations for the active
        // session so the SPA's feed shows up immediately on a page
        // reload. Python's ``seed_session`` Phase 3 historically
        // owned this path via ``_deferred_recommendations``, but it
        // depends on the python eventbus worker pool which has a
        // recurring silent-stall pattern. Subscribing rust here too
        // makes the recommendation feed independent of the python
        // bus's health. The session-level debounce
        // (``should_debounce``) prevents InitSession + DataExists
        // from double-firing when a reconnect coincides with a
        // dataset upload.
        "InitSession",
    ] {
        r.register(ev, |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_attempt_recommendations(state, event))
        });
    }
    for ev in [
        "PipelineRecommendationSelected",
        "PipelineRecommendationUpvoted",
        "PipelineRecommendationDownvoted",
    ] {
        r.register(ev, |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_recommendation_rerank(state, event))
        });
    }
}

// ---------------------------------------------------------------------------
// Per-session debounce (mirrors python ``_LAST_RUN`` dict).
// ---------------------------------------------------------------------------

fn debounce_state() -> &'static std::sync::Mutex<HashMap<String, f64>> {
    use std::sync::OnceLock;
    static STATE: OnceLock<std::sync::Mutex<HashMap<String, f64>>> = OnceLock::new();
    STATE.get_or_init(|| std::sync::Mutex::new(HashMap::new()))
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn should_debounce(session: &str, trigger: &str) -> bool {
    // Always let DataScienceTaskSelected through — that event carries
    // new context (the auto-detected task) the previous run from
    // DataProfiled couldn't have seen.
    if trigger == "DataScienceTaskSelected" {
        return false;
    }
    let now = now_secs();
    let mut s = debounce_state().lock().unwrap();
    let last = s.get(session).copied().unwrap_or(0.0);
    if now - last < DEBOUNCE_S {
        return true;
    }
    s.insert(session.to_string(), now);
    false
}

// ---------------------------------------------------------------------------
// Main handlers
// ---------------------------------------------------------------------------

async fn handle_attempt_recommendations(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let uid = payload
        .get("uid")
        .and_then(|v| v.as_str())
        .or(event.uid.as_deref())
        .unwrap_or("")
        .to_string();
    let session = payload
        .get("session")
        .and_then(|v| v.as_str())
        .or(event.session.as_deref())
        .unwrap_or("")
        .to_string();
    if uid.is_empty() || session.is_empty() {
        return Ok(());
    }
    if should_debounce(&session, &event.event_type) {
        return Ok(());
    }

    // ── Load meta + dataset ────────────────────────────────────────
    let mut conn = state.redis.clone();
    let raw: Option<String> = conn.get(keys::session_meta(&session)).await.ok().flatten();
    let Some(raw) = raw else {
        return Ok(());
    };
    let meta: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Ok(()),
    };
    let dataset = match meta.get("dataset") {
        Some(d) if d.is_object() => d.clone(),
        _ => return Ok(()),
    };
    let did = dataset
        .get("did")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let has_profile = dataset
        .get("profile")
        .map(|p| p.is_object() && !p.as_object().unwrap().is_empty())
        .unwrap_or(false);

    // ── Prompts: emit state/queries for missing task / eval ────────
    emit_setup_prompts(state, &uid, &session, &meta, &dataset, &did).await;

    // suggest_with_status only runs when we have a metafeature profile
    // — without it the KDTree-similar objective can't score.
    if !has_profile {
        return Ok(());
    }

    if let Err(err) = run_suggest_and_emit(state, &uid, &session, &meta).await {
        let payload = EmitPayload::new(
            "RecommendationEngineFailed",
            "rust-backend.handlers.recommendations_engine.attempt",
            json!({
                "uid": uid,
                "session": session,
                "error": format!("{err:#}"),
            }),
        )
        .with_envelope(event.request_id.clone(), Some(uid), Some(session));
        aemit(state, Lane::Bg, payload).await?;
    }
    Ok(())
}

async fn handle_recommendation_rerank(state: &AppState, event: &EventEnvelope) -> Result<()> {
    // ``recommendation::handle_interaction`` already ran and persisted
    // the redis interaction log + (selected) the meta pipeline save +
    // RecommendationPipelineSaved emit. This handler does the rerank
    // half of python's ``_handle_interaction``: re-suggest + push
    // state/pipelines/recommendation + RecommendationsFetched.
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let uid = payload
        .get("uid")
        .and_then(|v| v.as_str())
        .or(event.uid.as_deref())
        .unwrap_or("")
        .to_string();
    let session = payload
        .get("session")
        .and_then(|v| v.as_str())
        .or(event.session.as_deref())
        .unwrap_or("")
        .to_string();
    if uid.is_empty() || session.is_empty() {
        return Ok(());
    }
    let mut conn = state.redis.clone();
    let raw: Option<String> = conn.get(keys::session_meta(&session)).await.ok().flatten();
    let Some(raw) = raw else {
        return Ok(());
    };
    let meta: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Ok(()),
    };

    let _ = run_suggest_and_emit(state, &uid, &session, &meta).await;
    Ok(())
}

// ---------------------------------------------------------------------------
// Core suggest + emit pipeline
// ---------------------------------------------------------------------------

async fn run_suggest_and_emit(
    state: &AppState,
    uid: &str,
    session: &str,
    meta: &Value,
) -> Result<()> {
    let started = Instant::now();
    let store: Option<Arc<ExperimentStore>> = state.experiment_store.as_ref().cloned();

    // ── Build context ─────────────────────────────────────────────
    let ctx = build_context(state, uid, session, meta).await?;
    let exclude: std::collections::HashSet<&str> = ctx.downvoted.iter().map(|s| s.as_str()).collect();
    let primary_objective = ctx.objective_names.first().cloned();

    // ── Fetch candidates from doc_pipelines ─────────────────────
    let candidates = fetch_candidates(
        state,
        &exclude,
        RETRIEVAL_POOL,
        ctx.task.as_deref(),
        primary_objective.as_deref(),
    )
    .await?;

    // ── Resolve objectives + dependency status ────────────────────
    let mut objectives = Vec::with_capacity(ctx.objective_names.len());
    for name in &ctx.objective_names {
        let obj = match store.as_ref() {
            Some(s) => create_builtin_objective_with_store(name, s),
            None => create_builtin_objective(name),
        };
        if let Some(obj) = obj {
            objectives.push(obj);
        }
    }
    let status = check_dependencies(&objectives, &ctx);

    // ── Score + rank ──────────────────────────────────────────────
    let ranked: Vec<&Candidate> = if candidates.is_empty() || objectives.is_empty() {
        candidates.iter().take(DEFAULT_LIMIT).collect()
    } else {
        let scores = score_candidates(&candidates, &objectives, &ctx);
        let order = rank(&scores, candidates.len(), objectives.len(), RANKING_STRATEGY);
        order
            .into_iter()
            .take(DEFAULT_LIMIT)
            .filter_map(|i| candidates.get(i))
            .collect()
    };

    // Record what we suggested so PreviouslyUnseen can reason about it
    // next round. Mirrors python ``_append_interactions(suggested,
    // ids)``.
    if !ranked.is_empty() {
        let mut conn = state.redis.clone();
        let key = format!("session:{session}:recommendations:interactions");
        let raw: Option<String> = conn.get(&key).await.ok().flatten();
        let mut data: serde_json::Map<String, Value> = match raw {
            Some(s) => serde_json::from_str(&s).unwrap_or_default(),
            None => serde_json::Map::new(),
        };
        let arr = data
            .entry("suggested".to_string())
            .or_insert_with(|| Value::Array(Vec::new()));
        if let Value::Array(list) = arr {
            for c in &ranked {
                list.push(Value::String(c.id.clone()));
            }
        }
        let _: redis::RedisResult<()> = conn
            .set(&key, serde_json::to_string(&Value::Object(data)).unwrap_or_default())
            .await;
    }

    // ── Emit state events ─────────────────────────────────────────
    let mut conn = state.redis.clone();
    let stream = keys::ws_stream(uid, session);
    let suggestions_value: Vec<Value> = ranked.iter().map(|c| candidate_to_doc(c)).collect();
    let suggestions_str = serde_json::to_string(&Value::Array(suggestions_value.clone()))?;
    let _: redis::RedisResult<String> = conn
        .xadd_maxlen(
            &stream,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[
                ("event", "state/pipelines/recommendation"),
                ("value", suggestions_str.as_str()),
                ("type", "json"),
            ],
        )
        .await;
    let status_str = serde_json::to_string(&status)?;
    let _: redis::RedisResult<String> = conn
        .xadd_maxlen(
            &stream,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[
                ("event", "state/objectives/status"),
                ("value", status_str.as_str()),
                ("type", "json"),
            ],
        )
        .await;

    // RecommendationsFetched fan-out — observability + downstream
    // RiskIdentified replay (python debug_recommended_pipelines).
    let payload = EmitPayload::new(
        "RecommendationsFetched",
        "rust-backend.handlers.recommendations_engine.suggest",
        json!({
            "uid": uid,
            "session": session,
            "suggestions": suggestions_value,
            "elapsed_ms": started.elapsed().as_millis() as u64,
        }),
    )
    .with_envelope(None, Some(uid.to_string()), Some(session.to_string()));
    aemit(state, Lane::Bg, payload).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Context + interactions
// ---------------------------------------------------------------------------

async fn build_context(
    state: &AppState,
    uid: &str,
    session: &str,
    meta: &Value,
) -> Result<RecommendationContext> {
    let mut conn = state.redis.clone();
    let inter_key = format!("session:{session}:recommendations:interactions");
    let inter_raw: Option<String> = conn.get(&inter_key).await.ok().flatten();
    let interactions: Value = inter_raw
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or(json!({}));
    let id_list = |key: &str| -> Vec<String> {
        interactions
            .get(key)
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default()
    };

    let dataset_profile = meta
        .get("dataset")
        .and_then(|d| d.get("profile"))
        .filter(|v| v.is_object())
        .cloned();

    let objective_names: Vec<String> = meta
        .get("rankingObjectives")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|item| item.get("name").and_then(|n| n.as_str()).map(String::from))
                .collect()
        })
        .unwrap_or_default();

    let task = meta
        .get("selectedDataScienceTask")
        .and_then(|t| t.get("name"))
        .and_then(|v| v.as_str())
        .map(String::from);

    let current_pipeline =
        meta.get("pipeline").cloned().and_then(|raw| match raw {
            Value::String(s) => serde_json::from_str(&s).ok(),
            v if v.is_object() => Some(v),
            _ => None,
        });
    // Convert the inline pipeline JSON into a PipelineSnapshot the
    // rust scoring engine expects. The python sender used
    // ``_ctx_to_rust_payload``; we replicate inline since the
    // snapshot type only carries ``nodes``.
    let current_pipeline_snapshot = current_pipeline
        .as_ref()
        .and_then(|p| {
            let nodes_val = p.get("nodes")?;
            let nodes_map: HashMap<String, Value> = match nodes_val {
                Value::Object(m) => m.clone().into_iter().collect(),
                Value::Array(arr) => arr
                    .iter()
                    .enumerate()
                    .map(|(i, v)| (i.to_string(), v.clone()))
                    .collect(),
                _ => return None,
            };
            Some(optimizer::recommendation::objectives::PipelineSnapshot {
                nodes: nodes_map,
            })
        });

    Ok(RecommendationContext {
        uid: uid.to_string(),
        session: session.to_string(),
        current_pipeline: current_pipeline_snapshot,
        dataset_profile,
        dataset_profile_vec: None,
        upvoted: id_list("upvoted"),
        downvoted: id_list("downvoted"),
        selected: id_list("selected"),
        suggested: id_list("suggested"),
        objective_names,
        task,
    })
}

// ---------------------------------------------------------------------------
// Candidate fetch (postgres doc_pipelines)
// ---------------------------------------------------------------------------

async fn fetch_candidates(
    state: &AppState,
    exclude_ids: &std::collections::HashSet<&str>,
    limit: i64,
    task: Option<&str>,
    primary_objective: Option<&str>,
) -> Result<Vec<Candidate>> {
    let Some(pool) = state.pg.as_ref() else {
        return Ok(Vec::new());
    };

    // Anchor by primary objective when applicable.
    if matches!(primary_objective, Some("Faster Execution")) {
        if let Ok(c) = fetch_smallest_pipelines(state, exclude_ids, task, limit).await {
            return Ok(c);
        }
    }
    // PPR anchor by win rate is a python KB-cache call — skip in v1
    // (the rust ExperimentStore already has win_rates; we'd need a
    // top-K accessor on it). Falls through to random sample.

    let client = pool.get().await?;
    let exclude_vec: Vec<String> = exclude_ids.iter().map(|s| s.to_string()).collect();

    // Try the task-filtered query first; if it yields too few, retry
    // unfiltered. Mirrors python ``_TASK_FALLBACK_THRESHOLD``.
    if let Some(task_name) = task {
        let sql = build_sample_sql(true);
        let rows = client
            .query(&sql, &[&exclude_vec, &task_name, &limit])
            .await
            .unwrap_or_default();
        if rows.len() >= TASK_FALLBACK_THRESHOLD {
            return Ok(rows.into_iter().filter_map(row_to_candidate).collect());
        }
    }
    let sql = build_sample_sql(false);
    let rows = client
        .query(&sql, &[&exclude_vec, &limit])
        .await
        .unwrap_or_default();
    Ok(rows.into_iter().filter_map(row_to_candidate).collect())
}

fn build_sample_sql(with_task: bool) -> String {
    if with_task {
        "SELECT id, data FROM doc_pipelines \
         WHERE id <> ALL($1) AND data->>'task' = $2 \
         ORDER BY random() LIMIT $3"
            .to_string()
    } else {
        "SELECT id, data FROM doc_pipelines \
         WHERE id <> ALL($1) \
         ORDER BY random() LIMIT $2"
            .to_string()
    }
}

async fn fetch_smallest_pipelines(
    state: &AppState,
    exclude_ids: &std::collections::HashSet<&str>,
    task: Option<&str>,
    limit: i64,
) -> Result<Vec<Candidate>> {
    let Some(pool) = state.pg.as_ref() else {
        return Ok(Vec::new());
    };
    let client = pool.get().await?;
    let exclude_vec: Vec<String> = exclude_ids.iter().map(|s| s.to_string()).collect();
    let order = "ORDER BY (CASE jsonb_typeof(data->'nodes') \
                   WHEN 'array' THEN jsonb_array_length(data->'nodes') \
                   WHEN 'object' THEN \
                       (SELECT COUNT(*)::int FROM jsonb_object_keys(data->'nodes')) \
                   ELSE 1000000 \
                 END) ASC";
    let rows = if let Some(t) = task {
        let sql = format!(
            "SELECT id, data FROM doc_pipelines \
             WHERE id <> ALL($1) AND data->>'task' = $2 \
             {order} LIMIT $3"
        );
        client
            .query(&sql, &[&exclude_vec, &t, &limit])
            .await
            .unwrap_or_default()
    } else {
        let sql = format!(
            "SELECT id, data FROM doc_pipelines \
             WHERE id <> ALL($1) \
             {order} LIMIT $2"
        );
        client
            .query(&sql, &[&exclude_vec, &limit])
            .await
            .unwrap_or_default()
    };
    Ok(rows.into_iter().filter_map(row_to_candidate).collect())
}

fn row_to_candidate(row: tokio_postgres::Row) -> Option<Candidate> {
    let id: String = row.get(0);
    let data: tokio_postgres::types::Json<Value> = row.get(1);
    let mut obj = data.0.as_object().cloned().unwrap_or_default();
    obj.insert("_id".to_string(), Value::String(id));
    let cand: Candidate = serde_json::from_value(Value::Object(obj)).ok()?;
    Some(cand)
}

fn candidate_to_doc(c: &Candidate) -> Value {
    // Best-effort serialise into the dict shape the SPA expects.
    // Mirrors python ``_serialize_suggestions`` which round-trips
    // through JSON with default=str. We've already deserialised into
    // ``Candidate``; ship its serde representation back.
    let mut v = serde_json::to_value(c).unwrap_or(Value::Null);
    if let Some(obj) = v.as_object_mut() {
        if let Some(id_val) = obj.get("_id").cloned() {
            obj.entry("uuid".to_string()).or_insert(id_val.clone());
            obj.entry("pipeline_id".to_string()).or_insert(id_val);
        }
        // Operator FQN list — handy for the SPA to render the chip
        // strip without re-walking nodes.
        let ops = extract_operator_names(c);
        obj.insert("_operators".to_string(), json!(ops));
    }
    v
}

// ---------------------------------------------------------------------------
// Setup prompts (state/queries)
// ---------------------------------------------------------------------------

async fn emit_setup_prompts(
    state: &AppState,
    uid: &str,
    session: &str,
    meta: &Value,
    dataset: &Value,
    did: &str,
) {
    let mut conn = state.redis.clone();
    let task_qid = format!("session:{session}:task_selection");
    let eval_qid = format!("session:{session}:eval_selection");

    let pending: std::collections::HashSet<String> = meta
        .get("_pendingQueryIds")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();

    let has_task = meta
        .get("selectedDataScienceTask")
        .map(|v| !v.is_null())
        .unwrap_or(false);
    let has_eval = meta
        .get("selectedEvaluationProcedureId")
        .map(|v| !v.is_null())
        .unwrap_or(false)
        || meta
            .get("selectedEvaluationProcedureName")
            .map(|v| !v.is_null())
            .unwrap_or(false);

    // Skip the task question when the dataset has targets — auto-detection
    // will infer the task and emit DataScienceTaskSelected (mirrors
    // python's ``auto_detect_likely`` short-circuit).
    let auto_detect_likely = if !did.is_empty() {
        let raw: Option<String> = conn
            .get(keys::dataset_target_columns(did))
            .await
            .ok()
            .flatten();
        raw.as_deref().map(|s| s != "[]").unwrap_or(false)
    } else {
        false
    };
    let _ = dataset; // dataset is the source of did; keep param for parity

    let mut queries: Vec<Value> = Vec::new();
    if !has_task && !auto_detect_likely && !pending.contains(&task_qid) {
        if let Some(tasks) = list_tasks(state) {
            queries.push(json!({
                "id": task_qid,
                "type": "select",
                "question": "What data science task would you like to perform?",
                "options": tasks,
            }));
        }
    }
    if !has_eval && !pending.contains(&eval_qid) {
        let evals = list_evals(state).await;
        if !evals.is_empty() {
            queries.push(json!({
                "id": eval_qid,
                "type": "select",
                "question": "Which evaluation procedure should be used?",
                "options": evals,
            }));
        }
    }

    if queries.is_empty() {
        return;
    }

    // Persist pending IDs into meta + push state/queries to SPA.
    if let Ok(meta_str) = serde_json::to_string(meta) {
        let mut updated_meta: Value =
            serde_json::from_str(&meta_str).unwrap_or(meta.clone());
        if let Some(obj) = updated_meta.as_object_mut() {
            let mut list: Vec<Value> = pending.into_iter().map(Value::String).collect();
            for q in &queries {
                if let Some(id) = q.get("id").and_then(|v| v.as_str()) {
                    list.push(Value::String(id.to_string()));
                }
            }
            obj.insert("_pendingQueryIds".to_string(), Value::Array(list));
        }
        if let Ok(s) = serde_json::to_string(&updated_meta) {
            let _: redis::RedisResult<()> = conn.set(keys::session_meta(session), s).await;
        }
    }

    let stream = keys::ws_stream(uid, session);
    if let Ok(qstr) = serde_json::to_string(&Value::Array(queries)) {
        let _: redis::RedisResult<String> = conn
            .xadd_maxlen(
                &stream,
                StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
                "*",
                &[
                    ("event", "state/queries"),
                    ("value", qstr.as_str()),
                    ("type", "json"),
                ],
            )
            .await;
    }
}

fn list_tasks(state: &AppState) -> Option<Vec<String>> {
    let kb = state.kb.load_full()?;
    let mut tasks = std::collections::BTreeSet::new();
    for op in kb.all_operators() {
        for t in op.tasks.iter() {
            tasks.insert(t.clone());
        }
    }
    let v: Vec<String> = tasks.into_iter().collect();
    if v.is_empty() {
        None
    } else {
        Some(v)
    }
}

async fn list_evals(state: &AppState) -> Vec<String> {
    let Some(pool) = state.pg.as_ref() else {
        return Vec::new();
    };
    let Ok(client) = pool.get().await else {
        return Vec::new();
    };
    let rows = client
        .query(
            "SELECT data->>'name' FROM doc_evaluation_procedures \
             ORDER BY data->>'name'",
            &[],
        )
        .await
        .unwrap_or_default();
    rows.into_iter()
        .filter_map(|row| row.try_get::<_, Option<String>>(0).ok().flatten())
        .collect()
}
