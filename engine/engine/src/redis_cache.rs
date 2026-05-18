//! Tier-2 Redis-backed `CacheStore` — live connection-pool impl.
//!
//! The `cache::redis_store` module ships the wire envelope and the
//! config; this module is where the runtime lives. Placed in the
//! top-level engine crate because that's already the only workspace
//! member depending on the `redis` crate.
//!
//! Design:
//!   * Synchronous `CacheStore` trait wraps the async tokio-comp
//!     client via `block_on` on a shared runtime handle. The SDF
//!     scheduler is synchronous today; once we move to the fully-
//!     async dispatch path this can be replaced by an `AsyncCacheStore`
//!     trait without touching the scheduler.
//!   * Read path: GET, deserialise envelope, emit `Hit(entry)` with
//!     `hits=1` (we don't cross-worker increment the hit count —
//!     that lives in Tier-1 MemoryStore; Tier-2 only cares about
//!     presence).
//!   * Write path: SET with TTL if configured, payload dropped if
//!     larger than `max_entry_bytes`.
//!   * No CAS on the write path from the Rust side — the Go
//!     completion handler already does the CAS-by-completed_at for
//!     DQ keys and will for these; Rust just issues the write.
//!
//! Liveness tests ride on an env var:
//!
//!   `DORIAN_REDIS_CACHE_TEST_URL=redis://localhost:6379`
//!
//! Absence of that var → tests log-skip (never fail CI).

use std::sync::Arc;

use redis::{Client, Commands};

use cache::{CacheEntry, CacheKey, CacheOutcome, CacheStore, RedisEnvelope, RedisStoreConfig};

pub struct RedisCacheStore {
    client: Arc<Client>,
    config: RedisStoreConfig,
}

impl RedisCacheStore {
    /// Construct from a Redis URL. Uses a single multiplexed
    /// connection (redis crate's sync `Client::get_connection()`
    /// is a fresh TCP connection each call — fine for Tier-2
    /// prototype; production uses connection manager in the async
    /// path).
    pub fn new(url: &str, config: RedisStoreConfig) -> Result<Self, redis::RedisError> {
        let client = Client::open(url)?;
        Ok(RedisCacheStore {
            client: Arc::new(client),
            config,
        })
    }

    pub fn with_defaults(url: &str) -> Result<Self, redis::RedisError> {
        Self::new(url, RedisStoreConfig::default())
    }
}

impl CacheStore for RedisCacheStore {
    fn lookup(&self, key: &CacheKey) -> CacheOutcome {
        let mut conn = match self.client.get_connection() {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!(error = %e, "redis lookup: connection failed");
                return CacheOutcome::Miss;
            }
        };
        let redis_key = self.config.redis_key(key);
        let bytes: Option<Vec<u8>> = match conn.get(&redis_key) {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!(error = %e, key = %redis_key, "redis lookup: GET failed");
                return CacheOutcome::Miss;
            }
        };
        match bytes {
            Some(b) => match RedisEnvelope::from_bytes(&b) {
                Ok(env) => CacheOutcome::Hit(Arc::new(env.to_entry(*key))),
                Err(e) => {
                    tracing::warn!(error = %e, key = %redis_key, "redis lookup: envelope decode failed");
                    CacheOutcome::Miss
                }
            },
            None => CacheOutcome::Miss,
        }
    }

    fn put(&self, entry: CacheEntry) {
        if entry.size_bytes > self.config.max_entry_bytes {
            tracing::warn!(
                size = entry.size_bytes,
                limit = self.config.max_entry_bytes,
                "redis put: entry too large; dropping"
            );
            return;
        }
        let mut conn = match self.client.get_connection() {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!(error = %e, "redis put: connection failed");
                return;
            }
        };
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);
        let envelope = RedisEnvelope::from_entry(&entry, now_ms);
        let bytes = envelope.to_bytes();
        let redis_key = self.config.redis_key(&entry.key);
        let result: redis::RedisResult<()> = match self.config.ttl_secs {
            Some(ttl) => conn.set_ex(&redis_key, bytes, ttl),
            None => conn.set(&redis_key, bytes),
        };
        if let Err(e) = result {
            tracing::warn!(error = %e, key = %redis_key, "redis put: SET failed");
        }
    }

    fn len(&self) -> usize {
        // Tier-2 doesn't expose an efficient SCAN-less length. Callers
        // that need size metrics should go through admin tooling;
        // return 0 as a convention.
        0
    }
}

// ---------------------------------------------------------------------------
// Tests — live tests gated on DORIAN_REDIS_CACHE_TEST_URL.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use cache::{Artifact, CacheEntry, CacheKey};
    use serde_json::json;

    fn test_url() -> Option<String> {
        std::env::var("DORIAN_REDIS_CACHE_TEST_URL").ok()
    }

    #[test]
    fn construct_store_no_connection_required() {
        // `Client::open` does NOT connect; it just parses the URL.
        let store = RedisCacheStore::with_defaults("redis://localhost:6379").unwrap();
        // `len()` returns 0 by convention.
        assert_eq!(store.len(), 0);
    }

    #[test]
    fn put_miss_roundtrip_live() {
        let Some(url) = test_url() else {
            eprintln!("skipping: DORIAN_REDIS_CACHE_TEST_URL not set");
            return;
        };
        let store = RedisCacheStore::with_defaults(&url).unwrap();
        let key = CacheKey([0x77; 32]);
        let entry = CacheEntry::new(key, Artifact::Feature, json!({"live": true}), 0.5);
        store.put(entry);
        match store.lookup(&key) {
            CacheOutcome::Hit(e) => assert_eq!(e.artifact, Artifact::Feature),
            other => panic!("expected Hit, got {other:?}"),
        }
    }

    #[test]
    fn oversized_entry_is_dropped() {
        let mut cfg = RedisStoreConfig::default();
        cfg.max_entry_bytes = 10;
        let store = RedisCacheStore {
            client: Arc::new(Client::open("redis://localhost:6379/").unwrap()),
            config: cfg,
        };
        // Payload bigger than 10 bytes — put() logs a warning and
        // returns without touching Redis. We can't assert the log,
        // but the call must not panic.
        let key = CacheKey([0xABu8; 32]);
        let entry = CacheEntry::new(
            key,
            Artifact::Feature,
            json!({"big": "xxxxxxxxxxxxxxxxxxxx"}),
            0.0,
        );
        store.put(entry);
    }
}
