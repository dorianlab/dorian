//! Lower ``graph::ProcessGraph`` → ``task_graph::TaskGraph``.
//!
//! Rust equivalent of the Python side's implicit "flatten to Dask
//! dict" step. By making the boundary explicit we can:
//!
//!   * hand the lowered ``TaskGraph`` to the SDF scheduler (which
//!     eventually replaces ``dask.threaded.get``),
//!   * run optimizer passes on a concrete, serialisable form
//!     (constant-folding, dead-task pruning, fusion — see
//!     ``crate::lowering::prune``),
//!   * validate against a golden-file format in tests without
//!     carrying the full ``ProcessGraph`` shape.
//!
//! Lowering rules:
//!
//!   * ``Operator`` node → ``Task::Call`` with args drawn from the
//!     node's incoming edges, sorted by ``Position`` (index first in
//!     ascending order, keywords preserved as-is for ``**kwargs``
//!     unpacking at resolve time).
//!   * ``Snippet`` node → ``Task::Snippet``. Same arg ordering.
//!   * ``Parameter`` node → ``Task::Value``. The Python dtype-aware
//!     evaluation (``eval(dtype)(value)``) is NOT performed here —
//!     the executor resolves it when the consumer fires, matching
//!     ``operator_resolver.resolve``'s behaviour. We store the raw
//!     tuple ``(dtype, value)`` as a JSON object so the executor has
//!     what it needs.
//!   * ``Group`` node — opaque subgraph, not yet supported here.
//!     Callers must expand groups via the compound-expansion rule
//!     before lowering.

use graph::model::{Edge, Node, ProcessGraph};

use crate::task_graph::{ArgRef, OperatorRef, Task, TaskGraph, TaskKey};

/// Error conditions during lowering. Kept narrow — most structural
/// issues (missing nodes, cycles) are caught earlier by
/// ``graph::topology::validate``; this layer just refuses payloads it
/// can't lower.
#[derive(Debug, thiserror::Error)]
pub enum LoweringError {
    #[error("node {0} has unsupported payload kind Group — expand compound operators first")]
    UnsupportedGroup(TaskKey),
}

/// Lower *graph* into a flat task graph.
///
/// ``roots`` seeds ``TaskGraph::roots`` — if empty, the function
/// marks every leaf (no outgoing edges) as a root so the executor
/// computes the full pipeline. A caller with a narrower target
/// (e.g. "just materialise the dataframe node") passes its own list.
pub fn lower(graph: &ProcessGraph, roots: &[TaskKey]) -> Result<TaskGraph, LoweringError> {
    let mut out = TaskGraph::new();

    for (node_id, node) in graph.nodes.iter() {
        match node {
            Node::Parameter(p) => {
                out.insert(
                    node_id.clone(),
                    Task::Value {
                        value: serde_json::json!({
                            "dtype": format!("{:?}", p.dtype).to_lowercase(),
                            "value": p.value,
                            "name": p.name,
                        }),
                    },
                );
            }
            Node::Operator(op) => {
                let args = collect_args(graph, node_id);
                out.insert(
                    node_id.clone(),
                    Task::Call {
                        op: OperatorRef {
                            name: op.name.clone(),
                            language: op.language.clone(),
                        },
                        args,
                    },
                );
            }
            Node::Snippet(snip) => {
                let args = collect_args(graph, node_id);
                out.insert(
                    node_id.clone(),
                    Task::Snippet {
                        source: snip.code.clone(),
                        args,
                    },
                );
            }
            Node::Group(_) => {
                return Err(LoweringError::UnsupportedGroup(node_id.clone()));
            }
            // The graph model allows a handful of other payload types
            // (PatternNode for rewrite matching, etc.) that don't
            // correspond to executable tasks. Silently skip —
            // lowering only materialises things the executor can fire.
            _ => {}
        }
    }

    if roots.is_empty() {
        for leaf in find_leaves(graph) {
            out.add_root(leaf);
        }
    } else {
        for r in roots {
            out.add_root(r.clone());
        }
    }

    Ok(out)
}

/// Collect the ordered argument refs for a consumer node.
///
/// Edges carry ``position`` (positional index or keyword name) and
/// ``output`` (which slot of a multi-output producer to pick). The
/// task graph's ``ArgRef`` preserves both. We sort positional-index
/// args ascending so ``args[0]`` is position 0, ``args[1]`` position
/// 1, etc.; keyword args are appended afterwards in their natural
/// edge-iteration order (the executor unpacks via name, so their
/// relative order doesn't matter).
fn collect_args(graph: &ProcessGraph, node_id: &str) -> Vec<ArgRef> {
    let incoming: Vec<&Edge> = graph.incoming_edges(node_id);
    let mut positional: Vec<ArgRef> = Vec::new();
    let mut keyword: Vec<ArgRef> = Vec::new();
    for e in incoming {
        let arg = ArgRef::TaskRef {
            task: e.source.clone(),
            output: e.output_index(),
            position: e.position.clone(),
        };
        match &e.position {
            graph::model::Position::Index(_) => positional.push(arg),
            graph::model::Position::Keyword(_) => keyword.push(arg),
        }
    }
    positional.sort_by_key(|a| match a.position() {
        graph::model::Position::Index(i) => *i,
        graph::model::Position::Keyword(_) => i64::MAX,
    });
    positional.extend(keyword);
    positional
}

fn find_leaves(graph: &ProcessGraph) -> Vec<TaskKey> {
    let mut has_outgoing: rustc_hash::FxHashSet<&str> = rustc_hash::FxHashSet::default();
    for e in &graph.edges {
        has_outgoing.insert(e.source.as_str());
    }
    graph
        .nodes
        .keys()
        .filter(|id| !has_outgoing.contains(id.as_str()))
        .cloned()
        .collect()
}

// ---------------------------------------------------------------------------
// Optimizer passes
// ---------------------------------------------------------------------------

/// Drop every task that isn't transitively reached from ``roots``.
///
/// Complements the trainer / dispatcher's cost-aware pruning by
/// doing the cheap dead-code pass at lowering time. Safe — nothing
/// observable depends on unreferenced tasks, and downstream
/// passes (fusion, cache-key derivation) run faster on the smaller
/// graph.
pub fn prune(graph: &mut TaskGraph) {
    use rustc_hash::FxHashSet;
    let mut reachable: FxHashSet<TaskKey> = FxHashSet::default();
    let mut stack: Vec<TaskKey> = graph.roots.clone();
    while let Some(k) = stack.pop() {
        if !reachable.insert(k.clone()) {
            continue;
        }
        for dep in graph.direct_deps(&k) {
            if !reachable.contains(&dep) {
                stack.push(dep);
            }
        }
    }
    graph.tasks.retain(|k, _| reachable.contains(k));
}

#[cfg(test)]
mod tests {
    use super::*;
    use graph::model::{Operator, ParamDtype, Parameter, Position};

    fn make_simple_graph() -> ProcessGraph {
        let mut g = ProcessGraph::new();
        g.add_node(
            "fpath".into(),
            Node::Parameter(Parameter {
                name: "fpath".into(),
                dtype: ParamDtype::String,
                value: "/tmp/x.csv".into(),
            }),
        );
        g.add_node(
            "df".into(),
            Node::Operator(Operator {
                name: "pandas.read_csv".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_edge(Edge {
            source: "fpath".into(),
            destination: "df".into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: Default::default(),
        });
        g
    }

    #[test]
    fn lower_parameter_and_operator() {
        let g = make_simple_graph();
        let tg = lower(&g, &[]).unwrap();
        assert_eq!(tg.tasks.len(), 2);
        assert_eq!(tg.roots, vec!["df".to_string()]);
        match tg.tasks.get("df").unwrap() {
            Task::Call { op, args } => {
                assert_eq!(op.name, "pandas.read_csv");
                assert_eq!(args.len(), 1);
                match &args[0] {
                    ArgRef::TaskRef { task, .. } => assert_eq!(task, "fpath"),
                    _ => panic!("expected TaskRef"),
                }
            }
            _ => panic!("expected Call"),
        }
    }

    #[test]
    fn prune_drops_orphaned_tasks() {
        let mut tg = TaskGraph::new();
        tg.insert(
            "used".into(),
            Task::Value { value: serde_json::json!(1) },
        );
        tg.insert(
            "orphan".into(),
            Task::Value { value: serde_json::json!(2) },
        );
        tg.add_root("used".into());
        prune(&mut tg);
        assert!(tg.tasks.contains_key("used"));
        assert!(!tg.tasks.contains_key("orphan"));
    }
}
