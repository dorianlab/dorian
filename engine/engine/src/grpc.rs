//! gRPC server implementation for the Dorian engine.
//!
//! Implements the `EngineService` defined in `proto/engine.proto`.
//! This is the primary interface between the Go gateway and the Rust engine.
//!
//! Current scope (Phase 3.1):
//! - GetHealth: Returns system health (memory, goroutines, uptime)
//! - GetRuntimeStatus: Returns per-runtime health status
//! - CancelPipeline: Sets cancellation flag for a running pipeline
//! - GetExecutionState: Retrieves execution state from in-memory store
//!
//! Future (Phase 3.2+):
//! - ExecutePipeline: Full pipeline execution via Dataflow Director
//! - StreamHealth: Streaming health updates

use std::pin::Pin;
use std::sync::Arc;
use std::time::Instant;

use tokio::sync::RwLock;
use tonic::{Request, Response, Status};

use crate::events::EventLog;
use crate::state::{NodeStatus, PipelineExecution, PipelineRunStatus};
use std::sync::Arc as StdArc;

// ---------------------------------------------------------------------------
// Generated proto code — module structure must match the proto package nesting
// so cross-references like `super::execution::PipelineExecution` resolve.
// ---------------------------------------------------------------------------

/// Proto-generated types for the `dorian.*` proto packages.
///
/// The tonic code-generator produces `dorian.engine.rs` with internal
/// server/client modules that reference sibling packages as `super::super::X`.
/// We need the engine proto at a nesting level where its `super::super`
/// resolves to a module containing `execution`, `scaling`, etc.
///
/// Structure:
///   pb::execution  — dorian.execution proto types
///   pb::scaling    — dorian.scaling proto types
///   pb::runtime    — dorian.runtime proto types
///   pb::graph      — dorian.graph proto types
///   pb::engine     — dorian.engine proto (server, service trait)
///     └── engine_service_server — generated server code (uses super::super::execution)
pub mod pb {
    pub mod execution {
        tonic::include_proto!("dorian.execution");
    }
    pub mod scaling {
        tonic::include_proto!("dorian.scaling");
    }
    pub mod runtime {
        tonic::include_proto!("dorian.runtime");
    }
    pub mod graph {
        tonic::include_proto!("dorian.graph");
    }
    pub mod engine {
        // Re-export sibling modules so `super::execution` works from here.
        pub use super::execution;
        pub use super::graph;
        pub use super::runtime;
        pub use super::scaling;

        tonic::include_proto!("dorian.engine");
    }
}

use pb::engine::engine_service_server::EngineService;

// ---------------------------------------------------------------------------
// Engine state (shared across gRPC handlers)
// ---------------------------------------------------------------------------

/// Shared state for the engine gRPC service.
///
/// This holds the in-memory execution store, event log, and engine metadata.
/// All fields are behind Arc<RwLock<>> for concurrent access from gRPC handlers.
pub struct EngineState {
    /// Active pipeline executions, keyed by run_id.
    pub executions: RwLock<rustc_hash::FxHashMap<String, PipelineExecution>>,
    /// Event log with broadcast channels.
    pub event_log: StdArc<EventLog>,
    /// Cancellation flags, keyed by run_id.
    pub cancel_flags: RwLock<rustc_hash::FxHashSet<String>>,
    /// Engine start time for uptime calculation.
    pub start_time: Instant,
}

/// Maximum number of entries before cleanup is triggered for cancel_flags
/// and executions maps. When exceeded, terminal-state entries are pruned.
/// If cancel_flags still exceeds the cap after pruning, it is cleared
/// entirely — cancel flags are best-effort and safe to discard.
const STATE_CAP: usize = 10_000;

impl EngineState {
    pub fn new() -> Self {
        Self {
            executions: RwLock::new(rustc_hash::FxHashMap::default()),
            event_log: EventLog::new(10_000),
            cancel_flags: RwLock::new(rustc_hash::FxHashSet::default()),
            start_time: Instant::now(),
        }
    }

    /// Remove a completed run from both `cancel_flags` and `executions`.
    ///
    /// Should be called when a pipeline reaches a terminal state
    /// (Success / Failed / Cancelled) to prevent unbounded growth.
    /// Additionally enforces a size cap: if either collection exceeds
    /// `STATE_CAP`, terminal entries are bulk-pruned.
    pub async fn cleanup_terminal(&self, run_id: &str) {
        {
            let mut flags = self.cancel_flags.write().await;
            flags.remove(run_id);

            // Size cap: if cancel_flags is still too large, clear it entirely.
            // Cancel flags are best-effort; clearing stale ones is safe.
            if flags.len() > STATE_CAP {
                tracing::warn!(
                    count = flags.len(),
                    "cancel_flags exceeded cap, clearing all entries"
                );
                flags.clear();
            }
        }

        {
            let mut execs = self.executions.write().await;
            execs.remove(run_id);

            // Size cap: prune all terminal-state executions, keep running ones.
            if execs.len() > STATE_CAP {
                let terminal_ids: Vec<String> = execs
                    .iter()
                    .filter(|(_, e)| {
                        matches!(
                            e.status,
                            PipelineRunStatus::Success
                                | PipelineRunStatus::Failed
                                | PipelineRunStatus::Cancelled
                        )
                    })
                    .map(|(id, _)| id.clone())
                    .collect();

                let pruned = terminal_ids.len();
                for id in terminal_ids {
                    execs.remove(&id);
                }
                tracing::warn!(
                    pruned,
                    remaining = execs.len(),
                    "executions exceeded cap, pruned terminal entries"
                );
            }
        }
    }
}

impl Default for EngineState {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// gRPC service implementation
// ---------------------------------------------------------------------------

/// The Dorian engine gRPC service.
pub struct EngineServiceImpl {
    state: Arc<EngineState>,
}

impl EngineServiceImpl {
    pub fn new(state: Arc<EngineState>) -> Self {
        Self { state }
    }
}

#[tonic::async_trait]
impl EngineService for EngineServiceImpl {
    /// Server-streaming response type for ExecutePipeline.
    type ExecutePipelineStream = Pin<
        Box<
            dyn tokio_stream::Stream<Item = Result<pb::execution::StateUpdate, Status>>
                + Send
                + 'static,
        >,
    >;

    /// Submit a pipeline for execution. Returns a stream of state updates.
    ///
    /// Phase 3.2: This will parse the graph, build an ExecutionPlan, and
    /// dispatch through the DataflowDirector. For now, returns Unimplemented.
    async fn execute_pipeline(
        &self,
        _request: Request<pb::engine::ExecutePipelineRequest>,
    ) -> Result<Response<Self::ExecutePipelineStream>, Status> {
        Err(Status::unimplemented(
            "ExecutePipeline not yet implemented — coming in Phase 3.2",
        ))
    }

    /// Cancel a running pipeline.
    ///
    /// Sets a cancellation flag that the director checks cooperatively.
    async fn cancel_pipeline(
        &self,
        request: Request<pb::engine::CancelPipelineRequest>,
    ) -> Result<Response<pb::engine::CancelPipelineResponse>, Status> {
        let run_id = request.into_inner().run_id;
        if run_id.is_empty() {
            return Err(Status::invalid_argument("run_id is required"));
        }

        // Check if the run exists and is still active.
        let executions = self.state.executions.read().await;
        if let Some(exec) = executions.get(&run_id) {
            if exec.status == PipelineRunStatus::Success
                || exec.status == PipelineRunStatus::Failed
                || exec.status == PipelineRunStatus::Cancelled
            {
                return Ok(Response::new(pb::engine::CancelPipelineResponse {
                    acknowledged: false,
                    message: format!("Run {} already in terminal state: {:?}", run_id, exec.status),
                }));
            }
        }
        drop(executions);

        // Set the cancellation flag.
        let mut flags = self.state.cancel_flags.write().await;
        flags.insert(run_id.clone());

        tracing::info!(run_id = %run_id, "pipeline cancellation requested");

        Ok(Response::new(pb::engine::CancelPipelineResponse {
            acknowledged: true,
            message: format!("Cancellation requested for run {}", run_id),
        }))
    }

    /// Get current execution state for a pipeline run.
    async fn get_execution_state(
        &self,
        request: Request<pb::engine::GetExecutionStateRequest>,
    ) -> Result<Response<pb::execution::PipelineExecution>, Status> {
        let run_id = request.into_inner().run_id;
        if run_id.is_empty() {
            return Err(Status::invalid_argument("run_id is required"));
        }

        let executions = self.state.executions.read().await;
        match executions.get(&run_id) {
            Some(exec) => {
                let proto_exec = to_proto_execution(exec);
                Ok(Response::new(proto_exec))
            }
            None => Err(Status::not_found(format!("Run {} not found", run_id))),
        }
    }

    /// Get aggregated health report.
    async fn get_health(
        &self,
        _request: Request<pb::engine::GetHealthRequest>,
    ) -> Result<Response<pb::scaling::HealthReport>, Status> {
        let uptime = self.state.start_time.elapsed();
        let exec_count = self.state.executions.read().await.len();

        // Build a basic health report. In Phase 3.2+, this will include
        // actual runtime metrics from the scaling controller.
        let report = pb::scaling::HealthReport {
            system: Some(pb::scaling::ResourceMetrics {
                cpu_percent: 0.0,
                memory_percent: 0.0,
                memory_used_bytes: 0,
                memory_total_bytes: 0,
                disk_percent: 0.0,
                disk_used_bytes: 0,
                disk_total_bytes: 0,
                source: 0, // UNSPECIFIED
                timestamp: uptime.as_secs_f64(),
            }),
            runtimes: vec![],
            recent_decisions: vec![],
            total_inflight_pipelines: exec_count as i32,
            max_inflight_pipelines: 100,
            accepting_submissions: true,
        };

        Ok(Response::new(report))
    }

    /// Get per-runtime status.
    async fn get_runtime_status(
        &self,
        _request: Request<pb::engine::GetRuntimeStatusRequest>,
    ) -> Result<Response<pb::engine::RuntimeStatusResponse>, Status> {
        // Placeholder: no runtimes registered yet.
        // Phase 3.2 will populate this from the dispatcher.
        Ok(Response::new(pb::engine::RuntimeStatusResponse {
            runtimes: vec![],
        }))
    }

    /// Server-streaming response type for StreamHealth.
    type StreamHealthStream = Pin<
        Box<
            dyn tokio_stream::Stream<Item = Result<pb::scaling::HealthReport, Status>>
                + Send
                + 'static,
        >,
    >;

    /// Stream health updates at a configurable interval.
    async fn stream_health(
        &self,
        _request: Request<pb::engine::StreamHealthRequest>,
    ) -> Result<Response<Self::StreamHealthStream>, Status> {
        Err(Status::unimplemented(
            "StreamHealth not yet implemented — coming in Phase 3.2",
        ))
    }
}

// ---------------------------------------------------------------------------
// Proto conversion helpers
// ---------------------------------------------------------------------------

/// Convert an internal PipelineExecution to the proto message.
fn to_proto_execution(exec: &PipelineExecution) -> pb::execution::PipelineExecution {
    let status = match exec.status {
        PipelineRunStatus::Pending => 1,   // PIPELINE_RUN_STATUS_PENDING
        PipelineRunStatus::Running => 2,   // PIPELINE_RUN_STATUS_RUNNING
        PipelineRunStatus::Success => 3,   // PIPELINE_RUN_STATUS_SUCCESS
        PipelineRunStatus::Failed => 4,    // PIPELINE_RUN_STATUS_FAILED
        PipelineRunStatus::Cancelled => 5, // PIPELINE_RUN_STATUS_CANCELLED
    };

    let node_states: std::collections::HashMap<String, pb::execution::NodeState> = exec
        .node_states
        .iter()
        .map(|(id, ns)| {
            let ns_status = match ns.status {
                NodeStatus::Pending => 1,   // NODE_STATUS_PENDING
                NodeStatus::Running => 2,   // NODE_STATUS_RUNNING
                NodeStatus::Success => 3,   // NODE_STATUS_SUCCESS
                NodeStatus::Failed => 4,    // NODE_STATUS_FAILED
                NodeStatus::Skipped => 5,   // NODE_STATUS_SKIPPED
                NodeStatus::Cancelled => 6, // NODE_STATUS_CANCELLED
            };
            (
                id.clone(),
                pb::execution::NodeState {
                    node_id: id.clone(),
                    status: ns_status,
                    start_time: ns.start_time,
                    end_time: ns.end_time,
                    error: ns.error.clone(),
                    result_ref: ns.result_ref.clone(),
                },
            )
        })
        .collect();

    pb::execution::PipelineExecution {
        run_id: exec.run_id.clone(),
        session_id: exec.session_id.clone(),
        pipeline_id: exec.pipeline_id.clone(),
        uid: exec.uid.clone(),
        status,
        start_time: exec.start_time,
        end_time: exec.end_time,
        node_states,
    }
}

// ---------------------------------------------------------------------------
// Server startup helper
// ---------------------------------------------------------------------------

/// Start the gRPC server on the given address.
///
/// This is called from `main.rs` to launch the engine's gRPC interface.
pub async fn start_grpc_server(
    addr: std::net::SocketAddr,
    state: Arc<EngineState>,
) -> Result<(), Box<dyn std::error::Error>> {
    let service = EngineServiceImpl::new(state);

    tracing::info!(%addr, "starting gRPC server");

    tonic::transport::Server::builder()
        .add_service(pb::engine::engine_service_server::EngineServiceServer::new(service))
        .serve(addr)
        .await?;

    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_engine_state_default() {
        let state = EngineState::new();
        assert!(state.start_time.elapsed().as_secs() < 1);
    }

    #[tokio::test]
    async fn test_cancel_empty_run_id() {
        let state = Arc::new(EngineState::new());
        let svc = EngineServiceImpl::new(state);

        let req = Request::new(pb::engine::CancelPipelineRequest {
            run_id: String::new(),
        });
        let result = svc.cancel_pipeline(req).await;
        assert!(result.is_err());
        assert_eq!(result.unwrap_err().code(), tonic::Code::InvalidArgument);
    }

    #[tokio::test]
    async fn test_cancel_nonexistent_run() {
        let state = Arc::new(EngineState::new());
        let svc = EngineServiceImpl::new(state.clone());

        let req = Request::new(pb::engine::CancelPipelineRequest {
            run_id: "run-123".to_string(),
        });
        let result = svc.cancel_pipeline(req).await;
        assert!(result.is_ok());
        let resp = result.unwrap().into_inner();
        assert!(resp.acknowledged);

        // Verify cancel flag was set.
        let flags = state.cancel_flags.read().await;
        assert!(flags.contains("run-123"));
    }

    #[tokio::test]
    async fn test_get_execution_state_not_found() {
        let state = Arc::new(EngineState::new());
        let svc = EngineServiceImpl::new(state);

        let req = Request::new(pb::engine::GetExecutionStateRequest {
            run_id: "nonexistent".to_string(),
        });
        let result = svc.get_execution_state(req).await;
        assert!(result.is_err());
        assert_eq!(result.unwrap_err().code(), tonic::Code::NotFound);
    }

    #[tokio::test]
    async fn test_get_execution_state_found() {
        let state = Arc::new(EngineState::new());
        let exec = PipelineExecution::new(
            "run-1".to_string(),
            "sess-1".to_string(),
            "pipe-1".to_string(),
            "uid-1".to_string(),
        );
        state
            .executions
            .write()
            .await
            .insert("run-1".to_string(), exec);

        let svc = EngineServiceImpl::new(state);
        let req = Request::new(pb::engine::GetExecutionStateRequest {
            run_id: "run-1".to_string(),
        });
        let result = svc.get_execution_state(req).await;
        assert!(result.is_ok());
        let proto_exec = result.unwrap().into_inner();
        assert_eq!(proto_exec.run_id, "run-1");
        assert_eq!(proto_exec.status, 1); // PENDING
    }

    #[tokio::test]
    async fn test_get_health() {
        let state = Arc::new(EngineState::new());
        let svc = EngineServiceImpl::new(state);

        let req = Request::new(pb::engine::GetHealthRequest {});
        let result = svc.get_health(req).await;
        assert!(result.is_ok());
        let report = result.unwrap().into_inner();
        assert!(report.accepting_submissions);
        assert!(report.system.is_some());
    }

    #[tokio::test]
    async fn test_cancel_terminal_state_run() {
        let state = Arc::new(EngineState::new());
        let mut exec = PipelineExecution::new(
            "run-done".to_string(),
            "sess-1".to_string(),
            "pipe-1".to_string(),
            "uid-1".to_string(),
        );
        exec.status = PipelineRunStatus::Success;
        state
            .executions
            .write()
            .await
            .insert("run-done".to_string(), exec);

        let svc = EngineServiceImpl::new(state);
        let req = Request::new(pb::engine::CancelPipelineRequest {
            run_id: "run-done".to_string(),
        });
        let result = svc.cancel_pipeline(req).await;
        assert!(result.is_ok());
        let resp = result.unwrap().into_inner();
        assert!(!resp.acknowledged);
        assert!(resp.message.contains("terminal state"));
    }

    #[tokio::test]
    async fn test_get_runtime_status_empty() {
        let state = Arc::new(EngineState::new());
        let svc = EngineServiceImpl::new(state);

        let req = Request::new(pb::engine::GetRuntimeStatusRequest {});
        let result = svc.get_runtime_status(req).await;
        assert!(result.is_ok());
        assert!(result.unwrap().into_inner().runtimes.is_empty());
    }

    #[tokio::test]
    async fn test_stream_health_unimplemented() {
        let state = Arc::new(EngineState::new());
        let svc = EngineServiceImpl::new(state);

        let req = Request::new(pb::engine::StreamHealthRequest {
            interval_seconds: 1.0,
        });
        let result = svc.stream_health(req).await;
        match result {
            Err(status) => assert_eq!(status.code(), tonic::Code::Unimplemented),
            Ok(_) => panic!("expected Unimplemented error"),
        }
    }

    #[tokio::test]
    async fn test_get_health_with_active_executions() {
        let state = Arc::new(EngineState::new());
        let exec = PipelineExecution::new(
            "run-active".to_string(),
            "sess-1".to_string(),
            "pipe-1".to_string(),
            "uid-1".to_string(),
        );
        state
            .executions
            .write()
            .await
            .insert("run-active".to_string(), exec);

        let svc = EngineServiceImpl::new(state);
        let req = Request::new(pb::engine::GetHealthRequest {});
        let result = svc.get_health(req).await;
        assert!(result.is_ok());
        let report = result.unwrap().into_inner();
        assert_eq!(report.total_inflight_pipelines, 1);
    }

    #[tokio::test]
    async fn test_to_proto_execution_with_node_states() {
        let state = Arc::new(EngineState::new());
        let mut exec = PipelineExecution::new(
            "run-ns".to_string(),
            "sess-1".to_string(),
            "pipe-1".to_string(),
            "uid-1".to_string(),
        );
        // Add a node state manually.
        exec.node_states.insert(
            "node-1".to_string(),
            crate::state::NodeState::new("node-1".to_string()),
        );
        state
            .executions
            .write()
            .await
            .insert("run-ns".to_string(), exec);

        let svc = EngineServiceImpl::new(state);
        let req = Request::new(pb::engine::GetExecutionStateRequest {
            run_id: "run-ns".to_string(),
        });
        let result = svc.get_execution_state(req).await;
        assert!(result.is_ok());
        let proto_exec = result.unwrap().into_inner();
        assert_eq!(proto_exec.node_states.len(), 1);
        let ns = proto_exec.node_states.get("node-1").unwrap();
        assert_eq!(ns.node_id, "node-1");
        assert_eq!(ns.status, 1); // PENDING
    }

    #[tokio::test]
    async fn test_cleanup_terminal() {
        let state = Arc::new(EngineState::new());

        // Insert an execution and a cancel flag.
        let exec = PipelineExecution::new(
            "run-x".to_string(),
            "sess-1".to_string(),
            "pipe-1".to_string(),
            "uid-1".to_string(),
        );
        state
            .executions
            .write()
            .await
            .insert("run-x".to_string(), exec);
        state
            .cancel_flags
            .write()
            .await
            .insert("run-x".to_string());

        // Cleanup should remove both.
        state.cleanup_terminal("run-x").await;

        assert!(!state.executions.read().await.contains_key("run-x"));
        assert!(!state.cancel_flags.read().await.contains("run-x"));
    }

    #[tokio::test]
    async fn test_execute_pipeline_unimplemented() {
        let state = Arc::new(EngineState::new());
        let svc = EngineServiceImpl::new(state);

        let req = Request::new(pb::engine::ExecutePipelineRequest {
            graph: None,
            session_id: "s".to_string(),
            pipeline_id: "p".to_string(),
            uid: "u".to_string(),
            run_id: "r".to_string(),
            context: Default::default(),
        });
        let result = svc.execute_pipeline(req).await;
        match result {
            Err(status) => assert_eq!(status.code(), tonic::Code::Unimplemented),
            Ok(_) => panic!("expected Unimplemented error"),
        }
    }
}
