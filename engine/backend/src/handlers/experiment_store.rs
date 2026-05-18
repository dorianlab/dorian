//! Experiment-store write handlers — replaces
//! ``dorian/event/handlers/experiment.py``. Pure postgres I/O; no
//! python compute on the hot path.
//!
//! Five handlers:
//!
//!   * ``DataProfiled`` → upsert ``datasets`` row + write the
//!     persistent dataset record into ``doc_datasets``.
//!   * ``PipelineSaved`` → upsert ``pipelines`` row.
//!   * ``PipelineRunCompleted`` / ``PipelineRunFailed`` → record one
//!     ``evaluations`` row per metric (only on completed runs).
//!   * ``PipelineRecommendationSelected/Upvoted/Downvoted`` → append to
//!     the ``interactions`` table.
//!
//! KD/BKTree note: the rust ``ExperimentStore`` is loaded once at
//! startup (see ``engine/backend/src/experiment_store.rs``). Runtime
//! upserts here do NOT update that in-memory index — same as the
//! python ``handle_*`` handlers, which only wrote to the python KD/BK
//! trees. Per-restart freshness is the existing contract; an
//! incremental-update path is a follow-up.
//!
//! ``profile_vec`` is left NULL on rust inserts because the rust port
//! doesn't yet have the python ``profile_to_vector`` mapping. The
//! rust ``ExperimentStore::load_datasets`` filter
//! ``WHERE profile_vec IS NOT NULL`` skips these rows on next
//! restart — datasets stay queryable via the catalog API but won't
//! contribute to similarity search until the rust vectoriser lands.

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::{json, Value};
use std::collections::HashMap;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

pub fn register(r: &mut Registry) {
    r.register("DataProfiled", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_dataset_profiled(state, event))
    });
    r.register("PipelineSaved", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_pipeline_saved_to_store(state, event))
    });
    r.register("PipelineRunCompleted", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_run_completed(state, event))
    });
    r.register("PipelineRunFailed", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_run_completed(state, event))
    });
    for ev in [
        "PipelineRecommendationSelected",
        "PipelineRecommendationUpvoted",
        "PipelineRecommendationDownvoted",
    ] {
        r.register(ev, |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_recommendation_interaction_to_store(state, event))
        });
    }
}

async fn read_session_meta(state: &AppState, session: &str) -> Option<Value> {
    let mut conn = state.redis.clone();
    let raw: Option<String> = conn.get(keys::session_meta(session)).await.ok().flatten();
    raw.as_deref().and_then(|s| serde_json::from_str(s).ok())
}

fn extract_pipeline_id(meta: &Value) -> Option<String> {
    meta.get("pipeline")
        .and_then(|p| p.get("id"))
        .and_then(|v| v.as_str())
        .map(String::from)
}

fn extract_pipeline_dag(meta: &Value) -> Option<Value> {
    let pipeline = meta.get("pipeline")?;
    let raw = pipeline.get("pipeline")?;
    match raw {
        Value::String(s) => serde_json::from_str(s).ok(),
        Value::Object(_) => Some(raw.clone()),
        _ => None,
    }
}

/// Pull the Ptolemy II actor-graph payload out of session meta if
/// the canvas has migrated to send it. Forward-compatible — stays
/// `None` until the frontend wire-format flip lands; once it does,
/// this is the only path needed and `extract_pipeline_dag` retires.
fn extract_pipeline_model(meta: &Value) -> Option<Value> {
    let pipeline = meta.get("pipeline")?;
    let raw = pipeline.get("model")?;
    match raw {
        Value::String(s) => serde_json::from_str(s).ok(),
        Value::Object(_) => Some(raw.clone()),
        _ => None,
    }
}

fn extract_operator_names(dag: &Value) -> Vec<String> {
    let body = dag.get("pipeline").cloned().unwrap_or_else(|| dag.clone());
    let body = match body {
        Value::String(s) => serde_json::from_str(&s).unwrap_or(Value::Null),
        v => v,
    };
    let nodes = match body.get("nodes") {
        Some(n) => n,
        None => return Vec::new(),
    };
    let mut names: Vec<String> = Vec::new();
    let push = |out: &mut Vec<String>, node: &Value| {
        if let Some(name) = node.get("name").and_then(|v| v.as_str()) {
            if name.contains('.') {
                out.push(name.to_string());
            }
        }
    };
    match nodes {
        Value::Object(map) => {
            for v in map.values() {
                push(&mut names, v);
            }
        }
        Value::Array(arr) => {
            for v in arr {
                push(&mut names, v);
            }
        }
        _ => {}
    }
    names
}

// ---------------------------------------------------------------------------
// DataProfiled
// ---------------------------------------------------------------------------

async fn handle_dataset_profiled(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let session = match event
        .payload
        .get("session")
        .and_then(|v| v.as_str())
        .or(event.session.as_deref())
    {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => return Ok(()),
    };
    let did = match event
        .payload
        .get("did")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
    {
        Some(s) => s.to_string(),
        None => return Ok(()),
    };

    let Some(meta) = read_session_meta(state, &session).await else {
        return Ok(());
    };
    let dataset = match meta.get("dataset") {
        Some(d) => d.clone(),
        None => return Ok(()),
    };
    let profile = match dataset.get("profile") {
        Some(Value::Object(_)) => dataset.get("profile").cloned().unwrap_or(Value::Null),
        _ => return Ok(()),
    };

    let Some(pool) = state.pg.as_ref() else {
        return Ok(());
    };
    let client = pool.get().await?;
    let did_s = did.clone();
    let session_s = session.clone();
    let profile_json = tokio_postgres::types::Json(profile.clone());
    // ``datasets`` table — profile_vec stays NULL on the rust path
    // (see module docs). vec_version=1 matches python's
    // ``get_feature_version`` for the empty mapping; the rust
    // ExperimentStore filter ignores NULL profile_vec rows on next
    // load, so this row stays visible via /catalog APIs but doesn't
    // contribute to KD-tree similarity search.
    let _ = client
        .execute(
            "INSERT INTO datasets (id, session, profile, profile_vec, vec_version) \
             VALUES ($1, $2, $3, NULL, 1) \
             ON CONFLICT (id) DO UPDATE SET \
                 profile = EXCLUDED.profile, \
                 updated_at = NOW()",
            &[&did_s, &session_s, &profile_json],
        )
        .await?;

    // Also persist the user-facing dataset record into
    // ``doc_datasets`` (cross-session discovery).
    let uid = event
        .payload
        .get("uid")
        .and_then(|v| v.as_str())
        .or_else(|| dataset.get("uid").and_then(|v| v.as_str()))
        .unwrap_or("");
    let fpath = event
        .payload
        .get("fpath")
        .and_then(|v| v.as_str())
        .or_else(|| dataset.get("fpath").and_then(|v| v.as_str()))
        .unwrap_or("")
        .to_string();
    let filename = std::path::Path::new(&fpath)
        .file_name()
        .and_then(|o| o.to_str())
        .map(String::from)
        .unwrap_or_else(|| did.clone());
    let item_count: i64 = profile
        .get("NumberOfInstances")
        .and_then(|v| v.as_i64())
        .or_else(|| profile.get("NumberOfInstances").and_then(|v| v.as_f64()).map(|f| f as i64))
        .unwrap_or(0);

    let mut conn = state.redis.clone();
    let features_raw: Option<String> = conn
        .get(keys::dataset_feature_columns(&did))
        .await
        .ok()
        .flatten();
    let targets_raw: Option<String> = conn
        .get(keys::dataset_target_columns(&did))
        .await
        .ok()
        .flatten();
    let features: Value = features_raw
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or(Value::Null);
    let targets: Value = targets_raw
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or(Value::Null);

    // Skip the rewrite when this did is already a public dataset
    // (OpenML-seeded). Mirrors python's ``is_public_dedup`` branch.
    let existing_public: Option<bool> = client
        .query_opt(
            "SELECT (data->>'isPublic')::boolean OR \
                    (data->'source'->>'type') = 'openml' \
             FROM doc_datasets WHERE id = $1",
            &[&did_s],
        )
        .await
        .ok()
        .flatten()
        .and_then(|row| row.get::<_, Option<bool>>(0));
    let is_public_dedup = existing_public.unwrap_or(false);

    let content_hash = dataset
        .get("content_hash")
        .and_then(|v| v.as_str())
        .map(String::from);
    let description = dataset
        .get("description")
        .and_then(|v| v.as_str())
        .filter(|s| !s.trim().is_empty())
        .map(String::from);

    let now_iso = chrono::Utc::now().to_rfc3339();
    let dataset_doc = if is_public_dedup {
        // Profile + updatedAt only — leave name/source/ownership/storage
        // alone (catalogue owns them).
        json!({
            "_id": did,
            "profile": profile,
            "updatedAt": now_iso,
        })
    } else {
        let mut doc = json!({
            "_id": did,
            "ownerId": uid,
            "isPublic": false,
            "name": filename,
            "dataType": "tabular",
            "itemCount": item_count,
            "source": {
                "type": "user-upload",
                "originalId": did,
                "url": Value::Null,
            },
            "storage": {
                "format": "csv",
                "location": {"type": "local", "path": fpath},
            },
            "profile": profile,
            "features": features,
            "targets": targets,
            "updatedAt": now_iso,
        });
        if let Some(o) = doc.as_object_mut() {
            if let Some(h) = content_hash {
                o.insert("contentHash".into(), Value::String(h));
            }
            if let Some(d) = description {
                o.insert("description".into(), Value::String(d));
            }
        }
        doc
    };

    let dataset_json = tokio_postgres::types::Json(dataset_doc);
    let _ = client
        .execute(
            "INSERT INTO doc_datasets (id, data, created_at, updated_at) \
             VALUES ($1, $2, NOW(), NOW()) \
             ON CONFLICT (id) DO UPDATE SET \
                 data = doc_datasets.data || EXCLUDED.data, \
                 updated_at = NOW()",
            &[&did_s, &dataset_json],
        )
        .await?;

    let payload = EmitPayload::new(
        "DatasetPersistedToDocstore",
        "rust-backend.handlers.experiment_store.dataset_profiled",
        json!({"did": did}),
    )
    .with_envelope(
        event.request_id.clone(),
        event.uid.clone(),
        Some(session),
    );
    aemit(state, Lane::Bg, payload).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// PipelineSaved
// ---------------------------------------------------------------------------

async fn handle_pipeline_saved_to_store(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let session = match event
        .session
        .as_deref()
        .or_else(|| event.payload.get("session").and_then(|v| v.as_str()))
    {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => return Ok(()),
    };

    let Some(meta) = read_session_meta(state, &session).await else {
        return Ok(());
    };
    let Some(pipeline_id) = extract_pipeline_id(&meta) else {
        return Ok(());
    };
    let Some(dag) = extract_pipeline_dag(&meta) else {
        return Ok(());
    };
    let model = extract_pipeline_model(&meta);
    let task = meta
        .get("selectedDataScienceTask")
        .and_then(|t| t.get("name"))
        .and_then(|v| v.as_str())
        .map(String::from);
    let operators = extract_operator_names(&dag);

    let Some(pool) = state.pg.as_ref() else {
        return Ok(());
    };
    let client = pool.get().await?;
    let dag_json = tokio_postgres::types::Json(dag);
    // Forward-compatible: ``model`` is NULL until the canvas
    // migrates to send the Ptolemy II shape on PipelineSaved.
    // Both columns coexist during the storage canonicalisation
    // phase; once readers move off ``dag``, the legacy column
    // drops.
    let model_pg: Option<tokio_postgres::types::Json<Value>> =
        model.map(tokio_postgres::types::Json);
    let provenance = "user".to_string();
    let task_opt = task.unwrap_or_default();
    let _ = client
        .execute(
            "INSERT INTO pipelines (id, session, task, dag, model, operators, provenance) \
             VALUES ($1, $2, NULLIF($3, ''), $4, $5, $6, $7) \
             ON CONFLICT (id) DO UPDATE SET \
                 dag = EXCLUDED.dag, \
                 model = COALESCE(EXCLUDED.model, pipelines.model), \
                 operators = EXCLUDED.operators, \
                 task = EXCLUDED.task",
            &[
                &pipeline_id,
                &session,
                &task_opt,
                &dag_json,
                &model_pg,
                &operators,
                &provenance,
            ],
        )
        .await?;

    Ok(())
}

// ---------------------------------------------------------------------------
// PipelineRunCompleted / PipelineRunFailed
// ---------------------------------------------------------------------------

async fn handle_run_completed(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let run_id = payload
        .get("run_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let session = payload
        .get("session")
        .and_then(|v| v.as_str())
        .or(event.session.as_deref())
        .unwrap_or("")
        .to_string();
    if run_id.is_empty() || session.is_empty() {
        return Ok(());
    }
    let status = payload
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let is_failure = status.to_ascii_lowercase().contains("failed");

    // FAILURE PATH — write a sentinel ``__failed__`` row instead of
    // returning quietly. Without this, the (pipeline_id, dataset_id)
    // pair has no trace in ``evaluations`` and xproduct's
    // ``NOT EXISTS (SELECT 1 FROM evaluations e WHERE e.pipeline_id = p.id
    // AND e.dataset_id = d.id)`` keeps re-selecting the pair every 30s
    // — the system hangs on a tight retry loop on every broken pipeline.
    //
    // The sentinel row carries:
    //   * ``metric_name = '__failed__'``  (never a real metric)
    //   * ``metric_value = NaN``          (passes the NOT NULL constraint
    //                                      but is filtered out by every
    //                                      finite-value leaderboard query)
    //   * ``eval_config = {"status":"failed", "error_message": …}``
    //     so consumers can distinguish failure modes.
    // xproduct gets gated naturally via the existing NOT EXISTS query.
    if is_failure {
        let mut pipeline_id = payload
            .get("pipeline_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let meta = read_session_meta(state, &session).await;
        if pipeline_id.is_empty() {
            pipeline_id = meta
                .as_ref()
                .and_then(extract_pipeline_id)
                .unwrap_or_default();
        }
        let mut dataset_id = payload
            .get("dataset_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if dataset_id.is_empty() {
            if let Some(m) = &meta {
                dataset_id = m
                    .get("dataset")
                    .and_then(|d| d.get("did"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
            }
        }
        if pipeline_id.is_empty() || dataset_id.is_empty() {
            return Ok(());
        }
        let Some(pool) = state.pg.as_ref() else {
            return Ok(());
        };
        let client = pool.get().await?;
        let err_msg = payload
            .get("error_message")
            .and_then(|v| v.as_str())
            .or_else(|| payload.get("error").and_then(|v| v.as_str()))
            .unwrap_or("")
            .to_string();
        let source = payload
            .get("source")
            .and_then(|v| v.as_str())
            .unwrap_or("runner")
            .to_string();

        // ── Bind to the per-node failure record (Phase 1 of
        // pattern-gated retries; see internal design notes). The
        // python ``execution_error_handler`` writes the per-node
        // entry to ``doc_execution_error_instances`` BEFORE the
        // runner emits ``PipelineRunFailed``, so by the time we
        // hit this branch the row's already there. We pull the
        // ``signature`` (16-char traceback fingerprint) and
        // ``pattern_id`` (nullable) so xproduct's gate can join on
        // pattern → ``doc_exception_patterns`` and re-enable the
        // pair when the pattern is marked inactive.
        let mut signature: Option<String> = None;
        let mut pattern_id: Option<String> = None;
        let lookup = client
            .query_opt(
                "SELECT data->>'signature'  AS sig, \
                        data->>'pattern_id' AS pid \
                 FROM doc_execution_error_instances \
                 WHERE data->>'run_id' = $1 \
                 ORDER BY created_at DESC LIMIT 1",
                &[&run_id],
            )
            .await;
        if let Ok(Some(row)) = lookup {
            signature = row.try_get::<_, Option<String>>("sig").ok().flatten();
            pattern_id = row.try_get::<_, Option<String>>("pid").ok().flatten();
        }

        let eval_cfg = tokio_postgres::types::Json(json!({
            "status": "failed",
            "error_message": err_msg,
            "source": source,
            "signature": signature,
            "pattern_id": pattern_id,
        }));
        // Top-level columns (``source`` / ``status`` / ``error_message``)
        // are duplicated in ``eval_config`` for the cohort of
        // consumers (``rl/train/persistence.py``, the rust automl
        // ingest) that read either path — see
        // ``dorian/experiment/schema.py`` for the column list.
        let _ = client
            .execute(
                "INSERT INTO evaluations \
                    (pipeline_id, dataset_id, run_id, metric_name, metric_value, \
                     eval_config, source, status, error_message) \
                 VALUES ($1, $2, $3, '__failed__', 'NaN'::float, \
                         $4, $5, 'failed', $6) \
                 ON CONFLICT DO NOTHING",
                &[&pipeline_id, &dataset_id, &run_id, &eval_cfg, &source, &err_msg],
            )
            .await;
        return Ok(());
    }

    // Authoritative metrics dict + summary fallback. Mirrors python.
    let mut metrics: HashMap<String, f64> = HashMap::new();
    if let Some(m) = payload.get("metrics").and_then(|v| v.as_object()) {
        for (k, v) in m {
            if let Some(f) = v.as_f64() {
                metrics.insert(k.clone(), f);
            }
        }
    }
    if metrics.is_empty() {
        if let Some(summary) = payload.get("summary").and_then(|v| v.as_object()) {
            if let Some(nested) = summary.get("metrics").and_then(|v| v.as_object()) {
                for (k, v) in nested {
                    if let Some(f) = v.as_f64() {
                        metrics.insert(k.clone(), f);
                    }
                }
            }
            if metrics.is_empty() {
                for key in &["accuracy", "score", "f1", "auc", "rmse", "mse"] {
                    if let Some(f) = summary.get(*key).and_then(|v| v.as_f64()) {
                        metrics.insert((*key).to_string(), f);
                    }
                }
            }
        }
    }
    if metrics.is_empty() {
        return Ok(());
    }

    // Resolve pipeline_id + dataset_id.
    let mut pipeline_id = payload
        .get("pipeline_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let meta = read_session_meta(state, &session).await;
    if pipeline_id.is_empty() {
        pipeline_id = meta
            .as_ref()
            .and_then(extract_pipeline_id)
            .unwrap_or_default();
    }
    if pipeline_id.is_empty() {
        return Ok(());
    }
    let mut dataset_id = payload
        .get("dataset_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if dataset_id.is_empty() {
        if let Some(m) = &meta {
            dataset_id = m
                .get("dataset")
                .and_then(|d| d.get("did"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
        }
    }
    if dataset_id.is_empty() {
        return Ok(());
    }

    let Some(pool) = state.pg.as_ref() else {
        return Ok(());
    };
    let client = pool.get().await?;
    let eval_config_json = tokio_postgres::types::Json(json!({}));
    for (metric_name, metric_value) in &metrics {
        let _ = client
            .execute(
                "INSERT INTO evaluations \
                    (pipeline_id, dataset_id, run_id, metric_name, metric_value, eval_config) \
                 VALUES ($1, $2, $3, $4, $5, $6) \
                 ON CONFLICT DO NOTHING",
                &[
                    &pipeline_id,
                    &dataset_id,
                    &run_id,
                    metric_name,
                    metric_value,
                    &eval_config_json,
                ],
            )
            .await?;
    }
    let metric_keys: Vec<String> = metrics.keys().cloned().collect();
    let payload = EmitPayload::new(
        "EvaluationBatchRecorded",
        "rust-backend.handlers.experiment_store.run_completed",
        json!({
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "dataset_id": dataset_id,
            "metrics": metric_keys,
        }),
    )
    .with_envelope(event.request_id.clone(), event.uid.clone(), Some(session));
    aemit(state, Lane::Bg, payload).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Recommendation interactions → interactions table
// ---------------------------------------------------------------------------

async fn handle_recommendation_interaction_to_store(
    state: &AppState,
    event: &EventEnvelope,
) -> Result<()> {
    let payload = match event.payload.as_object() {
        Some(o) => o,
        None => return Ok(()),
    };
    let inner = payload
        .get("payload")
        .and_then(|v| v.as_object())
        .unwrap_or(payload);
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
    let pipeline_id = payload
        .get("pipelineId")
        .and_then(|v| v.as_str())
        .or_else(|| inner.get("pipelineId").and_then(|v| v.as_str()))
        .unwrap_or("")
        .to_string();
    if uid.is_empty() || session.is_empty() || pipeline_id.is_empty() {
        return Ok(());
    }

    let meta = match read_session_meta(state, &session).await {
        Some(m) => m,
        None => return Ok(()),
    };
    let dataset_id = meta
        .get("dataset")
        .and_then(|d| d.get("did"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if dataset_id.is_empty() {
        return Ok(());
    }
    let task = meta
        .get("selectedDataScienceTask")
        .and_then(|t| t.get("name"))
        .and_then(|v| v.as_str())
        .map(String::from);

    let Some(pool) = state.pg.as_ref() else {
        return Ok(());
    };
    let client = pool.get().await?;
    let event_type = &event.event_type;
    let task_opt: Option<&str> = task.as_deref();
    if event_type.contains("Downvoted") {
        // Mirror python's special-case: compared = preferred = discarded =
        // pipeline_id (the "not this one" sentinel; queries handle it).
        let _ = client
            .execute(
                "INSERT INTO interactions \
                    (dataset_id, task, compared_id, preferred_id, discarded_id, user_id) \
                 VALUES ($1, $2, $3, $4, $5, $6)",
                &[
                    &dataset_id,
                    &task_opt,
                    &pipeline_id,
                    &pipeline_id,
                    &pipeline_id,
                    &uid,
                ],
            )
            .await?;
    } else {
        let _ = client
            .execute(
                "INSERT INTO interactions \
                    (dataset_id, task, compared_id, preferred_id, user_id) \
                 VALUES ($1, $2, $3, $4, $5)",
                &[
                    &dataset_id,
                    &task_opt,
                    &pipeline_id,
                    &pipeline_id,
                    &uid,
                ],
            )
            .await?;
    }
    Ok(())
}
