//! Tier-2 Redis-backed cache store — wire envelope + config.
//!
//! This module ships the *shape* of the Tier-2 store without pulling
//! in a live Redis dep. The engine crate (`engine/engine/`) owns the
//! live-connection variant — it already depends on `redis` crate at
//! the workspace level.
//!
//! What lives here:
//!
//!   * `RedisEnvelope` — wire format stored at `dorian:cache:{key}`.
//!     Matches the existing `dq:*:*` CAS envelope shape (from the
//!     Go completion handler) so consumers can unwrap either path
//!     uniformly. Storage layout:
//!
//!     ```json
//!     { "completed_at": <epoch-ms>,
//!       "result":       <payload>,
//!       "artifact":     "feature" | "statistics" | "model" | "opaque",
//!       "size_bytes":   <u64>,
//!       "compute_secs": <f64>,
//!       "op_version":   <string|null> }
//!     ```
//!
//!   * `RedisStoreConfig` — key prefix + TTL.
//!   * `REDIS_KEY_PREFIX` — default `"dorian:cache:"`.
//!
//! The actual `CacheStore` trait impl with a live connection pool is
//! implemented once upstream (in the engine crate) so this sub-crate
//! stays dep-minimal.

use serde::{Deserialize, Serialize};

use crate::{Artifact, CacheEntry, CacheKey};

/// How the bytes inside `RedisEnvelope.result` should be interpreted.
///
/// Forward-compatibility note: the field defaults to `Json` via
/// `#[serde(default)]`, so legacy entries written before this tag
/// existed (including all Tier-0 `dq:*:*` entries) parse cleanly
/// and consumers that only know about JSON keep working.
///
/// See internal design note for the full sequencing
/// of the Arrow IPC adoption.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PayloadEncoding {
    /// `result` carries the payload directly as a `serde_json::Value`.
    /// Metadata artifacts (statistics, small Feature entries) and
    /// all legacy entries use this.
    #[default]
    Json,
    /// `result` is expected to be `Value::String(<base64 Arrow IPC
    /// stream bytes>)`. Consumers decode via the `arrow-rs` crate
    /// (Rust) or `pyarrow.ipc.open_stream` (Python) with zero copy
    /// inside the decoder.
    ArrowIpc,
    /// `result` is expected to be `Value::String(<base64 raw bytes>)`
    /// with no format assumption -- framework-specific serialisation
    /// (pickled sklearn estimator, torch state_dict archive). The
    /// `artifact` field (`Model`, etc) tells the consumer how to
    /// interpret.
    OpaqueBytes,
}

pub const REDIS_KEY_PREFIX: &str = "dorian:cache:";

/// Configuration for the Redis-backed store.
#[derive(Debug, Clone)]
pub struct RedisStoreConfig {
    /// Key prefix — defaults to `dorian:cache:`. Full key is
    /// `{prefix}{hex(CacheKey)}`.
    pub key_prefix: String,
    /// TTL per entry in seconds. `None` disables TTL. Tier-2
    /// default is 7 days matching the existing interaction-log TTL.
    pub ttl_secs: Option<u64>,
    /// Max entry size in bytes; writes exceeding this are dropped
    /// with a logged warning (mirror of `DORIAN_EXEC_DLQ_MAXLEN`
    /// for the job stream).
    pub max_entry_bytes: u64,
}

impl Default for RedisStoreConfig {
    fn default() -> Self {
        RedisStoreConfig {
            key_prefix: REDIS_KEY_PREFIX.to_string(),
            ttl_secs: Some(7 * 24 * 3600),
            max_entry_bytes: 64 * 1024 * 1024, // 64 MiB — Arrow-IPC payloads can be large
        }
    }
}

impl RedisStoreConfig {
    pub fn redis_key(&self, key: &CacheKey) -> String {
        format!("{}{}", self.key_prefix, key.hex())
    }
}

/// Wire envelope for Tier-2 storage. Shape matches the existing
/// `dq:*:*` CAS envelope that the Go completion handler produces
/// today (`{"completed_at": …, "result": …}`) with the MaR-taxonomy
/// extensions: artifact type, size, compute cost, version.
///
/// Consumers of the existing `dq:*:*` path can read via
/// `.result` exactly as before; new consumers pick up the richer
/// fields.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RedisEnvelope {
    pub completed_at: i64,
    pub result: serde_json::Value,
    #[serde(default)]
    pub artifact: Artifact,
    #[serde(default)]
    pub size_bytes: u64,
    #[serde(default)]
    pub compute_secs: f64,
    #[serde(default)]
    pub op_version: Option<String>,
    /// How to interpret `result`. Defaults to `Json` via
    /// `#[serde(default)]`, so entries written before this field
    /// existed parse unchanged.
    #[serde(default)]
    pub payload_encoding: PayloadEncoding,
}

impl RedisEnvelope {
    pub fn from_entry(entry: &CacheEntry, completed_at: i64) -> Self {
        RedisEnvelope {
            completed_at,
            result: entry.payload.clone(),
            artifact: entry.artifact,
            size_bytes: entry.size_bytes,
            compute_secs: entry.compute_secs,
            op_version: None,
            payload_encoding: PayloadEncoding::Json,
        }
    }

    /// Build an envelope for an Arrow-IPC-encoded payload. Caller
    /// is responsible for passing `result` as `Value::String(<base64
    /// arrow bytes>)`; see internal design note.
    pub fn for_arrow(
        completed_at: i64,
        artifact: Artifact,
        result: serde_json::Value,
        size_bytes: u64,
        compute_secs: f64,
    ) -> Self {
        RedisEnvelope {
            completed_at,
            result,
            artifact,
            size_bytes,
            compute_secs,
            op_version: None,
            payload_encoding: PayloadEncoding::ArrowIpc,
        }
    }

    pub fn to_entry(&self, key: CacheKey) -> CacheEntry {
        CacheEntry {
            key,
            artifact: self.artifact,
            payload: self.result.clone(),
            size_bytes: self.size_bytes,
            compute_secs: self.compute_secs,
            hits: 0,
        }
    }

    /// Legacy reader: unwrap just the `result` field, matching how
    /// existing DQ handlers read `dq:*:*` keys. Preserves Tier-0
    /// compatibility.
    pub fn unwrap_result(&self) -> &serde_json::Value {
        &self.result
    }

    /// Serialise to the JSON bytes the Go handler writes.
    pub fn to_bytes(&self) -> Vec<u8> {
        serde_json::to_vec(self).unwrap_or_default()
    }

    pub fn from_bytes(bytes: &[u8]) -> Result<Self, serde_json::Error> {
        serde_json::from_slice(bytes)
    }
}

// ---------------------------------------------------------------------------
// Tests — offline (no Redis connection required)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Artifact, CacheEntry, CacheKey};
    use serde_json::json;

    #[test]
    fn default_config_has_7_day_ttl() {
        let cfg = RedisStoreConfig::default();
        assert_eq!(cfg.ttl_secs, Some(7 * 24 * 3600));
        assert_eq!(cfg.key_prefix, REDIS_KEY_PREFIX);
    }

    #[test]
    fn redis_key_has_prefix_and_hex_suffix() {
        let cfg = RedisStoreConfig::default();
        let key = CacheKey([0xab; 32]);
        let k = cfg.redis_key(&key);
        assert!(k.starts_with(REDIS_KEY_PREFIX));
        assert_eq!(k.len(), REDIS_KEY_PREFIX.len() + 64);
    }

    #[test]
    fn envelope_roundtrip() {
        let key = CacheKey([0x33; 32]);
        let entry = CacheEntry::new(key, Artifact::Feature, json!({"n": 1}), 0.1);
        let env = RedisEnvelope::from_entry(&entry, 42_000);
        let bytes = env.to_bytes();
        let restored = RedisEnvelope::from_bytes(&bytes).unwrap();
        assert_eq!(restored.artifact, Artifact::Feature);
        assert_eq!(restored.compute_secs, 0.1);
        let back = restored.to_entry(key);
        assert_eq!(back.payload, json!({"n": 1}));
    }

    #[test]
    fn legacy_envelope_parses() {
        // Existing dq:*:* shape — only completed_at + result.
        let legacy = serde_json::json!({
            "completed_at": 100,
            "result": {"ok": true}
        });
        let env: RedisEnvelope = serde_json::from_value(legacy).unwrap();
        assert_eq!(env.unwrap_result(), &serde_json::json!({"ok": true}));
        // Defaults fill in the MaR fields.
        assert_eq!(env.artifact, Artifact::Opaque);
        assert_eq!(env.size_bytes, 0);
        assert!((env.compute_secs - 0.0).abs() < 1e-9);
        // payload_encoding defaults to Json for legacy entries.
        assert_eq!(env.payload_encoding, PayloadEncoding::Json);
    }

    #[test]
    fn arrow_envelope_roundtrip() {
        let key = CacheKey([0x42; 32]);
        let bytes_b64 = "QVJST1ctSVBDLUJZVEVTLUhFUkU=";
        let env = RedisEnvelope::for_arrow(
            123,
            Artifact::Feature,
            serde_json::Value::String(bytes_b64.to_string()),
            1234,
            0.2,
        );
        let raw = env.to_bytes();
        let decoded = RedisEnvelope::from_bytes(&raw).unwrap();
        assert_eq!(decoded.payload_encoding, PayloadEncoding::ArrowIpc);
        assert_eq!(decoded.artifact, Artifact::Feature);
        assert_eq!(
            decoded.result.as_str(),
            Some(bytes_b64)
        );
        // to_entry gives back a CacheEntry whose payload is the
        // string the worker wrote — downstream consumer decodes it
        // according to payload_encoding.
        let entry = decoded.to_entry(key);
        assert_eq!(entry.payload.as_str(), Some(bytes_b64));
    }

    #[test]
    fn default_payload_encoding_is_json() {
        assert_eq!(PayloadEncoding::default(), PayloadEncoding::Json);
    }

    #[test]
    fn envelope_serialises_to_dq_compat_shape() {
        let key = CacheKey([0x55; 32]);
        let entry = CacheEntry::new(key, Artifact::Model, json!(null), 0.0);
        let env = RedisEnvelope::from_entry(&entry, 777);
        let v = serde_json::to_value(&env).unwrap();
        // Both compat fields present.
        assert_eq!(v["completed_at"], 777);
        assert!(v.get("result").is_some());
        // Extended fields present.
        assert_eq!(v["artifact"], "model");
    }
}
