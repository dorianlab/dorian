//! Shadow-mode comparator.
//!
//! During the migration from Dask to the Rust engine we need a way
//! to run both in parallel and confirm they agree, for every
//! in-flight pipeline. Mirrors the `/observability/eventbus-
//! discrepancy` shape used during the Go event-bus migration.
//!
//! This crate ships a pure comparator:
//!
//!   * `ShadowRunner` — runs a `ProcessGraph` through two `Firer`s
//!     (the "authoritative" and "candidate" implementations).
//!   * `ShadowReport` — per-node outcomes + a discrepancy list.
//!   * `Discrepancy` kinds: Missing, Extra, PayloadMismatch,
//!     FireOrderMismatch.
//!
//! The default comparator treats payloads as `serde_json::Value` and
//! compares by canonicalised JSON string (order-independent for
//! objects, order-preserving for arrays — matches the cache key's
//! canonicalisation). Callers with stricter needs (bytewise Arrow
//! buffer comparison, numeric ε thresholds) can plug in their own
//! `PayloadComparator`.
//!
//! The comparator does not *itself* run Dask — that's the caller's
//! job. Typical use from `engine/engine/`:
//!
//! ```ignore
//! let dask_firer  = PythonDaskFirer::new(...);   // dispatches to
//!                                                 // existing Python
//! let rust_firer  = RustScheduledFirer::new(...);// SdfScheduler
//! let runner      = ShadowRunner::new(&cache);
//! let report      = runner.run(&graph, &dem,
//!                              &dask_firer, &rust_firer);
//! if !report.discrepancies.is_empty() {
//!     emit_alert(&report);
//! }
//! ```

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

use cache::{Artifact, CacheEntry, MemoryStore};
use graph::dem::DemAnnotations;
use graph::model::ProcessGraph;
use sdf::{Firer, SdfScheduler};

// ---------------------------------------------------------------------------
// Outcomes
// ---------------------------------------------------------------------------

/// Minimal per-node outcome captured by each side of the comparator.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NodeOutcome {
    pub node_id: String,
    pub payload: serde_json::Value,
    pub compute_secs: f64,
    pub artifact: Artifact,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum Discrepancy {
    /// Present in authoritative but absent in candidate.
    Missing { node_id: String },
    /// Present in candidate but absent in authoritative.
    Extra { node_id: String },
    /// Both fired but produced different payloads (after
    /// canonicalisation).
    PayloadMismatch {
        node_id: String,
        authoritative: serde_json::Value,
        candidate: serde_json::Value,
    },
    /// Artifact-type tags disagree.
    ArtifactMismatch {
        node_id: String,
        authoritative: Artifact,
        candidate: Artifact,
    },
    /// Both fired but in different orders — non-blocking for pure
    /// dataflow, relevant for DE.
    FireOrderMismatch {
        node_id: String,
        authoritative_index: usize,
        candidate_index: usize,
    },
}

#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct ShadowReport {
    pub authoritative: Vec<NodeOutcome>,
    pub candidate: Vec<NodeOutcome>,
    pub discrepancies: Vec<Discrepancy>,
}

impl ShadowReport {
    pub fn clean(&self) -> bool {
        self.discrepancies.is_empty()
    }
    pub fn discrepancy_count(&self) -> usize {
        self.discrepancies.len()
    }
}

// ---------------------------------------------------------------------------
// PayloadComparator
// ---------------------------------------------------------------------------

/// Plug-in for comparing payloads. Default impl uses canonicalised
/// JSON equality; callers with numeric tolerance plug in their own.
pub trait PayloadComparator {
    fn equivalent(&self, a: &serde_json::Value, b: &serde_json::Value) -> bool;
}

pub struct CanonicalJsonComparator;

impl PayloadComparator for CanonicalJsonComparator {
    fn equivalent(&self, a: &serde_json::Value, b: &serde_json::Value) -> bool {
        cache::canonicalise_json(a) == cache::canonicalise_json(b)
    }
}

// ---------------------------------------------------------------------------
// ShadowRunner
// ---------------------------------------------------------------------------

pub struct ShadowRunner;

impl Default for ShadowRunner {
    fn default() -> Self {
        Self::new()
    }
}

impl ShadowRunner {
    pub fn new() -> Self {
        ShadowRunner
    }

    /// Run both firers through the SDF scheduler over the same
    /// graph and diff the outcomes. Each side gets its own fresh
    /// `MemoryStore` so cache hits don't leak across comparisons.
    pub fn run(
        &self,
        run_id: &str,
        graph: &ProcessGraph,
        annotations: &DemAnnotations,
        authoritative: &dyn Firer,
        candidate: &dyn Firer,
    ) -> Result<ShadowReport, ShadowError> {
        self.run_with(
            run_id,
            graph,
            annotations,
            authoritative,
            candidate,
            &CanonicalJsonComparator,
        )
    }

    pub fn run_with(
        &self,
        run_id: &str,
        graph: &ProcessGraph,
        annotations: &DemAnnotations,
        authoritative: &dyn Firer,
        candidate: &dyn Firer,
        comparator: &dyn PayloadComparator,
    ) -> Result<ShadowReport, ShadowError> {
        let auth_outcomes = collect(run_id, graph, annotations, authoritative)?;
        let cand_outcomes = collect(run_id, graph, annotations, candidate)?;
        let discrepancies = compare(&auth_outcomes, &cand_outcomes, comparator);
        Ok(ShadowReport {
            authoritative: auth_outcomes,
            candidate: cand_outcomes,
            discrepancies,
        })
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ShadowError {
    #[error("scheduler error: {0}")]
    Scheduler(String),
}

fn collect(
    run_id: &str,
    graph: &ProcessGraph,
    annotations: &DemAnnotations,
    firer: &dyn Firer,
) -> Result<Vec<NodeOutcome>, ShadowError> {
    let cache = MemoryStore::new();
    let sched = SdfScheduler::new(&cache);
    let collector = CollectingFirer::new(firer);
    sched
        .execute(run_id, graph, annotations, &collector)
        .map_err(|e| ShadowError::Scheduler(e.to_string()))?;
    Ok(collector.into_outcomes())
}

/// Wraps a user-supplied firer, captures each firing's outcome into
/// an ordered list for comparison.
struct CollectingFirer<'a> {
    inner: &'a dyn Firer,
    outcomes: parking_lot_no_dep::Mutex<Vec<NodeOutcome>>,
}

// Use stdlib Mutex to keep the shadow crate dep-slim; we don't take
// a direct parking_lot dep here.
mod parking_lot_no_dep {
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
        pub fn into_inner(self) -> T {
            self.inner.into_inner().unwrap()
        }
    }
}

impl<'a> CollectingFirer<'a> {
    fn new(inner: &'a dyn Firer) -> Self {
        CollectingFirer {
            inner,
            outcomes: parking_lot_no_dep::Mutex::new(Vec::new()),
        }
    }

    fn into_outcomes(self) -> Vec<NodeOutcome> {
        self.outcomes.into_inner()
    }
}

impl<'a> Firer for CollectingFirer<'a> {
    fn fire(
        &self,
        run_id: &str,
        node_id: &str,
        graph: &ProcessGraph,
        upstream: &[std::sync::Arc<CacheEntry>],
    ) -> Result<sdf::FiredResult, sdf::FireError> {
        let result = self.inner.fire(run_id, node_id, graph, upstream)?;
        self.outcomes.lock().push(NodeOutcome {
            node_id: node_id.to_string(),
            payload: result.payload.clone(),
            compute_secs: result.compute_secs,
            artifact: result.artifact,
        });
        Ok(result)
    }
}

// ---------------------------------------------------------------------------
// Diff
// ---------------------------------------------------------------------------

fn compare(
    authoritative: &[NodeOutcome],
    candidate: &[NodeOutcome],
    comparator: &dyn PayloadComparator,
) -> Vec<Discrepancy> {
    let mut discs = Vec::new();

    let auth_by_id: FxHashMap<&str, (usize, &NodeOutcome)> = authoritative
        .iter()
        .enumerate()
        .map(|(i, o)| (o.node_id.as_str(), (i, o)))
        .collect();
    let cand_by_id: FxHashMap<&str, (usize, &NodeOutcome)> = candidate
        .iter()
        .enumerate()
        .map(|(i, o)| (o.node_id.as_str(), (i, o)))
        .collect();

    for (id, (ai, a)) in &auth_by_id {
        match cand_by_id.get(*id) {
            None => discs.push(Discrepancy::Missing {
                node_id: id.to_string(),
            }),
            Some((ci, c)) => {
                if !comparator.equivalent(&a.payload, &c.payload) {
                    discs.push(Discrepancy::PayloadMismatch {
                        node_id: id.to_string(),
                        authoritative: a.payload.clone(),
                        candidate: c.payload.clone(),
                    });
                }
                if a.artifact != c.artifact {
                    discs.push(Discrepancy::ArtifactMismatch {
                        node_id: id.to_string(),
                        authoritative: a.artifact,
                        candidate: c.artifact,
                    });
                }
                if ai != ci {
                    discs.push(Discrepancy::FireOrderMismatch {
                        node_id: id.to_string(),
                        authoritative_index: *ai,
                        candidate_index: *ci,
                    });
                }
            }
        }
    }
    for (id, _) in &cand_by_id {
        if !auth_by_id.contains_key(*id) {
            discs.push(Discrepancy::Extra {
                node_id: id.to_string(),
            });
        }
    }

    discs
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use cache::Artifact;
    use graph::dem::{ActorAnnotations, DeterminismClass};
    use graph::model::{DeliveryMode, Edge, Node, Operator, ParamDtype, Parameter, Position};
    use serde_json::json;
    use std::sync::Arc;

    struct AgreeingFirer;
    impl Firer for AgreeingFirer {
        fn fire(
            &self,
            _run_id: &str,
            node_id: &str,
            _graph: &ProcessGraph,
            _upstream: &[Arc<CacheEntry>],
        ) -> Result<sdf::FiredResult, sdf::FireError> {
            Ok(sdf::FiredResult {
                payload: json!({"n": node_id}),
                compute_secs: 0.01,
                artifact: Artifact::Feature,
            })
        }
    }

    struct PayloadDiffFirer;
    impl Firer for PayloadDiffFirer {
        fn fire(
            &self,
            _run_id: &str,
            node_id: &str,
            _graph: &ProcessGraph,
            _upstream: &[Arc<CacheEntry>],
        ) -> Result<sdf::FiredResult, sdf::FireError> {
            Ok(sdf::FiredResult {
                payload: json!({"n": node_id, "differs": true}),
                compute_secs: 0.01,
                artifact: Artifact::Feature,
            })
        }
    }

    fn linear(nodes: &[&str]) -> (ProcessGraph, DemAnnotations) {
        let mut g = ProcessGraph::new();
        for n in nodes {
            g.add_node(
                (*n).to_string(),
                Node::Operator(Operator {
                    name: format!("sklearn.X.{}", n),
                    language: "python".into(),
                    tasks: vec![],
                }),
            );
        }
        for pair in nodes.windows(2) {
            g.add_edge(Edge {
                source: pair[0].to_string(),
                destination: pair[1].to_string(),
                position: Position::Index(0),
                output: Position::Index(0),
                delivery_mode: DeliveryMode::Once,
            });
        }
        let mut dem = DemAnnotations::new();
        for n in nodes {
            let mut a = ActorAnnotations::sdf_default();
            a.determinism = DeterminismClass::Deterministic;
            a.operator_version = Some("1.0".into());
            dem.actors.insert((*n).to_string(), a);
        }
        (g, dem)
    }

    #[test]
    fn identical_firers_produce_clean_report() {
        let (g, dem) = linear(&["a", "b", "c"]);
        let runner = ShadowRunner::new();
        let report = runner
            .run("r1", &g, &dem, &AgreeingFirer, &AgreeingFirer)
            .unwrap();
        assert!(report.clean(), "{:?}", report.discrepancies);
        assert_eq!(report.authoritative.len(), 3);
        assert_eq!(report.candidate.len(), 3);
    }

    #[test]
    fn payload_mismatch_is_detected() {
        let (g, dem) = linear(&["a", "b"]);
        let runner = ShadowRunner::new();
        let report = runner
            .run("r1", &g, &dem, &AgreeingFirer, &PayloadDiffFirer)
            .unwrap();
        assert!(!report.clean());
        assert!(report
            .discrepancies
            .iter()
            .any(|d| matches!(d, Discrepancy::PayloadMismatch { .. })));
    }

    #[test]
    fn parameter_nodes_dont_show_up_in_outcomes() {
        let mut g = ProcessGraph::new();
        g.add_node(
            "p".into(),
            Node::Parameter(Parameter {
                name: "x".into(),
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
        g.add_edge(Edge {
            source: "p".into(),
            destination: "op".into(),
            position: Position::Keyword("x".into()),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        });
        let mut dem = DemAnnotations::new();
        let mut a = ActorAnnotations::sdf_default();
        a.determinism = DeterminismClass::Deterministic;
        a.operator_version = Some("1".into());
        dem.actors.insert("op".into(), a);

        let runner = ShadowRunner::new();
        let report = runner
            .run("r1", &g, &dem, &AgreeingFirer, &AgreeingFirer)
            .unwrap();
        assert_eq!(report.authoritative.len(), 1);
        assert_eq!(report.authoritative[0].node_id, "op");
    }
}
