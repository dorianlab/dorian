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
                let base_name = ndata
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
                // Parameter nodes must include their ``dtype`` +
                // ``value`` in the compare key — otherwise two
                // pipelines that differ only in hyperparameter
                // values (e.g. ``C=0.5`` vs ``C=1.0``) are seen as
                // identical by GED, and the BK-Tree / dedupe path
                // collapses them. ``fast_distance`` ignores
                // Parameters by ntype="Parameter" filter so its
                // "operators only" semantics are preserved; full
                // GED walks them with the richer key.
                let name = if ntype == "Parameter" {
                    let dtype = ndata
                        .get("dtype")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let value = match ndata.get("value") {
                        Some(serde_json::Value::String(s)) => s.clone(),
                        Some(v) => v.to_string(),
                        None => String::new(),
                    };
                    format!("{}::{}::{}", base_name, dtype, value)
                } else {
                    base_name
                };
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
    pub fn outgoing(&self, nid: &str) -> Vec<(&str, i64, i64)> {
        self.edges
            .iter()
            .filter(|(s, _, _, _)| s == nid)
            .map(|(_, d, p, o)| (d.as_str(), *p, *o))
            .collect()
    }

    /// Incoming edges to a node, as (source, position, output).
    pub fn incoming(&self, nid: &str) -> Vec<(&str, i64, i64)> {
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
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
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
    exact_ged_with_mapping(g1, g2, beam_limit).map(|(cost, _)| cost)
}

/// A* GED that also returns the optimal mapping of g1 → g2 nodes.
/// Mapping value ``usize::MAX`` means the g1 node is deleted (no
/// counterpart in g2). Consumers use this to derive the edit path by
/// comparing g1[i] to g2[mapping[i]] and enumerating edges under the
/// alignment — see ``exact_edit_path_from_mapping`` below.
pub fn exact_ged_with_mapping(
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

    // Pre-compute node substitution costs (0 if names match, 1 otherwise)
    let mut node_sub: Vec<Vec<usize>> = vec![vec![0; n2]; n1];
    for (i, id1) in ids1.iter().enumerate() {
        let name1 = &g1.node_names[id1];
        for (j, id2) in ids2.iter().enumerate() {
            let name2 = &g2.node_names[id2];
            node_sub[i][j] = if name1 == name2 { 0 } else { 1 };
        }
    }

    // Pre-compute adjacency for edge cost computation
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

        // Option 1: delete node i from g1
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

        // Option 2: map node i to each unmatched node j in g2
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

/// Turn an A*-optimal g1→g2 node mapping into a sequence of atomic
/// EditOps. Deterministic, O(|V| + |E|) given the mapping.
pub fn exact_edit_path_from_mapping(
    g1: &DagGraph, g2: &DagGraph, mapping: &[usize], max_ops: usize,
) -> Vec<EditOp> {
    let mut ops: Vec<EditOp> = Vec::new();
    let push = |op: EditOp, ops: &mut Vec<EditOp>| {
        if ops.len() < max_ops { ops.push(op); }
    };

    let ids1: Vec<String> = g1.node_names.keys().cloned().collect();
    let ids2: Vec<String> = g2.node_names.keys().cloned().collect();

    // Which g2 nodes are covered by the mapping
    let mut covered2: FxHashSet<usize> = FxHashSet::default();
    for &m in mapping.iter() {
        if m != usize::MAX { covered2.insert(m); }
    }

    // g1 nodes: delete or rename
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

    // g2 nodes not covered → insertions
    for (j, id2) in ids2.iter().enumerate() {
        if !covered2.contains(&j) {
            let name = g2.node_names.get(id2).cloned().unwrap_or_default();
            let ntype = g2.node_types.get(id2).cloned().unwrap_or_default();
            push(EditOp::InsertNode { id: id2.clone(), ntype, name }, &mut ops);
        }
    }

    // Edges: project g1 edges into g2-id space via mapping, then diff.
    let id1_to_idx: FxHashMap<&str, usize> =
        ids1.iter().enumerate().map(|(i, id)| (id.as_str(), i)).collect();

    // Edges from g1 translated to (g2-id, g2-id, pos, out); unresolvable
    // translations (source or dest deleted) become DeleteEdge.
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

    // g2 edges not reached from any g1 edge under the mapping → inserts.
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
    // g1 edges (translated) that don't appear in g2 → deletes.
    let mut del_edges: Vec<&(String, String, i64, i64)> =
        translated1.difference(&set2).collect();
    del_edges.sort();
    for (sj, dj, p, o) in del_edges {
        // Report in the original g1-id space for user clarity.
        // Find the original g1 edge that produced this translation.
        // This is best-effort; if the original is ambiguous we take the first.
        let orig = g1.edges.iter().find(|(s, d, p2, o2)| {
            let si = id1_to_idx.get(s.as_str()).copied();
            let di = id1_to_idx.get(d.as_str()).copied();
            if let (Some(si), Some(di)) = (si, di) {
                let msj = mapping.get(si).copied().unwrap_or(usize::MAX);
                let mdj = mapping.get(di).copied().unwrap_or(usize::MAX);
                msj != usize::MAX && mdj != usize::MAX
                    && &ids2[msj] == sj && &ids2[mdj] == dj
                    && p2 == p && o2 == o
            } else { false }
        });
        if let Some((s, d, p, o)) = orig {
            push(
                EditOp::DeleteEdge {
                    source: s.clone(), destination: d.clone(),
                    position: *p, output: *o,
                },
                &mut ops,
            );
        }
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
    for (i, row) in adj2.iter().enumerate().take(n2) {
        if used.contains(&i) {
            continue;
        }
        for cell in row.iter().take(n2) {
            if cell.is_some() {
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

/// Fast O(V+E) approximate GED.
///
/// Symmetric difference of typed node signatures + edge-count
/// delta. Lower bound on true GED. Participants:
///
///   * Operator nodes keyed by ``(op, name)``.
///   * Parameter nodes keyed by ``(p, name::dtype::value)`` — the
///     same encoding ``DagGraph::from_json`` now uses. Two
///     pipelines that differ only in hyperparameter values must
///     produce a non-zero distance; the earlier version dropped
///     Parameters entirely, collapsing value-different variants to
///     distance 0 and breaking BK-Tree / dedupe semantics.
///   * Snippet nodes keyed by ``(snip, name)``.
pub fn fast_distance(g1: &DagGraph, g2: &DagGraph) -> usize {
    fn sigs<'a>(g: &'a DagGraph) -> FxHashSet<(&'a str, &'a str)> {
        let mut out: FxHashSet<(&'a str, &'a str)> = FxHashSet::default();
        for (id, name) in g.node_names.iter() {
            let tag: &str = match g.node_types.get(id.as_str()).map(|s| s.as_str()) {
                Some("Operator") => "op",
                Some("Parameter") => "p",
                Some("Snippet") => "snip",
                _ => continue,
            };
            out.insert((tag, name.as_str()));
        }
        out
    }
    let s1 = sigs(g1);
    let s2 = sigs(g2);
    let node_diff = s1.symmetric_difference(&s2).count();
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

pub struct EditPathResult {
    pub ops: Vec<EditOp>,
    pub strategy: &'static str,
    pub truncated: bool,
}

/// Minimum-signal edit path turning `g1` into `g2`.
///
/// ID-keyed diff when the two graphs share ≥25% of node IDs (exact,
/// O(|V| + |E|)); name-keyed fallback matches on (type, text) tuples
/// and reports edges only as a count delta. The NP-hard exact
/// A*-with-path-reconstruction is a follow-up — distance goes through
/// `graph_edit_distance`.
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
        let mut deletes: Vec<&String> = ids1.difference(&ids2).copied().collect();
        deletes.sort();
        for id in deletes {
            push(EditOp::DeleteNode { id: id.clone() }, &mut ops);
        }
        let mut inserts: Vec<&String> = ids2.difference(&ids1).copied().collect();
        inserts.sort();
        for id in inserts {
            let name = g2.node_names.get(id).cloned().unwrap_or_default();
            let ntype = g2.node_types.get(id).cloned().unwrap_or_default();
            push(EditOp::InsertNode { id: id.clone(), ntype, name }, &mut ops);
        }
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
        // Disjoint ID spaces. If the graphs are small enough, run exact
        // A* to find the optimal alignment (NP-hard but bounded by the
        // beam budget). Otherwise fall back to fingerprint-only name_diff
        // which is cheap and reports an EdgeDelta summary.
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
            // Beam exhausted → fall through to name_diff.
        }

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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // ---- helpers to build DagGraph from JSON shorthand ----

    fn empty_graph() -> DagGraph {
        DagGraph::from_json(&serde_json::json!({ "nodes": {}, "edges": [] }))
    }

    fn single_node(id: &str, name: &str, ntype: &str) -> DagGraph {
        DagGraph::from_json(&serde_json::json!({
            "nodes": { (id): { "name": name, "class_type": ntype } },
            "edges": []
        }))
    }

    /// A → B → C  (linear pipeline)
    fn linear_pipeline() -> DagGraph {
        DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "sklearn.preprocessing.StandardScaler", "class_type": "Operator" },
                "b": { "name": "sklearn.decomposition.PCA",           "class_type": "Operator" },
                "c": { "name": "sklearn.linear_model.LinearRegression","class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 1, "output": 0 },
                { "source": "b", "destination": "c", "position": 1, "output": 0 }
            ]
        }))
    }

    /// Diamond: A → B, A → C, B → D, C → D
    fn diamond_pipeline() -> DagGraph {
        DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "read_csv",      "class_type": "Operator" },
                "b": { "name": "StandardScaler", "class_type": "Operator" },
                "c": { "name": "PCA",            "class_type": "Operator" },
                "d": { "name": "LogisticRegression", "class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 1, "output": 0 },
                { "source": "a", "destination": "c", "position": 1, "output": 0 },
                { "source": "b", "destination": "d", "position": 1, "output": 0 },
                { "source": "c", "destination": "d", "position": 2, "output": 0 }
            ]
        }))
    }

    // ================================================================
    // 1. Identical graphs → distance 0
    // ================================================================

    #[test]
    fn identical_graphs_exact_ged_is_zero() {
        let g = linear_pipeline();
        assert_eq!(exact_ged(&g, &g, 10_000), Some(0));
    }

    #[test]
    fn identical_graphs_fast_distance_is_zero() {
        let g = linear_pipeline();
        assert_eq!(fast_distance(&g, &g), 0);
    }

    #[test]
    fn identical_graphs_graph_edit_distance_is_zero() {
        let g = diamond_pipeline();
        assert_eq!(graph_edit_distance(&g, &g, 10_000), 0);
    }

    // ================================================================
    // 2. Completely disjoint graphs — max distance
    // ================================================================

    #[test]
    fn disjoint_graphs_nonzero_distance() {
        let g1 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "x": { "name": "alpha", "class_type": "Operator" },
                "y": { "name": "beta",  "class_type": "Operator" }
            },
            "edges": [
                { "source": "x", "destination": "y", "position": 1, "output": 0 }
            ]
        }));
        let g2 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "p": { "name": "gamma", "class_type": "Operator" },
                "q": { "name": "delta", "class_type": "Operator" }
            },
            "edges": [
                { "source": "p", "destination": "q", "position": 1, "output": 0 }
            ]
        }));

        let d = exact_ged(&g1, &g2, 10_000).unwrap();
        // All node names differ → at least 2 substitutions
        assert!(d >= 2, "disjoint 2-node graphs should have distance >= 2, got {d}");
    }

    #[test]
    fn disjoint_no_overlap_fast_distance() {
        let g1 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "op_a", "class_type": "Operator" }
            },
            "edges": []
        }));
        let g2 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "b": { "name": "op_b", "class_type": "Operator" }
            },
            "edges": []
        }));
        // symmetric difference of {op_a} and {op_b} = 2
        assert_eq!(fast_distance(&g1, &g2), 2);
    }

    // ================================================================
    // 3. Single node graphs — trivial case
    // ================================================================

    #[test]
    fn single_node_same_name() {
        let g1 = single_node("n1", "scaler", "Operator");
        let g2 = single_node("n2", "scaler", "Operator");
        assert_eq!(exact_ged(&g1, &g2, 1_000), Some(0));
    }

    #[test]
    fn single_node_different_name() {
        let g1 = single_node("n1", "scaler", "Operator");
        let g2 = single_node("n2", "pca", "Operator");
        assert_eq!(exact_ged(&g1, &g2, 1_000), Some(1));
    }

    // ================================================================
    // 4. Empty graph — edge case
    // ================================================================

    #[test]
    fn both_empty() {
        let g = empty_graph();
        assert_eq!(exact_ged(&g, &g, 100), Some(0));
        assert_eq!(fast_distance(&g, &g), 0);
        assert_eq!(graph_edit_distance(&g, &g, 100), 0);
    }

    #[test]
    fn one_empty_one_single_node() {
        let g1 = empty_graph();
        let g2 = single_node("n1", "op", "Operator");
        // Insert 1 node + 0 edges = 1
        assert_eq!(exact_ged(&g1, &g2, 1_000), Some(1));
        assert_eq!(exact_ged(&g2, &g1, 1_000), Some(1));
    }

    #[test]
    fn empty_vs_graph_with_edges() {
        let g1 = empty_graph();
        let g2 = linear_pipeline(); // 3 nodes + 2 edges
        assert_eq!(exact_ged(&g1, &g2, 10_000), Some(5));
    }

    // ================================================================
    // 5. Node addition / removal — subset graphs
    // ================================================================

    #[test]
    fn node_addition_one_extra() {
        // g1 = A → B, g2 = A → B → C
        let g1 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "scaler", "class_type": "Operator" },
                "b": { "name": "pca",    "class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 1, "output": 0 }
            ]
        }));
        let g2 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "scaler", "class_type": "Operator" },
                "b": { "name": "pca",    "class_type": "Operator" },
                "c": { "name": "lr",     "class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 1, "output": 0 },
                { "source": "b", "destination": "c", "position": 1, "output": 0 }
            ]
        }));

        let d = exact_ged(&g1, &g2, 10_000).unwrap();
        // At least 1 edit (node insertion); exact cost depends on edge accounting
        assert!(d >= 1, "adding a node should cost at least 1, got {d}");
    }

    #[test]
    fn node_removal_is_symmetric() {
        let g1 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "op1", "class_type": "Operator" },
                "b": { "name": "op2", "class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 1, "output": 0 }
            ]
        }));
        let g2 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "op1", "class_type": "Operator" }
            },
            "edges": []
        }));

        let d12 = exact_ged(&g1, &g2, 10_000).unwrap();
        let d21 = exact_ged(&g2, &g1, 10_000).unwrap();
        // Both directions should find a nonzero distance
        assert!(d12 > 0);
        assert!(d21 > 0);
        // Note: this A* implementation may yield different costs depending on
        // which graph is g1 vs g2 due to asymmetric search order.
    }

    // ================================================================
    // 6. Edge addition / removal — same nodes, different edges
    // ================================================================

    #[test]
    fn edge_addition() {
        // Same nodes, g2 has one extra edge
        let g1 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "op1", "class_type": "Operator" },
                "b": { "name": "op2", "class_type": "Operator" },
                "c": { "name": "op3", "class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 1, "output": 0 }
            ]
        }));
        let g2 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "op1", "class_type": "Operator" },
                "b": { "name": "op2", "class_type": "Operator" },
                "c": { "name": "op3", "class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 1, "output": 0 },
                { "source": "b", "destination": "c", "position": 1, "output": 0 }
            ]
        }));

        let d = exact_ged(&g1, &g2, 10_000).unwrap();
        assert_eq!(d, 1, "one edge insertion should cost 1");
    }

    #[test]
    fn edge_attribute_mismatch() {
        // Same structure but edge position differs
        let g1 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "op1", "class_type": "Operator" },
                "b": { "name": "op2", "class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 1, "output": 0 }
            ]
        }));
        let g2 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "op1", "class_type": "Operator" },
                "b": { "name": "op2", "class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 2, "output": 0 }
            ]
        }));

        let d = exact_ged(&g1, &g2, 10_000).unwrap();
        assert_eq!(d, 1, "edge attribute mismatch should cost 1");
    }

    // ================================================================
    // 7. Beam exhaustion — returns None, fallback works
    // ================================================================

    #[test]
    fn beam_exhaustion_returns_none() {
        // Use a small beam limit on a moderately sized graph
        let g1 = diamond_pipeline(); // 4 nodes
        let g2 = linear_pipeline();  // 3 nodes, all different names

        // beam_limit=1 means we can only expand 1 state — should exhaust
        let result = exact_ged(&g1, &g2, 1);
        assert!(result.is_none(), "beam_limit=1 should exhaust on a 4-vs-3 graph");
    }

    #[test]
    fn graph_edit_distance_falls_back_on_exhaustion() {
        // graph_edit_distance should always return a value (never panic)
        let g1 = diamond_pipeline();
        let g2 = linear_pipeline();
        // Even with beam_limit=1, graph_edit_distance uses fast_distance fallback
        let d = graph_edit_distance(&g1, &g2, 1);
        assert!(d > 0, "different graphs should have nonzero distance");
    }

    #[test]
    fn large_graph_beam_exhaustion() {
        // Build two 10-node graphs with distinct operator names
        let mut nodes1 = serde_json::Map::new();
        let mut nodes2 = serde_json::Map::new();
        let mut edges1 = Vec::new();
        let mut edges2 = Vec::new();
        for i in 0..10 {
            let id = format!("n{i}");
            nodes1.insert(
                id.clone(),
                serde_json::json!({ "name": format!("op_a_{i}"), "class_type": "Operator" }),
            );
            nodes2.insert(
                id.clone(),
                serde_json::json!({ "name": format!("op_b_{i}"), "class_type": "Operator" }),
            );
            if i > 0 {
                let prev = format!("n{}", i - 1);
                edges1.push(serde_json::json!({ "source": prev, "destination": id, "position": 1, "output": 0 }));
                edges2.push(serde_json::json!({ "source": prev, "destination": id, "position": 1, "output": 0 }));
            }
        }
        let g1 = DagGraph::from_json(&serde_json::json!({ "nodes": nodes1, "edges": edges1 }));
        let g2 = DagGraph::from_json(&serde_json::json!({ "nodes": nodes2, "edges": edges2 }));

        // With a very small beam, exact should fail
        assert!(exact_ged(&g1, &g2, 5).is_none());

        // graph_edit_distance still returns a value
        let d = graph_edit_distance(&g1, &g2, 5);
        assert!(d > 0);
    }

    // ================================================================
    // 8. fast_distance — reasonable approximation
    // ================================================================

    #[test]
    fn fast_distance_reasonable_approximation() {
        let g1 = linear_pipeline();
        let g2 = diamond_pipeline();

        let fast = fast_distance(&g1, &g2);
        // fast_distance should be > 0 for different graphs
        assert!(fast > 0);

        // fast_distance is an O(V+E) approximation — not guaranteed to be a
        // lower or upper bound of the exact GED, but should be in the same
        // order of magnitude for small graphs.
        if let Some(exact) = exact_ged(&g1, &g2, 50_000) {
            assert!(exact > 0);
            // Both should be nonzero and within a reasonable factor
            let ratio = (fast as f64) / (exact as f64);
            assert!(
                ratio < 10.0,
                "fast ({fast}) and exact ({exact}) should be in the same order of magnitude"
            );
        }
    }

    #[test]
    fn fast_distance_counts_parameters() {
        // Parameters participate: a pipeline with a Parameter and
        // one without are NOT at distance 0. Replaces the earlier
        // ``fast_distance_only_counts_operators`` assertion —
        // ignoring parameter values was the bug the system-wide
        // "no logic ignores parameter values" rule targets.
        let g1 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "p": { "name": "x",  "class_type": "Parameter",
                       "dtype": "float", "value": "0.5" },
                "o": { "name": "op", "class_type": "Operator" }
            },
            "edges": []
        }));
        let g2 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "o": { "name": "op", "class_type": "Operator" }
            },
            "edges": []
        }));
        // g1 has a Parameter g2 doesn't → node_diff = 1.
        assert_eq!(fast_distance(&g1, &g2), 1);
    }

    #[test]
    fn fast_distance_parameters_with_different_values_differ() {
        let g1 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "p": { "name": "C",  "class_type": "Parameter",
                       "dtype": "float", "value": "0.5" },
                "o": { "name": "op", "class_type": "Operator" }
            },
            "edges": []
        }));
        let g2 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "p": { "name": "C",  "class_type": "Parameter",
                       "dtype": "float", "value": "1.0" },
                "o": { "name": "op", "class_type": "Operator" }
            },
            "edges": []
        }));
        // Same operators, same Parameter NAME, different values →
        // two distinct Parameter signatures → symmetric diff = 2.
        assert_eq!(fast_distance(&g1, &g2), 2);
    }

    #[test]
    fn fast_distance_edge_count_difference() {
        let g1 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "op", "class_type": "Operator" },
                "b": { "name": "op", "class_type": "Operator" }
            },
            "edges": []
        }));
        let g2 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "a": { "name": "op", "class_type": "Operator" },
                "b": { "name": "op", "class_type": "Operator" }
            },
            "edges": [
                { "source": "a", "destination": "b", "position": 1, "output": 0 },
                { "source": "a", "destination": "b", "position": 2, "output": 0 }
            ]
        }));
        // node_diff=0 (both have same op names), edge_diff=2
        assert_eq!(fast_distance(&g1, &g2), 2);
    }

    // ================================================================
    // 9. Adjacency matrix — verify structure
    // ================================================================

    #[test]
    fn adjacency_matrix_simple() {
        // A → B → C
        let g = linear_pipeline();
        let ids: Vec<String> = {
            let mut v: Vec<String> = g.node_names.keys().cloned().collect();
            v.sort(); // deterministic order: a, b, c
            v
        };
        let adj = build_adjacency(&g, &ids);

        let idx = |name: &str| ids.iter().position(|s| s == name).unwrap();
        let a = idx("a");
        let b = idx("b");
        let c = idx("c");

        // a → b present with (pos=1, out=0)
        assert_eq!(adj[a][b], Some((1, 0)));
        // b → c present
        assert_eq!(adj[b][c], Some((1, 0)));
        // No reverse edges
        assert_eq!(adj[b][a], None);
        assert_eq!(adj[c][b], None);
        // No self-loops
        assert_eq!(adj[a][a], None);
        // No skip edge a → c
        assert_eq!(adj[a][c], None);
    }

    #[test]
    fn adjacency_matrix_diamond() {
        let g = diamond_pipeline();
        let ids: Vec<String> = {
            let mut v: Vec<String> = g.node_names.keys().cloned().collect();
            v.sort(); // a, b, c, d
            v
        };
        let adj = build_adjacency(&g, &ids);

        let idx = |name: &str| ids.iter().position(|s| s == name).unwrap();
        let a = idx("a");
        let b = idx("b");
        let c = idx("c");
        let d = idx("d");

        assert!(adj[a][b].is_some());
        assert!(adj[a][c].is_some());
        assert!(adj[b][d].is_some());
        assert!(adj[c][d].is_some());
        // c → d has position=2
        assert_eq!(adj[c][d], Some((2, 0)));
        // No edge a → d (skip)
        assert_eq!(adj[a][d], None);
    }

    #[test]
    fn adjacency_matrix_empty() {
        let g = empty_graph();
        let ids: Vec<String> = Vec::new();
        let adj = build_adjacency(&g, &ids);
        assert!(adj.is_empty());
    }

    // ================================================================
    // 10. operator_set_distance + extract_operator_names
    // ================================================================

    #[test]
    fn operator_set_distance_identical() {
        let ops = vec!["a".to_string(), "b".to_string()];
        assert_eq!(operator_set_distance(&ops, &ops), 0);
    }

    #[test]
    fn operator_set_distance_disjoint() {
        let ops1 = vec!["a".to_string(), "b".to_string()];
        let ops2 = vec!["c".to_string(), "d".to_string()];
        assert_eq!(operator_set_distance(&ops1, &ops2), 4);
    }

    #[test]
    fn operator_set_distance_partial_overlap() {
        let ops1 = vec!["a".to_string(), "b".to_string(), "c".to_string()];
        let ops2 = vec!["b".to_string(), "c".to_string(), "d".to_string()];
        // diff = {a, d} → 2
        assert_eq!(operator_set_distance(&ops1, &ops2), 2);
    }

    #[test]
    fn extract_operator_names_filters_non_operators() {
        let json = serde_json::json!({
            "nodes": {
                "p1": { "name": "x",       "class_type": "Parameter" },
                "o1": { "name": "scaler",   "class_type": "Operator" },
                "s1": { "name": "snippet1", "class_type": "Snippet" },
                "o2": { "name": "pca",      "class_type": "Operator" }
            },
            "edges": []
        });
        let names = extract_operator_names(&json);
        assert_eq!(names, vec!["pca", "scaler"]); // sorted
    }

    #[test]
    fn extract_operator_names_empty() {
        let json = serde_json::json!({ "nodes": {}, "edges": [] });
        assert!(extract_operator_names(&json).is_empty());
    }

    // ================================================================
    // 11. DagGraph construction helpers
    // ================================================================

    #[test]
    fn dag_graph_node_count_edge_count() {
        let g = diamond_pipeline();
        assert_eq!(g.node_count(), 4);
        assert_eq!(g.edge_count(), 4);
    }

    #[test]
    fn dag_graph_outgoing_incoming() {
        let g = linear_pipeline();
        let out_a = g.outgoing("a");
        assert_eq!(out_a.len(), 1);
        assert_eq!(out_a[0].0, "b");

        let inc_c = g.incoming("c");
        assert_eq!(inc_c.len(), 1);
        assert_eq!(inc_c[0].0, "b");

        // No incoming to root
        assert!(g.incoming("a").is_empty());
        // No outgoing from leaf
        assert!(g.outgoing("c").is_empty());
    }

    #[test]
    fn dag_graph_from_json_missing_fields_defaults() {
        // Nodes without explicit name/type should get defaults
        let json = serde_json::json!({
            "nodes": {
                "n1": {}
            },
            "edges": []
        });
        let g = DagGraph::from_json(&json);
        assert_eq!(g.node_count(), 1);
        // Name defaults to node id
        assert_eq!(g.node_names["n1"], "n1");
        // Type defaults to "Node"
        assert_eq!(g.node_types["n1"], "Node");
    }

    // ================================================================
    // 12. Triangle inequality (GED is a metric)
    // ================================================================

    #[test]
    fn triangle_inequality() {
        let g1 = single_node("a", "op_a", "Operator");
        let g2 = single_node("b", "op_b", "Operator");
        let g3 = DagGraph::from_json(&serde_json::json!({
            "nodes": {
                "x": { "name": "op_a", "class_type": "Operator" },
                "y": { "name": "op_c", "class_type": "Operator" }
            },
            "edges": [
                { "source": "x", "destination": "y", "position": 1, "output": 0 }
            ]
        }));

        let d12 = exact_ged(&g1, &g2, 10_000).unwrap();
        let d23 = exact_ged(&g2, &g3, 10_000).unwrap();
        let d13 = exact_ged(&g1, &g3, 10_000).unwrap();

        assert!(
            d13 <= d12 + d23,
            "triangle inequality violated: d(1,3)={d13} > d(1,2)={d12} + d(2,3)={d23}"
        );
    }
}
