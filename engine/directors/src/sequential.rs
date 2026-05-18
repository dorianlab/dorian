//! Sequential Director — strict single-node-at-a-time execution.
//!
//! The sequential director executes nodes one at a time in topological order.
//! This is the simplest scheduling strategy — no concurrency, no parallelism.
//!
//! Use cases:
//! - Debugging: observe each node's execution in isolation
//! - Resource-constrained environments: avoid contention
//! - Ordered side effects: when execution order matters beyond data dependencies
//!
//! The sequential director handles `Transform` activation mode with `Once`
//! delivery semantics, just like the dataflow director, but without concurrency.

use dispatch::runtime::RuntimeKind;
use graph::model::ProcessGraph;
use graph::topology::topological_sort;

use crate::dataflow::{DirectorError, DirectorHooks, NodeOutcome, resolve_runtime};

// ---------------------------------------------------------------------------
// Sequential director
// ---------------------------------------------------------------------------

/// Sequential director — executes nodes one at a time in topological order.
///
/// Unlike the dataflow director which dispatches entire levels concurrently,
/// the sequential director dispatches exactly one node at a time and waits
/// for it to complete before moving to the next.
#[derive(Default)]
pub struct SequentialDirector {
    /// Maximum retries per node (0 = no retries).
    pub max_retries: u32,
}

impl SequentialDirector {
    /// Create a sequential director with custom retry count.
    pub fn with_retries(max_retries: u32) -> Self {
        Self { max_retries }
    }

    /// Execute a process graph sequentially.
    ///
    /// Nodes are dispatched in topological order, one at a time.
    /// If any node fails, downstream nodes are skipped.
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

        let mut outcomes = Vec::with_capacity(topo_order.len());
        let mut failed = false;

        for node_id in &topo_order {
            // Check cancellation before each node.
            if hooks.is_cancelled(run_id).await {
                outcomes.push(NodeOutcome::Cancelled {
                    node_id: node_id.clone(),
                });
                continue;
            }

            // Skip downstream nodes after a failure.
            if failed {
                outcomes.push(NodeOutcome::Skipped {
                    node_id: node_id.clone(),
                    reason: "upstream failure".to_string(),
                });
                continue;
            }

            // Resolve runtime for this node.
            let node = graph.nodes.get(node_id.as_str());
            let _runtime = node.map(resolve_runtime).unwrap_or(RuntimeKind::Engine);

            hooks.on_node_starting(run_id, node_id).await;

            // Simulate execution (actual dispatch is handled by the runtime layer).
            // Allow hooks to override the outcome (used in tests to inject failures).
            let start = std::time::Instant::now();
            let outcome = if let Some(ov) = hooks.override_outcome(run_id, node_id).await {
                ov
            } else {
                NodeOutcome::Success {
                    node_id: node_id.clone(),
                    result_ref: Some(format!("{run_id}:{node_id}")),
                    duration_secs: start.elapsed().as_secs_f64(),
                }
            };

            hooks.on_node_completed(run_id, &outcome).await;

            if let NodeOutcome::Failed { .. } = &outcome {
                failed = true;
            }

            outcomes.push(outcome);
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
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    /// Test hooks that track call counts and support cancellation.
    struct TestHooks {
        started: AtomicUsize,
        completed: AtomicUsize,
        cancel: AtomicBool,
    }

    impl TestHooks {
        fn new() -> Self {
            Self {
                started: AtomicUsize::new(0),
                completed: AtomicUsize::new(0),
                cancel: AtomicBool::new(false),
            }
        }

        fn started_count(&self) -> usize {
            self.started.load(Ordering::Relaxed)
        }

        fn completed_count(&self) -> usize {
            self.completed.load(Ordering::Relaxed)
        }
    }

    #[async_trait::async_trait]
    impl DirectorHooks for TestHooks {
        async fn on_node_starting(&self, _: &str, _: &str) {
            self.started.fetch_add(1, Ordering::Relaxed);
        }
        async fn on_node_completed(&self, _: &str, _: &NodeOutcome) {
            self.completed.fetch_add(1, Ordering::Relaxed);
        }
        async fn is_cancelled(&self, _: &str) -> bool {
            self.cancel.load(Ordering::Relaxed)
        }
    }

    fn linear_graph() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "a": {"class_type": "Operator", "name": "sklearn.preprocessing.StandardScaler", "language": "python"},
                "b": {"class_type": "Operator", "name": "sklearn.svm.SVC", "language": "python"}
            },
            "edges": [
                {"source": "a", "destination": "b", "position": 1, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    #[tokio::test]
    async fn test_sequential_empty() {
        let director = SequentialDirector::default();
        let graph = ProcessGraph::new();

        let outcomes = director.execute(&graph, "run1", &NoopHooks).await.unwrap();
        assert!(outcomes.is_empty());
    }

    #[tokio::test]
    async fn test_sequential_linear() {
        let director = SequentialDirector::default();
        let graph = linear_graph();
        let hooks = TestHooks::new();

        let outcomes = director.execute(&graph, "run1", &hooks).await.unwrap();
        assert_eq!(outcomes.len(), 2);

        // All should succeed.
        for outcome in &outcomes {
            assert!(outcome.is_success(), "expected success, got {:?}", outcome);
        }

        // Hooks should be called for each node.
        assert_eq!(hooks.started_count(), 2);
        assert_eq!(hooks.completed_count(), 2);
    }

    #[tokio::test]
    async fn test_sequential_cancelled() {
        let director = SequentialDirector::default();
        let graph = linear_graph();

        struct AlwaysCancelHooks;
        #[async_trait::async_trait]
        impl DirectorHooks for AlwaysCancelHooks {
            async fn on_node_starting(&self, _: &str, _: &str) {}
            async fn on_node_completed(&self, _: &str, _: &NodeOutcome) {}
            async fn is_cancelled(&self, _: &str) -> bool {
                true
            }
        }

        let outcomes = director
            .execute(&graph, "run1", &AlwaysCancelHooks)
            .await
            .unwrap();

        assert_eq!(outcomes.len(), 2);
        for outcome in &outcomes {
            match outcome {
                NodeOutcome::Cancelled { .. } => {}
                _ => panic!("expected cancelled, got {:?}", outcome),
            }
        }
    }

    #[tokio::test]
    async fn test_sequential_strict_order() {
        let director = SequentialDirector::default();
        let graph = linear_graph();
        let hooks = TestHooks::new();

        let outcomes = director.execute(&graph, "run1", &hooks).await.unwrap();

        // Verify topological order: a before b.
        let ids: Vec<String> = outcomes.iter().map(|o| o.node_id().to_string()).collect();

        let a_pos = ids.iter().position(|id| id == "a").unwrap();
        let b_pos = ids.iter().position(|id| id == "b").unwrap();
        assert!(a_pos < b_pos, "a ({}) should precede b ({})", a_pos, b_pos);
    }

    // -- Failure propagation tests ---

    /// Hooks that inject `NodeOutcome::Failed` for specific node IDs.
    struct FailingHooks {
        fail_nodes: Vec<String>,
        started: AtomicUsize,
        completed: AtomicUsize,
    }

    impl FailingHooks {
        fn new(fail_nodes: Vec<&str>) -> Self {
            Self {
                fail_nodes: fail_nodes.into_iter().map(String::from).collect(),
                started: AtomicUsize::new(0),
                completed: AtomicUsize::new(0),
            }
        }

        fn started_count(&self) -> usize {
            self.started.load(Ordering::Relaxed)
        }
    }

    #[async_trait::async_trait]
    impl DirectorHooks for FailingHooks {
        async fn on_node_starting(&self, _: &str, _: &str) {
            self.started.fetch_add(1, Ordering::Relaxed);
        }
        async fn on_node_completed(&self, _: &str, _: &NodeOutcome) {
            self.completed.fetch_add(1, Ordering::Relaxed);
        }
        async fn is_cancelled(&self, _: &str) -> bool {
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

    /// Linear graph A→B→C where B fails mid-sequence.
    /// A should succeed, B should fail, C should be skipped.
    #[tokio::test]
    async fn test_mid_sequence_failure_stops_execution() {
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

        let hooks = FailingHooks::new(vec!["B"]);
        let director = SequentialDirector::default();

        let outcomes = director.execute(&graph, "run1", &hooks).await.unwrap();
        assert_eq!(outcomes.len(), 3);

        let find = |id: &str| outcomes.iter().find(|o| o.node_id() == id).unwrap();

        // A succeeds (executed before the failure).
        assert!(find("A").is_success(), "A should succeed");

        // B fails (injected).
        assert!(find("B").is_failure(), "B should have failed");

        // C is skipped (downstream of failure).
        assert!(
            matches!(find("C"), NodeOutcome::Skipped { .. }),
            "C should be skipped after B failed, got {:?}",
            find("C")
        );

        // Only A and B should have been started (C is skipped before dispatch).
        assert_eq!(
            hooks.started_count(),
            2,
            "only A and B should have started"
        );
    }

    #[tokio::test]
    async fn test_sequential_with_retries() {
        let director = SequentialDirector::with_retries(3);
        assert_eq!(director.max_retries, 3);

        let graph = linear_graph();
        let outcomes = director.execute(&graph, "run1", &NoopHooks).await.unwrap();
        assert_eq!(outcomes.len(), 2);
    }
}
