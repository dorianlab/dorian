//! Dataset-removal handler — replaces the python
//! ``handle_dataset_removed``. Mirrors its full side-effect surface:
//! clears the ``dataset`` block from session meta, deletes the four
//! per-did Redis keys, removes the physical file (best-effort),
//! deletes the postgres ``datasets`` collection row (best-effort),
//! pushes a cleared ``state/dataset`` to the WS stream, and emits
//! ``DatasetCleared``.
//!
//! Fault-isolation contract: every step that can fail externally
//! (file unlink, postgres delete) is best-effort and logs through
//! ``tracing::warn`` rather than aborting. Same semantics as the
//! python try/except blocks.
//!
//! ``handle_dataset_uploaded`` and ``handle_dataset_imported`` are
//! intentionally NOT registered: the python equivalents were
//! pass-through ``return`` no-ops (interaction-log persistence is
//! already owned by ``handlers/interactions.rs``). Dropping the
//! subscription is the port.

use anyhow::Result;
use redis::streams::StreamMaxlen;
use redis::AsyncCommands;
use serde_json::{json, Value};
use std::path::Path;

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::keys;
use crate::pg;
use crate::registry::{BoxFuture, Registry};
use crate::session::with_session_meta;
use crate::state::AppState;

const SOURCE: &str = "rust-backend.handlers.datasets.handle_dataset_removed";
const STREAM_MAXLEN_APPROX: usize = 10_000;
const PG_DATASETS_COLLECTION: &str = "datasets";

pub fn register(r: &mut Registry) {
    r.register("DatasetRemoved", |state, event| -> BoxFuture<'_, Result<()>> {
        Box::pin(handle_removed(state, event))
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

async fn handle_removed(state: &AppState, event: &EventEnvelope) -> Result<()> {
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

    // Clear dataset + selectedDataScienceTask from session meta and
    // capture the did the meta knew about. with_session_meta returns
    // None (release without write) when meta is absent, so we use a
    // shared cell (Arc) to ferry the did out of the closure.
    let did_cell: std::sync::Arc<std::sync::Mutex<Option<String>>> =
        std::sync::Arc::new(std::sync::Mutex::new(None));
    let did_writer = did_cell.clone();

    let _outcome = with_session_meta(state, &session, |meta| async move {
        if meta.is_new {
            return Ok(None);
        }
        let mut data = meta.data;
        let did = data
            .get("dataset")
            .and_then(|d| d.get("did"))
            .and_then(|v| v.as_str())
            .map(String::from)
            .unwrap_or_default();
        if let Value::Object(ref mut obj) = data {
            obj.insert("dataset".into(), Value::Object(Default::default()));
            obj.remove("selectedDataScienceTask");
        }
        *did_writer.lock().expect("did_cell poisoned") = Some(did);
        Ok(Some(data))
    })
    .await?;

    let did = did_cell
        .lock()
        .expect("did_cell poisoned")
        .clone()
        .unwrap_or_default();

    let mut conn = state.redis.clone();

    if !did.is_empty() {
        // Read the file path before deleting the key.
        let fpath_key = keys::dataset_fpath(&did);
        let fpath: Option<String> = conn.get(&fpath_key).await.ok();

        let keys_to_delete: Vec<String> = vec![
            fpath_key,
            keys::dataset_feature_columns(&did),
            keys::dataset_target_columns(&did),
            keys::protected_attributes(&did),
        ];
        let key_refs: Vec<&str> = keys_to_delete.iter().map(String::as_str).collect();
        let _: i64 = conn.del(&key_refs).await.unwrap_or(0);

        // File unlink + best-effort parent rmdir.
        if let Some(p) = fpath.as_deref().filter(|s| !s.is_empty()) {
            let path = Path::new(p);
            let _ = tokio::fs::remove_file(path).await;
            if let Some(parent) = path.parent() {
                let _ = tokio::fs::remove_dir(parent).await;
            }
        }

        // Postgres delete. Best-effort: the python ``try/except`` was
        // a silent swallow; tracing::warn keeps it visible without
        // aborting the handler.
        if let Some(pool) = state.pg.as_ref() {
            if let Err(e) =
                pg::delete_by_id(pool, PG_DATASETS_COLLECTION, &did).await
            {
                tracing::warn!(did = %did, "datasets pg delete failed: {e:#}");
            }
        }
    }

    // Push cleared dataset state to the user stream.
    let stream_key = keys::ws_stream(&uid, &session);
    let stream_payload: Vec<(&str, String)> = vec![
        ("event", "state/dataset".into()),
        ("value", "{}".into()),
        ("type", "json".into()),
    ];
    let _: String = conn
        .xadd_maxlen(
            &stream_key,
            StreamMaxlen::Approx(STREAM_MAXLEN_APPROX),
            "*",
            &stream_payload,
        )
        .await?;

    let payload = EmitPayload::new(
        "DatasetCleared",
        SOURCE,
        json!({
            "source":  SOURCE,
            "uid":     uid,
            "session": session,
            "did":     did,
        }),
    )
    .with_envelope(
        event.request_id.clone(),
        Some(uid),
        Some(session),
    );
    let _ = aemit(state, Lane::Bg, payload).await;

    Ok(())
}
