//! docstore / frontend pipeline JSON → DEM-annotated `ProcessGraph`.
//!
//! The stored and in-flight pipeline shapes vary: docstore documents use
//! `"type"` as the node discriminant, the Python `DAG.to_json_dict()`
//! writes `"class_type"`, and React-Flow-nested payloads wrap extra
//! fields under `"data"`. This module normalises all three into the
//! `ProcessGraph` model already defined in `model.rs` and emits a
//! parallel `DemAnnotations` block so schedulers see a single,
//! consistent view.
//!
//! Mirrors the surface of `dorian/pipeline/dag_analysis.py::_parse_pipeline`
//! closely enough that the Rust engine sees the same graph the Python
//! execution path has always seen.

use serde_json::{Map, Value};

use crate::dem::{
    classify_determinism_builtin, classify_domain_builtin,
    classify_random_state_param_builtin, ActorAnnotations, ChannelKey, DemAnnotations,
    DeterminismClass, DomainKind,
};
use crate::model::{
    DeliveryMode, Edge, GraphError, Group, Node, Operator, ParamDtype, Parameter, Position,
    ProcessGraph, Snippet,
};

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

/// Parse a pipeline JSON document into a `ProcessGraph` and its
/// associated DEM annotations.
///
/// Accepts the three in-the-wild shapes:
/// 1. docstore documents   — nodes keyed by `"type"`, flat fields.
/// 2. Python `DAG.to_json_dict()` output — nodes keyed by `"class_type"`.
/// 3. React-Flow payloads  — extra fields under `"data"`.
///
/// Wrapping under a `"pipeline"` key (as the session-meta blob stores
/// it) is also accepted.
pub fn parse_pipeline_json(data: &Value) -> Result<(ProcessGraph, DemAnnotations), GraphError> {
    // Session-meta wrap: `{"pipeline": {...}}` or a JSON-string inside.
    let root = unwrap_pipeline(data)?;

    let nodes_val = root
        .get("nodes")
        .ok_or(GraphError::MissingField("nodes"))?
        .as_object()
        .ok_or_else(|| {
            GraphError::DeserializationError("nodes: expected object".to_string())
        })?;

    let mut graph = ProcessGraph::new();
    let mut dem = DemAnnotations::new();

    for (nid, raw) in nodes_val {
        let node = deserialize_node(raw)?;
        let mut ann = ActorAnnotations::default();
        let fqn_for_domain = match &node {
            Node::Operator(op) => Some(op.name.as_str()),
            _ => None,
        };
        ann.domain = fqn_for_domain
            .map(classify_domain_builtin)
            .unwrap_or(DomainKind::Sdf);
        ann.determinism = classify_determinism_builtin(&node);
        ann.random_state_param_name = fqn_for_domain
            .and_then(classify_random_state_param_builtin);

        graph.add_node(nid.clone(), node);
        dem.actors.insert(nid.clone(), ann);
    }

    let empty_edges = Vec::<Value>::new();
    let edges_val = root
        .get("edges")
        .and_then(|v| v.as_array())
        .unwrap_or(&empty_edges);

    for edge_val in edges_val {
        let edge = deserialize_edge(edge_val)?;
        let channel_key = ChannelKey::from_edge(&edge);
        // ChannelAnnotations default — declared token type Unknown, rate 1.
        dem.channels.entry(channel_key).or_default();
        graph.add_edge(edge);
    }

    Ok((graph, dem))
}

// ---------------------------------------------------------------------------
// Pre-parse helpers
// ---------------------------------------------------------------------------

fn unwrap_pipeline(data: &Value) -> Result<&Value, GraphError> {
    // `{ "pipeline": {...} }` or `{ "pipeline": "{\"nodes\":{...}}" }`.
    // We handle the dict form here; the JSON-string form should be
    // parsed by the caller before reaching us (matches the Python
    // contract in `_parse_pipeline`).
    match data.get("pipeline") {
        Some(Value::Object(_)) => Ok(data.get("pipeline").unwrap()),
        Some(Value::String(_)) => Err(GraphError::DeserializationError(
            "pipeline field is a JSON string; caller must parse it first".to_string(),
        )),
        _ => Ok(data),
    }
}

// ---------------------------------------------------------------------------
// Node deserialisation — mirror of `_resolve_node_fields` in Python
// ---------------------------------------------------------------------------

fn deserialize_node(raw: &Value) -> Result<Node, GraphError> {
    let obj = raw
        .as_object()
        .ok_or_else(|| GraphError::DeserializationError("node: expected object".to_string()))?;

    // Detect Group first — the only node kind with nested structure.
    let type_lower = raw_type(obj).to_ascii_lowercase();
    if type_lower == "group" {
        // Groups accept their dict verbatim; `data.*` nesting is
        // flattened in the Python helper, mirror it here.
        let group_raw = obj
            .get("data")
            .and_then(Value::as_object)
            .unwrap_or(obj);
        let group_val = Value::Object(group_raw.clone());
        let group: Group = serde_json::from_value(group_val)
            .map_err(|e| GraphError::DeserializationError(format!("group: {e}")))?;
        return Ok(Node::Group(group));
    }

    let sub_owned = obj
        .get("data")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_else(Map::new);

    let node_type = normalise_node_type(&raw_type(obj));
    let name = read_str(obj, "name")
        .or_else(|| read_str(&sub_owned, "name"))
        .or_else(|| read_str(obj, "label"))
        .or_else(|| read_str(&sub_owned, "label"))
        .unwrap_or_default();
    let language = read_str(obj, "language")
        .or_else(|| read_str(&sub_owned, "language"))
        .unwrap_or_else(|| "python".to_string());

    match node_type.as_str() {
        "Parameter" => {
            let dtype_raw = read_str(obj, "dtype")
                .or_else(|| read_str(&sub_owned, "dtype"))
                .or_else(|| {
                    read_str(&sub_owned, "type").filter(|v| is_dtype_literal(v))
                })
                .unwrap_or_else(|| "str".to_string());
            let dtype = normalise_dtype(&dtype_raw);
            let value = resolve_value(obj, &sub_owned);
            Ok(Node::Parameter(Parameter { name, dtype, value }))
        }
        "Snippet" => {
            let code = read_str(obj, "code")
                .or_else(|| read_str(&sub_owned, "code"))
                .unwrap_or_else(|| "def foo(*a, **kw): pass".to_string());
            Ok(Node::Snippet(Snippet {
                name,
                code,
                language,
            }))
        }
        _ => {
            let tasks = obj
                .get("tasks")
                .or_else(|| sub_owned.get("tasks"))
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter_map(|v| v.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default();
            Ok(Node::Operator(Operator {
                name,
                language,
                tasks,
            }))
        }
    }
}

fn raw_type(obj: &Map<String, Value>) -> String {
    obj.get("type")
        .and_then(Value::as_str)
        .or_else(|| obj.get("class_type").and_then(Value::as_str))
        .or_else(|| {
            obj.get("data")
                .and_then(Value::as_object)
                .and_then(|d| d.get("type").and_then(Value::as_str))
        })
        .or_else(|| {
            obj.get("data")
                .and_then(Value::as_object)
                .and_then(|d| d.get("backendType").and_then(Value::as_str))
        })
        .unwrap_or("")
        .trim()
        .to_string()
}

fn normalise_node_type(raw: &str) -> String {
    match raw.to_ascii_lowercase().as_str() {
        "parameter" | "param" => "Parameter".to_string(),
        "snippet" => "Snippet".to_string(),
        "operator" | "visualizer" | "" => "Operator".to_string(),
        other => {
            // Preserve unexpected values so the heuristic in the
            // Python path still applies — but we only route to the
            // three known kinds here; anything else falls to Operator.
            let _ = other;
            "Operator".to_string()
        }
    }
}

fn is_dtype_literal(s: &str) -> bool {
    matches!(
        s.to_ascii_lowercase().as_str(),
        "int" | "float"
            | "string"
            | "str"
            | "bool"
            | "eval"
            | "env"
            | "state"
            | "list"
            | "categorical"
    )
}

fn normalise_dtype(raw: &str) -> ParamDtype {
    match raw.to_ascii_lowercase().as_str() {
        "int" => ParamDtype::Int,
        "float" => ParamDtype::Float,
        "string" | "str" => ParamDtype::String,
        "bool" => ParamDtype::Bool,
        "eval" => ParamDtype::Eval,
        "env" => ParamDtype::Env,
        _ => ParamDtype::Unknown,
    }
}

fn read_str(obj: &Map<String, Value>, key: &str) -> Option<String> {
    obj.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|s| !s.is_empty())
}

fn resolve_value(obj: &Map<String, Value>, sub: &Map<String, Value>) -> String {
    if let Some(v) = obj.get("value") {
        return value_to_string(v);
    }
    if let Some(v) = sub.get("value") {
        return value_to_string(v);
    }
    if let Some(meta) = obj.get("meta").and_then(Value::as_object) {
        if let Some(v) = meta.get("value") {
            return value_to_string(v);
        }
        if let Some(v) = meta.get("default") {
            return value_to_string(v);
        }
    }
    String::new()
}

fn value_to_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

// ---------------------------------------------------------------------------
// Edge deserialisation
// ---------------------------------------------------------------------------

fn deserialize_edge(raw: &Value) -> Result<Edge, GraphError> {
    let obj = raw
        .as_object()
        .ok_or_else(|| GraphError::DeserializationError("edge: expected object".to_string()))?;
    let source = obj
        .get("source")
        .and_then(Value::as_str)
        .ok_or(GraphError::MissingField("source"))?
        .to_string();
    let destination = obj
        .get("destination")
        .and_then(Value::as_str)
        .ok_or(GraphError::MissingField("destination"))?
        .to_string();
    let position = obj
        .get("position")
        .map(Position::from_json_value)
        .unwrap_or_default();
    let output = obj
        .get("output")
        .map(Position::from_json_value)
        .unwrap_or_default();
    // DeliveryMode may be absent on pre-DEM pipelines; default to Once.
    let delivery_mode = obj
        .get("delivery_mode")
        .and_then(Value::as_str)
        .map(parse_delivery_mode)
        .unwrap_or(DeliveryMode::Once);

    Ok(Edge {
        source,
        destination,
        position,
        output,
        delivery_mode,
    })
}

fn parse_delivery_mode(raw: &str) -> DeliveryMode {
    match raw.to_ascii_lowercase().as_str() {
        "stream" => DeliveryMode::Stream,
        "mailbox" => DeliveryMode::Mailbox,
        _ => DeliveryMode::Once,
    }
}

// ---------------------------------------------------------------------------
// Convenience: mapping verifier.
// ---------------------------------------------------------------------------

/// Summary of DEM classification across a parsed graph — used by the
/// plan-doc's "is the map clean?" check. The map is clean when every
/// operator has a domain assigned and the DE set contains only the
/// known async primitives.
#[derive(Debug, Default)]
pub struct DomainMapSummary {
    pub sdf_count: usize,
    pub de_count: usize,
    pub deterministic_count: usize,
    pub non_deterministic_count: usize,
    pub unknown_count: usize,
    pub de_node_ids: Vec<String>,
    pub non_deterministic_node_ids: Vec<String>,
}

pub fn summarise_domain_map(dem: &DemAnnotations) -> DomainMapSummary {
    let mut s = DomainMapSummary::default();
    // Stable ordering for deterministic tests.
    let mut entries: Vec<(&String, &ActorAnnotations)> = dem.actors.iter().collect();
    entries.sort_by(|a, b| a.0.cmp(b.0));
    for (id, ann) in entries {
        match ann.domain {
            DomainKind::Sdf => s.sdf_count += 1,
            DomainKind::De => {
                s.de_count += 1;
                s.de_node_ids.push(id.clone());
            }
        }
        match ann.determinism {
            DeterminismClass::Deterministic => s.deterministic_count += 1,
            DeterminismClass::NonDeterministic => {
                s.non_deterministic_count += 1;
                s.non_deterministic_node_ids.push(id.clone());
            }
            DeterminismClass::Unknown => s.unknown_count += 1,
        }
    }
    s
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn housing_pipeline() -> Value {
        // Verbatim shape of the real pipeline stored at
        // `.data/app/766398ff-*/pipeline.json` — uses `"type"` not
        // `"class_type"`.
        json!({
            "nodes": {
                "fname": {"type": "Parameter", "name": "fname", "dtype": "str", "value": "data/housing.csv"},
                "data_loading": {"type": "Operator", "name": "pandas.read_csv", "language": "python", "tasks": []},
                "preprocessing": {"type": "Snippet", "name": "projection", "code": "def foo(df): return df", "language": "python"},
                "split": {"type": "Operator", "name": "sklearn.model_selection.train_test_split", "language": "python", "tasks": []},
                "model": {"type": "Operator", "name": "sklearn.linear_model.LinearRegression", "language": "python", "tasks": []},
                "training": {"type": "Operator", "name": "fit", "language": "python", "tasks": []},
                "prediction": {"type": "Operator", "name": "predict", "language": "python", "tasks": []},
                "mse": {"type": "Operator", "name": "sklearn.metrics.mean_squared_error", "language": "python", "tasks": []}
            },
            "edges": [
                {"source": "fname", "destination": "data_loading", "position": 0, "output": 0},
                {"source": "data_loading", "destination": "preprocessing", "position": 0, "output": 0},
                {"source": "preprocessing", "destination": "split", "position": 0, "output": 0},
                {"source": "preprocessing", "destination": "split", "position": 1, "output": 1},
                {"source": "model", "destination": "training", "position": 0, "output": 0},
                {"source": "split", "destination": "training", "position": 1, "output": 0},
                {"source": "split", "destination": "training", "position": 2, "output": 2},
                {"source": "training", "destination": "prediction", "position": 0, "output": 0},
                {"source": "split", "destination": "prediction", "position": 1, "output": 1},
                {"source": "split", "destination": "mse", "position": 0, "output": 3},
                {"source": "prediction", "destination": "mse", "position": 1, "output": 0},
            ]
        })
    }

    #[test]
    fn parses_housing_pipeline() {
        let (graph, dem) = parse_pipeline_json(&housing_pipeline()).unwrap();
        assert_eq!(graph.node_count(), 8);
        assert_eq!(graph.edge_count(), 11);
        assert!(graph.get_node("fname").unwrap().is_parameter());
        assert!(graph.get_node("preprocessing").unwrap().is_snippet());
        assert!(graph.get_node("data_loading").unwrap().is_operator());
        // Every node gets an annotation.
        assert_eq!(dem.actors.len(), 8);
    }

    #[test]
    fn housing_pipeline_maps_entirely_to_sdf() {
        let (_, dem) = parse_pipeline_json(&housing_pipeline()).unwrap();
        let summary = summarise_domain_map(&dem);
        assert_eq!(summary.de_count, 0, "no DE nodes in housing pipeline");
        assert_eq!(summary.sdf_count, 8);
    }

    #[test]
    fn snippet_is_non_deterministic_in_map() {
        let (_, dem) = parse_pipeline_json(&housing_pipeline()).unwrap();
        let summary = summarise_domain_map(&dem);
        // Only the Snippet node (`preprocessing`) is non-deterministic.
        assert_eq!(summary.non_deterministic_count, 1);
        assert_eq!(summary.non_deterministic_node_ids, vec!["preprocessing"]);
    }

    #[test]
    fn accepts_class_type_discriminant() {
        let data = json!({
            "nodes": {
                "p": {"class_type": "Parameter", "name": "p", "dtype": "int", "value": "1"},
                "o": {"class_type": "Operator", "name": "sklearn.preprocessing.StandardScaler", "language": "python"}
            },
            "edges": [{"source": "p", "destination": "o", "position": 0, "output": 0}]
        });
        let (graph, _) = parse_pipeline_json(&data).unwrap();
        assert_eq!(graph.node_count(), 2);
        assert!(graph.get_node("p").unwrap().is_parameter());
        assert!(graph.get_node("o").unwrap().is_operator());
    }

    #[test]
    fn accepts_react_flow_nested_data() {
        let data = json!({
            "nodes": {
                "n1": {
                    "data": {
                        "type": "Parameter",
                        "name": "n_estimators",
                        "dtype": "int",
                        "value": "100"
                    }
                }
            },
            "edges": []
        });
        let (graph, _) = parse_pipeline_json(&data).unwrap();
        assert!(graph.get_node("n1").unwrap().is_parameter());
    }

    #[test]
    fn accepts_pipeline_wrapper() {
        let data = json!({
            "pipeline": {
                "nodes": {
                    "o": {"type": "Operator", "name": "pandas.read_csv", "language": "python"}
                },
                "edges": []
            }
        });
        let (graph, _) = parse_pipeline_json(&data).unwrap();
        assert_eq!(graph.node_count(), 1);
    }

    #[test]
    fn keyword_edge_position_preserved() {
        let data = json!({
            "nodes": {
                "p": {"type": "Parameter", "name": "n_estimators", "dtype": "int", "value": "100"},
                "o": {"type": "Operator", "name": "sklearn.ensemble.RandomForestClassifier", "language": "python"}
            },
            "edges": [
                {"source": "p", "destination": "o", "position": "n_estimators", "output": 0}
            ]
        });
        let (graph, _) = parse_pipeline_json(&data).unwrap();
        let edge = &graph.edges[0];
        assert_eq!(
            edge.position,
            Position::Keyword("n_estimators".to_string())
        );
    }

    #[test]
    fn llm_operator_flagged_non_deterministic() {
        let data = json!({
            "nodes": {
                "llm": {"type": "Operator", "name": "openrouter.chat.completion", "language": "python"}
            },
            "edges": []
        });
        let (_, dem) = parse_pipeline_json(&data).unwrap();
        assert_eq!(
            dem.actor("llm").unwrap().determinism,
            DeterminismClass::NonDeterministic
        );
    }

    #[test]
    fn cancel_operator_is_de_domain() {
        let data = json!({
            "nodes": {
                "c": {"type": "Operator", "name": "dorian.cancel", "language": "python"}
            },
            "edges": []
        });
        let (_, dem) = parse_pipeline_json(&data).unwrap();
        assert_eq!(dem.actor("c").unwrap().domain, DomainKind::De);
    }

    #[test]
    fn missing_nodes_field_errors() {
        let data = json!({"edges": []});
        assert!(matches!(
            parse_pipeline_json(&data),
            Err(GraphError::MissingField("nodes"))
        ));
    }

    #[test]
    fn dtype_str_normalises_to_string() {
        let data = json!({
            "nodes": {
                "p": {"type": "Parameter", "name": "x", "dtype": "str", "value": "a"}
            },
            "edges": []
        });
        let (graph, _) = parse_pipeline_json(&data).unwrap();
        if let Node::Parameter(p) = graph.get_node("p").unwrap() {
            assert_eq!(p.dtype, ParamDtype::String);
        } else {
            panic!("expected parameter");
        }
    }
}
