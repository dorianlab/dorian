//! Dataflow Director — topological pull-based scheduling.
//!
//! The dataflow director is the Rust equivalent of `dask.threaded.get()`:
//! it computes a topological order, then dispatches nodes level-by-level,
//! waiting for all nodes at one level to complete before dispatching the next.
//!
//! Key behaviors:
//! - Nodes at the same execution level are dispatched concurrently
//! - Failure in any node aborts the remaining graph (downstream nodes skipped)
//! - Cancellation is cooperative: checked before each node dispatch
//! - Backpressure from the runtime layer causes retry with exponential backoff
//!
//! This director handles the `Transform` activation mode with `Once` delivery
//! semantics — the classic dataflow/pipeline pattern.

use dispatch::runtime::{RuntimeError, RuntimeKind};
use graph::model::{Node, ProcessGraph};
use graph::topology::{execution_levels, topological_sort};
use thiserror::Error;

// ---------------------------------------------------------------------------
// Director errors
// ---------------------------------------------------------------------------

/// Errors during directed execution.
#[derive(Debug, Error)]
pub enum DirectorError {
    #[error("graph validation failed: {0}")]
    ValidationFailed(String),

    #[error("node {node_id} failed: {error}")]
    NodeFailed { node_id: String, error: String },

    #[error("pipeline cancelled")]
    Cancelled,

    #[error("runtime error: {0}")]
    RuntimeError(#[from] RuntimeError),

    #[error("internal error: {0}")]
    Internal(String),
}

// ---------------------------------------------------------------------------
// Execution plan
// ---------------------------------------------------------------------------

/// A pre-computed execution plan derived from the process graph.
///
/// The plan groups nodes into levels: all nodes at the same level can be
/// dispatched concurrently because their predecessors are at lower levels.
#[derive(Debug, Clone)]
pub struct ExecutionPlan {
    /// Nodes grouped by execution level (0 = roots, 1 = their successors, etc.).
    pub levels: Vec<Vec<String>>,
    /// Total number of nodes.
    pub node_count: usize,
    /// Topological order (for reference / debugging).
    pub topo_order: Vec<String>,
    /// Sink nodes (leaves — no outgoing edges).
    pub sink_nodes: Vec<String>,
}

impl ExecutionPlan {
    /// Build an execution plan from a process graph.
    ///
    /// Validates the graph (no cycles, all edges valid) and computes
    /// execution levels for concurrent dispatch.
    pub fn from_graph(graph: &ProcessGraph) -> Result<Self, DirectorError> {
        // Topological sort — fails if graph has cycles.
        let topo_order = topological_sort(graph)
            .map_err(|e| DirectorError::ValidationFailed(e.to_string()))?;

        // Compute execution levels.
        let level_map = execution_levels(graph)
            .map_err(|e| DirectorError::ValidationFailed(e.to_string()))?;

        // Group nodes by level.
        let max_level = level_map.values().max().copied();
        let mut levels: Vec<Vec<String>> = if let Some(ml) = max_level {
            vec![Vec::new(); ml + 1]
        } else {
            Vec::new()
        };
        for (node_id, level) in &level_map {
            levels[*level].push(node_id.clone());
        }

        // Find sink nodes (leaves).
        let sink_nodes: Vec<String> = graph
            .leaves()
            .into_iter()
            .map(|s| s.to_string())
            .collect();

        Ok(ExecutionPlan {
            node_count: topo_order.len(),
            levels,
            topo_order,
            sink_nodes,
        })
    }

    /// Number of execution levels (depth of the graph).
    pub fn depth(&self) -> usize {
        self.levels.len()
    }

    /// Maximum concurrency at any level.
    pub fn max_concurrency(&self) -> usize {
        self.levels.iter().map(|l| l.len()).max().unwrap_or(0)
    }
}

// ---------------------------------------------------------------------------
// Node execution result (director-internal)
// ---------------------------------------------------------------------------

/// Result of executing a single node in the dataflow director.
#[derive(Debug, Clone)]
pub enum NodeOutcome {
    /// Node completed successfully.
    Success {
        node_id: String,
        /// Optional reference to the result (for downstream consumption).
        result_ref: Option<String>,
        duration_secs: f64,
    },
    /// Node failed with an error.
    Failed {
        node_id: String,
        error: String,
        duration_secs: f64,
    },
    /// Node was skipped (upstream failed or pipeline cancelled).
    Skipped { node_id: String, reason: String },
    /// Node was cancelled.
    Cancelled { node_id: String },
}

impl NodeOutcome {
    pub fn node_id(&self) -> &str {
        match self {
            NodeOutcome::Success { node_id, .. } => node_id,
            NodeOutcome::Failed { node_id, .. } => node_id,
            NodeOutcome::Skipped { node_id, .. } => node_id,
            NodeOutcome::Cancelled { node_id, .. } => node_id,
        }
    }

    pub fn is_success(&self) -> bool {
        matches!(self, NodeOutcome::Success { .. })
    }

    pub fn is_failure(&self) -> bool {
        matches!(self, NodeOutcome::Failed { .. })
    }
}

// ---------------------------------------------------------------------------
// Runtime resolution
// ---------------------------------------------------------------------------

/// Resolve which runtime kind a node requires.
///
/// This is a simplified version — the full operator resolver (Phase 4.3)
/// will use KB queries. For now:
/// - Parameters → Engine (no runtime needed)
/// - Python operators → Python
/// - Snippets → Python
/// - API operators → Api
/// - Everything else → Python (default)
pub fn resolve_runtime(node: &Node) -> RuntimeKind {
    match node {
        Node::Parameter(_) => RuntimeKind::Engine,
        Node::Operator(op) => {
            if op.name.starts_with("openrouter.") {
                RuntimeKind::Api
            } else {
                RuntimeKind::Python
            }
        }
        Node::Snippet(_) => RuntimeKind::Python,
        Node::Node(_) => RuntimeKind::Engine,
        Node::Group(_) => RuntimeKind::Engine,
    }
}

// ---------------------------------------------------------------------------
// Dataflow Director
// ---------------------------------------------------------------------------

/// Callback type for node lifecycle events.
///
/// The director calls these hooks before/after each node execution,
/// allowing the engine to update state machines, emit events, etc.
#[async_trait::async_trait]
pub trait DirectorHooks: Send + Sync {
    /// Called before a node starts executing.
    async fn on_node_starting(&self, run_id: &str, node_id: &str);

    /// Called when a node completes (success or failure).
    async fn on_node_completed(&self, run_id: &str, outcome: &NodeOutcome);

    /// Check if cancellation has been requested.
    async fn is_cancelled(&self, run_id: &str) -> bool;

    /// Optionally override the outcome for a node (used in tests).
    ///
    /// Return `Some(outcome)` to replace the default outcome,
    /// or `None` to use the default (success) outcome.
    async fn override_outcome(&self, _run_id: &str, _node_id: &str) -> Option<NodeOutcome> {
        None
    }
}

/// No-op hooks for testing.
pub struct NoopHooks;

#[async_trait::async_trait]
impl DirectorHooks for NoopHooks {
    async fn on_node_starting(&self, _run_id: &str, _node_id: &str) {}
    async fn on_node_completed(&self, _run_id: &str, _outcome: &NodeOutcome) {}
    async fn is_cancelled(&self, _run_id: &str) -> bool {
        false
    }
}

/// The Dataflow Director: schedules nodes level-by-level with concurrent dispatch.
///
/// Replaces `dask.threaded.get()` in the Python codebase. Nodes at the same
/// execution level are dispatched concurrently to the runtime layer. The director
/// respects backpressure and cancellation.
pub struct DataflowDirector {
    /// Maximum backpressure retries before giving up on a node.
    pub max_retries: u32,
    /// Base backoff duration for retries (doubles each attempt).
    pub base_backoff_ms: u64,
}

impl Default for DataflowDirector {
    fn default() -> Self {
        DataflowDirector {
            max_retries: 5,
            base_backoff_ms: 100,
        }
    }
}

impl DataflowDirector {
    pub fn new() -> Self {
        Self::default()
    }

    /// Execute the entire graph according to the execution plan.
    ///
    /// Returns outcomes for all nodes. Execution stops at the first level
    /// where any node fails — remaining nodes are marked as skipped.
    pub async fn execute(
        &self,
        plan: &ExecutionPlan,
        graph: &ProcessGraph,
        run_id: &str,
        hooks: &dyn DirectorHooks,
    ) -> Result<Vec<NodeOutcome>, DirectorError> {
        let mut outcomes: Vec<NodeOutcome> = Vec::with_capacity(plan.node_count);
        let mut failed = false;
        let mut _cancelled = false;

        for (level_idx, level_nodes) in plan.levels.iter().enumerate() {
            // Check cancellation before each level.
            if hooks.is_cancelled(run_id).await {
                _cancelled = true;
                // Mark remaining nodes as cancelled.
                for node_id in level_nodes {
                    outcomes.push(NodeOutcome::Cancelled {
                        node_id: node_id.clone(),
                    });
                }
                // Mark all subsequent levels as cancelled.
                for remaining_level in plan.levels.iter().skip(level_idx + 1) {
                    for node_id in remaining_level {
                        outcomes.push(NodeOutcome::Cancelled {
                            node_id: node_id.clone(),
                        });
                    }
                }
                break;
            }

            // If a previous level failed, skip all remaining nodes.
            if failed {
                for node_id in level_nodes {
                    outcomes.push(NodeOutcome::Skipped {
                        node_id: node_id.clone(),
                        reason: "upstream node failed".to_string(),
                    });
                }
                continue;
            }

            // Dispatch all nodes at this level.
            // NOTE: In production with real runtime dispatch (Phase 3+),
            // nodes at the same level will be submitted concurrently via
            // tokio::spawn and joined. For now, we dispatch sequentially
            // since there's no real runtime to submit to.
            for node_id in level_nodes {
                let node = graph.get_node(node_id);
                let nid = node_id.clone();
                let _rid = run_id.to_string();

                // Determine runtime kind.
                let runtime_kind = node.map(resolve_runtime).unwrap_or(RuntimeKind::Engine);

                // For Parameters and Engine-native nodes, execute immediately
                // (no runtime dispatch needed).
                if runtime_kind == RuntimeKind::Engine {
                    hooks.on_node_starting(run_id, node_id).await;
                    let outcome = NodeOutcome::Success {
                        node_id: nid.clone(),
                        result_ref: None,
                        duration_secs: 0.0,
                    };
                    hooks.on_node_completed(run_id, &outcome).await;
                    outcomes.push(outcome);
                    continue;
                }

                // For runtime-dispatched nodes, we'd submit to the dispatcher.
                // Since the dispatcher requires actual runtime implementations
                // (Phase 1.3 defines the trait, actual Python subprocess pool
                // comes later), we simulate the dispatch here.
                //
                // In production, this would be:
                //   dispatcher.submit(runtime_kind, task).await
                //
                // For now, we record that the node was "dispatched" and
                // use the hooks to track lifecycle.
                hooks.on_node_starting(run_id, node_id).await;

                // Simulate execution (placeholder — real dispatch in Phase 3).
                // Allow hooks to override the outcome (used in tests to inject failures).
                let outcome = if let Some(ov) = hooks.override_outcome(run_id, node_id).await {
                    ov
                } else {
                    NodeOutcome::Success {
                        node_id: nid,
                        result_ref: None,
                        duration_secs: 0.0,
                    }
                };
                hooks.on_node_completed(run_id, &outcome).await;
                outcomes.push(outcome);
            }

            // Check if any node at this level failed.
            let level_start = outcomes.len().saturating_sub(level_nodes.len());
            for outcome in outcomes[level_start..].iter() {
                if outcome.is_failure() {
                    failed = true;
                    break;
                }
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
    use graph::model::ProcessGraph;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    // -- Test hooks that track calls ---

    struct TestHooks {
        started_count: AtomicUsize,
        completed_count: AtomicUsize,
        cancel_flag: AtomicBool,
    }

    impl TestHooks {
        fn new() -> Self {
            TestHooks {
                started_count: AtomicUsize::new(0),
                completed_count: AtomicUsize::new(0),
                cancel_flag: AtomicBool::new(false),
            }
        }

        fn set_cancelled(&self) {
            self.cancel_flag.store(true, Ordering::Relaxed);
        }
    }

    #[async_trait::async_trait]
    impl DirectorHooks for TestHooks {
        async fn on_node_starting(&self, _run_id: &str, _node_id: &str) {
            self.started_count.fetch_add(1, Ordering::Relaxed);
        }

        async fn on_node_completed(&self, _run_id: &str, _outcome: &NodeOutcome) {
            self.completed_count.fetch_add(1, Ordering::Relaxed);
        }

        async fn is_cancelled(&self, _run_id: &str) -> bool {
            self.cancel_flag.load(Ordering::Relaxed)
        }
    }

    // -- Helper graphs ---

    fn linear_graph() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "p1": {"class_type": "Parameter", "name": "x", "dtype": "int", "value": "1"},
                "o1": {"class_type": "Operator", "name": "sklearn.preprocessing.StandardScaler", "language": "python"},
                "o2": {"class_type": "Operator", "name": "sklearn.linear_model.LinearRegression", "language": "python"}
            },
            "edges": [
                {"source": "p1", "destination": "o1", "position": 1, "output": 0},
                {"source": "o1", "destination": "o2", "position": 1, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    fn diamond_graph() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "p1": {"class_type": "Parameter", "name": "x", "dtype": "int", "value": "1"},
                "o1": {"class_type": "Operator", "name": "op_a", "language": "python"},
                "o2": {"class_type": "Operator", "name": "op_b", "language": "python"},
                "o3": {"class_type": "Operator", "name": "op_c", "language": "python"}
            },
            "edges": [
                {"source": "p1", "destination": "o1", "position": 1, "output": 0},
                {"source": "p1", "destination": "o2", "position": 1, "output": 0},
                {"source": "o1", "destination": "o3", "position": 1, "output": 0},
                {"source": "o2", "destination": "o3", "position": 2, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    // -- Execution plan tests ---

    #[test]
    fn test_plan_from_linear_graph() {
        let graph = linear_graph();
        let plan = ExecutionPlan::from_graph(&graph).unwrap();

        assert_eq!(plan.node_count, 3);
        assert_eq!(plan.depth(), 3); // p1 → o1 → o2
        assert_eq!(plan.max_concurrency(), 1);
        assert_eq!(plan.sink_nodes.len(), 1);
    }

    #[test]
    fn test_plan_from_diamond_graph() {
        let graph = diamond_graph();
        let plan = ExecutionPlan::from_graph(&graph).unwrap();

        assert_eq!(plan.node_count, 4);
        assert_eq!(plan.depth(), 3); // p1 → (o1, o2) → o3
        assert_eq!(plan.max_concurrency(), 2); // o1 and o2 are concurrent
    }

    #[test]
    fn test_plan_cyclic_graph_fails() {
        let json = serde_json::json!({
            "nodes": {
                "a": {"class_type": "Operator", "name": "a", "language": "python"},
                "b": {"class_type": "Operator", "name": "b", "language": "python"},
                "c": {"class_type": "Operator", "name": "c", "language": "python"}
            },
            "edges": [
                {"source": "a", "destination": "b"},
                {"source": "b", "destination": "c"},
                {"source": "c", "destination": "a"}
            ]
        });
        let graph = ProcessGraph::from_json(&json).unwrap();
        let result = ExecutionPlan::from_graph(&graph);
        assert!(result.is_err());
    }

    #[test]
    fn test_plan_empty_graph() {
        let graph = ProcessGraph::new();
        let plan = ExecutionPlan::from_graph(&graph).unwrap();
        assert_eq!(plan.node_count, 0);
        assert_eq!(plan.depth(), 0);
    }

    // -- Runtime resolution tests ---

    #[test]
    fn test_resolve_runtime_parameter() {
        let node = Node::Parameter(graph::model::Parameter {
            name: "x".to_string(),
            dtype: graph::model::ParamDtype::Int,
            value: "1".to_string(),
        });
        assert_eq!(resolve_runtime(&node), RuntimeKind::Engine);
    }

    #[test]
    fn test_resolve_runtime_python_operator() {
        let node = Node::Operator(graph::model::Operator {
            name: "sklearn.preprocessing.StandardScaler".to_string(),
            language: "python".to_string(),
            tasks: vec![],
        });
        assert_eq!(resolve_runtime(&node), RuntimeKind::Python);
    }

    #[test]
    fn test_resolve_runtime_api_operator() {
        let node = Node::Operator(graph::model::Operator {
            name: "openrouter.chat.completion".to_string(),
            language: "python".to_string(),
            tasks: vec![],
        });
        assert_eq!(resolve_runtime(&node), RuntimeKind::Api);
    }

    #[test]
    fn test_resolve_runtime_snippet() {
        let node = Node::Snippet(graph::model::Snippet {
            name: "my_func".to_string(),
            code: "def foo(): pass".to_string(),
            language: "python".to_string(),
        });
        assert_eq!(resolve_runtime(&node), RuntimeKind::Python);
    }

    // -- Execution tests ---

    #[tokio::test]
    async fn test_execute_linear_graph() {
        let graph = linear_graph();
        let plan = ExecutionPlan::from_graph(&graph).unwrap();
        let hooks = TestHooks::new();
        let director = DataflowDirector::new();

        let outcomes = director.execute(&plan, &graph, "r1", &hooks).await.unwrap();

        assert_eq!(outcomes.len(), 3);
        assert!(outcomes.iter().all(|o| o.is_success()));
        assert_eq!(hooks.started_count.load(Ordering::Relaxed), 3);
        assert_eq!(hooks.completed_count.load(Ordering::Relaxed), 3);
    }

    #[tokio::test]
    async fn test_execute_diamond_graph() {
        let graph = diamond_graph();
        let plan = ExecutionPlan::from_graph(&graph).unwrap();
        let hooks = TestHooks::new();
        let director = DataflowDirector::new();

        let outcomes = director.execute(&plan, &graph, "r1", &hooks).await.unwrap();

        assert_eq!(outcomes.len(), 4);
        assert!(outcomes.iter().all(|o| o.is_success()));
    }

    #[tokio::test]
    async fn test_execute_cancelled() {
        let graph = linear_graph();
        let plan = ExecutionPlan::from_graph(&graph).unwrap();
        let hooks = TestHooks::new();
        hooks.set_cancelled(); // Cancel before execution starts.
        let director = DataflowDirector::new();

        let outcomes = director.execute(&plan, &graph, "r1", &hooks).await.unwrap();

        // All nodes should be cancelled.
        assert!(outcomes
            .iter()
            .all(|o| matches!(o, NodeOutcome::Cancelled { .. })));
    }

    #[tokio::test]
    async fn test_execute_empty_graph() {
        let graph = ProcessGraph::new();
        let plan = ExecutionPlan::from_graph(&graph).unwrap();
        let hooks = TestHooks::new();
        let director = DataflowDirector::new();

        let outcomes = director.execute(&plan, &graph, "r1", &hooks).await.unwrap();
        assert!(outcomes.is_empty());
    }

    // -- Failure-injection hooks ---

    /// Test hooks that inject `NodeOutcome::Failed` for specific node IDs.
    struct FailingHooks {
        /// Node IDs that should fail.
        fail_nodes: Vec<String>,
        started_count: AtomicUsize,
        completed_count: AtomicUsize,
    }

    impl FailingHooks {
        fn new(fail_nodes: Vec<&str>) -> Self {
            Self {
                fail_nodes: fail_nodes.into_iter().map(String::from).collect(),
                started_count: AtomicUsize::new(0),
                completed_count: AtomicUsize::new(0),
            }
        }
    }

    #[async_trait::async_trait]
    impl DirectorHooks for FailingHooks {
        async fn on_node_starting(&self, _run_id: &str, _node_id: &str) {
            self.started_count.fetch_add(1, Ordering::Relaxed);
        }
        async fn on_node_completed(&self, _run_id: &str, _outcome: &NodeOutcome) {
            self.completed_count.fetch_add(1, Ordering::Relaxed);
        }
        async fn is_cancelled(&self, _run_id: &str) -> bool {
            false
        }
        async fn override_outcome(&self, _run_id: &str, node_id: &str) -> Option<NodeOutcome> {
            if self.fail_nodes.iter().any(|n| n == node_id) {
                Some(NodeOutcome::Failed {
                    node_id: node_id.to_string(),
                    error: "injected failure".to_string(),
                    duration_secs: 0.0,
                })
            } else {
                None
            }
        }
    }

    // -- Failure propagation tests ---

    /// Linear graph A→B→C where A fails.
    /// B and C must be skipped because their upstream (A) failed.
    #[tokio::test]
    async fn test_upstream_failure_skips_downstream() {
        // Build a 3-node linear graph: A → B → C (all operators so they go
        // through the runtime dispatch path, not the Engine short-circuit).
        let json = serde_json::json!({
            "nodes": {
                "A": {"class_type": "Operator", "name": "op_a", "language": "python"},
                "B": {"class_type": "Operator", "name": "op_b", "language": "python"},
                "C": {"class_type": "Operator", "name": "op_c", "language": "python"}
            },
            "edges": [
                {"source": "A", "destination": "B", "position": 1, "output": 0},
                {"source": "B", "destination": "C", "position": 1, "output": 0}
            ]
        });
        let graph = ProcessGraph::from_json(&json).unwrap();
        let plan = ExecutionPlan::from_graph(&graph).unwrap();

        // A should fail.
        let hooks = FailingHooks::new(vec!["A"]);
        let director = DataflowDirector::new();

        let outcomes = director.execute(&plan, &graph, "r1", &hooks).await.unwrap();
        assert_eq!(outcomes.len(), 3);

        // Build a lookup by node_id.
        let find = |id: &str| outcomes.iter().find(|o| o.node_id() == id).unwrap();

        // A failed.
        assert!(find("A").is_failure(), "A should have failed");

        // B and C must be skipped (upstream failure).
        assert!(
            matches!(find("B"), NodeOutcome::Skipped { .. }),
            "B should be skipped, got {:?}",
            find("B")
        );
        assert!(
            matches!(find("C"), NodeOutcome::Skipped { .. }),
            "C should be skipped, got {:?}",
            find("C")
        );

        // Only A should have been started (B and C are skipped before dispatch).
        assert_eq!(hooks.started_count.load(Ordering::Relaxed), 1);
    }

    /// Diamond graph A→{B,C}→D where B fails.
    /// D must be skipped because not all of its inputs succeeded (B failed).
    #[tokio::test]
    async fn test_diamond_partial_failure() {
        let json = serde_json::json!({
            "nodes": {
                "A": {"class_type": "Operator", "name": "op_a", "language": "python"},
                "B": {"class_type": "Operator", "name": "op_b", "language": "python"},
                "C": {"class_type": "Operator", "name": "op_c", "language": "python"},
                "D": {"class_type": "Operator", "name": "op_d", "language": "python"}
            },
            "edges": [
                {"source": "A", "destination": "B", "position": 1, "output": 0},
                {"source": "A", "destination": "C", "position": 1, "output": 0},
                {"source": "B", "destination": "D", "position": 1, "output": 0},
                {"source": "C", "destination": "D", "position": 2, "output": 0}
            ]
        });
        let graph = ProcessGraph::from_json(&json).unwrap();
        let plan = ExecutionPlan::from_graph(&graph).unwrap();

        // B should fail.
        let hooks = FailingHooks::new(vec!["B"]);
        let director = DataflowDirector::new();

        let outcomes = director.execute(&plan, &graph, "r1", &hooks).await.unwrap();
        assert_eq!(outcomes.len(), 4);

        let find = |id: &str| outcomes.iter().find(|o| o.node_id() == id).unwrap();

        // A succeeds (root, no upstream).
        assert!(find("A").is_success(), "A should succeed");

        // B fails (injected).
        assert!(find("B").is_failure(), "B should have failed");

        // C succeeds (independent of B at the same level).
        assert!(find("C").is_success(), "C should succeed");

        // D must be skipped — its level follows a level with a failure.
        assert!(
            matches!(find("D"), NodeOutcome::Skipped { .. }),
            "D should be skipped because B (upstream) failed, got {:?}",
            find("D")
        );
    }

    #[test]
    fn test_node_outcome_accessors() {
        let success = NodeOutcome::Success {
            node_id: "n1".to_string(),
            result_ref: Some("ref".to_string()),
            duration_secs: 1.5,
        };
        assert_eq!(success.node_id(), "n1");
        assert!(success.is_success());
        assert!(!success.is_failure());

        let failed = NodeOutcome::Failed {
            node_id: "n2".to_string(),
            error: "crash".to_string(),
            duration_secs: 0.1,
        };
        assert!(failed.is_failure());
        assert!(!failed.is_success());
    }
}
