//! Pathway evaluator — replaces python
//! ``dorian/event/handlers/risk_pathways.py::evaluate_pathways``.
//!
//! On ``DataProfiled``, walk the KB pathway records (rust snapshot
//! already carries them) and emit ``MitigationActionsIdentified``
//! cards whenever a metric crosses a threshold for an applicable
//! model family / task. The downstream
//! ``risk_chain::handle_render_suggestion`` (already rust) renders
//! the cards on the SPA stream.
//!
//! All KB lookups go through ``state.kb`` — no python KB query
//! round-trip. Metric values are reconstructed from the SPA stream's
//! ``progress / computed`` entries (same source the python handler
//! used; the per-metafeature emit is owned by the exec-worker).

use anyhow::Result;
use redis::AsyncCommands;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

pub fn register(r: &mut Registry) {
    r.register("DataProfiled", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_evaluate_pathways(state, event))
    });
}

async fn handle_evaluate_pathways(state: &AppState, event: &EventEnvelope) -> Result<()> {
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

    let Some(kb) = state.kb.load_full() else {
        return Ok(());
    };

    let mut conn = state.redis.clone();

    // ── Collect current metric values from the SPA stream's ─────────
    // ``progress / computed`` entries. Same shape the per-metafeature
    // emit produces (event=progress, status=computed, metafeature=K,
    // value=V or {overall: V}).
    let stream = keys::ws_stream(&uid, &session);
    let entries: redis::streams::StreamReadReply = conn
        .xread_options(
            &[stream.as_str()],
            &["0"],
            &redis::streams::StreamReadOptions::default().count(500),
        )
        .await
        .unwrap_or_else(|_| redis::streams::StreamReadReply { keys: vec![] });

    let mut metric_values: HashMap<String, f64> = HashMap::new();
    for k in entries.keys {
        for entry in k.ids.iter().rev() {
            let mut event_field = String::new();
            let mut status = String::new();
            let mut metafeature = String::new();
            let mut value_str = String::new();
            for (kk, vv) in &entry.map {
                let s = match vv {
                    redis::Value::BulkString(b) => String::from_utf8_lossy(b).to_string(),
                    redis::Value::SimpleString(s) => s.clone(),
                    redis::Value::Int(i) => i.to_string(),
                    redis::Value::Double(f) => f.to_string(),
                    _ => continue,
                };
                match kk.as_str() {
                    "event" => event_field = s,
                    "status" => status = s,
                    "metafeature" => metafeature = s,
                    "value" => value_str = s,
                    _ => {}
                }
            }
            if event_field != "progress" || status != "computed" {
                continue;
            }
            if metafeature.is_empty() || value_str.is_empty() {
                continue;
            }
            // Try parsing as JSON dict ({overall: V}), JSON number, or float string.
            // Mirrors python's three-step parse.
            let parsed_v: Option<f64> = if let Ok(parsed) = serde_json::from_str::<Value>(&value_str) {
                match parsed {
                    Value::Object(m) => m
                        .get("overall")
                        .or_else(|| m.get("value"))
                        .and_then(|v| v.as_f64()),
                    Value::Number(n) => n.as_f64(),
                    _ => None,
                }
            } else {
                value_str.parse::<f64>().ok()
            };
            if let Some(v) = parsed_v {
                metric_values.entry(metafeature).or_insert(v);
            }
        }
    }
    if metric_values.is_empty() {
        return Ok(());
    }

    // ── Canvas operators + their model families ────────────────────
    let operators: Vec<String> = conn
        .smembers(keys::canvas_operators(&session))
        .await
        .unwrap_or_default();
    if operators.is_empty() {
        return Ok(());
    }
    let mut op_families: HashMap<String, Option<String>> = HashMap::new();
    let mut families_on_canvas: HashSet<String> = HashSet::new();
    for op in &operators {
        let fam = kb.model_family(op);
        if let Some(f) = fam.clone() {
            families_on_canvas.insert(f);
        }
        op_families.insert(op.clone(), fam);
    }

    // Tasks inferred from the operators that perform them.
    let mut tasks_on_canvas: HashSet<String> = HashSet::new();
    for task in &["Classification", "Regression"] {
        let task_ops = kb.operators_for_task(task);
        if task_ops.iter().any(|t| operators.contains(t)) {
            tasks_on_canvas.insert((*task).to_string());
        }
    }

    // ── Iterate pathways, emit MitigationActionsIdentified per match ──
    for pathway in kb.all_pathways() {
        let metric = pathway.metric.as_str();
        let Some(current) = metric_values.get(metric).copied() else {
            continue;
        };
        let crosses = match pathway.direction.as_str() {
            "below" => current < pathway.threshold,
            "above" => current > pathway.threshold,
            _ => false,
        };
        if !crosses {
            continue;
        }
        if !pathway.families.is_empty() {
            if !pathway
                .families
                .iter()
                .any(|f| families_on_canvas.contains(f))
            {
                continue;
            }
        }
        if let Some(task) = &pathway.task {
            if !tasks_on_canvas.contains(task) {
                continue;
            }
        }

        let target_ops: Vec<String> = if !pathway.families.is_empty() {
            operators
                .iter()
                .filter(|op| {
                    op_families
                        .get(*op)
                        .and_then(|f| f.clone())
                        .map(|f| pathway.families.contains(&f))
                        .unwrap_or(false)
                })
                .cloned()
                .collect()
        } else if !operators.is_empty() {
            vec![operators[0].clone()]
        } else {
            vec![]
        };
        let target_ops = if target_ops.is_empty() {
            vec!["dataset".to_string()]
        } else {
            target_ops
        };

        for target_op in &target_ops {
            let op_short = target_op
                .rsplit_once('.')
                .map(|(_, t)| t.to_string())
                .unwrap_or_else(|| target_op.clone());
            let family = op_families
                .get(target_op)
                .and_then(|f| f.clone())
                .unwrap_or_default();
            let desc_template = pathway.description.clone().unwrap_or_default();
            let metric_value_str = format!("{:.2}", current);
            let metric_value_pct = format!("{:.0}", current * 100.0);
            let desc = desc_template
                .replace("{operator}", &op_short)
                .replace("{family}", &family)
                .replace("{metric_value}", &metric_value_str)
                .replace("{metric_value_pct}", &metric_value_pct);

            let risk = pathway
                .risk
                .clone()
                .unwrap_or_else(|| pathway.metric.clone());
            let mut action = json!({
                "name": pathway.name,
                "short": desc,
                "long": "",
            });
            if let Some(prep) = &pathway.preprocessing {
                action.as_object_mut().unwrap().insert(
                    "preprocessing".to_string(),
                    Value::String(prep.clone()),
                );
            }
            if let Some(rep) = &pathway.replacement {
                action.as_object_mut().unwrap().insert(
                    "replacement".to_string(),
                    Value::String(rep.clone()),
                );
            }

            let payload = EmitPayload::new(
                "MitigationActionsIdentified",
                "rust-backend.handlers.pathway_evaluator",
                json!({
                    "uid": uid,
                    "session": session,
                    "operator": target_op,
                    "risk": risk,
                    "status": "actionable",
                    "actions": [action],
                    "pipeline_label": "Current pipeline",
                    "pipeline_id": "",
                    "check_message": format!(
                        "{metric}={current:.3} ({direction} {threshold})",
                        direction = pathway.direction,
                        threshold = pathway.threshold,
                    ),
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
