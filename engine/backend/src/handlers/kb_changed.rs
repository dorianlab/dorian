//! ``KBChanged`` handler — the rust-side broadcast half of what
//! ``dorian/event/handlers/session.py::handle_kb_changed`` does.
//!
//! Split of responsibilities with the still-python handler:
//!
//! - **Python side keeps:** clearing the ``@lru_cache``-wrapped
//!   query layer (``dorian.knowledge.queries``) + rebuilding the
//!   python ``_catalog_cache`` + regenerating the on-disk
//!   ``kb_snapshot.json``. None of that has a rust equivalent
//!   today; the python side has to invalidate its own caches or
//!   serve stale.
//! - **Rust side (this handler):** hot-reload the on-disk snapshot
//!   into ``state.kb`` (atomic ``ArcSwap``) so the next session_seed
//!   sees the new operator/task/objective/eval set, then push the
//!   refreshed catalog to every currently-connected SPA via the
//!   per-session redis stream so users don't have to reconnect to
//!   pick up the change.
//!
//! Active-connections set is the canonical
//! ``dorian:active_connections`` membership the python WS handler
//! and the rust gateway WS endpoint both maintain. We iterate the
//! set, parse each ``{uid}:{session}`` member, and XADD the same
//! Phase-2 catalog events session_seed produces on connect.
//! Order matters: snapshot reload first, broadcast second, so the
//! catalogs the broadcast carries match what new connections will
//! see.

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::Value;
use std::collections::BTreeMap;

use crate::event::EventEnvelope;
use crate::kb;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 100_000;
const ACTIVE_CONNECTIONS_KEY: &str = "dorian:active_connections";

pub fn register(r: &mut Registry) {
    r.register("KBChanged", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle(state, event))
    });
}

async fn handle(state: &AppState, _event: &EventEnvelope) -> Result<()> {
    // 1. Reload the snapshot from disk. The python side rewrites
    //    the snapshot file as part of its own KBChanged handling
    //    (``_ensure_kb_snapshot`` in ``main.py``); by the time
    //    this rust handler runs the file should be fresh. If the
    //    reload fails (file missing / parse error), keep the
    //    previous snapshot rather than wiping ``state.kb`` — a
    //    stale-but-present snapshot is more useful to the SPA
    //    than ``None`` (which would skip all catalog state).
    let new_kb = kb::try_load_from_env();
    if new_kb.is_some() {
        state.kb.store(new_kb);
    } else {
        tracing::warn!(
            "KBChanged: snapshot file unavailable; keeping previous in-memory snapshot"
        );
    }

    // 2. Iterate active connections and push fresh catalogs.
    let mut conn = state.redis.clone();
    let members: Vec<String> = match conn.smembers(ACTIVE_CONNECTIONS_KEY).await {
        Ok(v) => v,
        Err(err) => {
            tracing::warn!(%err, "KBChanged: SMEMBERS failed; skipping broadcast");
            return Ok(());
        }
    };
    if members.is_empty() {
        return Ok(());
    }

    // Reuse the same building blocks session_seed uses so the
    // wire format is identical — operators / tasks / operator-params
    // come from the (now-reloaded) KbSnapshot; objectives + evals
    // come from postgres (doc_* collections — same source as the
    // session_seed handler).
    let kb_arc = state.kb.load_full();
    let Some(kb) = kb_arc.as_ref() else {
        return Ok(());
    };

    // Pre-build the catalog payloads once; we'll xadd them per
    // connection.
    let mut op_lines: Vec<String> = kb
        .all_operators()
        .into_iter()
        .map(|o| {
            format!(
                "{}:{}:{}",
                stable_uuid(&o.name),
                o.name,
                op_type_for(&o.name)
            )
        })
        .collect();
    op_lines.sort();
    let operators_value = op_lines.join(",");

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
    let tasks_value = task_lines.join(",");

    let params_obj = build_operator_params_map(kb);
    let operator_params_value = serde_json::to_string(&Value::Object(params_obj))?;

    // Objectives + evals from postgres.
    let mut objectives_value = String::new();
    let mut evals_value = String::new();
    if let Some(pool) = state.pg.as_ref() {
        if let Ok(client) = pool.get().await {
            let rows = client
                .query(
                    "SELECT DISTINCT ON (data->>'name') data \
                     FROM doc_ranking_objectives \
                     ORDER BY data->>'name', created_at",
                    &[],
                )
                .await
                .unwrap_or_default();
            let mut lines: Vec<String> = Vec::with_capacity(rows.len());
            for row in rows {
                let data: tokio_postgres::types::Json<Value> = row.get(0);
                if let Some(name) = data.0.get("name").and_then(|v| v.as_str()) {
                    let uuid = data
                        .0
                        .get("uuid")
                        .and_then(|v| v.as_str())
                        .map(String::from)
                        .unwrap_or_else(|| stable_uuid(name));
                    lines.push(format!("{}:{}", uuid, name));
                }
            }
            objectives_value = lines.join(",");

            let rows = client
                .query(
                    "SELECT DISTINCT ON (data->>'name') data \
                     FROM doc_evaluation_procedures \
                     ORDER BY data->>'name', created_at",
                    &[],
                )
                .await
                .unwrap_or_default();
            let mut lines: Vec<String> = Vec::with_capacity(rows.len());
            for row in rows {
                let data: tokio_postgres::types::Json<Value> = row.get(0);
                if let Some(name) = data.0.get("name").and_then(|v| v.as_str()) {
                    let uuid = data
                        .0
                        .get("uuid")
                        .and_then(|v| v.as_str())
                        .map(String::from)
                        .unwrap_or_else(|| stable_uuid(name));
                    lines.push(format!("{}:{}", uuid, name));
                }
            }
            evals_value = lines.join(",");
        }
    }

    // Push to every active session.
    let messages: [(&str, &str, &str); 5] = [
        ("state/operators", operators_value.as_str(), "list"),
        ("state/tasks", tasks_value.as_str(), "list"),
        ("state/operator-params", operator_params_value.as_str(), "json"),
        ("state/objectives", objectives_value.as_str(), "list"),
        ("state/evals", evals_value.as_str(), "list"),
    ];

    for member in members {
        let Some((uid, session)) = member.split_once(':') else {
            continue;
        };
        let stream_key = keys::ws_stream(uid, session);
        for (event, value, type_) in messages.iter() {
            let res: redis::RedisResult<String> = conn
                .xadd_maxlen(
                    stream_key.as_str(),
                    StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
                    "*",
                    &[
                        ("event", *event),
                        ("value", *value),
                        ("type", *type_),
                    ],
                )
                .await;
            if let Err(err) = res {
                tracing::warn!(%err, stream = %stream_key, "KBChanged xadd failed");
                break;
            }
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Helpers — duplicated from session_seed.rs to avoid threading a shared
// helper module through every consumer for one change. Same
// implementations; if a third caller needs them, factor into
// ``handlers::catalog_helpers``.
// ---------------------------------------------------------------------------

fn stable_uuid(name: &str) -> String {
    uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_URL, name.as_bytes())
        .simple()
        .to_string()
}

fn op_type_for(fqn: &str) -> &'static str {
    let lc = fqn.to_ascii_lowercase();
    if lc.contains("metric") || lc.contains("score") {
        "metric"
    } else if lc.contains("guardrail") {
        "guardrail"
    } else if lc.contains(".io.") || lc.contains("dataset") {
        "io"
    } else if lc.contains("preprocess")
        || lc.contains("encoder")
        || lc.contains("imputer")
        || lc.contains("scaler")
        || lc.contains("normaliz")
        || lc.contains("transform")
        || lc.contains("decomposition")
    {
        "preprocessor"
    } else if lc.contains("ensemble")
        || lc.contains("classif")
        || lc.contains("regress")
        || lc.contains("svm")
        || lc.contains("linear_model")
        || lc.contains("tree")
        || lc.contains("naive_bayes")
        || lc.contains("neighbors")
    {
        "estimator"
    } else if lc.contains("model_selection") || lc.contains("split") {
        "splitter"
    } else {
        "operator"
    }
}

fn build_operator_params_map(
    kb: &optimizer::kb::KbSnapshot,
) -> serde_json::Map<String, Value> {
    let _ = BTreeMap::<String, String>::new(); // silence unused
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
    // Position is a String — kwarg-style ports keep their kwarg name
    // here (``random_state``) instead of being collapsed onto ``0``.
    m.insert("position".into(), Value::String(io.position));
    m.insert("type".into(), Value::String(io.dtype));
    Value::Object(m)
}
