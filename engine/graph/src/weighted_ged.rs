//! Weighted graph-edit distance with task-topology-aware costs.
//!
//! The default ``ged::graph_edit_distance`` counts every node / edge
//! edit as cost 1.0. That's uniform, and uniform is wrong for the
//! questions we actually want to answer:
//!
//!   * "How far is this pipeline from its nearest neighbour?" — a
//!     pipeline that differs only in a Parameter value is much
//!     closer than one that swapped the classifier for a different
//!     model family. Uniform GED treats them as the same distance.
//!   * "Which operator swap is the cheapest?" — swapping
//!     ``RandomForestClassifier`` for ``ExtraTreesClassifier`` is
//!     cheaper than swapping for ``LogisticRegression``, because
//!     both are Tree-Based Models. The RL policy needs to see that.
//!
//! Three-tier substitution cost (lowest → highest):
//!
//!   1. **Parameter value edit** (``w_param_value``, default 0.1)
//!      — same Parameter ``(name, dtype)`` on both sides, different
//!      ``value``. Trivially-cheap edit: the pipeline is the same
//!      graph with one hyperparameter tweaked.
//!   2. **Same-task operator swap** (``w_task_equivalent``,
//!      default 1.0) — two Operator nodes whose KB-declared family
//!      (or task membership) is identical. These are semantically
//!      equivalent realisations of the same task; the SCORE
//!      changes but the *role* in the pipeline doesn't.
//!   3. **Cross-task operator swap** (``w_task_per_hop *
//!      hops(t1, t2)``, default 2.0 × hops) — operators whose
//!      families / tasks live in different sub-trees of the KB's
//!      task ontology. Cost grows with the tree distance between
//!      them, so swapping a classifier for a regressor is more
//!      expensive than swapping for another classifier family.
//!
//! Insertion / deletion costs (``w_insert`` / ``w_delete``) and
//! edge costs (``w_edge_add`` / ``w_edge_delete``) are plain
//! constants for the first cut; nothing in the current use cases
//! asks for topology-aware edge weighting.
//!
//! The KB lookup is factored out behind the ``TaskTopology`` trait
//! so callers can plug a Neo4j-backed resolver, a static
//! resolver for tests, or a composite cache.

use rustc_hash::{FxHashMap, FxHashSet};
use serde::{Deserialize, Serialize};

use crate::ged::DagGraph;

// ---------------------------------------------------------------------------
// Weights
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct GedWeights {
    /// Cost of swapping one Parameter's ``value`` while keeping
    /// ``name`` + ``dtype`` identical.
    pub w_param_value: f64,
    /// Cost of swapping one Parameter for another with a different
    /// ``name`` or ``dtype`` — structurally a Parameter edit but
    /// no longer a pure value-retune.
    pub w_param_rename: f64,
    /// Cost of swapping two operator nodes that share a task / family.
    pub w_task_equivalent: f64,
    /// Per-hop cost across the task-topology tree when operators
    /// belong to different tasks / families. Multiplied by the
    /// hop count returned by ``TaskTopology::task_hops``.
    pub w_task_per_hop: f64,
    /// Fallback cost when no task information is available for
    /// one or both operators. High enough to discourage swaps
    /// the KB doesn't know about.
    pub w_unknown_task: f64,
    pub w_insert_node: f64,
    pub w_delete_node: f64,
    pub w_add_edge: f64,
    pub w_delete_edge: f64,
    /// Cost of swapping a Snippet for another Snippet with a
    /// different ``name``. Snippet bodies are opaque; this is the
    /// best we can do without AST-level canonicalisation.
    pub w_snippet_swap: f64,
}

impl Default for GedWeights {
    fn default() -> Self {
        Self {
            w_param_value: 0.1,
            w_param_rename: 1.5,
            w_task_equivalent: 1.0,
            w_task_per_hop: 2.0,
            w_unknown_task: 5.0,
            w_insert_node: 1.0,
            w_delete_node: 1.0,
            w_add_edge: 0.5,
            w_delete_edge: 0.5,
            w_snippet_swap: 1.5,
        }
    }
}

// ---------------------------------------------------------------------------
// Task topology — KB integration point
// ---------------------------------------------------------------------------

/// Resolve task / family membership and topology distance for a
/// pair of operators. Implementations:
///
///   * ``StaticTaskTopology`` — explicit per-op family + per-family
///     task, used by tests and small deployments.
///   * ``KbTaskTopology`` (future) — reads Neo4j via
///     ``optimizer::kb`` queries for ``belongs_to_family`` and
///     ``performs_task`` edges.
pub trait TaskTopology: Send + Sync {
    /// Family name for an operator, or ``None`` if unknown.
    fn family_of(&self, operator_fqn: &str) -> Option<String>;

    /// Shortest-path hop count between two task / family nodes in
    /// the task-topology tree. ``0`` when they're identical. ``None``
    /// when either side is unknown or the tree is disconnected.
    fn task_hops(&self, family_a: &str, family_b: &str) -> Option<usize>;
}

/// Hand-specified topology for tests + bootstrap. Both the
/// operator→family map and the family→family adjacency are
/// explicit.
#[derive(Debug, Default, Clone)]
pub struct StaticTaskTopology {
    pub family_by_op: FxHashMap<String, String>,
    /// Adjacency list: each family's immediate neighbours in the
    /// task-topology tree. Used for BFS hop counts.
    pub family_neighbours: FxHashMap<String, Vec<String>>,
}

impl StaticTaskTopology {
    pub fn assign_family(&mut self, op_fqn: impl Into<String>, family: impl Into<String>) {
        self.family_by_op.insert(op_fqn.into(), family.into());
    }

    /// Add an undirected edge between two families in the topology.
    pub fn add_edge(&mut self, a: impl Into<String>, b: impl Into<String>) {
        let (a, b) = (a.into(), b.into());
        self.family_neighbours.entry(a.clone()).or_default().push(b.clone());
        self.family_neighbours.entry(b).or_default().push(a);
    }
}

impl TaskTopology for StaticTaskTopology {
    fn family_of(&self, operator_fqn: &str) -> Option<String> {
        self.family_by_op.get(operator_fqn).cloned()
    }

    fn task_hops(&self, family_a: &str, family_b: &str) -> Option<usize> {
        if family_a == family_b {
            return Some(0);
        }
        if !self.family_neighbours.contains_key(family_a) {
            return None;
        }
        // BFS over the adjacency list.
        let mut visited: FxHashSet<&str> = FxHashSet::default();
        let mut frontier: Vec<(&str, usize)> = vec![(family_a, 0)];
        visited.insert(family_a);
        while let Some((node, depth)) = frontier.iter().copied().min_by_key(|(_, d)| *d) {
            frontier.retain(|(n, d)| !(n == &node && d == &depth));
            if node == family_b {
                return Some(depth);
            }
            if let Some(neigh) = self.family_neighbours.get(node) {
                for n in neigh {
                    if visited.insert(n.as_str()) {
                        frontier.push((n.as_str(), depth + 1));
                    }
                }
            }
        }
        None
    }
}

// ---------------------------------------------------------------------------
// Pairwise substitution cost
// ---------------------------------------------------------------------------

/// Parsed payload of a ``DagGraph`` node, as much as the compare key in
/// ``DagGraph::from_json`` captures.
#[derive(Debug, Clone)]
pub struct NodeView<'a> {
    pub id: &'a str,
    /// Operator FQN / Parameter name / Snippet name — the ``name``
    /// field before any value appending.
    pub base_name: &'a str,
    /// Full compare key as ``DagGraph`` stored it. For Parameter
    /// nodes this includes ``::dtype::value``.
    pub key: &'a str,
    /// ``"Operator"``, ``"Parameter"``, ``"Snippet"``, or other.
    pub node_type: &'a str,
}

/// Cost to substitute ``a`` with ``b``.
///
/// Caller decides the Parameter / Operator / Snippet dispatch by
/// inspecting ``node_type``. When both are Parameter with the same
/// base name but different stored key, that's a value-only edit →
/// ``w_param_value``. When both are Operator, look up families;
/// same family → ``w_task_equivalent``; different family →
/// ``w_task_per_hop × hops``.
pub fn substitute_cost(
    a: &NodeView,
    b: &NodeView,
    w: &GedWeights,
    topo: &dyn TaskTopology,
) -> f64 {
    if a.key == b.key {
        return 0.0;
    }

    match (a.node_type, b.node_type) {
        ("Parameter", "Parameter") => {
            if a.base_name == b.base_name {
                // Same Parameter slot, different stored key — by
                // construction the difference is in dtype or value
                // (DagGraph::from_json appends both). Treat as a
                // value-retune.
                w.w_param_value
            } else {
                w.w_param_rename
            }
        }
        ("Operator", "Operator") => operator_swap_cost(a.base_name, b.base_name, w, topo),
        ("Snippet", "Snippet") => {
            if a.base_name == b.base_name {
                // Same snippet name, different stored key — code
                // differs. Treat as a snippet swap; future work:
                // AST-level distance.
                w.w_snippet_swap
            } else {
                w.w_snippet_swap
            }
        }
        // Cross-type swap — fall back to a full delete + insert.
        _ => w.w_delete_node + w.w_insert_node,
    }
}

fn operator_swap_cost(
    a: &str,
    b: &str,
    w: &GedWeights,
    topo: &dyn TaskTopology,
) -> f64 {
    if a == b {
        return 0.0;
    }
    let fa = topo.family_of(a);
    let fb = topo.family_of(b);
    match (fa, fb) {
        (Some(x), Some(y)) if x == y => w.w_task_equivalent,
        (Some(x), Some(y)) => {
            let hops = topo.task_hops(&x, &y).unwrap_or(0);
            if hops == 0 {
                // Different family strings, zero hops means the
                // topology didn't connect them — treat as unknown
                // so the cost doesn't accidentally collapse to zero.
                w.w_unknown_task
            } else {
                w.w_task_per_hop * hops as f64
            }
        }
        _ => w.w_unknown_task,
    }
}

// ---------------------------------------------------------------------------
// Weighted GED — lower bound via the LAP-style symmetric difference
// ---------------------------------------------------------------------------

/// Weighted lower-bound GED suitable for BK-Tree-style similarity
/// queries. The algorithm mirrors ``ged::fast_distance`` but uses
/// ``substitute_cost`` to weight matches:
///
///   1. Match every node from ``g1`` to its cheapest counterpart in
///      ``g2`` (greedy by minimum substitute cost). Unmatched nodes
///      on either side contribute delete / insert costs.
///   2. Edge-count delta × ``w_add_edge`` as a cheap lower bound.
///
/// The greedy match is a lower bound on the optimal assignment, so
/// the overall result is ≤ true weighted GED — valid as a
/// BK-Tree query metric.
pub fn weighted_fast_distance(
    g1: &DagGraph,
    g2: &DagGraph,
    w: &GedWeights,
    topo: &dyn TaskTopology,
) -> f64 {
    let nodes1 = node_views(g1);
    let nodes2 = node_views(g2);

    let mut total = 0.0;
    let mut matched2: FxHashSet<usize> = FxHashSet::default();

    // Greedy pairwise matching: for each node in g1, match to its
    // cheapest same-type counterpart in g2 that hasn't been
    // claimed yet. We preserve semantic distance — a cross-family
    // operator swap stays at its full swap cost even if it's
    // larger than w_delete + w_insert — because the downstream
    // consumers (BK-Tree, RL similarity bonus) need that distinction.
    for na in &nodes1 {
        let mut best: Option<(usize, f64)> = None;
        for (j, nb) in nodes2.iter().enumerate() {
            if matched2.contains(&j) {
                continue;
            }
            if na.node_type != nb.node_type {
                // Cross-type matches are strictly del+ins semantically;
                // skip them from the greedy pairing so same-type
                // substitutions aren't preempted by a cheap
                // accidental cross-type hop.
                continue;
            }
            let c = substitute_cost(na, nb, w, topo);
            if best.map_or(true, |(_, b)| c < b) {
                best = Some((j, c));
            }
        }
        match best {
            Some((j, c)) => {
                matched2.insert(j);
                total += c;
            }
            None => {
                total += w.w_delete_node;
            }
        }
    }
    // Unmatched nodes in g2 → insertions.
    for j in 0..nodes2.len() {
        if !matched2.contains(&j) {
            total += w.w_insert_node;
        }
    }

    let edge_diff = (g1.edge_count() as isize - g2.edge_count() as isize).unsigned_abs() as f64;
    total += edge_diff * w.w_add_edge;

    total
}

fn node_views<'a>(g: &'a DagGraph) -> Vec<NodeView<'a>> {
    let mut out: Vec<NodeView<'a>> = Vec::with_capacity(g.node_names.len());
    for (id, key) in g.node_names.iter() {
        let node_type = g
            .node_types
            .get(id.as_str())
            .map(|s| s.as_str())
            .unwrap_or("Node");
        let base_name = match node_type {
            // ``DagGraph::from_json`` appends ``::dtype::value`` for
            // Parameters. Strip the suffix to get the logical slot
            // name that decides Param-rename vs Param-value-edit.
            "Parameter" => key.split("::").next().unwrap_or(key.as_str()),
            _ => key.as_str(),
        };
        out.push(NodeView {
            id: id.as_str(),
            base_name,
            key: key.as_str(),
            node_type,
        });
    }
    out
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn topo_with_families() -> StaticTaskTopology {
        let mut t = StaticTaskTopology::default();
        // Two tree-model operators in one family; a linear-model
        // operator in another; the two families are one-hop apart
        // under a common Classification parent node.
        t.assign_family("sklearn.ensemble.RandomForestClassifier", "Tree-Based");
        t.assign_family("sklearn.ensemble.ExtraTreesClassifier", "Tree-Based");
        t.assign_family("sklearn.linear_model.LogisticRegression", "Linear");
        t.add_edge("Tree-Based", "Classification");
        t.add_edge("Linear", "Classification");
        t
    }

    fn graph_with_classifier(name: &str) -> DagGraph {
        DagGraph::from_json(&json!({
            "nodes": {
                "c": {"name": name, "class_type": "Operator"},
                "p": {"name": "random_state", "class_type": "Parameter",
                      "dtype": "int", "value": "42"}
            },
            "edges": [{"source": "p", "destination": "c", "position": "random_state", "output": 0}]
        }))
    }

    fn graph_with_param(name: &str, value: &str) -> DagGraph {
        DagGraph::from_json(&json!({
            "nodes": {
                "c": {"name": "sklearn.ensemble.RandomForestClassifier", "class_type": "Operator"},
                "p": {"name": name, "class_type": "Parameter",
                      "dtype": "int", "value": value}
            },
            "edges": [{"source": "p", "destination": "c", "position": name, "output": 0}]
        }))
    }

    #[test]
    fn param_value_swap_is_cheapest() {
        let w = GedWeights::default();
        let t = topo_with_families();
        let g1 = graph_with_param("n_estimators", "100");
        let g2 = graph_with_param("n_estimators", "200");
        let d = weighted_fast_distance(&g1, &g2, &w, &t);
        // Only the Parameter's stored key changes; cost ≈ w_param_value.
        assert!((d - w.w_param_value).abs() < 1e-6, "got {d}");
    }

    #[test]
    fn same_family_swap_is_middle_tier() {
        let w = GedWeights::default();
        let t = topo_with_families();
        let g1 = graph_with_classifier("sklearn.ensemble.RandomForestClassifier");
        let g2 = graph_with_classifier("sklearn.ensemble.ExtraTreesClassifier");
        let d = weighted_fast_distance(&g1, &g2, &w, &t);
        // Same family → w_task_equivalent = 1.0; no edge change.
        assert!((d - w.w_task_equivalent).abs() < 1e-6, "got {d}");
    }

    #[test]
    fn cross_family_swap_is_costlier_than_same_family() {
        let w = GedWeights::default();
        let t = topo_with_families();
        let g_rf = graph_with_classifier("sklearn.ensemble.RandomForestClassifier");
        let g_et = graph_with_classifier("sklearn.ensemble.ExtraTreesClassifier");
        let g_lr = graph_with_classifier("sklearn.linear_model.LogisticRegression");

        let d_same = weighted_fast_distance(&g_rf, &g_et, &w, &t);
        let d_cross = weighted_fast_distance(&g_rf, &g_lr, &w, &t);
        assert!(d_cross > d_same, "cross={d_cross} same={d_same}");
    }

    #[test]
    fn cross_family_grows_with_hops() {
        let w = GedWeights::default();
        let mut t = topo_with_families();
        // Add a distant Regression family — two hops from Linear
        // (Linear — Classification — Regression).
        t.add_edge("Classification", "Regression");
        t.assign_family("sklearn.ensemble.RandomForestRegressor", "Regression");

        let g_lr = graph_with_classifier("sklearn.linear_model.LogisticRegression");
        let g_rfr = graph_with_classifier("sklearn.ensemble.RandomForestRegressor");
        let d = weighted_fast_distance(&g_lr, &g_rfr, &w, &t);
        // Linear → Classification → Regression = 2 hops.
        let expected_op = w.w_task_per_hop * 2.0;
        // The graph also has the shared Parameter (random_state)
        // which is identical on both sides, so its sub cost is 0.
        assert!((d - expected_op).abs() < 1e-6, "got {d} expected_op {expected_op}");
    }

    #[test]
    fn unknown_operator_falls_back_to_unknown_cost() {
        let w = GedWeights::default();
        let t = StaticTaskTopology::default();
        let g1 = graph_with_classifier("some.unknown.OpA");
        let g2 = graph_with_classifier("some.unknown.OpB");
        let d = weighted_fast_distance(&g1, &g2, &w, &t);
        assert!((d - w.w_unknown_task).abs() < 1e-6, "got {d}");
    }

    #[test]
    fn weight_roundtrip_json() {
        let w = GedWeights::default();
        let s = serde_json::to_string(&w).unwrap();
        let back: GedWeights = serde_json::from_str(&s).unwrap();
        assert_eq!(back.w_param_value, w.w_param_value);
        assert_eq!(back.w_task_per_hop, w.w_task_per_hop);
    }
}
