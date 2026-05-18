//! Execution state machine — per-node and per-pipeline state tracking.
//!
//! Ports `dorian/models/execution.py` and `dorian/state/execution.py` to Rust.
//!
//! Key design decisions (matching Python):
//! - Per-node state stored at individual Redis keys → zero contention
//! - Run-level state stored separately, finalized at end with snapshot
//! - State transitions are validated (only legal transitions allowed)
//! - Crash recovery: stale run detection at configurable timeout

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use thiserror::Error;

// ---------------------------------------------------------------------------
// Status enums
// ---------------------------------------------------------------------------

/// Per-node execution status.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum NodeStatus {
    /// Not yet started.
    Pending,
    /// Currently executing.
    Running,
    /// Completed successfully.
    Success,
    /// Raised an exception.
    Failed,
    /// Downstream of a failed node (not reached).
    Skipped,
    /// User cancelled the pipeline.
    Cancelled,
}

impl NodeStatus {
    /// Whether this is a terminal state (no further transitions).
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            NodeStatus::Success | NodeStatus::Failed | NodeStatus::Skipped | NodeStatus::Cancelled
        )
    }
}

impl std::fmt::Display for NodeStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            NodeStatus::Pending => write!(f, "PENDING"),
            NodeStatus::Running => write!(f, "RUNNING"),
            NodeStatus::Success => write!(f, "SUCCESS"),
            NodeStatus::Failed => write!(f, "FAILED"),
            NodeStatus::Skipped => write!(f, "SKIPPED"),
            NodeStatus::Cancelled => write!(f, "CANCELLED"),
        }
    }
}

/// Pipeline-level execution status.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum PipelineRunStatus {
    /// Run queued but not started.
    Pending,
    /// One or more nodes executing.
    Running,
    /// All nodes succeeded.
    Success,
    /// One or more nodes failed.
    Failed,
    /// User cancelled.
    Cancelled,
}

impl PipelineRunStatus {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            PipelineRunStatus::Success
                | PipelineRunStatus::Failed
                | PipelineRunStatus::Cancelled
        )
    }
}

impl std::fmt::Display for PipelineRunStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PipelineRunStatus::Pending => write!(f, "PENDING"),
            PipelineRunStatus::Running => write!(f, "RUNNING"),
            PipelineRunStatus::Success => write!(f, "SUCCESS"),
            PipelineRunStatus::Failed => write!(f, "FAILED"),
            PipelineRunStatus::Cancelled => write!(f, "CANCELLED"),
        }
    }
}

// ---------------------------------------------------------------------------
// Node state
// ---------------------------------------------------------------------------

/// State of a single node within a pipeline execution.
///
/// Mirrors `dorian.models.execution.NodeState`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NodeState {
    pub node_id: String,
    pub status: NodeStatus,
    /// Epoch seconds when the node transitioned to RUNNING.
    pub start_time: Option<f64>,
    /// Epoch seconds when the node reached a terminal state.
    pub end_time: Option<f64>,
    /// Error message / traceback (if FAILED).
    pub error: Option<String>,
    /// Pointer to stored result: "redis:{key}" or "file:{path}".
    pub result_ref: Option<String>,
}

impl NodeState {
    /// Create a new node state in PENDING status.
    pub fn new(node_id: impl Into<String>) -> Self {
        NodeState {
            node_id: node_id.into(),
            status: NodeStatus::Pending,
            start_time: None,
            end_time: None,
            error: None,
            result_ref: None,
        }
    }

    /// Wall-clock duration in seconds (if both start and end are set).
    pub fn duration(&self) -> Option<f64> {
        match (self.start_time, self.end_time) {
            (Some(s), Some(e)) => Some(e - s),
            _ => None,
        }
    }

    /// Transition to RUNNING.
    pub fn mark_running(&mut self) -> Result<(), StateError> {
        self.validate_transition(NodeStatus::Running)?;
        self.status = NodeStatus::Running;
        self.start_time = Some(epoch_now());
        Ok(())
    }

    /// Transition to SUCCESS with a result reference.
    pub fn mark_success(&mut self, result_ref: Option<String>) -> Result<(), StateError> {
        self.validate_transition(NodeStatus::Success)?;
        self.status = NodeStatus::Success;
        self.end_time = Some(epoch_now());
        self.result_ref = result_ref;
        Ok(())
    }

    /// Transition to FAILED with an error message.
    pub fn mark_failed(&mut self, error: String) -> Result<(), StateError> {
        self.validate_transition(NodeStatus::Failed)?;
        self.status = NodeStatus::Failed;
        self.end_time = Some(epoch_now());
        self.error = Some(error);
        Ok(())
    }

    /// Transition to SKIPPED.
    pub fn mark_skipped(&mut self, reason: Option<String>) -> Result<(), StateError> {
        self.validate_transition(NodeStatus::Skipped)?;
        self.status = NodeStatus::Skipped;
        self.end_time = Some(epoch_now());
        self.error = reason;
        Ok(())
    }

    /// Transition to CANCELLED.
    pub fn mark_cancelled(&mut self) -> Result<(), StateError> {
        self.validate_transition(NodeStatus::Cancelled)?;
        self.status = NodeStatus::Cancelled;
        self.end_time = Some(epoch_now());
        Ok(())
    }

    /// Validate that a state transition is legal.
    fn validate_transition(&self, to: NodeStatus) -> Result<(), StateError> {
        if self.status.is_terminal() {
            return Err(StateError::InvalidTransition {
                node_id: self.node_id.clone(),
                from: self.status,
                to,
            });
        }

        let valid = match (self.status, to) {
            (NodeStatus::Pending, NodeStatus::Running) => true,
            (NodeStatus::Pending, NodeStatus::Skipped) => true,
            (NodeStatus::Pending, NodeStatus::Cancelled) => true,
            (NodeStatus::Pending, NodeStatus::Failed) => true, // crash recovery
            (NodeStatus::Running, NodeStatus::Success) => true,
            (NodeStatus::Running, NodeStatus::Failed) => true,
            (NodeStatus::Running, NodeStatus::Skipped) => true,
            (NodeStatus::Running, NodeStatus::Cancelled) => true,
            _ => false,
        };

        if valid {
            Ok(())
        } else {
            Err(StateError::InvalidTransition {
                node_id: self.node_id.clone(),
                from: self.status,
                to,
            })
        }
    }
}

// ---------------------------------------------------------------------------
// Pipeline execution
// ---------------------------------------------------------------------------

/// State of an entire pipeline execution run.
///
/// Mirrors `dorian.models.execution.PipelineExecution`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineExecution {
    pub run_id: String,
    pub session_id: String,
    pub pipeline_id: String,
    pub uid: String,
    pub status: PipelineRunStatus,
    /// Epoch seconds when the run transitioned to RUNNING.
    pub start_time: Option<f64>,
    /// Epoch seconds when the run reached a terminal state.
    pub end_time: Option<f64>,
    /// Embedded snapshot of all node states (populated at finalization).
    pub node_states: FxHashMap<String, NodeState>,
}

impl PipelineExecution {
    /// Create a new execution in PENDING status.
    pub fn new(
        run_id: impl Into<String>,
        session_id: impl Into<String>,
        pipeline_id: impl Into<String>,
        uid: impl Into<String>,
    ) -> Self {
        PipelineExecution {
            run_id: run_id.into(),
            session_id: session_id.into(),
            pipeline_id: pipeline_id.into(),
            uid: uid.into(),
            status: PipelineRunStatus::Pending,
            start_time: None,
            end_time: None,
            node_states: FxHashMap::default(),
        }
    }

    /// Transition to RUNNING.
    pub fn mark_running(&mut self) -> Result<(), StateError> {
        self.validate_run_transition(PipelineRunStatus::Running)?;
        self.status = PipelineRunStatus::Running;
        self.start_time = Some(epoch_now());
        Ok(())
    }

    /// Finalize the run based on collected node states.
    ///
    /// Determines the final status from node outcomes:
    /// - All SUCCESS → SUCCESS
    /// - Any FAILED/SKIPPED → FAILED
    /// - Any CANCELLED → CANCELLED
    pub fn finalize(&mut self, node_states: FxHashMap<String, NodeState>) {
        self.node_states = node_states;
        self.end_time = Some(epoch_now());

        let has_cancelled = self
            .node_states
            .values()
            .any(|ns| ns.status == NodeStatus::Cancelled);
        let has_failures = self.node_states.values().any(|ns| {
            ns.status == NodeStatus::Failed || ns.status == NodeStatus::Skipped
        });

        if has_cancelled {
            self.status = PipelineRunStatus::Cancelled;
        } else if has_failures {
            self.status = PipelineRunStatus::Failed;
        } else {
            self.status = PipelineRunStatus::Success;
        }
    }

    /// Mark as failed (externally, e.g. stale run detection).
    pub fn mark_failed(&mut self) {
        self.status = PipelineRunStatus::Failed;
        self.end_time = Some(epoch_now());
    }

    /// Whether any node has failed or been skipped.
    pub fn has_failures(&self) -> bool {
        self.node_states.values().any(|ns| {
            ns.status == NodeStatus::Failed || ns.status == NodeStatus::Skipped
        })
    }

    /// Duration in seconds (if start and end are both set).
    pub fn duration(&self) -> Option<f64> {
        match (self.start_time, self.end_time) {
            (Some(s), Some(e)) => Some(e - s),
            _ => None,
        }
    }

    /// Summary suitable for frontend display.
    pub fn summary(&self) -> serde_json::Value {
        let node_summaries: serde_json::Map<String, serde_json::Value> = self
            .node_states
            .iter()
            .map(|(id, ns)| {
                (
                    id.clone(),
                    serde_json::json!({
                        "status": ns.status.to_string(),
                        "duration": ns.duration(),
                        "error": ns.error,
                    }),
                )
            })
            .collect();

        serde_json::json!({
            "run_id": self.run_id,
            "status": self.status.to_string(),
            "duration": self.duration(),
            "node_count": self.node_states.len(),
            "nodes": node_summaries,
        })
    }

    fn validate_run_transition(&self, to: PipelineRunStatus) -> Result<(), StateError> {
        if self.status.is_terminal() {
            return Err(StateError::RunInTerminalState {
                run_id: self.run_id.clone(),
                status: self.status,
            });
        }

        let valid = matches!(
            (self.status, to),
            (PipelineRunStatus::Pending, PipelineRunStatus::Running)
        );

        if valid {
            Ok(())
        } else {
            Err(StateError::RunInTerminalState {
                run_id: self.run_id.clone(),
                status: self.status,
            })
        }
    }
}

// ---------------------------------------------------------------------------
// Stale run detection (crash recovery)
// ---------------------------------------------------------------------------

/// Default stale run timeout: 30 minutes.
pub const STALE_RUN_TIMEOUT: Duration = Duration::from_secs(30 * 60);

/// Check if a pipeline execution is stale (stuck in RUNNING beyond timeout).
///
/// If stale, marks all non-terminal nodes as FAILED and the run as FAILED.
pub fn cleanup_stale_run(execution: &mut PipelineExecution, timeout: Duration) -> bool {
    if execution.status != PipelineRunStatus::Running {
        return false;
    }

    let now = epoch_now();
    if let Some(start) = execution.start_time {
        if (now - start) > timeout.as_secs_f64() {
            // Mark all non-terminal nodes as FAILED.
            for ns in execution.node_states.values_mut() {
                if !ns.status.is_terminal() {
                    ns.status = NodeStatus::Failed;
                    ns.end_time = Some(now);
                    ns.error = Some("Process terminated unexpectedly".to_string());
                }
            }
            execution.status = PipelineRunStatus::Failed;
            execution.end_time = Some(now);
            return true;
        }
    }

    false
}

/// Sweep all non-terminal nodes after pipeline failure.
///
/// Nodes still PENDING or RUNNING are marked SKIPPED (pipeline failed,
/// node not reached) or CANCELLED (user cancelled).
pub fn sweep_abandoned_nodes(
    node_states: &mut FxHashMap<String, NodeState>,
    cancelled: bool,
) {
    let now = epoch_now();
    for ns in node_states.values_mut() {
        if !ns.status.is_terminal() {
            if cancelled {
                ns.status = NodeStatus::Cancelled;
            } else {
                ns.status = NodeStatus::Skipped;
                ns.error = Some("pipeline failed — node not reached".to_string());
            }
            ns.end_time = Some(now);
        }
    }
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

#[derive(Debug, Error)]
pub enum StateError {
    #[error("invalid node transition: {node_id} {from} → {to}")]
    InvalidTransition {
        node_id: String,
        from: NodeStatus,
        to: NodeStatus,
    },
    #[error("run {run_id} is in terminal state {status}")]
    RunInTerminalState {
        run_id: String,
        status: PipelineRunStatus,
    },
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn epoch_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

// ---------------------------------------------------------------------------
// Redis key helpers (mirrors dorian/infra/keys.py)
// ---------------------------------------------------------------------------

/// Redis key patterns for execution state.
pub struct ExecutionKeys;

impl ExecutionKeys {
    /// Run-level key: `execution:{run_id}`
    pub fn run(run_id: &str) -> String {
        format!("execution:{run_id}")
    }

    /// Per-node key: `execution:{run_id}:node:{node_id}`
    pub fn node(run_id: &str, node_id: &str) -> String {
        format!("execution:{run_id}:node:{node_id}")
    }

    /// Cancel sentinel: `execution:{run_id}:cancel`
    pub fn cancel(run_id: &str) -> String {
        format!("execution:{run_id}:cancel")
    }

    /// Result storage: `result:{run_id}:{node_id}`
    pub fn result(run_id: &str, node_id: &str) -> String {
        format!("result:{run_id}:{node_id}")
    }

    /// Session metadata: `session:{session}:meta`
    pub fn session_meta(session: &str) -> String {
        format!("session:{session}:meta")
    }

    /// User stream: `{uid}:{session}:stream`
    pub fn user_stream(uid: &str, session: &str) -> String {
        format!("{uid}:{session}:stream")
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- NodeState tests ---

    #[test]
    fn test_node_state_new() {
        let ns = NodeState::new("n1");
        assert_eq!(ns.status, NodeStatus::Pending);
        assert!(ns.start_time.is_none());
        assert!(ns.end_time.is_none());
        assert!(ns.error.is_none());
    }

    #[test]
    fn test_node_pending_to_running() {
        let mut ns = NodeState::new("n1");
        ns.mark_running().unwrap();
        assert_eq!(ns.status, NodeStatus::Running);
        assert!(ns.start_time.is_some());
    }

    #[test]
    fn test_node_running_to_success() {
        let mut ns = NodeState::new("n1");
        ns.mark_running().unwrap();
        ns.mark_success(Some("redis:result:r1:n1".to_string()))
            .unwrap();
        assert_eq!(ns.status, NodeStatus::Success);
        assert!(ns.end_time.is_some());
        assert!(ns.duration().is_some());
        assert_eq!(ns.result_ref.as_deref(), Some("redis:result:r1:n1"));
    }

    #[test]
    fn test_node_running_to_failed() {
        let mut ns = NodeState::new("n1");
        ns.mark_running().unwrap();
        ns.mark_failed("ZeroDivisionError".to_string()).unwrap();
        assert_eq!(ns.status, NodeStatus::Failed);
        assert_eq!(ns.error.as_deref(), Some("ZeroDivisionError"));
    }

    #[test]
    fn test_node_running_to_skipped() {
        let mut ns = NodeState::new("n1");
        ns.mark_running().unwrap();
        ns.mark_skipped(Some("upstream failed".to_string()))
            .unwrap();
        assert_eq!(ns.status, NodeStatus::Skipped);
    }

    #[test]
    fn test_node_running_to_cancelled() {
        let mut ns = NodeState::new("n1");
        ns.mark_running().unwrap();
        ns.mark_cancelled().unwrap();
        assert_eq!(ns.status, NodeStatus::Cancelled);
    }

    #[test]
    fn test_node_pending_to_skipped() {
        let mut ns = NodeState::new("n1");
        ns.mark_skipped(Some("pipeline failed".to_string()))
            .unwrap();
        assert_eq!(ns.status, NodeStatus::Skipped);
    }

    #[test]
    fn test_node_pending_to_cancelled() {
        let mut ns = NodeState::new("n1");
        ns.mark_cancelled().unwrap();
        assert_eq!(ns.status, NodeStatus::Cancelled);
    }

    #[test]
    fn test_node_terminal_state_rejects_transition() {
        let mut ns = NodeState::new("n1");
        ns.mark_running().unwrap();
        ns.mark_success(None).unwrap();

        // Can't transition from SUCCESS.
        assert!(ns.mark_running().is_err());
        assert!(ns.mark_failed("oops".to_string()).is_err());
    }

    #[test]
    fn test_node_invalid_transition() {
        let mut ns = NodeState::new("n1");
        // PENDING → SUCCESS is not valid (must go through RUNNING).
        assert!(ns.mark_success(None).is_err());
    }

    #[test]
    fn test_node_is_terminal() {
        assert!(!NodeStatus::Pending.is_terminal());
        assert!(!NodeStatus::Running.is_terminal());
        assert!(NodeStatus::Success.is_terminal());
        assert!(NodeStatus::Failed.is_terminal());
        assert!(NodeStatus::Skipped.is_terminal());
        assert!(NodeStatus::Cancelled.is_terminal());
    }

    // -- PipelineExecution tests ---

    #[test]
    fn test_pipeline_new() {
        let exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        assert_eq!(exec.status, PipelineRunStatus::Pending);
        assert!(exec.start_time.is_none());
    }

    #[test]
    fn test_pipeline_mark_running() {
        let mut exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        exec.mark_running().unwrap();
        assert_eq!(exec.status, PipelineRunStatus::Running);
        assert!(exec.start_time.is_some());
    }

    #[test]
    fn test_pipeline_finalize_success() {
        let mut exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        exec.mark_running().unwrap();

        let mut states = FxHashMap::default();
        let mut n1 = NodeState::new("n1");
        n1.mark_running().unwrap();
        n1.mark_success(None).unwrap();
        states.insert("n1".to_string(), n1);

        let mut n2 = NodeState::new("n2");
        n2.mark_running().unwrap();
        n2.mark_success(None).unwrap();
        states.insert("n2".to_string(), n2);

        exec.finalize(states);
        assert_eq!(exec.status, PipelineRunStatus::Success);
        assert!(exec.end_time.is_some());
        assert!(!exec.has_failures());
    }

    #[test]
    fn test_pipeline_finalize_with_failure() {
        let mut exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        exec.mark_running().unwrap();

        let mut states = FxHashMap::default();
        let mut n1 = NodeState::new("n1");
        n1.mark_running().unwrap();
        n1.mark_success(None).unwrap();
        states.insert("n1".to_string(), n1);

        let mut n2 = NodeState::new("n2");
        n2.mark_running().unwrap();
        n2.mark_failed("crash".to_string()).unwrap();
        states.insert("n2".to_string(), n2);

        exec.finalize(states);
        assert_eq!(exec.status, PipelineRunStatus::Failed);
        assert!(exec.has_failures());
    }

    #[test]
    fn test_pipeline_finalize_cancelled() {
        let mut exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        exec.mark_running().unwrap();

        let mut states = FxHashMap::default();
        let mut n1 = NodeState::new("n1");
        n1.mark_running().unwrap();
        n1.mark_cancelled().unwrap();
        states.insert("n1".to_string(), n1);

        exec.finalize(states);
        assert_eq!(exec.status, PipelineRunStatus::Cancelled);
    }

    #[test]
    fn test_pipeline_double_start_rejected() {
        let mut exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        exec.mark_running().unwrap();
        assert!(exec.mark_running().is_err());
    }

    #[test]
    fn test_pipeline_summary() {
        let mut exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        exec.mark_running().unwrap();
        let mut states = FxHashMap::default();
        let mut n1 = NodeState::new("n1");
        n1.mark_running().unwrap();
        n1.mark_success(None).unwrap();
        states.insert("n1".to_string(), n1);
        exec.finalize(states);

        let summary = exec.summary();
        assert_eq!(summary["run_id"], "r1");
        assert_eq!(summary["status"], "SUCCESS");
        assert_eq!(summary["node_count"], 1);
    }

    // -- Stale run detection ---

    #[test]
    fn test_stale_run_detection() {
        let mut exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        exec.mark_running().unwrap();

        // Set start time far in the past.
        exec.start_time = Some(epoch_now() - 3600.0);

        let mut n1 = NodeState::new("n1");
        n1.mark_running().unwrap();
        exec.node_states.insert("n1".to_string(), n1);

        let was_stale = cleanup_stale_run(&mut exec, STALE_RUN_TIMEOUT);
        assert!(was_stale);
        assert_eq!(exec.status, PipelineRunStatus::Failed);
        assert_eq!(
            exec.node_states["n1"].status,
            NodeStatus::Failed
        );
    }

    #[test]
    fn test_not_stale_yet() {
        let mut exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        exec.mark_running().unwrap();
        // start_time is now — not stale yet.

        let was_stale = cleanup_stale_run(&mut exec, STALE_RUN_TIMEOUT);
        assert!(!was_stale);
        assert_eq!(exec.status, PipelineRunStatus::Running);
    }

    // -- Sweep abandoned nodes ---

    #[test]
    fn test_sweep_abandoned_skipped() {
        let mut states = FxHashMap::default();
        let n1 = NodeState::new("n1"); // PENDING
        let mut n2 = NodeState::new("n2");
        n2.mark_running().unwrap();
        let mut n3 = NodeState::new("n3");
        n3.mark_running().unwrap();
        n3.mark_success(None).unwrap(); // terminal

        states.insert("n1".to_string(), n1);
        states.insert("n2".to_string(), n2);
        states.insert("n3".to_string(), n3);

        sweep_abandoned_nodes(&mut states, false);

        assert_eq!(states["n1"].status, NodeStatus::Skipped);
        assert_eq!(states["n2"].status, NodeStatus::Skipped);
        assert_eq!(states["n3"].status, NodeStatus::Success); // unchanged
    }

    #[test]
    fn test_sweep_abandoned_cancelled() {
        let mut states = FxHashMap::default();
        let n1 = NodeState::new("n1");
        states.insert("n1".to_string(), n1);

        sweep_abandoned_nodes(&mut states, true);
        assert_eq!(states["n1"].status, NodeStatus::Cancelled);
    }

    // -- Serialization ---

    #[test]
    fn test_node_state_serialization() {
        let mut ns = NodeState::new("n1");
        ns.mark_running().unwrap();
        ns.mark_success(Some("redis:result:r1:n1".to_string()))
            .unwrap();

        let json = serde_json::to_string(&ns).unwrap();
        let parsed: NodeState = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed.node_id, "n1");
        assert_eq!(parsed.status, NodeStatus::Success);
        assert_eq!(parsed.result_ref.as_deref(), Some("redis:result:r1:n1"));
    }

    #[test]
    fn test_pipeline_execution_serialization() {
        let mut exec = PipelineExecution::new("r1", "s1", "p1", "u1");
        exec.mark_running().unwrap();

        let json = serde_json::to_string(&exec).unwrap();
        let parsed: PipelineExecution = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed.run_id, "r1");
        assert_eq!(parsed.status, PipelineRunStatus::Running);
    }

    // -- Redis key helpers ---

    #[test]
    fn test_execution_keys() {
        assert_eq!(ExecutionKeys::run("r1"), "execution:r1");
        assert_eq!(ExecutionKeys::node("r1", "n1"), "execution:r1:node:n1");
        assert_eq!(ExecutionKeys::cancel("r1"), "execution:r1:cancel");
        assert_eq!(ExecutionKeys::result("r1", "n1"), "result:r1:n1");
        assert_eq!(
            ExecutionKeys::session_meta("s1"),
            "session:s1:meta"
        );
        assert_eq!(
            ExecutionKeys::user_stream("u1", "s1"),
            "u1:s1:stream"
        );
    }
}
