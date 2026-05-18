//! Shared state carried between request handlers.
//!
//! At this skeleton stage the state only owns a Redis connection
//! manager. Subsequent commits add: HTTP client for the Python
//! backend reverse proxy, HMAC verifier, WebSocket broadcaster,
//! session store handle. All go here so handlers take a single
//! ``State<AppState>`` extractor rather than juggling extractor
//! combinatorics per-route.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use redis::aio::ConnectionManager;

use optimizer::kb::KbSnapshot;

use crate::config::GatewayConfig;

#[derive(Clone)]
pub struct AppState {
    pub inner: Arc<AppStateInner>,
}

pub struct AppStateInner {
    pub config: GatewayConfig,
    pub redis: ConnectionManager,
    pub http_client: reqwest::Client,
    /// Read-only KB snapshot used by the catalog handlers
    /// (``/operators``, ``/tasks``, ``/objectives``, ``/evals``,
    /// ``/operator-params``). ``None`` when the snapshot file is
    /// absent — those routes 503 in that case rather than silently
    /// falling back to the python catalog.
    pub kb: Option<Arc<KbSnapshot>>,
}

impl AppState {
    pub async fn new(cfg: &GatewayConfig) -> Result<Self> {
        let client = redis::Client::open(cfg.redis_url.clone())?;
        let redis = ConnectionManager::new(client).await?;
        // Shared reqwest client — reuses connections across proxied
        // requests. Timeouts bounded so a wedged backend can't wedge
        // every caller.
        let http_client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .pool_idle_timeout(Duration::from_secs(90))
            .build()?;
        let kb = load_kb_snapshot();
        Ok(Self {
            inner: Arc::new(AppStateInner {
                config: cfg.clone(),
                redis,
                http_client,
                kb,
            }),
        })
    }
}

/// Load the KB snapshot from ``DORIAN_KB_SNAPSHOT`` (default
/// ``/app/volumes/kb_snapshot.json``). Mirrors the python +
/// rust-backend behaviour — None on missing file, with a warn log.
fn load_kb_snapshot() -> Option<Arc<KbSnapshot>> {
    let path = std::env::var("DORIAN_KB_SNAPSHOT")
        .unwrap_or_else(|_| "/app/volumes/kb_snapshot.json".to_string());
    let pb = PathBuf::from(&path);
    match std::fs::read_to_string(&pb) {
        Ok(raw) => match KbSnapshot::from_json(&raw) {
            Ok(snap) => Some(Arc::new(snap)),
            Err(e) => {
                tracing::warn!(path = %path, "KB snapshot parse failed: {e:#}");
                None
            }
        },
        Err(e) => {
            tracing::warn!(path = %path, "KB snapshot load failed: {e:#}");
            None
        }
    }
}
