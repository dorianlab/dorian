//! `dorian.io.printout` — terminal display-formatter expansion.
//!
//! Every ``dorian.io.printout`` Operator becomes a Snippet whose
//! inline ``foo(data)`` body auto-detects the data shape (LLM
//! response, DataFrame, ndarray, scalar, dict, str) and returns
//! a structured dict the frontend's VisualizerNode renders.
//!
//! Mirrors `dorian.pipeline.printout._expand_printout` byte-for-
//! byte. Pure graph mutation — incoming + outgoing edges are
//! preserved (printout is typically terminal but we don't enforce
//! that here).

use crate::model::{Edge, Node, NodeId, Operator, ProcessGraph, Snippet};

const PRINTOUT_SNIPPET: &str = r#"def foo(data):
    """Format pipeline output for display.

    Auto-detects the data type and returns a structured dict:
        {type: str, content: ..., ...metadata}

    Handles OpenAI-compatible ChatCompletion responses, dicts,
    DataFrames, numpy arrays, scalars, and strings.
    """
    import json as _json

    # -- Pydantic model (OpenRouter SDK, OpenAI SDK, etc.) --
    if hasattr(data, "model_dump"):
        return {"type": "json", "content": data.model_dump()}

    # -- object with .to_dict() (older SDKs) --
    if hasattr(data, "to_dict") and not hasattr(data, "columns"):
        return {"type": "json", "content": data.to_dict()}

    # -- dict --
    if isinstance(data, dict):
        return {"type": "json", "content": data}

    # -- DataFrame (pandas) --
    if hasattr(data, "to_dict") and hasattr(data, "columns"):
        rows = data.head(100).to_dict(orient="records")
        return {
            "type": "dataframe",
            "content": rows,
            "shape": list(data.shape),
            "columns": list(data.columns),
        }

    # -- ndarray (numpy) --
    if hasattr(data, "tolist") and hasattr(data, "shape") and hasattr(data, "dtype"):
        flat = data.flatten().tolist()[:100]
        return {
            "type": "array",
            "content": flat,
            "shape": list(data.shape),
            "dtype": str(data.dtype),
        }

    # -- list / tuple --
    if isinstance(data, (list, tuple)):
        items = list(data)[:100]
        return {"type": "json", "content": items}

    # -- scalar --
    if isinstance(data, (int, float, bool)):
        return {"type": "scalar", "content": data}

    # -- string (try JSON parse) --
    if isinstance(data, str):
        try:
            parsed = _json.loads(data)
            return {"type": "json", "content": parsed}
        except (ValueError, TypeError):
            pass
        return {"type": "text", "content": data}

    # -- fallback --
    return {"type": "text", "content": str(data)}
"#;

pub fn expand_printout_nodes(graph: ProcessGraph) -> ProcessGraph {
    let placeholders: Vec<NodeId> = graph
        .nodes
        .iter()
        .filter_map(|(id, node)| match node {
            Node::Operator(Operator { name, .. }) if name == "dorian.io.printout" => {
                Some(id.clone())
            }
            _ => None,
        })
        .collect();
    if placeholders.is_empty() {
        return graph;
    }

    let mut nodes = graph.nodes;
    let mut edges = graph.edges;

    for nid in placeholders {
        let incoming: Vec<_> = edges
            .iter()
            .filter(|e| e.destination == nid)
            .map(|e| (e.source.clone(), e.position.clone(), e.output.clone()))
            .collect();
        let outgoing: Vec<_> = edges
            .iter()
            .filter(|e| e.source == nid)
            .map(|e| (e.destination.clone(), e.position.clone(), e.output.clone()))
            .collect();

        nodes.remove(&nid);
        edges.retain(|e| e.source != nid && e.destination != nid);

        let snippet_id = format!("printout_{nid}");
        nodes.insert(
            snippet_id.clone(),
            Node::Snippet(Snippet {
                name: "dorian.io.printout".into(),
                code: PRINTOUT_SNIPPET.into(),
                language: "python".into(),
            }),
        );

        for (src, pos, out) in incoming {
            edges.push(Edge {
                source: src,
                destination: snippet_id.clone(),
                position: pos,
                output: out,
                delivery_mode: Default::default(),
            });
        }
        for (dst, pos, out) in outgoing {
            edges.push(Edge {
                source: snippet_id.clone(),
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
    use crate::model::Position;
    use rustc_hash::FxHashMap;

    fn op(name: &str) -> Node {
        Node::Operator(Operator {
            name: name.into(),
            language: "python".into(),
            tasks: Vec::new(),
        })
    }

    #[test]
    fn replaces_printout_and_preserves_edges() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("clf".into(), op("sklearn.linear_model.LogisticRegression"));
        nodes.insert("po".into(), op("dorian.io.printout"));
        let edges = vec![Edge {
            source: "clf".into(),
            destination: "po".into(),
            position: Position::Index(0),
            output: Position::Index(1),
            delivery_mode: Default::default(),
        }];
        let g = ProcessGraph { nodes, edges };
        let out = expand_printout_nodes(g);
        assert_eq!(out.nodes.len(), 2);
        assert!(out.nodes.contains_key("printout_po"));
        assert!(matches!(out.nodes["printout_po"], Node::Snippet(_)));
        let edge = out
            .edges
            .iter()
            .find(|e| e.destination == "printout_po")
            .expect("incoming");
        assert_eq!(edge.source, "clf");
        assert_eq!(edge.output, Position::Index(1));
    }

    #[test]
    fn no_op_when_no_printout() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("a".into(), op("sklearn.preprocessing.StandardScaler"));
        let g = ProcessGraph {
            nodes,
            edges: vec![],
        };
        let out = expand_printout_nodes(g);
        assert_eq!(out.nodes.len(), 1);
        assert!(out.nodes.contains_key("a"));
    }
}
