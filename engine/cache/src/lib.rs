//! Content-addressable cache for DEM actor firings.
//!
//! Tier-1 reuse primitive — not an optimisation, a load-bearing part
//! of the scheduler. Every domain scheduler consults this cache
//! before firing an actor; on hit, the cached result is used and a
//! `NodeCacheHit` event is emitted; on miss, the actor fires and the
//! result is stored under the computed key.
//!
//! The key is `hash(op_fqn, canonicalised_params, input_key, op_version)`
//! with `input_key` propagated recursively from upstream nodes. This
//! makes identical subgraphs across different pipelines share one
//! physical firing — the RL fan-out win.
//!
//! Artifact taxonomy follows Derakhshan's MaR thesis (Ch 4): each
//! entry carries a type (feature / statistics / model) that changes
//! which downstream reuse strategy is available.
//!
//! v1 ships:
//!   * `CacheKey` — 32-byte SHA-256 digest.
//!   * `compute_key` — deterministic canonicalisation + hashing.
//!   * `Artifact` — feature / statistics / model tag.
//!   * `CacheEntry` — payload + metadata (size, compute cost, hits).
//!   * `CacheStore` trait + `MemoryStore` impl (Tier-1, in-process).
//!   * `CacheOutcome` — Hit(entry) / Miss.
//!
//! Tier-2 Redis backend and tier-3 GDSF eviction land later; the
//! trait is the plug-in point.

use std::sync::Arc;

use parking_lot::RwLock;
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use graph::dem::{ActorAnnotations, DeterminismClass};
use graph::model::{Node, Parameter};

pub mod arrow_store;
pub mod benefit;
pub mod index;
pub mod redis_store;

pub use arrow_store::{ArrowStore, ArrowStoreConfig, PayloadKind};
pub use benefit::{benefit, pick_eviction, BenefitScore, Cost, CostProfile};
pub use index::{
    cache_affinity, match_pipeline, plan_batch, BatchPlan, ExperimentGraphIndex, ReuseMatch,
};
// Legacy redis-envelope spec retained for the eventbus payload-format
// tests; the active Tier-2 store is `arrow_store::ArrowStore`. New
// callers should not reach for `redis_store` types.
pub use redis_store::{PayloadEncoding, RedisEnvelope, RedisStoreConfig, REDIS_KEY_PREFIX};

// ---------------------------------------------------------------------------
// CacheKey
// ---------------------------------------------------------------------------

/// 32-byte SHA-256 digest used as a cache key.
///
/// Value semantics — copyable, hashable, suitable as a `HashMap` key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct CacheKey(pub [u8; 32]);

impl CacheKey {
    /// Hex-encoded representation (64 chars). For logging and Redis keys.
    pub fn hex(&self) -> String {
        let mut out = String::with_capacity(64);
        for b in &self.0 {
            out.push_str(&format!("{:02x}", b));
        }
        out
    }
}

impl std::fmt::Display for CacheKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.hex())
    }
}

// ---------------------------------------------------------------------------
// Artifact taxonomy (Derakhshan Ch 4)
// ---------------------------------------------------------------------------

/// Tag that changes how downstream consumers may reuse a cached entry.
///
/// * `Feature` — transformed data (splittable in principle; we don't
///   split today, but the tag lets future windowed retraining pick
///   these up).
/// * `Statistics` — fit outputs (mean, variance, class priors) that
///   can be incrementally merged across partitions.
/// * `Model` — trained estimator; not splittable, potentially
///   warmstart-able if the KB says so.
/// * `Opaque` — anything we don't classify; plain value-level reuse only.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Artifact {
    Feature,
    Statistics,
    Model,
    #[default]
    Opaque,
}

// ---------------------------------------------------------------------------
// CacheEntry
// ---------------------------------------------------------------------------

/// Metadata sidecar describing a cached payload. Payloads are opaque
/// JSON values for v1 — Arrow IPC buffers land in Tier 2 for the
/// zero-copy cross-worker path.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CacheEntry {
    pub key: CacheKey,
    pub artifact: Artifact,
    /// Opaque payload — in production a pointer into a blob store;
    /// for v1 we carry a JSON value so tests exercise the full
    /// round-trip.
    pub payload: serde_json::Value,
    /// Bytes stored (approximate). Feeds the GDSF score later.
    pub size_bytes: u64,
    /// Real-time cost of the firing that produced this entry, in
    /// seconds. Feeds benefit scoring; correctness is only required
    /// in *ordering*, not absolute value.
    pub compute_secs: f64,
    /// How many times this entry has been served. The scheduler
    /// increments this on every hit — benefit scoring reads it.
    pub hits: u64,
}

impl CacheEntry {
    pub fn new(
        key: CacheKey,
        artifact: Artifact,
        payload: serde_json::Value,
        compute_secs: f64,
    ) -> Self {
        let size_bytes = payload.to_string().len() as u64;
        CacheEntry {
            key,
            artifact,
            payload,
            size_bytes,
            compute_secs,
            hits: 0,
        }
    }
}

// ---------------------------------------------------------------------------
// CacheOutcome
// ---------------------------------------------------------------------------

/// Returned by `CacheStore::lookup`. Schedulers branch on this before
/// firing.
#[derive(Debug, Clone)]
pub enum CacheOutcome {
    Hit(Arc<CacheEntry>),
    Miss,
    /// The actor is opted out of caching (non-deterministic, user
    /// code, or missing operator version). Always fire; never cache
    /// the result.
    Bypass,
}

impl CacheOutcome {
    pub fn is_hit(&self) -> bool {
        matches!(self, CacheOutcome::Hit(_))
    }
    pub fn is_miss(&self) -> bool {
        matches!(self, CacheOutcome::Miss)
    }
    pub fn is_bypass(&self) -> bool {
        matches!(self, CacheOutcome::Bypass)
    }
}

// ---------------------------------------------------------------------------
// CacheStore trait
// ---------------------------------------------------------------------------

/// Storage backend for the cache. Tier-1 is in-memory; Tier-2 will
/// back this with Redis + on-disk staging.
pub trait CacheStore: Send + Sync {
    /// Look up by key. Should increment the `hits` counter on a hit.
    fn lookup(&self, key: &CacheKey) -> CacheOutcome;

    /// Store an entry. Overwrites any prior entry under the same key
    /// (last-writer-wins; CAS-by-completed_at is a Tier-2 concern).
    fn put(&self, entry: CacheEntry);

    /// Number of entries currently held.
    fn len(&self) -> usize;

    /// True when the store is empty.
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

// ---------------------------------------------------------------------------
// MemoryStore — Tier-1 in-process implementation
// ---------------------------------------------------------------------------

/// In-memory cache store backed by a single lock. Adequate for Tier-1
/// (single-worker reuse inside one Rust engine); Tier-2 will switch
/// to a shard-free lock-free map when we add cross-host reuse.
pub struct MemoryStore {
    inner: RwLock<FxHashMap<CacheKey, Arc<CacheEntry>>>,
}

impl Default for MemoryStore {
    fn default() -> Self {
        Self {
            inner: RwLock::new(FxHashMap::default()),
        }
    }
}

impl MemoryStore {
    pub fn new() -> Self {
        Self::default()
    }
}

impl CacheStore for MemoryStore {
    fn lookup(&self, key: &CacheKey) -> CacheOutcome {
        // Two-phase: read-hit path avoids the writer lock; on hit we
        // upgrade to increment `hits`. For Tier-1 we eat the write
        // lock per hit — the numbers are small.
        if !self.inner.read().contains_key(key) {
            return CacheOutcome::Miss;
        }
        let mut guard = self.inner.write();
        if let Some(existing) = guard.get(key).cloned() {
            // Increment hits. Arc::make_mut clones if shared — for a
            // single cache with no external Arc handles this is free.
            let mut next = (*existing).clone();
            next.hits += 1;
            let new_arc = Arc::new(next);
            guard.insert(*key, new_arc.clone());
            CacheOutcome::Hit(new_arc)
        } else {
            CacheOutcome::Miss
        }
    }

    fn put(&self, entry: CacheEntry) {
        let mut guard = self.inner.write();
        guard.insert(entry.key, Arc::new(entry));
    }

    fn len(&self) -> usize {
        self.inner.read().len()
    }
}

// ---------------------------------------------------------------------------
// Key computation — canonicalisation + hashing
// ---------------------------------------------------------------------------

/// Inputs a node sees at firing time, from the scheduler's point of
/// view. `upstream_keys` carry the cache keys of upstream firings,
/// giving recursive propagation — a node downstream of a miss
/// reflects that miss in its own key.
pub struct KeyInputs<'a> {
    /// FQN of the operator (or snippet name; bypassed non-deterministic
    /// nodes never call this).
    pub op_fqn: &'a str,
    /// Method sequence on the operator (dorian::Operator::tasks).
    /// Two firings that share an FQN but differ on method sequence
    /// (e.g. `fit` alone vs. `fit; transform`) MUST NOT share a
    /// cache key — behavior differs.
    pub op_tasks: &'a [String],
    /// Optional version string; embedded in the hash so a library
    /// upgrade invalidates affected entries.
    pub op_version: Option<&'a str>,
    /// Canonicalised parameter bindings, in declaration order. Keys
    /// are port/keyword names; values are the parameter payloads.
    pub params: Vec<(String, serde_json::Value)>,
    /// Upstream cache keys; order matters because it reflects the
    /// data-flow topology the scheduler walks.
    pub upstream_keys: Vec<CacheKey>,
    /// Root-level content hash — the dataset identity. Optional
    /// because not every graph has a single root (e.g. rewrite test
    /// inputs); when absent, only upstream_keys carry the pedigree.
    pub root_content_hash: Option<CacheKey>,
}

/// Compute a content-addressable cache key for a single actor firing.
///
/// Determinism contract: identical `KeyInputs` → identical `CacheKey`.
/// The caller is responsible for canonicalising `params` (stable
/// ordering + normalised types) — see `canonicalise_params` for a
/// default implementation over the pipeline's `Parameter` nodes.
pub fn compute_key(inputs: &KeyInputs<'_>) -> CacheKey {
    let mut hasher = Sha256::new();

    // Prefix each region so attacker-controlled collisions across
    // field boundaries are harder.
    hasher.update(b"op_fqn\x00");
    hasher.update(inputs.op_fqn.as_bytes());
    hasher.update(b"\x00");

    // Method sequence — part of the op fingerprint. Two firings with
    // same FQN but different task lists execute different method
    // chains and MUST NOT collide.
    hasher.update(b"op_tasks\x00");
    for t in inputs.op_tasks {
        hasher.update(t.as_bytes());
        hasher.update(b";");
    }
    hasher.update(b"\x00");

    hasher.update(b"op_version\x00");
    hasher.update(inputs.op_version.unwrap_or("").as_bytes());
    hasher.update(b"\x00");

    hasher.update(b"params\x00");
    for (k, v) in &inputs.params {
        hasher.update(k.as_bytes());
        hasher.update(b"=");
        // Use compact canonical JSON so ordering variations in the
        // source JSON are normalised.
        let canon = canonicalise_json(v);
        hasher.update(canon.as_bytes());
        hasher.update(b"\x00");
    }

    hasher.update(b"upstream\x00");
    for key in &inputs.upstream_keys {
        hasher.update(&key.0);
    }
    hasher.update(b"\x00");

    hasher.update(b"root\x00");
    if let Some(root) = &inputs.root_content_hash {
        hasher.update(&root.0);
    }

    let digest = hasher.finalize();
    let mut out = [0u8; 32];
    out.copy_from_slice(&digest);
    CacheKey(out)
}

/// Canonicalise a JSON value so semantic equality implies byte
/// equality on the output. Object keys are sorted lexicographically;
/// numbers are serialised via `serde_json`'s default (it rounds-trips
/// IEEE 754).
///
/// Not a full JCS implementation — enough for v1 when our params are
/// restricted to parameter-node payloads (scalars, small lists, small
/// dicts).
pub fn canonicalise_json(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            let mut out = String::from("{");
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push('"');
                out.push_str(k);
                out.push_str("\":");
                out.push_str(&canonicalise_json(&m[*k]));
            }
            out.push('}');
            out
        }
        serde_json::Value::Array(a) => {
            let mut out = String::from("[");
            for (i, item) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&canonicalise_json(item));
            }
            out.push(']');
            out
        }
        // Note: scalar canonicalisation (incl. float precision
        // normalisation) happens upstream of this hash on the
        // Python facade — Parameter values arrive as JSON strings
        // pre-rounded to 12 significant digits via
        // ``intermediates_cache.canonicalise_param_string``. So the
        // Number branch here is only reached when a Rust caller
        // passes a raw f64 directly, which the current callers
        // don't do. Keep this site simple; if a future Rust caller
        // wants float canonicalisation, mirror %.12g formatting
        // here.
        other => other.to_string(),
    }
}

/// Collect parameter bindings for an actor from its upstream
/// `Parameter` nodes — a convenience for callers that have a
/// `ProcessGraph` handy.
///
/// Returns a list of `(handle, value)` pairs sorted by handle so the
/// order fed into `compute_key` is deterministic. `handle` is the
/// keyword name or stringified positional index on the edge.
///
/// Every Parameter-node upstream edge is included. Parameters whose
/// values live outside the graph (e.g. operator defaults, rewrite-
/// injected constants) must be materialised as explicit Parameter
/// nodes before caching — otherwise they don't enter the cache key
/// and correctness breaks. The DEM annotation's
/// `random_state_param_name` guards the most common case
/// (unseeded sklearn ops) via `eligibility_with_incoming`.
pub fn extract_param_bindings(
    graph: &graph::model::ProcessGraph,
    node_id: &str,
) -> Vec<(String, serde_json::Value)> {
    let mut bindings: Vec<(String, serde_json::Value)> = Vec::new();
    for edge in graph.incoming_edges(node_id) {
        let src_node = match graph.get_node(&edge.source) {
            Some(n) => n,
            None => continue,
        };
        if let Node::Parameter(Parameter { name: _, dtype, value }) = src_node {
            let handle = match &edge.position {
                graph::model::Position::Index(i) => i.to_string(),
                graph::model::Position::Keyword(k) => k.clone(),
            };
            // Parameter payload = (dtype tag, raw string). Both feed
            // the hash so a string "1" and an int 1 never collide.
            let payload = serde_json::json!({
                "dtype": format!("{:?}", dtype).to_lowercase(),
                "value": value,
            });
            bindings.push((handle, payload));
        }
    }
    bindings.sort_by(|a, b| a.0.cmp(&b.0));
    bindings
}

/// Return the set of parameter handles wired to `node_id` — i.e. the
/// keyword names (or stringified positional indices) of every
/// upstream Parameter-node edge. Feeds `eligibility_with_incoming`
/// to detect declared-but-unwired seed parameters.
pub fn incoming_param_handles(
    graph: &graph::model::ProcessGraph,
    node_id: &str,
) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for edge in graph.incoming_edges(node_id) {
        if matches!(
            graph.get_node(&edge.source),
            Some(Node::Parameter(_))
        ) {
            let h = match &edge.position {
                graph::model::Position::Index(i) => i.to_string(),
                graph::model::Position::Keyword(k) => k.clone(),
            };
            out.push(h);
        }
    }
    out.sort();
    out.dedup();
    out
}

/// FQNs where an `ActorAnnotations::random_state_param_name` is
/// declared but no Parameter is wired to that handle in the given
/// graph — the nodes a mitigation rewrite should seed before the
/// cache can safely participate.
pub fn detect_missing_random_state(
    graph: &graph::model::ProcessGraph,
    annotations: &graph::dem::DemAnnotations,
) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for (id, _) in &graph.nodes {
        let ann = match annotations.actor(id) {
            Some(a) => a,
            None => continue,
        };
        let seed_name = match ann.random_state_param_name.as_deref() {
            Some(s) => s,
            None => continue,
        };
        let handles = incoming_param_handles(graph, id);
        if !handles.iter().any(|h| h == seed_name) {
            out.push(id.clone());
        }
    }
    out.sort();
    out
}

/// Decide whether a node participates in caching at all. Bypass
/// precedes key computation — non-deterministic actors skip the
/// whole cache path.
///
/// Coarse form: considers only the operator's determinism class.
/// Callers that have the node's incoming parameter handles should
/// prefer `eligibility_with_incoming` — it catches the random_state
/// correctness hole where a nominally-deterministic operator is
/// actually non-deterministic because its seed parameter is unwired.
pub fn eligibility(ann: &ActorAnnotations) -> Eligibility {
    match ann.determinism {
        DeterminismClass::Deterministic => Eligibility::Cacheable,
        DeterminismClass::NonDeterministic => Eligibility::Bypass,
        DeterminismClass::Unknown => Eligibility::Bypass,
    }
}

/// Stricter eligibility gate. Same as `eligibility` except it also
/// forces Bypass when:
///
///   1. `ann.random_state_param_name` is set (operator declares a
///      reproducibility-seed parameter), AND
///   2. `incoming_param_handles` does NOT contain that name.
///
/// Meaning: the operator has a default-unseeded randomness source
/// that would make the firing silently non-reproducible, and the
/// pipeline has not explicitly bound a seed. The correct response
/// is to refuse caching — a downstream mitigation rewrite adds the
/// missing `random_state` Parameter node and, on the next pass,
/// this check returns Cacheable because the seed is now wired.
pub fn eligibility_with_incoming(
    ann: &ActorAnnotations,
    incoming_param_handles: &[&str],
) -> Eligibility {
    let base = eligibility(ann);
    if base != Eligibility::Cacheable {
        return base;
    }
    if let Some(seed_name) = ann.random_state_param_name.as_deref() {
        if !incoming_param_handles.iter().any(|h| *h == seed_name) {
            return Eligibility::Bypass;
        }
    }
    Eligibility::Cacheable
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Eligibility {
    Cacheable,
    Bypass,
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
pub enum CacheError {
    #[error("serialisation error: {0}")]
    SerdeError(String),
    #[error("store error: {0}")]
    StoreError(String),
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use graph::dem::{classify_determinism_builtin, ActorAnnotations, DeterminismClass};
    use graph::model::{Node, Operator, ParamDtype, Parameter};
    use serde_json::json;

    #[test]
    fn cache_key_hex_is_64_chars() {
        let k = CacheKey([0xabu8; 32]);
        assert_eq!(k.hex().len(), 64);
        assert!(k.hex().chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn compute_key_is_deterministic() {
        let inputs = KeyInputs {
            op_fqn: "sklearn.preprocessing.StandardScaler",
            op_tasks: &[],
            op_version: Some("1.0.0"),
            params: vec![("with_mean".into(), json!(true))],
            upstream_keys: vec![],
            root_content_hash: None,
        };
        let k1 = compute_key(&inputs);
        let k2 = compute_key(&inputs);
        assert_eq!(k1, k2);
    }

    #[test]
    fn compute_key_different_for_different_params() {
        let base = KeyInputs {
            op_fqn: "sklearn.preprocessing.StandardScaler",
            op_tasks: &[],
            op_version: Some("1.0.0"),
            params: vec![("with_mean".into(), json!(true))],
            upstream_keys: vec![],
            root_content_hash: None,
        };
        let alt = KeyInputs {
            op_fqn: "sklearn.preprocessing.StandardScaler",
            op_tasks: &[],
            op_version: Some("1.0.0"),
            params: vec![("with_mean".into(), json!(false))],
            upstream_keys: vec![],
            root_content_hash: None,
        };
        assert_ne!(compute_key(&base), compute_key(&alt));
    }

    #[test]
    fn compute_key_changes_with_version() {
        let v1 = KeyInputs {
            op_fqn: "pandas.read_csv",
            op_tasks: &[],
            op_version: Some("1.0.0"),
            params: vec![],
            upstream_keys: vec![],
            root_content_hash: None,
        };
        let v2 = KeyInputs {
            op_fqn: "pandas.read_csv",
            op_tasks: &[],
            op_version: Some("2.0.0"),
            params: vec![],
            upstream_keys: vec![],
            root_content_hash: None,
        };
        assert_ne!(compute_key(&v1), compute_key(&v2));
    }

    #[test]
    fn compute_key_propagates_upstream() {
        let parent_key = CacheKey([1u8; 32]);
        let other_parent_key = CacheKey([2u8; 32]);

        let with_parent = KeyInputs {
            op_fqn: "op",
            op_tasks: &[],
            op_version: None,
            params: vec![],
            upstream_keys: vec![parent_key],
            root_content_hash: None,
        };
        let with_other = KeyInputs {
            op_fqn: "op",
            op_tasks: &[],
            op_version: None,
            params: vec![],
            upstream_keys: vec![other_parent_key],
            root_content_hash: None,
        };
        assert_ne!(compute_key(&with_parent), compute_key(&with_other));
    }

    #[test]
    fn canonicalise_json_sorts_object_keys() {
        let a = json!({"b": 1, "a": 2});
        let b = json!({"a": 2, "b": 1});
        assert_eq!(canonicalise_json(&a), canonicalise_json(&b));
    }

    #[test]
    fn memory_store_put_then_hit() {
        let store = MemoryStore::new();
        let key = CacheKey([42u8; 32]);
        let entry = CacheEntry::new(key, Artifact::Feature, json!("hello"), 0.1);
        store.put(entry);
        assert_eq!(store.len(), 1);
        match store.lookup(&key) {
            CacheOutcome::Hit(e) => {
                assert_eq!(e.artifact, Artifact::Feature);
                assert_eq!(e.hits, 1);
            }
            other => panic!("expected Hit, got {other:?}"),
        }
        match store.lookup(&key) {
            CacheOutcome::Hit(e) => assert_eq!(e.hits, 2),
            other => panic!("expected Hit, got {other:?}"),
        }
    }

    #[test]
    fn memory_store_miss_for_unknown_key() {
        let store = MemoryStore::new();
        let key = CacheKey([9u8; 32]);
        assert!(matches!(store.lookup(&key), CacheOutcome::Miss));
    }

    #[test]
    fn eligibility_follows_determinism() {
        let mut ann = ActorAnnotations::sdf_default();
        ann.determinism = DeterminismClass::Deterministic;
        assert_eq!(eligibility(&ann), Eligibility::Cacheable);
        ann.determinism = DeterminismClass::NonDeterministic;
        assert_eq!(eligibility(&ann), Eligibility::Bypass);
        ann.determinism = DeterminismClass::Unknown;
        assert_eq!(eligibility(&ann), Eligibility::Bypass);
    }

    #[test]
    fn extract_params_orders_by_handle() {
        let mut g = graph::model::ProcessGraph::new();
        g.add_node(
            "p_b".into(),
            Node::Parameter(Parameter {
                name: "b".into(),
                dtype: ParamDtype::Int,
                value: "2".into(),
            }),
        );
        g.add_node(
            "p_a".into(),
            Node::Parameter(Parameter {
                name: "a".into(),
                dtype: ParamDtype::Int,
                value: "1".into(),
            }),
        );
        g.add_node(
            "op".into(),
            Node::Operator(Operator {
                name: "sklearn.X".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_edge(graph::model::Edge {
            source: "p_a".into(),
            destination: "op".into(),
            position: graph::model::Position::Keyword("a".into()),
            output: graph::model::Position::Index(0),
            delivery_mode: graph::model::DeliveryMode::Once,
        });
        g.add_edge(graph::model::Edge {
            source: "p_b".into(),
            destination: "op".into(),
            position: graph::model::Position::Keyword("b".into()),
            output: graph::model::Position::Index(0),
            delivery_mode: graph::model::DeliveryMode::Once,
        });
        let params = extract_param_bindings(&g, "op");
        assert_eq!(params.len(), 2);
        assert_eq!(params[0].0, "a");
        assert_eq!(params[1].0, "b");
    }

    #[test]
    fn op_tasks_affect_cache_key() {
        // Different method sequences MUST produce different keys —
        // even if FQN, version, and params all match.
        let fit_only = vec!["fit".to_string()];
        let fit_transform = vec!["fit".to_string(), "transform".to_string()];
        let k1 = compute_key(&KeyInputs {
            op_fqn: "sklearn.preprocessing.StandardScaler",
            op_tasks: &fit_only,
            op_version: Some("1.0.0"),
            params: vec![],
            upstream_keys: vec![],
            root_content_hash: None,
        });
        let k2 = compute_key(&KeyInputs {
            op_fqn: "sklearn.preprocessing.StandardScaler",
            op_tasks: &fit_transform,
            op_version: Some("1.0.0"),
            params: vec![],
            upstream_keys: vec![],
            root_content_hash: None,
        });
        assert_ne!(k1, k2);
    }

    #[test]
    fn eligibility_with_incoming_forces_bypass_when_seed_unwired() {
        // Operator has random_state_param_name declared but the
        // pipeline doesn't wire a Parameter to that handle.
        let mut ann = ActorAnnotations::sdf_default();
        ann.determinism = DeterminismClass::Deterministic;
        ann.random_state_param_name = Some("random_state".to_string());
        ann.operator_version = Some("1.0".to_string());

        let unseeded: &[&str] = &["n_estimators"];
        let seeded: &[&str] = &["n_estimators", "random_state"];

        assert_eq!(
            eligibility_with_incoming(&ann, unseeded),
            Eligibility::Bypass,
            "unseeded must bypass"
        );
        assert_eq!(
            eligibility_with_incoming(&ann, seeded),
            Eligibility::Cacheable,
            "seeded must be cacheable"
        );
    }

    #[test]
    fn eligibility_with_incoming_matches_base_when_no_seed_declared() {
        let mut ann = ActorAnnotations::sdf_default();
        ann.determinism = DeterminismClass::Deterministic;
        ann.random_state_param_name = None;
        let handles: &[&str] = &[];
        assert_eq!(
            eligibility_with_incoming(&ann, handles),
            Eligibility::Cacheable
        );
    }

    #[test]
    fn non_deterministic_operator_classifies_as_bypass() {
        // Stand-in for the KB → annotation flow.
        let op = Node::Operator(Operator {
            name: "openrouter.chat.completion".into(),
            language: "python".into(),
            tasks: vec![],
        });
        let mut ann = ActorAnnotations::sdf_default();
        ann.determinism = classify_determinism_builtin(&op);
        assert_eq!(eligibility(&ann), Eligibility::Bypass);
    }
}
