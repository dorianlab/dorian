//! Redis key patterns. Mirrors ``dorian/infra/keys.py::RedisKeys`` but
//! as plain functions — no point translating an empty static class
//! into a Rust struct that does the same thing. Tenant prefixing is
//! omitted because the python version threads it through
//! ``contextvars`` and the only tenant code in production is the
//! default ``""`` prefix; if multi-tenancy lands later, the same
//! threading can plug in here via a ``thread_local!`` or just plumb
//! a ``&Tenant`` argument.

// Add new key patterns when a handler that uses them lands.
// Don't pre-translate the python ``RedisKeys`` API — keys with no
// callers rot when the schema shifts.

pub fn session_meta(session: &str) -> String {
    format!("session:{session}:meta")
}

pub fn session_meta_lock(session: &str) -> String {
    format!("session:{session}:meta:lock")
}

pub fn cancel_run(run_id: &str) -> String {
    format!("cancel:{run_id}")
}

/// Per-(uid, session) outgoing WS Redis stream. Frontend consumers
/// XREAD this; handlers write event lines via XADD.
pub fn ws_stream(uid: &str, session: &str) -> String {
    format!("{uid}:{session}:stream")
}

/// XREAD cursor position for the WS consumer.
pub fn cursor(uid: &str, session: &str) -> String {
    format!("{uid}:{session}:last")
}

/// SET of operator FQNs on the canvas — scopes the AI Debugger's
/// suggestion set.
pub fn canvas_operators(session: &str) -> String {
    format!("session:{session}:canvas_operators")
}

pub fn dataset_fpath(did: &str) -> String {
    format!("dataset:fpath:{did}")
}

pub fn dataset_feature_columns(did: &str) -> String {
    format!("dataset:{did}:feature_columns")
}

pub fn dataset_target_columns(did: &str) -> String {
    format!("dataset:{did}:target_columns")
}

pub fn protected_attributes(did: &str) -> String {
    format!("dataset:{did}:protected_attributes")
}

/// Redis key carrying the most recent extraction id for a session.
/// Set by the ExtractPipeline handler so downstream tools can look
/// up the user's currently-active extraction without re-parsing the
/// source. Mirrors ``RedisKeys.active_extraction(session)`` from
/// the python keys module.
pub fn active_extraction(session: &str) -> String {
    format!("extraction:active:{session}")
}
