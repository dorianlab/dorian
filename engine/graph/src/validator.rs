//! Pipeline validator — structural + type-level checks run BEFORE the
//! executor ever sees the DAG.
//!
//! Purpose: produce typed, metadata-rich errors at the point where the
//! pipeline is submitted for execution. The RL environment (and any
//! other consumer) gets specific failure reasons it can feed straight
//! into the exception registry — no traceback templating, no LLM, no
//! guessing. Each `ValidationError` variant is already a stable leaf
//! identity, clustered naturally by its variant name.
//!
//! See internal design note for the end-to-end flow. The
//! validator emits errors with `site_library = "dorian.pipeline.validator"`
//! and the variant name as the exception-type surrogate.
//!
//! Design note: the validator does NOT know about Dask. Its contract is
//! "given a DAG + KB-sourced port signatures, tell me what's wrong with
//! the DAG." Nothing runs. No external state. Pure function.

use crate::model::{Edge, Node, NodeId, Position, ProcessGraph};
use rustc_hash::{FxHashMap, FxHashSet};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

// ═══════════════════════════════════════════════════════════════════════════
// KB-sourced port signatures (input to the validator)
// ═══════════════════════════════════════════════════════════════════════════

/// One input or output port on an operator.
///
/// Sourced from the Dorian KB (`get_operator_io` / `get_interface_io` in
/// `dorian/knowledge/queries.py`). The catalog can seed this at engine
/// startup; new-operator additions only need KB entries, not Rust edits.
///
/// Semantic identity
/// -----------------
/// Type alone (e.g. `Array`) is too coarse for ML-pipeline wiring:
/// `X_test` and `y_test` are both arrays but semantically swappable is a
/// bug, not a feature. Every port carries (optionally) a ``role`` tag
/// from a fixed vocabulary — ``"feature" | "target" | "model" |
/// "prediction" | "metric" | "split_indices"`` — and a ``split`` tag
/// (``"train" | "test" | "val"``). The compatibility check consults all
/// three (type, role, split), rejecting edges that fail any dimension.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortSig {
    /// Port name (numeric string for positional args — "0", "1"; word for
    /// keyword args — "n_estimators").
    pub name: String,
    /// KB-declared type (e.g. "Model", "DataFrame", "Array", "Prediction").
    /// `None` means "any" — legitimate for variadic container slots.
    #[serde(rename = "type")]
    pub port_type: Option<String>,
    /// Input ports can be required (must have an incoming edge) or
    /// optional (default value or variadic with zero-fan-in allowed).
    /// Ignored for output ports.
    #[serde(default)]
    pub required: bool,
    /// Variadic input ports accept 0..N edges at the same logical slot.
    /// Ignored for output ports.
    #[serde(default)]
    pub variadic: bool,
    /// Semantic role — narrows the type-match with intent. Fixed vocab:
    /// ``feature`` (X, X_train, X_test, X_transformed), ``target`` (y,
    /// y_train, y_test), ``prediction`` (y_pred, predictions), ``model``
    /// (model, estimator, classifier), ``metric`` (score). ``None``
    /// disables the role check for this port (backward-compat with
    /// older catalog entries).
    #[serde(default)]
    pub role: Option<String>,
    /// Split partition the port carries, when applicable. Only matters
    /// for ``feature`` and ``target`` roles — a ``fit`` expects
    /// ``split="train"`` on its data inputs; a ``predict`` expects
    /// ``split="test"``. ``None`` = split-agnostic (either is fine or
    /// split concept doesn't apply).
    #[serde(default)]
    pub split: Option<String>,
}

/// Full signature for one operator — its inputs and outputs.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OperatorSig {
    pub inputs: Vec<PortSig>,
    pub outputs: Vec<PortSig>,
}

/// Signature registry: operator name → signature. The validator consults
/// this for every operator in the DAG.
pub type SignatureRegistry = FxHashMap<String, OperatorSig>;

// ═══════════════════════════════════════════════════════════════════════════
// Validation errors — structured leaves for the exception registry
// ═══════════════════════════════════════════════════════════════════════════

/// Every variant corresponds to a bucket key in the exception registry.
/// Fields are the metadata the registry + UI + RL observation consume
/// directly — no parsing, no templating, no guessing.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "kind")]
pub enum ValidationError {
    /// An operator in the graph has a required input port with no
    /// incoming edge. This is the class of bug that surfaced as
    /// "DataFrame has no attribute chain 'predict'" at runtime — the
    /// agent committed a pipeline where `predict.0` (self-port, type
    /// Model) was unwired, and Dask built a zero-arg task that then
    /// blamed the wrong frame.
    UnwiredRequiredInput {
        node_id: NodeId,
        operator: String,
        port: String,
        expected_type: Option<String>,
    },

    /// An edge connects ports that are incompatible on at least one
    /// dimension — type, role, or split. Carries ``reasons`` to pinpoint
    /// which dimension(s) failed so the debugger's surgical fix targets
    /// the right column (e.g. role mismatch on accuracy_score's y_pred
    /// port ⇒ propose rerouting to a prediction-typed source, NOT just
    /// any Array).
    ///
    /// Prior single-dimension name ``TypeMismatch`` retained as the
    /// error ``kind`` for registry stability — adding dimensions didn't
    /// change the bucket, just enriched the evidence.
    TypeMismatch {
        source_node: NodeId,
        source_operator: String,
        source_port: String,
        source_type: Option<String>,
        source_role: Option<String>,
        source_split: Option<String>,
        destination_node: NodeId,
        destination_operator: String,
        destination_port: String,
        destination_type: Option<String>,
        destination_role: Option<String>,
        destination_split: Option<String>,
        /// Which of {"type", "role", "split"} failed. Empty means
        /// all passed (should never fire in that case).
        reasons: Vec<String>,
    },

    /// An edge references a destination port name that doesn't exist
    /// on the destination operator. Usually a stale rewrite or a
    /// hand-written DAG with a typo. Distinct from UnwiredRequiredInput
    /// because there IS an edge — it's just aimed at nothing.
    UnknownPort {
        node_id: NodeId,
        operator: String,
        port: String,
        available_ports: Vec<String>,
    },

    /// An edge's source operator is unknown to the KB (not in the
    /// signature registry). Likely a bogus operator name or a KB gap.
    UnknownOperator {
        node_id: NodeId,
        operator: String,
    },

    /// Cycle detected in the directed graph. Dask would hang forever;
    /// the RL mask `_would_create_cycle` prevents it from forming but
    /// manually-authored DAGs can slip through.
    CycleDetected { cycle_nodes: Vec<NodeId> },

    /// A non-root node is unreachable from any source (a node has no
    /// path from a sourceless operator like a loader). Usually means
    /// dangling subgraph from a partial delete.
    UnreachableFromSource {
        node_id: NodeId,
        operator: String,
    },

    /// No path from this (non-sink) node reaches the designated sink
    /// (metric). The node's output is effectively discarded.
    UnreachableToSink {
        node_id: NodeId,
        operator: String,
        sink_node: NodeId,
    },

    /// Two edges target the same non-variadic input port. The first one
    /// wins in Dask's graph, but this usually means a bug in rewriting.
    DuplicateEdgeAtPort {
        node_id: NodeId,
        operator: String,
        port: String,
        edge_count: usize,
    },

    /// An edge references a node that doesn't exist in `graph.nodes`.
    /// The source or destination (or both) point at a missing id. Gives
    /// the debugger a surgical pointer: delete this edge, OR add the
    /// referenced node back.
    DanglingEdge {
        source_node: NodeId,
        destination_node: NodeId,
        missing_side: DanglingEdgeSide,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum DanglingEdgeSide {
    Source,
    Destination,
    Both,
}

impl ValidationError {
    /// Stable variant name — this is the bucket key for the exception
    /// registry. Never changes across instances; forms the first-tier
    /// cluster for free.
    pub fn kind(&self) -> &'static str {
        match self {
            ValidationError::UnwiredRequiredInput { .. } => "UnwiredRequiredInput",
            ValidationError::TypeMismatch { .. } => "TypeMismatch",
            ValidationError::UnknownPort { .. } => "UnknownPort",
            ValidationError::UnknownOperator { .. } => "UnknownOperator",
            ValidationError::CycleDetected { .. } => "CycleDetected",
            ValidationError::UnreachableFromSource { .. } => "UnreachableFromSource",
            ValidationError::UnreachableToSink { .. } => "UnreachableToSink",
            ValidationError::DuplicateEdgeAtPort { .. } => "DuplicateEdgeAtPort",
            ValidationError::DanglingEdge { .. } => "DanglingEdge",
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// The validator itself
// ═══════════════════════════════════════════════════════════════════════════

/// Validate a pipeline DAG against a KB-sourced signature registry.
///
/// Returns `Ok(())` if the DAG is well-formed enough to execute, else
/// `Err(Vec<ValidationError>)` with ALL errors (collected, not
/// short-circuited, so the UI / RL agent can present the full list).
///
/// Sink discovery: if `sink_node_id` is `Some`, reachability-to-sink
/// is checked. Supply the metric operator's node id here from the RL
/// env's frozen harness; `None` skips this check.
pub fn validate_pipeline(
    graph: &ProcessGraph,
    registry: &SignatureRegistry,
    sink_node_id: Option<&NodeId>,
) -> Result<(), Vec<ValidationError>> {
    let mut errors: Vec<ValidationError> = Vec::new();

    // 0. Dangling edges — every structural check below assumes edges
    //    reference nodes that exist in the graph. Catch orphan refs first
    //    so later checks don't produce misleading cascade-errors.
    for edge in &graph.edges {
        let src_missing = !graph.nodes.contains_key(&edge.source);
        let dst_missing = !graph.nodes.contains_key(&edge.destination);
        if src_missing || dst_missing {
            let side = match (src_missing, dst_missing) {
                (true, true) => DanglingEdgeSide::Both,
                (true, false) => DanglingEdgeSide::Source,
                (false, true) => DanglingEdgeSide::Destination,
                (false, false) => unreachable!(),
            };
            errors.push(ValidationError::DanglingEdge {
                source_node: edge.source.clone(),
                destination_node: edge.destination.clone(),
                missing_side: side,
            });
        }
    }

    // 1. Operator presence in the registry
    for (nid, node) in &graph.nodes {
        if let Node::Operator(op) = node {
            if !registry.contains_key(&op.name) {
                errors.push(ValidationError::UnknownOperator {
                    node_id: nid.clone(),
                    operator: op.name.clone(),
                });
            }
        }
    }

    // 2. Edge-level checks: known port + type match + duplicate port usage
    let mut port_edge_counts: FxHashMap<(NodeId, String), usize> = FxHashMap::default();
    for edge in &graph.edges {
        let src_name = operator_name(graph, &edge.source);
        let dst_name = operator_name(graph, &edge.destination);

        let dst_port_name = position_to_port(&edge.position);
        *port_edge_counts
            .entry((edge.destination.clone(), dst_port_name.clone()))
            .or_insert(0) += 1;

        if let Some(dst_name) = dst_name.as_deref() {
            if let Some(dst_sig) = registry.get(dst_name) {
                match find_input(dst_sig, &dst_port_name) {
                    None => {
                        errors.push(ValidationError::UnknownPort {
                            node_id: edge.destination.clone(),
                            operator: dst_name.to_string(),
                            port: dst_port_name.clone(),
                            available_ports: dst_sig
                                .inputs
                                .iter()
                                .map(|p| p.name.clone())
                                .collect(),
                        });
                    }
                    Some(dst_port) => {
                        if let Some(src_name) = src_name.as_deref() {
                            if let Some(src_sig) = registry.get(src_name) {
                                let src_port_name =
                                    source_output_port(src_sig, &edge.output);
                                let src_port = src_sig
                                    .outputs
                                    .iter()
                                    .find(|p| p.name == src_port_name);
                                if let Some(src_port) = src_port {
                                    let reasons =
                                        port_incompat_reasons(src_port, dst_port);
                                    if !reasons.is_empty() {
                                        errors.push(ValidationError::TypeMismatch {
                                            source_node: edge.source.clone(),
                                            source_operator: src_name.to_string(),
                                            source_port: src_port.name.clone(),
                                            source_type: src_port.port_type.clone(),
                                            source_role: src_port.role.clone(),
                                            source_split: src_port.split.clone(),
                                            destination_node: edge.destination.clone(),
                                            destination_operator: dst_name.to_string(),
                                            destination_port: dst_port.name.clone(),
                                            destination_type: dst_port
                                                .port_type
                                                .clone(),
                                            destination_role: dst_port.role.clone(),
                                            destination_split: dst_port.split.clone(),
                                            reasons,
                                        });
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // Duplicate edges at the same non-variadic port.
    for ((dst_node, port), count) in &port_edge_counts {
        if *count > 1 {
            // Allow duplicates only if the destination port is variadic.
            let operator = operator_name(graph, dst_node).unwrap_or_default();
            let is_variadic = registry
                .get(&operator)
                .and_then(|sig| find_input(sig, port))
                .map(|p| p.variadic)
                .unwrap_or(false);
            if !is_variadic {
                errors.push(ValidationError::DuplicateEdgeAtPort {
                    node_id: dst_node.clone(),
                    operator,
                    port: port.clone(),
                    edge_count: *count,
                });
            }
        }
    }

    // 3. Required-port wiring
    let incoming_by_node: FxHashMap<&NodeId, FxHashSet<String>> =
        graph.edges.iter().fold(FxHashMap::default(), |mut acc, e| {
            acc.entry(&e.destination)
                .or_default()
                .insert(position_to_port(&e.position));
            acc
        });
    let empty_wired: FxHashSet<String> = FxHashSet::default();
    for (nid, node) in &graph.nodes {
        if let Node::Operator(op) = node {
            if let Some(sig) = registry.get(&op.name) {
                let wired: &FxHashSet<String> =
                    incoming_by_node.get(nid).unwrap_or(&empty_wired);
                for port in &sig.inputs {
                    if port.required && !wired.contains(&port.name) {
                        errors.push(ValidationError::UnwiredRequiredInput {
                            node_id: nid.clone(),
                            operator: op.name.clone(),
                            port: port.name.clone(),
                            expected_type: port.port_type.clone(),
                        });
                    }
                }
            }
        }
    }

    // 4. Cycle detection (Kahn's algorithm; if the queue fails to drain
    //    the residual is part of a cycle).
    if let Some(cycle) = detect_cycle(graph) {
        errors.push(ValidationError::CycleDetected {
            cycle_nodes: cycle,
        });
    }

    // 5. Reachability from source + to sink
    let sources: Vec<&NodeId> = graph
        .nodes
        .iter()
        .filter(|(_, node)| matches!(node, Node::Operator(_) | Node::Parameter(_)))
        .filter(|(nid, _)| !graph.edges.iter().any(|e| &e.destination == *nid))
        .map(|(nid, _)| nid)
        .collect();
    let reachable_from_sources = bfs_reachable_from(graph, &sources);
    for (nid, node) in &graph.nodes {
        if !matches!(node, Node::Operator(_)) {
            continue;
        }
        if !reachable_from_sources.contains(nid) {
            errors.push(ValidationError::UnreachableFromSource {
                node_id: nid.clone(),
                operator: operator_name(graph, nid).unwrap_or_default(),
            });
        }
    }

    if let Some(sink) = sink_node_id {
        let reachable_to_sink = bfs_reachable_to(graph, sink);
        for (nid, node) in &graph.nodes {
            if nid == sink {
                continue;
            }
            if !matches!(node, Node::Operator(_)) {
                continue;
            }
            if !reachable_to_sink.contains(nid) {
                errors.push(ValidationError::UnreachableToSink {
                    node_id: nid.clone(),
                    operator: operator_name(graph, nid).unwrap_or_default(),
                    sink_node: sink.clone(),
                });
            }
        }
    }

    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════════

fn operator_name(graph: &ProcessGraph, nid: &NodeId) -> Option<String> {
    match graph.nodes.get(nid)? {
        Node::Operator(op) => Some(op.name.clone()),
        _ => None,
    }
}

fn position_to_port(pos: &Position) -> String {
    match pos {
        Position::Index(i) => i.to_string(),
        Position::Keyword(k) => k.clone(),
    }
}

fn find_input<'a>(sig: &'a OperatorSig, port_name: &str) -> Option<&'a PortSig> {
    sig.inputs.iter().find(|p| p.name == port_name)
}

/// Map an edge's output index to the named output port. Falls back to
/// the index-as-string when the operator has no named outputs at that
/// position (rare — indicates a KB/signature gap).
fn source_output_port(sig: &OperatorSig, output: &Position) -> String {
    match output {
        Position::Index(i) => sig
            .outputs
            .get(*i as usize)
            .map(|p| p.name.clone())
            .unwrap_or_else(|| i.to_string()),
        Position::Keyword(k) => k.clone(),
    }
}

/// Types are compatible if: both unset (wildcards), exactly equal, or
/// either side is the literal `"any"`. Extension point for type
/// hierarchies (e.g. "Prediction" is-a "Array") — add subtype rules here.
fn types_compatible(src: &Option<String>, dst: &Option<String>) -> bool {
    match (src, dst) {
        (None, _) | (_, None) => true,
        (Some(s), Some(d)) => {
            if s == d {
                return true;
            }
            if s == "any" || d == "any" {
                return true;
            }
            // Prediction is a tightening of Array (see label_shortcut_guard).
            if s == "Prediction" && d == "Array" {
                return true;
            }
            false
        }
    }
}

/// Roles are compatible when equal, or when at least one side is
/// unset. The role vocabulary is closed by convention
/// ("feature"/"target"/"prediction"/"model"/"metric") but the validator
/// treats it as opaque strings — richer role hierarchies belong in the
/// KB, not here.
fn roles_compatible(src: &Option<String>, dst: &Option<String>) -> bool {
    match (src, dst) {
        (None, _) | (_, None) => true,
        (Some(s), Some(d)) => s == d,
    }
}

/// Splits are compatible when equal, or when at least one side is
/// unset (split-agnostic). Unlike roles, splits should rarely be
/// strict — most ops don't care. The strict case is ``fit`` wanting
/// train-split inputs; ``predict`` wanting test-split; metrics wanting
/// test-split on both sides.
fn splits_compatible(src: &Option<String>, dst: &Option<String>) -> bool {
    match (src, dst) {
        (None, _) | (_, None) => true,
        (Some(s), Some(d)) => s == d,
    }
}

/// Holistic port compatibility across all three dimensions. Returns the
/// list of failing dimensions (empty = fully compatible). Called at
/// edge-check time; the failures populate ``TypeMismatch.reasons``.
fn port_incompat_reasons(src: &PortSig, dst: &PortSig) -> Vec<String> {
    let mut reasons = Vec::new();
    if !types_compatible(&src.port_type, &dst.port_type) {
        reasons.push("type".to_string());
    }
    if !roles_compatible(&src.role, &dst.role) {
        reasons.push("role".to_string());
    }
    if !splits_compatible(&src.split, &dst.split) {
        reasons.push("split".to_string());
    }
    reasons
}

fn detect_cycle(graph: &ProcessGraph) -> Option<Vec<NodeId>> {
    let mut in_degree: FxHashMap<&NodeId, usize> =
        graph.nodes.keys().map(|k| (k, 0)).collect();
    for edge in &graph.edges {
        *in_degree.entry(&edge.destination).or_insert(0) += 1;
    }
    let mut queue: VecDeque<&NodeId> = in_degree
        .iter()
        .filter(|(_, d)| **d == 0)
        .map(|(k, _)| *k)
        .collect();
    let mut visited: usize = 0;
    while let Some(nid) = queue.pop_front() {
        visited += 1;
        for edge in graph.edges.iter().filter(|e| &e.source == nid) {
            let deg = in_degree.get_mut(&edge.destination).unwrap();
            *deg -= 1;
            if *deg == 0 {
                queue.push_back(&edge.destination);
            }
        }
    }
    if visited == graph.nodes.len() {
        None
    } else {
        let cycle: Vec<NodeId> = in_degree
            .iter()
            .filter(|(_, d)| **d > 0)
            .map(|(k, _)| (*k).clone())
            .collect();
        Some(cycle)
    }
}

fn bfs_reachable_from(graph: &ProcessGraph, sources: &[&NodeId]) -> FxHashSet<NodeId> {
    let mut reached: FxHashSet<NodeId> = sources.iter().map(|s| (*s).clone()).collect();
    let mut queue: VecDeque<NodeId> = sources.iter().map(|s| (*s).clone()).collect();
    while let Some(nid) = queue.pop_front() {
        for edge in graph.edges.iter().filter(|e| e.source == nid) {
            if reached.insert(edge.destination.clone()) {
                queue.push_back(edge.destination.clone());
            }
        }
    }
    reached
}

fn bfs_reachable_to(graph: &ProcessGraph, sink: &NodeId) -> FxHashSet<NodeId> {
    let mut reached: FxHashSet<NodeId> = FxHashSet::default();
    reached.insert(sink.clone());
    let mut queue: VecDeque<NodeId> = VecDeque::new();
    queue.push_back(sink.clone());
    while let Some(nid) = queue.pop_front() {
        for edge in graph.edges.iter().filter(|e| e.destination == nid) {
            if reached.insert(edge.source.clone()) {
                queue.push_back(edge.source.clone());
            }
        }
    }
    reached
}

// Silence unused-import warning on edge for the moment; Edge is reachable
// through ProcessGraph but the compiler doesn't always see the tie.
#[allow(dead_code)]
fn _noop(_: &Edge) {}

// ═══════════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{DeliveryMode, Edge, Node, Operator, Position, ProcessGraph};

    fn mk_op(name: &str) -> Node {
        Node::Operator(Operator {
            name: name.to_string(),
            language: "python".into(),
            tasks: vec![],
        })
    }

    fn registry_with(entries: &[(&str, OperatorSig)]) -> SignatureRegistry {
        let mut r = SignatureRegistry::default();
        for (k, v) in entries {
            r.insert((*k).to_string(), v.clone());
        }
        r
    }

    fn add_edge(g: &mut ProcessGraph, src: &str, dst: &str, position: Position, output: i64) {
        g.edges.push(Edge {
            source: src.into(),
            destination: dst.into(),
            position,
            output: Position::Index(output),
            delivery_mode: DeliveryMode::Once,
        });
    }

    #[test]
    fn unwired_required_input_flags_predict_without_model() {
        // RandomForestClassifier → predict is missing; predict port 0
        // (self) is required with type "Model".
        let mut g = ProcessGraph::new();
        g.add_node("rf".into(), mk_op("sklearn.ensemble.RandomForestClassifier"));
        g.add_node("pred".into(), mk_op("predict"));
        g.add_node("xtest".into(), mk_op("split.out"));
        add_edge(&mut g, "xtest", "pred", Position::Index(1), 0);

        let reg = registry_with(&[
            (
                "sklearn.ensemble.RandomForestClassifier",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "model".into(),
                        port_type: Some("Model".into()),
                        required: false,
                        variadic: false,
                        role: None,
                        split: None,
                    }],
                },
            ),
            (
                "predict",
                OperatorSig {
                    inputs: vec![
                        PortSig {
                            name: "0".into(),
                            port_type: Some("Model".into()),
                            required: true,
                            variadic: false,
                            role: None,
                            split: None,
                        },
                        PortSig {
                            name: "1".into(),
                            port_type: Some("DataFrame".into()),
                            required: true,
                            variadic: false,
                            role: None,
                            split: None,
                        },
                    ],
                    outputs: vec![
                        PortSig {
                            name: "model".into(),
                            port_type: Some("Model".into()),
                            required: false,
                            variadic: false,
                            role: None,
                            split: None,
                        },
                        PortSig {
                            name: "y_pred".into(),
                            port_type: Some("Prediction".into()),
                            required: false,
                            variadic: false,
                            role: None,
                            split: None,
                        },
                    ],
                },
            ),
            (
                "split.out",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "x".into(),
                        port_type: Some("DataFrame".into()),
                        required: false,
                        variadic: false,
                        role: None,
                        split: None,
                    }],
                },
            ),
        ]);

        let result = validate_pipeline(&g, &reg, None);
        let errs = result.expect_err("must report unwired predict port 0");
        let kinds: Vec<&str> = errs.iter().map(|e| e.kind()).collect();
        assert!(
            kinds.contains(&"UnwiredRequiredInput"),
            "expected UnwiredRequiredInput in {:?}",
            kinds
        );
    }

    #[test]
    fn type_mismatch_caught_on_dataframe_to_model_port() {
        // Wire a DataFrame source into a Model destination port.
        let mut g = ProcessGraph::new();
        g.add_node("scaler".into(), mk_op("scaler.transform"));
        g.add_node("pred".into(), mk_op("predict"));
        add_edge(&mut g, "scaler", "pred", Position::Index(0), 0);
        add_edge(&mut g, "scaler", "pred", Position::Index(1), 0);

        let reg = registry_with(&[
            (
                "scaler.transform",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "X".into(),
                        port_type: Some("DataFrame".into()),
                        required: false,
                        variadic: false,
                        role: None,
                        split: None,
                    }],
                },
            ),
            (
                "predict",
                OperatorSig {
                    inputs: vec![
                        PortSig {
                            name: "0".into(),
                            port_type: Some("Model".into()),
                            required: true,
                            variadic: false,
                            role: None,
                            split: None,
                        },
                        PortSig {
                            name: "1".into(),
                            port_type: Some("DataFrame".into()),
                            required: true,
                            variadic: false,
                            role: None,
                            split: None,
                        },
                    ],
                    outputs: vec![PortSig {
                        name: "y_pred".into(),
                        port_type: Some("Prediction".into()),
                        required: false,
                        variadic: false,
                        role: None,
                        split: None,
                    }],
                },
            ),
        ]);

        let errs = validate_pipeline(&g, &reg, None).unwrap_err();
        let kinds: Vec<&str> = errs.iter().map(|e| e.kind()).collect();
        assert!(
            kinds.contains(&"TypeMismatch"),
            "expected TypeMismatch in {:?}",
            kinds
        );
        // New: the error carries a reasons list with at least "type" in it.
        let tm = errs
            .iter()
            .find(|e| matches!(e, ValidationError::TypeMismatch { .. }))
            .expect("at least one TypeMismatch variant");
        if let ValidationError::TypeMismatch { reasons, .. } = tm {
            assert!(
                reasons.iter().any(|r| r == "type"),
                "expected 'type' in reasons {:?}",
                reasons
            );
        }
    }

    #[test]
    fn dangling_edge_caught_when_source_missing() {
        let mut g = ProcessGraph::new();
        g.add_node("real".into(), mk_op("foo"));
        add_edge(&mut g, "ghost", "real", Position::Index(0), 0);

        let reg = registry_with(&[(
            "foo",
            OperatorSig {
                inputs: vec![],
                outputs: vec![],
            },
        )]);

        let errs = validate_pipeline(&g, &reg, None).unwrap_err();
        assert!(errs
            .iter()
            .any(|e| matches!(e, ValidationError::DanglingEdge { missing_side: DanglingEdgeSide::Source, .. })));
    }

    #[test]
    fn unknown_operator_flagged() {
        let mut g = ProcessGraph::new();
        g.add_node("x".into(), mk_op("does.not.exist"));

        let reg = registry_with(&[]);
        let errs = validate_pipeline(&g, &reg, None).unwrap_err();
        assert!(errs.iter().any(|e| matches!(e, ValidationError::UnknownOperator { .. })));
    }

    #[test]
    fn prediction_is_compatible_with_array_per_label_shortcut_guard() {
        // Prediction → Array is allowed (tightening from label_shortcut_guard).
        // Exactly the path predict.y_pred (Prediction) → accuracy_score.1 (Array).
        assert!(types_compatible(
            &Some("Prediction".into()),
            &Some("Array".into())
        ));
        assert!(!types_compatible(
            &Some("DataFrame".into()),
            &Some("Model".into())
        ));
    }

    #[test]
    fn happy_path_no_errors() {
        // Minimal fit → predict chain with all types matching + required
        // ports wired.
        let mut g = ProcessGraph::new();
        g.add_node("rf".into(), mk_op("sklearn.ensemble.RandomForestClassifier"));
        g.add_node("fit".into(), mk_op("fit"));
        g.add_node("pred".into(), mk_op("predict"));
        g.add_node("xtrain".into(), mk_op("split.x_train"));
        g.add_node("ytrain".into(), mk_op("split.y_train"));
        g.add_node("xtest".into(), mk_op("split.x_test"));
        add_edge(&mut g, "rf", "fit", Position::Index(0), 0);
        add_edge(&mut g, "xtrain", "fit", Position::Index(1), 0);
        add_edge(&mut g, "ytrain", "fit", Position::Index(2), 0);
        add_edge(&mut g, "fit", "pred", Position::Index(0), 0);
        add_edge(&mut g, "xtest", "pred", Position::Index(1), 0);

        let model_sig = |required: bool| PortSig {
            name: "0".into(),
            port_type: Some("Model".into()),
            required,
            variadic: false,
            role: None,
            split: None,
        };
        let df_sig = |name: &str, required: bool| PortSig {
            name: name.into(),
            port_type: Some("DataFrame".into()),
            required,
            variadic: false,
            role: None,
            split: None,
        };
        let arr_sig = |name: &str, required: bool| PortSig {
            name: name.into(),
            port_type: Some("Array".into()),
            required,
            variadic: false,
            role: None,
            split: None,
        };

        let reg = registry_with(&[
            (
                "sklearn.ensemble.RandomForestClassifier",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "model".into(),
                        port_type: Some("Model".into()),
                        required: false,
                        variadic: false,
                        role: None,
                        split: None,
                    }],
                },
            ),
            (
                "fit",
                OperatorSig {
                    inputs: vec![
                        model_sig(true),
                        df_sig("1", true),
                        arr_sig("2", true),
                    ],
                    outputs: vec![PortSig {
                        name: "model".into(),
                        port_type: Some("Model".into()),
                        required: false,
                        variadic: false,
                        role: None,
                        split: None,
                    }],
                },
            ),
            (
                "predict",
                OperatorSig {
                    inputs: vec![model_sig(true), df_sig("1", true)],
                    outputs: vec![
                        PortSig {
                            name: "model".into(),
                            port_type: Some("Model".into()),
                            required: false,
                            variadic: false,
                            role: None,
                            split: None,
                        },
                        PortSig {
                            name: "y_pred".into(),
                            port_type: Some("Prediction".into()),
                            required: false,
                            variadic: false,
                            role: None,
                            split: None,
                        },
                    ],
                },
            ),
            (
                "split.x_train",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![df_sig("x", false)],
                },
            ),
            (
                "split.y_train",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![arr_sig("y", false)],
                },
            ),
            (
                "split.x_test",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![df_sig("x", false)],
                },
            ),
        ]);

        validate_pipeline(&g, &reg, None).expect("well-formed pipeline should validate");
    }

    #[test]
    fn role_mismatch_catches_x_y_swap() {
        // Two Array sources, both split="test", one feature one target.
        // fit wants (Model, feature-train, target-train). Here we feed
        // target into the feature port — types agree (both Array) but
        // roles disagree. Validator must flag.
        let mut g = ProcessGraph::new();
        g.add_node("xsrc".into(), mk_op("x_source"));
        g.add_node("ysrc".into(), mk_op("y_source"));
        g.add_node("fit".into(), mk_op("fit"));
        g.add_node("rf".into(), mk_op("rf"));
        // Swap: y → feature-port (1), x → target-port (2)
        add_edge(&mut g, "rf", "fit", Position::Index(0), 0);
        add_edge(&mut g, "ysrc", "fit", Position::Index(1), 0);
        add_edge(&mut g, "xsrc", "fit", Position::Index(2), 0);

        let reg = registry_with(&[
            (
                "rf",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "model".into(),
                        port_type: Some("Model".into()),
                        required: false,
                        variadic: false,
                        role: Some("model".into()),
                        split: None,
                    }],
                },
            ),
            (
                "x_source",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "x".into(),
                        port_type: Some("Array".into()),
                        required: false,
                        variadic: false,
                        role: Some("feature".into()),
                        split: Some("train".into()),
                    }],
                },
            ),
            (
                "y_source",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "y".into(),
                        port_type: Some("Array".into()),
                        required: false,
                        variadic: false,
                        role: Some("target".into()),
                        split: Some("train".into()),
                    }],
                },
            ),
            (
                "fit",
                OperatorSig {
                    inputs: vec![
                        PortSig {
                            name: "0".into(),
                            port_type: Some("Model".into()),
                            required: true,
                            variadic: false,
                            role: Some("model".into()),
                            split: None,
                        },
                        PortSig {
                            name: "1".into(),
                            port_type: Some("Array".into()),
                            required: true,
                            variadic: false,
                            role: Some("feature".into()),
                            split: Some("train".into()),
                        },
                        PortSig {
                            name: "2".into(),
                            port_type: Some("Array".into()),
                            required: true,
                            variadic: false,
                            role: Some("target".into()),
                            split: Some("train".into()),
                        },
                    ],
                    outputs: vec![],
                },
            ),
        ]);

        let errs = validate_pipeline(&g, &reg, None).unwrap_err();
        // Two TypeMismatches expected: y→feature-port AND x→target-port.
        let role_mismatches: Vec<_> = errs
            .iter()
            .filter_map(|e| match e {
                ValidationError::TypeMismatch { reasons, .. }
                    if reasons.iter().any(|r| r == "role") =>
                {
                    Some(e)
                }
                _ => None,
            })
            .collect();
        assert_eq!(
            role_mismatches.len(),
            2,
            "expected two role-mismatch errors, got {:?}",
            errs
        );
        // And "type" should NOT appear in those reasons — types agree
        // (Array → Array), only roles disagree.
        for e in &role_mismatches {
            if let ValidationError::TypeMismatch { reasons, .. } = e {
                assert!(!reasons.iter().any(|r| r == "type"));
            }
        }
    }

    #[test]
    fn split_mismatch_catches_accuracy_tautology() {
        // accuracy_score(y_test, y_test) — both inputs wire to the SAME
        // test-split target. Ports on accuracy want (target, prediction).
        // We emulate: y_test → y_true port (ok), y_test → y_pred port
        // (role mismatch: target→prediction). Complementary to the x/y
        // swap test.
        let mut g = ProcessGraph::new();
        g.add_node("ytest".into(), mk_op("y_test_source"));
        g.add_node("acc".into(), mk_op("accuracy_score"));
        add_edge(&mut g, "ytest", "acc", Position::Index(0), 0);
        add_edge(&mut g, "ytest", "acc", Position::Index(1), 0);

        let reg = registry_with(&[
            (
                "y_test_source",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "y".into(),
                        port_type: Some("Array".into()),
                        required: false,
                        variadic: false,
                        role: Some("target".into()),
                        split: Some("test".into()),
                    }],
                },
            ),
            (
                "accuracy_score",
                OperatorSig {
                    inputs: vec![
                        PortSig {
                            name: "0".into(),
                            port_type: Some("Array".into()),
                            required: true,
                            variadic: false,
                            role: Some("target".into()),
                            split: Some("test".into()),
                        },
                        PortSig {
                            name: "1".into(),
                            port_type: Some("Array".into()),
                            required: true,
                            variadic: false,
                            role: Some("prediction".into()),
                            split: Some("test".into()),
                        },
                    ],
                    outputs: vec![PortSig {
                        name: "score".into(),
                        port_type: Some("Metric".into()),
                        required: false,
                        variadic: false,
                        role: Some("metric".into()),
                        split: None,
                    }],
                },
            ),
        ]);

        let errs = validate_pipeline(&g, &reg, None).unwrap_err();
        // Position 0 (y_true): target→target, ok. Position 1 (y_pred):
        // target→prediction, role mismatch. Exactly one TypeMismatch.
        let tm: Vec<_> = errs
            .iter()
            .filter(|e| matches!(e, ValidationError::TypeMismatch { .. }))
            .collect();
        assert_eq!(tm.len(), 1, "expected 1 TypeMismatch, got {:?}", errs);
        if let ValidationError::TypeMismatch {
            destination_port,
            reasons,
            ..
        } = tm[0]
        {
            assert_eq!(destination_port, "1");
            assert!(reasons.iter().any(|r| r == "role"));
            assert!(!reasons.iter().any(|r| r == "type"));
        }
    }

    #[test]
    fn split_mismatch_catches_predict_on_train_data() {
        // predict.1 expects split="test"; we wire a split="train" source.
        // Role agrees (feature→feature), type agrees (Array→Array),
        // only split disagrees.
        let mut g = ProcessGraph::new();
        g.add_node("xtrain".into(), mk_op("x_train"));
        g.add_node("rf".into(), mk_op("rf"));
        g.add_node("pred".into(), mk_op("predict"));
        add_edge(&mut g, "rf", "pred", Position::Index(0), 0);
        add_edge(&mut g, "xtrain", "pred", Position::Index(1), 0);

        let reg = registry_with(&[
            (
                "rf",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "model".into(),
                        port_type: Some("Model".into()),
                        required: false,
                        variadic: false,
                        role: Some("model".into()),
                        split: None,
                    }],
                },
            ),
            (
                "x_train",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "x".into(),
                        port_type: Some("Array".into()),
                        required: false,
                        variadic: false,
                        role: Some("feature".into()),
                        split: Some("train".into()),
                    }],
                },
            ),
            (
                "predict",
                OperatorSig {
                    inputs: vec![
                        PortSig {
                            name: "0".into(),
                            port_type: Some("Model".into()),
                            required: true,
                            variadic: false,
                            role: Some("model".into()),
                            split: None,
                        },
                        PortSig {
                            name: "1".into(),
                            port_type: Some("Array".into()),
                            required: true,
                            variadic: false,
                            role: Some("feature".into()),
                            split: Some("test".into()),
                        },
                    ],
                    outputs: vec![],
                },
            ),
        ]);

        let errs = validate_pipeline(&g, &reg, None).unwrap_err();
        let tm: Vec<_> = errs
            .iter()
            .filter(|e| matches!(e, ValidationError::TypeMismatch { .. }))
            .collect();
        assert_eq!(tm.len(), 1);
        if let ValidationError::TypeMismatch { reasons, .. } = tm[0] {
            assert_eq!(reasons.len(), 1);
            assert_eq!(reasons[0], "split");
        }
    }

    #[test]
    fn unset_role_or_split_is_compatible_backward_compat() {
        // Older catalog entries have no role/split. Wiring them into
        // role-tagged ports must NOT flag — unset means "don't care".
        let mut g = ProcessGraph::new();
        g.add_node("legacy".into(), mk_op("legacy_op"));
        g.add_node("fit".into(), mk_op("fit"));
        g.add_node("rf".into(), mk_op("rf"));
        add_edge(&mut g, "rf", "fit", Position::Index(0), 0);
        add_edge(&mut g, "legacy", "fit", Position::Index(1), 0);
        add_edge(&mut g, "legacy", "fit", Position::Index(2), 0);

        let reg = registry_with(&[
            (
                "rf",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "model".into(),
                        port_type: Some("Model".into()),
                        required: false,
                        variadic: false,
                        role: None,
                        split: None,
                    }],
                },
            ),
            (
                "legacy_op",
                OperatorSig {
                    inputs: vec![],
                    outputs: vec![PortSig {
                        name: "out".into(),
                        port_type: Some("Array".into()),
                        required: false,
                        variadic: false,
                        role: None,  // legacy — no semantic tags
                        split: None,
                    }],
                },
            ),
            (
                "fit",
                OperatorSig {
                    inputs: vec![
                        PortSig {
                            name: "0".into(),
                            port_type: Some("Model".into()),
                            required: true,
                            variadic: false,
                            role: Some("model".into()),
                            split: None,
                        },
                        PortSig {
                            name: "1".into(),
                            port_type: Some("Array".into()),
                            required: true,
                            variadic: false,
                            role: Some("feature".into()),
                            split: Some("train".into()),
                        },
                        PortSig {
                            name: "2".into(),
                            port_type: Some("Array".into()),
                            required: true,
                            variadic: false,
                            role: Some("target".into()),
                            split: Some("train".into()),
                        },
                    ],
                    outputs: vec![],
                },
            ),
        ]);

        // Duplicate-edge-at-port will fire because both edges land on
        // distinct ports; that's fine. What we care about is: NO
        // TypeMismatch errors — backward-compat.
        match validate_pipeline(&g, &reg, None) {
            Ok(()) => (),
            Err(errs) => {
                let tm: Vec<_> = errs
                    .iter()
                    .filter(|e| matches!(e, ValidationError::TypeMismatch { .. }))
                    .collect();
                assert!(
                    tm.is_empty(),
                    "unset role/split must be treated as compatible; got {:?}",
                    tm
                );
            }
        }
    }
}
