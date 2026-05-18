//! Python ↔ Rust parser parity test.
//!
//! Runs `uv run python` against the real `_parse_pipeline` helper in
//! `dorian/pipeline/dag_analysis.py`, dumps a canonical signature of
//! the parsed DAG, then parses the same JSON through
//! `graph::parse_pipeline_json` and asserts they agree.
//!
//! The signature captured is intentionally small: sorted node ids
//! with their class + name, and sorted `(source, destination,
//! position_str, output_int)` edge tuples. That's enough to catch
//! silent drift in the Rust parser without pinning it to Python-
//! internal types.
//!
//! The test is `#[ignore]` by default because it depends on an
//! installed `uv` + working Python environment. Run with:
//!
//! ```bash
//! cd engine
//! cargo test -p graph --test parity -- --ignored
//! ```

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Command;

use serde_json::Value;

use graph::{parse_pipeline_json, Node};

fn repo_root() -> PathBuf {
    // engine/graph/tests/parity.rs → ../../../ is repo root.
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // engine/graph
    p.pop(); // engine
    p
}

fn pipeline_fixture() -> PathBuf {
    repo_root().join(".data/app/766398ff-8100-4d30-a81c-8aea2b3d0ca7/pipeline.json")
}

/// Parse via Python's `_parse_pipeline`, dump a canonical signature.
fn python_signature(pipeline_path: &PathBuf) -> (BTreeMap<String, (String, String)>, Vec<(String, String, String, i64)>) {
    let script = r#"
import json
import sys
from dorian.pipeline.dag_analysis import _parse_pipeline

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
dag = _parse_pipeline(data, flatten_groups=False)

nodes_sig = {}
for nid, node in dag.nodes.items():
    class_name = node.__class__.__name__
    name = getattr(node, "name", None) or getattr(node, "text", "") or nid
    nodes_sig[nid] = (class_name, name)

edges_sig = []
for e in dag.edges:
    pos = str(e.position)
    out = int(e.output) if isinstance(e.output, int) else int(str(e.output))
    edges_sig.append((e.source, e.destination, pos, out))

sys.stdout.write(json.dumps({
    "nodes": nodes_sig,
    "edges": edges_sig,
}))
"#;

    let output = Command::new("uv")
        .args(["run", "python", "-c", script, pipeline_path.to_str().unwrap()])
        .current_dir(repo_root())
        .output()
        .expect("failed to run `uv run python`");
    assert!(
        output.status.success(),
        "python parse failed: stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );
    let v: Value = serde_json::from_slice(&output.stdout)
        .expect("python parse produced non-JSON output");

    let mut nodes_sig = BTreeMap::new();
    for (id, val) in v.get("nodes").and_then(Value::as_object).unwrap() {
        let arr = val.as_array().unwrap();
        let class_name = arr[0].as_str().unwrap().to_string();
        let name = arr[1].as_str().unwrap().to_string();
        nodes_sig.insert(id.clone(), (class_name, name));
    }
    let mut edges_sig: Vec<(String, String, String, i64)> = v
        .get("edges")
        .and_then(Value::as_array)
        .unwrap()
        .iter()
        .map(|e| {
            let a = e.as_array().unwrap();
            (
                a[0].as_str().unwrap().to_string(),
                a[1].as_str().unwrap().to_string(),
                a[2].as_str().unwrap().to_string(),
                a[3].as_i64().unwrap(),
            )
        })
        .collect();
    edges_sig.sort();
    (nodes_sig, edges_sig)
}

fn rust_signature(pipeline_path: &PathBuf) -> (BTreeMap<String, (String, String)>, Vec<(String, String, String, i64)>) {
    let raw = std::fs::read_to_string(pipeline_path).unwrap();
    let value: Value = serde_json::from_str(&raw).unwrap();
    let (graph, _) = parse_pipeline_json(&value).unwrap();

    let mut nodes = BTreeMap::new();
    for (id, node) in &graph.nodes {
        let (class_name, name) = match node {
            Node::Operator(o) => ("Operator".to_string(), o.name.clone()),
            Node::Snippet(s) => ("Snippet".to_string(), s.name.clone()),
            Node::Parameter(p) => ("Parameter".to_string(), p.name.clone()),
            Node::Group(g) => ("Group".to_string(), g.name.clone()),
            Node::Node(n) => ("Node".to_string(), n.text.clone()),
        };
        nodes.insert(id.clone(), (class_name, name));
    }
    let mut edges: Vec<(String, String, String, i64)> = graph
        .edges
        .iter()
        .map(|e| {
            let pos = match &e.position {
                graph::Position::Index(i) => i.to_string(),
                graph::Position::Keyword(k) => k.clone(),
            };
            let out = match &e.output {
                graph::Position::Index(i) => *i,
                graph::Position::Keyword(_) => 0,
            };
            (e.source.clone(), e.destination.clone(), pos, out)
        })
        .collect();
    edges.sort();
    (nodes, edges)
}

#[test]
#[ignore]
fn python_rust_parser_parity_housing() {
    let path = pipeline_fixture();
    if !path.exists() {
        eprintln!("fixture missing at {path:?} — skipping");
        return;
    }
    let (py_nodes, py_edges) = python_signature(&path);
    let (rs_nodes, rs_edges) = rust_signature(&path);
    assert_eq!(
        py_nodes, rs_nodes,
        "node signatures diverge between Python _parse_pipeline and Rust parse_pipeline_json"
    );
    assert_eq!(
        py_edges, rs_edges,
        "edge signatures diverge"
    );
}
