//! Closed, data-only vocabulary for declarative graph rewrites.
//!
//! Design summary
//! ~~~~~~~~~~~~~~
//! Rewrite rules are **pure data** (JSON, stored in the KB). The
//! engine executes them via a generic evaluator. Neither the rule
//! format nor the evaluator carries Rust closures — that keeps
//! rules serialisable, diffable, inspectable, and free of the
//! hardcoded heuristics that the Python-side Apply functions
//! accumulated (feature-flow name matching, Parameter exclusion,
//! position coercion, KB I/O lookup all baked into individual
//! functions). Every one of those concerns moves to either the
//! KB (port role ↔ interface) or the primitive vocabulary
//! (Parameter exclusion = one-line NodeSelector).
//!
//! Scope of this slice
//! ~~~~~~~~~~~~~~~~~~~
//! This module captures **operator-level** primitives — the
//! minimum set that can express the Python ``_APPLY_REGISTRY``
//! entries (reroute_incoming, reroute_outgoing, replace_node,
//! insert_x_preprocessor, duplicate_data_kwarg) as pure data.
//!
//! The abstraction-lattice work described in the
//! architecture-sweep thread (``AbstractTask`` nodes, runtime
//! bindings, cost-driven lowering) is a follow-up: it needs a new
//! ``Node::AbstractTask`` variant, and introducing one variant
//! today would ripple through ~125 match-arms across 19 files.
//! The ``LatticeLevel`` / ``Runtime`` / ``InterfaceTag`` types are
//! defined here so KB entries authored now use the stable shape,
//! but nothing in this module yet unfolds abstract tasks — that's
//! a separate slice.
//!
//! The KB integration point is a single trait: ``RoleResolver``.
//! Role lookup (FeatureFlow / LabelFlow / Config / …) is the one
//! place the evaluator consults the KB; everything else is pure
//! graph state + rule data.

use rustc_hash::{FxHashMap, FxHashSet};
use serde::{Deserialize, Serialize};

use crate::model::{Edge, Group, Node, NodeId, Operator, Parameter, Position, ProcessGraph, Snippet};
use crate::model::ParamDtype;

// ---------------------------------------------------------------------------
// Abstraction lattice shapes (scaffolding for a later slice)
// ---------------------------------------------------------------------------

/// Position on the abstraction lattice: a node is either an abstract
/// task (``what``), a concrete operator (``how``), or a bound
/// invocation (``how + where``).
///
/// Stored here so KB entries authored today can already tag their
/// level; the evaluator doesn't yet unfold tasks, but carrying the
/// tag avoids schema churn later.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LatticeLevel {
    /// ``classify`` / ``encode-categorical`` / ``normalise-features``.
    /// Multiple operators can realise the same task.
    AbstractTask,
    /// ``sklearn.ensemble.RandomForestClassifier`` — a concrete
    /// operator, runtime not yet chosen.
    Operator,
    /// Operator + runtime binding — fully resolved.
    Binding,
}

/// Runtime a bound operator executes on. Enumerated because the
/// scheduler needs to reason about cross-runtime data handoffs.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(tag = "runtime", rename_all = "snake_case")]
pub enum Runtime {
    /// CPython — today's default, used for sklearn / pandas / guardrails.
    Python,
    /// Rust-native reimplementation (future: some sklearn ports exist).
    RustNative,
    /// WASM sandbox (future).
    Wasm,
    /// Remote gRPC service at *endpoint*.
    Grpc { endpoint: String },
}

/// Semantic role a port carries. Derived from KB facts (operator's
/// declared I/O type), not from port-name heuristics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PortRole {
    /// Feature-side data flow (X, X_train, X_test, X_transformed).
    FeatureFlow,
    /// Label-side data flow (y, y_train, y_test, y_pred).
    LabelFlow,
    /// Model / fitted-estimator channel.
    ModelFlow,
    /// Hyperparameter / config slot (wired from a Parameter node).
    Config,
    /// Session state / env / runtime metadata.
    Context,
    /// KB has no declared role for this port.
    Unknown,
}

// ---------------------------------------------------------------------------
// Node / edge selectors — declarative predicates
// ---------------------------------------------------------------------------

/// Identifies a node by one of several declarative strategies.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "sel", rename_all = "snake_case")]
pub enum NodeSelector {
    /// Exact node ID — typed out by the rule author.
    Id { id: NodeId },
    /// Resolved via the rule's pattern-match mapping.
    FromMapping { key: String },
    /// Payload kind (Operator / Parameter / Snippet / Group).
    PayloadKind { payload: PayloadKind },
    /// Intersection of multiple selectors — all must match.
    All { of: Vec<NodeSelector> },
    /// Union — any may match.
    Any { of: Vec<NodeSelector> },
    /// Negation.
    Not { inner: Box<NodeSelector> },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PayloadKind {
    Operator,
    Parameter,
    Snippet,
    Group,
}

/// Predicate over edges. Every field is optional — an empty
/// selector matches every edge. Fields combine conjunctively.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct EdgeSelector {
    pub source: Option<NodeSelector>,
    pub destination: Option<NodeSelector>,
    pub position: Option<PositionPredicate>,
    /// Destination-side port role, resolved via ``RoleResolver``.
    pub destination_role: Option<PortRole>,
    /// Source-side port role (for multi-output producers).
    pub source_role: Option<PortRole>,
    pub source_output: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "pred", rename_all = "snake_case")]
pub enum PositionPredicate {
    IndexEq { i: i64 },
    KeywordEq { k: String },
    AnyIndex,
    AnyKeyword,
    OneOf { positions: Vec<Position> },
}

// ---------------------------------------------------------------------------
// Primitive ops — the closed vocabulary
// ---------------------------------------------------------------------------

/// Declarative description of a new node's payload.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "payload", rename_all = "snake_case")]
pub enum NodePayloadSpec {
    Operator { name: String, language: String },
    Parameter { name: String, dtype: String, value: String },
    Snippet { name: String, code: String, language: String },
}

/// A single declarative graph edit. Rules are ordered lists of these.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum PrimitiveOp {
    /// Add a new node. ``id`` is its concrete ID in the graph;
    /// ``bind`` optionally binds that same ID to a name in the
    /// rule mapping so later primitives can reference it via
    /// ``NodeSelector::FromMapping``.
    AddNode {
        id: NodeId,
        bind: Option<String>,
        payload: NodePayloadSpec,
    },

    /// Add a directed edge. The ``source`` and ``destination``
    /// selectors must resolve to **exactly one** node each; the
    /// evaluator refuses ambiguous selectors for AddEdge.
    AddEdge {
        source: NodeSelector,
        destination: NodeSelector,
        #[serde(default)]
        position: Position,
        #[serde(default)]
        output: Position,
    },

    /// Delete every node matching ``selector`` (plus their incident edges).
    DeleteNode { selector: NodeSelector },

    /// Delete every edge matching ``selector``.
    DeleteEdges { selector: EdgeSelector },

    /// Replace matching nodes' payload in place (keeps the ID and
    /// all incident edges intact). Common use: swap one operator
    /// FQN for another without rewiring.
    SetNodePayload {
        selector: NodeSelector,
        payload: NodePayloadSpec,
    },

    /// Rewire every edge matching ``selector`` to flow **through**
    /// the resolved ``through`` node. ``X → target`` becomes
    /// ``X → through → target`` for each intercepted edge.
    ///
    /// Replaces Python's ``reroute_incoming`` / ``reroute_outgoing``:
    /// the feature-flow vs label-flow distinction is expressed in
    /// the selector's ``destination_role`` / ``source_role`` fields
    /// (consulted via ``RoleResolver``), not hardcoded in a function
    /// body.
    RerouteEdges {
        selector: EdgeSelector,
        /// Must resolve to exactly one node.
        through: NodeSelector,
    },

    /// Lower an abstract-task node to a concrete operator. Abstract
    /// tasks are encoded as ``Operator { name: "task:<role>" }``
    /// per convention (lightweight alternative to a full
    /// ``Node::AbstractTask`` variant that would ripple across
    /// every match-arm in the engine). ``realisations`` is the
    /// ordered list of operator FQNs eligible to realise the task;
    /// the evaluator picks the first candidate whose KB-declared
    /// interface satisfies the constraints. First-fit chosen here
    /// because the cost-model / data-profile-aware selection is the
    /// optimiser's job — this primitive is the substitution, not
    /// the policy.
    LowerTask {
        selector: NodeSelector,
        realisations: Vec<OperatorRealisation>,
    },
}

/// One candidate realisation of an abstract task.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OperatorRealisation {
    pub fqn: String,
    #[serde(default = "default_python_language")]
    pub language: String,
}

fn default_python_language() -> String {
    "python".to_string()
}

/// Prefix convention — an ``Operator`` whose ``name`` begins with
/// this string is an abstract-task placeholder waiting for a
/// ``LowerTask`` primitive.
pub const TASK_PREFIX: &str = "task:";

// ---------------------------------------------------------------------------
// Role resolver — the single KB integration point
// ---------------------------------------------------------------------------

/// Resolve the semantic role of a port. Every role lookup the
/// evaluator does goes through this trait. Production will bind it
/// to a KB-backed resolver; tests bind a ``StaticRoleResolver``.
pub trait RoleResolver: Send + Sync {
    /// Role of a destination port: op ``destination_op`` receiving
    /// an edge at ``position``.
    fn destination_role(&self, destination_op: &str, position: &Position) -> PortRole;

    /// Role of a source port: op ``source_op`` producing output slot ``output``.
    fn source_role(&self, source_op: &str, output: i64) -> PortRole;
}

/// Fixed-map role resolver for tests and small deployments.
#[derive(Debug, Default)]
pub struct StaticRoleResolver {
    pub destination_roles: FxHashMap<(String, Position), PortRole>,
    pub source_roles: FxHashMap<(String, i64), PortRole>,
}

impl RoleResolver for StaticRoleResolver {
    fn destination_role(&self, op: &str, position: &Position) -> PortRole {
        self.destination_roles
            .get(&(op.to_string(), position.clone()))
            .copied()
            .unwrap_or(PortRole::Unknown)
    }
    fn source_role(&self, op: &str, output: i64) -> PortRole {
        self.source_roles
            .get(&(op.to_string(), output))
            .copied()
            .unwrap_or(PortRole::Unknown)
    }
}

/// Name-prefix heuristic resolver. Mirrors the Python fallback in
/// ``dorian/pipeline/mitigation_rewrites.py::_port_role_for_position``:
/// keyword positions starting with ``X`` (or named ``features``) are
/// FeatureFlow, ``y`` (or ``labels``/``target``) are LabelFlow,
/// ``model``/``estimator`` are ModelFlow. Index positions and unknown
/// keywords fall through to ``Unknown``. Used when no KB is bound; the
/// production resolver should consult Neo4j and fall back to this.
#[derive(Debug, Default)]
pub struct HeuristicRoleResolver;

impl RoleResolver for HeuristicRoleResolver {
    fn destination_role(&self, _op: &str, position: &Position) -> PortRole {
        if let Position::Keyword(s) = position {
            let lower = s.to_ascii_lowercase();
            if lower.starts_with('x') || lower == "features" {
                return PortRole::FeatureFlow;
            }
            if lower.starts_with('y') || lower == "labels" || lower == "target" {
                return PortRole::LabelFlow;
            }
            if lower == "model" || lower == "estimator" {
                return PortRole::ModelFlow;
            }
        }
        PortRole::Unknown
    }

    fn source_role(&self, _op: &str, _output: i64) -> PortRole {
        PortRole::Unknown
    }
}

// ---------------------------------------------------------------------------
// Mapping — same contract as ``rewrite::Mapping`` (pattern var → NodeId).
// ---------------------------------------------------------------------------

pub type Mapping = FxHashMap<String, NodeId>;

// ---------------------------------------------------------------------------
// Selector evaluation
// ---------------------------------------------------------------------------

impl NodeSelector {
    pub fn matches(&self, node_id: &str, node: &Node, mapping: &Mapping) -> bool {
        match self {
            NodeSelector::Id { id } => id == node_id,
            NodeSelector::FromMapping { key } => {
                mapping.get(key).map(|v| v == node_id).unwrap_or(false)
            }
            NodeSelector::PayloadKind { payload } => payload_kind_matches(*payload, node),
            NodeSelector::All { of } => of.iter().all(|s| s.matches(node_id, node, mapping)),
            NodeSelector::Any { of } => of.iter().any(|s| s.matches(node_id, node, mapping)),
            NodeSelector::Not { inner } => !inner.matches(node_id, node, mapping),
        }
    }

    /// Resolve to the set of node IDs in *graph* that match.
    pub fn resolve(&self, graph: &ProcessGraph, mapping: &Mapping) -> Vec<NodeId> {
        let mut out = Vec::new();
        for (id, node) in graph.nodes.iter() {
            if self.matches(id, node, mapping) {
                out.push(id.clone());
            }
        }
        out
    }
}

fn payload_kind_matches(kind: PayloadKind, node: &Node) -> bool {
    match (kind, node) {
        (PayloadKind::Operator, Node::Operator(_)) => true,
        (PayloadKind::Parameter, Node::Parameter(_)) => true,
        (PayloadKind::Snippet, Node::Snippet(_)) => true,
        (PayloadKind::Group, Node::Group(_)) => true,
        _ => false,
    }
}

impl PositionPredicate {
    pub fn matches(&self, pos: &Position) -> bool {
        match (self, pos) {
            (PositionPredicate::IndexEq { i }, Position::Index(j)) => i == j,
            (PositionPredicate::KeywordEq { k }, Position::Keyword(s)) => k == s,
            (PositionPredicate::AnyIndex, Position::Index(_)) => true,
            (PositionPredicate::AnyKeyword, Position::Keyword(_)) => true,
            (PositionPredicate::OneOf { positions }, p) => positions.iter().any(|q| q == p),
            _ => false,
        }
    }
}

impl EdgeSelector {
    pub fn matches_edge(
        &self,
        edge: &Edge,
        graph: &ProcessGraph,
        mapping: &Mapping,
        roles: &dyn RoleResolver,
    ) -> bool {
        // source / destination predicates
        if let Some(sel) = &self.source {
            let src = match graph.nodes.get(&edge.source) {
                Some(n) => n,
                None => return false,
            };
            if !sel.matches(&edge.source, src, mapping) {
                return false;
            }
        }
        if let Some(sel) = &self.destination {
            let dst = match graph.nodes.get(&edge.destination) {
                Some(n) => n,
                None => return false,
            };
            if !sel.matches(&edge.destination, dst, mapping) {
                return false;
            }
        }
        if let Some(pos) = &self.position {
            if !pos.matches(&edge.position) {
                return false;
            }
        }
        if let Some(src_out) = self.source_output {
            if edge.output_index() != src_out {
                return false;
            }
        }
        if let Some(expected) = self.destination_role {
            if let Some(Node::Operator(op)) = graph.nodes.get(&edge.destination) {
                if roles.destination_role(&op.name, &edge.position) != expected {
                    return false;
                }
            } else {
                return false;
            }
        }
        if let Some(expected) = self.source_role {
            if let Some(Node::Operator(op)) = graph.nodes.get(&edge.source) {
                if roles.source_role(&op.name, edge.output_index()) != expected {
                    return false;
                }
            } else {
                return false;
            }
        }
        true
    }
}

// ---------------------------------------------------------------------------
// Evaluator — executes a rule's primitive-op list on a ProcessGraph
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
pub enum EvalError {
    #[error("AddEdge requires exactly one source match, got {0}")]
    AmbiguousSource(usize),
    #[error("AddEdge requires exactly one destination match, got {0}")]
    AmbiguousDestination(usize),
    #[error("RerouteEdges requires exactly one ``through`` match, got {0}")]
    AmbiguousThrough(usize),
    #[error("unsupported payload kind Group — not expressible as an AddNode")]
    UnsupportedPayload,
}

/// Execute a list of primitive operations against *graph*.
///
/// The mapping is threaded through — primitives with ``bind: Some(_)``
/// add to it; later primitives read via ``FromMapping``. Matches the
/// ``rewrite::sync_apply`` contract.
pub fn apply_ops(
    graph: &mut ProcessGraph,
    ops: &[PrimitiveOp],
    mapping: &mut Mapping,
    roles: &dyn RoleResolver,
) -> Result<(), EvalError> {
    for op in ops {
        apply_one(graph, op, mapping, roles)?;
    }
    Ok(())
}

fn apply_one(
    graph: &mut ProcessGraph,
    op: &PrimitiveOp,
    mapping: &mut Mapping,
    roles: &dyn RoleResolver,
) -> Result<(), EvalError> {
    match op {
        PrimitiveOp::AddNode { id, bind, payload } => {
            graph.add_node(id.clone(), payload_spec_to_node(payload)?);
            if let Some(name) = bind {
                mapping.insert(name.clone(), id.clone());
            }
            Ok(())
        }
        PrimitiveOp::AddEdge { source, destination, position, output } => {
            let srcs = source.resolve(graph, mapping);
            if srcs.len() != 1 {
                return Err(EvalError::AmbiguousSource(srcs.len()));
            }
            let dsts = destination.resolve(graph, mapping);
            if dsts.len() != 1 {
                return Err(EvalError::AmbiguousDestination(dsts.len()));
            }
            graph.add_edge(Edge {
                source: srcs.into_iter().next().unwrap(),
                destination: dsts.into_iter().next().unwrap(),
                position: position.clone(),
                output: output.clone(),
                delivery_mode: Default::default(),
            });
            Ok(())
        }
        PrimitiveOp::DeleteNode { selector } => {
            let ids: FxHashSet<NodeId> = selector
                .resolve(graph, mapping)
                .into_iter()
                .collect();
            graph.nodes.retain(|id, _| !ids.contains(id));
            graph.edges.retain(|e| !ids.contains(&e.source) && !ids.contains(&e.destination));
            Ok(())
        }
        PrimitiveOp::DeleteEdges { selector } => {
            let keep: Vec<Edge> = graph
                .edges
                .iter()
                .filter(|e| !selector.matches_edge(e, graph, mapping, roles))
                .cloned()
                .collect();
            graph.edges = keep;
            Ok(())
        }
        PrimitiveOp::SetNodePayload { selector, payload } => {
            let ids = selector.resolve(graph, mapping);
            let new_node = payload_spec_to_node(payload)?;
            for id in ids {
                graph.nodes.insert(id, new_node.clone());
            }
            Ok(())
        }
        PrimitiveOp::LowerTask { selector, realisations } => {
            if realisations.is_empty() {
                return Ok(());
            }
            let chosen = &realisations[0];
            let ids = selector.resolve(graph, mapping);
            for id in ids {
                // Preserve the node's identity + incident edges;
                // only the operator FQN changes. The edge-role
                // semantics are unaffected because Role-aware
                // selectors read the KB by operator FQN at apply
                // time, not at lowering time.
                graph.nodes.insert(
                    id,
                    Node::Operator(Operator {
                        name: chosen.fqn.clone(),
                        language: chosen.language.clone(),
                        tasks: vec![],
                    }),
                );
            }
            Ok(())
        }
        PrimitiveOp::RerouteEdges { selector, through } => {
            let throughs = through.resolve(graph, mapping);
            if throughs.len() != 1 {
                return Err(EvalError::AmbiguousThrough(throughs.len()));
            }
            let through_id = throughs.into_iter().next().unwrap();

            // Partition edges without holding a mutable borrow on
            // graph.edges while we check predicates (which need to
            // look up endpoint nodes via graph.nodes). Indices first,
            // then split.
            let intercept_indices: Vec<usize> = graph
                .edges
                .iter()
                .enumerate()
                .filter(|(_, e)| {
                    selector.matches_edge(e, graph, mapping, roles)
                        && e.source != through_id
                        && e.destination != through_id
                })
                .map(|(i, _)| i)
                .collect();
            let intercept_set: FxHashSet<usize> = intercept_indices.iter().copied().collect();
            let mut intercepted: Vec<Edge> = Vec::with_capacity(intercept_indices.len());
            let mut kept: Vec<Edge> = Vec::with_capacity(graph.edges.len() - intercept_indices.len());
            for (i, e) in std::mem::take(&mut graph.edges).into_iter().enumerate() {
                if intercept_set.contains(&i) {
                    intercepted.push(e);
                } else {
                    kept.push(e);
                }
            }
            graph.edges = kept;

            // Idempotency: the surrounding ``Add`` transformation in
            // a migrated rewrite typically wires *one* half of the
            // bridge (src→through OR through→dst) at the
            // through-operator's intended port. The pre-migration
            // ``_make_reroute_outgoing`` / ``_make_reroute_incoming``
            // emitted only the missing half so each data flow carried
            // a single port name end-to-end. Mirror that contract:
            // skip emitting a bridge edge when *any* path already
            // exists between the endpoints — Add's wiring wins.
            let has_path = |edges: &[Edge], src: &str, dst: &str| -> bool {
                edges.iter().any(|e| e.source == src && e.destination == dst)
            };
            for e in intercepted {
                if !has_path(&graph.edges, &e.source, &through_id) {
                    graph.add_edge(Edge {
                        source: e.source.clone(),
                        destination: through_id.clone(),
                        position: e.position.clone(),
                        output: e.output.clone(),
                        delivery_mode: Default::default(),
                    });
                }
                if !has_path(&graph.edges, &through_id, &e.destination) {
                    graph.add_edge(Edge {
                        source: through_id.clone(),
                        destination: e.destination,
                        position: e.position,
                        output: Position::Index(0),
                        delivery_mode: Default::default(),
                    });
                }
            }
            Ok(())
        }
    }
}

fn payload_spec_to_node(spec: &NodePayloadSpec) -> Result<Node, EvalError> {
    match spec {
        NodePayloadSpec::Operator { name, language } => Ok(Node::Operator(Operator {
            name: name.clone(),
            language: language.clone(),
            tasks: vec![],
        })),
        NodePayloadSpec::Parameter { name, dtype, value } => {
            let dtype = match dtype.as_str() {
                "int" => ParamDtype::Int,
                "float" => ParamDtype::Float,
                "string" | "str" => ParamDtype::String,
                "eval" => ParamDtype::Eval,
                "env" => ParamDtype::Env,
                _ => ParamDtype::String,
            };
            Ok(Node::Parameter(Parameter {
                name: name.clone(),
                dtype,
                value: value.clone(),
            }))
        }
        NodePayloadSpec::Snippet { name, code, language } => Ok(Node::Snippet(Snippet {
            name: name.clone(),
            code: code.clone(),
            language: language.clone(),
        })),
    }
}

// Force the Group import to stay live for future slices.
#[allow(dead_code)]
fn _ensure_group_imported(_: Group) {}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn simple_fit_graph() -> ProcessGraph {
        // X_source → fit; y_source → fit
        // (X_source and y_source are regular operators representing
        // upstream transforms; we attach port roles via the role
        // resolver below so the selector can pick just the X edge.)
        let mut g = ProcessGraph::new();
        g.add_node(
            "X_source".into(),
            Node::Operator(Operator {
                name: "upstream.X".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_node(
            "y_source".into(),
            Node::Operator(Operator {
                name: "upstream.y".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_node(
            "fit".into(),
            Node::Operator(Operator {
                name: "sklearn.ensemble.RandomForestClassifier.fit".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_edge(Edge {
            source: "X_source".into(),
            destination: "fit".into(),
            position: Position::Index(1),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });
        g.add_edge(Edge {
            source: "y_source".into(),
            destination: "fit".into(),
            position: Position::Index(2),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });
        g
    }

    fn role_resolver_rf() -> StaticRoleResolver {
        let mut r = StaticRoleResolver::default();
        r.destination_roles.insert(
            (
                "sklearn.ensemble.RandomForestClassifier.fit".into(),
                Position::Index(1),
            ),
            PortRole::FeatureFlow,
        );
        r.destination_roles.insert(
            (
                "sklearn.ensemble.RandomForestClassifier.fit".into(),
                Position::Index(2),
            ),
            PortRole::LabelFlow,
        );
        r
    }

    #[test]
    fn reroute_incoming_as_primitives_feature_only() {
        // Reproduces the Python ``_make_reroute_incoming`` default
        // (non-anchor) behaviour: intercept feature-flow edges
        // landing on the target, route them through a new scaler,
        // leave label-flow edges alone.
        let mut g = simple_fit_graph();
        let roles = role_resolver_rf();
        let mut mapping = Mapping::default();

        let ops = vec![
            PrimitiveOp::AddNode {
                id: "scaler".into(),
                bind: Some("through".into()),
                payload: NodePayloadSpec::Operator {
                    name: "sklearn.preprocessing.StandardScaler".into(),
                    language: "python".into(),
                },
            },
            PrimitiveOp::RerouteEdges {
                selector: EdgeSelector {
                    destination: Some(NodeSelector::Id { id: "fit".into() }),
                    destination_role: Some(PortRole::FeatureFlow),
                    source: Some(NodeSelector::Not {
                        inner: Box::new(NodeSelector::PayloadKind {
                            payload: PayloadKind::Parameter,
                        }),
                    }),
                    ..Default::default()
                },
                through: NodeSelector::FromMapping { key: "through".into() },
            },
        ];

        apply_ops(&mut g, &ops, &mut mapping, &roles).unwrap();

        // Expected edges: X_source → scaler (pos 0), scaler → fit
        // (pos 1), y_source → fit (pos 2 unchanged).
        assert_eq!(g.edges.len(), 3);
        let mut shapes: Vec<(String, String, Position)> = g
            .edges
            .iter()
            .map(|e| (e.source.clone(), e.destination.clone(), e.position.clone()))
            .collect();
        shapes.sort_by(|a, b| format!("{:?}", a).cmp(&format!("{:?}", b)));

        // src→through preserves the original edge's position so each
        // data flow carries one port name end-to-end. This matches
        // ``_make_reroute_incoming`` in dorian/pipeline/mitigation_rewrites.py.
        assert!(
            shapes.contains(&(
                "X_source".into(),
                "scaler".into(),
                Position::Index(1),
            )),
            "missing X_source → scaler @ pos 1 (got {:?})",
            shapes,
        );
        assert!(
            shapes.contains(&(
                "scaler".into(),
                "fit".into(),
                Position::Index(1),
            )),
            "missing scaler → fit @ pos 1"
        );
        assert!(
            shapes.contains(&(
                "y_source".into(),
                "fit".into(),
                Position::Index(2),
            )),
            "label-flow edge should be untouched"
        );
    }

    #[test]
    fn ops_roundtrip_json() {
        let ops = vec![
            PrimitiveOp::AddNode {
                id: "scaler".into(),
                bind: Some("through".into()),
                payload: NodePayloadSpec::Operator {
                    name: "sklearn.preprocessing.StandardScaler".into(),
                    language: "python".into(),
                },
            },
            PrimitiveOp::RerouteEdges {
                selector: EdgeSelector {
                    destination: Some(NodeSelector::Id { id: "fit".into() }),
                    destination_role: Some(PortRole::FeatureFlow),
                    ..Default::default()
                },
                through: NodeSelector::FromMapping { key: "through".into() },
            },
        ];
        let json = serde_json::to_string(&ops).unwrap();
        let back: Vec<PrimitiveOp> = serde_json::from_str(&json).unwrap();
        let json2 = serde_json::to_string(&back).unwrap();
        assert_eq!(json, json2);
    }

    #[test]
    fn lattice_level_roundtrip() {
        for lvl in [
            LatticeLevel::AbstractTask,
            LatticeLevel::Operator,
            LatticeLevel::Binding,
        ] {
            let s = serde_json::to_string(&lvl).unwrap();
            let back: LatticeLevel = serde_json::from_str(&s).unwrap();
            assert_eq!(lvl, back);
        }
    }

    #[test]
    fn delete_edges_with_role_filter() {
        let mut g = simple_fit_graph();
        let roles = role_resolver_rf();
        let mut mapping = Mapping::default();

        // Delete every LabelFlow edge landing on fit.
        let ops = vec![PrimitiveOp::DeleteEdges {
            selector: EdgeSelector {
                destination: Some(NodeSelector::Id { id: "fit".into() }),
                destination_role: Some(PortRole::LabelFlow),
                ..Default::default()
            },
        }];
        apply_ops(&mut g, &ops, &mut mapping, &roles).unwrap();
        assert_eq!(g.edges.len(), 1);
        assert_eq!(g.edges[0].source, "X_source");
    }

    #[test]
    fn set_node_payload_swaps_operator_in_place() {
        let mut g = simple_fit_graph();
        let roles = StaticRoleResolver::default();
        let mut mapping = Mapping::default();

        let ops = vec![PrimitiveOp::SetNodePayload {
            selector: NodeSelector::Id { id: "fit".into() },
            payload: NodePayloadSpec::Operator {
                name: "sklearn.ensemble.GradientBoostingClassifier.fit".into(),
                language: "python".into(),
            },
        }];
        apply_ops(&mut g, &ops, &mut mapping, &roles).unwrap();
        match g.nodes.get("fit").unwrap() {
            Node::Operator(op) => assert_eq!(
                op.name,
                "sklearn.ensemble.GradientBoostingClassifier.fit"
            ),
            _ => panic!("expected operator"),
        }
        assert_eq!(g.edges.len(), 2); // edges preserved
    }

    #[test]
    fn lower_task_replaces_abstract_with_first_realisation() {
        // Graph with an abstract-task node wired to a fit slot.
        let mut g = ProcessGraph::new();
        g.add_node(
            "classify_task".into(),
            Node::Operator(Operator {
                name: format!("{}classify", TASK_PREFIX),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_node(
            "dataset".into(),
            Node::Operator(Operator {
                name: "dorian.io.dataset".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_edge(Edge {
            source: "dataset".into(),
            destination: "classify_task".into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });

        let roles = StaticRoleResolver::default();
        let mut mapping = Mapping::default();
        let ops = vec![PrimitiveOp::LowerTask {
            selector: NodeSelector::Id { id: "classify_task".into() },
            realisations: vec![
                OperatorRealisation {
                    fqn: "sklearn.ensemble.RandomForestClassifier".into(),
                    language: "python".into(),
                },
                OperatorRealisation {
                    fqn: "sklearn.linear_model.LogisticRegression".into(),
                    language: "python".into(),
                },
            ],
        }];
        apply_ops(&mut g, &ops, &mut mapping, &roles).unwrap();

        match g.nodes.get("classify_task").unwrap() {
            Node::Operator(op) => assert_eq!(
                op.name, "sklearn.ensemble.RandomForestClassifier",
            ),
            _ => panic!("expected operator"),
        }
        // Incident edge preserved.
        assert_eq!(g.edges.len(), 1);
        assert_eq!(g.edges[0].source, "dataset");
        assert_eq!(g.edges[0].destination, "classify_task");
    }

    #[test]
    fn lower_task_noop_on_empty_realisations() {
        let mut g = ProcessGraph::new();
        g.add_node(
            "task".into(),
            Node::Operator(Operator {
                name: format!("{}encode", TASK_PREFIX),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        let roles = StaticRoleResolver::default();
        let mut mapping = Mapping::default();
        let ops = vec![PrimitiveOp::LowerTask {
            selector: NodeSelector::Id { id: "task".into() },
            realisations: vec![],
        }];
        apply_ops(&mut g, &ops, &mut mapping, &roles).unwrap();
        // Node untouched — no realisation means caller hasn't
        // declared candidates; primitive refuses to guess.
        match g.nodes.get("task").unwrap() {
            Node::Operator(op) => assert!(op.name.starts_with(TASK_PREFIX)),
            _ => panic!("expected operator"),
        }
    }
}
