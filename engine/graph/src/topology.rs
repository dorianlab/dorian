//! Graph topology algorithms: topological sort, cycle detection, validation.
//!
//! These are used by the Dataflow director to determine execution order,
//! and by the engine to validate graphs before execution.

use crate::model::{GraphError, NodeId, ProcessGraph};
use rustc_hash::FxHashMap;
use std::collections::VecDeque;

/// Kahn's algorithm for topological sort.
///
/// Returns nodes in execution order (all predecessors before a node).
/// Returns `Err(CycleDetected)` if the graph contains a cycle.
///
/// This is the Rust equivalent of the topological ordering that
/// `dask.threaded.get()` computes internally.
pub fn topological_sort(graph: &ProcessGraph) -> Result<Vec<NodeId>, GraphError> {
    let mut in_degree: FxHashMap<&str, usize> = FxHashMap::default();
    let mut adjacency: FxHashMap<&str, Vec<&str>> = FxHashMap::default();

    // Initialize in-degree for all nodes.
    for id in graph.nodes.keys() {
        in_degree.entry(id.as_str()).or_insert(0);
        adjacency.entry(id.as_str()).or_default();
    }

    // Count incoming edges.
    for edge in &graph.edges {
        *in_degree.entry(edge.destination.as_str()).or_insert(0) += 1;
        adjacency
            .entry(edge.source.as_str())
            .or_default()
            .push(edge.destination.as_str());
    }

    // Start with nodes that have no incoming edges.
    let mut queue: VecDeque<&str> = in_degree
        .iter()
        .filter(|(_, &deg)| deg == 0)
        .map(|(&id, _)| id)
        .collect();

    let mut sorted: Vec<NodeId> = Vec::with_capacity(graph.nodes.len());

    while let Some(node) = queue.pop_front() {
        sorted.push(node.to_string());

        if let Some(neighbors) = adjacency.get(node) {
            for &neighbor in neighbors {
                if let Some(deg) = in_degree.get_mut(neighbor) {
                    *deg -= 1;
                    if *deg == 0 {
                        queue.push_back(neighbor);
                    }
                }
            }
        }
    }

    if sorted.len() != graph.nodes.len() {
        return Err(GraphError::CycleDetected);
    }

    Ok(sorted)
}

/// Detect whether the graph contains cycles.
pub fn has_cycle(graph: &ProcessGraph) -> bool {
    topological_sort(graph).is_err()
}

/// Validate the graph structure before execution.
///
/// Checks:
/// 1. All edge endpoints reference existing nodes
/// 2. No self-loops
/// 3. No duplicate edges (same source+destination+position+output)
/// 4. All `dorian.*` platform operators must be expanded before execution
pub fn validate(graph: &ProcessGraph) -> Result<(), Vec<GraphError>> {
    let mut errors = Vec::new();

    // Check edge endpoints.
    for (i, edge) in graph.edges.iter().enumerate() {
        if !graph.nodes.contains_key(&edge.source) {
            errors.push(GraphError::ValidationError(format!(
                "edge {i}: source '{}' not found in nodes",
                edge.source
            )));
        }
        if !graph.nodes.contains_key(&edge.destination) {
            errors.push(GraphError::ValidationError(format!(
                "edge {i}: destination '{}' not found in nodes",
                edge.destination
            )));
        }
        if edge.source == edge.destination {
            errors.push(GraphError::ValidationError(format!(
                "edge {i}: self-loop on '{}'",
                edge.source
            )));
        }
    }

    // Check for unexpanded platform operators.
    for (id, node) in &graph.nodes {
        if let crate::model::Node::Operator(op) = node {
            if op.name.starts_with("dorian.") {
                errors.push(GraphError::ValidationError(format!(
                    "node '{}': platform operator '{}' must be expanded before execution",
                    id, op.name
                )));
            }
        }
    }

    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}

/// Compute the execution levels (depth from roots).
///
/// Useful for visualization and parallel execution: nodes at the same
/// level can be dispatched concurrently.
pub fn execution_levels(graph: &ProcessGraph) -> Result<FxHashMap<NodeId, usize>, GraphError> {
    let sorted = topological_sort(graph)?;
    let mut levels: FxHashMap<NodeId, usize> = FxHashMap::default();

    for node_id in &sorted {
        let max_pred_level = graph
            .predecessors(node_id)
            .iter()
            .filter_map(|pred| levels.get(*pred))
            .max()
            .copied()
            .unwrap_or(0);

        let level = if graph.predecessors(node_id).is_empty() {
            0
        } else {
            max_pred_level + 1
        };

        levels.insert(node_id.clone(), level);
    }

    Ok(levels)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::*;

    fn linear_dag() -> ProcessGraph {
        // n1 → n2 → n3
        let json = serde_json::json!({
            "nodes": {
                "n1": {"class_type": "Parameter", "name": "x", "dtype": "int", "value": "1"},
                "n2": {"class_type": "Operator", "name": "sklearn.preprocessing.StandardScaler", "language": "python"},
                "n3": {"class_type": "Operator", "name": "sklearn.linear_model.LinearRegression", "language": "python"}
            },
            "edges": [
                {"source": "n1", "destination": "n2", "position": 1, "output": 0},
                {"source": "n2", "destination": "n3", "position": 1, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    fn diamond_dag() -> ProcessGraph {
        //     n1
        //    / \
        //  n2   n3
        //    \ /
        //     n4
        let json = serde_json::json!({
            "nodes": {
                "n1": {"class_type": "Parameter", "name": "x", "dtype": "int", "value": "1"},
                "n2": {"class_type": "Operator", "name": "op_a", "language": "python"},
                "n3": {"class_type": "Operator", "name": "op_b", "language": "python"},
                "n4": {"class_type": "Operator", "name": "op_c", "language": "python"}
            },
            "edges": [
                {"source": "n1", "destination": "n2", "position": 1, "output": 0},
                {"source": "n1", "destination": "n3", "position": 1, "output": 0},
                {"source": "n2", "destination": "n4", "position": 1, "output": 0},
                {"source": "n3", "destination": "n4", "position": 2, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    fn cyclic_graph() -> ProcessGraph {
        // n1 → n2 → n3 → n1 (cycle)
        let json = serde_json::json!({
            "nodes": {
                "n1": {"class_type": "Operator", "name": "a", "language": "python"},
                "n2": {"class_type": "Operator", "name": "b", "language": "python"},
                "n3": {"class_type": "Operator", "name": "c", "language": "python"}
            },
            "edges": [
                {"source": "n1", "destination": "n2"},
                {"source": "n2", "destination": "n3"},
                {"source": "n3", "destination": "n1"}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    #[test]
    fn test_topological_sort_linear() {
        let dag = linear_dag();
        let sorted = topological_sort(&dag).unwrap();
        assert_eq!(sorted.len(), 3);
        // n1 must come before n2, n2 before n3
        let pos_n1 = sorted.iter().position(|x| x == "n1").unwrap();
        let pos_n2 = sorted.iter().position(|x| x == "n2").unwrap();
        let pos_n3 = sorted.iter().position(|x| x == "n3").unwrap();
        assert!(pos_n1 < pos_n2);
        assert!(pos_n2 < pos_n3);
    }

    #[test]
    fn test_topological_sort_diamond() {
        let dag = diamond_dag();
        let sorted = topological_sort(&dag).unwrap();
        assert_eq!(sorted.len(), 4);
        let pos_n1 = sorted.iter().position(|x| x == "n1").unwrap();
        let pos_n2 = sorted.iter().position(|x| x == "n2").unwrap();
        let pos_n3 = sorted.iter().position(|x| x == "n3").unwrap();
        let pos_n4 = sorted.iter().position(|x| x == "n4").unwrap();
        assert!(pos_n1 < pos_n2);
        assert!(pos_n1 < pos_n3);
        assert!(pos_n2 < pos_n4);
        assert!(pos_n3 < pos_n4);
    }

    #[test]
    fn test_cycle_detection() {
        let dag = cyclic_graph();
        assert!(has_cycle(&dag));
        assert!(topological_sort(&dag).is_err());
    }

    #[test]
    fn test_no_cycle_in_dag() {
        let dag = linear_dag();
        assert!(!has_cycle(&dag));
    }

    #[test]
    fn test_validate_valid_dag() {
        let dag = linear_dag();
        assert!(validate(&dag).is_ok());
    }

    #[test]
    fn test_validate_dangling_edge() {
        let mut dag = linear_dag();
        dag.edges.push(Edge {
            source: "n3".to_string(),
            destination: "nonexistent".to_string(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        });
        let errors = validate(&dag).unwrap_err();
        assert!(errors.iter().any(|e| e.to_string().contains("nonexistent")));
    }

    #[test]
    fn test_validate_self_loop() {
        let mut dag = linear_dag();
        dag.edges.push(Edge {
            source: "n2".to_string(),
            destination: "n2".to_string(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        });
        let errors = validate(&dag).unwrap_err();
        assert!(errors.iter().any(|e| e.to_string().contains("self-loop")));
    }

    #[test]
    fn test_validate_unexpanded_platform_op() {
        let json = serde_json::json!({
            "nodes": {
                "n1": {"class_type": "Operator", "name": "dorian.io.dataset", "language": "python"}
            },
            "edges": []
        });
        let dag = ProcessGraph::from_json(&json).unwrap();
        let errors = validate(&dag).unwrap_err();
        assert!(errors
            .iter()
            .any(|e| e.to_string().contains("dorian.io.dataset")));
    }

    #[test]
    fn test_execution_levels_linear() {
        let dag = linear_dag();
        let levels = execution_levels(&dag).unwrap();
        assert_eq!(levels["n1"], 0);
        assert_eq!(levels["n2"], 1);
        assert_eq!(levels["n3"], 2);
    }

    #[test]
    fn test_execution_levels_diamond() {
        let dag = diamond_dag();
        let levels = execution_levels(&dag).unwrap();
        assert_eq!(levels["n1"], 0);
        assert_eq!(levels["n2"], 1);
        assert_eq!(levels["n3"], 1);
        assert_eq!(levels["n4"], 2);
    }

    #[test]
    fn test_empty_graph() {
        let dag = ProcessGraph::new();
        let sorted = topological_sort(&dag).unwrap();
        assert!(sorted.is_empty());
        assert!(validate(&dag).is_ok());
    }

    #[test]
    fn test_single_node() {
        let json = serde_json::json!({
            "nodes": {
                "n1": {"class_type": "Parameter", "name": "x", "dtype": "int", "value": "42"}
            },
            "edges": []
        });
        let dag = ProcessGraph::from_json(&json).unwrap();
        let sorted = topological_sort(&dag).unwrap();
        assert_eq!(sorted, vec!["n1"]);
        assert!(validate(&dag).is_ok());
    }
}
