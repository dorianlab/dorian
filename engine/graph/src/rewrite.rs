//! Rewrite rule engine — pattern matching and graph transformations.
//!
//! Ports the Python rewrite system (`dorian/pipeline/parser.py`,
//! `dorian/pipeline/transforms.py`, `dorian/code/parsing/rule.py`)
//! to Rust.
//!
//! Core concepts:
//! - **Pattern**: A small `ProcessGraph` where `PatternNode` entries have
//!   regex constraints on `type` and `text`.
//! - **Matching**: `match_rule()` finds a mapping from pattern node IDs
//!   to concrete graph node IDs.
//! - **Transformations**: `Add`, `Delete`, `Apply`, `Replace` — applied
//!   sequentially, each receiving the (possibly extended) mapping.
//! - **RewriteRule**: Groups a pattern with its transformations and metadata.
//! - **`sync_apply()`**: Applies a rule exhaustively (loops until no match).

use crate::model::{
    DeliveryMode, Edge, Node, PatternNode, Position, ProcessGraph,
};
use regex::Regex;
use rustc_hash::FxHashMap;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use uuid::Uuid;

/// Process-wide regex cache keyed on the *anchored* pattern string.
/// The Python matcher pre-compiles each pattern via ``_re_cache`` so
/// the hot-loop comparator is a hash lookup; without this cache the
/// rust matcher recompiles ``^(?:sklearn\.ensemble\..*)`` every time
/// it walks a candidate node, which dominates wall time on DAGs of
/// any non-trivial size.
fn _regex_cache() -> &'static Mutex<FxHashMap<String, Option<Regex>>> {
    static CACHE: OnceLock<Mutex<FxHashMap<String, Option<Regex>>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(FxHashMap::default()))
}

// ---------------------------------------------------------------------------
// Transformation types (mirrors dorian/code/parsing/rule.py)
// ---------------------------------------------------------------------------

/// Priority level for rewrite rules.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Default)]
pub enum Priority {
    Low = 0,
    #[default]
    Medium = 50,
    High = 100,
}

/// Purge mode for Delete transformations.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum PurgeMode {
    /// Remove the node and all descendant subgraph.
    Recursive,
    /// Remove the node, reconnect predecessors to successors.
    #[default]
    Isolated,
}

/// Add nodes and/or edges to the graph.
#[derive(Debug, Clone)]
pub struct Add {
    /// Named nodes (local_id → Node) — added to graph with generated UUIDs,
    /// mapping extended with `local_id → real_uuid`.
    pub nodes: FxHashMap<String, Node>,
    /// Edges to add. Source/destination can be local IDs or pattern-mapped IDs.
    pub edges: Vec<EdgeSpec>,
}

/// Specification for an edge to be added during a rewrite.
///
/// Source and destination refer to *mapping keys* (pattern IDs or local IDs
/// from `Add.nodes`), not real graph node IDs.
#[derive(Debug, Clone)]
pub struct EdgeSpec {
    pub source: String,
    pub destination: String,
    pub position: Position,
    pub output: Position,
    pub delivery_mode: DeliveryMode,
}

impl EdgeSpec {
    pub fn new(source: impl Into<String>, destination: impl Into<String>) -> Self {
        EdgeSpec {
            source: source.into(),
            destination: destination.into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        }
    }

    pub fn with_position(mut self, pos: Position) -> Self {
        self.position = pos;
        self
    }

    pub fn with_output(mut self, out: Position) -> Self {
        self.output = out;
        self
    }
}

/// Delete nodes and/or edges from the graph.
#[derive(Debug, Clone)]
pub struct Delete {
    /// Pattern-mapped node IDs to remove.
    pub nodes: Vec<String>,
    /// Edge tuples (source_key, destination_key) to remove.
    pub edges: Vec<(String, String)>,
    /// Purge mode.
    pub mode: PurgeMode,
}

/// Apply an arbitrary function to the graph.
///
/// The function receives `(graph, mapping, meta)` and returns a new graph.
/// This is the escape hatch for complex transformations that can't be
/// expressed as Add/Delete.
pub type ApplyFn = Box<dyn Fn(ProcessGraph, &Mapping, &Meta) -> ProcessGraph + Send + Sync>;

/// Wrapper for the Apply transformation (to hold the boxed closure).
pub struct Apply {
    pub f: ApplyFn,
}

impl std::fmt::Debug for Apply {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Apply(<fn>)")
    }
}

/// Replace the entire graph (effectively clears it).
#[derive(Debug, Clone)]
pub struct Replace;

/// A single transformation step in a rewrite rule.
#[derive(Debug)]
pub enum Transformation {
    Add(Add),
    Delete(Delete),
    Apply(Apply),
    Replace(Replace),
}

// ---------------------------------------------------------------------------
// Mapping and context types
// ---------------------------------------------------------------------------

/// Maps pattern node IDs (and local Add IDs) to concrete graph node IDs.
pub type Mapping = FxHashMap<String, String>;

/// Maps pattern node IDs to concrete graph node IDs (alias for processed checks).
pub type Candidate = FxHashMap<String, String>;

/// Runtime context carried through rewrite chains.
pub type Meta = HashMap<String, String>;

// ---------------------------------------------------------------------------
// RewriteRule
// ---------------------------------------------------------------------------

/// A rewrite rule: pattern + transformations.
///
/// Mirrors `dorian.code.parsing.rule.RewriteRule`.
pub struct RewriteRule {
    /// Pattern graph with PatternNode entries for regex matching.
    pub pattern: ProcessGraph,
    /// Human-readable description.
    pub description: String,
    /// Ordered sequence of transformations to apply on match.
    pub transformations: Vec<Transformation>,
    /// Priority (higher = applied first when multiple rules match).
    pub priority: Priority,
    /// Stable identity for this rule.
    pub id: String,
}

impl std::fmt::Debug for RewriteRule {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RewriteRule")
            .field("id", &self.id)
            .field("description", &self.description)
            .field("priority", &self.priority)
            .field("pattern_nodes", &self.pattern.node_count())
            .field("transformations", &self.transformations.len())
            .finish()
    }
}

impl RewriteRule {
    /// Create a new rewrite rule.
    pub fn new(
        pattern: ProcessGraph,
        description: impl Into<String>,
        transformations: Vec<Transformation>,
    ) -> Self {
        let id = Uuid::new_v4().to_string();
        RewriteRule {
            pattern,
            description: description.into(),
            transformations,
            priority: Priority::default(),
            id,
        }
    }

    /// Set the priority.
    pub fn with_priority(mut self, priority: Priority) -> Self {
        self.priority = priority;
        self
    }

    /// Set a stable ID.
    pub fn with_id(mut self, id: impl Into<String>) -> Self {
        self.id = id.into();
        self
    }
}

// ---------------------------------------------------------------------------
// Pattern matching
// ---------------------------------------------------------------------------

/// Compare a concrete graph node against a pattern node using regex.
///
/// Mirrors `comparator()` in `dorian/pipeline/parser.py` exactly:
///
///   * Pattern (``Node``) vs concrete ``Operator``: regex-match the
///     pattern's ``type`` against the literal "Operator", ``text``
///     against the operator's name, ``language`` against the
///     operator's language.
///   * Pattern vs concrete ``Parameter``: regex-match the pattern's
///     ``type`` against the literal "Parameter". Text is **not**
///     consulted — Python's comparator ignores it for Parameter
///     matches, and several KB rules rely on that looseness.
///   * Pattern vs concrete ``Snippet``: always false. Snippets are
///     content-exact and Dorian's rewrite system never patterns over
///     them.
///   * Concrete-vs-concrete (Operator/Parameter/Snippet pattern
///     against the same kind): exact-name equality. This is a Rust-
///     only convenience for tests; the Python compiler emits only
///     ``Node`` patterns.
fn node_matches(concrete: &Node, pattern: &Node) -> bool {
    match pattern {
        Node::Node(pat) => {
            match concrete {
                Node::Operator(op) => {
                    let type_ok = regex_matches(&pat.node_type, "Operator");
                    let text_ok = regex_matches(&pat.text, &op.name);
                    let lang_ok =
                        pat.language == ".*" || regex_matches(&pat.language, &op.language);
                    type_ok && text_ok && lang_ok
                }
                Node::Parameter(_) => {
                    // Python comparator ignores text/language for
                    // Parameter matches. Match on type discriminator only.
                    regex_matches(&pat.node_type, "Parameter")
                }
                // Snippets never match patterns — content-exact.
                Node::Snippet(_) => false,
                Node::Node(_) => false,
                Node::Group(g) => {
                    let type_ok = regex_matches(&pat.node_type, "Group");
                    let text_ok = regex_matches(&pat.text, &g.name);
                    type_ok && text_ok
                }
            }
        }
        Node::Operator(pat_op) => matches!(concrete, Node::Operator(op) if op.name == pat_op.name),
        Node::Parameter(pat_p) => {
            matches!(concrete, Node::Parameter(p) if p.name == pat_p.name && p.dtype == pat_p.dtype)
        }
        Node::Snippet(pat_s) => {
            matches!(concrete, Node::Snippet(s) if s.name == pat_s.name)
        }
        Node::Group(_) => false,
    }
}

/// Match `pattern` against `text` with Python ``re.match`` semantics —
/// anchored at the start, *not* at the end. ``"sklearn"`` matches
/// ``"sklearn.ensemble"`` because the prefix matches, mirroring
/// ``re.match("sklearn", "sklearn.ensemble")``. Compiled patterns
/// are cached process-wide; an entry of ``None`` records an invalid
/// regex so we don't keep retrying the compile.
fn regex_matches(pattern: &str, text: &str) -> bool {
    let anchored = if pattern.starts_with('^') {
        pattern.to_string()
    } else {
        format!("^(?:{pattern})")
    };
    let mut cache = match _regex_cache().lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    let entry = cache
        .entry(anchored.clone())
        .or_insert_with(|| Regex::new(&anchored).ok());
    match entry {
        Some(re) => re.is_match(text),
        None => text.starts_with(pattern),
    }
}

/// Find a mapping from pattern node IDs to concrete graph node IDs.
///
/// Mirrors `match()` in `dorian/pipeline/parser.py`.
///
/// Uses brute-force cartesian product of possible mappings (patterns are
/// typically 1-3 nodes, so this is fast).
pub fn match_rule(
    pattern: &ProcessGraph,
    graph: &ProcessGraph,
    processed: &[Candidate],
) -> Option<Mapping> {
    let pattern_ids: Vec<&str> = pattern.nodes.keys().map(|s| s.as_str()).collect();
    if pattern_ids.is_empty() {
        return None;
    }

    // For each pattern node, collect all graph nodes that match.
    let mut candidates_per_pattern: Vec<Vec<&str>> = Vec::new();
    for pid in &pattern_ids {
        let pat_node = pattern.nodes.get(*pid).unwrap();
        let matching: Vec<&str> = graph
            .nodes
            .iter()
            .filter(|(_, node)| node_matches(node, pat_node))
            .map(|(id, _)| id.as_str())
            .collect();
        if matching.is_empty() {
            return None; // pattern node has no candidate → no match possible
        }
        candidates_per_pattern.push(matching);
    }

    // Guard: bail out if the cartesian product would exceed the safety limit.
    // Patterns are typically 1-3 nodes so this rarely triggers, but a
    // pathological pattern against a large graph could explode combinatorially.
    const MAX_COMBINATIONS: usize = 100_000;
    let total: usize = candidates_per_pattern
        .iter()
        .try_fold(1usize, |acc, c| acc.checked_mul(c.len()))
        .unwrap_or(usize::MAX);
    if total > MAX_COMBINATIONS {
        log::warn!(
            "match_rule: cartesian product ({}) exceeds limit ({}), skipping",
            total,
            MAX_COMBINATIONS
        );
        return None;
    }

    // Generate all combinations (cartesian product).
    let mut combos: Vec<Vec<&str>> = vec![vec![]];
    for candidates in &candidates_per_pattern {
        let mut new_combos = Vec::new();
        for combo in &combos {
            for &c in candidates {
                let mut extended = combo.clone();
                extended.push(c);
                new_combos.push(extended);
            }
        }
        combos = new_combos;
    }

    // Check each candidate mapping.
    for combo in &combos {
        // All pattern node IDs must map to distinct graph node IDs.
        let unique: std::collections::HashSet<&str> = combo.iter().copied().collect();
        if unique.len() != combo.len() {
            continue;
        }

        // Build mapping.
        let mapping: Mapping = pattern_ids
            .iter()
            .zip(combo.iter())
            .map(|(pid, gid)| (pid.to_string(), gid.to_string()))
            .collect();

        // Check if already processed.
        if processed.contains(&mapping) {
            continue;
        }

        // Verify all pattern edges exist in the graph.
        let edges_ok = pattern.edges.iter().all(|pe| {
            let src = mapping.get(&pe.source);
            let dst = mapping.get(&pe.destination);
            match (src, dst) {
                (Some(gs), Some(gd)) => graph
                    .edges
                    .iter()
                    .any(|ge| ge.source == *gs && ge.destination == *gd),
                _ => false,
            }
        });

        if edges_ok {
            return Some(mapping);
        }
    }

    None
}

// ---------------------------------------------------------------------------
// Transformation execution
// ---------------------------------------------------------------------------

/// Resolve a key through the mapping, returning the concrete graph node ID.
fn resolve(key: &str, mapping: &Mapping) -> Option<String> {
    mapping.get(key).cloned()
}

/// Apply a single transformation to the graph, returning the updated graph
/// and (possibly extended) mapping.
fn apply_transformation(
    mut graph: ProcessGraph,
    mut mapping: Mapping,
    transformation: &Transformation,
    meta: &Meta,
) -> (ProcessGraph, Mapping) {
    match transformation {
        Transformation::Add(add) => {
            // Add named nodes with generated UUIDs, extend mapping.
            for (local_id, node) in &add.nodes {
                let real_id = Uuid::new_v4().to_string();
                graph.add_node(real_id.clone(), node.clone());
                mapping.insert(local_id.clone(), real_id);
            }

            // Add edges, resolving source/dest through extended mapping.
            for spec in &add.edges {
                let src = resolve(&spec.source, &mapping);
                let dst = resolve(&spec.destination, &mapping);
                if let (Some(s), Some(d)) = (src, dst) {
                    graph.add_edge(Edge {
                        source: s,
                        destination: d,
                        position: spec.position.clone(),
                        output: spec.output.clone(),
                        delivery_mode: spec.delivery_mode,
                    });
                }
            }

            (graph, mapping)
        }

        Transformation::Delete(del) => {
            // Collect real IDs to remove.
            let ids_to_remove: Vec<String> = del
                .nodes
                .iter()
                .filter_map(|key| resolve(key, &mapping))
                .collect();

            match del.mode {
                PurgeMode::Isolated => {
                    // Reconnect: for each removed node, connect its predecessors
                    // to its successors with existing edge metadata.
                    for id in &ids_to_remove {
                        let incoming: Vec<Edge> = graph
                            .incoming_edges(id)
                            .iter()
                            .map(|e| (*e).clone())
                            .collect();
                        let outgoing: Vec<Edge> = graph
                            .outgoing_edges(id)
                            .iter()
                            .map(|e| (*e).clone())
                            .collect();

                        for inc in &incoming {
                            for out in &outgoing {
                                graph.add_edge(Edge {
                                    source: inc.source.clone(),
                                    destination: out.destination.clone(),
                                    position: out.position.clone(),
                                    output: inc.output.clone(),
                                    delivery_mode: out.delivery_mode,
                                });
                            }
                        }
                    }
                }
                PurgeMode::Recursive => {
                    // TODO: recursively remove all descendants
                }
            }

            // Remove nodes.
            for id in &ids_to_remove {
                graph.nodes.remove(id);
            }

            // Remove edges touching deleted nodes.
            graph.edges.retain(|e| {
                !ids_to_remove.contains(&e.source) && !ids_to_remove.contains(&e.destination)
            });

            // Remove explicitly listed edges.
            for (src_key, dst_key) in &del.edges {
                if let (Some(s), Some(d)) = (resolve(src_key, &mapping), resolve(dst_key, &mapping))
                {
                    graph.edges.retain(|e| !(e.source == s && e.destination == d));
                }
            }

            (graph, mapping)
        }

        Transformation::Apply(apply) => {
            let new_graph = (apply.f)(graph, &mapping, meta);
            (new_graph, mapping)
        }

        Transformation::Replace(_) => {
            // Replace entire graph with empty.
            (ProcessGraph::new(), mapping)
        }
    }
}

/// Apply a sequence of transformations to the graph.
fn rewrite(
    graph: ProcessGraph,
    mapping: Mapping,
    transformations: &[Transformation],
    meta: &Meta,
) -> ProcessGraph {
    let mut current = graph;
    let mut current_mapping = mapping;

    for t in transformations {
        let (g, m) = apply_transformation(current, current_mapping, t, meta);
        current = g;
        current_mapping = m;
    }

    current
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Apply a rewrite rule exhaustively to the graph (sync).
///
/// Loops until no new matches are found, mirroring `sync_apply()` in
/// `dorian/pipeline/transforms.py`.
pub fn sync_apply(rule: &RewriteRule, graph: ProcessGraph, meta: &Meta) -> ProcessGraph {
    const MAX_ITERATIONS: usize = 1000;

    let mut current = graph;
    let mut processed: Vec<Candidate> = Vec::new();

    while let Some(mapping) = match_rule(&rule.pattern, &current, &processed) {
        processed.push(mapping.clone());
        current = rewrite(current, mapping, &rule.transformations, meta);

        if processed.len() >= MAX_ITERATIONS {
            log::warn!(
                "sync_apply: reached {} iterations for rule '{}', breaking to avoid infinite loop",
                MAX_ITERATIONS,
                rule.id
            );
            break;
        }
    }

    current
}

/// Apply a sequence of rewrite rules in order.
///
/// Mirrors `transform()` in `dorian/pipeline/parser.py`.
pub fn transform(graph: ProcessGraph, rules: &[&RewriteRule], meta: &Meta) -> ProcessGraph {
    let mut current = graph;
    for rule in rules {
        current = sync_apply(rule, current, meta);
    }
    current
}

/// Remove a node by ID and reconnect predecessors to successors (isolated purge).
pub fn remove_node_isolated(graph: &mut ProcessGraph, node_id: &str) {
    let incoming: Vec<Edge> = graph
        .incoming_edges(node_id)
        .iter()
        .map(|e| (*e).clone())
        .collect();
    let outgoing: Vec<Edge> = graph
        .outgoing_edges(node_id)
        .iter()
        .map(|e| (*e).clone())
        .collect();

    // Reconnect.
    for inc in &incoming {
        for out in &outgoing {
            graph.add_edge(Edge {
                source: inc.source.clone(),
                destination: out.destination.clone(),
                position: out.position.clone(),
                output: inc.output.clone(),
                delivery_mode: out.delivery_mode,
            });
        }
    }

    // Remove node and its edges.
    graph.nodes.remove(node_id);
    graph
        .edges
        .retain(|e| e.source != node_id && e.destination != node_id);
}

// ---------------------------------------------------------------------------
// Builder helpers for creating rules concisely
// ---------------------------------------------------------------------------

/// Create a pattern graph with a single PatternNode.
pub fn single_node_pattern(
    id: impl Into<String>,
    node_type: impl Into<String>,
    text: impl Into<String>,
) -> ProcessGraph {
    let mut g = ProcessGraph::new();
    g.add_node(
        id.into(),
        Node::Node(PatternNode {
            node_type: node_type.into(),
            text: text.into(),
            language: ".*".to_string(),
        }),
    );
    g
}

/// Create an Add transformation with named nodes and edge specs.
pub fn add_nodes_and_edges(
    nodes: Vec<(impl Into<String>, Node)>,
    edges: Vec<EdgeSpec>,
) -> Transformation {
    let mut node_map = FxHashMap::default();
    for (id, node) in nodes {
        node_map.insert(id.into(), node);
    }
    Transformation::Add(Add {
        nodes: node_map,
        edges,
    })
}

/// Create a Delete transformation for pattern-mapped node IDs.
pub fn delete_nodes(nodes: Vec<impl Into<String>>, mode: PurgeMode) -> Transformation {
    Transformation::Delete(Delete {
        nodes: nodes.into_iter().map(Into::into).collect(),
        edges: Vec::new(),
        mode,
    })
}

/// Create an Apply transformation from a closure.
pub fn apply_fn<F>(f: F) -> Transformation
where
    F: Fn(ProcessGraph, &Mapping, &Meta) -> ProcessGraph + Send + Sync + 'static,
{
    Transformation::Apply(Apply { f: Box::new(f) })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Operator, Parameter, Snippet};

    // -- Helper: build a simple test graph ---
    fn test_graph() -> ProcessGraph {
        // param → read_csv → scaler
        let json = serde_json::json!({
            "nodes": {
                "p1": {"class_type": "Parameter", "name": "fpath", "dtype": "string", "value": "/data/test.csv"},
                "o1": {"class_type": "Operator", "name": "pandas.read_csv", "language": "python"},
                "o2": {"class_type": "Operator", "name": "sklearn.preprocessing.StandardScaler", "language": "python"}
            },
            "edges": [
                {"source": "p1", "destination": "o1", "position": 0, "output": 0},
                {"source": "o1", "destination": "o2", "position": 1, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    // -- Pattern matching tests ---

    #[test]
    fn test_match_single_operator() {
        let graph = test_graph();

        // Pattern: match any operator with "pandas.*" name.
        let pattern = single_node_pattern("n", "Operator", r"pandas\..*");

        let result = match_rule(&pattern, &graph, &[]);
        assert!(result.is_some());
        let mapping = result.unwrap();
        assert_eq!(mapping["n"], "o1");
    }

    #[test]
    fn test_match_single_parameter() {
        let graph = test_graph();

        let pattern = single_node_pattern("n", "Parameter", ".*");
        let result = match_rule(&pattern, &graph, &[]);
        assert!(result.is_some());
        assert_eq!(result.unwrap()["n"], "p1");
    }

    #[test]
    fn test_match_specific_operator() {
        let graph = test_graph();

        // Pattern: match only sklearn operators.
        let pattern = single_node_pattern("n", "Operator", r"sklearn\..*");
        let result = match_rule(&pattern, &graph, &[]);
        assert!(result.is_some());
        assert_eq!(result.unwrap()["n"], "o2");
    }

    #[test]
    fn test_match_no_match() {
        let graph = test_graph();

        // Pattern: match a WASM operator (none exist).
        let pattern = single_node_pattern("n", "Operator", r"wasm\..*");
        let result = match_rule(&pattern, &graph, &[]);
        assert!(result.is_none());
    }

    #[test]
    fn test_match_with_edge() {
        let graph = test_graph();

        // Pattern: Parameter connected to an Operator.
        let mut pattern = ProcessGraph::new();
        pattern.add_node(
            "a".to_string(),
            Node::Node(PatternNode {
                node_type: "Parameter".to_string(),
                text: ".*".to_string(),
                language: ".*".to_string(),
            }),
        );
        pattern.add_node(
            "b".to_string(),
            Node::Node(PatternNode {
                node_type: "Operator".to_string(),
                text: ".*".to_string(),
                language: ".*".to_string(),
            }),
        );
        pattern.add_edge(Edge::new("a".to_string(), "b".to_string()));

        let result = match_rule(&pattern, &graph, &[]);
        assert!(result.is_some());
        let mapping = result.unwrap();
        assert_eq!(mapping["a"], "p1");
        assert_eq!(mapping["b"], "o1");
    }

    #[test]
    fn test_match_skip_processed() {
        let graph = test_graph();

        // Pattern: match any operator.
        let pattern = single_node_pattern("n", "Operator", ".*");

        let first = match_rule(&pattern, &graph, &[]).unwrap();
        let second = match_rule(&pattern, &graph, std::slice::from_ref(&first));
        assert!(second.is_some());
        let second = second.unwrap();
        assert_ne!(first["n"], second["n"]);

        // Third should be None — only two operators.
        let third = match_rule(&pattern, &graph, &[first, second]);
        assert!(third.is_none());
    }

    // -- Transformation tests ---

    #[test]
    fn test_add_transformation() {
        let graph = test_graph();
        let initial_count = graph.node_count();

        let rule = RewriteRule::new(
            single_node_pattern("n", "Operator", r"pandas\.read_csv"),
            "add a logger after read_csv",
            vec![add_nodes_and_edges(
                vec![(
                    "logger".to_string(),
                    Node::Snippet(Snippet {
                        name: "log_shape".to_string(),
                        code: "def foo(df): print(df.shape); return df".to_string(),
                        language: "python".to_string(),
                    }),
                )],
                vec![EdgeSpec::new("n", "logger").with_position(Position::Index(1))],
            )],
        );

        let meta = Meta::new();
        let result = sync_apply(&rule, graph, &meta);
        assert_eq!(result.node_count(), initial_count + 1);
        // The new snippet node should exist.
        assert!(result.nodes.values().any(|n| {
            if let Node::Snippet(s) = n {
                s.name == "log_shape"
            } else {
                false
            }
        }));
    }

    #[test]
    fn test_delete_transformation() {
        let graph = test_graph();

        // Delete the scaler node (o2), isolated mode reconnects o1 → nothing.
        let rule = RewriteRule::new(
            single_node_pattern("n", "Operator", r"sklearn\..*"),
            "remove sklearn operator",
            vec![delete_nodes(vec!["n"], PurgeMode::Isolated)],
        );

        let meta = Meta::new();
        let result = sync_apply(&rule, graph, &meta);
        assert_eq!(result.node_count(), 2); // p1, o1
        assert!(result.nodes.values().all(|n| {
            match n {
                Node::Operator(op) => !op.name.starts_with("sklearn"),
                _ => true,
            }
        }));
    }

    #[test]
    fn test_apply_fn_transformation() {
        let graph = test_graph();

        // Apply function that adds a tag to meta (just verify it's called by
        // checking graph is returned unchanged).
        let rule = RewriteRule::new(
            single_node_pattern("n", "Parameter", ".*"),
            "noop apply",
            vec![apply_fn(|g, _mapping, _meta| g)],
        );

        let meta = Meta::new();
        let result = sync_apply(&rule, graph.clone(), &meta);
        assert_eq!(result.node_count(), graph.node_count());
    }

    #[test]
    fn test_replace_transformation() {
        let graph = test_graph();

        let rule = RewriteRule::new(
            single_node_pattern("n", "Parameter", ".*"),
            "replace with empty",
            vec![Transformation::Replace(Replace)],
        );

        let meta = Meta::new();
        let result = sync_apply(&rule, graph, &meta);
        assert_eq!(result.node_count(), 0);
    }

    #[test]
    fn test_exhaustive_application() {
        // Verify sync_apply loops until no more matches.
        let json = serde_json::json!({
            "nodes": {
                "d1": {"class_type": "Operator", "name": "dorian.io.dataset", "language": "python"},
                "d2": {"class_type": "Operator", "name": "dorian.io.dataset", "language": "python"},
                "o1": {"class_type": "Operator", "name": "sklearn.preprocessing.StandardScaler", "language": "python"}
            },
            "edges": [
                {"source": "d1", "destination": "o1", "position": 1, "output": 0},
                {"source": "d2", "destination": "o1", "position": 2, "output": 0}
            ]
        });
        let graph = ProcessGraph::from_json(&json).unwrap();

        // Rule: replace dorian.io.dataset with a parameter + pandas.read_csv.
        let rule = RewriteRule::new(
            single_node_pattern("n", "Operator", r"dorian\.io\.dataset"),
            "expand dataset",
            vec![apply_fn(|mut g, mapping, _meta| {
                let matched_id = &mapping["n"];
                let outgoing = g.outgoing_edges(matched_id).iter().map(|e| (*e).clone()).collect::<Vec<_>>();

                // Remove the old node.
                g.nodes.remove(matched_id);
                g.edges.retain(|e| e.source != *matched_id && e.destination != *matched_id);

                // Add parameter + read_csv.
                let param_id = Uuid::new_v4().to_string();
                let loader_id = Uuid::new_v4().to_string();

                g.add_node(param_id.clone(), Node::Parameter(Parameter {
                    name: "fpath".to_string(),
                    dtype: crate::model::ParamDtype::String,
                    value: "/data/test.csv".to_string(),
                }));
                g.add_node(loader_id.clone(), Node::Operator(Operator {
                    name: "pandas.read_csv".to_string(),
                    language: "python".to_string(),
                    tasks: vec![],
                }));
                g.add_edge(Edge {
                    source: param_id,
                    destination: loader_id.clone(),
                    position: Position::Index(0),
                    output: Position::Index(0),
                    delivery_mode: DeliveryMode::Once,
                });

                // Rewire outgoing edges.
                for out in outgoing {
                    g.add_edge(Edge {
                        source: loader_id.clone(),
                        destination: out.destination,
                        position: out.position,
                        output: Position::Index(0),
                        delivery_mode: out.delivery_mode,
                    });
                }

                g
            })],
        );

        let meta = Meta::new();
        let result = sync_apply(&rule, graph, &meta);

        // Both dorian.io.dataset nodes should be expanded.
        assert!(!result
            .nodes
            .values()
            .any(|n| matches!(n, Node::Operator(op) if op.name == "dorian.io.dataset")));
        // Should have 2 params + 2 read_csv + 1 scaler = 5 nodes.
        assert_eq!(result.node_count(), 5);
    }

    #[test]
    fn test_transform_chain() {
        let json = serde_json::json!({
            "nodes": {
                "d1": {"class_type": "Operator", "name": "dorian.io.dataset", "language": "python"},
                "o1": {"class_type": "Operator", "name": "dorian.io.printout", "language": "python"}
            },
            "edges": [
                {"source": "d1", "destination": "o1", "position": 1, "output": 0}
            ]
        });
        let graph = ProcessGraph::from_json(&json).unwrap();

        // Two rules applied in sequence.
        let rule1 = RewriteRule::new(
            single_node_pattern("n", "Operator", r"dorian\.io\.dataset"),
            "remove dataset",
            vec![delete_nodes(vec!["n"], PurgeMode::Isolated)],
        );
        let rule2 = RewriteRule::new(
            single_node_pattern("n", "Operator", r"dorian\.io\.printout"),
            "remove printout",
            vec![delete_nodes(vec!["n"], PurgeMode::Isolated)],
        );

        let meta = Meta::new();
        let result = transform(graph, &[&rule1, &rule2], &meta);
        assert_eq!(result.node_count(), 0);
    }

    #[test]
    fn test_isolated_delete_reconnects() {
        // a → b → c  ──(delete b)──►  a → c
        let json = serde_json::json!({
            "nodes": {
                "a": {"class_type": "Parameter", "name": "x", "dtype": "int", "value": "1"},
                "b": {"class_type": "Operator", "name": "middle", "language": "python"},
                "c": {"class_type": "Operator", "name": "end", "language": "python"}
            },
            "edges": [
                {"source": "a", "destination": "b", "position": 1, "output": 0},
                {"source": "b", "destination": "c", "position": 1, "output": 0}
            ]
        });
        let graph = ProcessGraph::from_json(&json).unwrap();

        let rule = RewriteRule::new(
            single_node_pattern("n", "Operator", "middle"),
            "remove middle",
            vec![delete_nodes(vec!["n"], PurgeMode::Isolated)],
        );

        let meta = Meta::new();
        let result = sync_apply(&rule, graph, &meta);
        assert_eq!(result.node_count(), 2);
        // Edge from a → c should exist.
        assert!(result
            .edges
            .iter()
            .any(|e| e.source == "a" && e.destination == "c"));
    }

    #[test]
    fn test_chained_add_and_delete() {
        // Pattern: find dataset operator, replace with param+loader, delete original.
        let graph = {
            let json = serde_json::json!({
                "nodes": {
                    "d1": {"class_type": "Operator", "name": "dorian.io.dataset", "language": "python"},
                    "o1": {"class_type": "Operator", "name": "sklearn.preprocessing.StandardScaler", "language": "python"}
                },
                "edges": [
                    {"source": "d1", "destination": "o1", "position": 1, "output": 0}
                ]
            });
            ProcessGraph::from_json(&json).unwrap()
        };

        let rule = RewriteRule::new(
            single_node_pattern("n", "Operator", r"dorian\.io\.dataset"),
            "expand dataset ref",
            vec![
                // 1. Add param + loader nodes
                add_nodes_and_edges(
                    vec![
                        (
                            "param".to_string(),
                            Node::Parameter(Parameter {
                                name: "fpath".to_string(),
                                dtype: crate::model::ParamDtype::String,
                                value: "".to_string(),
                            }),
                        ),
                        (
                            "loader".to_string(),
                            Node::Operator(Operator {
                                name: "pandas.read_csv".to_string(),
                                language: "python".to_string(),
                                tasks: vec![],
                            }),
                        ),
                    ],
                    vec![EdgeSpec::new("param", "loader")],
                ),
                // 2. Apply: rewire outgoing edges from matched node to loader
                apply_fn(|mut g, mapping, _meta| {
                    let matched = &mapping["n"];
                    let loader = &mapping["loader"];
                    let outgoing: Vec<Edge> = g
                        .outgoing_edges(matched)
                        .iter()
                        .map(|e| (*e).clone())
                        .collect();
                    for e in outgoing {
                        g.add_edge(Edge {
                            source: loader.clone(),
                            destination: e.destination,
                            position: e.position,
                            output: e.output,
                            delivery_mode: e.delivery_mode,
                        });
                    }
                    g
                }),
                // 3. Delete the original dorian.io.dataset node
                delete_nodes(vec!["n"], PurgeMode::Isolated),
            ],
        );

        let meta = Meta::new();
        let result = sync_apply(&rule, graph, &meta);

        // Should have: param, loader, scaler = 3 nodes.
        assert_eq!(result.node_count(), 3);
        // No dorian.io.dataset left.
        assert!(!result
            .nodes
            .values()
            .any(|n| matches!(n, Node::Operator(op) if op.name == "dorian.io.dataset")));
        // Should have param → loader and loader → scaler edges.
        assert_eq!(result.edge_count(), 2);
    }

    #[test]
    fn test_regex_matches_function() {
        assert!(regex_matches(".*", "anything"));
        assert!(regex_matches("Operator", "Operator"));
        assert!(!regex_matches("Operator", "Parameter"));
        assert!(regex_matches(r"dorian\.io\..*", "dorian.io.dataset"));
        assert!(!regex_matches(r"dorian\.io\..*", "sklearn.preprocessing"));
        assert!(regex_matches(r"sklearn\..*", "sklearn.preprocessing.StandardScaler"));
    }

    #[test]
    fn test_remove_node_isolated() {
        let json = serde_json::json!({
            "nodes": {
                "a": {"class_type": "Parameter", "name": "x", "dtype": "int", "value": "1"},
                "b": {"class_type": "Operator", "name": "mid", "language": "python"},
                "c": {"class_type": "Operator", "name": "end", "language": "python"}
            },
            "edges": [
                {"source": "a", "destination": "b", "position": 1, "output": 0},
                {"source": "b", "destination": "c", "position": 1, "output": 0}
            ]
        });
        let mut graph = ProcessGraph::from_json(&json).unwrap();
        remove_node_isolated(&mut graph, "b");

        assert_eq!(graph.node_count(), 2);
        assert!(graph.edges.iter().any(|e| e.source == "a" && e.destination == "c"));
    }

    #[test]
    fn test_empty_pattern_no_match() {
        let graph = test_graph();
        let pattern = ProcessGraph::new();
        assert!(match_rule(&pattern, &graph, &[]).is_none());
    }
}
