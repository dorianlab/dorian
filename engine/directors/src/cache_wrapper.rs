//! Cache-aware wrapper around `DataflowDirector`.
//!
//! Bridges the existing director (level-based concurrent dispatch
//! over a `ProcessGraph`) to the content-addressable cache layer.
//! Instead of reimplementing the SDF scheduler, we wrap the existing
//! director's `DirectorHooks` so each "node starting" event consults
//! the cache and short-circuits to a synthetic Success outcome on
//! hit.
//!
//! The wrapper is opt-in: existing call sites keep their hooks; the
//! migration path is "wrap your hooks in a `CacheAwareHooks`, pass
//! the same DEM annotations the parser produced, and the rest is
//! transparent".
//!
//! Doesn't (yet) replace the existing scheduler — that's the
//! medium-term goal but requires the dispatch crate to honour the
//! cache key when emitting back results. This wrapper is the
//! observability/short-circuit slice that lands first.

use cache::{
    compute_key, eligibility_with_incoming, extract_param_bindings, incoming_param_handles,
    CacheKey, CacheOutcome, CacheStore, Eligibility, KeyInputs,
};
use graph::dem::{DemAnnotations, DomainKind};
use graph::model::{Node, ProcessGraph};

use crate::dataflow::{DirectorHooks, NodeOutcome};

/// Result of consulting the cache for a single node.
#[derive(Debug, Clone)]
pub enum CacheDecision {
    /// Cache hit — wrapper short-circuits with this outcome.
    Hit { key: CacheKey, hits: u64 },
    /// Cache miss — fall through to the wrapped hooks; on success
    /// the wrapper writes the result back to the cache.
    Miss { key: CacheKey },
    /// Bypass — non-deterministic actor; never cache.
    Bypass,
    /// Not annotated (no DEM info, or DE-domain) — pass through
    /// without touching the cache.
    PassThrough,
}

/// Wraps another `DirectorHooks` impl with cache consultation.
pub struct CacheAwareHooks<'a, H: DirectorHooks> {
    pub inner: H,
    pub cache: &'a dyn CacheStore,
    pub graph: &'a ProcessGraph,
    pub annotations: &'a DemAnnotations,
    /// In-flight decisions per `(run_id, node_id)`. We need a
    /// place to remember which key the miss-path will later store
    /// into. Keyed by `node_id` only because the director processes
    /// one run at a time per hook instance.
    decisions: parking_mutex::Mutex<rustc_hash::FxHashMap<String, CacheDecision>>,
    /// Per-node propagated keys; lets downstream cache lookups use
    /// upstream key pedigrees.
    keys: parking_mutex::Mutex<rustc_hash::FxHashMap<String, CacheKey>>,
}

mod parking_mutex {
    pub struct Mutex<T> {
        inner: std::sync::Mutex<T>,
    }
    impl<T> Mutex<T> {
        pub fn new(v: T) -> Self {
            Mutex {
                inner: std::sync::Mutex::new(v),
            }
        }
        pub fn lock(&self) -> std::sync::MutexGuard<'_, T> {
            self.inner.lock().unwrap()
        }
    }
}

impl<'a, H: DirectorHooks> CacheAwareHooks<'a, H> {
    pub fn new(
        inner: H,
        cache: &'a dyn CacheStore,
        graph: &'a ProcessGraph,
        annotations: &'a DemAnnotations,
    ) -> Self {
        CacheAwareHooks {
            inner,
            cache,
            graph,
            annotations,
            decisions: parking_mutex::Mutex::new(rustc_hash::FxHashMap::default()),
            keys: parking_mutex::Mutex::new(rustc_hash::FxHashMap::default()),
        }
    }

    fn decide(&self, node_id: &str) -> CacheDecision {
        let node = match self.graph.get_node(node_id) {
            Some(n) => n,
            None => return CacheDecision::PassThrough,
        };
        if matches!(node, Node::Parameter(_)) {
            return CacheDecision::PassThrough;
        }
        let ann = match self.annotations.actor(node_id) {
            Some(a) => a,
            None => return CacheDecision::PassThrough,
        };
        if ann.domain != DomainKind::Sdf {
            return CacheDecision::PassThrough;
        }
        let handles = incoming_param_handles(self.graph, node_id);
        let handle_refs: Vec<&str> = handles.iter().map(String::as_str).collect();
        match eligibility_with_incoming(ann, &handle_refs) {
            Eligibility::Bypass => CacheDecision::Bypass,
            Eligibility::Cacheable => {
                // Build upstream pedigree from the per-node key map.
                let upstream_keys: Vec<CacheKey> = {
                    let keys = self.keys.lock();
                    self.graph
                        .incoming_edges(node_id)
                        .iter()
                        .filter_map(|edge| keys.get(&edge.source).copied())
                        .collect()
                };
                let (op_fqn, op_tasks) = match node {
                    Node::Operator(o) => (o.name.clone(), o.tasks.clone()),
                    Node::Snippet(s) => (format!("snippet::{}", s.name), Vec::new()),
                    _ => ("unknown".to_string(), Vec::new()),
                };
                let params = extract_param_bindings(self.graph, node_id);
                let inputs = KeyInputs {
                    op_fqn: &op_fqn,
                    op_tasks: &op_tasks,
                    op_version: ann.operator_version.as_deref(),
                    params,
                    upstream_keys,
                    root_content_hash: None,
                };
                let key = compute_key(&inputs);
                match self.cache.lookup(&key) {
                    CacheOutcome::Hit(entry) => CacheDecision::Hit {
                        key,
                        hits: entry.hits,
                    },
                    CacheOutcome::Miss | CacheOutcome::Bypass => CacheDecision::Miss { key },
                }
            }
        }
    }
}

#[async_trait::async_trait]
impl<'a, H: DirectorHooks + Sync> DirectorHooks for CacheAwareHooks<'a, H> {
    async fn on_node_starting(&self, run_id: &str, node_id: &str) {
        let decision = self.decide(node_id);
        {
            if let CacheDecision::Hit { key, .. } | CacheDecision::Miss { key } = &decision {
                self.keys.lock().insert(node_id.to_string(), *key);
            }
        }
        {
            self.decisions.lock().insert(node_id.to_string(), decision);
        }
        self.inner.on_node_starting(run_id, node_id).await;
    }

    async fn on_node_completed(&self, run_id: &str, outcome: &NodeOutcome) {
        // Miss-then-Success write. The director doesn't hand us the
        // real payload yet (it flows through the dispatch layer when
        // that lands); for now we write a minimal envelope carrying
        // the result_ref so downstream runs observe a Hit and short-
        // circuit to the same ref. That's enough to exercise the
        // cache-aware scheduler end-to-end today; once dispatch
        // ships real bytes, swap `payload` here for the actual
        // serialised artifact.
        let decision = {
            let decisions = self.decisions.lock();
            decisions.get(outcome.node_id()).cloned()
        };
        if let (
            Some(CacheDecision::Miss { key }),
            NodeOutcome::Success {
                node_id,
                result_ref,
                duration_secs,
            },
        ) = (decision, outcome)
        {
            let payload = serde_json::json!({
                "result_ref": result_ref,
                "run_id": run_id,
                "placeholder": true,
            });
            let entry =
                CacheEntry::new(key, Artifact::Feature, payload, *duration_secs);
            self.cache.put(entry);
            tracing::debug!(
                run_id,
                node_id,
                cache_key = %key,
                "cache wrapper: wrote miss payload"
            );
        }
        self.inner.on_node_completed(run_id, outcome).await;
    }

    async fn is_cancelled(&self, run_id: &str) -> bool {
        self.inner.is_cancelled(run_id).await
    }

    async fn override_outcome(&self, run_id: &str, node_id: &str) -> Option<NodeOutcome> {
        // On cache hit, short-circuit with a Success outcome carrying
        // a synthetic result_ref derived from the cache key.
        let decision = {
            let decisions = self.decisions.lock();
            decisions.get(node_id).cloned()
        };
        if let Some(CacheDecision::Hit { key, .. }) = decision {
            return Some(NodeOutcome::Success {
                node_id: node_id.to_string(),
                result_ref: Some(format!("cache:{}", key.hex())),
                duration_secs: 0.0,
            });
        }
        // Otherwise fall through to the inner hooks (tests etc).
        self.inner.override_outcome(run_id, node_id).await
    }
}

/// Re-export key types so callers don't need a separate `cache::` import.
pub use cache::{Artifact, CacheEntry};
pub use cache::{CacheStore as CacheStoreTrait, MemoryStore as DefaultMemoryStore};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dataflow::{DataflowDirector, ExecutionPlan, NoopHooks};
    use cache::{CacheEntry, CacheKey, MemoryStore};
    use graph::dem::{ActorAnnotations, DeterminismClass};
    use graph::model::{DeliveryMode, Edge, Operator, Position};

    fn linear_two_nodes() -> (ProcessGraph, DemAnnotations) {
        let mut g = ProcessGraph::new();
        g.add_node(
            "a".into(),
            Node::Operator(Operator {
                name: "sklearn.X".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_node(
            "b".into(),
            Node::Operator(Operator {
                name: "sklearn.Y".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_edge(Edge {
            source: "a".into(),
            destination: "b".into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        });
        let mut dem = DemAnnotations::new();
        for id in ["a", "b"] {
            let mut ann = ActorAnnotations::sdf_default();
            ann.determinism = DeterminismClass::Deterministic;
            ann.operator_version = Some("1".into());
            dem.actors.insert(id.into(), ann);
        }
        (g, dem)
    }

    #[tokio::test]
    async fn cold_cache_yields_misses_for_all_nodes() {
        let (g, dem) = linear_two_nodes();
        let store = MemoryStore::new();
        let hooks = CacheAwareHooks::new(NoopHooks, &store, &g, &dem);
        let plan = ExecutionPlan::from_graph(&g).unwrap();
        let director = DataflowDirector::new();
        let _ = director.execute(&plan, &g, "r1", &hooks).await.unwrap();
        let decisions = hooks.decisions.lock();
        // Both nodes were classified as Miss (cold cache).
        assert!(matches!(
            decisions.get("a"),
            Some(CacheDecision::Miss { .. })
        ));
        assert!(matches!(
            decisions.get("b"),
            Some(CacheDecision::Miss { .. })
        ));
    }

    #[tokio::test]
    async fn pre_seeded_cache_short_circuits_to_success() {
        let (g, dem) = linear_two_nodes();
        let store = MemoryStore::new();

        // Seed the cache with the key the wrapper will compute for "a".
        // Since `a` has no upstream and no params, the key is fixed.
        let key_a = compute_key(&KeyInputs {
            op_fqn: "sklearn.X",
            op_tasks: &[],
            op_version: Some("1"),
            params: vec![],
            upstream_keys: vec![],
            root_content_hash: None,
        });
        store.put(CacheEntry::new(
            key_a,
            cache::Artifact::Feature,
            serde_json::json!({"hot": true}),
            0.0,
        ));

        let hooks = CacheAwareHooks::new(NoopHooks, &store, &g, &dem);
        let plan = ExecutionPlan::from_graph(&g).unwrap();
        let director = DataflowDirector::new();
        let outcomes = director.execute(&plan, &g, "r1", &hooks).await.unwrap();
        // First node should be Success with a result_ref starting cache:.
        let outcome_a = outcomes
            .iter()
            .find(|o| o.node_id() == "a")
            .expect("missing outcome for a");
        if let NodeOutcome::Success { result_ref, .. } = outcome_a {
            assert!(
                result_ref
                    .as_ref()
                    .map(|s| s.starts_with("cache:"))
                    .unwrap_or(false),
                "expected cache: result_ref, got {result_ref:?}"
            );
        } else {
            panic!("expected Success outcome, got {outcome_a:?}");
        }
    }

    #[tokio::test]
    async fn parameter_nodes_pass_through_unchanged() {
        let mut g = ProcessGraph::new();
        g.add_node(
            "p".into(),
            Node::Parameter(graph::Parameter {
                name: "x".into(),
                dtype: graph::ParamDtype::Int,
                value: "1".into(),
            }),
        );
        let dem = DemAnnotations::new();
        let store = MemoryStore::new();
        let hooks = CacheAwareHooks::new(NoopHooks, &store, &g, &dem);
        let plan = ExecutionPlan::from_graph(&g).unwrap();
        let director = DataflowDirector::new();
        let _ = director.execute(&plan, &g, "r1", &hooks).await.unwrap();
        let decisions = hooks.decisions.lock();
        assert!(matches!(
            decisions.get("p"),
            Some(CacheDecision::PassThrough)
        ));
    }

    #[tokio::test]
    async fn unrelated_cache_key_does_not_match() {
        let (g, dem) = linear_two_nodes();
        let store = MemoryStore::new();
        // Plant a key that does NOT match anything in the graph.
        store.put(CacheEntry::new(
            CacheKey([99u8; 32]),
            cache::Artifact::Feature,
            serde_json::json!(null),
            0.0,
        ));
        let hooks = CacheAwareHooks::new(NoopHooks, &store, &g, &dem);
        let plan = ExecutionPlan::from_graph(&g).unwrap();
        let director = DataflowDirector::new();
        let _ = director.execute(&plan, &g, "r1", &hooks).await.unwrap();
        let decisions = hooks.decisions.lock();
        // Both nodes still Miss.
        assert!(matches!(
            decisions.get("a"),
            Some(CacheDecision::Miss { .. })
        ));
    }
}
