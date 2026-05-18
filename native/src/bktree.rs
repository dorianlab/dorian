//! BK-Tree for pipeline similarity search using graph edit distance.
//!
//! The tree is fully in Rust — no Python callbacks during traversal.
//! Pipelines are stored with their sorted operator-name list for fast
//! lower-bound pruning before computing the expensive exact GED.

use crate::ged::{self, DagGraph};
use rustc_hash::FxHashSet;

/// A node in the BK-Tree.
struct BKNode {
    pipeline_id: String,
    operators: Vec<String>,
    /// Kept for round-trip reconstruction / telemetry even though
    /// the live traversal only consults ``dag_graph``. Removing it
    /// would force re-serialisation when a node has to be exported
    /// back to Python; cheaper to store once at insert time.
    #[allow(dead_code)]
    dag_json: serde_json::Value,
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
            dag_json,
            dag_graph,
            children: Vec::new(),
        }
    }

    fn add(
        &mut self,
        pipeline_id: String,
        operators: Vec<String>,
        dag_json: serde_json::Value,
        use_exact: bool,
        beam_limit: usize,
    ) {
        let other_graph = DagGraph::from_json(&dag_json);
        let d = if use_exact {
            ged::graph_edit_distance(&self.dag_graph, &other_graph, beam_limit)
        } else {
            ged::fast_distance(&self.dag_graph, &other_graph)
        };

        // Find existing child at this distance
        for (cd, child) in &mut self.children {
            if *cd == d {
                child.add(pipeline_id, operators, dag_json, use_exact, beam_limit);
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
        use_exact: bool,
        beam_limit: usize,
        results: &mut Vec<(String, usize)>,
    ) {
        // Fast lower bound: operator set symmetric difference
        let lb = ged::operator_set_distance(&self.operators, target_ops);

        let d = if lb <= max_distance {
            // Lower bound passes — compute real distance
            let d = if use_exact {
                ged::graph_edit_distance(&self.dag_graph, target_graph, beam_limit)
            } else {
                ged::fast_distance(&self.dag_graph, target_graph)
            };
            if d <= max_distance {
                results.push((self.pipeline_id.clone(), d));
            }
            d
        } else {
            lb // use lower bound for pruning range
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
}

impl PipelineBKTree {
    pub fn new(use_exact: bool, beam_limit: usize) -> Self {
        PipelineBKTree {
            root: None,
            size: 0,
            ids: FxHashSet::default(),
            use_exact,
            beam_limit,
        }
    }

    pub fn len(&self) -> usize {
        self.size
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
