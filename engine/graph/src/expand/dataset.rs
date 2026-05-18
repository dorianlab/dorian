//! `dorian.io.dataset` reference expansion.
//!
//! Replaces every ``dorian.io.dataset`` placeholder operator with a
//! concrete loader sub-chain:
//!
//! ```text
//!     Parameter(fpath) ─┐
//!                       ├─► loader (e.g. pandas.read_csv) ─► (optional) split_xy ─► (X, y)
//!     Parameter(features) ─► split_xy
//!     Parameter(target)   ─► split_xy
//! ```
//!
//! The trailing ``split_xy`` snippet is only inserted when at least
//! one outgoing edge of the placeholder declares ``output=1`` (the y
//! channel). Legacy auto-sklearn pipelines that bring their own
//! split keep a single-DataFrame fan-out from the loader.
//!
//! Mirrors `dorian.pipeline.transforms._expand_dataset` byte-for-byte
//! — see that function's docstring for the wider rationale. The
//! python rule (``DATASET_EXPANSION_RULE``) and rust port apply the
//! same pattern (any node whose ``Operator.name`` matches the
//! ``dorian\.io\.dataset`` regex).

use crate::model::{
    Edge, Node, NodeId, Operator, ParamDtype, Parameter, Position, ProcessGraph, Snippet,
};

/// Meta the python caller assembles from session redis state. Keep
/// the field set minimal — the python facade in
/// ``dorian/pipeline/transforms.py:expand_dataset_refs`` is still
/// the source of truth for *how* to fetch each value.
#[derive(Debug, Clone)]
pub struct DatasetMeta {
    /// Absolute path to the dataset file (CSV / Parquet / …).
    pub fpath: String,
    /// Loader operator FQN — e.g. ``pandas.read_csv``.
    pub loader: String,
    /// Feature column names. Empty list means "fall back to all-but-last".
    pub features: Vec<String>,
    /// Target column. Empty string means "fall back to last column".
    /// Lists are also accepted (the python snippet picks the first).
    pub target: TargetSpec,
}

/// Target column may be a single name, a list of names, or absent.
/// The python parameter uses ``repr`` to round-trip whichever shape
/// the session meta holds; the rust side replicates that.
#[derive(Debug, Clone)]
pub enum TargetSpec {
    None,
    Single(String),
    Many(Vec<String>),
}

impl TargetSpec {
    fn to_repr(&self) -> String {
        match self {
            TargetSpec::None => "''".to_string(),
            TargetSpec::Single(s) => format!("'{}'", s.replace('\'', "\\'")),
            TargetSpec::Many(items) => {
                let inner = items
                    .iter()
                    .map(|s| format!("'{}'", s.replace('\'', "\\'")))
                    .collect::<Vec<_>>()
                    .join(", ");
                format!("[{inner}]")
            }
        }
    }
}

/// Inline ``split_xy`` snippet body. Identical to
/// ``dorian.pipeline.transforms._SPLIT_XY_SNIPPET`` so downstream
/// callable resolution works unchanged.
const SPLIT_XY_SNIPPET: &str = r#"def foo(df, features=None, target=None):
    """Split a DataFrame into (X, y) using session feature/target columns.

    Injected by ``DATASET_EXPANSION_RULE``. ``features`` and ``target`` are
    passed as kwargs from Parameter nodes seeded from session dataset meta.
    Falls back to "all-but-last-column is X, last is y" when either is empty
    so pipelines against un-profiled datasets still execute.
    """
    import pandas as pd
    if isinstance(features, str):
        features = [features]
    if isinstance(target, (list, tuple)):
        target = target[0] if target else None
    if not target:
        target = df.columns[-1]
    if not features:
        features = [c for c in df.columns if c != target]
    X = df[list(features)]
    y = df[target]
    return X, y
"#;

/// Expand every ``dorian.io.dataset`` node in ``graph``. ``graph``
/// is consumed and a new graph is returned. ``meta`` is used for
/// every match — sessions only ever have one active dataset, so the
/// list-of-meta variant isn't worth the API surface.
pub fn expand_dataset_refs(graph: ProcessGraph, meta: &DatasetMeta) -> ProcessGraph {
    // Find every dataset-placeholder node id in one pass. Holding a
    // ``Vec<NodeId>`` instead of iterating during mutation avoids
    // the borrow-checker fight when we mutate ``graph.nodes``.
    let placeholders: Vec<NodeId> = graph
        .nodes
        .iter()
        .filter_map(|(id, node)| {
            if is_dataset_placeholder(node) {
                Some(id.clone())
            } else {
                None
            }
        })
        .collect();

    if placeholders.is_empty() {
        return graph;
    }

    let mut nodes = graph.nodes;
    let mut edges = graph.edges;
    for nid in placeholders {
        let outgoing: Vec<(NodeId, Position, Position)> = edges
            .iter()
            .filter(|e| e.source == nid)
            .map(|e| (e.destination.clone(), e.position.clone(), e.output.clone()))
            .collect();

        // Drop the placeholder + its incident edges.
        nodes.remove(&nid);
        edges.retain(|e| e.source != nid && e.destination != nid);

        let needs_split = outgoing.iter().any(|(_, _, out)| {
            matches!(out, Position::Index(i) if *i == 1)
        });

        let fpath_id = format!("fpath_{nid}");
        let loader_id = format!("loader_{nid}");
        nodes.insert(
            fpath_id.clone(),
            Node::Parameter(Parameter {
                name: "fpath".into(),
                dtype: ParamDtype::Str,
                value: meta.fpath.clone(),
            }),
        );
        nodes.insert(
            loader_id.clone(),
            Node::Operator(Operator {
                name: meta.loader.clone(),
                language: "python".into(),
                tasks: Vec::new(),
            }),
        );
        edges.push(Edge {
            source: fpath_id,
            destination: loader_id.clone(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });

        if !needs_split {
            // Single-DataFrame fan-out — preserves the original
            // ``position``/``output`` on each outgoing edge.
            for (dst, pos, out) in outgoing {
                edges.push(Edge {
                    source: loader_id.clone(),
                    destination: dst,
                    position: pos,
                    output: out,
                    delivery_mode: Default::default(),
                });
            }
            continue;
        }

        // X/y split fan-out: loader → split_xy → (X, y)
        let split_id = format!("split_xy_{nid}");
        let features_id = format!("features_{nid}");
        let target_id = format!("target_{nid}");
        nodes.insert(
            features_id.clone(),
            Node::Parameter(Parameter {
                name: "features".into(),
                dtype: ParamDtype::Eval,
                value: features_repr(&meta.features),
            }),
        );
        nodes.insert(
            target_id.clone(),
            Node::Parameter(Parameter {
                name: "target".into(),
                dtype: ParamDtype::Eval,
                value: meta.target.to_repr(),
            }),
        );
        nodes.insert(
            split_id.clone(),
            Node::Snippet(Snippet {
                name: "split_xy".into(),
                code: SPLIT_XY_SNIPPET.into(),
                language: "python".into(),
            }),
        );
        edges.push(Edge {
            source: loader_id,
            destination: split_id.clone(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });
        edges.push(Edge {
            source: features_id,
            destination: split_id.clone(),
            position: Position::Keyword("features".into()),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });
        edges.push(Edge {
            source: target_id,
            destination: split_id.clone(),
            position: Position::Keyword("target".into()),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });

        for (dst, pos, out) in outgoing {
            let out_idx: i64 = match out {
                Position::Index(i) => i,
                Position::Keyword(_) => 0,
            };
            let new_out = if out_idx == 1 {
                Position::Index(1)
            } else {
                Position::Index(0)
            };
            edges.push(Edge {
                source: split_id.clone(),
                destination: dst,
                position: pos,
                output: new_out,
                delivery_mode: Default::default(),
            });
        }
    }

    ProcessGraph { nodes, edges }
}

fn is_dataset_placeholder(node: &Node) -> bool {
    match node {
        Node::Operator(op) => op.name == "dorian.io.dataset",
        _ => false,
    }
}

fn features_repr(features: &[String]) -> String {
    if features.is_empty() {
        return "[]".to_string();
    }
    let inner = features
        .iter()
        .map(|s| format!("'{}'", s.replace('\'', "\\'")))
        .collect::<Vec<_>>()
        .join(", ");
    format!("[{inner}]")
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustc_hash::FxHashMap;

    fn op_node(name: &str) -> Node {
        Node::Operator(Operator {
            name: name.into(),
            language: "python".into(),
            tasks: Vec::new(),
        })
    }

    fn meta() -> DatasetMeta {
        DatasetMeta {
            fpath: "/data/iris.csv".into(),
            loader: "pandas.read_csv".into(),
            features: vec!["sepal_length".into(), "sepal_width".into()],
            target: TargetSpec::Single("species".into()),
        }
    }

    fn small_graph(consumer_output: i64) -> ProcessGraph {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("ds".into(), op_node("dorian.io.dataset"));
        nodes.insert("clf".into(), op_node("sklearn.linear_model.LogisticRegression"));
        let edges = vec![Edge {
            source: "ds".into(),
            destination: "clf".into(),
            position: Position::Index(0),
            output: Position::Index(consumer_output),
            delivery_mode: Default::default(),
        }];
        ProcessGraph { nodes, edges }
    }

    #[test]
    fn no_op_when_no_placeholder() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("a".into(), op_node("sklearn.preprocessing.StandardScaler"));
        let g = ProcessGraph { nodes, edges: vec![] };
        let out = expand_dataset_refs(g.clone(), &meta());
        assert_eq!(out.nodes.len(), 1);
        assert!(out.nodes.contains_key("a"));
    }

    #[test]
    fn single_dataframe_fanout_when_no_y_consumer() {
        // consumer reads output=0 only — no split_xy needed.
        let g = small_graph(0);
        let out = expand_dataset_refs(g, &meta());
        // ds removed; fpath + loader added; total 3 nodes (loader, fpath, clf).
        assert_eq!(out.nodes.len(), 3);
        assert!(out.nodes.contains_key("fpath_ds"));
        assert!(out.nodes.contains_key("loader_ds"));
        assert!(!out.nodes.contains_key("split_xy_ds"));
        // Loader → clf edge preserved.
        assert!(out.edges.iter().any(|e|
            e.source == "loader_ds" && e.destination == "clf"
        ));
    }

    #[test]
    fn split_xy_inserted_when_consumer_reads_y() {
        let g = small_graph(1);
        let out = expand_dataset_refs(g, &meta());
        // 5 nodes: fpath, loader, features, target, split_xy + clf.
        assert_eq!(out.nodes.len(), 6);
        assert!(out.nodes.contains_key("split_xy_ds"));
        assert!(out.nodes.contains_key("features_ds"));
        assert!(out.nodes.contains_key("target_ds"));
        // Outgoing y rewired from split_xy output=1.
        let y = out
            .edges
            .iter()
            .find(|e| e.destination == "clf")
            .expect("clf edge");
        assert_eq!(y.source, "split_xy_ds");
        assert_eq!(y.output, Position::Index(1));
    }

    #[test]
    fn features_repr_round_trips() {
        assert_eq!(features_repr(&[]), "[]");
        assert_eq!(
            features_repr(&["a".to_string(), "b".to_string()]),
            "['a', 'b']"
        );
        assert_eq!(
            features_repr(&["it's".to_string()]),
            r"['it\'s']",
        );
    }

    #[test]
    fn target_repr_handles_all_shapes() {
        assert_eq!(TargetSpec::None.to_repr(), "''");
        assert_eq!(TargetSpec::Single("y".into()).to_repr(), "'y'");
        assert_eq!(
            TargetSpec::Many(vec!["a".into(), "b".into()]).to_repr(),
            "['a', 'b']"
        );
    }
}
