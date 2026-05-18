//! MessagePassing Director — event-loop, push-based, reactive scheduling.
//!
//! The message-passing director is designed for agent collaboration and
//! chatbot flows where nodes react to incoming messages rather than
//! pulling data from upstream.
//!
//! Key behaviors:
//! - Nodes are activated by incoming messages (push-based)
//! - Each node has an inbox; messages are delivered according to channel semantics
//! - Supports reactive activation mode — nodes fire whenever input arrives
//! - Supports stream delivery mode — messages flow continuously
//! - Cycle-safe: nodes can send messages back to predecessors (event loop)
//! - Termination: runs until no messages remain or a termination predicate fires
//!
//! This director handles the `Reactive` activation mode with `Stream` and
//! `Mailbox` delivery semantics — the agent/chatbot pattern.

use std::collections::{HashMap, VecDeque};

use graph::model::ProcessGraph;

use crate::dataflow::{DirectorError, DirectorHooks, NodeOutcome};

// ---------------------------------------------------------------------------
// Message types
// ---------------------------------------------------------------------------

/// A message flowing between nodes in the message-passing director.
#[derive(Debug, Clone)]
pub struct Message {
    /// Source node that emitted this message.
    pub source: String,
    /// Destination node.
    pub destination: String,
    /// Payload reference (opaque — actual data lives in the runtime/store layer).
    pub payload_ref: String,
    /// Monotonic sequence number for ordering within a channel.
    pub sequence: u64,
}

/// Termination condition for the message-passing event loop.
#[derive(Debug, Clone)]
pub enum TerminationCondition {
    /// Stop after N total messages processed.
    MaxMessages(u64),
    /// Stop after N rounds (a round = drain all current inboxes once).
    MaxRounds(u64),
    /// Stop when inbox is empty (natural quiescence).
    Quiescence,
}

impl Default for TerminationCondition {
    fn default() -> Self {
        // Default: quiescence with a safety cap.
        TerminationCondition::MaxMessages(10_000)
    }
}

// ---------------------------------------------------------------------------
// MessagePassing Director
// ---------------------------------------------------------------------------

/// MessagePassing director — event-loop, push-based execution.
///
/// Nodes react to incoming messages. The director runs an event loop:
/// 1. Seed initial messages from source nodes (roots)
/// 2. For each node with messages in its inbox, activate the node
/// 3. Node produces output messages → routed to destination inboxes
/// 4. Repeat until termination condition met
///
/// This supports cycles (feedback loops) — unlike the dataflow director
/// which requires a DAG. Termination is guaranteed by the termination
/// condition (max messages, max rounds, or quiescence).
#[derive(Default)]
pub struct MessagePassingDirector {
    /// Termination condition for the event loop.
    pub termination: TerminationCondition,
    /// Maximum retries per node activation (0 = no retries).
    pub max_retries: u32,
}

impl MessagePassingDirector {
    /// Create with a specific termination condition.
    pub fn with_termination(termination: TerminationCondition) -> Self {
        Self {
            termination,
            max_retries: 0,
        }
    }

    /// Execute the message-passing event loop.
    ///
    /// Seeds initial messages from root nodes, then runs the event loop
    /// until termination. Returns outcomes for every node activation
    /// (a node may appear multiple times if activated multiple times).
    pub async fn execute(
        &self,
        graph: &ProcessGraph,
        run_id: &str,
        hooks: &dyn DirectorHooks,
    ) -> Result<Vec<NodeOutcome>, DirectorError> {
        if graph.nodes.is_empty() {
            return Ok(Vec::new());
        }

        // Build adjacency: source → [(destination, edge_index)].
        let mut adjacency: HashMap<&str, Vec<&str>> = HashMap::new();
        for edge in &graph.edges {
            adjacency
                .entry(edge.source.as_str())
                .or_default()
                .push(edge.destination.as_str());
        }

        // Identify root nodes (no incoming edges).
        let destinations: std::collections::HashSet<&str> =
            graph.edges.iter().map(|e| e.destination.as_str()).collect();
        let roots: Vec<&str> = graph
            .nodes
            .keys()
            .filter(|id| !destinations.contains(id.as_str()))
            .map(|s| s.as_str())
            .collect();

        // Initialize inboxes — seed roots with a synthetic "start" message.
        let mut inboxes: HashMap<&str, VecDeque<Message>> = HashMap::new();
        let mut sequence: u64 = 0;
        for root in &roots {
            let msg = Message {
                source: "__seed__".to_string(),
                destination: root.to_string(),
                payload_ref: format!("{run_id}:seed:{root}"),
                sequence,
            };
            sequence += 1;
            inboxes.entry(root).or_default().push_back(msg);
        }

        let mut outcomes = Vec::new();
        let mut total_messages: u64 = 0;
        let mut rounds: u64 = 0;

        // Event loop.
        loop {
            // Check termination.
            match &self.termination {
                TerminationCondition::MaxMessages(max) if total_messages >= *max => break,
                TerminationCondition::MaxRounds(max) if rounds >= *max => break,
                _ => {}
            }

            // Check cancellation.
            if hooks.is_cancelled(run_id).await {
                // Mark all nodes with pending messages as cancelled.
                for (node_id, inbox) in &inboxes {
                    if !inbox.is_empty() {
                        outcomes.push(NodeOutcome::Cancelled {
                            node_id: node_id.to_string(),
                        });
                    }
                }
                break;
            }

            // Collect all nodes that have messages waiting.
            let active_nodes: Vec<&str> = inboxes
                .iter()
                .filter(|(_, inbox)| !inbox.is_empty())
                .map(|(id, _)| *id)
                .collect();

            // Quiescence check: no active nodes → done.
            if active_nodes.is_empty() {
                break;
            }

            // Process one round: activate each node with pending messages.
            for node_id in &active_nodes {
                // Drain this node's inbox.
                let messages: Vec<Message> = inboxes
                    .get_mut(node_id)
                    .map(|inbox| inbox.drain(..).collect())
                    .unwrap_or_default();

                if messages.is_empty() {
                    continue;
                }

                total_messages += messages.len() as u64;

                hooks.on_node_starting(run_id, node_id).await;

                // Simulate node activation (real dispatch in Phase 3+).
                // In production, the node processes all messages in its inbox
                // and produces output messages.
                let start = std::time::Instant::now();
                let outcome = NodeOutcome::Success {
                    node_id: node_id.to_string(),
                    result_ref: Some(format!("{run_id}:{node_id}:r{rounds}")),
                    duration_secs: start.elapsed().as_secs_f64(),
                };

                hooks.on_node_completed(run_id, &outcome).await;

                // Route output messages to downstream inboxes.
                if let Some(downstream) = adjacency.get(node_id) {
                    for dest in downstream {
                        let msg = Message {
                            source: node_id.to_string(),
                            destination: dest.to_string(),
                            payload_ref: format!("{run_id}:{node_id}→{dest}:s{sequence}"),
                            sequence,
                        };
                        sequence += 1;
                        inboxes.entry(dest).or_default().push_back(msg);
                    }
                }

                outcomes.push(outcome);
            }

            rounds += 1;
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
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct CountingHooks {
        started: AtomicUsize,
        completed: AtomicUsize,
    }

    impl CountingHooks {
        fn new() -> Self {
            Self {
                started: AtomicUsize::new(0),
                completed: AtomicUsize::new(0),
            }
        }
    }

    #[async_trait::async_trait]
    impl DirectorHooks for CountingHooks {
        async fn on_node_starting(&self, _: &str, _: &str) {
            self.started.fetch_add(1, Ordering::Relaxed);
        }
        async fn on_node_completed(&self, _: &str, _: &NodeOutcome) {
            self.completed.fetch_add(1, Ordering::Relaxed);
        }
        async fn is_cancelled(&self, _: &str) -> bool {
            false
        }
    }

    fn chain_graph() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "agent_a": {"class_type": "Operator", "name": "agent.llm", "language": "python"},
                "agent_b": {"class_type": "Operator", "name": "agent.summarizer", "language": "python"}
            },
            "edges": [
                {"source": "agent_a", "destination": "agent_b", "position": 1, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    fn diamond_graph() -> ProcessGraph {
        let json = serde_json::json!({
            "nodes": {
                "src": {"class_type": "Operator", "name": "source", "language": "python"},
                "left": {"class_type": "Operator", "name": "left_proc", "language": "python"},
                "right": {"class_type": "Operator", "name": "right_proc", "language": "python"},
                "sink": {"class_type": "Operator", "name": "sink", "language": "python"}
            },
            "edges": [
                {"source": "src", "destination": "left", "position": 1, "output": 0},
                {"source": "src", "destination": "right", "position": 1, "output": 0},
                {"source": "left", "destination": "sink", "position": 1, "output": 0},
                {"source": "right", "destination": "sink", "position": 2, "output": 0}
            ]
        });
        ProcessGraph::from_json(&json).unwrap()
    }

    #[tokio::test]
    async fn test_mp_empty_graph() {
        let director = MessagePassingDirector::default();
        let graph = ProcessGraph::new();
        let outcomes = director.execute(&graph, "r1", &NoopHooks).await.unwrap();
        assert!(outcomes.is_empty());
    }

    #[tokio::test]
    async fn test_mp_chain() {
        let director = MessagePassingDirector::with_termination(TerminationCondition::Quiescence);
        let graph = chain_graph();
        let hooks = CountingHooks::new();

        let outcomes = director.execute(&graph, "r1", &hooks).await.unwrap();

        // Root (agent_a) activated once, then agent_b activated once.
        assert_eq!(outcomes.len(), 2);
        assert!(outcomes.iter().all(|o| o.is_success()));
        assert_eq!(hooks.started.load(Ordering::Relaxed), 2);
        assert_eq!(hooks.completed.load(Ordering::Relaxed), 2);
    }

    #[tokio::test]
    async fn test_mp_diamond() {
        let director = MessagePassingDirector::with_termination(TerminationCondition::Quiescence);
        let graph = diamond_graph();
        let hooks = CountingHooks::new();

        let outcomes = director.execute(&graph, "r1", &hooks).await.unwrap();

        // src activates → left + right activate → sink activates.
        // sink may activate once or twice depending on round grouping.
        assert!(outcomes.len() >= 3);
        assert!(outcomes.iter().all(|o| o.is_success()));
    }

    #[tokio::test]
    async fn test_mp_max_messages_terminates() {
        // Cycle with a seed node that feeds into the loop.
        // seed → a → b → a (cycle). MaxMessages prevents infinite loop.
        let json = serde_json::json!({
            "nodes": {
                "seed": {"class_type": "Operator", "name": "trigger", "language": "python"},
                "a": {"class_type": "Operator", "name": "ping", "language": "python"},
                "b": {"class_type": "Operator", "name": "pong", "language": "python"}
            },
            "edges": [
                {"source": "seed", "destination": "a", "position": 1, "output": 0},
                {"source": "a", "destination": "b", "position": 1, "output": 0},
                {"source": "b", "destination": "a", "position": 1, "output": 0}
            ]
        });
        let graph = ProcessGraph::from_json(&json).unwrap();

        let director =
            MessagePassingDirector::with_termination(TerminationCondition::MaxMessages(10));
        let outcomes = director.execute(&graph, "r1", &NoopHooks).await.unwrap();

        // Should terminate after processing ~10 messages total.
        assert!(!outcomes.is_empty());
        // Bounded — won't run forever.
        assert!(outcomes.len() <= 20); // generous upper bound
    }

    #[tokio::test]
    async fn test_mp_max_rounds_terminates() {
        let graph = chain_graph();
        let director =
            MessagePassingDirector::with_termination(TerminationCondition::MaxRounds(1));

        let outcomes = director.execute(&graph, "r1", &NoopHooks).await.unwrap();

        // Only 1 round: root node activates, but downstream doesn't get a round.
        assert!(!outcomes.is_empty());
    }

    #[tokio::test]
    async fn test_mp_cancelled() {
        let graph = chain_graph();
        let director = MessagePassingDirector::default();

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

        // All pending nodes cancelled.
        assert!(!outcomes.is_empty());
        for o in &outcomes {
            match o {
                NodeOutcome::Cancelled { .. } => {}
                _ => panic!("expected cancelled, got {:?}", o),
            }
        }
    }

    #[test]
    fn test_message_fields() {
        let msg = Message {
            source: "a".to_string(),
            destination: "b".to_string(),
            payload_ref: "ref1".to_string(),
            sequence: 42,
        };
        assert_eq!(msg.source, "a");
        assert_eq!(msg.destination, "b");
        assert_eq!(msg.sequence, 42);
    }

    #[test]
    fn test_termination_default() {
        let t = TerminationCondition::default();
        match t {
            TerminationCondition::MaxMessages(n) => assert_eq!(n, 10_000),
            _ => panic!("expected MaxMessages default"),
        }
    }
}
