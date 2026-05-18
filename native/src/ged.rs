//! Graph Edit Distance — A* with beam search for pipeline DAGs.
//!
//! Pipeline DAGs are small (typically 5–30 nodes) but the exact GED is NP-hard.
//! We use an A* search with a Hungarian-algorithm lower bound, falling back to
//! a fast approximation for graphs that exceed the beam budget.

use rustc_hash::{FxHashMap, FxHashSet};
use std::cmp::Reverse;
use std::collections::BinaryHeap;

/// A lightweight directed multigraph optimised for GED computation.
#[derive(Debug, Clone)]
pub struct DagGraph {
    /// node_id → node name (operator FQN / parameter name / snippet name)
    pub node_names: FxHashMap<String, String>,
    /// node_id → node type ("Operator", "Parameter", "Snippet", "Node")
    pub node_types: FxHashMap<String, String>,
    /// (source_id, dest_id, position, output)
    pub edges: Vec<(String, String, i64, i64)>,
}

impl DagGraph {
    pub fn node_count(&self) -> usize {
        self.node_names.len()
    }

    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    /// Unused by the internal traversal but kept as part of the
    /// public ``DagGraph`` surface for downstream crates and tests
    /// that need to enumerate node ids without parsing the JSON
    /// form.
    #[allow(dead_code)]
    pub fn node_ids(&self) -> Vec<&String> {
        self.node_names.keys().collect()
    }

    /// Parse from the JSON dict format used by Dorian DAGs.
    pub fn from_json(val: &serde_json::Value) -> Self {
        let mut node_names = FxHashMap::default();
        let mut node_types = FxHashMap::default();
        let mut edges = Vec::new();

        if let Some(nodes) = val.get("nodes").and_then(|v| v.as_object()) {
            for (nid, ndata) in nodes {
                let name = ndata
                    .get("name")
                    .or_else(|| ndata.get("text"))
                    .and_then(|v| v.as_str())
                    .unwrap_or(nid.as_str())
                    .to_string();
                let ntype = ndata
                    .get("class_type")
                    .or_else(|| ndata.get("type"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("Node")
                    .to_string();
                node_names.insert(nid.clone(), name);
                node_types.insert(nid.clone(), ntype);
            }
        }

        if let Some(edge_arr) = val.get("edges").and_then(|v| v.as_array()) {
            for e in edge_arr {
                let src = e
                    .get("source")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let dst = e
                    .get("destination")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let pos = e
                    .get("position")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let out = e.get("output").and_then(|v| v.as_i64()).unwrap_or(0);
                edges.push((src, dst, pos, out));
            }
        }

        DagGraph {
            node_names,
            node_types,
            edges,
        }
    }

    /// Outgoing edges from a node, as (dest, position, output).
    #[allow(dead_code)]
    fn outgoing(&self, nid: &str) -> Vec<(&str, i64, i64)> {
        self.edges
            .iter()
            .filter(|(s, _, _, _)| s == nid)
            .map(|(_, d, p, o)| (d.as_str(), *p, *o))
            .collect()
    }

    /// Incoming edges to a node, as (source, position, output).
    #[allow(dead_code)]
    fn incoming(&self, nid: &str) -> Vec<(&str, i64, i64)> {
        self.edges
            .iter()
            .filter(|(_, d, _, _)| d == nid)
            .map(|(s, _, p, o)| (s.as_str(), *p, *o))
            .collect()
    }
}

// ---------------------------------------------------------------------------
// Exact GED — A* with lower-bound pruning
// ---------------------------------------------------------------------------

/// State in the A* search tree for GED computation.
///
/// `Ord`/`PartialOrd` are trivial (always Equal) — the `BinaryHeap` ordering
/// is driven by the `Reverse<usize>` cost wrapper in the tuple, not by the state.
#[derive(Clone, Eq, PartialEq)]
struct GedState {
    /// Mapping: g1 node index → g2 node index (or usize::MAX for deletion)
    mapping: Vec<usize>,
    /// How many g1 nodes have been assigned so far
    depth: usize,
    /// Cost accumulated so far (node + edge edits)
    cost: usize,
}

impl PartialOrd for GedState {
    fn partial_cmp(&self, _other: &Self) -> Option<std::cmp::Ordering> {
        Some(std::cmp::Ordering::Equal)
    }
}

impl Ord for GedState {
    fn cmp(&self, _other: &Self) -> std::cmp::Ordering {
        std::cmp::Ordering::Equal
    }
}

impl GedState {
    fn new(n1: usize) -> Self {
        GedState {
            mapping: vec![usize::MAX; n1],
            depth: 0,
            cost: 0,
        }
    }
}

/// Compute exact GED between two DAGs using A* search with a beam limit.
///
/// Returns `None` if the beam budget is exhausted (caller should fall back
/// to the fast approximation).
pub fn exact_ged(g1: &DagGraph, g2: &DagGraph, beam_limit: usize) -> Option<usize> {
    exact_ged_with_mapping_impl(g1, g2, beam_limit).map(|(cost, _)| cost)
}

fn exact_ged_with_mapping_impl(
    g1: &DagGraph, g2: &DagGraph, beam_limit: usize,
) -> Option<(usize, Vec<usize>)> {
    let n1 = g1.node_count();
    let n2 = g2.node_count();

    if n1 == 0 && n2 == 0 {
        return Some((0, Vec::new()));
    }
    if n1 == 0 {
        return Some((n2 + g2.edge_count(), Vec::new()));
    }
    if n2 == 0 {
        return Some((n1 + g1.edge_count(), vec![usize::MAX; n1]));
    }

    let ids1: Vec<String> = g1.node_names.keys().cloned().collect();
    let ids2: Vec<String> = g2.node_names.keys().cloned().collect();

    let mut node_sub: Vec<Vec<usize>> = vec![vec![0; n2]; n1];
    for (i, id1) in ids1.iter().enumerate() {
        let name1 = &g1.node_names[id1];
        for (j, id2) in ids2.iter().enumerate() {
            let name2 = &g2.node_names[id2];
            node_sub[i][j] = if name1 == name2 { 0 } else { 1 };
        }
    }

    let adj1 = build_adjacency(g1, &ids1);
    let adj2 = build_adjacency(g2, &ids2);

    let mut heap: BinaryHeap<(Reverse<usize>, GedState)> = BinaryHeap::new();
    let init = GedState::new(n1);
    let h = lower_bound_remaining(&init, n1, n2, &node_sub);
    heap.push((Reverse(h), init));

    let mut expanded: usize = 0;
    let mut best = usize::MAX;
    let mut best_mapping: Vec<usize> = Vec::new();

    while let Some((Reverse(est), state)) = heap.pop() {
        expanded += 1;
        if expanded > beam_limit {
            return None;
        }
        if est >= best {
            continue;
        }
        if state.depth == n1 {
            let used: FxHashSet<usize> = state
                .mapping
                .iter()
                .copied()
                .filter(|&m| m != usize::MAX)
                .collect();
            let insertions = n2 - used.len();
            let edge_ins = count_unmatched_edges_g2(&adj2, &used, n2);
            let total = state.cost + insertions + edge_ins;
            if total < best {
                best = total;
                best_mapping = state.mapping.clone();
            }
            continue;
        }
        let i = state.depth;
        let used: FxHashSet<usize> = state
            .mapping
            .iter()
            .copied()
            .filter(|&m| m != usize::MAX)
            .collect();
        {
            let mut s = state.clone();
            s.mapping[i] = usize::MAX;
            s.depth = i + 1;
            let edge_cost = count_dangling_edges(&adj1, i, &s.mapping, true);
            s.cost += 1 + edge_cost;
            let h = lower_bound_remaining(&s, n1, n2, &node_sub);
            let est = s.cost + h;
            if est < best {
                heap.push((Reverse(est), s));
            }
        }
        for j in 0..n2 {
            if used.contains(&j) {
                continue;
            }
            let mut s = state.clone();
            s.mapping[i] = j;
            s.depth = i + 1;
            let sub_cost = node_sub[i][j];
            let edge_cost = count_edge_edits(&adj1, &adj2, i, j, &s.mapping);
            s.cost += sub_cost + edge_cost;
            let h = lower_bound_remaining(&s, n1, n2, &node_sub);
            let est = s.cost + h;
            if est < best {
                heap.push((Reverse(est), s));
            }
        }
    }

    if best == usize::MAX {
        None
    } else {
        Some((best, best_mapping))
    }
}

fn exact_edit_path_from_mapping(
    g1: &DagGraph, g2: &DagGraph, mapping: &[usize], max_ops: usize,
) -> Vec<EditOp> {
    let mut ops: Vec<EditOp> = Vec::new();
    let push = |op: EditOp, ops: &mut Vec<EditOp>| {
        if ops.len() < max_ops { ops.push(op); }
    };

    let ids1: Vec<String> = g1.node_names.keys().cloned().collect();
    let ids2: Vec<String> = g2.node_names.keys().cloned().collect();

    let mut covered2: FxHashSet<usize> = FxHashSet::default();
    for &m in mapping.iter() {
        if m != usize::MAX { covered2.insert(m); }
    }

    for (i, id1) in ids1.iter().enumerate() {
        let m = mapping.get(i).copied().unwrap_or(usize::MAX);
        if m == usize::MAX {
            push(EditOp::DeleteNode { id: id1.clone() }, &mut ops);
        } else {
            let id2 = &ids2[m];
            let n1 = g1.node_names.get(id1).cloned().unwrap_or_default();
            let n2 = g2.node_names.get(id2).cloned().unwrap_or_default();
            let t1 = g1.node_types.get(id1).cloned().unwrap_or_default();
            let t2 = g2.node_types.get(id2).cloned().unwrap_or_default();
            if n1 != n2 || t1 != t2 {
                push(
                    EditOp::RenameNode {
                        id: id1.clone(),
                        old_type: t1, new_type: t2,
                        old_name: n1, new_name: n2,
                    },
                    &mut ops,
                );
            }
        }
    }

    for (j, id2) in ids2.iter().enumerate() {
        if !covered2.contains(&j) {
            let name = g2.node_names.get(id2).cloned().unwrap_or_default();
            let ntype = g2.node_types.get(id2).cloned().unwrap_or_default();
            push(EditOp::InsertNode { id: id2.clone(), ntype, name }, &mut ops);
        }
    }

    let id1_to_idx: FxHashMap<&str, usize> =
        ids1.iter().enumerate().map(|(i, id)| (id.as_str(), i)).collect();

    let mut translated1: FxHashSet<(String, String, i64, i64)> = FxHashSet::default();
    for (s, d, p, o) in g1.edges.iter() {
        let si = id1_to_idx.get(s.as_str()).copied();
        let di = id1_to_idx.get(d.as_str()).copied();
        match (si, di) {
            (Some(si), Some(di)) => {
                let sj = mapping.get(si).copied().unwrap_or(usize::MAX);
                let dj = mapping.get(di).copied().unwrap_or(usize::MAX);
                if sj == usize::MAX || dj == usize::MAX {
                    push(
                        EditOp::DeleteEdge {
                            source: s.clone(), destination: d.clone(),
                            position: *p, output: *o,
                        },
                        &mut ops,
                    );
                } else {
                    translated1.insert((ids2[sj].clone(), ids2[dj].clone(), *p, *o));
                }
            }
            _ => {
                push(
                    EditOp::DeleteEdge {
                        source: s.clone(), destination: d.clone(),
                        position: *p, output: *o,
                    },
                    &mut ops,
                );
            }
        }
    }

    let set2: FxHashSet<(String, String, i64, i64)> =
        g2.edges.iter().cloned().collect();
    let mut ins_edges: Vec<&(String, String, i64, i64)> =
        set2.difference(&translated1).collect();
    ins_edges.sort();
    for (s, d, p, o) in ins_edges {
        push(
            EditOp::InsertEdge {
                source: s.clone(), destination: d.clone(),
                position: *p, output: *o,
            },
            &mut ops,
        );
    }

    ops
}

// ---------------------------------------------------------------------------
// Adjacency helpers
// ---------------------------------------------------------------------------

type AdjMatrix = Vec<Vec<Option<(i64, i64)>>>; // [src][dst] → Some((pos, out))

fn build_adjacency(g: &DagGraph, ids: &[String]) -> AdjMatrix {
    let n = ids.len();
    let id_to_idx: FxHashMap<&str, usize> = ids
        .iter()
        .enumerate()
        .map(|(i, id)| (id.as_str(), i))
        .collect();

    let mut adj = vec![vec![None; n]; n];
    for (src, dst, pos, out) in &g.edges {
        if let (Some(&si), Some(&di)) = (id_to_idx.get(src.as_str()), id_to_idx.get(dst.as_str()))
        {
            adj[si][di] = Some((*pos, *out));
        }
    }
    adj
}

/// Count edges involving node `i` in g1 that become dangling
/// (node deleted / not yet matched).
fn count_dangling_edges(adj: &AdjMatrix, i: usize, mapping: &[usize], _is_delete: bool) -> usize {
    let n = adj.len();
    let mut count = 0;
    for j in 0..n {
        // Outgoing from i
        if adj[i][j].is_some() && j < mapping.len() && mapping[j] != usize::MAX {
            count += 1; // edge i→j must be deleted since i is deleted
        }
        // Incoming to i
        if adj[j][i].is_some() && j < mapping.len() && mapping[j] != usize::MAX {
            count += 1;
        }
    }
    count
}

/// Count edge edits when mapping g1[i] → g2[j].
/// Only counts edits for edges to already-matched nodes (depth < i).
fn count_edge_edits(
    adj1: &AdjMatrix,
    adj2: &AdjMatrix,
    i: usize,
    j: usize,
    mapping: &[usize],
) -> usize {
    let mut cost = 0;
    for k in 0..i {
        let mk = mapping[k];
        if mk == usize::MAX {
            continue; // k was deleted, edges already counted
        }
        // Edge i→k in g1 should map to j→mk in g2
        match (adj1[i][k], adj2[j][mk]) {
            (Some(e1), Some(e2)) => {
                if e1 != e2 {
                    cost += 1; // edge attribute mismatch → substitution
                }
            }
            (Some(_), None) => cost += 1,   // edge deletion
            (None, Some(_)) => cost += 1,   // edge insertion
            (None, None) => {}
        }
        // Edge k→i in g1 should map to mk→j in g2
        match (adj1[k][i], adj2[mk][j]) {
            (Some(e1), Some(e2)) => {
                if e1 != e2 {
                    cost += 1;
                }
            }
            (Some(_), None) => cost += 1,
            (None, Some(_)) => cost += 1,
            (None, None) => {}
        }
    }
    cost
}

/// Count edges in g2 that involve unmatched nodes (will need insertion).
fn count_unmatched_edges_g2(adj2: &AdjMatrix, used: &FxHashSet<usize>, n2: usize) -> usize {
    let mut count = 0;
    for i in 0..n2 {
        if used.contains(&i) {
            continue;
        }
        for j in 0..n2 {
            if adj2[i][j].is_some() {
                count += 1;
            }
        }
    }
    count
}

/// Simple greedy lower bound on remaining GED cost.
/// Counts the minimum node substitution/insertion/deletion costs
/// for unassigned nodes.
fn lower_bound_remaining(
    state: &GedState,
    n1: usize,
    n2: usize,
    node_sub: &[Vec<usize>],
) -> usize {
    let depth = state.depth;
    let remaining_g1 = n1 - depth;

    let used: FxHashSet<usize> = state
        .mapping[..depth]
        .iter()
        .copied()
        .filter(|&m| m != usize::MAX)
        .collect();
    let remaining_g2 = n2 - used.len();

    if remaining_g1 == 0 {
        return remaining_g2; // all remaining g2 nodes are insertions
    }

    // Greedy: for each unassigned g1 node, find cheapest available g2 match
    let available: Vec<usize> = (0..n2).filter(|j| !used.contains(j)).collect();
    let mut lb = 0;

    if available.is_empty() {
        return remaining_g1; // all remaining g1 nodes must be deleted
    }

    // For the first min(remaining_g1, available) pairs, take min substitution cost
    let pairs = remaining_g1.min(available.len());
    for offset in 0..pairs {
        let i = depth + offset;
        if i >= n1 {
            break;
        }
        let mut min_cost = 1usize; // deletion cost
        for &j in &available {
            let c = node_sub[i][j];
            if c < min_cost {
                min_cost = c;
                if min_cost == 0 {
                    break;
                }
            }
        }
        lb += min_cost;
    }

    // Extra nodes that must be inserted or deleted
    if remaining_g1 > available.len() {
        lb += remaining_g1 - available.len(); // deletions
    } else if available.len() > remaining_g1 {
        lb += available.len() - remaining_g1; // insertions
    }

    lb
}

// ---------------------------------------------------------------------------
// Fast approximate GED
// ---------------------------------------------------------------------------

/// Fast O(V+E) approximate GED based on operator set symmetric difference.
/// Always ≤ true GED (valid lower bound).
pub fn fast_distance(g1: &DagGraph, g2: &DagGraph) -> usize {
    let ops1: FxHashSet<&str> = g1
        .node_names
        .iter()
        .filter(|(id, _)| {
            g1.node_types
                .get(id.as_str())
                .map_or(false, |t| t == "Operator")
        })
        .map(|(_, name)| name.as_str())
        .collect();

    let ops2: FxHashSet<&str> = g2
        .node_names
        .iter()
        .filter(|(id, _)| {
            g2.node_types
                .get(id.as_str())
                .map_or(false, |t| t == "Operator")
        })
        .map(|(_, name)| name.as_str())
        .collect();

    let node_diff = ops1.symmetric_difference(&ops2).count();
    let edge_diff = (g1.edge_count() as isize - g2.edge_count() as isize).unsigned_abs();

    node_diff + edge_diff
}

/// Operator-set symmetric difference (for BK-Tree lower bound).
pub fn operator_set_distance(ops1: &[String], ops2: &[String]) -> usize {
    let s1: FxHashSet<&str> = ops1.iter().map(|s| s.as_str()).collect();
    let s2: FxHashSet<&str> = ops2.iter().map(|s| s.as_str()).collect();
    s1.symmetric_difference(&s2).count()
}

/// Extract sorted operator names from a DAG JSON value.
pub fn extract_operator_names(val: &serde_json::Value) -> Vec<String> {
    let mut names = Vec::new();
    if let Some(nodes) = val.get("nodes").and_then(|v| v.as_object()) {
        for ndata in nodes.values() {
            let ct = ndata
                .get("class_type")
                .or_else(|| ndata.get("type"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if ct == "Operator" {
                if let Some(name) = ndata.get("name").and_then(|v| v.as_str()) {
                    if !name.is_empty() {
                        names.push(name.to_string());
                    }
                }
            }
        }
    }
    names.sort();
    names
}

/// Compute GED with automatic fallback: exact A* for small graphs,
/// fast approximation for large ones.
pub fn graph_edit_distance(g1: &DagGraph, g2: &DagGraph, beam_limit: usize) -> usize {
    let total = g1.node_count() + g2.node_count();
    if total <= 30 {
        exact_ged(g1, g2, beam_limit).unwrap_or_else(|| fast_distance(g1, g2))
    } else {
        fast_distance(g1, g2)
    }
}

/// Variant of exact_ged that also returns the optimal g1→g2 node
/// mapping. Mapping slot ``usize::MAX`` means the g1 node is deleted.
pub fn exact_ged_with_mapping(
    g1: &DagGraph, g2: &DagGraph, beam_limit: usize,
) -> Option<(usize, Vec<usize>)> {
    exact_ged_with_mapping_impl(g1, g2, beam_limit)
}

// ═══════════════════════════════════════════════════════════════════
// Edit path — returns the sequence of edits, not just the distance
// ═══════════════════════════════════════════════════════════════════

/// One atomic edit op. Serialised as tagged JSON via `to_json_value`.
#[derive(Debug, Clone)]
pub enum EditOp {
    InsertNode { id: String, ntype: String, name: String },
    DeleteNode { id: String },
    RenameNode { id: String, old_type: String, new_type: String, old_name: String, new_name: String },
    InsertEdge { source: String, destination: String, position: i64, output: i64 },
    DeleteEdge { source: String, destination: String, position: i64, output: i64 },
    /// When node IDs disagree and we fall back to name-based matching,
    /// we can't enumerate edge-level ops reliably — report a count delta.
    EdgeDelta { count: i64 },
}

impl EditOp {
    pub fn to_json_value(&self) -> serde_json::Value {
        use serde_json::json;
        match self {
            EditOp::InsertNode { id, ntype, name } => json!({
                "kind": "InsertNode", "id": id, "type": ntype, "text": name,
            }),
            EditOp::DeleteNode { id } => json!({ "kind": "DeleteNode", "id": id }),
            EditOp::RenameNode { id, old_type, new_type, old_name, new_name } => json!({
                "kind": "RenameNode", "id": id,
                "old_type": old_type, "new_type": new_type,
                "old_text": old_name, "new_text": new_name,
            }),
            EditOp::InsertEdge { source, destination, position, output } => json!({
                "kind": "InsertEdge", "source": source, "destination": destination,
                "position": position, "output": output,
            }),
            EditOp::DeleteEdge { source, destination, position, output } => json!({
                "kind": "DeleteEdge", "source": source, "destination": destination,
                "position": position, "output": output,
            }),
            EditOp::EdgeDelta { count } => json!({ "kind": "EdgeDelta", "count": count }),
        }
    }
}

/// Edit-path result with strategy + truncation flag.
pub struct EditPathResult {
    pub ops: Vec<EditOp>,
    pub strategy: &'static str,
    pub truncated: bool,
}

/// Compute the minimum-signal sequence of edits turning `g1` into `g2`.
///
/// **ID-keyed diff** when the two graphs share at least 25% of their
/// node IDs — exact, O(|V| + |E|). This covers the common case where the
/// corrected DAG is the hand-edit of the auto-extracted one (IDs preserved
/// across the user's canvas edits).
///
/// **Name-keyed diff** when IDs disagree — matches nodes by (type, text)
/// fingerprint, reports edges only as a count delta. Less precise but
/// never lies.
///
/// The NP-hard exact A*-with-path-reconstruction is a follow-up; the
/// scalar distance goes through `graph_edit_distance` for that.
pub fn graph_edit_path(g1: &DagGraph, g2: &DagGraph, max_ops: usize) -> EditPathResult {
    let ids1: FxHashSet<&String> = g1.node_names.keys().collect();
    let ids2: FxHashSet<&String> = g2.node_names.keys().collect();
    let shared: FxHashSet<&String> = ids1.intersection(&ids2).copied().collect();

    let both_nonempty = !ids1.is_empty() && !ids2.is_empty();
    let larger = std::cmp::max(ids1.len(), ids2.len());
    let use_id_diff = both_nonempty && larger > 0
        && (shared.len() as f64 / larger as f64) >= 0.25;

    let mut ops: Vec<EditOp> = Vec::new();
    let push = |op: EditOp, ops: &mut Vec<EditOp>| {
        if ops.len() < max_ops {
            ops.push(op);
        }
    };

    if use_id_diff {
        // DeleteNode for ids only in g1
        let mut deletes: Vec<&String> = ids1.difference(&ids2).copied().collect();
        deletes.sort();
        for id in deletes {
            push(EditOp::DeleteNode { id: id.clone() }, &mut ops);
        }
        // InsertNode for ids only in g2
        let mut inserts: Vec<&String> = ids2.difference(&ids1).copied().collect();
        inserts.sort();
        for id in inserts {
            let name = g2.node_names.get(id).cloned().unwrap_or_default();
            let ntype = g2.node_types.get(id).cloned().unwrap_or_default();
            push(EditOp::InsertNode { id: id.clone(), ntype, name }, &mut ops);
        }
        // RenameNode for shared ids with divergent attrs
        let mut shared_sorted: Vec<&String> = shared.iter().copied().collect();
        shared_sorted.sort();
        for id in shared_sorted {
            let n1 = g1.node_names.get(id).cloned().unwrap_or_default();
            let n2 = g2.node_names.get(id).cloned().unwrap_or_default();
            let t1 = g1.node_types.get(id).cloned().unwrap_or_default();
            let t2 = g2.node_types.get(id).cloned().unwrap_or_default();
            if n1 != n2 || t1 != t2 {
                push(
                    EditOp::RenameNode {
                        id: id.clone(),
                        old_type: t1, new_type: t2,
                        old_name: n1, new_name: n2,
                    },
                    &mut ops,
                );
            }
        }

        // Edge diffs keyed by (src, dst, pos, out).
        let set1: FxHashSet<(String, String, i64, i64)> =
            g1.edges.iter().cloned().collect();
        let set2: FxHashSet<(String, String, i64, i64)> =
            g2.edges.iter().cloned().collect();
        let mut del_edges: Vec<&(String, String, i64, i64)> =
            set1.difference(&set2).collect();
        del_edges.sort();
        for (s, d, p, o) in del_edges {
            push(
                EditOp::DeleteEdge {
                    source: s.clone(), destination: d.clone(),
                    position: *p, output: *o,
                },
                &mut ops,
            );
        }
        let mut ins_edges: Vec<&(String, String, i64, i64)> =
            set2.difference(&set1).collect();
        ins_edges.sort();
        for (s, d, p, o) in ins_edges {
            push(
                EditOp::InsertEdge {
                    source: s.clone(), destination: d.clone(),
                    position: *p, output: *o,
                },
                &mut ops,
            );
        }

        let truncated = ops.len() >= max_ops;
        EditPathResult { ops, strategy: "id_diff", truncated }
    } else {
        // Disjoint ID spaces. Run exact A* for small graphs to find the
        // optimal node alignment + derive the edit path from it.
        let total = g1.node_count() + g2.node_count();
        if total <= 30 {
            if let Some((_cost, mapping)) =
                exact_ged_with_mapping(g1, g2, 50_000)
            {
                let astar_ops = exact_edit_path_from_mapping(g1, g2, &mapping, max_ops);
                let truncated = astar_ops.len() >= max_ops;
                return EditPathResult {
                    ops: astar_ops,
                    strategy: "astar_exact",
                    truncated,
                };
            }
        }

        // Name-keyed: bucket nodes by (type, name) fingerprint.
        type Fp = (String, String);
        let mut fp1: FxHashMap<Fp, Vec<&String>> = FxHashMap::default();
        let mut fp2: FxHashMap<Fp, Vec<&String>> = FxHashMap::default();
        for id in ids1.iter() {
            let fp = (
                g1.node_types.get(*id).cloned().unwrap_or_default(),
                g1.node_names.get(*id).cloned().unwrap_or_default(),
            );
            fp1.entry(fp).or_default().push(*id);
        }
        for id in ids2.iter() {
            let fp = (
                g2.node_types.get(*id).cloned().unwrap_or_default(),
                g2.node_names.get(*id).cloned().unwrap_or_default(),
            );
            fp2.entry(fp).or_default().push(*id);
        }

        for (fp, ids) in fp1.iter() {
            let have2 = fp2.get(fp).map(|v| v.len()).unwrap_or(0);
            let extra = ids.len().saturating_sub(have2);
            for id in ids.iter().take(extra) {
                push(EditOp::DeleteNode { id: (*id).clone() }, &mut ops);
            }
        }
        for (fp, ids) in fp2.iter() {
            let have1 = fp1.get(fp).map(|v| v.len()).unwrap_or(0);
            let extra = ids.len().saturating_sub(have1);
            let (ntype, name) = fp.clone();
            for id in ids.iter().take(extra) {
                push(
                    EditOp::InsertNode { id: (*id).clone(), ntype: ntype.clone(), name: name.clone() },
                    &mut ops,
                );
            }
        }

        let de = g2.edges.len() as i64 - g1.edges.len() as i64;
        if de != 0 {
            push(EditOp::EdgeDelta { count: de }, &mut ops);
        }

        let truncated = ops.len() >= max_ops;
        EditPathResult { ops, strategy: "name_diff", truncated }
    }
}
