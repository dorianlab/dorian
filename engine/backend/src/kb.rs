//! KB snapshot view for backend handlers.
//!
//! The optimizer crate already owns ``KbSnapshot`` (see slice #71 —
//! retiring neo4j). Backend handlers that previously imported python
//! ``dorian.knowledge.queries.get_*`` go through the snapshot
//! directly. Loaded once at lifespan start from
//! ``DORIAN_KB_SNAPSHOT`` (default ``/app/volumes/kb_snapshot.json``);
//! any handler call against the snapshot is an in-memory hash hit.
//!
//! Why the snapshot lives here and on optimizer-side both: optimizer
//! exposes the data type + queries; this module wires it into the
//! backend's lifecycle (load, share via Arc). Handlers see only the
//! ``Arc<KbSnapshot>`` on ``AppState``.

use anyhow::{Context, Result};
use std::path::PathBuf;
use std::sync::Arc;

pub use optimizer::kb::KbSnapshot;

/// Default location matching the python lifespan's autogen output.
const DEFAULT_PATH: &str = "/app/volumes/kb_snapshot.json";

/// Load the snapshot from disk. Returns ``None`` (with a warning
/// logged) when the file is missing — the backend stays runnable
/// in dev environments without the python warm-up cycle, handlers
/// that need the KB just no-op until it lands.
pub fn try_load_from_env() -> Option<Arc<KbSnapshot>> {
    let path = std::env::var("DORIAN_KB_SNAPSHOT").unwrap_or_else(|_| DEFAULT_PATH.to_string());
    match load(PathBuf::from(&path)) {
        Ok(snap) => Some(Arc::new(snap)),
        Err(e) => {
            tracing::warn!(path = %path, "KB snapshot load failed: {e:#}");
            None
        }
    }
}

fn load(path: PathBuf) -> Result<KbSnapshot> {
    let raw = std::fs::read_to_string(&path)
        .with_context(|| format!("read {}", path.display()))?;
    KbSnapshot::from_json(&raw).with_context(|| format!("parse {}", path.display()))
}
