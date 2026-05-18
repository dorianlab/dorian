//! Categorical-encoding insertion.
//!
//! Mirrors `dorian.pipeline.transforms._insert_encoder`: when the
//! session's dataset has categorical features, interpose an
//! ``sklearn.preprocessing.OrdinalEncoder`` between the X data
//! source and the ``train_test_split`` operator.
//!
//! The python facade decides whether to insert (``force_encoding``
//! override OR ``profile.NumberOfCategoricalFeatures > 0``) and
//! passes that single bool to the rust path. All Redis I/O stays
//! python-side; rust does pure graph mutation.
//!
//! Guards (mirrored byte-for-byte from python):
//!   * if any encoding op (``OrdinalEncoder`` / ``LabelEncoder`` /
//!     ``OneHotEncoder``) is already present → no-op
//!   * if ``should_insert`` is false → no-op
//!   * if no suitable X edge (position 0, non-Parameter source)
//!     into the matched ``train_test_split`` → no-op for that match
//!
//! Pattern matches every ``train_test_split`` Operator. The
//! ``_has_encoding_operator`` guard short-circuits subsequent
//! matches once the first encoder lands, exactly like the python
//! fixed-point under ``sync_apply``.

use crate::model::{
    Edge, Node, NodeId, Operator, ParamDtype, Parameter, Position, ProcessGraph,
};

const ENCODING_OPS: &[&str] = &[
    "sklearn.preprocessing.OrdinalEncoder",
    "sklearn.preprocessing.LabelEncoder",
    "sklearn.preprocessing.OneHotEncoder",
];

const TRAIN_TEST_SPLIT: &str = "sklearn.model_selection.train_test_split";

fn has_encoding_operator(graph: &ProcessGraph) -> bool {
    graph.nodes.values().any(|n| match n {
        Node::Operator(Operator { name, .. }) => {
            ENCODING_OPS.iter().any(|enc| enc == name)
        }
        _ => false,
    })
}

/// Insert ``OrdinalEncoder`` upstream of every ``train_test_split``
/// when ``should_insert`` is true and no encoding op is already
/// present. Returns the graph unchanged when guards trip.
pub fn expand_categorical_encoding(
    graph: ProcessGraph,
    should_insert: bool,
) -> ProcessGraph {
    if !should_insert {
        return graph;
    }
    if has_encoding_operator(&graph) {
        return graph;
    }

    let split_ids: Vec<NodeId> = graph
        .nodes
        .iter()
        .filter_map(|(id, node)| match node {
            Node::Operator(Operator { name, .. }) if name == TRAIN_TEST_SPLIT => {
                Some(id.clone())
            }
            _ => None,
        })
        .collect();
    if split_ids.is_empty() {
        return graph;
    }

    let mut nodes = graph.nodes;
    let mut edges = graph.edges;

    for nid in split_ids {
        // Re-check the encoding-operator guard inside the loop:
        // the first iteration may have inserted one, in which case
        // subsequent ``train_test_split`` matches must short-circuit
        // (matches the python fixed-point's behaviour exactly).
        let already_encoded = nodes.values().any(|n| match n {
            Node::Operator(Operator { name, .. }) => {
                ENCODING_OPS.iter().any(|enc| enc == name)
            }
            _ => false,
        });
        if already_encoded {
            continue;
        }

        // Find the X edge: position 0, non-Parameter source.
        let x_edge_idx = edges.iter().position(|e| {
            e.destination == nid
                && matches!(e.position, Position::Index(0))
                && !matches!(nodes.get(&e.source), Some(Node::Parameter(_)))
        });
        let Some(x_idx) = x_edge_idx else {
            continue;
        };
        let x_edge = edges[x_idx].clone();

        let encoder_id: NodeId = format!("encoder_{nid}");
        let p_handle_id: NodeId = format!("p_handle_unknown_{nid}");
        let p_unkval_id: NodeId = format!("p_unknown_value_{nid}");

        nodes.insert(
            encoder_id.clone(),
            Node::Operator(Operator {
                name: "sklearn.preprocessing.OrdinalEncoder".into(),
                language: "python".into(),
                tasks: Vec::new(),
            }),
        );
        nodes.insert(
            p_handle_id.clone(),
            Node::Parameter(Parameter {
                name: "handle_unknown".into(),
                dtype: ParamDtype::Str,
                value: "use_encoded_value".into(),
            }),
        );
        nodes.insert(
            p_unkval_id.clone(),
            Node::Parameter(Parameter {
                name: "unknown_value".into(),
                dtype: ParamDtype::Eval,
                value: "-1".into(),
            }),
        );

        // Drop the original X edge and rewire through the encoder.
        edges.remove(x_idx);
        edges.push(Edge {
            source: x_edge.source.clone(),
            destination: encoder_id.clone(),
            position: Position::Index(0),
            output: x_edge.output.clone(),
            delivery_mode: Default::default(),
        });
        edges.push(Edge {
            source: encoder_id.clone(),
            destination: nid.clone(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });
        edges.push(Edge {
            source: p_handle_id,
            destination: encoder_id.clone(),
            position: Position::Keyword("handle_unknown".into()),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });
        edges.push(Edge {
            source: p_unkval_id,
            destination: encoder_id,
            position: Position::Keyword("unknown_value".into()),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });
    }

    ProcessGraph { nodes, edges }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustc_hash::FxHashMap;

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
    fn no_op_when_should_insert_false() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("ld".into(), op("pandas.read_csv"));
        nodes.insert("sp".into(), op(TRAIN_TEST_SPLIT));
        let edges = vec![Edge {
            source: "ld".into(),
            destination: "sp".into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        }];
        let g = ProcessGraph { nodes, edges };
        let out = expand_categorical_encoding(g, false);
        assert_eq!(out.nodes.len(), 2);
        assert!(!out.nodes.contains_key("encoder_sp"));
    }

    #[test]
    fn no_op_when_encoder_already_present() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("ld".into(), op("pandas.read_csv"));
        nodes.insert("enc".into(), op("sklearn.preprocessing.OrdinalEncoder"));
        nodes.insert("sp".into(), op(TRAIN_TEST_SPLIT));
        let edges = vec![
            Edge {
                source: "ld".into(),
                destination: "enc".into(),
                position: Position::Index(0),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
            Edge {
                source: "enc".into(),
                destination: "sp".into(),
                position: Position::Index(0),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
        ];
        let g = ProcessGraph {
            nodes,
            edges: edges.clone(),
        };
        let out = expand_categorical_encoding(g, true);
        // Untouched: enc + sp + ld, no new encoder_sp Parameter trio.
        assert_eq!(out.nodes.len(), 3);
        assert!(!out.nodes.contains_key("encoder_sp"));
        assert!(!out.nodes.contains_key("p_handle_unknown_sp"));
    }

    #[test]
    fn no_op_when_x_edge_missing() {
        // train_test_split with only Parameter sources (no real X path)
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("rs".into(), param("random_state", ParamDtype::Eval, "42"));
        nodes.insert("sp".into(), op(TRAIN_TEST_SPLIT));
        let edges = vec![Edge {
            source: "rs".into(),
            destination: "sp".into(),
            position: Position::Keyword("random_state".into()),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        }];
        let g = ProcessGraph { nodes, edges };
        let out = expand_categorical_encoding(g, true);
        assert_eq!(out.nodes.len(), 2);
        assert!(!out.nodes.contains_key("encoder_sp"));
    }

    #[test]
    fn inserts_encoder_with_correct_wiring() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("ld".into(), op("pandas.read_csv"));
        nodes.insert("sp".into(), op(TRAIN_TEST_SPLIT));
        let edges = vec![Edge {
            source: "ld".into(),
            destination: "sp".into(),
            position: Position::Index(0),
            output: Position::Index(2),
            delivery_mode: Default::default(),
        }];
        let g = ProcessGraph { nodes, edges };
        let out = expand_categorical_encoding(g, true);

        // ld + sp + encoder_sp + 2 parameters = 5 nodes
        assert_eq!(out.nodes.len(), 5);
        assert!(out.nodes.contains_key("encoder_sp"));
        assert!(out.nodes.contains_key("p_handle_unknown_sp"));
        assert!(out.nodes.contains_key("p_unknown_value_sp"));

        // ld → encoder_sp must carry the original ``output=2`` slice.
        let pre = out
            .edges
            .iter()
            .find(|e| e.source == "ld" && e.destination == "encoder_sp")
            .expect("ld → encoder edge");
        assert_eq!(pre.position, Position::Index(0));
        assert_eq!(pre.output, Position::Index(2));

        // encoder_sp → sp at position 0 (replacing the original X edge).
        let post = out
            .edges
            .iter()
            .find(|e| e.source == "encoder_sp" && e.destination == "sp")
            .expect("encoder → sp edge");
        assert_eq!(post.position, Position::Index(0));

        // Parameter wiring: handle_unknown + unknown_value as kwargs.
        let handle_edge = out
            .edges
            .iter()
            .find(|e| e.source == "p_handle_unknown_sp")
            .expect("handle_unknown edge");
        assert!(matches!(
            &handle_edge.position,
            Position::Keyword(k) if k == "handle_unknown"
        ));
        let unkval_edge = out
            .edges
            .iter()
            .find(|e| e.source == "p_unknown_value_sp")
            .expect("unknown_value edge");
        assert!(matches!(
            &unkval_edge.position,
            Position::Keyword(k) if k == "unknown_value"
        ));

        // The original ld → sp edge must be gone.
        assert!(!out.edges.iter().any(|e| {
            e.source == "ld" && e.destination == "sp"
        }));
    }

    #[test]
    fn second_train_test_split_does_not_double_insert() {
        // Two split ops in the same DAG. The fixed-point semantics
        // mean only the first one gets an encoder; the second
        // short-circuits because an encoder is now present.
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("ld".into(), op("pandas.read_csv"));
        nodes.insert("sp1".into(), op(TRAIN_TEST_SPLIT));
        nodes.insert("sp2".into(), op(TRAIN_TEST_SPLIT));
        let edges = vec![
            Edge {
                source: "ld".into(),
                destination: "sp1".into(),
                position: Position::Index(0),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
            Edge {
                source: "ld".into(),
                destination: "sp2".into(),
                position: Position::Index(0),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
        ];
        let g = ProcessGraph { nodes, edges };
        let out = expand_categorical_encoding(g, true);

        let encoder_count = out
            .nodes
            .values()
            .filter(|n| matches!(
                n,
                Node::Operator(Operator { name, .. })
                    if name == "sklearn.preprocessing.OrdinalEncoder"
            ))
            .count();
        assert_eq!(encoder_count, 1, "exactly one encoder must be inserted");
    }
}
