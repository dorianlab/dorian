//! Neo4j client with connection pooling and LRU caching.
//!
//! Wraps `neo4rs::Graph` (bolt connection pool) with per-query LRU caches.
//! All queries are async and parameterized.

use std::sync::Arc;

use lru::LruCache;
use neo4rs::Graph;
use tokio::sync::Mutex;

use super::types::*;

/// Cached I/O spec pair: (inputs, outputs).
type IoCache = Mutex<LruCache<String, (Vec<IoSpec>, Vec<IoSpec>)>>;

/// Configuration for the KB client.
#[derive(Debug, Clone)]
pub struct KbConfig {
    /// Neo4j bolt URI (e.g., "bolt://localhost:7687").
    pub uri: String,
    /// Neo4j username.
    pub user: String,
    /// Neo4j password.
    pub password: String,
    /// Neo4j database name (default: "dorian").
    pub database: String,
    /// Maximum connections in the bolt pool.
    pub max_connections: u32,
}

impl Default for KbConfig {
    fn default() -> Self {
        let password =
            std::env::var("DORIAN_NEO4J_PASSWORD").unwrap_or_default();
        if password.is_empty() {
            log::warn!(
                "DORIAN_NEO4J_PASSWORD is not set — Neo4j connection will use an empty password"
            );
        }
        Self {
            uri: "bolt://localhost:7687".to_string(),
            user: "dorian".to_string(),
            password,
            database: "dorian".to_string(),
            max_connections: 25,
        }
    }
}

/// KB client with Neo4j bolt connection and LRU caches.
///
/// Thread-safe: all caches are behind `tokio::sync::Mutex`.
/// The `neo4rs::Graph` is internally arc-wrapped and cloneable.
pub struct KbClient {
    pub(crate) graph: Graph,
    pub(crate) cache_interface: Mutex<LruCache<String, Option<String>>>,
    pub(crate) cache_import_path: Mutex<LruCache<String, Option<String>>>,
    pub(crate) cache_method_seq: Mutex<LruCache<String, Vec<String>>>,
    pub(crate) cache_params: Mutex<LruCache<String, Vec<ParameterSpec>>>,
    pub(crate) cache_io: IoCache,
    pub(crate) cache_method_io: Mutex<LruCache<String, MethodIo>>,
    pub(crate) cache_attrs: Mutex<LruCache<String, Vec<String>>>,
    pub(crate) cache_risks: Mutex<LruCache<String, Vec<String>>>,
    pub(crate) cache_family: Mutex<LruCache<String, Option<String>>>,
    pub(crate) cache_task_ops: Mutex<LruCache<String, Vec<String>>>,
}

impl KbClient {
    /// Connect to Neo4j and create a new KB client.
    pub async fn connect(config: &KbConfig) -> Result<Self, neo4rs::Error> {
        let graph = Graph::new(&config.uri, &config.user, &config.password).await?;

        tracing::info!(uri = %config.uri, "connected to Neo4j KB");

        Ok(Self {
            graph,
            cache_interface: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(512).unwrap(),
            )),
            cache_import_path: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(256).unwrap(),
            )),
            cache_method_seq: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(256).unwrap(),
            )),
            cache_params: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(256).unwrap(),
            )),
            cache_io: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(128).unwrap(),
            )),
            cache_method_io: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(128).unwrap(),
            )),
            cache_attrs: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(128).unwrap(),
            )),
            cache_risks: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(256).unwrap(),
            )),
            cache_family: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(256).unwrap(),
            )),
            cache_task_ops: Mutex::new(LruCache::new(
                std::num::NonZeroUsize::new(512).unwrap(),
            )),
        })
    }

    /// Get a reference to the underlying Neo4j graph for direct queries.
    pub fn graph(&self) -> &Graph {
        &self.graph
    }

    /// Invalidate all caches (e.g., after KB seeding).
    pub async fn invalidate_caches(&self) {
        self.cache_interface.lock().await.clear();
        self.cache_import_path.lock().await.clear();
        self.cache_method_seq.lock().await.clear();
        self.cache_params.lock().await.clear();
        self.cache_io.lock().await.clear();
        self.cache_method_io.lock().await.clear();
        self.cache_attrs.lock().await.clear();
        self.cache_risks.lock().await.clear();
        self.cache_family.lock().await.clear();
        self.cache_task_ops.lock().await.clear();
        tracing::info!("KB caches invalidated");
    }

    /// Get cache statistics for monitoring.
    pub async fn cache_stats(&self) -> Vec<(&'static str, usize, usize)> {
        vec![
            ("interface", self.cache_interface.lock().await.len(), 512),
            ("import_path", self.cache_import_path.lock().await.len(), 256),
            ("method_seq", self.cache_method_seq.lock().await.len(), 256),
            ("params", self.cache_params.lock().await.len(), 256),
            ("io", self.cache_io.lock().await.len(), 128),
            ("method_io", self.cache_method_io.lock().await.len(), 128),
            ("attrs", self.cache_attrs.lock().await.len(), 128),
            ("risks", self.cache_risks.lock().await.len(), 256),
            ("family", self.cache_family.lock().await.len(), 256),
            ("task_ops", self.cache_task_ops.lock().await.len(), 512),
        ]
    }
}

/// Wrap KbClient in Arc for shared ownership across async tasks.
pub type SharedKbClient = Arc<KbClient>;
