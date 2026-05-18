//! ``DataProfiled`` / ``DataExists`` → auto-detect data-science task.
//! Replaces the python ``handle_auto_task_selection``.
//!
//! Inspects the dataset's first target column to infer
//! Classification / Regression and writes the result to session
//! meta + emits ``state/selected-task`` so the frontend's task
//! picker shows an "auto-detected" badge. Respects an existing
//! user selection.
//!
//! Subscribes to BOTH events because ``import_existing_dataset``
//! emits ``DataExists`` when the underlying dataset doc has no
//! pre-computed metafeature ``profile`` (e.g. crawler ran with
//! ``--no-profile``). Auto-detection only needs the target column
//! and a file path — it re-reads the CSV — so the profile field's
//! presence shouldn't gate task inference. Without this, importing
//! an unprofiled public dataset silently disables every downstream
//! cue (task badge, eval picker, recommendations).
//!
//! Why rust: the python original used pandas (a 100+ MB GIL-bound
//! dependency) for what's structurally one-pass column inspection
//! — read column, drop nulls, check dtype, count uniques. The
//! ``csv`` crate handles this in <100 lines with no python
//! interpreter call on the hot path.

use anyhow::Result;
use redis::streams::StreamMaxlen;
use redis::AsyncCommands;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::path::Path;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::session::with_session_meta;
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 10_000;
const SOURCE: &str = "rust-backend.handlers.auto_task";
/// Mirrors python's "tiny target = classification" rule.
const SMALL_CARDINALITY_THRESHOLD: usize = 20;
const RATIO_THRESHOLD: f64 = 0.05;

pub fn register(r: &mut Registry) {
    r.register("DataProfiled", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle(state, event))
    });
    r.register("DataExists", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle(state, event))
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

/// Public so tests + AI Debugger ports can re-use the inference.
pub enum InferredTask {
    Classification(String),
    Regression(String),
    Skipped(String),
}

pub fn infer_from_target(fpath: &Path, target: &str) -> InferredTask {
    let mut rdr = match csv::ReaderBuilder::new()
        .has_headers(true)
        .from_path(fpath)
    {
        Ok(r) => r,
        Err(e) => return InferredTask::Skipped(format!("csv reader failed: {e}")),
    };

    // Locate the target column index from the header row.
    let headers = match rdr.headers() {
        Ok(h) => h.clone(),
        Err(e) => return InferredTask::Skipped(format!("csv headers: {e}")),
    };
    let Some(col_idx) = headers.iter().position(|h| h == target) else {
        return InferredTask::Skipped(format!("target column '{target}' not in header"));
    };

    let mut total: usize = 0;
    let mut numeric_total: usize = 0;
    let mut bool_total: usize = 0;
    let mut uniques: HashSet<String> = HashSet::new();

    for record in rdr.records() {
        let rec = match record {
            Ok(r) => r,
            Err(_) => continue,
        };
        let raw = rec.get(col_idx).unwrap_or("");
        let trimmed = raw.trim();
        if trimmed.is_empty()
            || trimmed.eq_ignore_ascii_case("nan")
            || trimmed.eq_ignore_ascii_case("none")
            || trimmed == "null"
        {
            continue;
        }

        total += 1;
        if trimmed.parse::<f64>().is_ok() {
            numeric_total += 1;
        }
        if matches!(
            trimmed.to_ascii_lowercase().as_str(),
            "true" | "false" | "0" | "1"
        ) {
            bool_total += 1;
        }
        // Sample uniques up to a cap so the HashSet doesn't grow
        // unbounded on a 1M-row continuous target — once we cross
        // the cap, the regression branch is effectively chosen.
        if uniques.len() <= 200 {
            uniques.insert(trimmed.to_string());
        }
    }

    if total == 0 {
        return InferredTask::Skipped("target column is empty".into());
    }

    let unique_count = uniques.len();
    // Mirror python: pure-bool target → Classification (no numeric check).
    let mostly_bool = bool_total > 0 && bool_total as f64 / total as f64 > 0.9;
    if mostly_bool {
        return InferredTask::Classification(format!(
            "target '{target}' is bool-like ({total} rows)"
        ));
    }
    let mostly_numeric = numeric_total as f64 / total as f64 > 0.95;
    if !mostly_numeric {
        return InferredTask::Classification(format!(
            "target '{target}' is non-numeric"
        ));
    }
    let ratio = unique_count as f64 / total.max(1) as f64;
    if unique_count <= SMALL_CARDINALITY_THRESHOLD || ratio <= RATIO_THRESHOLD {
        return InferredTask::Classification(format!(
            "target '{target}' has {unique_count} unique values across {total} rows"
        ));
    }
    InferredTask::Regression(format!(
        "target '{target}' is continuous ({unique_count}+ unique values)"
    ))
}

async fn handle(state: &AppState, event: &EventEnvelope) -> Result<()> {
    let uid = event
        .uid
        .clone()
        .or_else(|| payload_str(event, "uid"))
        .filter(|s| !s.is_empty());
    let session = event
        .session
        .clone()
        .or_else(|| payload_str(event, "session"))
        .filter(|s| !s.is_empty());
    let did = payload_str(event, "did");
    let (Some(uid), Some(session), Some(did)) = (uid, session, did) else {
        return Ok(());
    };

    // -- peek meta + targets without holding the lock ---------------------
    let mut conn = state.redis.clone();
    let raw: Option<String> = conn.get(keys::session_meta(&session)).await?;
    let Some(raw) = raw else {
        return Ok(());
    };
    let meta: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(_) => return Ok(()),
    };
    if meta.get("selectedDataScienceTask").is_some() {
        return Ok(()); // respect existing user selection
    }
    let dataset = meta.get("dataset").cloned().unwrap_or(Value::Null);
    let fpath = dataset
        .get("fpath")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(String::from);
    let Some(fpath) = fpath else { return Ok(()); };
    let path = std::path::PathBuf::from(&fpath);
    if !path.is_file() {
        return Ok(());
    }

    let targets_raw: Option<String> = conn
        .get(keys::dataset_target_columns(&did))
        .await
        .ok()
        .flatten();
    let targets: Vec<String> = targets_raw
        .as_deref()
        .and_then(|s| serde_json::from_str::<Vec<String>>(s).ok())
        .unwrap_or_default();
    let Some(target) = targets.into_iter().next() else {
        return Ok(());
    };

    // -- inference (off the GIL, off pandas) ------------------------------
    let inferred = tokio::task::spawn_blocking({
        let path = path.clone();
        let target = target.clone();
        move || infer_from_target(&path, &target)
    })
    .await?;

    let (task_name, reason) = match inferred {
        InferredTask::Classification(r) => ("Classification", r),
        InferredTask::Regression(r) => ("Regression", r),
        InferredTask::Skipped(r) => {
            // Same observability surface as python.
            let payload = EmitPayload::new(
                "AutoTaskSelectionSkipped",
                SOURCE,
                json!({
                    "session": session, "uid": uid, "did": did, "reason": r,
                }),
            )
            .with_envelope(
                event.request_id.clone(),
                Some(uid),
                Some(session),
            );
            let _ = aemit(state, Lane::Bg, payload).await;
            return Ok(());
        }
    };

    // -- meta tx: set selectedDataScienceTask + default eval if unset ----
    //
    // Auto-defaulting the evaluation procedure to ``Automated (Hold-out)``
    // is the right pre-selection for every supervised task that picks
    // up via auto-task (Classification + Regression families). The user
    // can still override with one click; the win is that a fresh-import
    // session walks straight to the recommendations panel instead of
    // sitting with two un-answered picker questions in the sidebar.
    // Only writes when neither selection field is set, so a user who
    // already chose K-fold CV doesn't get clobbered.
    let task_for_meta = task_name.to_string();
    let reason_for_meta = reason.clone();
    let _ = with_session_meta(state, &session, |meta| async move {
        if meta.is_new {
            return Ok(None);
        }
        let mut data = meta.data;
        if let Value::Object(ref mut obj) = data {
            if obj.get("selectedDataScienceTask").is_some() {
                return Ok(None); // raced with user selection — defer
            }
            obj.insert(
                "selectedDataScienceTask".into(),
                json!({
                    "id":     null_if_none(None::<&str>),
                    "name":   task_for_meta,
                    "auto":   true,
                    "reason": reason_for_meta,
                }),
            );
            // Default eval procedure — only when neither name nor id is set.
            let eval_name_unset = obj
                .get("selectedEvaluationProcedureName")
                .map(|v| v.is_null() || v.as_str().map(|s| s.is_empty()).unwrap_or(false))
                .unwrap_or(true);
            let eval_id_unset = obj
                .get("selectedEvaluationProcedureId")
                .map(|v| v.is_null())
                .unwrap_or(true);
            if eval_name_unset && eval_id_unset {
                obj.insert(
                    "selectedEvaluationProcedureName".into(),
                    json!("Automated (Hold-out)"),
                );
                obj.insert(
                    "selectedEvaluationProcedureAuto".into(),
                    json!(true),
                );
            }
        }
        Ok(Some(data))
    })
    .await;

    // -- WS xadd state/selected-eval -------------------------------------
    // Plain string (the eval procedure NAME) so the existing
    // ``state/selected-eval`` handler in usePipelineSocket can pass
    // it straight to ``setSelectedEval`` without parse work. The
    // legacy contract is "value == display name" — sending a JSON
    // object renders the literal ``{"auto":true,"name":"..."}``
    // string in the picker, which is what tripped the previous
    // attempt.
    let eval_payload: Vec<(&str, String)> = vec![
        ("event", "state/selected-eval".into()),
        ("value", "Automated (Hold-out)".into()),
        ("type",  "string".into()),
    ];
    let _: redis::RedisResult<String> = conn
        .xadd_maxlen(
            &keys::ws_stream(&uid, &session),
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &eval_payload,
        )
        .await;

    // -- WS xadd state/selected-task --------------------------------------
    let stream_key = keys::ws_stream(&uid, &session);
    let value_obj = json!({
        "name":   task_name,
        "auto":   true,
        "reason": reason,
    });
    let payload: Vec<(&str, String)> = vec![
        ("event", "state/selected-task".into()),
        ("value", serde_json::to_string(&value_obj)?),
        ("type",  "json".into()),
    ];
    let _: String = conn
        .xadd_maxlen(
            &stream_key,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &payload,
        )
        .await?;

    // -- re-emit DataScienceTaskSelected so the operator-list filter
    //    + recommendations downstream pick it up. ------------------------
    let task_selected = EmitPayload::new(
        "DataScienceTaskSelected",
        SOURCE,
        json!({
            "uid":     uid,
            "session": session,
            "payload": {
                "taskName": task_name,
                "auto":     true,
                "reason":   reason,
            }
        }),
    )
    .with_envelope(
        event.request_id.clone(),
        Some(uid.clone()),
        Some(session.clone()),
    );
    let _ = aemit(state, Lane::User, task_selected).await;

    let auto_done = EmitPayload::new(
        "AutoTaskSelected",
        SOURCE,
        json!({
            "session": session, "uid": uid, "did": did,
            "task": task_name, "reason": reason,
        }),
    )
    .with_envelope(
        event.request_id.clone(),
        Some(uid),
        Some(session),
    );
    let _ = aemit(state, Lane::Bg, auto_done).await;

    Ok(())
}

fn null_if_none<T: serde::Serialize>(v: Option<T>) -> Value {
    match v {
        Some(x) => serde_json::to_value(x).unwrap_or(Value::Null),
        None => Value::Null,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_csv(content: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        f.write_all(content.as_bytes()).unwrap();
        f.flush().unwrap();
        f
    }

    #[test]
    fn classification_for_string_target() {
        let f = write_csv("a,b,target\n1,2,red\n3,4,blue\n5,6,red\n");
        match infer_from_target(f.path(), "target") {
            InferredTask::Classification(_) => {}
            _ => panic!("expected classification"),
        }
    }

    #[test]
    fn classification_for_low_cardinality_numeric() {
        let mut s = String::from("x,target\n");
        for i in 0..100 {
            s.push_str(&format!("{i},{}\n", i % 3));
        }
        let f = write_csv(&s);
        match infer_from_target(f.path(), "target") {
            InferredTask::Classification(_) => {}
            _ => panic!("expected classification (3 classes / 100 rows)"),
        }
    }

    #[test]
    fn regression_for_high_cardinality_numeric() {
        let mut s = String::from("x,target\n");
        for i in 0..1000 {
            s.push_str(&format!("{i},{}.{}\n", i, i * 7 % 1000));
        }
        let f = write_csv(&s);
        match infer_from_target(f.path(), "target") {
            InferredTask::Regression(_) => {}
            other => panic!("expected regression, got {:?}", match other {
                InferredTask::Classification(_) => "classification",
                InferredTask::Skipped(_) => "skipped",
                _ => "regression"
            }),
        }
    }

    #[test]
    fn skipped_when_target_missing_from_header() {
        let f = write_csv("a,b,c\n1,2,3\n");
        match infer_from_target(f.path(), "missing") {
            InferredTask::Skipped(_) => {}
            _ => panic!("expected skipped"),
        }
    }

    #[test]
    fn skipped_when_target_all_null() {
        let f = write_csv("x,target\n1,\n2,\n3,nan\n");
        match infer_from_target(f.path(), "target") {
            InferredTask::Skipped(_) => {}
            _ => panic!("expected skipped"),
        }
    }
}
