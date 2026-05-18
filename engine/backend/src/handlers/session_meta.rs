//! Session-meta upsert handlers — collected here because they all
//! follow the same pattern: take an event payload, compute a
//! ``session:meta`` field update, write it back. Replaces several
//! python files (``ranking_objective.py``,
//! ``evaluation.py::handle_evaluation_procedure_selected``,
//! the simpler bits of ``data_science_task.py``) that each
//! re-implemented the same scaffolding.
//!
//! Refactor over the python design: the python had eight different
//! files whose handlers were structurally identical — read meta,
//! mutate one field, write back, emit ``SessionNotFound`` on miss.
//! The variation is the field name, the field type, and how the
//! payload maps onto it. ``MetaUpsertSpec`` captures that
//! variation declaratively; new ports usually mean adding one entry
//! to ``SPECS``.
//!
//! Handlers in *this* module touch only ``session:meta`` and
//! optionally emit ``SessionNotFound``. Anything that also writes
//! to postgres / compiles user code / hits Neo4j stays in python
//! (the python operator runtime carve-out).

use anyhow::Result;
use serde_json::{json, Value};

use crate::emit::{aemit, EmitPayload, Lane};
use crate::event::EventEnvelope;
use crate::registry::{BoxFuture, Registry};
use crate::session::{with_session_meta, SessionMetaOutcome};
use crate::state::AppState;

/// Where in the payload to read the value from. Either an exact
/// field, or a "first-of-many" lookup for handlers that accept
/// several aliased field names (the python ``EvaluationProcedureSelected``
/// did this — selectedProcedureId / procedureId / uuid / id).
type PayloadPath = &'static [&'static str];

#[derive(Clone, Copy)]
struct UpsertField {
    /// Where to read the value from on the inbound payload.
    payload_keys: PayloadPath,
    /// Where to write it on ``session:meta``.
    meta_field: &'static str,
}

struct HandlerSpec {
    event_type: &'static str,
    /// Field updates this handler performs. All fields are written
    /// in one ``with_session_meta`` round so multi-field handlers
    /// don't pay the lock twice.
    fields: &'static [UpsertField],
    /// Source tag for ``SessionNotFound`` emits.
    source: &'static str,
    /// Whether to emit ``SessionNotFound`` on missing session. Some
    /// handlers (e.g. canvas-changed-style debounced ones) are
    /// fail-silent — left ``true`` here because the ones in this
    /// module only fire on explicit user action.
    emit_session_not_found: bool,
    /// Optional event type to emit *after* the meta write commits.
    /// Replaces the in-process sequencing the python subscriber
    /// gave us for free: when rust and python consume the same
    /// stream in different consumer groups, the python rerun has
    /// to wait for rust's write or it reads stale meta. Emitting
    /// a strictly-after event lets python (or anything else)
    /// subscribe to "the meta is now reflecting your change"
    /// instead of racing against the original event.
    committed_event: Option<&'static str>,
}

const SPECS: &[HandlerSpec] = &[
    HandlerSpec {
        event_type: "RankingObjectivesChanged",
        fields: &[
            UpsertField {
                payload_keys: &["objectives"],
                meta_field: "rankingObjectives",
            },
            UpsertField {
                payload_keys: &["__literal:custom"],
                meta_field: "objectiveMode",
            },
        ],
        source: "rust-backend.handlers.ranking_objective.changed",
        emit_session_not_found: true,
        committed_event: Some("RankingObjectivesCommitted"),
    },
    HandlerSpec {
        event_type: "EvaluationProcedureSelected",
        fields: &[
            UpsertField {
                payload_keys: &["selectedProcedureId", "procedureId", "uuid", "id"],
                meta_field: "selectedEvaluationProcedureId",
            },
            UpsertField {
                payload_keys: &["selectedProcedureName", "procedureName", "name"],
                meta_field: "selectedEvaluationProcedureName",
            },
        ],
        source: "rust-backend.handlers.evaluation.selected",
        emit_session_not_found: true,
        committed_event: Some("EvaluationProcedureCommitted"),
    },
];

pub fn register(r: &mut Registry) {
    for spec in SPECS {
        let fields = spec.fields;
        let source = spec.source;
        let emit_snf = spec.emit_session_not_found;
        let committed = spec.committed_event;
        r.register(spec.event_type, move |state, event| -> BoxFuture<'_, Result<()>> {
            Box::pin(handle(state, event, fields, source, emit_snf, committed))
        });
    }
}

async fn handle(
    state: &AppState,
    event: &EventEnvelope,
    fields: &'static [UpsertField],
    source: &'static str,
    emit_snf: bool,
    committed_event: Option<&'static str>,
) -> Result<()> {
    let session = match event.session.clone() {
        Some(s) if !s.is_empty() => s,
        _ => return Ok(()),
    };

    // Collect updates from the payload upfront — closures can't borrow
    // the payload past the await of with_session_meta. Keep a copy so
    // the post-commit emit can include the field names.
    let updates: Vec<(String, Value)> = fields
        .iter()
        .filter_map(|f| {
            let value = resolve_value(&event.payload, f.payload_keys)?;
            Some((f.meta_field.to_string(), value))
        })
        .collect();
    let written_fields: Vec<String> = updates.iter().map(|(k, _)| k.clone()).collect();

    let outcome = with_session_meta(state, &session, |meta| {
        let mut data = meta.data.clone();
        let is_new = meta.is_new;
        async move {
            if is_new {
                return Ok(None);
            }
            if let Some(obj) = data.as_object_mut() {
                for (field, value) in updates {
                    obj.insert(field, value);
                }
            }
            Ok(Some(data))
        }
    })
    .await?;

    match outcome {
        SessionMetaOutcome::Missing => {
            if emit_snf {
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
        }
        SessionMetaOutcome::Updated | SessionMetaOutcome::Unchanged => {
            // Emit the strictly-after-write event so downstream
            // consumers (python attempt_recommendations etc.) read the
            // freshly-committed meta instead of racing the original
            // event. ``Unchanged`` still fires it because the caller
            // chose to write — they just happened to write the same
            // value. ``Missing`` doesn't fire because nothing was
            // committed.
            if let Some(committed_type) = committed_event {
                let payload = EmitPayload::new(
                    committed_type,
                    source,
                    json!({
                        "session": session,
                        "uid": event.uid.clone(),
                        "fields": written_fields,
                    }),
                )
                .with_envelope(
                    event.request_id.clone(),
                    event.uid.clone(),
                    Some(session),
                );
                aemit(state, Lane::Bg, payload).await?;
            }
        }
    }
    Ok(())
}

/// Walk *keys* in order; the first present key's value wins. The
/// special ``__literal:`` prefix lets a spec inject a constant
/// (used for ``objectiveMode = "custom"``-style fixed values that
/// don't come from the payload).
fn resolve_value(payload: &Value, keys: &[&str]) -> Option<Value> {
    for k in keys {
        if let Some(stripped) = k.strip_prefix("__literal:") {
            return Some(Value::String(stripped.to_string()));
        }
        if let Some(v) = payload.get(*k) {
            if !v.is_null() {
                return Some(v.clone());
            }
        }
    }
    None
}
