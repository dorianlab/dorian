//! Experiment-Graph-style flat index over the cache (Derakhshan Ch 2).
//!
//! Linear-time reuse matcher: given a new pipeline graph, walk its
//! vertices in topological order, compute the cache key for each,
//! look it up in the flat index. Hits become "materialised prefix"
//! information for the scheduler; misses become a priority-ordered
//! list of things to fire.
//!
//! The flat index shape is `FxHashMap<CacheKey, Arc<CacheEntry>>`
//! exactly like `MemoryStore` but exposed separately so it can be
//! populated independently of a store (e.g. from a Redis scan, from
//! a dump file, or from a warmstart bootstrap).
//!
//! v1 ships:
//!   * `ExperimentGraphIndex` — flat key → entry map.
//!   * `match_pipeline(index, graph, annotations) -> ReuseMatch`
//!     — walks one pipeline, returns hits + miss list + pedigree.
//!   * `plan_batch(index, graphs, annotations) -> BatchPlan`
//!     — walks N pipelines, collapses duplicate cache-keys so the
//!     scheduler fires each unique node once. This is the RL
//!     fan-out reduction primitive.
//!
//! Cost model for benefit-driven eviction lives in `cache::benefit`.

use std::sync::Arc;

use rustc_hash::{FxHashMap, FxHashSet};

use graph::dem::{DemAnnotations, DomainKind};
use graph::model::{Node, ProcessGraph};
use graph::topology::topological_sort;

use crate::{
    compute_key, eligibility_with_incoming, extract_param_bindings, incoming_param_handles,
    CacheEntry, CacheKey, Eligibility, KeyInputs,
};

// ---------------------------------------------------------------------------
// ExperimentGraphIndex
// ---------------------------------------------------------------------------

/// Flat key → entry index. Separately populated from any specific
/// store so bootstrap paths (warmstart files, Redis SCAN snapshots)
/// can build an index without committing to a backend.
#[derive(Debug, Default)]
pub struct ExperimentGraphIndex {
    entries: FxHashMap<CacheKey, Arc<CacheEntry>>,
}

impl ExperimentGraphIndex {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, entry: CacheEntry) {
        self.entries.insert(entry.key, Arc::new(entry));
    }

    pub fn insert_arc(&mut self, entry: Arc<CacheEntry>) {
        self.entries.insert(entry.key, entry);
    }

    pub fn contains(&self, key: &CacheKey) -> bool {
        self.entries.contains_key(key)
    }

    pub fn get(&self, key: &CacheKey) -> Option<Arc<CacheEntry>> {
        self.entries.get(key).cloned()
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

// ---------------------------------------------------------------------------
// ReuseMatch — result of matching one pipeline against the index
// ---------------------------------------------------------------------------

/// Per-pipeline reuse match. `node_keys` records the computed cache
/// key for every deterministic node (cache-ineligible nodes are
/// absent — they have no key). `hits` are the subset of keys the
/// index served; `misses` are the keys to fire.
#[derive(Debug, Default, Clone)]
pub struct ReuseMatch {
    pub node_keys: FxHashMap<String, CacheKey>,
    pub hits: FxHashMap<String, Arc<CacheEntry>>,
    pub misses: Vec<String>,
    pub bypassed: Vec<String>,
}

impl ReuseMatch {
    pub fn hit_ratio(&self) -> f64 {
        let total = self.hits.len() + self.misses.len();
        if total == 0 {
            0.0
        } else {
            self.hits.len() as f64 / total as f64
        }
    }
}

/// Cache-affinity score for a single pipeline: fraction of its
/// cacheable nodes that the index can serve today. Used as a
/// logit-nudge signal in the next-gen RL agent — see
/// internal design note.
pub fn cache_affinity(
    index: &ExperimentGraphIndex,
    graph: &ProcessGraph,
    annotations: &DemAnnotations,
) -> f64 {
    match_pipeline(index, graph, annotations).hit_ratio()
}

/// Walk a pipeline's SDF nodes in topological order; compute each
/// node's cache key from upstream pedigree + params; check the
/// index; record hits, misses, bypasses.
///
/// Non-SDF (DE) and parameter nodes are passed through without a key.
pub fn match_pipeline(
    index: &ExperimentGraphIndex,
    graph: &ProcessGraph,
    annotations: &DemAnnotations,
) -> ReuseMatch {
    let mut result = ReuseMatch::default();
    let topo = match topological_sort(graph) {
        Ok(t) => t,
        Err(_) => return result,
    };
    for node_id in &topo {
        let node = match graph.get_node(node_id) {
            Some(n) => n,
            None => continue,
        };
        if matches!(node, Node::Parameter(_)) {
            continue;
        }
        let ann = match annotations.actor(node_id) {
            Some(a) => a,
            None => continue,
        };
        if ann.domain != DomainKind::Sdf {
            continue;
        }
        let handles = incoming_param_handles(graph, node_id);
        let handle_refs: Vec<&str> = handles.iter().map(String::as_str).collect();
        match eligibility_with_incoming(ann, &handle_refs) {
            Eligibility::Bypass => {
                result.bypassed.push(node_id.clone());
            }
            Eligibility::Cacheable => {
                // Gather upstream cache keys (data edges only).
                let mut upstream_keys: Vec<CacheKey> = Vec::new();
                for edge in graph.incoming_edges(node_id) {
                    if matches!(
                        graph.get_node(&edge.source),
                        Some(Node::Parameter(_))
                    ) {
                        continue;
                    }
                    if let Some(k) = result.node_keys.get(&edge.source) {
                        upstream_keys.push(*k);
                    }
                }
                let op_fqn = operator_fqn(node);
                let op_tasks = operator_tasks(node);
                let params = extract_param_bindings(graph, node_id);
                let inputs = KeyInputs {
                    op_fqn: &op_fqn,
                    op_tasks: &op_tasks,
                    op_version: ann.operator_version.as_deref(),
                    params,
                    upstream_keys,
                    root_content_hash: None,
                };
                let key = compute_key(&inputs);
                result.node_keys.insert(node_id.clone(), key);
                if let Some(entry) = index.get(&key) {
                    result.hits.insert(node_id.clone(), entry);
                } else {
                    result.misses.push(node_id.clone());
                }
            }
        }
    }
    result
}

fn operator_tasks(node: &Node) -> Vec<String> {
    match node {
        Node::Operator(o) => o.tasks.clone(),
        _ => Vec::new(),
    }
}

fn operator_fqn(node: &Node) -> String {
    match node {
        Node::Operator(o) => o.name.clone(),
        Node::Snippet(s) => format!("snippet::{}", s.name),
        Node::Parameter(p) => format!("param::{}", p.name),
        Node::Node(n) => format!("pattern::{}", n.text),
        Node::Group(g) => format!("group::{}", g.name),
    }
}

// ---------------------------------------------------------------------------
// Batch planning — RL fan-out dedup
// ---------------------------------------------------------------------------

/// Plan for a batch of pipelines. The scheduler fires each entry in
/// `unique_misses` exactly once, then serves the result to every
/// pipeline that depended on it. `per_pipeline` records the
/// pipeline-level reuse match for observability + downstream event
/// wiring.
#[derive(Debug, Default)]
pub struct BatchPlan {
    pub per_pipeline: Vec<ReuseMatch>,
    /// All cache keys across all pipelines — unique.
    pub unique_misses: Vec<CacheKey>,
    /// `unique_misses` entries that were served directly from the
    /// index (hit) rather than queued for firing.
    pub unique_hits: Vec<CacheKey>,
    /// Count of (pipeline, node) pairs that would have fired without
    /// batching but collapse to a shared firing under batching.
    pub collapsed_firings: usize,
}

impl BatchPlan {
    /// Total unique nodes to fire — the RL fan-out "real" compute cost.
    pub fn unique_fire_count(&self) -> usize {
        self.unique_misses.len()
    }

    /// Total naive fire count (no batching) — hits + misses across
    /// all pipelines.
    pub fn naive_fire_count(&self) -> usize {
        self.per_pipeline
            .iter()
            .map(|r| r.hits.len() + r.misses.len())
            .sum()
    }

    /// Ratio of naive firings saved by batching. 1.0 means fully
    /// collapsed (single deduplicated DAG); 0.0 means no overlap.
    pub fn collapse_ratio(&self) -> f64 {
        let naive = self.naive_fire_count();
        if naive == 0 {
            0.0
        } else {
            self.collapsed_firings as f64 / naive as f64
        }
    }
}

/// Plan a batch of N pipelines against the index. Keys appearing
/// across multiple pipelines collapse to a single firing.
pub fn plan_batch(
    index: &ExperimentGraphIndex,
    graphs: &[&ProcessGraph],
    annotations: &[&DemAnnotations],
) -> BatchPlan {
    assert_eq!(graphs.len(), annotations.len(), "graphs/annotations len mismatch");
    let mut plan = BatchPlan::default();
    let mut seen_keys: FxHashSet<CacheKey> = FxHashSet::default();
    let mut unique_misses: Vec<CacheKey> = Vec::new();
    let mut unique_hits: Vec<CacheKey> = Vec::new();

    let mut hit_keys_seen: FxHashSet<CacheKey> = FxHashSet::default();
    let mut miss_keys_seen: FxHashSet<CacheKey> = FxHashSet::default();

    for (g, ann) in graphs.iter().zip(annotations.iter()) {
        let m = match_pipeline(index, g, ann);
        for (node_id, key) in &m.node_keys {
            if seen_keys.insert(*key) {
                // First occurrence overall.
                if m.hits.contains_key(node_id) {
                    unique_hits.push(*key);
                    hit_keys_seen.insert(*key);
                } else {
                    unique_misses.push(*key);
                    miss_keys_seen.insert(*key);
                }
            } else {
                plan.collapsed_firings += 1;
            }
        }
        plan.per_pipeline.push(m);
    }

    plan.unique_hits = unique_hits;
    plan.unique_misses = unique_misses;
    plan
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Artifact, CacheEntry};
    use graph::dem::{ActorAnnotations, DeterminismClass};
    use graph::model::{DeliveryMode, Edge, Operator, ParamDtype, Parameter, Position};
    use serde_json::json;

    fn mk_det_annotations() -> ActorAnnotations {
        let mut a = ActorAnnotations::sdf_default();
        a.determinism = DeterminismClass::Deterministic;
        a.operator_version = Some("1.0".into());
        a
    }

    fn linear_pipeline(path: &str) -> (ProcessGraph, DemAnnotations) {
        let mut g = ProcessGraph::new();
        g.add_node(
            "p".into(),
            Node::Parameter(Parameter {
                name: "path".into(),
                dtype: ParamDtype::String,
                value: path.into(),
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
        dem.actors.insert("load".into(), mk_det_annotations());
        dem.actors.insert("scale".into(), mk_det_annotations());
        (g, dem)
    }

    #[test]
    fn empty_index_yields_all_misses() {
        let (g, ann) = linear_pipeline("a.csv");
        let idx = ExperimentGraphIndex::new();
        let m = match_pipeline(&idx, &g, &ann);
        assert_eq!(m.hits.len(), 0);
        assert_eq!(m.misses.len(), 2);
        assert!(m.bypassed.is_empty());
        assert_eq!(m.hit_ratio(), 0.0);
    }

    #[test]
    fn identical_pipelines_share_keys() {
        let (g1, ann1) = linear_pipeline("a.csv");
        let (g2, ann2) = linear_pipeline("a.csv");
        let idx = ExperimentGraphIndex::new();
        let m1 = match_pipeline(&idx, &g1, &ann1);
        let m2 = match_pipeline(&idx, &g2, &ann2);
        // Same keys for the same node ids.
        for (id, k) in &m1.node_keys {
            assert_eq!(m2.node_keys.get(id), Some(k));
        }
    }

    #[test]
    fn different_params_yield_different_keys() {
        let (g1, ann1) = linear_pipeline("a.csv");
        let (g2, ann2) = linear_pipeline("b.csv");
        let idx = ExperimentGraphIndex::new();
        let m1 = match_pipeline(&idx, &g1, &ann1);
        let m2 = match_pipeline(&idx, &g2, &ann2);
        assert_ne!(
            m1.node_keys.get("load").unwrap(),
            m2.node_keys.get("load").unwrap()
        );
        // Scale downstream also differs — pedigree propagates.
        assert_ne!(
            m1.node_keys.get("scale").unwrap(),
            m2.node_keys.get("scale").unwrap()
        );
    }

    #[test]
    fn batch_plan_collapses_shared_subgraph() {
        // Two pipelines reading the same file → same keys → batch
        // collapses one firing per node.
        let (g1, ann1) = linear_pipeline("a.csv");
        let (g2, ann2) = linear_pipeline("a.csv");
        let idx = ExperimentGraphIndex::new();
        let plan = plan_batch(&idx, &[&g1, &g2], &[&ann1, &ann2]);
        // 2 pipelines × 2 det nodes = 4 naive firings; batching
        // collapses to 2 unique + 2 collapsed.
        assert_eq!(plan.naive_fire_count(), 4);
        assert_eq!(plan.unique_fire_count(), 2);
        assert_eq!(plan.collapsed_firings, 2);
        assert!((plan.collapse_ratio() - 0.5).abs() < 1e-9);
    }

    #[test]
    fn batch_plan_no_overlap_no_collapse() {
        let (g1, ann1) = linear_pipeline("a.csv");
        let (g2, ann2) = linear_pipeline("b.csv");
        let idx = ExperimentGraphIndex::new();
        let plan = plan_batch(&idx, &[&g1, &g2], &[&ann1, &ann2]);
        assert_eq!(plan.naive_fire_count(), 4);
        assert_eq!(plan.unique_fire_count(), 4);
        assert_eq!(plan.collapsed_firings, 0);
    }

    #[test]
    fn populated_index_yields_hits_in_match() {
        let (g, ann) = linear_pipeline("a.csv");
        // Pre-compute the load key and plant it.
        let m_first = match_pipeline(&ExperimentGraphIndex::new(), &g, &ann);
        let load_key = *m_first.node_keys.get("load").unwrap();
        let mut idx = ExperimentGraphIndex::new();
        idx.insert(CacheEntry::new(
            load_key,
            Artifact::Feature,
            json!(null),
            0.01,
        ));
        let m = match_pipeline(&idx, &g, &ann);
        assert_eq!(m.hits.len(), 1);
        assert!(m.hits.contains_key("load"));
        assert_eq!(m.misses.len(), 1);
        assert_eq!(m.misses[0], "scale");
    }

    #[test]
    fn non_deterministic_is_bypassed_in_match() {
        let mut g = ProcessGraph::new();
        g.add_node(
            "llm".into(),
            Node::Operator(Operator {
                name: "openrouter.chat.completion".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        let mut dem = DemAnnotations::new();
        let mut a = ActorAnnotations::sdf_default();
        a.determinism = DeterminismClass::NonDeterministic;
        dem.actors.insert("llm".into(), a);
        let idx = ExperimentGraphIndex::new();
        let m = match_pipeline(&idx, &g, &dem);
        assert_eq!(m.bypassed, vec!["llm"]);
        assert_eq!(m.hits.len(), 0);
        assert_eq!(m.misses.len(), 0);
        assert_eq!(m.node_keys.len(), 0);
    }
}
