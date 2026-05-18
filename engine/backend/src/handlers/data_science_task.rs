//! ``DataScienceTaskSelected`` handler — replaces python
//! ``handle_data_science_task_selected``. Persists the selected task
//! to ``session:meta`` and re-emits the operator list filtered by
//! the selected task so the frontend's sidebar palette refreshes.
//!
//! Skipped vs the python original: ``handle_auto_task_selection``
//! (DataProfiled-driven inference from the target column dtype)
//! stays python-side because it reads the CSV with pandas. Likewise
//! the no-op ``handle_data_science_task_added`` was already retired.

use anyhow::Result;
use redis::streams::StreamMaxlen;
use redis::AsyncCommands;
use serde_json::Value;
use uuid::Uuid;

use crate::event::EventEnvelope;
use crate::keys;
use crate::registry::{BoxFuture, Registry};
use crate::session::with_session_meta;
use crate::state::AppState;

const STREAM_MAXLEN_APPROX: usize = 10_000;

pub fn register(r: &mut Registry) {
    r.register(
        "DataScienceTaskSelected",
        |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle_selected(state, event))
        },
    );
}

fn payload_str(event: &EventEnvelope, key: &str) -> Option<String> {
    event
        .payload
        .get(key)
        .and_then(|v| v.as_str())
        .map(String::from)
        .filter(|s| !s.is_empty())
}

fn payload_bool(event: &EventEnvelope, key: &str) -> bool {
    event
        .payload
        .get(key)
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

async fn handle_selected(state: &AppState, event: &EventEnvelope) -> Result<()> {
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
    let (Some(uid), Some(session)) = (uid, session) else {
        return Ok(());
    };

    // Match python's "first non-empty wins" lookup across taskId/uuid/id.
    let selected_id = payload_str(event, "taskId")
        .or_else(|| payload_str(event, "uuid"))
        .or_else(|| payload_str(event, "id"));
    let selected_name = payload_str(event, "taskName").or_else(|| payload_str(event, "name"));
    let auto = payload_bool(event, "auto");
    let reason = payload_str(event, "reason");

    if selected_id.is_none() && selected_name.is_none() {
        // Python returns from the meta-tx without writing in this case;
        // we still need to push the (unfiltered) operator list to the
        // frontend so the sidebar reflects the no-task-selected state.
        emit_operator_list(state, &uid, &session, None).await?;
        return Ok(());
    }

    let entry_name = selected_name.clone();
    let entry_id = selected_id.clone();
    let entry_auto = auto;
    let entry_reason = reason.clone();
    let _ = with_session_meta(state, &session, |meta| async move {
        if meta.is_new {
            return Ok(None);
        }
        let mut data = meta.data;
        let mut entry = serde_json::Map::new();
        entry.insert(
            "id".into(),
            entry_id.map(Value::String).unwrap_or(Value::Null),
        );
        entry.insert(
            "name".into(),
            entry_name.map(Value::String).unwrap_or(Value::Null),
        );
        if entry_auto {
            entry.insert("auto".into(), Value::Bool(true));
            if let Some(r) = entry_reason {
                entry.insert("reason".into(), Value::String(r));
            }
        }
        if let Value::Object(ref mut obj) = data {
            obj.insert("selectedDataScienceTask".into(), Value::Object(entry));
        }
        Ok(Some(data))
    })
    .await?;

    emit_operator_list(state, &uid, &session, selected_name.as_deref()).await
}

/// XADD a comma-joined ``{uuid}:{name}`` list to the per-(uid, session)
/// WS stream. Filters by the KB's ``operators_for_task`` index when a
/// task name is provided and the KB returns a non-empty allowlist.
async fn emit_operator_list(
    state: &AppState,
    uid: &str,
    session: &str,
    task_name: Option<&str>,
) -> Result<()> {
    // Without a KB snapshot we can't enumerate operators — leave the
    // python facade to handle the rare cold-start path. (Same fault-
    // isolation as ExperimentStore-dependent objectives.)
    let kb_arc = state.kb.load_full();
    let Some(kb) = kb_arc.as_ref() else {
        tracing::warn!(
            "DataScienceTaskSelected handler: no KB snapshot loaded; skipping operator emit"
        );
        return Ok(());
    };

    let all_ops = kb.all_operators();
    let allowed: Option<Vec<String>> = task_name.and_then(|name| {
        let ops = kb.operators_for_task(name);
        if ops.is_empty() {
            None
        } else {
            Some(ops)
        }
    });

    let names: Vec<&str> = match allowed.as_ref() {
        Some(allowed) => {
            let allow_set: rustc_hash::FxHashSet<&str> =
                allowed.iter().map(String::as_str).collect();
            all_ops
                .iter()
                .filter(|o| allow_set.contains(o.name.as_str()))
                .map(|o| o.name.as_str())
                .collect()
        }
        None => all_ops.iter().map(|o| o.name.as_str()).collect(),
    };

    // Match python wire format: comma-separated ``{uuid}:{name}``.
    // uuid4 is regenerated each call in python (Operator dataclass
    // default factory), so consumers don't rely on stability.
    let value = names
        .iter()
        .map(|n| format!("{}:{}", Uuid::new_v4().simple(), n))
        .collect::<Vec<_>>()
        .join(",");

    let stream_key = keys::ws_stream(uid, session);
    let payload: Vec<(&str, String)> = vec![
        ("event", "state/operators".into()),
        ("value", value),
        ("type", "list".into()),
    ];
    let mut conn = state.redis.clone();
    let _: String = conn
        .xadd_maxlen(
            &stream_key,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &payload,
        )
        .await?;
    Ok(())
}
