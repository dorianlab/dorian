//! `dorian.io.state` reference expansion.
//!
//! Replaces every state placeholder with a Parameter node carrying
//! the resolved value. Two placeholder shapes are supported, matching
//! `dorian.pipeline.state._expand_state`:
//!
//! 1. **Legacy** — ``Operator(name="dorian.io.state")`` plus an
//!    incoming ``Parameter(name="key", value="dataset.features")``
//!    (and optional ``Parameter(name="dataset", value="<alias>")``).
//!    Both Parameters are consumed alongside the operator.
//!
//! 2. **Compact** — ``Parameter(name="dorian.io.state",
//!    dtype="state", value="dataset.features")`` standing alone.
//!    Only the Parameter itself is consumed.
//!
//! The replacement is always a single Parameter whose ``name`` is
//! the resolved key (e.g. ``dataset.features``), ``dtype`` /
//! ``value`` come from the python caller's pre-resolved meta. The
//! rust side does no I/O — the python facade in
//! ``dorian/pipeline/state.py`` reads Redis + invokes the resolver
//! allowlist before calling here.

use rustc_hash::FxHashMap;

use crate::model::{Edge, Node, NodeId, ParamDtype, Parameter, Position, ProcessGraph};

/// Resolved-value record for one placeholder. The ``node_id`` matches
/// the placeholder's id in the input graph (compact-form Parameter or
/// legacy-form Operator). ``dtype`` is one of ``str`` or ``eval`` —
/// matches python's ``_expand_state`` which picks ``str`` for raw
/// strings and ``eval`` (``ast.literal_eval``-able) for everything
/// else, including ``None`` (encoded as ``"None"``).
#[derive(Debug, Clone)]
pub struct ResolvedState {
    pub node_id: NodeId,
    /// Cosmetic Parameter name — the original state key, e.g.
    /// ``dataset.features``. Mirrors python's choice so downstream
    /// code that pretty-prints by name reads identically.
    pub key: String,
    /// One of ``"str"`` or ``"eval"``.
    pub dtype: String,
    /// String literal — already escaped/repr'd by the python
    /// resolver where needed.
    pub value: String,
}

pub fn expand_state_refs(
    graph: ProcessGraph,
    resolutions: &[ResolvedState],
) -> ProcessGraph {
    if resolutions.is_empty() {
        return graph;
    }

    // Index resolutions by placeholder node id for O(1) lookup.
    let mut by_id: FxHashMap<&str, &ResolvedState> = FxHashMap::default();
    for r in resolutions {
        by_id.insert(r.node_id.as_str(), r);
    }

    let mut nodes = graph.nodes;
    let mut edges = graph.edges;

    for resolved in resolutions {
        let nid = resolved.node_id.clone();
        let placeholder = match nodes.get(&nid) {
            Some(n) => n.clone(),
            None => continue, // already removed by a sibling resolution
        };

        // Determine which incoming Parameter ids to consume — only the
        // legacy ``Operator(...)`` form pulls in ``key`` + ``dataset``
        // Parameters; the compact ``Parameter(state)`` form has no
        // incoming Parameters to drop.
        let mut consumed: Vec<NodeId> = Vec::new();
        if matches!(placeholder, Node::Operator(_)) {
            for e in &edges {
                if e.destination != nid {
                    continue;
                }
                if let Some(Node::Parameter(p)) = nodes.get(&e.source) {
                    if p.name == "key" || p.name == "dataset" {
                        consumed.push(e.source.clone());
                    }
                }
            }
        }

        // Capture outgoing edges before we drop them.
        let outgoing: Vec<(NodeId, Position, Position)> = edges
            .iter()
            .filter(|e| e.source == nid)
            .map(|e| (e.destination.clone(), e.position.clone(), e.output.clone()))
            .collect();

        // Drop placeholder + consumed Parameters + their incident edges.
        nodes.remove(&nid);
        for c in &consumed {
            nodes.remove(c);
        }
        let removed: rustc_hash::FxHashSet<&str> = std::iter::once(nid.as_str())
            .chain(consumed.iter().map(|s| s.as_str()))
            .collect();
        edges.retain(|e| !removed.contains(e.source.as_str())
            && !removed.contains(e.destination.as_str()));

        // Insert the single replacement Parameter.
        let value_id = format!("state_{nid}");
        let dtype = match resolved.dtype.as_str() {
            "str" => ParamDtype::Str,
            "eval" => ParamDtype::Eval,
            "int" => ParamDtype::Int,
            "float" => ParamDtype::Float,
            "bool" => ParamDtype::Bool,
            "env" => ParamDtype::Env,
            "string" => ParamDtype::String,
            // Unknown dtypes fall back to ``eval`` so the value still
            // round-trips through the Python ast.literal_eval handler.
            // Logging is graph-crate-noise-free on purpose.
            _ => ParamDtype::Eval,
        };
        nodes.insert(
            value_id.clone(),
            Node::Parameter(Parameter {
                name: resolved.key.clone(),
                dtype,
                value: resolved.value.clone(),
            }),
        );

        // Rewire outgoing edges from the new Parameter.
        for (dst, pos, out) in outgoing {
            edges.push(Edge {
                source: value_id.clone(),
                destination: dst,
                position: pos,
                output: out,
                delivery_mode: Default::default(),
            });
        }
    }

    ProcessGraph { nodes, edges }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustc_hash::FxHashMap;

    use crate::model::{Operator, Snippet};

    fn op(name: &str) -> Node {
        Node::Operator(Operator {
            name: name.into(),
            language: "python".into(),
            tasks: Vec::new(),
        })
    }

    fn param(name: &str, dtype: ParamDtype, value: &str) -> Node {
        Node::Parameter(Parameter {
            name: name.into(),
            dtype,
            value: value.into(),
        })
    }

    #[test]
    fn legacy_form_consumes_key_and_dataset_params() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("st".into(), op("dorian.io.state"));
        nodes.insert(
            "k".into(),
            param("key", ParamDtype::Str, "dataset.features"),
        );
        nodes.insert(
            "d".into(),
            param("dataset", ParamDtype::Str, "iris"),
        );
        nodes.insert("clf".into(), op("sklearn.linear_model.LogisticRegression"));
        let edges = vec![
            Edge {
                source: "k".into(),
                destination: "st".into(),
                position: Position::Keyword("key".into()),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
            Edge {
                source: "d".into(),
                destination: "st".into(),
                position: Position::Keyword("dataset".into()),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
            Edge {
                source: "st".into(),
                destination: "clf".into(),
                position: Position::Keyword("features".into()),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
        ];
        let graph = ProcessGraph { nodes, edges };

        let resolutions = vec![ResolvedState {
            node_id: "st".into(),
            key: "dataset.features".into(),
            dtype: "eval".into(),
            value: "['sepal_length', 'sepal_width']".into(),
        }];
        let out = expand_state_refs(graph, &resolutions);

        // st + k + d removed, state_st added, clf retained.
        assert_eq!(out.nodes.len(), 2);
        assert!(out.nodes.contains_key("clf"));
        assert!(out.nodes.contains_key("state_st"));
        // Outgoing edge rewired from new Parameter.
        let outgoing: Vec<&Edge> = out
            .edges
            .iter()
            .filter(|e| e.destination == "clf")
            .collect();
        assert_eq!(outgoing.len(), 1);
        assert_eq!(outgoing[0].source, "state_st");
        // Replacement Parameter shape.
        if let Node::Parameter(p) = &out.nodes["state_st"] {
            assert_eq!(p.name, "dataset.features");
            assert!(matches!(p.dtype, ParamDtype::Eval));
            assert_eq!(p.value, "['sepal_length', 'sepal_width']");
        } else {
            panic!("state_st should be a Parameter");
        }
    }

    #[test]
    fn compact_form_only_consumes_self() {
        // The compact form has no incoming Parameters — just the
        // single ``Parameter(dtype="state")`` node with outgoing
        // edges directly to consumers.
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert(
            "st".into(),
            param("dorian.io.state", ParamDtype::Eval, "session.task"),
        );
        nodes.insert("clf".into(), op("sklearn.linear_model.LogisticRegression"));
        let edges = vec![Edge {
            source: "st".into(),
            destination: "clf".into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        }];
        let graph = ProcessGraph { nodes, edges };

        let resolutions = vec![ResolvedState {
            node_id: "st".into(),
            key: "session.task".into(),
            dtype: "str".into(),
            value: "Classification".into(),
        }];
        let out = expand_state_refs(graph, &resolutions);

        assert_eq!(out.nodes.len(), 2); // state_st + clf
        assert!(out.nodes.contains_key("state_st"));
        if let Node::Parameter(p) = &out.nodes["state_st"] {
            assert!(matches!(p.dtype, ParamDtype::Str));
            assert_eq!(p.value, "Classification");
        } else {
            panic!("state_st should be a Parameter");
        }
    }

    #[test]
    fn no_op_when_no_resolutions() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("a".into(), op("sklearn.preprocessing.StandardScaler"));
        let g = ProcessGraph {
            nodes,
            edges: vec![],
        };
        let out = expand_state_refs(g.clone(), &[]);
        assert_eq!(out.nodes.len(), 1);
    }

    #[test]
    fn dropped_resolution_skips_missing_node() {
        // Defensive: a resolution that points at a node id that's
        // already gone from the graph (e.g. removed by a prior
        // expansion pass) is silently skipped.
        let g = ProcessGraph {
            nodes: FxHashMap::default(),
            edges: vec![],
        };
        let resolutions = vec![ResolvedState {
            node_id: "ghost".into(),
            key: "dataset.features".into(),
            dtype: "eval".into(),
            value: "[]".into(),
        }];
        let out = expand_state_refs(g, &resolutions);
        assert!(out.nodes.is_empty());
    }
}
