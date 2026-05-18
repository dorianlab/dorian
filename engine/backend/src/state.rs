//! Shared application state — the handle threaded through every
//! handler. Holds the redis connection (cloned per-call: the redis
//! crate's ConnectionManager is async-shareable), the parsed config,
//! and any internal-crate caches we want to keep at process scope.
//!
//! Mirrors the python ``backend.events.AppState`` in spirit but
//! only contains what rust-side handlers actually need; growth is
//! deliberate (each new port adds the field it requires).

use anyhow::{Context, Result};
use arc_swap::ArcSwapOption;
use deadpool_postgres::Pool;
use redis::aio::ConnectionManager;
use rustc_hash::FxHashSet;
use std::sync::Arc;

use optimizer::recommendation::ExperimentStore;

use crate::config::Config;
use crate::experiment_store;
use crate::kb::KbSnapshot;
use crate::pg;

#[derive(Clone)]
pub struct AppState {
    pub config: Config,
    pub redis: ConnectionManager,
    /// Postgres connection pool — ``None`` when the backend boots
    /// without postgres reachable. Handlers that need it match on
    /// ``Some(pool)`` and silently no-op otherwise (same fault-
    /// isolation contract as the python facade's lazy-init path).
    pub pg: Option<Pool>,
    /// KB snapshot — atomically hot-swappable so
    /// ``handle_kb_changed`` can reload the file after a KB
    /// mutation without restarting the binary. ``load_full()``
    /// returns ``None`` when the file isn't on disk yet
    /// (``try_load_from_env`` returned ``None``). Read sites that
    /// took ``state.kb.as_ref()`` previously now take
    /// ``state.kb.load_full()`` — both are non-blocking.
    pub kb: Arc<ArcSwapOption<KbSnapshot>>,
    /// Experiment store — dataset metafeature index + win-rate
    /// cache. ``None`` when postgres is unreachable at startup or
    /// the load query fails. The two store-backed objectives
    /// (``SimilarDataPerformance``, ``PipelinePreferenceRatio``)
    /// degrade gracefully on a missing store.
    pub experiment_store: Option<Arc<ExperimentStore>>,
    /// Mitigation slugs (lowercased, hyphenated names) for which a
    /// rewrite rule exists in the ``doc_rewrites`` postgres table.
    /// Populated once at startup; the AI Debugger's
    /// ``risk_chain::render_suggestion`` flips ``has_rewrite`` per
    /// suggestion card from this set without a per-render db hit.
    /// The slug computation matches python's
    /// ``mitigation_name.lower().replace(" ", "-")``.
    pub rewrite_rule_slugs: Arc<ArcSwapOption<FxHashSet<String>>>,
}

impl AppState {
    pub async fn new(config: Config) -> Result<Self> {
        let client = redis::Client::open(config.redis_url.as_str())
            .with_context(|| "redis client")?;
        let redis = ConnectionManager::new(client)
            .await
            .with_context(|| "redis connection")?;
        let pg = match pg::pool_from_env() {
            Ok(p) => Some(p),
            Err(e) => {
                tracing::warn!("postgres pool unavailable: {e:#}");
                None
            }
        };
        let kb = Arc::new(ArcSwapOption::from(crate::kb::try_load_from_env()));
        let experiment_store = match pg.as_ref() {
            Some(pool) => match experiment_store::load(pool).await {
                Ok(store) => {
                    tracing::info!(
                        datasets = store.size(),
                        win_rate_entries = store.win_rates.len(),
                        "experiment store loaded",
                    );
                    Some(store)
                }
                Err(e) => {
                    tracing::warn!("experiment store load failed: {e:#}");
                    None
                }
            },
            None => None,
        };

        // Pre-load mitigation rewrite-rule slugs so the AI Debugger's
        // render_suggestion can flag ``has_rewrite`` per card without
        // hitting the DB. ``doc_rewrites`` row id == slug
        // (``mitigation_name.lower().replace(' ', '-')``); fall back
        // to scanning ``data->>'name'`` for legacy rows.
        let rewrite_slugs = match pg.as_ref() {
            Some(pool) => match load_rewrite_slugs(pool).await {
                Ok(set) => {
                    tracing::info!(slug_count = set.len(), "rewrite rule slugs loaded");
                    Some(Arc::new(set))
                }
                Err(e) => {
                    tracing::warn!("rewrite slugs load failed: {e:#}");
                    None
                }
            },
            None => None,
        };
        let rewrite_rule_slugs = Arc::new(ArcSwapOption::from(rewrite_slugs));

        Ok(Self {
            config,
            redis,
            pg,
            kb,
            experiment_store,
            rewrite_rule_slugs,
        })
    }
}

/// One round-trip to ``doc_rewrites``: every row's id is the slug
/// (the seeder writes them that way); legacy rows get name-based
/// slugs computed in the same pass so the lookup matches python's
/// ``mitigation_name.lower().replace(' ', '-')`` rule for both.
async fn load_rewrite_slugs(pool: &Pool) -> Result<FxHashSet<String>> {
    let client = pool.get().await?;
    let rows = client
        .query("SELECT id, data->>'name' AS name FROM doc_rewrites", &[])
        .await?;
    let mut out = FxHashSet::default();
    for row in rows {
        let id: String = row.get(0);
        if !id.is_empty() {
            out.insert(id);
        }
        let name: Option<String> = row.get(1);
        if let Some(n) = name {
            out.insert(n.to_ascii_lowercase().replace(' ', "-"));
        }
    }
    Ok(out)
}
