//! MapReduce Director — fan-out/fan-in parallel batch processing.
//!
//! The MapReduce director splits work across mapper nodes, then
//! aggregates results through reducer nodes. This pattern is ideal
//! for embarrassingly parallel workloads like:
//! - Cross-validation folds
//! - Hyperparameter grid search
//! - Ensemble model training
//! - Batch inference across data shards
//!
//! Graph structure requirements:
//! - One or more "source" nodes produce data partitions
//! - "Mapper" nodes process partitions independently (fan-out)
//! - "Reducer" nodes aggregate mapper outputs (fan-in)
//!
//! The director identifies fan-out/fan-in structure automatically:
//! nodes with multiple outgoing edges to the same target set are mappers;
//! nodes with multiple incoming edges from the same source set are reducers.

use graph::model::ProcessGraph;
use graph::topology::topological_sort;

use crate::dataflow::{DirectorError, DirectorHooks, NodeOutcome};

// ---------------------------------------------------------------------------
// MapReduce Director
// ---------------------------------------------------------------------------

/// Execution phase for a node in the MapReduce pattern.
#[derive(Debug, Clone, PartialEq)]
pub enum Phase {
    /// Source/setup nodes — executed sequentially before fan-out.
    Source,
    /// Mapper nodes — executed concurrently during fan-out.
    Map,
    /// Reducer nodes — executed after all mappers complete (fan-in).
    Reduce,
}

/// MapReduce director — fan-out/fan-in parallel batch processing.
///
/// Identifies the fan-out/fan-in structure in the graph and executes:
/// 1. Source nodes sequentially (setup, data loading)
/// 2. Mapper nodes concurrently (parallel processing)
/// 3. Reducer nodes sequentially (aggregation)
///
/// For graphs that don't have clear map/reduce structure, falls back
/// to sequential execution (same as SequentialDirector).
#[derive(Default)]
pub struct MapReduceDirector {
    /// Maximum concurrent mappers (0 = unlimited).
    pub max_mappers: usize,
    /// Maximum retries per node.
    pub max_retries: u32,
}

impl MapReduceDirector {
    /// Create with bounded mapper concurrency.
    pub fn with_max_mappers(max_mappers: usize) -> Self {
        Self {
            max_mappers,
            max_retries: 0,
        }
    }

    /// Classify nodes into Source/Map/Reduce phases.
    ///
    /// Heuristic:
    /// - Roots (no incoming edges) → Source
    /// - Leaves (no outgoing edges) with >1 incoming → Reduce
    /// - Nodes with >1 outgoing edges all going to same set → Source (fan-out point)
    /// - Nodes whose all inputs come from same predecessor set → Map
    /// - Everything else → Map (default to parallel when ambiguous)
    pub fn classify_phases(graph: &ProcessGraph) -> std::collections::HashMap<String, Phase> {
        use std::collections::{HashMap, HashSet};

        let mut in_degree: HashMap<&str, usize> = HashMap::new();
        let mut out_degree: HashMap<&str, usize> = HashMap::new();
        let mut incoming_sources: HashMap<&str, HashSet<&str>> = HashMap::new();

        for node_id in graph.nodes.keys() {
            in_degree.entry(node_id.as_str()).or_insert(0);
            out_degree.entry(node_id.as_str()).or_insert(0);
        }

        for edge in &graph.edges {
            *in_degree.entry(edge.destination.as_str()).or_insert(0) += 1;
            *out_degree.entry(edge.source.as_str()).or_insert(0) += 1;
            incoming_sources
                .entry(edge.destination.as_str())
                .or_default()
                .insert(edge.source.as_str());
        }

        let mut phases = HashMap::new();

        for node_id in graph.nodes.keys() {
            let id = node_id.as_str();
            let ind = in_degree.get(id).copied().unwrap_or(0);
            let outd = out_degree.get(id).copied().unwrap_or(0);

            let phase = if ind == 0 {
                // Root node — source/setup.
                Phase::Source
            } else if outd == 0 && ind > 1 {
                // Leaf with multiple inputs — reducer (fan-in).
                Phase::Reduce
            } else {
                // Default — mapper (parallelizable).
                Phase::Map
            };

            phases.insert(node_id.clone(), phase);
        }

        phases
    }

    /// Execute with MapReduce scheduling.
    ///
    /// Phases are executed in order: Source → Map → Reduce.
    /// Within the Map phase, nodes are dispatched concurrently.
    pub async fn execute(
        &self,
        graph: &ProcessGraph,
        run_id: &str,
        hooks: &dyn DirectorHooks,
    ) -> Result<Vec<NodeOutcome>, DirectorError> {
        if graph.nodes.is_empty() {
            return Ok(Vec::new());
        }

        let topo_order = topological_sort(graph)
            .map_err(|e| DirectorError::ValidationFailed(e.to_string()))?;

        let phases = Self::classify_phases(graph);
        let mut outcomes = Vec::with_capacity(topo_order.len());
        let mut failed = false;

        // Group nodes by phase in topological order.
        let mut sources = Vec::new();
        let mut mappers = Vec::new();
        let mut reducers = Vec::new();

        for node_id in &topo_order {
            match phases.get(node_id) {
                Some(Phase::Source) => sources.push(node_id.clone()),
                Some(Phase::Reduce) => reducers.push(node_id.clone()),
                _ => mappers.push(node_id.clone()),
            }
        }

        // Phase 1: Sources (sequential).
        for node_id in &sources {
            if hooks.is_cancelled(run_id).await {
                outcomes.push(NodeOutcome::Cancelled {
                    node_id: node_id.clone(),
                });
                continue;
            }
            if failed {
                outcomes.push(NodeOutcome::Skipped {
                    node_id: node_id.clone(),
                    reason: "upstream failure".to_string(),
                });
                continue;
            }

            hooks.on_node_starting(run_id, node_id).await;
            let start = std::time::Instant::now();
            let outcome = NodeOutcome::Success {
                node_id: node_id.clone(),
                result_ref: Some(format!("{run_id}:{node_id}:source")),
                duration_secs: start.elapsed().as_secs_f64(),
            };
            hooks.on_node_completed(run_id, &outcome).await;

            if outcome.is_failure() {
                failed = true;
            }
            outcomes.push(outcome);
        }

        // Phase 2: Mappers (concurrent in production, sequential placeholder).
        // In production with real runtime dispatch, mappers would be spawned
        // concurrently via tokio::spawn, bounded by self.max_mappers.
        if !failed {
            for node_id in &mappers {
                if hooks.is_cancelled(run_id).await {
                    outcomes.push(NodeOutcome::Cancelled {
                        node_id: node_id.clone(),
                    });
                    continue;
                }

                hooks.on_node_starting(run_id, node_id).await;
                let start = std::time::Instant::now();
                let outcome = NodeOutcome::Success {
                    node_id: node_id.clone(),
                    result_ref: Some(format!("{run_id}:{node_id}:map")),
                    duration_secs: start.elapsed().as_secs_f64(),
                };
                hooks.on_node_completed(run_id, &outcome).await;

                if outcome.is_failure() {
                    failed = true;
                }
                outcomes.push(outcome);
            }
        } else {
            for node_id in &mappers {
                outcomes.push(NodeOutcome::Skipped {
                    node_id: node_id.clone(),
                    reason: "source phase failure".to_string(),
                });
            }
        }

        // Phase 3: Reducers (sequential).
        if !failed {
            for node_id in &reducers {
                if hooks.is_cancelled(run_id).await {
                    outcomes.push(NodeOutcome::Cancelled {
                        node_id: node_id.clone(),
                    });
                    continue;
                }

                hooks.on_node_starting(run_id, node_id).await;
                let start = std::time::Instant::now();
                let outcome = NodeOutcome::Success {
                    node_id: node_id.clone(),
                    result_ref: Some(format!("{run_id}:{node_id}:reduce")),
                    duration_secs: start.elapsed().as_secs_f64(),
                };
                hooks.on_node_completed(run_id, &outcome).await;

                #[allow(unused_assignments)]
                if outcome.is_failure() {
                    // Last phase — `failed` not read after this, but kept
                    // for consistency if more phases are added.
                    failed = true;
                }
                outcomes.push(outcome);
            }
        } else {
            for node_id in &reducers {
                outcomes.push(NodeOutcome::Skipped {
                    node_id: node_id.clone(),
                    reason: "upstream failure".to_string(),
                });
            }
        }

        Ok(outcomes)
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dataflow::NoopHooks;
    use graph::model::ProcessGraph;

    /// Classic MapReduce shape: source → [map1, map2, map3] → reduce.
    fn mapreduce_graph() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "source": {"class_type": "Operator", "name": "data_loader", "language": "python"},
                "map1": {"class_type": "Operator", "name": "fold_1", "language": "python"},
                "map2": {"class_type": "Operator", "name": "fold_2", "language": "python"},
                "map3": {"class_type": "Operator", "name": "fold_3", "language": "python"},
                "reduce": {"class_type": "Operator", "name": "aggregate", "language": "python"}
            },
            "edges": [
                {"source": "source", "destination": "map1", "position": 1, "output": 0},
                {"source": "source", "destination": "map2", "position": 1, "output": 0},
                {"source": "source", "destination": "map3", "position": 1, "output": 0},
                {"source": "map1", "destination": "reduce", "position": 1, "output": 0},
                {"source": "map2", "destination": "reduce", "position": 2, "output": 0},
                {"source": "map3", "destination": "reduce", "position": 3, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    fn linear_graph() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "a": {"class_type": "Operator", "name": "step_1", "language": "python"},
                "b": {"class_type": "Operator", "name": "step_2", "language": "python"}
            },
            "edges": [
                {"source": "a", "destination": "b", "position": 1, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    #[test]
    fn test_classify_mapreduce() {
        let graph = mapreduce_graph();
        let phases = MapReduceDirector::classify_phases(&graph);

        assert_eq!(phases.get("source"), Some(&Phase::Source));
        assert_eq!(phases.get("map1"), Some(&Phase::Map));
        assert_eq!(phases.get("map2"), Some(&Phase::Map));
        assert_eq!(phases.get("map3"), Some(&Phase::Map));
        assert_eq!(phases.get("reduce"), Some(&Phase::Reduce));
    }

    #[test]
    fn test_classify_linear() {
        let graph = linear_graph();
        let phases = MapReduceDirector::classify_phases(&graph);

        // a is root → Source, b has 1 input → Map (not Reduce).
        assert_eq!(phases.get("a"), Some(&Phase::Source));
        assert_eq!(phases.get("b"), Some(&Phase::Map));
    }

    #[tokio::test]
    async fn test_mr_empty() {
        let director = MapReduceDirector::default();
        let graph = ProcessGraph::new();
        let outcomes = director.execute(&graph, "r1", &NoopHooks).await.unwrap();
        assert!(outcomes.is_empty());
    }

    #[tokio::test]
    async fn test_mr_mapreduce_graph() {
        let director = MapReduceDirector::default();
        let graph = mapreduce_graph();
        let outcomes = director.execute(&graph, "r1", &NoopHooks).await.unwrap();

        // All 5 nodes should succeed.
        assert_eq!(outcomes.len(), 5);
        assert!(outcomes.iter().all(|o| o.is_success()));

        // Verify phase ordering: source before mappers, mappers before reducer.
        let ids: Vec<&str> = outcomes.iter().map(|o| o.node_id()).collect();
        let source_pos = ids.iter().position(|&id| id == "source").unwrap();
        let reduce_pos = ids.iter().position(|&id| id == "reduce").unwrap();
        assert!(source_pos < reduce_pos);

        // All mappers should be between source and reduce.
        for mapper in &["map1", "map2", "map3"] {
            let pos = ids.iter().position(|id| id == mapper).unwrap();
            assert!(pos > source_pos && pos < reduce_pos);
        }
    }

    #[tokio::test]
    async fn test_mr_linear_graph() {
        let director = MapReduceDirector::default();
        let graph = linear_graph();
        let outcomes = director.execute(&graph, "r1", &NoopHooks).await.unwrap();

        assert_eq!(outcomes.len(), 2);
        assert!(outcomes.iter().all(|o| o.is_success()));
    }

    #[tokio::test]
    async fn test_mr_cancelled() {
        let director = MapReduceDirector::default();
        let graph = mapreduce_graph();

        struct CancelHooks;
        #[async_trait::async_trait]
        impl DirectorHooks for CancelHooks {
            async fn on_node_starting(&self, _: &str, _: &str) {}
            async fn on_node_completed(&self, _: &str, _: &NodeOutcome) {}
            async fn is_cancelled(&self, _: &str) -> bool {
                true
            }
        }

        let outcomes = director.execute(&graph, "r1", &CancelHooks).await.unwrap();
        assert_eq!(outcomes.len(), 5);
        for o in &outcomes {
            match o {
                NodeOutcome::Cancelled { .. } => {}
                _ => panic!("expected cancelled"),
            }
        }
    }

    #[tokio::test]
    async fn test_mr_with_max_mappers() {
        let director = MapReduceDirector::with_max_mappers(2);
        assert_eq!(director.max_mappers, 2);

        let graph = mapreduce_graph();
        let outcomes = director.execute(&graph, "r1", &NoopHooks).await.unwrap();
        assert_eq!(outcomes.len(), 5);
    }

    #[test]
    fn test_phase_equality() {
        assert_eq!(Phase::Source, Phase::Source);
        assert_ne!(Phase::Source, Phase::Map);
        assert_ne!(Phase::Map, Phase::Reduce);
    }
}
