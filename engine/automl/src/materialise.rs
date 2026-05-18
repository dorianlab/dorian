//! Suggestion → concrete pipeline materialiser.
//!
//! Stage 2 of the AutoML driver. After SmacOptimizer.ask returns
//! N Suggestions, we have to bind each one to the template — replace
//! every LogicalTask placeholder node with the chosen Operator + the
//! hyperparameter Parameter nodes the Suggestion specifies.
//!
//! The result is a fully-bound DAG that the runner can execute
//! without further binding. It writes to the `doc_pipelines`
//! table (which mirror-syncs into `pipelines` via the existing
//! trigger) so the bridge worker can find it by id when the trial
//! pops off `task_queue`.
//!
//! Edge wiring rules:
//!
//!   * Every original edge that targeted the LogicalTask node now
//!     targets the chosen Operator instead. Position / output stay
//!     the same (data flow doesn't care which concrete operator
//!     fills the slot — the slot's I/O signature is uniform).
//!
//!   * Every original edge that originated from the LogicalTask
//!     node now originates from the chosen Operator. Same.
//!
//!   * For each hyperparameter binding, add a fresh Parameter node
//!     plus an edge `Parameter → Operator` with `position=<param_name>`
//!     (keyword arg). That mirrors how the Python parser produces
//!     Parameters during pipeline expansion.

use serde_json::{json, Value};
use uuid::Uuid;

use crate::config::ParamValue;
use crate::optimizer::Suggestion;

/// Take a template DAG (JSONB shape: `{nodes: {id: node}, edges: [...]}`) +
/// a Suggestion that maps LogicalTask `task_path` to chosen
/// (op_fqn, params), and produce a concrete DAG with no
/// LogicalTask nodes left. Returns the new DAG as `serde_json::Value`.
///
/// The caller is responsible for inserting the resulting DAG into
/// the pipelines table + enqueueing the trial. This function is
/// pure — no DB access.
pub fn materialise(template_dag: &Value, suggestion: &Suggestion) -> Result<Value, String> {
    let mut nodes = template_dag
        .get("nodes")
        .and_then(|v| v.as_object())
        .cloned()
        .ok_or_else(|| "template DAG missing nodes object".to_string())?;
    let mut edges: Vec<Value> = template_dag
        .get("edges")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    // Step 1: identify every LogicalTask node and the binding that
    // applies to it. Build a (logical_node_id → operator_node_id +
    // param_node_ids) map so the edge rewiring is straightforward.
    let mut id_replacements: rustc_hash::FxHashMap<String, String> =
        rustc_hash::FxHashMap::default();
    let mut new_param_edges: Vec<Value> = Vec::new();
    let mut nodes_to_drop: Vec<String> = Vec::new();
    let mut nodes_to_add: rustc_hash::FxHashMap<String, Value> = rustc_hash::FxHashMap::default();

    // Slots that pick the identity sentinel are dropped entirely;
    // the slot's data edges get short-circuited (each (src→LT) edge
    // is rewritten to point at every (LT→dst) destination, with the
    // dst's `position` preserved). The remaining edges from/to the
    // LT node are then deleted.
    let mut nodes_to_skip: rustc_hash::FxHashSet<String> = rustc_hash::FxHashSet::default();

    for (node_id, node_val) in &nodes {
        let class_type = node_val
            .get("class_type")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if class_type != "LogicalTask" {
            continue;
        }
        let path: Vec<String> = node_val
            .get("path")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|p| p.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default();
        let dotted = path.join(".");
        let binding = match suggestion.bindings.get(&dotted) {
            Some(b) => b,
            None => {
                // No suggestion entry for this slot — caller forgot
                // to feed every slot's task_path. Surface the
                // mismatch loudly rather than ship a half-bound DAG.
                return Err(format!(
                    "suggestion has no binding for slot {dotted:?} (node {node_id})"
                ));
            }
        };

        // Identity bypass — drop the slot, the rewiring is done in
        // a second pass after we know every LT→identity decision.
        if binding.op_fqn == crate::driver::IDENTITY_OP_FQN {
            nodes_to_skip.insert(node_id.clone());
            nodes_to_drop.push(node_id.clone());
            continue;
        }

        // New Operator node replacing the LogicalTask. Reuse the
        // node id so existing edges naturally swing to the new
        // operator.
        let op_node = json!({
            "class_type": "Operator",
            "name": binding.op_fqn,
            "language": "python",
        });
        nodes_to_add.insert(node_id.clone(), op_node);
        nodes_to_drop.push(node_id.clone());
        id_replacements.insert(node_id.clone(), node_id.clone()); // identity

        // Parameter nodes for each hyperparam binding + keyword
        // edge into the operator. Empty-string fallbacks (the result
        // of KB params declared as ``dtype: "any"`` with no default
        // — they enter the optimizer's search space as
        // ``ParamDomain::Constant(Str(""))``) get skipped: sklearn
        // refuses to coerce ``""`` into ``bool``/``int``, and the
        // operator's own default is the correct binding anyway.
        for (param_name, param_value) in &binding.params {
            if matches!(param_value, ParamValue::Str(s) if s.is_empty()) {
                continue;
            }
            let param_id = format!("p_{}", Uuid::new_v4());
            let (dtype, value) = param_value_to_dtype_value(param_value);
            nodes_to_add.insert(
                param_id.clone(),
                json!({
                    "class_type": "Parameter",
                    "name": param_name,
                    "dtype": dtype,
                    "value": value,
                }),
            );
            new_param_edges.push(json!({
                "source": param_id,
                "destination": node_id,
                "position": param_name,
                "output": 0,
            }));
        }
    }

    // Step 2a: short-circuit identity slots. Chains of consecutive
    // identity LTs are contracted transitively — for each non-skip
    // edge whose ``destination`` is a skipped LT, we trace forward
    // through the skipped chain to the first non-skipped consumer;
    // similarly for sources. The bypass edge carries the upstream
    // output index and the downstream position so port semantics
    // survive the contraction.
    if !nodes_to_skip.is_empty() {
        let original_edges = std::mem::take(&mut edges);
        let mut bypass_edges: Vec<Value> = Vec::new();

        // Pre-index outgoing edges per skipped LT so the trace
        // doesn't quadratic-scan the whole edge list per hop.
        let mut outgoing_by_node: rustc_hash::FxHashMap<&str, Vec<&Value>> =
            rustc_hash::FxHashMap::default();
        for e in &original_edges {
            if let Some(src) = e.get("source").and_then(|v| v.as_str()) {
                if nodes_to_skip.contains(src) {
                    outgoing_by_node.entry(src).or_default().push(e);
                }
            }
        }

        // Recursively trace through skipped chain, collecting
        // (final_destination, final_position) tuples. Cycles are
        // impossible by construction (DAG), but cap depth defensively.
        fn trace<'a>(
            current: &'a str,
            inherited_pos: &'a Value,
            skipped: &rustc_hash::FxHashSet<String>,
            outgoing: &rustc_hash::FxHashMap<&'a str, Vec<&'a Value>>,
            depth: usize,
            sink: &mut Vec<(String, Value)>,
        ) {
            if depth > 32 {
                return; // safety cap
            }
            if !skipped.contains(current) {
                sink.push((current.to_string(), inherited_pos.clone()));
                return;
            }
            // Walk every outgoing edge of this skipped node; each
            // hop carries that hop's `position` forward as the new
            // inherited position (the next consumer's slot is what
            // matters; the skipped LT's own position is irrelevant).
            if let Some(out_edges) = outgoing.get(current) {
                for out_e in out_edges {
                    let next = out_e
                        .get("destination")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let next_pos = out_e
                        .get("position")
                        .cloned()
                        .unwrap_or(json!(0));
                    trace(next, &next_pos, skipped, outgoing, depth + 1, sink);
                }
            }
        }

        for e in &original_edges {
            let src = e.get("source").and_then(|v| v.as_str()).unwrap_or("");
            let dst = e.get("destination").and_then(|v| v.as_str()).unwrap_or("");
            // Edges originating from a skipped LT are emitted by the
            // contraction trace below (driven from the upstream-non-
            // skipped sources), so skip them here.
            if nodes_to_skip.contains(src) {
                continue;
            }
            if !nodes_to_skip.contains(dst) {
                bypass_edges.push(e.clone());
                continue;
            }
            // ``e`` enters a skipped LT. If the upstream source is a
            // Parameter, the param feeder is dropped (identity has
            // no params). For data edges, trace through the skipped
            // chain to the first non-skipped consumer and emit one
            // bypass edge per terminal destination.
            let src_is_param = nodes
                .get(src)
                .and_then(|n| n.get("class_type"))
                .and_then(|v| v.as_str())
                == Some("Parameter");
            if src_is_param {
                continue;
            }
            let mut sinks: Vec<(String, Value)> = Vec::new();
            // The first hop's ``inherited_pos`` is the original
            // edge's position — but since the LT is being skipped,
            // we re-inherit at the trace's hop-by-hop boundary.
            // Start the trace from this LT (not e.dst) so each
            // outgoing hop carries its own position forward.
            trace(
                dst,
                &json!(0), // placeholder; trace overwrites at hop boundaries
                &nodes_to_skip,
                &outgoing_by_node,
                0,
                &mut sinks,
            );
            for (final_dst, final_pos) in sinks {
                bypass_edges.push(json!({
                    "source": e.get("source").cloned().unwrap_or(json!(null)),
                    "output": e.get("output").cloned().unwrap_or(json!(0)),
                    "destination": final_dst,
                    "position": final_pos,
                }));
            }
        }
        edges = bypass_edges;
    }

    // Step 2b: apply node-level replacements + add the param edges.
    for nid in &nodes_to_drop {
        nodes.remove(nid);
    }
    for (nid, val) in nodes_to_add {
        nodes.insert(nid, val);
    }
    edges.extend(new_param_edges);

    Ok(json!({
        "nodes": nodes,
        "edges": edges,
    }))
}

fn param_value_to_dtype_value(v: &ParamValue) -> (&'static str, String) {
    match v {
        ParamValue::Int(n) => ("int", n.to_string()),
        ParamValue::Float(n) => ("float", format!("{n}")),
        ParamValue::Bool(b) => ("bool", if *b { "True".into() } else { "False".into() }),
        ParamValue::Str(s) => ("string", s.clone()),
    }
}


#[cfg(test)]
mod tests {
    use super::*;
    use rustc_hash::FxHashMap;
    use crate::config::ParamValue;
    use crate::optimizer::SlotBinding;

    fn template_dag() -> Value {
        json!({
            "nodes": {
                "p_path": {
                    "class_type": "Parameter",
                    "name": "filepath_or_buffer", "dtype": "str",
                    "value": "/data/credit-g.csv",
                },
                "reader": {
                    "class_type": "Operator",
                    "name": "pandas.read_csv", "language": "python",
                },
                "preproc": {
                    "class_type": "LogicalTask",
                    "path": ["Preprocessing", "Imputation"],
                    "name": "Preprocessing.Imputation",
                },
                "clf": {
                    "class_type": "LogicalTask",
                    "path": ["Modeling", "Classification"],
                    "name": "Modeling.Classification",
                },
            },
            "edges": [
                {"source": "p_path", "destination": "reader", "position": 0, "output": 0},
                {"source": "reader", "destination": "preproc", "position": 0, "output": 0},
                {"source": "preproc", "destination": "clf", "position": 0, "output": 0},
            ],
        })
    }

    fn suggestion_for_template() -> Suggestion {
        let mut bindings: FxHashMap<String, SlotBinding> = FxHashMap::default();
        let mut preproc_params = FxHashMap::default();
        preproc_params.insert("strategy".into(), ParamValue::Str("median".into()));
        bindings.insert(
            "Preprocessing.Imputation".into(),
            SlotBinding {
                op_fqn: "sklearn.impute.SimpleImputer".into(),
                params: preproc_params,
            },
        );
        let mut clf_params = FxHashMap::default();
        clf_params.insert("C".into(), ParamValue::Float(0.5));
        clf_params.insert("max_iter".into(), ParamValue::Int(200));
        bindings.insert(
            "Modeling.Classification".into(),
            SlotBinding {
                op_fqn: "sklearn.linear_model.LogisticRegression".into(),
                params: clf_params,
            },
        );
        Suggestion { bindings }
    }

    #[test]
    fn materialise_replaces_logical_tasks_with_operators() {
        let dag = template_dag();
        let sugg = suggestion_for_template();
        let bound = materialise(&dag, &sugg).expect("materialise");
        let nodes = bound.get("nodes").unwrap().as_object().unwrap();
        // No LogicalTask nodes left.
        for (_id, node) in nodes {
            assert_ne!(
                node.get("class_type").and_then(|v| v.as_str()),
                Some("LogicalTask"),
                "leftover LogicalTask node",
            );
        }
        // Original Operator + Parameter nodes preserved.
        assert!(nodes.contains_key("p_path"));
        assert!(nodes.contains_key("reader"));
        // LogicalTask ids reused for the chosen operator (preserves edges).
        assert_eq!(
            nodes["preproc"]["name"].as_str(),
            Some("sklearn.impute.SimpleImputer"),
        );
        assert_eq!(
            nodes["clf"]["name"].as_str(),
            Some("sklearn.linear_model.LogisticRegression"),
        );
    }

    #[test]
    fn materialise_preserves_data_edges() {
        let dag = template_dag();
        let sugg = suggestion_for_template();
        let bound = materialise(&dag, &sugg).expect("materialise");
        let edges = bound.get("edges").unwrap().as_array().unwrap();
        // Every original data edge survives unchanged (LogicalTask
        // ids reused, so reader→preproc still points at the now-
        // SimpleImputer node).
        let has_reader_to_preproc = edges.iter().any(|e| {
            e["source"].as_str() == Some("reader") && e["destination"].as_str() == Some("preproc")
        });
        assert!(has_reader_to_preproc, "reader→preproc edge missing");
    }

    #[test]
    fn materialise_adds_parameter_nodes_per_binding() {
        let dag = template_dag();
        let sugg = suggestion_for_template();
        let bound = materialise(&dag, &sugg).expect("materialise");
        let nodes = bound.get("nodes").unwrap().as_object().unwrap();
        // 3 hyperparam Parameters added (strategy, C, max_iter).
        let mut hp_count = 0;
        for (_id, node) in nodes {
            if node.get("class_type").and_then(|v| v.as_str()) == Some("Parameter") {
                let name = node.get("name").and_then(|v| v.as_str()).unwrap_or("");
                if name == "strategy" || name == "C" || name == "max_iter" {
                    hp_count += 1;
                }
            }
        }
        assert_eq!(hp_count, 3);
    }

    #[test]
    fn identity_binding_bypasses_logical_task() {
        // template: reader → preproc(LT) → clf(LT)
        // Suggest identity for preproc, real op for clf. After
        // materialise: reader → clf, preproc node + edges removed.
        let dag = template_dag();
        let mut bindings: FxHashMap<String, SlotBinding> = FxHashMap::default();
        bindings.insert(
            "Preprocessing.Imputation".into(),
            SlotBinding {
                op_fqn: crate::driver::IDENTITY_OP_FQN.into(),
                params: FxHashMap::default(),
            },
        );
        let mut clf_params = FxHashMap::default();
        clf_params.insert("C".into(), ParamValue::Float(0.5));
        bindings.insert(
            "Modeling.Classification".into(),
            SlotBinding {
                op_fqn: "sklearn.linear_model.LogisticRegression".into(),
                params: clf_params,
            },
        );
        let bound = materialise(&dag, &Suggestion { bindings }).expect("materialise");
        let nodes = bound.get("nodes").unwrap().as_object().unwrap();
        // preproc node is gone.
        assert!(!nodes.contains_key("preproc"), "skipped LT must be removed");
        // clf still present and bound to LR.
        assert_eq!(
            nodes["clf"]["name"].as_str(),
            Some("sklearn.linear_model.LogisticRegression"),
        );
        let edges = bound.get("edges").unwrap().as_array().unwrap();
        // No edge targeting or sourced from preproc.
        for e in edges {
            assert_ne!(e["source"].as_str(), Some("preproc"));
            assert_ne!(e["destination"].as_str(), Some("preproc"));
        }
        // Bypass edge created: reader → clf with reader's output preserved.
        let has_bypass = edges
            .iter()
            .any(|e| e["source"] == "reader" && e["destination"] == "clf");
        assert!(has_bypass, "expected reader→clf bypass edge");
    }

    #[test]
    fn chained_identity_skips_contract_transitively() {
        // Three-slot chain: reader → a → b → clf
        // Slots a and b both pick identity; clf picks a real op.
        // The bypass must produce one edge reader → clf, no leftover
        // references to a or b.
        let dag = json!({
            "nodes": {
                "reader": {"class_type": "Operator", "name": "pandas.read_csv", "language": "python"},
                "a": {"class_type": "LogicalTask", "path": ["Missing Data Imputation"], "name": "a"},
                "b": {"class_type": "LogicalTask", "path": ["Data Normalization"], "name": "b"},
                "clf": {"class_type": "LogicalTask", "path": ["Classification"], "name": "clf"},
            },
            "edges": [
                {"source": "reader", "destination": "a", "position": 0, "output": 0},
                {"source": "a", "destination": "b", "position": 0, "output": 0},
                {"source": "b", "destination": "clf", "position": 0, "output": 0},
            ],
        });
        let mut bindings: FxHashMap<String, SlotBinding> = FxHashMap::default();
        bindings.insert(
            "Missing Data Imputation".into(),
            SlotBinding {
                op_fqn: crate::driver::IDENTITY_OP_FQN.into(),
                params: FxHashMap::default(),
            },
        );
        bindings.insert(
            "Data Normalization".into(),
            SlotBinding {
                op_fqn: crate::driver::IDENTITY_OP_FQN.into(),
                params: FxHashMap::default(),
            },
        );
        bindings.insert(
            "Classification".into(),
            SlotBinding {
                op_fqn: "sklearn.svm.SVC".into(),
                params: FxHashMap::default(),
            },
        );
        let bound = materialise(&dag, &Suggestion { bindings }).expect("materialise");
        let nodes = bound.get("nodes").unwrap().as_object().unwrap();
        // Both skipped LTs gone, clf bound to SVC.
        assert!(!nodes.contains_key("a"));
        assert!(!nodes.contains_key("b"));
        assert_eq!(nodes["clf"]["name"].as_str(), Some("sklearn.svm.SVC"));
        let edges = bound.get("edges").unwrap().as_array().unwrap();
        // No edge can reference a or b in either endpoint.
        for e in edges {
            assert_ne!(e["source"].as_str(), Some("a"));
            assert_ne!(e["source"].as_str(), Some("b"));
            assert_ne!(e["destination"].as_str(), Some("a"));
            assert_ne!(e["destination"].as_str(), Some("b"));
        }
        // Single bypass edge reader → clf must exist.
        let count = edges
            .iter()
            .filter(|e| e["source"] == "reader" && e["destination"] == "clf")
            .count();
        assert_eq!(count, 1, "expected exactly one reader→clf bypass edge");
    }

    #[test]
    fn missing_suggestion_for_slot_errors_loudly() {
        let dag = template_dag();
        // Build a suggestion that's missing the classifier slot.
        let mut bindings: FxHashMap<String, SlotBinding> = FxHashMap::default();
        let mut params = FxHashMap::default();
        params.insert("strategy".into(), ParamValue::Str("mean".into()));
        bindings.insert(
            "Preprocessing.Imputation".into(),
            SlotBinding {
                op_fqn: "sklearn.impute.SimpleImputer".into(),
                params,
            },
        );
        let sugg = Suggestion { bindings };
        let err = materialise(&dag, &sugg).expect_err("should error");
        assert!(err.contains("Modeling.Classification"));
    }
}
