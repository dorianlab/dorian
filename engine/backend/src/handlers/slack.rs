//! Slack-webhook notifications. Replaces
//! ``dorian/event/handlers/notifications.py`` + the slack-specific
//! parts of ``dorian/notifications/slack.py``.
//!
//! Subscriptions:
//!   * Error events (every entry in ``notifications.SLACK_ERROR_EVENTS``)
//!     → ``slack_on_error`` with the engine-session filter and
//!     redis-backed dedup that the python handler did.
//!   * ``FeedbackReceived``           → ``slack_on_feedback``
//!   * ``SessionCreated``             → ``slack_on_session_created``
//!   * ``InitSession``                → ``slack_on_session_init``
//!   * ``SystemBackupCompleted``      → ``slack_on_backup``
//!   * ``ContactFormSubmitted``       → ``slack_on_contact_form``
//!   * ``OnboardingTooltipFeedback``  → ``slack_on_tooltip_feedback``
//!
//! Webhook URL is read from ``DORIAN_SLACK_WEBHOOK_URL``. When that env
//! is empty (the dev default), every handler short-circuits — same
//! behaviour as the python ``_is_enabled()`` gate.
//!
//! Dedup contract: identical errors within a short window collapse
//! into one Slack post. The python handler used an in-process
//! ``asyncio.Lock``-guarded dict; the rust port stores the
//! fingerprint in redis (``slack:dedup:{hash}``, TTL 60s) so the
//! window is shared across rust-backend replicas. ``SETNX`` returns
//! ``true`` only on the first occurrence — repeats become no-ops
//! and don't roundtrip the network call.

use anyhow::Result;
use reqwest::Client;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::env;
use std::sync::OnceLock;
use std::time::Duration;

use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::state::AppState;

/// Window inside which identical errors are collapsed. Keep in sync
/// with python ``_DEDUP_WINDOW`` (60s) so cross-runtime semantics
/// don't drift while both still fire.
const DEDUP_WINDOW_S: usize = 60;

/// Engine sessions whose ``PipelineRunFailed`` events the user
/// considers EXPECTED noise — see
/// ``project_slack_engine_pipeline_failures.md``. Mirrors the python
/// filter in ``dorian/event/handlers/notifications.py``.
const ENGINE_SESSION_PREFIXES: &[&str] = &["automl", "rl", "xproduct"];

/// One shared HTTP client for the whole process. Pool reuse +
/// connection keep-alive means the Slack POST per error is just a
/// frame-roundtrip, not a TLS handshake.
static HTTP_CLIENT: OnceLock<Client> = OnceLock::new();

fn http() -> &'static Client {
    HTTP_CLIENT.get_or_init(|| {
        Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .expect("reqwest client init")
    })
}

fn webhook_url() -> Option<String> {
    let raw = env::var("DORIAN_SLACK_WEBHOOK_URL").ok()?;
    if raw.is_empty() { None } else { Some(raw) }
}

/// Categories the python ``_notify_on`` gate uses. The rust port
/// only differentiates "errors" from the rest because the dev
/// channel's ``categories`` config is set ON for everything in this
/// repo's deployment; if a future config wants finer control, this
/// is the lookup point to plumb it through.
fn category_enabled(_category: &str) -> bool {
    // Reading dynaconf-style ``slack.categories.<name>`` from rust
    // would mean adding a YAML parser dependency. Leave the gate
    // wide-open while the rust port settles; the engine-session
    // filter inside ``handle_error`` is the load-bearing one.
    true
}

/// The error event types the python registry routes to
/// ``slack_on_error``. Mirror the list verbatim so cutover doesn't
/// silently drop categories. Adding a new error event in python
/// requires adding it here too.
pub const SLACK_ERROR_EVENTS: &[&str] = &[
    "EventHandlerError",
    "BackgroundTaskFailed",
    "SessionInitFailed",
    "RecommendationEngineFailed",
    "WebsocketPayloadTooLarge",
    "WebsocketMalformedPayload",
    "WebsocketOnReceiveError",
    "WebsocketOnSendError",
    "PipelineRunFailed",
    "MetricComputeFailed",
    "KFoldPipelineBuildFailed",
    "KFoldFailed",
    "CustomEvalCompileFailed",
    "CustomEvalFailed",
    "KBQueryFailed",
    "TrialEnqueueFailed",
    "ExperimentStoreInitFailed",
    "GeneratedPipelineIndexFailed",
    "GeneratedPipelineSubmitFailed",
    "GeneratedPipelineExecutionFailed",
    "PipelineDedupLookupFailed",
    "GenerationBatchFailed",
    "SyntheticSessionSeedFailed",
    "BKTreeLoadFailed",
    "BKTreeDrainFailed",
    "PostgresPipelineLookupFailed",
    "ExtractionPersistenceFailed",
    "LeaderboardQueryFailed",
    "Neo4jQueryFailed",
    "OperatorResolutionFailed",
    "KbCypherQueryFailed",
    "LlmJsonParseFailed",
    "MetafeaturesImportFailed",
    "EvalProcedurePersistFailed",
];

/// Capture-once snapshot of the slack URL — read at first use so
/// the env doesn't need to be set before lifespan / module load.
/// ``OnceLock<Option<String>>`` is the stdlib idiom for "compute
/// once, then read forever" — no extra crate dependency.
static URL_SNAPSHOT: OnceLock<Option<String>> = OnceLock::new();

fn resolved_url() -> Option<String> {
    URL_SNAPSHOT.get_or_init(webhook_url).clone()
}

pub fn register(r: &mut Registry) {
    for ev in SLACK_ERROR_EVENTS {
        r.register(*ev, |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_error(state, event))
        });
    }
    r.register(
        "FeedbackReceived",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_feedback(state, event))
        },
    );
    r.register(
        "SessionCreated",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_session_created(state, event))
        },
    );
    r.register(
        "InitSession",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_session_init(state, event))
        },
    );
    r.register(
        "SystemBackupCompleted",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_backup(state, event))
        },
    );
    r.register(
        "ContactFormSubmitted",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_contact(state, event))
        },
    );
    r.register(
        "OnboardingTooltipFeedback",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_tooltip_feedback(state, event))
        },
    );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn payload_str_field<'a>(envelope: &'a EventEnvelope, key: &str) -> Option<&'a str> {
    envelope.payload.get(key).and_then(|v| v.as_str())
}

/// Heuristic for "this answer key is a setup-query selection, not a
/// genuine feedback question." Mirrors the frontend FeedbackModal's
/// answer-id contract:
///   * ``<question_id>:task_selection``
///   * ``<question_id>:eval_selection``
///   * ``<question_id>:objective_selection``
///   * ``dataset:<did>:<field>`` (per-dataset config: features,
///     target, quality thresholds, sensitive columns, ...)
fn is_config_answer_key(key: &str) -> bool {
    if key.starts_with("dataset:") {
        return true;
    }
    const SUFFIXES: &[&str] = &[
        ":task_selection",
        ":eval_selection",
        ":objective_selection",
    ];
    SUFFIXES.iter().any(|s| key.ends_with(s))
}

fn fingerprint(source: &str, event_type: &str, error: &str) -> String {
    let mut h = Sha256::new();
    h.update(source.as_bytes());
    h.update(b"\0");
    h.update(event_type.as_bytes());
    h.update(b"\0");
    // Trim long traces — only the first chunk discriminates
    // identical-source loops.
    let head = if error.len() > 256 { &error[..256] } else { error };
    h.update(head.as_bytes());
    let digest = h.finalize();
    format!("slack:dedup:{:x}", digest)
}

/// Returns ``true`` when this fingerprint hasn't been sent in the
/// last ``DEDUP_WINDOW_S`` seconds. ``SET NX EX`` is a single
/// roundtrip so the dedup overhead is fixed regardless of error
/// volume.
async fn should_send_error(
    redis_conn: &mut redis::aio::ConnectionManager,
    fp: &str,
) -> bool {
    let res: redis::RedisResult<Option<String>> = redis::cmd("SET")
        .arg(fp)
        .arg("1")
        .arg("NX")
        .arg("EX")
        .arg(DEDUP_WINDOW_S)
        .query_async(redis_conn)
        .await;
    matches!(res, Ok(Some(_)))
}

async fn post_to_slack(text: &str, blocks: Option<Value>) {
    let Some(url) = resolved_url() else { return };
    let mut body = serde_json::Map::new();
    body.insert("text".into(), Value::String(text.to_string()));
    if let Some(blocks) = blocks {
        body.insert("blocks".into(), blocks);
    }
    let res = http()
        .post(&url)
        .json(&Value::Object(body))
        .send()
        .await;
    match res {
        Ok(r) if r.status().is_success() => {}
        Ok(r) => {
            tracing::warn!(status = %r.status(), "slack webhook returned non-2xx");
        }
        Err(err) => {
            tracing::warn!(%err, "slack webhook post failed");
        }
    }
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn handle_error(state: &AppState, event: &EventEnvelope) -> Result<()> {
    if !category_enabled("errors") {
        return Ok(());
    }
    if resolved_url().is_none() {
        return Ok(());
    }

    // ``PipelineRunFailed`` from automl/rl/xproduct sessions is the
    // BO-trial-failure flood — see the python comment for context.
    if event.event_type == "PipelineRunFailed" {
        if let Some(sess) = event.session.as_deref() {
            if let Some(prefix) = sess.split_once(':').map(|(p, _)| p) {
                if ENGINE_SESSION_PREFIXES.contains(&prefix) {
                    return Ok(());
                }
            }
        }
    }

    let source = payload_str_field(event, "source")
        .unwrap_or("unknown")
        .to_string();
    let mut error_msg = payload_str_field(event, "error")
        .unwrap_or("unknown error")
        .to_string();
    let mut trace = payload_str_field(event, "trace").unwrap_or("").to_string();

    // Same fallback as the python handler: when the ``error`` field
    // already contains a multi-line traceback, promote it to ``trace``
    // and use just the first line as the summary.
    if trace.is_empty() && error_msg.contains('\n') {
        trace = error_msg.clone();
        let first = error_msg
            .lines()
            .last()
            .or_else(|| error_msg.lines().next())
            .unwrap_or("")
            .trim();
        if !first.is_empty() {
            error_msg = first.to_string();
        }
    }

    let fp = fingerprint(&source, &event.event_type, &error_msg);
    let mut conn = state.redis.clone();
    if !should_send_error(&mut conn, &fp).await {
        return Ok(());
    }

    let trace_short = if trace.len() > 2500 {
        &trace[trace.len() - 2500..]
    } else {
        trace.as_str()
    };

    let mut blocks = vec![
        json!({
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": format!("Error: {}", event.event_type),
                "emoji": true,
            },
        }),
        json!({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": format!("*Source:*\n`{source}`")},
                {"type": "mrkdwn", "text": format!("*Error:*\n{error_msg}")},
            ],
        }),
    ];

    let mut context_fields = Vec::new();
    if let Some(uid) = event.uid.as_deref() {
        if !uid.is_empty() {
            context_fields.push(json!({"type": "mrkdwn", "text": format!("*User:* `{uid}`")}));
        }
    }
    if let Some(sess) = event.session.as_deref() {
        if !sess.is_empty() {
            let prefix: String = sess.chars().take(12).collect();
            context_fields.push(json!({
                "type": "mrkdwn",
                "text": format!("*Session:* `{prefix}...`"),
            }));
        }
    }
    if !context_fields.is_empty() {
        blocks.push(json!({"type": "section", "fields": context_fields}));
    }

    if !trace_short.is_empty() {
        blocks.push(json!({
            "type": "section",
            "text": {"type": "mrkdwn", "text": format!("```{trace_short}```")},
        }));
    }

    let summary = format!("[ERROR] {} in {}: {}", event.event_type, source, error_msg);
    post_to_slack(&summary, Some(Value::Array(blocks))).await;
    Ok(())
}

async fn handle_feedback(_state: &AppState, event: &EventEnvelope) -> Result<()> {
    if !category_enabled("feedback") {
        return Ok(());
    }
    if resolved_url().is_none() {
        return Ok(());
    }
    let uid = payload_str_field(event, "uid").unwrap_or("?");
    let session = payload_str_field(event, "session").unwrap_or("?");
    let request_id = payload_str_field(event, "requestId").unwrap_or("?");
    let answers_value = event.payload.get("answers");

    // ── Suppress query-resolver "feedback" ────────────────────────────
    // The frontend's FeedbackModal emits FeedbackReceived on every
    // section save, including sections that are pure setup queries
    // (task / eval / dataset-config selections that have their own
    // dedicated event types: DataScienceTaskSelected,
    // EvaluationProcedureSelected, etc.). Those aren't feedback to a
    // human reader — slacking them buries the genuine feedback.
    //
    // Skip when every answer key is config-shaped:
    //   * ends with ":task_selection" / ":eval_selection" /
    //     ":objective_selection"
    //   * starts with "dataset:" (per-dataset config: features,
    //     target, quality thresholds, sensitive columns, ...)
    if let Some(answers) = answers_value.and_then(|v| v.as_object()) {
        if !answers.is_empty()
            && answers.keys().all(|k| is_config_answer_key(k))
        {
            return Ok(());
        }
    }

    let answers = answers_value
        .map(|v| serde_json::to_string_pretty(v).unwrap_or_default())
        .unwrap_or_else(|| "{}".to_string());
    let summary = format!("[Feedback] uid={uid} session={session} req={request_id}");
    let blocks = json!([
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "User feedback received", "emoji": true},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": format!("*User:* `{uid}`")},
                {"type": "mrkdwn", "text": format!("*Session:* `{session}`")},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": format!("```{answers}```")},
        },
    ]);
    post_to_slack(&summary, Some(blocks)).await;
    Ok(())
}

async fn handle_session_created(_state: &AppState, event: &EventEnvelope) -> Result<()> {
    if !category_enabled("session_lifecycle") {
        return Ok(());
    }
    if resolved_url().is_none() {
        return Ok(());
    }
    let uid = payload_str_field(event, "uid").unwrap_or("?");
    let session = payload_str_field(event, "session_id").unwrap_or("?");
    let name = payload_str_field(event, "name").unwrap_or("");
    post_to_slack(
        &format!("[SessionCreated] uid={uid} session={session} name={name}"),
        None,
    )
    .await;
    Ok(())
}

async fn handle_session_init(_state: &AppState, event: &EventEnvelope) -> Result<()> {
    if !category_enabled("session_lifecycle") {
        return Ok(());
    }
    if resolved_url().is_none() {
        return Ok(());
    }
    let uid = payload_str_field(event, "uid").unwrap_or("?");
    let session = payload_str_field(event, "session").unwrap_or("?");
    post_to_slack(
        &format!("[UserConnected] uid={uid} session={session}"),
        None,
    )
    .await;
    Ok(())
}

async fn handle_backup(_state: &AppState, event: &EventEnvelope) -> Result<()> {
    if !category_enabled("backup") {
        return Ok(());
    }
    if resolved_url().is_none() {
        return Ok(());
    }
    let path = payload_str_field(event, "path").unwrap_or("?");
    let triggered_by = payload_str_field(event, "triggered_by").unwrap_or("?");
    let errors = event
        .payload
        .get("errors")
        .and_then(|v| v.as_array())
        .map(|arr| arr.len())
        .unwrap_or(0);
    let status = if errors == 0 { "ok" } else { "with errors" };
    post_to_slack(
        &format!("[Backup {status}] path={path} triggered_by={triggered_by} errors={errors}"),
        None,
    )
    .await;
    Ok(())
}

async fn handle_contact(_state: &AppState, event: &EventEnvelope) -> Result<()> {
    if !category_enabled("contact") {
        return Ok(());
    }
    if resolved_url().is_none() {
        return Ok(());
    }
    let name = payload_str_field(event, "name").unwrap_or("?");
    let email = payload_str_field(event, "email").unwrap_or("?");
    let topic = payload_str_field(event, "topic").unwrap_or("?");
    let message = payload_str_field(event, "message").unwrap_or("");
    let blocks = json!([
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Contact form submitted", "emoji": true},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": format!("*Name:* {name}")},
                {"type": "mrkdwn", "text": format!("*Email:* {email}")},
                {"type": "mrkdwn", "text": format!("*Topic:* {topic}")},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": format!("```{message}```")},
        },
    ]);
    post_to_slack(
        &format!("[Contact] {name} <{email}>: {topic}"),
        Some(blocks),
    )
    .await;
    Ok(())
}

async fn handle_tooltip_feedback(_state: &AppState, event: &EventEnvelope) -> Result<()> {
    if !category_enabled("onboarding") {
        return Ok(());
    }
    if resolved_url().is_none() {
        return Ok(());
    }
    let uid = payload_str_field(event, "uid").unwrap_or("?");
    let tooltip_id = payload_str_field(event, "tooltip_id").unwrap_or("?");
    let vote = payload_str_field(event, "vote").unwrap_or("?");
    let dwell = event
        .payload
        .get("dwell_ms")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    post_to_slack(
        &format!("[Tooltip {vote}] uid={uid} tooltip={tooltip_id} dwell_ms={dwell}"),
        None,
    )
    .await;
    Ok(())
}
