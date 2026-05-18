//! BK-Tree for pipeline similarity search using graph edit distance.
//!
//! The tree is fully in Rust — no Python callbacks during traversal.
//! Pipelines are stored with their sorted operator-name list for fast
//! lower-bound pruning before computing the expensive exact GED.
//!
//! Two distance modes are supported:
//!
//!   * ``DistanceMode::Uniform`` (legacy default) — every node /
//!     edge edit costs 1. Integer distances throughout.
//!   * ``DistanceMode::Weighted`` — consults
//!     ``weighted_ged::substitute_cost`` with a ``TaskTopology``
//!     resolver so param-value edits ≪ same-family swaps ≪
//!     cross-family swaps. Internally scaled ×100 and rounded to
//!     usize so the tree's integer-pruning invariants stay intact.
//!     The integer representation is an optimization; the
//!     triangle inequality the greedy weighted lower-bound obeys
//!     survives scaling + rounding up to a constant additive error
//!     that's within the tree's pruning tolerance.

use std::sync::Arc;

use crate::ged::{self, DagGraph};
use crate::weighted_ged::{weighted_fast_distance, GedWeights, TaskTopology};
use rustc_hash::FxHashSet;

const WEIGHTED_SCALE: f64 = 100.0;

/// How the tree should score pairwise pipeline distance.
pub enum DistanceMode {
    /// Legacy uniform-cost GED. Integer distances as before.
    Uniform,
    /// Task-topology-aware weighted GED. See
    /// ``weighted_ged::GedWeights`` + ``TaskTopology`` for the
    /// cost tiers. ``Arc`` on both sides so multiple trees can
    /// share one resolver without clone-cost on each lookup.
    Weighted {
        weights: GedWeights,
        topology: Arc<dyn TaskTopology>,
    },
}

impl Default for DistanceMode {
    fn default() -> Self {
        DistanceMode::Uniform
    }
}

impl std::fmt::Debug for DistanceMode {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DistanceMode::Uniform => f.write_str("Uniform"),
            DistanceMode::Weighted { .. } => f.write_str("Weighted{..}"),
        }
    }
}

/// Compute pipeline distance according to the given mode.
///
/// Weighted mode scales by ``WEIGHTED_SCALE`` and rounds so the tree's
/// integer-distance invariants hold. Scaling error is bounded by 0.5
/// per call, negligible relative to the integer-valued tiers
/// (10 for param-value, 100 for same-task, 200+ for cross-task).
fn compute_distance(
    g1: &DagGraph,
    g2: &DagGraph,
    mode: &DistanceMode,
    use_exact: bool,
    beam_limit: usize,
) -> usize {
    match mode {
        DistanceMode::Uniform => {
            if use_exact {
                ged::graph_edit_distance(g1, g2, beam_limit)
            } else {
                ged::fast_distance(g1, g2)
            }
        }
        DistanceMode::Weighted { weights, topology } => {
            let raw = weighted_fast_distance(g1, g2, weights, topology.as_ref());
            (raw * WEIGHTED_SCALE).round().max(0.0) as usize
        }
    }
}


/// A node in the BK-Tree.
struct BKNode {
    pipeline_id: String,
    operators: Vec<String>,
    _dag_json: serde_json::Value,
    dag_graph: DagGraph,
    /// distance → child node
    children: Vec<(usize, BKNode)>,
}

impl BKNode {
    fn new(pipeline_id: String, operators: Vec<String>, dag_json: serde_json::Value) -> Self {
        let dag_graph = DagGraph::from_json(&dag_json);
        BKNode {
            pipeline_id,
            operators,
            _dag_json: dag_json,
            dag_graph,
            children: Vec::new(),
        }
    }

    fn add(
        &mut self,
        pipeline_id: String,
        operators: Vec<String>,
        dag_json: serde_json::Value,
        mode: &DistanceMode,
        use_exact: bool,
        beam_limit: usize,
    ) {
        let other_graph = DagGraph::from_json(&dag_json);
        let d = compute_distance(&self.dag_graph, &other_graph, mode, use_exact, beam_limit);

        // Find existing child at this distance
        for (cd, child) in &mut self.children {
            if *cd == d {
                child.add(pipeline_id, operators, dag_json, mode, use_exact, beam_limit);
                return;
            }
        }
        // No child at this distance — create one
        self.children
            .push((d, BKNode::new(pipeline_id, operators, dag_json)));
    }

    fn query(
        &self,
        target_ops: &[String],
        target_graph: &DagGraph,
        max_distance: usize,
        mode: &DistanceMode,
        use_exact: bool,
        beam_limit: usize,
        results: &mut Vec<(String, usize)>,
    ) {
        // Fast operator-set lower bound — valid only in uniform
        // mode, where operator-set symmetric difference is a
        // lower bound on the actual distance. Weighted mode uses
        // different cost tiers so skip the pre-check there.
        let uniform_lb_ok = matches!(mode, DistanceMode::Uniform)
            && ged::operator_set_distance(&self.operators, target_ops) <= max_distance;

        let d = if matches!(mode, DistanceMode::Weighted { .. }) || uniform_lb_ok {
            let d = compute_distance(
                &self.dag_graph, target_graph, mode, use_exact, beam_limit,
            );
            if d <= max_distance {
                results.push((self.pipeline_id.clone(), d));
            }
            d
        } else {
            // Uniform LB exceeded → prune with the LB as the
            // pivot for triangle-inequality child pruning.
            ged::operator_set_distance(&self.operators, target_ops)
        };

        // BK-Tree triangle inequality: visit children in [d - max, d + max]
        let lo = d.saturating_sub(max_distance);
        let hi = d + max_distance;
        for (cd, child) in &self.children {
            if *cd >= lo && *cd <= hi {
                child.query(
                    target_ops,
                    target_graph,
                    max_distance,
                    mode,
                    use_exact,
                    beam_limit,
                    results,
                );
            }
        }
    }
}

/// Python-facing BK-Tree for pipeline similarity search.
pub struct PipelineBKTree {
    root: Option<BKNode>,
    size: usize,
    ids: FxHashSet<String>,
    use_exact: bool,
    beam_limit: usize,
    mode: DistanceMode,
}

impl PipelineBKTree {
    pub fn new(use_exact: bool, beam_limit: usize) -> Self {
        PipelineBKTree {
            root: None,
            size: 0,
            ids: FxHashSet::default(),
            use_exact,
            beam_limit,
            mode: DistanceMode::Uniform,
        }
    }

    /// Build a tree that scores distance via task-topology-aware
    /// weighted GED. Integer distances returned by ``query`` /
    /// ``find_nearest`` are scaled by ``WEIGHTED_SCALE`` (×100); a
    /// param-value edit returns ~10, a same-family op swap ~100,
    /// a cross-family swap 200+ per hop.
    pub fn new_weighted(
        use_exact: bool,
        beam_limit: usize,
        weights: GedWeights,
        topology: Arc<dyn TaskTopology>,
    ) -> Self {
        PipelineBKTree {
            root: None,
            size: 0,
            ids: FxHashSet::default(),
            use_exact,
            beam_limit,
            mode: DistanceMode::Weighted { weights, topology },
        }
    }

    pub fn len(&self) -> usize {
        self.size
    }

    pub fn is_empty(&self) -> bool {
        self.size == 0
    }

    pub fn contains(&self, pipeline_id: &str) -> bool {
        self.ids.contains(pipeline_id)
    }

    /// Add a pipeline (JSON string) to the tree. Returns false if already present.
    pub fn add(&mut self, pipeline_id: &str, dag_json_str: &str) -> bool {
        if self.ids.contains(pipeline_id) {
            return false;
        }

        let dag_json: serde_json::Value = match serde_json::from_str(dag_json_str) {
            Ok(v) => v,
            Err(_) => return false,
        };

        let operators = ged::extract_operator_names(&dag_json);

        match &mut self.root {
            None => {
                self.root = Some(BKNode::new(
                    pipeline_id.to_string(),
                    operators,
                    dag_json,
                ));
            }
            Some(root) => {
                root.add(
                    pipeline_id.to_string(),
                    operators,
                    dag_json,
                    &self.mode,
                    self.use_exact,
                    self.beam_limit,
                );
            }
        }

        self.ids.insert(pipeline_id.to_string());
        self.size += 1;
        true
    }

    /// Query for all pipelines within max_distance edits.
    /// Returns Vec<(pipeline_id, distance)> sorted by distance ascending.
    pub fn query(&self, dag_json_str: &str, max_distance: usize) -> Vec<(String, usize)> {
        let root = match &self.root {
            Some(r) => r,
            None => return Vec::new(),
        };

        let dag_json: serde_json::Value = match serde_json::from_str(dag_json_str) {
            Ok(v) => v,
            Err(_) => return Vec::new(),
        };

        let target_ops = ged::extract_operator_names(&dag_json);
        let target_graph = DagGraph::from_json(&dag_json);

        let mut results = Vec::new();
        root.query(
            &target_ops,
            &target_graph,
            max_distance,
            &self.mode,
            self.use_exact,
            self.beam_limit,
            &mut results,
        );

        results.sort_by_key(|(_, d)| *d);
        results
    }

    /// Find the k nearest pipelines (up to max_distance).
    pub fn find_nearest(
        &self,
        dag_json_str: &str,
        k: usize,
        max_distance: usize,
    ) -> Vec<(String, usize)> {
        let mut results = self.query(dag_json_str, max_distance);
        results.truncate(k);
        results
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Helper: minimal pipeline DAG JSON with given operators and edges.
    fn make_dag(operators: &[&str], edges: &[(&str, &str)]) -> String {
        let mut nodes = serde_json::Map::new();
        for (i, &op) in operators.iter().enumerate() {
            let nid = format!("n{}", i);
            nodes.insert(
                nid,
                serde_json::json!({
                    "name": op,
                    "class_type": "Operator"
                }),
            );
        }
        let edge_arr: Vec<serde_json::Value> = edges
            .iter()
            .map(|(src, dst)| {
                serde_json::json!({
                    "source": src,
                    "destination": dst,
                    "position": 0,
                    "output": 0
                })
            })
            .collect();
        serde_json::json!({ "nodes": nodes, "edges": edge_arr }).to_string()
    }

    fn simple_dag() -> String {
        make_dag(
            &["sklearn.preprocessing.StandardScaler", "sklearn.linear_model.LogisticRegression"],
            &[("n0", "n1")],
        )
    }

    // ---- Empty tree ----

    #[test]
    fn empty_tree_query_returns_empty() {
        let tree = PipelineBKTree::new(false, 100);
        assert!(tree.is_empty());
        assert_eq!(tree.len(), 0);
        let results = tree.query(&simple_dag(), 10);
        assert!(results.is_empty());
    }

    #[test]
    fn empty_tree_find_nearest_returns_empty() {
        let tree = PipelineBKTree::new(false, 100);
        let results = tree.find_nearest(&simple_dag(), 5, 10);
        assert!(results.is_empty());
    }

    // ---- Insert and query ----

    #[test]
    fn insert_and_query_exact_match() {
        let mut tree = PipelineBKTree::new(false, 100);
        let dag = simple_dag();
        assert!(tree.add("p1", &dag));
        assert_eq!(tree.len(), 1);
        assert!(!tree.is_empty());

        let results = tree.query(&dag, 0);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].0, "p1");
        assert_eq!(results[0].1, 0);
    }

    #[test]
    fn insert_and_query_with_distance() {
        let mut tree = PipelineBKTree::new(false, 100);
        let dag1 = simple_dag();
        let dag2 = make_dag(
            &["sklearn.preprocessing.StandardScaler", "sklearn.ensemble.RandomForestClassifier"],
            &[("n0", "n1")],
        );

        tree.add("p1", &dag1);
        tree.add("p2", &dag2);

        // Query with dag1 at distance 0 — should only find p1
        let results = tree.query(&dag1, 0);
        assert!(results.iter().any(|(id, _)| id == "p1"));

        // Query with large distance — should find both
        let results = tree.query(&dag1, 20);
        assert!(results.len() >= 2);
    }

    // ---- Multiple entries with increasing distance thresholds ----

    #[test]
    fn increasing_distance_returns_more_results() {
        let mut tree = PipelineBKTree::new(false, 100);

        let dag_a = make_dag(&["op.A"], &[]);
        let dag_b = make_dag(&["op.A", "op.B"], &[("n0", "n1")]);
        let dag_c = make_dag(&["op.X", "op.Y", "op.Z"], &[("n0", "n1"), ("n1", "n2")]);

        tree.add("a", &dag_a);
        tree.add("b", &dag_b);
        tree.add("c", &dag_c);

        let r0 = tree.query(&dag_a, 0);
        let r5 = tree.query(&dag_a, 5);
        let r50 = tree.query(&dag_a, 50);

        assert!(r0.len() <= r5.len());
        assert!(r5.len() <= r50.len());
    }

    // ---- Duplicate insert ----

    #[test]
    fn duplicate_insert_returns_false_and_no_double_count() {
        let mut tree = PipelineBKTree::new(false, 100);
        let dag = simple_dag();

        assert!(tree.add("p1", &dag));
        assert!(!tree.add("p1", &dag));
        assert_eq!(tree.len(), 1);

        let results = tree.query(&dag, 0);
        assert_eq!(results.len(), 1);
    }

    // ---- find_nearest ----

    #[test]
    fn find_nearest_returns_top_k_sorted() {
        let mut tree = PipelineBKTree::new(false, 100);

        let dag_a = make_dag(&["op.A"], &[]);
        let dag_b = make_dag(&["op.A", "op.B"], &[("n0", "n1")]);
        let dag_c = make_dag(&["op.X", "op.Y", "op.Z"], &[("n0", "n1"), ("n1", "n2")]);

        tree.add("a", &dag_a);
        tree.add("b", &dag_b);
        tree.add("c", &dag_c);

        // Ask for top-2 within large distance
        let results = tree.find_nearest(&dag_a, 2, 50);
        assert!(results.len() <= 2);

        // Verify sorted by distance ascending
        for w in results.windows(2) {
            assert!(w[0].1 <= w[1].1);
        }
    }

    #[test]
    fn find_nearest_k_larger_than_tree() {
        let mut tree = PipelineBKTree::new(false, 100);
        let dag = simple_dag();
        tree.add("p1", &dag);

        let results = tree.find_nearest(&dag, 100, 50);
        assert_eq!(results.len(), 1);
    }

    // ---- Invalid/empty JSON ----

    #[test]
    fn add_invalid_json_returns_false() {
        let mut tree = PipelineBKTree::new(false, 100);
        assert!(!tree.add("bad", "not valid json {{{"));
        assert_eq!(tree.len(), 0);
    }

    #[test]
    fn query_invalid_json_returns_empty() {
        let mut tree = PipelineBKTree::new(false, 100);
        tree.add("p1", &simple_dag());

        let results = tree.query("not json", 10);
        assert!(results.is_empty());
    }

    #[test]
    fn add_empty_json_object() {
        let mut tree = PipelineBKTree::new(false, 100);
        assert!(tree.add("empty", "{}"));
        assert_eq!(tree.len(), 1);

        // Querying the empty dag against itself should yield distance 0
        let results = tree.query("{}", 0);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].1, 0);
    }

    #[test]
    fn query_empty_json_against_populated_tree() {
        let mut tree = PipelineBKTree::new(false, 100);
        tree.add("p1", &simple_dag());

        // Empty query DAG — distance > 0 from a real pipeline
        let results = tree.query("{}", 0);
        // May or may not find p1 at distance 0 — it shouldn't since they differ
        // Just ensure no panic
        for (_, d) in &results {
            assert_eq!(*d, 0);
        }
    }

    // ---- contains ----

    #[test]
    fn contains_check() {
        let mut tree = PipelineBKTree::new(false, 100);
        assert!(!tree.contains("p1"));

        tree.add("p1", &simple_dag());
        assert!(tree.contains("p1"));
        assert!(!tree.contains("p2"));
    }

    // ---- Results are sorted ----

    #[test]
    fn query_results_sorted_by_distance() {
        let mut tree = PipelineBKTree::new(false, 100);

        for i in 0..5 {
            let ops: Vec<&str> = (0..=i).map(|j| match j {
                0 => "op.A",
                1 => "op.B",
                2 => "op.C",
                3 => "op.D",
                _ => "op.E",
            }).collect();
            let dag = make_dag(&ops, &[]);
            tree.add(&format!("p{}", i), &dag);
        }

        let query = make_dag(&["op.A"], &[]);
        let results = tree.query(&query, 50);

        for w in results.windows(2) {
            assert!(w[0].1 <= w[1].1, "results not sorted: {} > {}", w[0].1, w[1].1);
        }
    }

    // ---- Exact mode ----

    #[test]
    fn exact_mode_insert_and_query() {
        let mut tree = PipelineBKTree::new(true, 50);
        let dag = simple_dag();
        tree.add("p1", &dag);

        let results = tree.query(&dag, 0);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].0, "p1");
        assert_eq!(results[0].1, 0);
    }
}
