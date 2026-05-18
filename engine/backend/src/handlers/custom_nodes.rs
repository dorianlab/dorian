//! Custom node / snippet / parameter upsert. Consolidates three
//! near-identical handlers from
//! ``dorian/event/handlers/custom_nodes.py`` into one parameterised
//! handler keyed on the meta-field name.
//!
//! Refactor over the python design: the original had three copy-
//! pasted functions that differed only in the ``meta`` field they
//! wrote (``customOperators`` / ``customSnippets`` /
//! ``customParameters``) and the ``SessionNotFound`` source string
//! they emitted on missing-session. One ``HandlerSpec`` plus a
//! single ``handle`` function captures the variation declaratively.

use anyhow::Result;
use serde_json::{json, Value};

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::session::{upsert_by_key, with_session_meta};
use crate::state::AppState;
use tracing::warn;

struct HandlerSpec {
    /// Event type the handler subscribes to.
    event_type: &'static str,
    /// Field name on ``session:<session>:meta`` to append the custom
    /// item into.
    meta_field: &'static str,
    /// Source tag for observability events emitted by this handler.
    source: &'static str,
}

const SPECS: &[HandlerSpec] = &[
    HandlerSpec {
        event_type: "CustomOperatorAdded",
        meta_field: "customOperators",
        source: "rust-backend.handlers.custom_nodes.operator",
    },
    HandlerSpec {
        event_type: "CustomSnippetAdded",
        meta_field: "customSnippets",
        source: "rust-backend.handlers.custom_nodes.snippet",
    },
    HandlerSpec {
        event_type: "CustomParameterAdded",
        meta_field: "customParameters",
        source: "rust-backend.handlers.custom_nodes.parameter",
    },
];

pub fn register(r: &mut Registry) {
    for spec in SPECS {
        let field = spec.meta_field;
        let source = spec.source;
        r.register(spec.event_type, move |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle(state, event, field, source))
        });
    }
}

async fn handle(
    state: &AppState,
    event: &EventEnvelope,
    meta_field: &'static str,
    source: &'static str,
) -> Result<()> {
    let session = match event.session.clone() {
        Some(s) if !s.is_empty() => s,
        _ => {
            warn!(?event.event_type, "missing session — skipping");
            return Ok(());
        }
    };
    let item = event.payload.clone();
    if !item.is_object() {
        // The python helpers tolerate non-object payloads silently;
        // this version logs once so the bad emit gets noticed.
        warn!(?event.event_type, "payload is not an object — skipping");
        return Ok(());
    }

    let outcome = with_session_meta(state, &session, |meta| {
        let mut data = meta.data.clone();
        let is_new = meta.is_new;
        async move {
            if is_new {
                // Same contract as python: don't lazy-create the
                // session record from a CustomOperatorAdded event.
                return Ok(None);
            }
            let current = data
                .get(meta_field)
                .cloned()
                .and_then(|v| v.as_array().cloned())
                .unwrap_or_default();
            let updated = upsert_by_key(current, item, "uuid");
            if let Some(obj) = data.as_object_mut() {
                obj.insert(meta_field.to_string(), Value::Array(updated));
            }
            Ok(Some(data))
        }
    })
    .await?;

    if matches!(outcome, crate::session::SessionMetaOutcome::Missing) {
        let payload = EmitPayload::new(
            "SessionNotFound",
            source,
            json!({"session": session, "uid": event.uid.clone()}),
        )
        .with_envelope(
            event.request_id.clone(),
            event.uid.clone(),
            Some(session),
        );
        aemit(state, Lane::Bg, payload).await?;
    }
    Ok(())
}
