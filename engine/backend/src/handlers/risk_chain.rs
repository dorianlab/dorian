//! AI Debugger chain — first hop only this turn:
//! ``identify_risks`` (TaskIdentified → PotentialRiskIdentified).
//!
//! Replaces ``dorian/event/handlers/risk_debugger.py::identify_risks``.
//! Pure KB lookup + emit — no python compute. Subsequent hops
//! (``identify_mitigations`` → ``render_suggestion`` →
//! ``apply_mitigation``) stay python-subscribed for now and consume
//! the ``PotentialRiskIdentified`` we emit. Each new hop ports as a
//! standalone handler in this module.
//!
//! The KB snapshot's ``operator_risks(fqn)`` was reading
//! ``out(op, "checks_for")`` — wrong predicate (that's check→risk,
//! not operator→risk). Fixed to ``might_introduce`` in the same
//! commit; before this fix the rust port would have returned an
//! empty risk list for every operator.

use anyhow::Result;
use redis::AsyncCommands;
use redis::streams::StreamMaxlen;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::sync::{Mutex as StdMutex, OnceLock};
use std::time::Duration;
use tokio::task::JoinHandle;
use uuid::Uuid;

use optimizer::risk::{builtin_strategies, MitigationStrategy};

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 100_000;

/// Static strategy registry. Constructed once at first access and
/// reused — strategies are stateless. Adding a new strategy is one
/// line in ``optimizer::risk::strategies::builtin_strategies``.
fn strategies() -> &'static [Box<dyn MitigationStrategy>] {
    static STRATEGIES: OnceLock<Vec<Box<dyn MitigationStrategy>>> = OnceLock::new();
    STRATEGIES.get_or_init(builtin_strategies)
}

pub fn register(r: &mut Registry) {
    r.register("TaskIdentified", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_task_identified(state, event))
    });
    r.register("PotentialRiskIdentified", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_identify_mitigations(state, event))
    });
    r.register("RiskIdentified", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_identify_mitigations(state, event))
    });
    r.register("MitigationActionsIdentified", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_render_suggestion(state, event))
    });
    // SuggestionInteraction / DataMitigationDecision → persist the
    // user's accept/reject decision and emit
    // SuggestionAccepted/Rejected. The downstream apply_mitigation
    // handler (DAG rewrite) stays python until that engine ports.
    r.register("SuggestionInteraction", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_suggestion_interaction(state, event))
    });
    r.register("DataMitigationDecision", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_data_mitigation_decision(state, event))
    });
    // PipelineNodeAdded → debounced batch trigger of identify_risks.
    // Mirrors python ``identify_operator_risks`` but the debounce
    // state lives in this rust process (per-session HashMap +
    // JoinHandle), not in python's in-process dict.
    r.register("PipelineNodeAdded", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_operator_dropped_debounce(state, event))
    });
}

// ---------------------------------------------------------------------------
// Debounced risk analysis (replaces python identify_operator_risks)
// ---------------------------------------------------------------------------

/// Quiet-window before the risk-analysis batch fires. Mirrors
/// python's ``_RISK_DEBOUNCE_SECONDS = 0.3``.
const RISK_DEBOUNCE: Duration = Duration::from_millis(300);

#[derive(Default)]
struct DebounceState {
    /// Pending operator FQNs per session.
    pending: HashMap<String, HashSet<String>>,
    /// Latest uid per session (last writer wins; same user pattern as python).
    uids: HashMap<String, String>,
    /// Active debounce task per session — abort()ed on every fresh
    /// PipelineNodeAdded so the timer effectively resets.
    tasks: HashMap<String, JoinHandle<()>>,
}

fn debounce_state() -> &'static StdMutex<DebounceState> {
    static STATE: OnceLock<StdMutex<DebounceState>> = OnceLock::new();
    STATE.get_or_init(|| StdMutex::new(DebounceState::default()))
}

async fn handle_operator_dropped_debounce(
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
    let op_name = inner
        .get("nodeName")
        .or_else(|| inner.get("name"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if uid.is_empty() || session.is_empty() || op_name.is_empty() || !op_name.contains('.') {
        return Ok(());
    }

    // Update accumulator + cancel any in-flight timer.
    {
        let mut s = debounce_state().lock().unwrap();
        s.pending
            .entry(session.clone())
            .or_default()
            .insert(op_name);
        s.uids.insert(session.clone(), uid);
        if let Some(prev) = s.tasks.remove(&session) {
            prev.abort();
        }
    }

    // Schedule the drain. ``tokio::spawn`` is fine here — the AppState
    // is Clone (Arc internally) and this future doesn't need to be
    // Send across the closure boundary.
    let state_clone = state.clone();
    let session_for_task = session.clone();
    let request_id = event.request_id.clone();
    let task = tokio::spawn(async move {
        tokio::time::sleep(RISK_DEBOUNCE).await;
        // Drain.
        let (uid, operators) = {
            let mut s = debounce_state().lock().unwrap();
            s.tasks.remove(&session_for_task);
            let uid = s.uids.remove(&session_for_task).unwrap_or_default();
            let ops = s.pending.remove(&session_for_task).unwrap_or_default();
            (uid, ops)
        };
        if uid.is_empty() || operators.is_empty() {
            return;
        }
        for op_name in &operators {
            let task_payload = EmitPayload::new(
                "TaskIdentified",
                "rust-backend.handlers.risk_chain.debounced_risk_analysis",
                json!({
                    "uid": uid,
                    "session": session_for_task,
                    "operator": op_name,
                }),
            )
            .with_envelope(
                request_id.clone(),
                Some(uid.clone()),
                Some(session_for_task.clone()),
            );
            let _ = aemit(&state_clone, Lane::Bg, task_payload).await;
        }
    });
    {
        let mut s = debounce_state().lock().unwrap();
        s.tasks.insert(session, task);
    }
    Ok(())
}

async fn handle_task_identified(state: &AppState, event: &EventEnvelope) -> Result<()> {
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
    let operator = payload
        .get("operator")
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session), Some(operator)) = (uid, session, operator) else {
        return Ok(());
    };

    let Some(kb) = state.kb.load_full() else {
        // KB snapshot not on disk — no risks to surface. Same shape
        // as python's ``load_kb`` raising and the handler swallowing.
        return Ok(());
    };

    // Pre-fetched-data version of the python helper. The rust risk
    // module's ``RiskIdentifier::identify_potential_risks`` formalises
    // the same logic but its struct surface adds boilerplate for a
    // one-liner — emit directly here.
    let risks = kb.operator_risks(&operator);
    for risk_name in risks {
        let payload = EmitPayload::new(
            "PotentialRiskIdentified",
            "rust-backend.handlers.risk_chain.identify_risks",
            json!({
                "uid": uid,
                "session": session,
                "operator": operator,
                "risk": risk_name,
                "status": "potential",
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

/// PotentialRiskIdentified / RiskIdentified → MitigationActionsIdentified.
///
/// Mirrors python ``identify_mitigations``:
///   1. Look up mitigations for the risk via ``kb.mitigations_for_risk``.
///   2. For each mitigation, fetch ``with_description`` /
///      ``with_long_description`` templates from the snapshot and fill
///      placeholders.
///   3. Append a "Direct Alternative" action when the operator has
///      same-task siblings without the risk.
///   4. Emit MitigationActionsIdentified (consumed by
///      ``handle_render_suggestion`` below).
async fn handle_identify_mitigations(state: &AppState, event: &EventEnvelope) -> Result<()> {
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
    let risk = payload
        .get("risk")
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session), Some(risk)) = (uid, session, risk) else {
        return Ok(());
    };
    let operator = payload
        .get("operator")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let status = payload
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("potential")
        .to_string();

    let Some(kb) = state.kb.load_full() else {
        return Ok(());
    };

    // Run every registered strategy and concatenate the results. Each
    // strategy is stateless and pure — see
    // ``optimizer::risk::strategies`` for the trait + builtin set.
    let mut actions: Vec<Value> = Vec::new();
    for strategy in strategies().iter() {
        for action in strategy.generate(&operator, &risk, &kb) {
            // Serialize the strategy-emitted MitigationAction into the
            // wire-format the SPA expects. Each action is one
            // suggestion card; ``source`` lets the frontend group /
            // badge cards by their generating strategy.
            actions.push(serde_json::to_value(action).unwrap_or(Value::Null));
        }
    }

    if actions.is_empty() {
        return Ok(());
    }

    let mit_payload = EmitPayload::new(
        "MitigationActionsIdentified",
        "rust-backend.handlers.risk_chain.identify_mitigations",
        json!({
            "uid": uid,
            "session": session,
            "operator": operator,
            "risk": risk,
            "status": status,
            "actions": actions,
            "pipeline_label": payload.get("pipeline_label").cloned().unwrap_or(Value::String(String::new())),
            "pipeline_id": payload.get("pipeline_id").cloned().unwrap_or(Value::String(String::new())),
            "check_message": payload.get("check_message").cloned().unwrap_or(Value::String(String::new())),
        }),
    )
    .with_envelope(event.request_id.clone(), Some(uid), Some(session));
    aemit(state, Lane::Bg, mit_payload).await?;
    Ok(())
}

/// SuggestionInteraction → persist + emit SuggestionAccepted/Rejected.
///
/// Mirrors python ``handle_suggestion_interaction`` for the
/// ``SuggestionInteraction`` event variant (the
/// ``DataMitigationDecision`` variant has its own handler below — they
/// share the persistence path but the Decision case has dataset-level
/// guards python embedded inline).
async fn handle_suggestion_interaction(state: &AppState, event: &EventEnvelope) -> Result<()> {
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

    // Frontend sometimes wraps under "payload"; mirror python.
    let inner = payload
        .get("payload")
        .and_then(|v| v.as_object())
        .unwrap_or(payload);
    let action_type = inner
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let suggestion = inner.get("suggestion").cloned().unwrap_or(Value::Null);
    let pipeline = inner.get("pipeline").cloned();

    // Persist the interaction in the session log (mirrors python's
    // ``RPUSH interactions:{uid}:{session}`` write).
    let interaction_key = format!("interactions:{uid}:{session}");
    let mut log_entry = inner.clone();
    log_entry.insert(
        "event".to_string(),
        Value::String("SuggestionInteraction".to_string()),
    );
    let log_str = serde_json::to_string(&Value::Object(log_entry)).unwrap_or_default();
    let mut conn = state.redis.clone();
    let _: redis::RedisResult<()> = conn.rpush(&interaction_key, log_str).await;

    match action_type.as_str() {
        "accept" => {
            let mut accepted = serde_json::Map::new();
            accepted.insert("uid".into(), Value::String(uid.clone()));
            accepted.insert("session".into(), Value::String(session.clone()));
            accepted.insert("suggestion".into(), suggestion);
            if let Some(pipeline) = pipeline {
                accepted.insert("pipeline".into(), pipeline);
            }
            let payload = EmitPayload::new(
                "SuggestionAccepted",
                "rust-backend.handlers.risk_chain.suggestion_interaction",
                Value::Object(accepted),
            )
            .with_envelope(event.request_id.clone(), Some(uid), Some(session));
            aemit(state, Lane::Bg, payload).await?;
        }
        "reject" => {
            let payload = EmitPayload::new(
                "SuggestionRejected",
                "rust-backend.handlers.risk_chain.suggestion_interaction",
                json!({
                    "uid": uid,
                    "session": session,
                    "suggestion": suggestion,
                }),
            )
            .with_envelope(event.request_id.clone(), Some(uid), Some(session));
            aemit(state, Lane::Bg, payload).await?;
        }
        _ => {
            // Other interaction types (upvote/downvote) are persisted but
            // don't currently emit a downstream event — same as python.
        }
    }
    Ok(())
}

/// DataMitigationDecision → SuggestionAccepted (when accept).
///
/// Closed-loop dataset-quality flow: the user clicks "accept" on a
/// dataset-level mitigation card, we synthesise a SuggestionAccepted
/// payload from the embedded ``mitigation_action`` so the downstream
/// (still-python) ``apply_mitigation`` handler runs the dataset
/// transform, creates a new dataset version, and re-enters the
/// DataExists / DataProfiled loop.
///
/// Reject / ignore decisions are no-ops (the original dataset stays
/// active) — same as the python.
async fn handle_data_mitigation_decision(
    state: &AppState,
    event: &EventEnvelope,
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

    let inner = payload
        .get("payload")
        .and_then(|v| v.as_object())
        .unwrap_or(payload);
    let did = inner.get("did").and_then(|v| v.as_str()).unwrap_or("");
    let decision = inner
        .get("decision")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if decision != "accept" || did.is_empty() {
        return Ok(());
    }

    let check_name = inner
        .get("check")
        .and_then(|v| v.as_str())
        .unwrap_or("DataQuality");
    let mit_action = inner
        .get("mitigation_action")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let action = mit_action
        .get("dataset")
        .and_then(|d| d.get("action"))
        .and_then(|v| v.as_str())
        .or_else(|| mit_action.get("action").and_then(|v| v.as_str()))
        .unwrap_or("");
    if action.is_empty() {
        return Ok(());
    }

    // Verify the dataset id matches the session's active dataset
    // (python does the same — silently drops decisions that point to a
    // stale dataset).
    let mut conn = state.redis.clone();
    let raw: Option<String> = conn.get(keys::session_meta(&session)).await.ok().flatten();
    let Some(raw) = raw else {
        return Ok(());
    };
    let meta: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Ok(()),
    };
    let active_did = meta
        .get("dataset")
        .and_then(|d| d.get("did"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if active_did != did {
        return Ok(());
    }

    let title = mit_action
        .get("title")
        .and_then(|v| v.as_str())
        .unwrap_or(action);
    let description = mit_action
        .get("description")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let suggestion = json!({
        "action": action,
        "risk": check_name,
        "task": "dataset",
        "dataset": {"action": action},
        "title": title,
        "description": description,
    });
    let payload = EmitPayload::new(
        "SuggestionAccepted",
        "rust-backend.handlers.risk_chain.data_mitigation_decision",
        json!({
            "uid": uid,
            "session": session,
            "suggestion": suggestion,
        }),
    )
    .with_envelope(event.request_id.clone(), Some(uid), Some(session));
    aemit(state, Lane::Bg, payload).await?;
    Ok(())
}

/// MitigationActionsIdentified → ``suggestion`` cards on the SPA stream.
///
/// Mirrors python ``render_suggestion``: applicability gate (operator
/// must still be on the canvas), enrichment with EU principles +
/// available checks, and per-action xadd. Unlike python this does NOT
/// pipeline xadd (rust uses ``conn.xadd_maxlen`` per call); throughput
/// is fine because the per-message size is small and the entire
/// suggestion render is bounded by ``actions.len()`` (typically <10).
async fn handle_render_suggestion(state: &AppState, event: &EventEnvelope) -> Result<()> {
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
    let risk = payload
        .get("risk")
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty());
    let (Some(uid), Some(session), Some(risk)) = (uid, session, risk) else {
        return Ok(());
    };
    let operator = payload
        .get("operator")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let status = payload
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("potential")
        .to_string();
    let actions = match payload.get("actions").and_then(|v| v.as_array()) {
        Some(a) => a.clone(),
        None => return Ok(()),
    };

    let pipeline_label = payload
        .get("pipeline_label")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let pipeline_id = payload
        .get("pipeline_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let check_message = payload
        .get("check_message")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    let mut conn = state.redis.clone();

    // Applicability gate: the dataset pseudo-operator skips the
    // SISMEMBER check, mirroring python.
    if !operator.is_empty() && operator != "dataset" {
        let on_canvas: bool = conn
            .sismember(keys::canvas_operators(&session), &operator)
            .await
            .unwrap_or(false);
        if !on_canvas {
            return Ok(());
        }
    }

    let Some(kb) = state.kb.load_full() else {
        return Ok(());
    };
    let principles = kb.principles_for_risk(&risk);
    let checks = kb.checks_for_risk(&risk);
    let principles_str = serde_json::to_string(&principles).unwrap_or_else(|_| "[]".into());
    let checks_str = serde_json::to_string(&checks).unwrap_or_else(|_| "[]".into());

    let severity = if status == "actionable" { "high" } else { "medium" };
    let source_hint = if status == "potential" { "kb" } else { "data_check" };

    let stream = keys::ws_stream(&uid, &session);
    let rewrite_slugs = state.rewrite_rule_slugs.load_full();

    for action in actions {
        let action_obj = match action.as_object() {
            Some(o) => o,
            None => continue,
        };
        let action_name = action_obj
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let short = action_obj
            .get("short")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let long = action_obj
            .get("long")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        // Strategy id (e.g. ``"kb"`` / ``"direct_alternative"``) — see
        // ``optimizer::risk::strategies``. The SPA uses this to badge
        // / group cards by their generating strategy.
        let strategy = action_obj
            .get("source")
            .and_then(|v| v.as_str())
            .unwrap_or("kb")
            .to_string();
        // Replacement-style strategies (Direct Alternative) populate
        // ``target_operator``; KB-catalog mitigations leave it absent.
        let target_operator = action_obj
            .get("target_operator")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let action_task = action_obj
            .get("task")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let has_rewrite = rewrite_slugs
            .as_ref()
            .map(|set| {
                let slug = action_name.to_ascii_lowercase().replace(' ', "-");
                set.contains(&slug) || set.contains(action_name)
            })
            .unwrap_or(false);

        // ``severity_kind`` (was a fixed "kb"/"data_check" hint) is
        // now the strategy id — same wire field, finer granularity.
        let _ = source_hint; // keeping the name available if a downstream consumer reads it
        let strategy_str = strategy.as_str();

        let sid = Uuid::new_v4().to_string();
        let _: redis::RedisResult<String> = conn
            .xadd_maxlen(
                &stream,
                StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
                "*",
                &[
                    ("event", "suggestion"),
                    ("sid", sid.as_str()),
                    ("uid", uid.as_str()),
                    ("session", session.as_str()),
                    ("task", operator.as_str()),
                    ("risk", risk.as_str()),
                    ("action", action_name),
                    ("description_short", short),
                    ("description_long", long),
                    ("principles", principles_str.as_str()),
                    ("checks", checks_str.as_str()),
                    ("severity", severity),
                    ("status", status.as_str()),
                    ("source", source_hint),
                    ("strategy", strategy_str),
                    ("target_operator", target_operator.as_str()),
                    ("action_task", action_task.as_str()),
                    ("pipeline_label", pipeline_label.as_str()),
                    ("pipeline_id", pipeline_id.as_str()),
                    ("check_message", check_message.as_str()),
                    ("has_rewrite", if has_rewrite { "true" } else { "false" }),
                ],
            )
            .await;
    }
    Ok(())
}
