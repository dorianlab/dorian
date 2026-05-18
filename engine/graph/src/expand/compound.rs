//! Compound (class-interface) operator expansion — rust port.
//!
//! Mirrors the `_route_data_edges_by_kb` / `_route_outgoing_edges`
//! halves of `dorian.pipeline.transforms._expand_compound_operator`
//! for the **non-passthrough KB-driven path** — the common case
//! covering sklearn transformers + estimators (StandardScaler,
//! LogisticRegression, RandomForestClassifier, …).
//!
//! What rust handles:
//!   * Class operators with KB interface + method sequence ≥ 2
//!   * Per-method I/O present in the KB (no introspection fallback)
//!   * Non-passthrough interfaces (no Guardrail-style snippet inject)
//!
//! What stays in python (caller-side):
//!   * Passthrough interfaces (Guardrail's __init__ + Snippet)
//!   * Operators with no KB interface or methods < 2 (KB-seeding gaps)
//!   * Method I/O absent → introspection-based fit_arity fallback
//!   * Function interfaces (no expansion needed at all)
//!
//! The python facade walks all candidates, builds a record list for
//! the rust-eligible ones, calls this entry, then runs the python
//! `sync_apply` rule on the result so the leftover edge cases get
//! their original treatment. Rust-already-expanded methods carry
//! `_cx_` in their ids and KB-known method shortcut names, so the
//! python rule's guards skip them on the second pass.

use rustc_hash::FxHashMap;

use crate::model::{Edge, Node, NodeId, Operator, Position, ProcessGraph};

/// Per-method I/O view derived from the KB.
#[derive(Debug, Clone)]
pub struct MethodIo {
    pub method: String,
    /// (input_name, internal_position) — ``position`` is the slot the
    /// method's own signature exposes (e.g. ``fit.X`` at slot 1).
    pub inputs: Vec<(String, i64)>,
    /// (output_name, position) — order in this Vec defines the
    /// method-local output index, which the python implementation
    /// assigns sequentially across the full method chain to derive
    /// the interface-level ``output`` port.
    pub outputs: Vec<(String, i64)>,
}

/// One eligible compound-operator expansion. The python facade
/// resolves all KB look-ups and packs the result here so rust does
/// no I/O.
#[derive(Debug, Clone)]
pub struct CompoundRecord {
    /// Node id of the placeholder Operator to expand.
    pub node_id: NodeId,
    /// Method-chain methods, deduped, in declaration order. First
    /// entry is always ``__init__``.
    pub methods: Vec<String>,
    /// KB parameter declarations: ``(param_name, target_method)``.
    /// Empty when the operator has no KB-declared parameters (all
    /// param edges fall back to ``__init__``).
    pub kb_params: Vec<(String, String)>,
    /// Interface-level input declarations: ``(input_name, external_position)``.
    pub interface_inputs: Vec<(String, i64)>,
    /// Per-method I/O. Methods absent here fall through to the
    /// caller's python introspection fallback — they should never
    /// appear in a record passed to rust (the facade screens them out).
    pub method_io: Vec<MethodIo>,
}

pub fn expand_compound_operators(
    graph: ProcessGraph,
    records: &[CompoundRecord],
) -> ProcessGraph {
    if records.is_empty() {
        return graph;
    }

    let mut nodes = graph.nodes;
    let mut edges = graph.edges;

    for rec in records {
        let nid = &rec.node_id;
        // The op may have been removed by a prior record's expansion
        // touching the same id (shouldn't happen, but defensive).
        let Some(node) = nodes.get(nid).cloned() else {
            continue;
        };
        let Node::Operator(orig_op) = node else {
            continue;
        };
        if rec.methods.len() < 2 {
            continue;
        }

        // Split incoming edges by source-node kind.
        let mut param_edges: Vec<Edge> = Vec::new();
        let mut data_edges: Vec<Edge> = Vec::new();
        for e in edges.iter() {
            if &e.destination != nid {
                continue;
            }
            let is_param = matches!(nodes.get(&e.source), Some(Node::Parameter(_)));
            if is_param {
                param_edges.push(e.clone());
            } else {
                data_edges.push(e.clone());
            }
        }
        // Stable order keyed by edge position to mirror python's
        // ``sorted(..., key=lambda e: e.position)``.
        data_edges.sort_by(|a, b| compare_positions(&a.position, &b.position));

        let out_edges: Vec<Edge> = edges
            .iter()
            .filter(|e| &e.source == nid)
            .cloned()
            .collect();

        // Drop the placeholder + its incident edges.
        edges.retain(|e| &e.source != nid && &e.destination != nid);
        nodes.remove(nid);

        let prefix = format!("{nid}_cx");

        // 1. One Operator node per method in the chain.
        // ``__init__`` keeps the original operator name; subsequent
        // methods become operators with the method's KB-fqn name.
        let mut method_ids: FxHashMap<String, NodeId> = FxHashMap::default();
        for (i, method_name) in rec.methods.iter().enumerate() {
            let mid: NodeId = if method_name == "__init__" {
                format!("{prefix}_init")
            } else {
                format!(
                    "{}_{}_{}",
                    prefix,
                    method_name.replace('.', "_"),
                    i,
                )
            };
            let op_name = if method_name == "__init__" {
                orig_op.name.clone()
            } else {
                method_name.clone()
            };
            nodes.insert(
                mid.clone(),
                Node::Operator(Operator {
                    name: op_name,
                    language: orig_op.language.clone(),
                    tasks: Vec::new(),
                }),
            );
            method_ids.insert(method_name.clone(), mid);
        }

        // 2. Linear instance chaining: method[i].out0 → method[i+1].pos0
        for i in 0..rec.methods.len() - 1 {
            let src_mid = &method_ids[&rec.methods[i]];
            let dst_mid = &method_ids[&rec.methods[i + 1]];
            edges.push(Edge {
                source: src_mid.clone(),
                destination: dst_mid.clone(),
                position: Position::Index(0),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            });
        }

        // 3. Route external data edges using KB per-method I/O.
        route_data_edges_by_kb(&data_edges, rec, &method_ids, &mut edges);

        // 4. Route param edges by KB method declarations.
        let kb_param_map: FxHashMap<&str, &str> = rec
            .kb_params
            .iter()
            .map(|(n, m)| (n.as_str(), m.as_str()))
            .collect();
        let init_id = method_ids[&rec.methods[0]].clone();
        for e in param_edges {
            let pname = match nodes.get(&e.source) {
                Some(Node::Parameter(p)) => Some(p.name.as_str()),
                _ => None,
            };
            let target_mid = pname
                .and_then(|n| kb_param_map.get(n))
                .and_then(|m| method_ids.get(*m))
                .cloned()
                .unwrap_or_else(|| init_id.clone());
            edges.push(Edge {
                source: e.source,
                destination: target_mid,
                position: e.position,
                output: e.output,
                delivery_mode: Default::default(),
            });
        }

        // 5. Rewire outgoing edges from the producing method.
        route_outgoing_edges(&out_edges, rec, &method_ids, &mut edges);
    }

    ProcessGraph { nodes, edges }
}

/// Mirror of ``_route_data_edges_by_kb``: route external data edges
/// to the methods that the KB says consume them, by name.
fn route_data_edges_by_kb(
    data_edges: &[Edge],
    rec: &CompoundRecord,
    method_ids: &FxHashMap<String, NodeId>,
    out_edges: &mut Vec<Edge>,
) {
    // ``ext_pos_to_name``: external interface position → input name.
    // Python normalises positions to both int and str keys (positions
    // arrive from KB as strings but Edge.position is int). We use a
    // canonical Position-keyed map to dodge that.
    let mut ext_pos_to_name: FxHashMap<Position, String> = FxHashMap::default();
    for (name, pos) in &rec.interface_inputs {
        ext_pos_to_name.insert(Position::Index(*pos), name.clone());
        // Also cover the keyword form, for completeness.
        ext_pos_to_name.insert(Position::Keyword(name.clone()), name.clone());
    }

    // ``name_to_targets``: input name → list of (method_name, internal_pos).
    let mut name_to_targets: FxHashMap<&str, Vec<(&str, i64)>> = FxHashMap::default();
    for mio in &rec.method_io {
        for (inp_name, int_pos) in &mio.inputs {
            name_to_targets
                .entry(inp_name.as_str())
                .or_default()
                .push((mio.method.as_str(), *int_pos));
        }
    }

    let fallback_method = &rec.methods[1];

    for e in data_edges {
        let inp_name = ext_pos_to_name.get(&e.position);
        let targets = inp_name
            .and_then(|n| name_to_targets.get(n.as_str()))
            .cloned()
            .unwrap_or_default();

        if !targets.is_empty() {
            for (method_name, int_pos) in targets {
                if let Some(mid) = method_ids.get(method_name) {
                    out_edges.push(Edge {
                        source: e.source.clone(),
                        destination: mid.clone(),
                        position: Position::Index(int_pos),
                        output: e.output.clone(),
                        delivery_mode: Default::default(),
                    });
                }
            }
        } else {
            // Fallback: route to first non-init method at original pos.
            let mid = &method_ids[fallback_method];
            out_edges.push(Edge {
                source: e.source.clone(),
                destination: mid.clone(),
                position: e.position.clone(),
                output: e.output.clone(),
                delivery_mode: Default::default(),
            });
        }
    }
}

/// Mirror of ``_route_outgoing_edges``: producing-method-aware
/// rewire, with ``output=1`` (the method's data result, not the
/// instance) on every emitted edge.
fn route_outgoing_edges(
    out_edges: &[Edge],
    rec: &CompoundRecord,
    method_ids: &FxHashMap<String, NodeId>,
    edges: &mut Vec<Edge>,
) {
    let terminal_mid = &method_ids[rec.methods.last().unwrap()];

    // Cumulative interface-level output position → producing method id.
    let mut output_pos_to_mid: FxHashMap<i64, NodeId> = FxHashMap::default();
    {
        let by_method: FxHashMap<&str, &MethodIo> =
            rec.method_io.iter().map(|m| (m.method.as_str(), m)).collect();
        let mut pos: i64 = 0;
        for method_name in &rec.methods {
            if let Some(mio) = by_method.get(method_name.as_str()) {
                for _ in &mio.outputs {
                    output_pos_to_mid.insert(pos, method_ids[method_name].clone());
                    pos += 1;
                }
            }
        }
    }

    for e in out_edges {
        let out_idx = match &e.output {
            Position::Index(i) => *i,
            // Best-effort coerce keyword → int; non-numeric falls
            // through to terminal.
            Position::Keyword(s) => s.parse::<i64>().unwrap_or(-1),
        };
        let src_mid = output_pos_to_mid
            .get(&out_idx)
            .cloned()
            .unwrap_or_else(|| terminal_mid.clone());
        edges.push(Edge {
            source: src_mid,
            destination: e.destination.clone(),
            position: e.position.clone(),
            output: Position::Index(1),
            delivery_mode: Default::default(),
        });
    }
}

/// Stable comparator for Position so Index/Keyword interleave the
/// same way python's ``sorted(..., key=position)`` does (ints
/// compare numerically, strings lexicographically; we order ints
/// before keywords to mirror python where edges with int positions
/// dominate hyperparameter-style keyword edges in the data-edge
/// path — and data_edges are filtered to the non-Parameter set
/// before sorting anyway, so keywords are unusual here).
fn compare_positions(a: &Position, b: &Position) -> std::cmp::Ordering {
    use std::cmp::Ordering::*;
    match (a, b) {
        (Position::Index(x), Position::Index(y)) => x.cmp(y),
        (Position::Keyword(x), Position::Keyword(y)) => x.cmp(y),
        (Position::Index(_), Position::Keyword(_)) => Less,
        (Position::Keyword(_), Position::Index(_)) => Greater,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ParamDtype, Parameter};

    fn op(name: &str) -> Node {
        Node::Operator(Operator {
            name: name.into(),
            language: "python".into(),
            tasks: Vec::new(),
        })
    }

    fn param(name: &str, value: &str) -> Node {
        Node::Parameter(Parameter {
            name: name.into(),
            dtype: ParamDtype::Int,
            value: value.into(),
        })
    }

    fn standard_scaler_record(node_id: &str) -> CompoundRecord {
        CompoundRecord {
            node_id: node_id.into(),
            methods: vec!["__init__".into(), "fit".into(), "transform".into()],
            kb_params: vec![],
            interface_inputs: vec![("X".into(), 1)],
            method_io: vec![
                MethodIo {
                    method: "fit".into(),
                    inputs: vec![("X".into(), 1)],
                    outputs: vec![],
                },
                MethodIo {
                    method: "transform".into(),
                    inputs: vec![("X".into(), 1)],
                    outputs: vec![("X_out".into(), 1)],
                },
            ],
        }
    }

    #[test]
    fn no_op_when_no_records() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("a".into(), op("sklearn.preprocessing.StandardScaler"));
        let g = ProcessGraph {
            nodes,
            edges: vec![],
        };
        let out = expand_compound_operators(g, &[]);
        assert!(out.nodes.contains_key("a"));
        assert_eq!(out.nodes.len(), 1);
    }

    #[test]
    fn standard_scaler_three_method_chain() {
        // ld → scaler → consumer (consumer reads scaler's transform output)
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("ld".into(), op("pandas.read_csv"));
        nodes.insert("sc".into(), op("sklearn.preprocessing.StandardScaler"));
        nodes.insert("cs".into(), op("downstream"));
        let edges = vec![
            // X edge into scaler at external position 1
            Edge {
                source: "ld".into(),
                destination: "sc".into(),
                position: Position::Index(1),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
            // outgoing: consumer reads scaler's output 0 (interface-level)
            Edge {
                source: "sc".into(),
                destination: "cs".into(),
                position: Position::Index(0),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
        ];
        let g = ProcessGraph { nodes, edges };
        let rec = standard_scaler_record("sc");
        let out = expand_compound_operators(g, &[rec]);

        // Original placeholder gone, three method nodes present.
        assert!(!out.nodes.contains_key("sc"));
        assert!(out.nodes.contains_key("sc_cx_init"));
        assert!(out.nodes.contains_key("sc_cx_fit_1"));
        assert!(out.nodes.contains_key("sc_cx_transform_2"));

        // __init__ keeps the operator name; fit / transform get the
        // method-shortcut name.
        match &out.nodes["sc_cx_init"] {
            Node::Operator(o) => assert_eq!(o.name, "sklearn.preprocessing.StandardScaler"),
            _ => panic!(),
        }
        match &out.nodes["sc_cx_fit_1"] {
            Node::Operator(o) => assert_eq!(o.name, "fit"),
            _ => panic!(),
        }
        match &out.nodes["sc_cx_transform_2"] {
            Node::Operator(o) => assert_eq!(o.name, "transform"),
            _ => panic!(),
        }

        // Instance chaining: init → fit, fit → transform, both at pos 0 / out 0.
        let inst_init_to_fit = out
            .edges
            .iter()
            .find(|e| e.source == "sc_cx_init" && e.destination == "sc_cx_fit_1")
            .expect("init→fit");
        assert_eq!(inst_init_to_fit.position, Position::Index(0));
        assert_eq!(inst_init_to_fit.output, Position::Index(0));
        assert!(out
            .edges
            .iter()
            .any(|e| e.source == "sc_cx_fit_1"
                && e.destination == "sc_cx_transform_2"
                && e.position == Position::Index(0)));

        // Data edge fan-out: ld → fit at internal pos 1, ld → transform at internal pos 1.
        let to_fit = out
            .edges
            .iter()
            .find(|e| e.source == "ld" && e.destination == "sc_cx_fit_1")
            .expect("ld→fit");
        assert_eq!(to_fit.position, Position::Index(1));
        let to_trans = out
            .edges
            .iter()
            .find(|e| e.source == "ld" && e.destination == "sc_cx_transform_2")
            .expect("ld→transform");
        assert_eq!(to_trans.position, Position::Index(1));

        // Outgoing: sc → cs (output=0) becomes transform → cs (output=1, the
        // method result port).
        let to_consumer = out
            .edges
            .iter()
            .find(|e| e.destination == "cs")
            .expect("transform→cs");
        assert_eq!(to_consumer.source, "sc_cx_transform_2");
        assert_eq!(to_consumer.output, Position::Index(1));
    }

    #[test]
    fn param_edge_routes_to_init_by_default() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("ld".into(), op("pandas.read_csv"));
        nodes.insert(
            "p_ws".into(),
            param("with_mean", "True"),
        );
        nodes.insert("sc".into(), op("sklearn.preprocessing.StandardScaler"));
        let edges = vec![
            Edge {
                source: "ld".into(),
                destination: "sc".into(),
                position: Position::Index(1),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
            Edge {
                source: "p_ws".into(),
                destination: "sc".into(),
                position: Position::Keyword("with_mean".into()),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
        ];
        let g = ProcessGraph { nodes, edges };
        let out = expand_compound_operators(g, &[standard_scaler_record("sc")]);

        // p_ws → init (no kb_params declared → fallback to __init__).
        let pe = out
            .edges
            .iter()
            .find(|e| e.source == "p_ws")
            .expect("param edge");
        assert_eq!(pe.destination, "sc_cx_init");
    }

    #[test]
    fn param_edge_routes_to_kb_declared_method() {
        let mut nodes: FxHashMap<NodeId, Node> = FxHashMap::default();
        nodes.insert("ld".into(), op("pandas.read_csv"));
        nodes.insert("p_eval".into(), param("eval_metric", "logloss"));
        nodes.insert("clf".into(), op("xgboost.XGBClassifier"));
        let edges = vec![
            Edge {
                source: "ld".into(),
                destination: "clf".into(),
                position: Position::Index(1),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
            Edge {
                source: "p_eval".into(),
                destination: "clf".into(),
                position: Position::Keyword("eval_metric".into()),
                output: Position::Index(0),
                delivery_mode: Default::default(),
            },
        ];
        let g = ProcessGraph { nodes, edges };

        // KB declares eval_metric belongs to fit, not __init__.
        let rec = CompoundRecord {
            node_id: "clf".into(),
            methods: vec!["__init__".into(), "fit".into(), "predict".into()],
            kb_params: vec![("eval_metric".into(), "fit".into())],
            interface_inputs: vec![("X".into(), 1)],
            method_io: vec![
                MethodIo {
                    method: "fit".into(),
                    inputs: vec![("X".into(), 1)],
                    outputs: vec![],
                },
                MethodIo {
                    method: "predict".into(),
                    inputs: vec![("X".into(), 1)],
                    outputs: vec![("y_pred".into(), 1)],
                },
            ],
        };
        let out = expand_compound_operators(g, &[rec]);

        let pe = out
            .edges
            .iter()
            .find(|e| e.source == "p_eval")
            .expect("eval_metric edge");
        // Routed to fit (KB-declared), not __init__.
        assert_eq!(pe.destination, "clf_cx_fit_1");
    }

    #[test]
    fn missing_node_skipped_gracefully() {
        let g = ProcessGraph {
            nodes: FxHashMap::default(),
            edges: vec![],
        };
        let out = expand_compound_operators(g, &[standard_scaler_record("ghost")]);
        // No nodes existed; nothing to do.
        assert!(out.nodes.is_empty());
    }
}
