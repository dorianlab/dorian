//! Session-state replay on ``InitSession``.
//!
//! Replaces ``dorian/event/handlers/session.py::seed_session``'s
//! Phase 1 + Phase 2 — the exact set of WS events the SPA needs to
//! render the canvas, sidebar selectors, and ranking-objectives panel
//! when a user (re)connects. Phase 3 (recommendations + tooltips +
//! operator-params hot path) is still owned by the python handler;
//! the rust port handles only the load-bearing bits because every
//! reconnect goes through here and the python event-bus has a
//! recurring silent-stall pattern (see
//! ``project_python_eventbus_workers_degrade.md``).
//!
//! Shape parity with python:
//!
//! Phase 1 (always — derives entirely from session_meta in Redis):
//!   * ``state/pipeline``       (json) — meta.pipelineHistory or {}
//!   * ``state/dataset``        (json) — meta.dataset or {}
//!   * ``state/target``         (json) — when meta.dataset.target is set
//!   * ``state/lastRun``        (json) — when meta.lastRun is set
//!   * ``state/selected-task``  (json|string) — auto-detect badge
//!   * ``state/selected-eval``  (string) — when picked
//!   * ``state/custom-evals``   (json)   — meta.EvaluationProcedures
//!
//! Phase 2 (KB catalogs):
//!   * ``state/operators``           (list) — uuid:name:type per FQN
//!   * ``state/tasks``               (list) — uuid:name from operator.tasks
//!   * ``state/objectives``          (list) — from postgres doc_ranking_objectives
//!   * ``state/evals``               (list) — from postgres doc_evaluation_procedures
//!   * ``state/objectives/selected`` (list) — uuids from session_meta.rankingObjectives
//!   * ``state/operator-params``     (json) — full param catalog from KB
//!
//! ``state/queries`` and ``state/pipelines/recommendation`` are
//! Phase 3 deliverables and stay python-side until the recommendation
//! pipeline ports too.
//!
//! Why we don't unsubscribe Python's ``seed_session`` outright: the
//! rust handler covers the on-the-wire SPA contract for state
//! seeding, but Phase 3 is still required for full UX parity. The
//! Python side now short-circuits Phase 1 + Phase 2 (the rust
//! handler already covered them) and only fires Phase 3, plus
//! ``slack_on_session_init`` and the ``state/queries`` resolver.

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 100_000;

pub fn register(r: &mut Registry) {
    r.register("InitSession", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(seed_session(state, event))
    });
}

fn payload_str(event: &EventEnvelope, key: &str) -> Option<String> {
    event
        .payload
        .get(key)
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty())
}

/// Stable uuid for built-in catalog entries (operators, tasks,
/// objectives, evals). The python ``Operators.get()`` returns
/// uuids generated from KB declarations; the SPA uses them only as
/// stable identifiers, so a deterministic ``uuid5(URL, name)``
/// suffices and avoids the per-request random uuids that the
/// gateway's catalog endpoint uses (those rotate every page load and
/// break sticky selections).
fn stable_uuid(name: &str) -> String {
    Uuid::new_v5(&Uuid::NAMESPACE_URL, name.as_bytes())
        .simple()
        .to_string()
}

/// Best-guess category for an operator FQN. Mirrors the python
/// ``_get_operator_type_lookup`` heuristic just enough to distinguish
/// the major buckets the sidebar groups by. Falls back to
/// ``"operator"`` for anything we don't recognise — the SPA tolerates
/// that.
fn op_type_for(fqn: &str) -> &'static str {
    let lc = fqn.to_ascii_lowercase();
    if lc.contains("metric") || lc.contains("score") {
        "metric"
    } else if lc.contains("guardrail") {
        "guardrail"
    } else if lc.contains(".io.") || lc.contains("dataset") {
        "io"
    } else if lc.contains("preprocess") || lc.contains("encoder")
        || lc.contains("imputer") || lc.contains("scaler")
        || lc.contains("normaliz") || lc.contains("transform")
        || lc.contains("decomposition") {
        "preprocessor"
    } else if lc.contains("ensemble") || lc.contains("classif")
        || lc.contains("regress") || lc.contains("svm")
        || lc.contains("linear_model") || lc.contains("tree")
        || lc.contains("naive_bayes") || lc.contains("neighbors") {
        "estimator"
    } else if lc.contains("model_selection") || lc.contains("split") {
        "splitter"
    } else {
        "operator"
    }
}

async fn seed_session(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let uid = event.uid.clone().or_else(|| payload_str(event, "uid"));
    let session = event
        .session
        .clone()
        .or_else(|| payload_str(event, "session"));
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };
    if uid.is_empty() || session.is_empty() {
        return Ok(());
    }

    // ── Pull session meta (Phase 1 source) ───────────────────────────
    let meta_key = keys::session_meta(&session);
    let mut conn = state.redis.clone();
    let raw: Option<String> = conn.get(&meta_key).await?;
    let Some(raw) = raw else {
        // No meta — nothing to replay. The python handler emits
        // ``SessionNotFound`` here; we stay quiet because the python
        // path will fire that on the same event.
        return Ok(());
    };
    let meta: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(err) => {
            tracing::warn!(session, %err, "session meta JSON parse failed; skipping seed");
            return Ok(());
        }
    };

    let stream_key = keys::ws_stream(&uid, &session);

    // -----------------------------------------------------------------
    // PHASE 1 — derive everything from the meta we already have.
    // -----------------------------------------------------------------
    let pipeline_history = meta
        .get("pipelineHistory")
        .cloned()
        .unwrap_or(json!({}));
    push_event(
        &mut conn,
        &stream_key,
        "state/pipeline",
        &serde_json::to_string(&pipeline_history)?,
        "json",
    )
    .await;

    let dataset = meta.get("dataset").cloned().unwrap_or(json!({}));
    push_event(
        &mut conn,
        &stream_key,
        "state/dataset",
        &serde_json::to_string(&dataset)?,
        "json",
    )
    .await;

    if let Some(target) = dataset.get("target").cloned() {
        if !target.is_null() {
            push_event(
                &mut conn,
                &stream_key,
                "state/target",
                &serde_json::to_string(&target)?,
                "json",
            )
            .await;
        }
    }

    if let Some(last_run) = meta.get("lastRun").cloned() {
        if !last_run.is_null() {
            push_event(
                &mut conn,
                &stream_key,
                "state/lastRun",
                &serde_json::to_string(&last_run)?,
                "json",
            )
            .await;
        }
    }

    // selected-task: dict (with auto badge) → json; plain name → string.
    // ALWAYS emit an event — even when the meta has no task selected —
    // so the SPA's in-memory Zustand store clears any task it picked up
    // from a previous session in the same browser tab. The frontend's
    // ``state/selected-task`` handler reads ``null`` / empty string as
    // "reset to undefined", matching the meta's actual state.
    let mut emitted_task = false;
    if let Some(selected_task) = meta.get("selectedDataScienceTask") {
        if let Some(obj) = selected_task.as_object() {
            if obj.get("name").and_then(|v| v.as_str()).is_some() {
                if obj.get("auto").and_then(|v| v.as_bool()).unwrap_or(false) {
                    push_event(
                        &mut conn,
                        &stream_key,
                        "state/selected-task",
                        &serde_json::to_string(selected_task)?,
                        "json",
                    )
                    .await;
                    emitted_task = true;
                } else if let Some(name) =
                    obj.get("name").and_then(|v| v.as_str())
                {
                    push_event(
                        &mut conn,
                        &stream_key,
                        "state/selected-task",
                        name,
                        "string",
                    )
                    .await;
                    emitted_task = true;
                }
            }
        }
    }
    if !emitted_task {
        // Empty payload signals "no task selected" so the SPA can clear
        // a stale in-memory selection from a prior session.
        push_event(&mut conn, &stream_key, "state/selected-task", "", "string").await;
    }

    let eval_name = meta
        .get("selectedEvaluationProcedureName")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    push_event(
        &mut conn,
        &stream_key,
        "state/selected-eval",
        eval_name,
        "string",
    )
    .await;

    if let Some(custom_evals) = meta.get("EvaluationProcedures") {
        if !custom_evals.is_null()
            && custom_evals.as_array().map(|a| !a.is_empty()).unwrap_or(false)
        {
            push_event(
                &mut conn,
                &stream_key,
                "state/custom-evals",
                &serde_json::to_string(custom_evals)?,
                "json",
            )
            .await;
        }
    }

    // -----------------------------------------------------------------
    // PHASE 2 — KB catalogs. ``state/operators``, ``state/tasks``,
    // ``state/operator-params`` are read from the snapshot;
    // ``state/objectives`` + ``state/evals`` come from postgres
    // because the snapshot doesn't carry them yet.
    // -----------------------------------------------------------------
    if let Some(kb) = state.kb.load_full() {
        // state/operators
        let mut op_lines: Vec<String> = kb
            .all_operators()
            .into_iter()
            .map(|o| format!("{}:{}:{}", stable_uuid(&o.name), o.name, op_type_for(&o.name)))
            .collect();
        op_lines.sort();
        push_event(
            &mut conn,
            &stream_key,
            "state/operators",
            &op_lines.join(","),
            "list",
        )
        .await;

        // state/tasks — derived from the per-operator ``tasks`` field.
        let mut task_set: rustc_hash::FxHashSet<String> =
            rustc_hash::FxHashSet::default();
        for op in kb.all_operators() {
            for t in op.tasks.iter() {
                task_set.insert(t.clone());
            }
        }
        let mut task_lines: Vec<String> = task_set
            .into_iter()
            .map(|n| format!("{}:{}", stable_uuid(&n), n))
            .collect();
        task_lines.sort();
        push_event(
            &mut conn,
            &stream_key,
            "state/tasks",
            &task_lines.join(","),
            "list",
        )
        .await;

        // state/operator-params — JSON object keyed by FQN. Mirrors
        // the gateway catalog's build_operator_params_map but inlined
        // here to avoid pulling the gateway crate into the backend.
        let params_obj = build_operator_params_map(&kb);
        push_event(
            &mut conn,
            &stream_key,
            "state/operator-params",
            &serde_json::to_string(&Value::Object(params_obj))?,
            "json",
        )
        .await;
    }

    // state/objectives + state/evals — postgres-backed. ``doc_*``
    // collection tables hold the canonical built-in lists. Each row's
    // ``data`` JSONB carries ``name`` (and sometimes ``uuid``).
    let mut objective_uuids_by_name: std::collections::HashMap<String, String> =
        std::collections::HashMap::new();
    if let Some(pool) = state.pg.as_ref() {
        if let Ok(client) = pool.get().await {
            // Objectives. ``DISTINCT ON (name)`` collapses any
            // accidental duplicates (e.g. the same KB-derived name
            // re-seeded with a different uuid by an older bootstrap
            // run) so the SPA's catalog dropdown never shows
            // repeated entries.
            let rows = client
                .query(
                    "SELECT DISTINCT ON (data->>'name') data \
                     FROM doc_ranking_objectives \
                     ORDER BY data->>'name', created_at",
                    &[],
                )
                .await
                .unwrap_or_default();
            let mut obj_lines: Vec<String> = Vec::with_capacity(rows.len());
            for row in rows {
                let data: tokio_postgres::types::Json<Value> = row.get(0);
                if let Some(name) = data.0.get("name").and_then(|v| v.as_str()) {
                    let uuid = data
                        .0
                        .get("uuid")
                        .and_then(|v| v.as_str())
                        .map(String::from)
                        .unwrap_or_else(|| stable_uuid(name));
                    objective_uuids_by_name.insert(name.to_string(), uuid.clone());
                    obj_lines.push(format!("{}:{}", uuid, name));
                }
            }
            push_event(
                &mut conn,
                &stream_key,
                "state/objectives",
                &obj_lines.join(","),
                "list",
            )
            .await;

            // Evals — same DISTINCT ON dedup as objectives above.
            let rows = client
                .query(
                    "SELECT DISTINCT ON (data->>'name') data \
                     FROM doc_evaluation_procedures \
                     ORDER BY data->>'name', created_at",
                    &[],
                )
                .await
                .unwrap_or_default();
            let mut eval_lines: Vec<String> = Vec::with_capacity(rows.len());
            for row in rows {
                let data: tokio_postgres::types::Json<Value> = row.get(0);
                if let Some(name) = data.0.get("name").and_then(|v| v.as_str()) {
                    let uuid = data
                        .0
                        .get("uuid")
                        .and_then(|v| v.as_str())
                        .map(String::from)
                        .unwrap_or_else(|| stable_uuid(name));
                    eval_lines.push(format!("{}:{}", uuid, name));
                }
            }
            push_event(
                &mut conn,
                &stream_key,
                "state/evals",
                &eval_lines.join(","),
                "list",
            )
            .await;
        }
    }

    // state/objectives/selected — from session_meta.rankingObjectives,
    // or fall back to a context-aware default list when the meta is
    // empty (matches python's ``ensure_ranking_objectives`` in
    // ``dorian/event/helpers/lifecycle.py``). The SPA expects
    // ``uuid:name`` pairs (its handler filters out items without ``:``);
    // the previous version pushed bare uuids and the SPA dropped them
    // even when the meta did populate them.
    //
    // SCRATCH_DEFAULT_NAMES / PIPELINE_DEFAULT_NAMES mirror the python
    // constants exactly. When the meta is updated to fill defaults, we
    // also persist it back to redis so subsequent reconnects see the
    // same selection (the python helper did the same).
    const SCRATCH_DEFAULTS: &[&str] = &[
        "Good Performance On Similar Data",
        "Good General Performance",
    ];
    const PIPELINE_DEFAULTS: &[&str] = &[
        "Previously Unseen",
        "Atomic Changes",
    ];

    fn render_pair(uuid: &str, name: &str) -> String {
        format!("{uuid}:{name}")
    }

    let mut selected_pair_lines: Vec<String> = Vec::new();
    let mut selected_persist: Vec<Value> = Vec::new();

    let mut raw_arr = meta
        .get("rankingObjectives")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    // Defensive: if a session was auto-flipped to PIPELINE_DEFAULTS
    // before the ``PipelineRemoved → revert`` handler shipped, it can
    // arrive here with stale pipeline-mode objectives but an empty
    // ``pipelineHistory``. ``Previously Unseen`` / ``Atomic Changes``
    // can't be evaluated without a pipeline reference, so silently
    // discard them and let the defaults branch re-pick SCRATCH.
    let pipeline_history_empty = meta
        .get("pipelineHistory")
        .map(|v| match v {
            Value::Object(obj) => obj.is_empty(),
            Value::Array(arr) => arr.is_empty(),
            Value::Null => true,
            _ => false,
        })
        .unwrap_or(true);
    if pipeline_history_empty
        && !raw_arr.is_empty()
        && raw_arr.iter().all(|entry| {
            entry
                .get("name")
                .and_then(|n| n.as_str())
                .map(|n| PIPELINE_DEFAULTS.iter().any(|d| *d == n))
                .unwrap_or(false)
        })
    {
        raw_arr.clear();
    }

    if !raw_arr.is_empty() {
        for entry in &raw_arr {
            let name = entry.get("name").and_then(|v| v.as_str());
            let stored_uuid = entry.get("uuid").and_then(|v| v.as_str());
            let resolved: Option<(String, String)> = match (stored_uuid, name) {
                (Some(u), Some(n)) if !u.is_empty() && !n.is_empty() => {
                    Some((u.to_string(), n.to_string()))
                }
                (Some(u), None) if !u.is_empty() => Some((u.to_string(), String::new())),
                (_, Some(n)) => {
                    let uuid = objective_uuids_by_name
                        .get(n)
                        .cloned()
                        .unwrap_or_else(|| stable_uuid(n));
                    Some((uuid, n.to_string()))
                }
                _ => None,
            };
            if let Some((uuid, name)) = resolved {
                if !name.is_empty() {
                    selected_pair_lines.push(render_pair(&uuid, &name));
                }
            }
        }
    } else if !objective_uuids_by_name.is_empty() {
        // Defaults pick — preserve the python helper's behaviour of
        // choosing PIPELINE_* when the session already has a pipeline,
        // SCRATCH_* otherwise. ``has_pipeline`` is "any pipelineHistory
        // entry present"; an empty dict counts as no pipeline.
        let has_pipeline = meta
            .get("pipelineHistory")
            .map(|v| match v {
                Value::Object(obj) => !obj.is_empty(),
                Value::Array(arr) => !arr.is_empty(),
                Value::Null => false,
                _ => true,
            })
            .unwrap_or(false);
        let default_names: &[&str] = if has_pipeline {
            PIPELINE_DEFAULTS
        } else {
            SCRATCH_DEFAULTS
        };
        for name in default_names {
            if let Some(uuid) = objective_uuids_by_name.get(*name) {
                selected_pair_lines.push(render_pair(uuid, name));
                selected_persist.push(json!({"uuid": uuid, "name": name}));
            }
        }
    }

    push_event(
        &mut conn,
        &stream_key,
        "state/objectives/selected",
        &selected_pair_lines.join(","),
        "list",
    )
    .await;

    // Persist the freshly-picked defaults back to session_meta so a
    // reconnect resumes from the same selection. Skip when nothing
    // changed (raw_arr non-empty path) or when no defaults landed
    // (postgres unreachable / doc_ranking_objectives empty).
    if !selected_persist.is_empty() {
        if let Ok(mut updated) = serde_json::to_value(&meta) {
            if let Some(obj) = updated.as_object_mut() {
                obj.insert(
                    "rankingObjectives".into(),
                    Value::Array(selected_persist),
                );
                let mode = if obj
                    .get("pipelineHistory")
                    .map(|v| !v.is_null() && v.as_object().map(|m| !m.is_empty()).unwrap_or(true))
                    .unwrap_or(false)
                {
                    "pipeline_default"
                } else {
                    "scratch_default"
                };
                obj.insert("objectiveMode".into(), Value::String(mode.to_string()));
                if let Ok(raw) = serde_json::to_string(&updated) {
                    let _: redis::RedisResult<()> =
                        conn.set(&meta_key, raw).await;
                }
            }
        }
    }

    // Pending-notifications flush — used to be ``flush_pending`` in
    // ``dorian/infra/notifications.py`` called from python's
    // seed_session. Port is mechanical (LRANGE → XADD batch → DEL)
    // so the python event-bus is no longer in the reconnect path
    // for offline-accumulated notifications either.
    flush_pending_notifications(&mut conn, &uid, &session, &stream_key).await;

    Ok(())
}

/// Flush any notifications that accumulated while the user's WS was
/// down. Mirrors ``dorian.infra.notifications.flush_pending``: LRANGE
/// the per-session pending list, push it as a single
/// ``notifications/batch`` event on the WS stream, DEL the list.
async fn flush_pending_notifications(
    conn: &mut redis::aio::ConnectionManager,
    uid: &str,
    session: &str,
    stream_key: &str,
) {
    let pending_key = format!("notifications:{uid}:{session}:pending");
    let items: Vec<String> = match conn.lrange(&pending_key, 0, -1).await {
        Ok(v) => v,
        Err(err) => {
            tracing::warn!(%err, %pending_key, "flush_pending lrange failed");
            return;
        }
    };
    if items.is_empty() {
        return;
    }
    let parsed: Vec<Value> = items
        .into_iter()
        .filter_map(|raw| serde_json::from_str(&raw).ok())
        .collect();
    if parsed.is_empty() {
        let _: redis::RedisResult<()> = conn.del(&pending_key).await;
        return;
    }
    let inner = match serde_json::to_string(&Value::Array(parsed)) {
        Ok(s) => s,
        Err(err) => {
            tracing::warn!(%err, "flush_pending serialise failed");
            return;
        }
    };
    let batch_payload = json!({
        "type": "notifications/batch",
        "value": inner,
    });
    let payload_str = batch_payload.to_string();
    let res: redis::RedisResult<String> = conn
        .xadd_maxlen(
            stream_key,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[("data", payload_str.as_str())],
        )
        .await;
    if let Err(err) = res {
        tracing::warn!(%err, "flush_pending xadd failed");
        return;
    }
    let _: redis::RedisResult<()> = conn.del(&pending_key).await;
}

/// XADD wrapper. The python handler uses ``maxlen=STREAM_MAXLEN,
/// approximate=True`` which the SPA's WS send loop relies on for
/// bounded memory; matching that here keeps the Redis-side trim
/// behaviour identical.
async fn push_event(
    conn: &mut redis::aio::ConnectionManager,
    stream_key: &str,
    event: &str,
    value: &str,
    type_: &str,
) {
    let res: redis::RedisResult<String> = conn
        .xadd_maxlen(
            stream_key,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &[
                ("event", event),
                ("value", value),
                ("type", type_),
            ],
        )
        .await;
    if let Err(err) = res {
        tracing::warn!(%err, stream = stream_key, event, "xadd failed during seed");
    }
}

/// Builds the ``operator-params`` catalog map. Same shape as the
/// gateway's ``build_operator_params_map``: keyed by operator FQN,
/// each entry has ``params`` (always present, possibly empty),
/// ``methods``, ``inputs``, ``outputs``, plus optional ``interface``
/// + ``family``.
fn build_operator_params_map(
    kb: &optimizer::kb::KbSnapshot,
) -> serde_json::Map<String, Value> {
    let mut out = serde_json::Map::with_capacity(kb.all_operators().len());
    for op in kb.all_operators() {
        let params: Vec<Value> = kb
            .operator_parameters(&op.name)
            .into_iter()
            .map(|p| {
                let default_val = match p.default.as_deref() {
                    Some(d) => Value::String(d.to_string()),
                    None => Value::Null,
                };
                let mut spec = serde_json::Map::new();
                spec.insert("name".into(), Value::String(p.name));
                spec.insert("dtype".into(), Value::String(p.dtype));
                spec.insert("default".into(), default_val);
                if let Some(m) = p.method {
                    spec.insert("method".into(), Value::String(m));
                }
                Value::Object(spec)
            })
            .collect();

        let (inputs, outputs) = match kb.operator_io(&op.name) {
            Some((ins, outs)) => (
                ins.into_iter().map(io_to_json).collect::<Vec<_>>(),
                outs.into_iter().map(io_to_json).collect::<Vec<_>>(),
            ),
            None => (Vec::new(), Vec::new()),
        };

        let methods: Vec<Value> = op
            .interface
            .as_deref()
            .map(|iface| kb.method_sequence(iface))
            .unwrap_or_default()
            .into_iter()
            .map(Value::String)
            .collect();

        let mut entry = serde_json::Map::new();
        entry.insert("params".into(), Value::Array(params));
        entry.insert("methods".into(), Value::Array(methods));
        entry.insert("inputs".into(), Value::Array(inputs));
        entry.insert("outputs".into(), Value::Array(outputs));
        if let Some(iface) = op.interface.as_deref() {
            entry.insert("interface".into(), Value::String(iface.to_string()));
        }
        if let Some(family) = op.family.as_deref() {
            entry.insert("family".into(), Value::String(family.to_string()));
        }
        out.insert(op.name.clone(), Value::Object(entry));
    }
    out
}

fn io_to_json(io: optimizer::kb::types::IoSpec) -> Value {
    let mut m = serde_json::Map::new();
    m.insert("name".into(), Value::String(io.name));
    // Position is a String — numeric positions look like "0"/"1"
    // and kwarg-positioned ports carry their kwarg name. The SPA's
    // ``HandleRenderer`` already prefers ``name`` over ``position``
    // for the displayed label, so end users see semantic handles
    // regardless of whether the position is numeric or kwarg.
    m.insert("position".into(), Value::String(io.position));
    m.insert("type".into(), Value::String(io.dtype));
    Value::Object(m)
}
