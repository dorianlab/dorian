//! Synchronous Dataflow domain scheduler — v1 sketch.
//!
//! The SDF scheduler walks the DEM-annotated graph in topological
//! order and, for each SDF actor, consults the cache before firing:
//!
//!   1. Compute the cache key from `(op_fqn, canonicalised_params,
//!      upstream_keys, op_version)`.
//!   2. Look up the key in the `CacheStore`:
//!      - `Hit`  — reuse the stored payload; emit `NodeCacheHit`;
//!        propagate the same key to downstream nodes.
//!      - `Miss` — fire the actor (stubbed here via `Firer`);
//!        store the result under the key; emit `NodeComputed`.
//!      - `Bypass` (non-deterministic) — fire unconditionally; skip
//!        the cache write; downstream nodes get a synthetic
//!        "no-pedigree" key so their own cache lookups don't collide
//!        with deterministic pedigrees.
//!
//! This crate does not implement the actor-execution runtime (that
//! lives in `dispatch::` and eventually submits to the Go exec-jobs
//! stream). It implements the *scheduling* side — the decisions
//! around whether to fire, whether to serve from cache, and how to
//! propagate keys.
//!
//! v1 runs level-by-level (matches existing `DataflowDirector`) and
//! is purely synchronous — sufficient to prove the cache-key plumbing
//! and the DEM annotation contract. Shadow-mode migration layered on
//! top comes in Tier 2.

use std::sync::Arc;

use rustc_hash::FxHashMap;

use cache::{
    compute_key, eligibility_with_incoming, incoming_param_handles, CacheEntry, CacheKey,
    CacheOutcome, CacheStore, Eligibility, KeyInputs,
};
use graph::dem::{DemAnnotations, DomainKind};
use graph::model::{Node, Parameter, ProcessGraph};
use graph::topology::topological_sort;

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

/// Observability events emitted by the scheduler. The engine crate
/// forwards these onto the existing event bus (`NodeCacheHit` and
/// `NodeComputed` are new event types that ride the same Redis
/// stream).
#[derive(Debug, Clone)]
pub enum SchedulerEvent {
    NodeCacheHit {
        run_id: String,
        node_id: String,
        cache_key: CacheKey,
        hits: u64,
    },
    NodeComputed {
        run_id: String,
        node_id: String,
        cache_key: CacheKey,
        compute_secs: f64,
    },
    NodeBypassed {
        run_id: String,
        node_id: String,
        reason: String,
    },
    NodeSkipped {
        run_id: String,
        node_id: String,
        reason: String,
    },
}

// ---------------------------------------------------------------------------
// Firer — pluggable actor-execution trait
// ---------------------------------------------------------------------------

/// Fires a single actor. In production this trampolines to the
/// dispatch layer (Python exec-jobs, native Rust, Wasm); in tests we
/// use a mock that returns a synthetic payload keyed by node id.
pub trait Firer {
    fn fire(
        &self,
        run_id: &str,
        node_id: &str,
        graph: &ProcessGraph,
        upstream_payloads: &[Arc<CacheEntry>],
    ) -> Result<FiredResult, FireError>;
}

/// Result of a single firing. `compute_secs` is recorded into the
/// cache entry so benefit-driven eviction has real measurements
/// downstream.
#[derive(Debug, Clone)]
pub struct FiredResult {
    pub payload: serde_json::Value,
    pub compute_secs: f64,
    pub artifact: cache::Artifact,
}

#[derive(Debug, thiserror::Error)]
pub enum FireError {
    #[error("actor {0} failed: {1}")]
    ActorFailed(String, String),
}

// ---------------------------------------------------------------------------
// Scheduler
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
pub enum SchedulerError {
    #[error("graph validation failed: {0}")]
    Validation(String),
    #[error("fire error: {0}")]
    Fire(#[from] FireError),
}

/// SDF domain scheduler. Cache is a trait object so callers can swap
/// Tier-1 (in-memory) for Tier-2 (Redis) without touching scheduler
/// code.
pub struct SdfScheduler<'a> {
    pub cache: &'a dyn CacheStore,
}

/// Summary the scheduler returns at the end of a run. Used for
/// shadow-mode comparisons against Dask + for benchmark harnesses.
#[derive(Debug, Default)]
pub struct SchedulerReport {
    pub events: Vec<SchedulerEvent>,
    pub node_keys: FxHashMap<String, CacheKey>,
    pub hits: usize,
    pub misses: usize,
    pub bypasses: usize,
    pub skipped: usize,
}

impl<'a> SdfScheduler<'a> {
    pub fn new(cache: &'a dyn CacheStore) -> Self {
        SdfScheduler { cache }
    }

    /// Execute the SDF subset of the graph, consulting the cache
    /// before each firing. Nodes annotated under a non-SDF domain
    /// are skipped (the DE scheduler owns those).
    pub fn execute(
        &self,
        run_id: &str,
        graph: &ProcessGraph,
        annotations: &DemAnnotations,
        firer: &dyn Firer,
    ) -> Result<SchedulerReport, SchedulerError> {
        let topo = topological_sort(graph)
            .map_err(|e| SchedulerError::Validation(e.to_string()))?;
        let mut report = SchedulerReport::default();
        let mut payloads: FxHashMap<String, Arc<CacheEntry>> = FxHashMap::default();

        for node_id in &topo {
            let node = match graph.get_node(node_id) {
                Some(n) => n,
                None => continue,
            };
            // Only schedule SDF nodes here. DE nodes are the DE
            // scheduler's business. Parameter nodes are constants and
            // never "fire" — their payloads flow as hash input only.
            if matches!(node, Node::Parameter(_)) {
                continue;
            }
            let ann = match annotations.actor(node_id) {
                Some(a) => a,
                None => {
                    // An actor without annotations is a sign the
                    // parser didn't touch it — skip defensively.
                    report.skipped += 1;
                    report.events.push(SchedulerEvent::NodeSkipped {
                        run_id: run_id.to_string(),
                        node_id: node_id.clone(),
                        reason: "no DEM annotation".to_string(),
                    });
                    continue;
                }
            };
            if ann.domain != DomainKind::Sdf {
                // Leave DE nodes alone — the composed scheduler
                // handles cross-domain handoffs.
                continue;
            }

            // Upstream keys (data-dependency edges only — Parameter
            // contributions are folded into `params` instead).
            let mut upstream_keys: Vec<CacheKey> = Vec::new();
            let mut upstream_payloads: Vec<Arc<CacheEntry>> = Vec::new();
            for edge in graph.incoming_edges(node_id) {
                if matches!(graph.get_node(&edge.source), Some(Node::Parameter(_))) {
                    continue;
                }
                if let Some(k) = report.node_keys.get(&edge.source) {
                    upstream_keys.push(*k);
                }
                if let Some(p) = payloads.get(&edge.source) {
                    upstream_payloads.push(Arc::clone(p));
                }
            }

            let handles = incoming_param_handles(graph, node_id);
            let handle_refs: Vec<&str> = handles.iter().map(String::as_str).collect();
            match eligibility_with_incoming(ann, &handle_refs) {
                Eligibility::Bypass => {
                    // Non-deterministic: always fire, never cache.
                    let fired = firer.fire(run_id, node_id, graph, &upstream_payloads)?;
                    let synthetic_key = synthetic_bypass_key(node_id, run_id);
                    let entry = CacheEntry::new(
                        synthetic_key,
                        fired.artifact,
                        fired.payload,
                        fired.compute_secs,
                    );
                    let arc = Arc::new(entry);
                    payloads.insert(node_id.clone(), arc.clone());
                    report.node_keys.insert(node_id.clone(), synthetic_key);
                    report.bypasses += 1;
                    report.events.push(SchedulerEvent::NodeBypassed {
                        run_id: run_id.to_string(),
                        node_id: node_id.clone(),
                        reason: "non-deterministic or unwired seed".to_string(),
                    });
                }
                Eligibility::Cacheable => {
                    let op_fqn = operator_fqn(node);
                    let op_tasks = operator_tasks(node);
                    let params = cache::extract_param_bindings(graph, node_id);
                    let inputs = KeyInputs {
                        op_fqn: &op_fqn,
                        op_tasks: &op_tasks,
                        op_version: ann.operator_version.as_deref(),
                        params,
                        upstream_keys: upstream_keys.clone(),
                        root_content_hash: None,
                    };
                    let key = compute_key(&inputs);
                    match self.cache.lookup(&key) {
                        CacheOutcome::Hit(entry) => {
                            payloads.insert(node_id.clone(), entry.clone());
                            report.node_keys.insert(node_id.clone(), key);
                            report.hits += 1;
                            report.events.push(SchedulerEvent::NodeCacheHit {
                                run_id: run_id.to_string(),
                                node_id: node_id.clone(),
                                cache_key: key,
                                hits: entry.hits,
                            });
                        }
                        CacheOutcome::Miss | CacheOutcome::Bypass => {
                            let fired =
                                firer.fire(run_id, node_id, graph, &upstream_payloads)?;
                            let entry = CacheEntry::new(
                                key,
                                fired.artifact,
                                fired.payload,
                                fired.compute_secs,
                            );
                            self.cache.put(entry.clone());
                            payloads.insert(node_id.clone(), Arc::new(entry));
                            report.node_keys.insert(node_id.clone(), key);
                            report.misses += 1;
                            report.events.push(SchedulerEvent::NodeComputed {
                                run_id: run_id.to_string(),
                                node_id: node_id.clone(),
                                cache_key: key,
                                compute_secs: fired.compute_secs,
                            });
                        }
                    }
                }
            }
        }

        Ok(report)
    }
}

fn operator_fqn(node: &Node) -> String {
    match node {
        Node::Operator(o) => o.name.clone(),
        Node::Snippet(s) => format!("snippet::{}", s.name),
        Node::Parameter(Parameter { name, .. }) => format!("param::{name}"),
        Node::Node(n) => format!("pattern::{}", n.text),
        Node::Group(g) => format!("group::{}", g.name),
    }
}

fn operator_tasks(node: &Node) -> Vec<String> {
    match node {
        Node::Operator(o) => o.tasks.clone(),
        _ => Vec::new(),
    }
}

/// Synthetic key for bypass firings. Deterministic on `(node, run)`
/// so downstream deterministic nodes don't accidentally share a
/// pedigree with a different run — just an observability identifier,
/// no cache-hit surface.
fn synthetic_bypass_key(node_id: &str, run_id: &str) -> CacheKey {
    let inputs = KeyInputs {
        op_fqn: "__bypass__",
        op_tasks: &[],
        op_version: Some(run_id),
        params: vec![("node_id".to_string(), serde_json::Value::String(node_id.to_string()))],
        upstream_keys: vec![],
        root_content_hash: None,
    };
    compute_key(&inputs)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use cache::{Artifact, MemoryStore};
    use graph::dem::{ActorAnnotations, DeterminismClass};
    use graph::model::{DeliveryMode, Edge, Node, Operator, ParamDtype, Parameter, Position};
    use serde_json::json;

    /// Firer that returns a fixed payload so the scheduler's caching
    /// logic is the thing under test.
    struct StubFirer {
        compute_secs: f64,
    }

    impl Firer for StubFirer {
        fn fire(
            &self,
            _run_id: &str,
            node_id: &str,
            _graph: &ProcessGraph,
            _upstream: &[Arc<CacheEntry>],
        ) -> Result<FiredResult, FireError> {
            Ok(FiredResult {
                payload: json!({"computed_from": node_id}),
                compute_secs: self.compute_secs,
                artifact: Artifact::Feature,
            })
        }
    }

    fn make_param_graph() -> (ProcessGraph, DemAnnotations) {
        let mut g = ProcessGraph::new();
        g.add_node(
            "p".into(),
            Node::Parameter(Parameter {
                name: "path".into(),
                dtype: ParamDtype::String,
                value: "data.csv".into(),
            }),
        );
        g.add_node(
            "load".into(),
            Node::Operator(Operator {
                name: "pandas.read_csv".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_node(
            "scale".into(),
            Node::Operator(Operator {
                name: "sklearn.preprocessing.StandardScaler".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_edge(Edge {
            source: "p".into(),
            destination: "load".into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        });
        g.add_edge(Edge {
            source: "load".into(),
            destination: "scale".into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        });

        let mut dem = DemAnnotations::new();
        for id in ["load", "scale"] {
            let mut a = ActorAnnotations::sdf_default();
            a.determinism = DeterminismClass::Deterministic;
            a.operator_version = Some("1.0.0".into());
            dem.actors.insert(id.into(), a);
        }
        (g, dem)
    }

    #[test]
    fn first_run_all_miss() {
        let (g, dem) = make_param_graph();
        let cache = MemoryStore::new();
        let sched = SdfScheduler::new(&cache);
        let firer = StubFirer { compute_secs: 0.05 };
        let report = sched.execute("run-1", &g, &dem, &firer).unwrap();
        assert_eq!(report.misses, 2);
        assert_eq!(report.hits, 0);
        assert_eq!(report.bypasses, 0);
    }

    #[test]
    fn second_run_all_hit() {
        let (g, dem) = make_param_graph();
        let cache = MemoryStore::new();
        let sched = SdfScheduler::new(&cache);
        let firer = StubFirer { compute_secs: 0.05 };
        sched.execute("run-1", &g, &dem, &firer).unwrap();
        let report = sched.execute("run-2", &g, &dem, &firer).unwrap();
        assert_eq!(report.hits, 2);
        assert_eq!(report.misses, 0);
    }

    #[test]
    fn non_deterministic_node_always_bypasses() {
        let (mut g, mut dem) = make_param_graph();
        // Add an LLM node downstream — forced non-deterministic.
        g.add_node(
            "llm".into(),
            Node::Operator(Operator {
                name: "openrouter.chat.completion".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_edge(Edge {
            source: "scale".into(),
            destination: "llm".into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        });
        let mut ann = ActorAnnotations::sdf_default();
        ann.determinism = DeterminismClass::NonDeterministic;
        dem.actors.insert("llm".into(), ann);

        let cache = MemoryStore::new();
        let sched = SdfScheduler::new(&cache);
        let firer = StubFirer { compute_secs: 0.1 };
        let r1 = sched.execute("run-1", &g, &dem, &firer).unwrap();
        let r2 = sched.execute("run-2", &g, &dem, &firer).unwrap();
        // LLM bypasses in both runs.
        assert_eq!(r1.bypasses, 1);
        assert_eq!(r2.bypasses, 1);
        // Deterministic upstream hits in the second run.
        assert_eq!(r2.hits, 2);
    }

    #[test]
    fn identical_subgraph_across_runs_collapses() {
        // Models the RL fan-out case: two "pipelines" with the same
        // loader + scaler share a cached pedigree, so run-2's loader
        // and scaler hit run-1's entries.
        let (g, dem) = make_param_graph();
        let cache = MemoryStore::new();
        let sched = SdfScheduler::new(&cache);
        let firer = StubFirer { compute_secs: 0.2 };
        let r1 = sched.execute("rl-pipe-1", &g, &dem, &firer).unwrap();
        let r2 = sched.execute("rl-pipe-2", &g, &dem, &firer).unwrap();
        assert_eq!(r1.misses, 2);
        assert_eq!(r2.hits, 2);
        // Cache has exactly 2 entries — dedup worked.
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn events_include_cache_hits_and_computed() {
        let (g, dem) = make_param_graph();
        let cache = MemoryStore::new();
        let sched = SdfScheduler::new(&cache);
        let firer = StubFirer { compute_secs: 0.01 };
        let r1 = sched.execute("a", &g, &dem, &firer).unwrap();
        let r2 = sched.execute("b", &g, &dem, &firer).unwrap();

        let has_computed = r1
            .events
            .iter()
            .any(|e| matches!(e, SchedulerEvent::NodeComputed { .. }));
        let has_hit = r2
            .events
            .iter()
            .any(|e| matches!(e, SchedulerEvent::NodeCacheHit { .. }));
        assert!(has_computed);
        assert!(has_hit);
    }
}
