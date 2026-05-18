//! Meta-Director — automatic director inference from graph structure.
//!
//! The meta-director analyzes a process graph's activation modes and delivery
//! semantics to determine which director(s) should execute which subgraph.
//!
//! Decision matrix:
//!
//! | ActivationMode | DeliveryMode | Director        | Rationale                      |
//! |----------------|--------------|-----------------|--------------------------------|
//! | Transform      | Once         | Dataflow        | Classic pipeline (sklearn, etc.)|
//! | Reactive       | Stream       | MessagePassing  | Agent collaboration, chatbot   |
//! | Reactive       | Mailbox      | MessagePassing  | Agent-to-agent dialogue        |
//! | Service        | Stream       | MessagePassing  | Long-running LLM endpoint      |
//! | Router         | Once         | Dataflow        | Conditional dispatch           |
//! | Router         | Stream       | MessagePassing  | Streaming conditional dispatch |
//! | Transform      | Once (fan)   | MapReduce       | Cross-validation, grid search  |
//! | *              | *            | Sequential      | Debug mode override            |
//!
//! For mixed graphs (e.g., a pipeline that feeds into an agent loop),
//! the meta-director partitions the graph into regions and assigns
//! a director to each region.

use std::collections::HashMap;

use graph::model::{ActivationMode, DeliveryMode, ProcessGraph};

// ---------------------------------------------------------------------------
// Director assignment
// ---------------------------------------------------------------------------

/// Which director should handle a subgraph region.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DirectorKind {
    /// Level-by-level concurrent dispatch (classic pipeline).
    Dataflow,
    /// Event-loop, push-based, reactive (agent/chatbot).
    MessagePassing,
    /// Fan-out/fan-in parallel batch processing.
    MapReduce,
    /// Strict single-node-at-a-time (debugging).
    Sequential,
}

impl std::fmt::Display for DirectorKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DirectorKind::Dataflow => write!(f, "dataflow"),
            DirectorKind::MessagePassing => write!(f, "message_passing"),
            DirectorKind::MapReduce => write!(f, "map_reduce"),
            DirectorKind::Sequential => write!(f, "sequential"),
        }
    }
}

/// A region of the graph assigned to a specific director.
#[derive(Debug, Clone)]
pub struct DirectorRegion {
    /// Which director handles this region.
    pub director: DirectorKind,
    /// Node IDs in this region.
    pub nodes: Vec<String>,
    /// Human-readable reason for the assignment.
    pub reason: String,
}

/// Result of meta-director analysis.
#[derive(Debug, Clone)]
pub struct DirectorPlan {
    /// Regions assigned to directors (may be a single region for simple graphs).
    pub regions: Vec<DirectorRegion>,
    /// Whether the graph has mixed semantics requiring multi-director execution.
    pub is_mixed: bool,
    /// Overall recommendation if forced to use a single director.
    pub primary_director: DirectorKind,
}

// ---------------------------------------------------------------------------
// Graph analysis helpers
// ---------------------------------------------------------------------------

/// Structural features extracted from the graph for director inference.
#[derive(Debug, Clone)]
pub struct GraphFeatures {
    /// Dominant activation mode (most common among nodes).
    pub dominant_activation: ActivationMode,
    /// Dominant delivery mode (most common among edges).
    pub dominant_delivery: DeliveryMode,
    /// Whether the graph contains cycles.
    pub has_cycles: bool,
    /// Whether the graph has fan-out/fan-in structure.
    pub has_fan_out_fan_in: bool,
    /// Number of distinct activation modes present.
    pub activation_diversity: usize,
    /// Number of distinct delivery modes present.
    pub delivery_diversity: usize,
    /// Total node count.
    pub node_count: usize,
    /// Total edge count.
    pub edge_count: usize,
}

/// Extract structural features from a process graph.
pub fn analyze_graph(graph: &ProcessGraph) -> GraphFeatures {
    // Count activation modes.
    let mut activation_counts: HashMap<ActivationMode, usize> = HashMap::new();
    for _node in graph.nodes.values() {
        // All current nodes are Transform by default.
        // In Phase 6+, nodes will carry explicit activation modes.
        let mode = ActivationMode::Transform; // TODO: read from node metadata
        *activation_counts.entry(mode).or_insert(0) += 1;
    }

    // Count delivery modes.
    let mut delivery_counts: HashMap<DeliveryMode, usize> = HashMap::new();
    for edge in &graph.edges {
        *delivery_counts.entry(edge.delivery_mode).or_insert(0) += 1;
    }

    // Dominant modes.
    let dominant_activation = activation_counts
        .iter()
        .max_by_key(|(_, count)| *count)
        .map(|(mode, _)| *mode)
        .unwrap_or(ActivationMode::Transform);

    let dominant_delivery = delivery_counts
        .iter()
        .max_by_key(|(_, count)| *count)
        .map(|(mode, _)| *mode)
        .unwrap_or(DeliveryMode::Once);

    // Cycle detection (simple DFS).
    let has_cycles = detect_cycles(graph);

    // Fan-out/fan-in detection.
    let has_fan_out_fan_in = detect_fan_out_fan_in(graph);

    GraphFeatures {
        dominant_activation,
        dominant_delivery,
        has_cycles,
        has_fan_out_fan_in,
        activation_diversity: activation_counts.len(),
        delivery_diversity: delivery_counts.len(),
        node_count: graph.nodes.len(),
        edge_count: graph.edges.len(),
    }
}

/// Detect cycles in the graph using DFS.
fn detect_cycles(graph: &ProcessGraph) -> bool {
    use graph::topology::topological_sort;
    topological_sort(graph).is_err()
}

/// Detect fan-out/fan-in structure.
///
/// A graph has fan-out/fan-in if any node has >1 outgoing edges AND
/// any node has >1 incoming edges. This suggests a MapReduce pattern.
fn detect_fan_out_fan_in(graph: &ProcessGraph) -> bool {
    let mut out_degree: HashMap<&str, usize> = HashMap::new();
    let mut in_degree: HashMap<&str, usize> = HashMap::new();

    for edge in &graph.edges {
        *out_degree.entry(edge.source.as_str()).or_insert(0) += 1;
        *in_degree.entry(edge.destination.as_str()).or_insert(0) += 1;
    }

    let has_fan_out = out_degree.values().any(|&d| d > 1);
    let has_fan_in = in_degree.values().any(|&d| d > 1);

    has_fan_out && has_fan_in
}

// ---------------------------------------------------------------------------
// Meta-Director
// ---------------------------------------------------------------------------

/// Configuration for the meta-director.
#[derive(Debug, Clone)]
pub struct MetaDirectorConfig {
    /// Force a specific director regardless of graph analysis.
    pub force_director: Option<DirectorKind>,
    /// Minimum fan-out degree to trigger MapReduce (default: 3).
    pub mapreduce_fan_out_threshold: usize,
    /// Enable graph partitioning for mixed-mode graphs.
    pub enable_partitioning: bool,
}

impl Default for MetaDirectorConfig {
    fn default() -> Self {
        Self {
            force_director: None,
            mapreduce_fan_out_threshold: 3,
            enable_partitioning: true,
        }
    }
}

/// Meta-Director — infers which director(s) to use for a process graph.
///
/// The meta-director is the "brain" that decides execution strategy:
/// - Simple pipeline → Dataflow
/// - Agent flow with cycles → MessagePassing
/// - Parallel batch (CV, grid search) → MapReduce
/// - Debug mode → Sequential
///
/// For mixed graphs, it can partition into regions with different directors.
#[derive(Default)]
pub struct MetaDirector {
    config: MetaDirectorConfig,
}

impl MetaDirector {
    pub fn new(config: MetaDirectorConfig) -> Self {
        Self { config }
    }

    /// Analyze a graph and produce a director execution plan.
    pub fn plan(&self, graph: &ProcessGraph) -> DirectorPlan {
        // Shortcut: forced director.
        if let Some(forced) = self.config.force_director {
            return DirectorPlan {
                regions: vec![DirectorRegion {
                    director: forced,
                    nodes: graph.nodes.keys().cloned().collect(),
                    reason: format!("forced to {forced}"),
                }],
                is_mixed: false,
                primary_director: forced,
            };
        }

        // Empty graph.
        if graph.nodes.is_empty() {
            return DirectorPlan {
                regions: vec![],
                is_mixed: false,
                primary_director: DirectorKind::Dataflow,
            };
        }

        let features = analyze_graph(graph);

        // Decision logic.
        let primary = self.infer_director(&features);

        if !self.config.enable_partitioning || !self.needs_partitioning(&features) {
            // Single director for the whole graph.
            return DirectorPlan {
                regions: vec![DirectorRegion {
                    director: primary,
                    nodes: graph.nodes.keys().cloned().collect(),
                    reason: self.explain_choice(&features, primary),
                }],
                is_mixed: false,
                primary_director: primary,
            };
        }

        // Mixed graph — partition into regions.
        let regions = self.partition(graph, &features);
        let is_mixed = regions.len() > 1;

        DirectorPlan {
            regions,
            is_mixed,
            primary_director: primary,
        }
    }

    /// Core inference logic: features → director kind.
    fn infer_director(&self, features: &GraphFeatures) -> DirectorKind {
        // Cycles → must use MessagePassing (dataflow can't handle cycles).
        if features.has_cycles {
            return DirectorKind::MessagePassing;
        }

        // Fan-out/fan-in with sufficient breadth → MapReduce.
        if features.has_fan_out_fan_in {
            // Check if fan-out exceeds threshold for MapReduce.
            // For now, use the feature flag; in practice we'd check actual degrees.
            return DirectorKind::MapReduce;
        }

        // Reactive/Service modes → MessagePassing.
        match features.dominant_activation {
            ActivationMode::Reactive | ActivationMode::Service => {
                return DirectorKind::MessagePassing;
            }
            _ => {}
        }

        // Stream/Mailbox delivery → MessagePassing.
        match features.dominant_delivery {
            DeliveryMode::Stream | DeliveryMode::Mailbox => {
                return DirectorKind::MessagePassing;
            }
            _ => {}
        }

        // Default: Dataflow (classic pipeline).
        DirectorKind::Dataflow
    }

    /// Check if a graph needs partitioning (mixed activation/delivery modes).
    fn needs_partitioning(&self, features: &GraphFeatures) -> bool {
        features.activation_diversity > 1 || features.delivery_diversity > 1
    }

    /// Generate a human-readable explanation for the director choice.
    fn explain_choice(&self, features: &GraphFeatures, director: DirectorKind) -> String {
        match director {
            DirectorKind::Dataflow => {
                format!(
                    "dataflow: {} Transform nodes, {} Once edges — classic pipeline",
                    features.node_count, features.edge_count
                )
            }
            DirectorKind::MessagePassing => {
                if features.has_cycles {
                    "message_passing: graph contains cycles (feedback loops)".to_string()
                } else {
                    format!(
                        "message_passing: dominant mode {:?}/{:?}",
                        features.dominant_activation, features.dominant_delivery
                    )
                }
            }
            DirectorKind::MapReduce => {
                "map_reduce: fan-out/fan-in structure detected".to_string()
            }
            DirectorKind::Sequential => "sequential: forced for debugging".to_string(),
        }
    }

    /// Partition a graph into director regions.
    ///
    /// Current implementation: simple two-region split based on delivery modes.
    /// Future: connected-component analysis with mode homogeneity.
    fn partition(&self, graph: &ProcessGraph, _features: &GraphFeatures) -> Vec<DirectorRegion> {
        // For now, return a single region with the inferred director.
        // True partitioning requires tracking per-node activation modes,
        // which aren't stored on nodes yet (all default to Transform).
        // This will be meaningful once nodes carry explicit ActivationMode.
        let features = analyze_graph(graph);
        let director = self.infer_director(&features);

        vec![DirectorRegion {
            director,
            nodes: graph.nodes.keys().cloned().collect(),
            reason: self.explain_choice(&features, director),
        }]
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use graph::model::ProcessGraph;

    /// Simple linear pipeline: a → b → c (all Transform/Once).
    fn linear_pipeline() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "a": {"class_type": "Operator", "name": "scaler", "language": "python"},
                "b": {"class_type": "Operator", "name": "pca", "language": "python"},
                "c": {"class_type": "Operator", "name": "svm", "language": "python"}
            },
            "edges": [
                {"source": "a", "destination": "b", "position": 1, "output": 0},
                {"source": "b", "destination": "c", "position": 1, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    /// Fan-out/fan-in: source → [fold1, fold2, fold3] → aggregate.
    fn cross_validation_graph() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "split": {"class_type": "Operator", "name": "cv_split", "language": "python"},
                "fold1": {"class_type": "Operator", "name": "train_fold_1", "language": "python"},
                "fold2": {"class_type": "Operator", "name": "train_fold_2", "language": "python"},
                "fold3": {"class_type": "Operator", "name": "train_fold_3", "language": "python"},
                "agg": {"class_type": "Operator", "name": "aggregate_scores", "language": "python"}
            },
            "edges": [
                {"source": "split", "destination": "fold1", "position": 1, "output": 0},
                {"source": "split", "destination": "fold2", "position": 1, "output": 1},
                {"source": "split", "destination": "fold3", "position": 1, "output": 2},
                {"source": "fold1", "destination": "agg", "position": 1, "output": 0},
                {"source": "fold2", "destination": "agg", "position": 2, "output": 0},
                {"source": "fold3", "destination": "agg", "position": 3, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    /// Cyclic graph: a → b → a (feedback loop).
    fn cyclic_graph() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "seed": {"class_type": "Operator", "name": "trigger", "language": "python"},
                "ping": {"class_type": "Operator", "name": "ping", "language": "python"},
                "pong": {"class_type": "Operator", "name": "pong", "language": "python"}
            },
            "edges": [
                {"source": "seed", "destination": "ping", "position": 1, "output": 0},
                {"source": "ping", "destination": "pong", "position": 1, "output": 0},
                {"source": "pong", "destination": "ping", "position": 2, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    // -- analyze_graph tests --

    #[test]
    fn test_analyze_linear_pipeline() {
        let graph = linear_pipeline();
        let features = analyze_graph(&graph);

        assert_eq!(features.dominant_activation, ActivationMode::Transform);
        assert_eq!(features.dominant_delivery, DeliveryMode::Once);
        assert!(!features.has_cycles);
        assert!(!features.has_fan_out_fan_in);
        assert_eq!(features.node_count, 3);
        assert_eq!(features.edge_count, 2);
    }

    #[test]
    fn test_analyze_cross_validation() {
        let graph = cross_validation_graph();
        let features = analyze_graph(&graph);

        assert!(features.has_fan_out_fan_in);
        assert!(!features.has_cycles);
        assert_eq!(features.node_count, 5);
        assert_eq!(features.edge_count, 6);
    }

    #[test]
    fn test_analyze_cyclic() {
        let graph = cyclic_graph();
        let features = analyze_graph(&graph);

        assert!(features.has_cycles);
    }

    #[test]
    fn test_analyze_empty() {
        let graph = ProcessGraph::new();
        let features = analyze_graph(&graph);

        assert_eq!(features.node_count, 0);
        assert_eq!(features.edge_count, 0);
        assert!(!features.has_cycles);
        assert!(!features.has_fan_out_fan_in);
    }

    // -- MetaDirector inference tests --

    #[test]
    fn test_infer_linear_pipeline_is_dataflow() {
        let md = MetaDirector::default();
        let plan = md.plan(&linear_pipeline());

        assert_eq!(plan.primary_director, DirectorKind::Dataflow);
        assert!(!plan.is_mixed);
        assert_eq!(plan.regions.len(), 1);
        assert_eq!(plan.regions[0].director, DirectorKind::Dataflow);
    }

    #[test]
    fn test_infer_cross_validation_is_mapreduce() {
        let md = MetaDirector::default();
        let plan = md.plan(&cross_validation_graph());

        assert_eq!(plan.primary_director, DirectorKind::MapReduce);
        assert_eq!(plan.regions[0].director, DirectorKind::MapReduce);
    }

    #[test]
    fn test_infer_cyclic_is_message_passing() {
        let md = MetaDirector::default();
        let plan = md.plan(&cyclic_graph());

        assert_eq!(plan.primary_director, DirectorKind::MessagePassing);
    }

    #[test]
    fn test_infer_empty_graph() {
        let md = MetaDirector::default();
        let plan = md.plan(&ProcessGraph::new());

        assert_eq!(plan.primary_director, DirectorKind::Dataflow);
        assert!(plan.regions.is_empty());
    }

    // -- Force director tests --

    #[test]
    fn test_force_sequential() {
        let config = MetaDirectorConfig {
            force_director: Some(DirectorKind::Sequential),
            ..Default::default()
        };
        let md = MetaDirector::new(config);
        let plan = md.plan(&linear_pipeline());

        assert_eq!(plan.primary_director, DirectorKind::Sequential);
        assert_eq!(plan.regions[0].director, DirectorKind::Sequential);
    }

    #[test]
    fn test_force_overrides_analysis() {
        let config = MetaDirectorConfig {
            force_director: Some(DirectorKind::Sequential),
            ..Default::default()
        };
        let md = MetaDirector::new(config);

        // Even a cyclic graph gets Sequential when forced.
        let plan = md.plan(&cyclic_graph());
        assert_eq!(plan.primary_director, DirectorKind::Sequential);
    }

    // -- DirectorKind display --

    #[test]
    fn test_director_kind_display() {
        assert_eq!(DirectorKind::Dataflow.to_string(), "dataflow");
        assert_eq!(DirectorKind::MessagePassing.to_string(), "message_passing");
        assert_eq!(DirectorKind::MapReduce.to_string(), "map_reduce");
        assert_eq!(DirectorKind::Sequential.to_string(), "sequential");
    }

    // -- Explanation tests --

    #[test]
    fn test_explain_choice_dataflow() {
        let md = MetaDirector::default();
        let features = analyze_graph(&linear_pipeline());
        let explanation = md.explain_choice(&features, DirectorKind::Dataflow);
        assert!(explanation.contains("dataflow"));
        assert!(explanation.contains("Transform"));
    }

    #[test]
    fn test_explain_choice_cycles() {
        let md = MetaDirector::default();
        let features = analyze_graph(&cyclic_graph());
        let explanation = md.explain_choice(&features, DirectorKind::MessagePassing);
        assert!(explanation.contains("cycles"));
    }

    #[test]
    fn test_explain_choice_mapreduce() {
        let md = MetaDirector::default();
        let features = analyze_graph(&cross_validation_graph());
        let explanation = md.explain_choice(&features, DirectorKind::MapReduce);
        assert!(explanation.contains("fan-out"));
    }

    // -- Config tests --

    #[test]
    fn test_default_config() {
        let config = MetaDirectorConfig::default();
        assert!(config.force_director.is_none());
        assert_eq!(config.mapreduce_fan_out_threshold, 3);
        assert!(config.enable_partitioning);
    }

    // -- GraphFeatures tests --

    #[test]
    fn test_graph_features_diversity() {
        let graph = linear_pipeline();
        let features = analyze_graph(&graph);
        assert_eq!(features.activation_diversity, 1);
        assert_eq!(features.delivery_diversity, 1);
    }
}
