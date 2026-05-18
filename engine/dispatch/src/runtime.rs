//! Runtime trait — the contract between the Dorian engine and any execution runtime.
//!
//! Every runtime (Python subprocess, WASM sandbox, API endpoint, container)
//! implements this trait. The dispatcher routes node execution to the
//! appropriate runtime based on operator resolution.
//!
//! Backpressure is built into the trait: `submit()` returns a
//! `SubmitResponse` that can signal ACCEPT, THROTTLE, or REJECT.

use std::fmt;
use thiserror::Error;

// ---------------------------------------------------------------------------
// Runtime kinds (mirrors proto/runtime.proto RuntimeKind)
// ---------------------------------------------------------------------------

/// What kind of runtime a node requires.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RuntimeKind {
    /// Python subprocess pool (sklearn, pandas, user snippets, etc.)
    Python,
    /// HTTP/gRPC external service (LLM APIs, external tools)
    Api,
    /// Wasmtime/Wasmer sandbox (future: user snippets, objectives)
    Wasm,
    /// Container-based execution (future: heavy operators)
    Container,
    /// Engine-native (parameter resolution, no external runtime needed)
    Engine,
}

impl fmt::Display for RuntimeKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RuntimeKind::Python => write!(f, "python"),
            RuntimeKind::Api => write!(f, "api"),
            RuntimeKind::Wasm => write!(f, "wasm"),
            RuntimeKind::Container => write!(f, "container"),
            RuntimeKind::Engine => write!(f, "engine"),
        }
    }
}

// ---------------------------------------------------------------------------
// Task types
// ---------------------------------------------------------------------------

/// A node task submitted to a runtime for execution.
#[derive(Debug, Clone)]
pub struct NodeTask {
    /// Unique per-submission identifier.
    pub task_id: String,
    /// Pipeline run context.
    pub run_id: String,
    /// Node within the graph.
    pub node_id: String,
    /// Serialized node payload (protobuf bytes or JSON).
    pub payload: Vec<u8>,
    /// Input data references from upstream nodes.
    pub inputs: Vec<InputRef>,
    /// Execution context (session, vault refs, etc.).
    pub context: std::collections::HashMap<String, String>,
    /// Timeout for this node (0 = runtime default).
    pub timeout_seconds: f64,
}

/// Reference to input data from an upstream node.
#[derive(Debug, Clone)]
pub struct InputRef {
    pub source_node_id: String,
    pub output_port: i32,
    pub input_position: i32,
    pub input_keyword: String,
    /// Data: either inline bytes or a reference to shared storage.
    pub data: InputData,
}

/// How input data is passed to a runtime.
#[derive(Debug, Clone)]
pub enum InputData {
    /// Small payload (<1MB) passed inline.
    Inline(Vec<u8>),
    /// Reference to shared storage (mmap, Arrow IPC file, etc.).
    Reference(String),
}

/// Result from executing a node.
#[derive(Debug, Clone)]
pub struct NodeResult {
    pub task_id: String,
    pub node_id: String,
    pub status: NodeResultStatus,
    pub outputs: Vec<OutputData>,
    pub error_message: Option<String>,
    pub error_traceback: Option<String>,
    pub duration_seconds: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeResultStatus {
    Success,
    Failed,
    Cancelled,
    Timeout,
}

/// Output data from a node execution (per output port).
#[derive(Debug, Clone)]
pub struct OutputData {
    pub port: i32,
    pub data: InputData, // reuses Inline/Reference enum
    pub type_hint: String,
}

// ---------------------------------------------------------------------------
// Backpressure
// ---------------------------------------------------------------------------

/// Response to a submit request — signals backpressure state.
#[derive(Debug, Clone)]
pub enum SubmitResponse {
    /// Submission accepted, task is queued/running.
    Accepted { task_id: String },
    /// Runtime is under pressure — slow down submissions.
    Throttle {
        task_id: String,
        retry_after_seconds: f64,
    },
    /// Queue full — reject, retry later.
    Rejected { retry_after_seconds: f64 },
}

impl SubmitResponse {
    pub fn is_accepted(&self) -> bool {
        matches!(self, SubmitResponse::Accepted { .. })
    }

    pub fn is_rejected(&self) -> bool {
        matches!(self, SubmitResponse::Rejected { .. })
    }
}

// ---------------------------------------------------------------------------
// Runtime health
// ---------------------------------------------------------------------------

/// Health status of a runtime.
#[derive(Debug, Clone)]
pub struct RuntimeHealth {
    pub kind: RuntimeKind,
    pub healthy: bool,
    pub current_workers: u32,
    pub max_workers: u32,
    pub queue_depth: u32,
    pub queue_capacity: u32,
    pub inflight_tasks: u32,
    pub cpu_percent: f64,
    pub memory_percent: f64,
}

/// Capabilities of a runtime.
#[derive(Debug, Clone)]
pub struct RuntimeCapabilities {
    pub kind: RuntimeKind,
    pub supported_languages: Vec<String>,
    pub supports_cancellation: bool,
    pub supports_streaming: bool,
    pub max_concurrent_tasks: u32,
}

// ---------------------------------------------------------------------------
// Runtime trait
// ---------------------------------------------------------------------------

/// The core abstraction: any execution runtime must implement this.
///
/// This is async because runtimes involve I/O (subprocess communication,
/// HTTP calls, WASM execution). The trait is object-safe for dynamic dispatch.
#[async_trait::async_trait]
pub trait Runtime: Send + Sync {
    /// What kind of runtime this is.
    fn kind(&self) -> RuntimeKind;

    /// What this runtime can execute.
    fn capabilities(&self) -> RuntimeCapabilities;

    /// Current health status.
    fn health(&self) -> RuntimeHealth;

    /// Submit a node for execution.
    ///
    /// Returns immediately with a `SubmitResponse`:
    /// - `Accepted`: task is queued/running
    /// - `Throttle`: runtime under pressure, slow down
    /// - `Rejected`: queue full, retry after delay
    async fn submit(&self, task: NodeTask) -> Result<SubmitResponse, RuntimeError>;

    /// Wait for a previously submitted task to complete.
    async fn wait(&self, task_id: &str) -> Result<NodeResult, RuntimeError>;

    /// Cancel a running task.
    async fn cancel(&self, task_id: &str) -> Result<(), RuntimeError>;

    /// Current queue depth (for scaling controller).
    fn queue_depth(&self) -> u32;

    /// Maximum queue capacity.
    fn queue_capacity(&self) -> u32;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

#[derive(Debug, Error)]
pub enum RuntimeError {
    #[error("runtime {0} is not healthy")]
    Unhealthy(RuntimeKind),
    #[error("task {0} not found")]
    TaskNotFound(String),
    #[error("task {0} timed out after {1}s")]
    Timeout(String, f64),
    #[error("task {0} was cancelled")]
    Cancelled(String),
    #[error("runtime error: {0}")]
    Internal(String),
    #[error("backpressure: {0}")]
    Backpressure(String),
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_runtime_kind_display() {
        assert_eq!(RuntimeKind::Python.to_string(), "python");
        assert_eq!(RuntimeKind::Api.to_string(), "api");
        assert_eq!(RuntimeKind::Wasm.to_string(), "wasm");
        assert_eq!(RuntimeKind::Container.to_string(), "container");
        assert_eq!(RuntimeKind::Engine.to_string(), "engine");
    }

    #[test]
    fn test_submit_response_checks() {
        let accepted = SubmitResponse::Accepted {
            task_id: "t1".into(),
        };
        assert!(accepted.is_accepted());
        assert!(!accepted.is_rejected());

        let rejected = SubmitResponse::Rejected {
            retry_after_seconds: 1.0,
        };
        assert!(rejected.is_rejected());
        assert!(!rejected.is_accepted());
    }

    #[test]
    fn test_node_result_status() {
        assert_eq!(NodeResultStatus::Success, NodeResultStatus::Success);
        assert_ne!(NodeResultStatus::Success, NodeResultStatus::Failed);
    }
}
