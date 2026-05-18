//! ProcessGraph model — Rust equivalent of `dorian/dag.py`.
//!
//! This module provides the core data types for port-typed process graphs:
//! - `Operator`, `Snippet`, `Parameter` — node payloads
//! - `ActivationMode` — how a node activates (Transform, Reactive, Service, Router)
//! - `DeliveryMode` — channel semantics on edges (Once, Stream, Mailbox)
//! - `Edge` — with position coercion (int or keyword string)
//! - `Group` — collapsed compound operator sub-DAGs
//! - `ProcessGraph` — the top-level graph (supports cycles in Phase 6)
//!
//! All types support JSON serialization compatible with the docstore
//! document format used by the Python codebase.

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// UUID type alias (matches Python `dorian.types.UUID`).
pub type NodeId = String;

// ---------------------------------------------------------------------------
// Activation modes and delivery semantics (from architecture vision §2)
// ---------------------------------------------------------------------------

/// How a node activates when inputs arrive.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActivationMode {
    /// Stateless, fires once when all inputs ready (sklearn, pandas, etc.)
    #[default]
    Transform,
    /// Stateful, fires on each incoming message (LLM agent, chatbot)
    Reactive,
    /// Long-running, accepts async requests (LLM endpoint, DB pool)
    Service,
    /// Conditional dispatch on message content (intent classifier, A/B)
    Router,
}

/// Channel delivery semantics on an edge.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeliveryMode {
    /// Artifact produced once, consumed downstream (DataFrame between transforms)
    #[default]
    Once,
    /// Items arrive incrementally (LLM token stream, sensor data)
    Stream,
    /// Message queue with backpressure (agent-to-agent dialogue)
    Mailbox,
}

// ---------------------------------------------------------------------------
// Edge position: can be an integer (positional arg) or string (keyword arg).
// Mirrors `Edge.position` in Python which is `Positional | Keyword`.
// ---------------------------------------------------------------------------

/// Edge position — either a positional integer or a keyword argument name.
///
/// JSON deserialization handles both `"position": 0` and `"position": "strategy"`.
/// Mirrors the `_coerce` logic in `dorian/dag.py`.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Position {
    Index(i64),
    Keyword(String),
}

impl Position {
    /// Coerce from a JSON value, mirroring `Edge._coerce()` in Python.
    pub fn from_json_value(val: &serde_json::Value) -> Self {
        match val {
            serde_json::Value::Number(n) => {
                Position::Index(n.as_i64().unwrap_or(0))
            }
            serde_json::Value::String(s) => {
                // Try parsing as int first (JSON sometimes sends "0" as string).
                if let Ok(i) = s.parse::<i64>() {
                    Position::Index(i)
                } else {
                    Position::Keyword(s.clone())
                }
            }
            _ => Position::Index(0),
        }
    }

    /// Returns the integer value if this is an Index, None otherwise.
    pub fn as_index(&self) -> Option<i64> {
        match self {
            Position::Index(i) => Some(*i),
            Position::Keyword(_) => None,
        }
    }

    /// Returns the keyword name if this is a Keyword, None otherwise.
    pub fn as_keyword(&self) -> Option<&str> {
        match self {
            Position::Keyword(s) => Some(s),
            Position::Index(_) => None,
        }
    }
}

impl Default for Position {
    fn default() -> Self {
        Position::Index(0)
    }
}

// ---------------------------------------------------------------------------
// Node payload types
// ---------------------------------------------------------------------------

/// A library operator (e.g. `sklearn.preprocessing.StandardScaler`).
///
/// Mirrors `dorian.dag.Operator`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Operator {
    pub name: String,
    pub language: String,
    #[serde(default)]
    pub tasks: Vec<String>,
}

/// User-defined inline code block (must define a `foo(...)` function).
///
/// Mirrors `dorian.dag.Snippet`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Snippet {
    pub name: String,
    pub code: String,
    pub language: String,
}

/// Supported parameter types (matches Python `SupportedType` literal).
///
/// `State` is used by `dorian.io.state` placeholders that the
/// state-expansion pass replaces in-place with a resolved-value
/// Parameter. Without an explicit variant, the rust round-trip
/// would coerce `dtype: "state"` to `Unknown` and the python state
/// expansion (which keys on `dtype == "state"`) would silently skip
/// the node — leaving the literal placeholder string for downstream
/// consumers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ParamDtype {
    Int,
    Float,
    String,
    Str,
    Bool,
    Eval,
    Env,
    State,
    #[serde(other)]
    Unknown,
}

/// A typed constant value injected into the pipeline graph.
///
/// Mirrors `dorian.dag.Parameter`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Parameter {
    pub name: String,
    pub dtype: ParamDtype,
    pub value: String,
}

/// Pattern-matching node (rewrite rules only).
///
/// Mirrors `dorian.dag.Node`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PatternNode {
    #[serde(rename = "type", default = "wildcard")]
    pub node_type: String,
    #[serde(default = "wildcard")]
    pub text: String,
    #[serde(default = "wildcard")]
    pub language: String,
}

fn wildcard() -> String {
    ".*".to_string()
}

/// Maps an external handle on a Group to an internal node's port.
///
/// Mirrors `dorian.dag.IOMapping`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IOMapping {
    pub direction: String, // "input" or "output"
    #[serde(alias = "internalNodeId")]
    pub internal_node_id: String,
    #[serde(alias = "internalHandle")]
    pub internal_handle: Position,
}

/// Collapsed compound operator sub-DAG.
///
/// Mirrors `dorian.dag.Group`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Group {
    pub name: String,
    #[serde(default)]
    pub children: HashMap<String, serde_json::Value>,
    #[serde(default, alias = "internalEdges")]
    pub internal_edges: Vec<Edge>,
    #[serde(default, alias = "ioMap")]
    pub io_map: HashMap<String, IOMapping>,
    #[serde(default = "default_true")]
    pub collapsed: bool,
    #[serde(default, alias = "sourceInterface")]
    pub source_interface: String,
    #[serde(default, alias = "sourcePipelineId")]
    pub source_pipeline_id: String,
}

fn default_true() -> bool {
    true
}

// ---------------------------------------------------------------------------
// Node — tagged union of all payload types
// ---------------------------------------------------------------------------

/// A node in the process graph. Tagged union matching `dorian.dag.Nodes`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "class_type")]
pub enum Node {
    Operator(Operator),
    Snippet(Snippet),
    Parameter(Parameter),
    Node(PatternNode),
    Group(Group),
}

impl Node {
    /// Returns the human-readable name of this node.
    pub fn display_name(&self) -> &str {
        match self {
            Node::Operator(op) => &op.name,
            Node::Snippet(sn) => &sn.name,
            Node::Parameter(p) => &p.name,
            Node::Node(n) => &n.text,
            Node::Group(g) => &g.name,
        }
    }

    /// Returns the language of this node (if applicable).
    pub fn language(&self) -> Option<&str> {
        match self {
            Node::Operator(op) => Some(&op.language),
            Node::Snippet(sn) => Some(&sn.language),
            Node::Parameter(_) => None,
            Node::Node(n) => Some(&n.language),
            Node::Group(_) => None,
        }
    }

    /// Returns true if this is an Operator node.
    pub fn is_operator(&self) -> bool {
        matches!(self, Node::Operator(_))
    }

    /// Returns true if this is a Parameter node.
    pub fn is_parameter(&self) -> bool {
        matches!(self, Node::Parameter(_))
    }

    /// Returns true if this is a Snippet node.
    pub fn is_snippet(&self) -> bool {
        matches!(self, Node::Snippet(_))
    }

    /// Returns true if this is a Group node.
    pub fn is_group(&self) -> bool {
        matches!(self, Node::Group(_))
    }
}

// ---------------------------------------------------------------------------
// Edge
// ---------------------------------------------------------------------------

/// A directed edge connecting two nodes.
///
/// Mirrors `dorian.dag.Edge` with automatic position/output coercion.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Edge {
    pub source: NodeId,
    pub destination: NodeId,
    #[serde(default)]
    pub position: Position,
    #[serde(default)]
    pub output: Position,
    /// Channel delivery semantics (default: Once for classic dataflow).
    #[serde(default)]
    pub delivery_mode: DeliveryMode,
}

impl Edge {
    pub fn new(source: NodeId, destination: NodeId) -> Self {
        Edge {
            source,
            destination,
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        }
    }

    /// Output port as integer (panics if output is a keyword — should not
    /// happen per the data model, where output is always int).
    pub fn output_index(&self) -> i64 {
        self.output.as_index().unwrap_or(0)
    }
}

// ---------------------------------------------------------------------------
// ProcessGraph (DAG)
// ---------------------------------------------------------------------------

/// The top-level pipeline graph.
///
/// Mirrors `dorian.dag.DAG`. Named `ProcessGraph` to align with the
/// architecture vision (supports cycles in Phase 6), but currently
/// implements DAG semantics only.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ProcessGraph {
    pub nodes: FxHashMap<NodeId, Node>,
    pub edges: Vec<Edge>,
}

/// Type alias — `DAG` is the Python-era name, `ProcessGraph` is the target.
pub type DAG = ProcessGraph;

impl ProcessGraph {
    /// Create an empty graph.
    pub fn new() -> Self {
        ProcessGraph {
            nodes: FxHashMap::default(),
            edges: Vec::new(),
        }
    }

    /// Number of nodes.
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    /// Number of edges.
    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    /// Add a node to the graph.
    pub fn add_node(&mut self, id: NodeId, node: Node) {
        self.nodes.insert(id, node);
    }

    /// Add an edge to the graph.
    pub fn add_edge(&mut self, edge: Edge) {
        self.edges.push(edge);
    }

    /// Get a node by ID.
    pub fn get_node(&self, id: &str) -> Option<&Node> {
        self.nodes.get(id)
    }

    /// Get all edges where `source` matches the given node ID.
    pub fn outgoing_edges(&self, node_id: &str) -> Vec<&Edge> {
        self.edges.iter().filter(|e| e.source == node_id).collect()
    }

    /// Get all edges where `destination` matches the given node ID.
    pub fn incoming_edges(&self, node_id: &str) -> Vec<&Edge> {
        self.edges
            .iter()
            .filter(|e| e.destination == node_id)
            .collect()
    }

    /// Get predecessor node IDs (nodes with edges pointing to `node_id`).
    pub fn predecessors(&self, node_id: &str) -> Vec<&str> {
        self.incoming_edges(node_id)
            .iter()
            .map(|e| e.source.as_str())
            .collect()
    }

    /// Get successor node IDs (nodes `node_id` points to).
    pub fn successors(&self, node_id: &str) -> Vec<&str> {
        self.outgoing_edges(node_id)
            .iter()
            .map(|e| e.destination.as_str())
            .collect()
    }

    /// Returns node IDs with no incoming edges (graph roots / sources).
    pub fn roots(&self) -> Vec<&str> {
        let destinations: std::collections::HashSet<&str> =
            self.edges.iter().map(|e| e.destination.as_str()).collect();
        self.nodes
            .keys()
            .filter(|id| !destinations.contains(id.as_str()))
            .map(|id| id.as_str())
            .collect()
    }

    /// Returns node IDs with no outgoing edges (graph leaves / sinks).
    pub fn leaves(&self) -> Vec<&str> {
        let sources: std::collections::HashSet<&str> =
            self.edges.iter().map(|e| e.source.as_str()).collect();
        self.nodes
            .keys()
            .filter(|id| !sources.contains(id.as_str()))
            .map(|id| id.as_str())
            .collect()
    }

    // -- Serialization (docstore document format) ----------------------------

    /// Deserialize from the JSON dict format used in docstore and the Python DAG.
    ///
    /// Mirrors `DAG.from_json_dict()` in Python.
    pub fn from_json(data: &serde_json::Value) -> Result<Self, GraphError> {
        let nodes_val = data
            .get("nodes")
            .ok_or(GraphError::MissingField("nodes"))?;
        let empty_edges = serde_json::Value::Array(vec![]);
        let edges_val = data.get("edges").unwrap_or(&empty_edges);

        let mut nodes = FxHashMap::default();
        if let serde_json::Value::Object(obj) = nodes_val {
            for (id, node_val) in obj {
                let node: Node = serde_json::from_value(node_val.clone())
                    .map_err(|e| GraphError::DeserializationError(format!("node {id}: {e}")))?;
                nodes.insert(id.clone(), node);
            }
        }

        let mut edges = Vec::new();
        if let serde_json::Value::Array(arr) = edges_val {
            for edge_val in arr {
                let edge: Edge = serde_json::from_value(edge_val.clone())
                    .map_err(|e| GraphError::DeserializationError(format!("edge: {e}")))?;
                edges.push(edge);
            }
        }

        Ok(ProcessGraph { nodes, edges })
    }

    /// Serialize to the JSON dict format for docstore / frontend.
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "version": "1.0",
            "metadata": {
                "node_count": self.node_count(),
                "edge_count": self.edge_count(),
            },
            "nodes": self.nodes.iter().map(|(id, node)| {
                (id.clone(), serde_json::to_value(node).unwrap_or_default())
            }).collect::<serde_json::Map<String, serde_json::Value>>(),
            "edges": self.edges.iter().map(|e| {
                serde_json::to_value(e).unwrap_or_default()
            }).collect::<Vec<_>>(),
        })
    }
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
pub enum GraphError {
    #[error("missing field: {0}")]
    MissingField(&'static str),
    #[error("deserialization error: {0}")]
    DeserializationError(String),
    #[error("validation error: {0}")]
    ValidationError(String),
    #[error("cycle detected")]
    CycleDetected,
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_dag_json() -> serde_json::Value {
        serde_json::json!({
            "nodes": {
                "n1": {
                    "class_type": "Parameter",
                    "name": "fpath",
                    "dtype": "string",
                    "value": "/data/test.csv"
                },
                "n2": {
                    "class_type": "Operator",
                    "name": "pandas.read_csv",
                    "language": "python"
                },
                "n3": {
                    "class_type": "Operator",
                    "name": "sklearn.preprocessing.StandardScaler",
                    "language": "python"
                }
            },
            "edges": [
                {"source": "n1", "destination": "n2", "position": 0, "output": 0},
                {"source": "n2", "destination": "n3", "position": 1, "output": 0}
            ]
        })
    }

    #[test]
    fn test_parse_dag_from_json() {
        let json = sample_dag_json();
        let dag = ProcessGraph::from_json(&json).unwrap();
        assert_eq!(dag.node_count(), 3);
        assert_eq!(dag.edge_count(), 2);
    }

    #[test]
    fn test_node_types() {
        let json = sample_dag_json();
        let dag = ProcessGraph::from_json(&json).unwrap();

        assert!(dag.get_node("n1").unwrap().is_parameter());
        assert!(dag.get_node("n2").unwrap().is_operator());
        assert!(dag.get_node("n3").unwrap().is_operator());
    }

    #[test]
    fn test_edge_position_coercion() {
        // Integer position
        let pos = Position::from_json_value(&serde_json::json!(0));
        assert_eq!(pos, Position::Index(0));

        // String-encoded integer
        let pos = Position::from_json_value(&serde_json::json!("1"));
        assert_eq!(pos, Position::Index(1));

        // Keyword position
        let pos = Position::from_json_value(&serde_json::json!("strategy"));
        assert_eq!(pos, Position::Keyword("strategy".to_string()));
    }

    #[test]
    fn test_graph_navigation() {
        let json = sample_dag_json();
        let dag = ProcessGraph::from_json(&json).unwrap();

        // n1 has no predecessors (root)
        assert!(dag.predecessors("n1").is_empty());
        // n2 has n1 as predecessor
        assert_eq!(dag.predecessors("n2"), vec!["n1"]);
        // n3 has n2 as predecessor
        assert_eq!(dag.predecessors("n3"), vec!["n2"]);

        // n1 → n2 → n3
        assert_eq!(dag.successors("n1"), vec!["n2"]);
        assert_eq!(dag.successors("n2"), vec!["n3"]);
        assert!(dag.successors("n3").is_empty());
    }

    #[test]
    fn test_roots_and_leaves() {
        let json = sample_dag_json();
        let dag = ProcessGraph::from_json(&json).unwrap();

        let roots = dag.roots();
        assert_eq!(roots.len(), 1);
        assert!(roots.contains(&"n1"));

        let leaves = dag.leaves();
        assert_eq!(leaves.len(), 1);
        assert!(leaves.contains(&"n3"));
    }

    #[test]
    fn test_roundtrip_json() {
        let json = sample_dag_json();
        let dag = ProcessGraph::from_json(&json).unwrap();
        let json2 = dag.to_json();
        let dag2 = ProcessGraph::from_json(&json2).unwrap();
        assert_eq!(dag.node_count(), dag2.node_count());
        assert_eq!(dag.edge_count(), dag2.edge_count());
    }

    #[test]
    fn test_snippet_node() {
        let json = serde_json::json!({
            "nodes": {
                "s1": {
                    "class_type": "Snippet",
                    "name": "auto_select",
                    "code": "def foo(df):\n    return df",
                    "language": "python"
                }
            },
            "edges": []
        });
        let dag = ProcessGraph::from_json(&json).unwrap();
        let node = dag.get_node("s1").unwrap();
        assert!(node.is_snippet());
        assert_eq!(node.display_name(), "auto_select");
    }

    #[test]
    fn test_keyword_edge_position() {
        let json = serde_json::json!({
            "nodes": {
                "p1": {"class_type": "Parameter", "name": "n_estimators", "dtype": "int", "value": "100"},
                "o1": {"class_type": "Operator", "name": "sklearn.ensemble.RandomForestClassifier", "language": "python"}
            },
            "edges": [
                {"source": "p1", "destination": "o1", "position": "n_estimators", "output": 0}
            ]
        });
        let dag = ProcessGraph::from_json(&json).unwrap();
        let edge = &dag.edges[0];
        assert_eq!(edge.position, Position::Keyword("n_estimators".to_string()));
    }

    #[test]
    fn test_group_node() {
        let json = serde_json::json!({
            "nodes": {
                "g1": {
                    "class_type": "Group",
                    "name": "StandardScaler",
                    "children": {
                        "c1": {"class_type": "Operator", "name": "sklearn.preprocessing.StandardScaler.__init__", "language": "python"},
                        "c2": {"class_type": "Operator", "name": "fit_transform", "language": "python"}
                    },
                    "internalEdges": [
                        {"source": "c1", "destination": "c2", "position": 0, "output": 0}
                    ],
                    "ioMap": {
                        "input-1": {"direction": "input", "internalNodeId": "c2", "internalHandle": 1}
                    },
                    "collapsed": true,
                    "sourceInterface": "Sklearn Transformer"
                }
            },
            "edges": []
        });
        let dag = ProcessGraph::from_json(&json).unwrap();
        let node = dag.get_node("g1").unwrap();
        assert!(node.is_group());
        if let Node::Group(g) = node {
            assert_eq!(g.children.len(), 2);
            assert_eq!(g.internal_edges.len(), 1);
            assert_eq!(g.io_map.len(), 1);
            assert!(g.collapsed);
            assert_eq!(g.source_interface, "Sklearn Transformer");
        }
    }
}
