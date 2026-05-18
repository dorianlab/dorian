//! Container Runtime — container-based operator execution.
//!
//! Phase 6 stub. Will be implemented for heavy operators that need
//! their own isolated environment (custom Python packages, GPU access,
//! large model inference, etc.).
//!
//! Target use cases:
//! - Heavy ML model inference (large transformers, diffusion models)
//! - Operators with non-standard Python dependencies
//! - GPU-accelerated workloads
//! - Untrusted third-party operator packages
//!
//! Architecture:
//! - Each container runs a lightweight gRPC sidecar that receives NodeTask
//! - Container images are pre-built per operator family (sklearn, torch, etc.)
//! - Scaling controller manages container pool (create/destroy)
//! - On Kubernetes: uses Job API; on bare metal: uses Docker/Podman CLI

use crate::runtime::{
    NodeResult, NodeTask, Runtime, RuntimeCapabilities, RuntimeError, RuntimeHealth, RuntimeKind,
    SubmitResponse,
};

// ---------------------------------------------------------------------------
// Container Runtime (stub)
// ---------------------------------------------------------------------------

/// Container Runtime — isolated container-based execution.
///
/// Phase 6 stub. The implementation will use Docker/Podman CLI on bare metal
/// and Kubernetes Job API in container environments.
pub struct ContainerRuntime {
    /// Maximum concurrent containers.
    pub max_containers: u32,
    /// Container image registry prefix.
    pub registry: String,
    /// Default memory limit per container.
    pub memory_limit_mb: u64,
    /// Default CPU limit (millicores).
    pub cpu_limit_millicores: u32,
}

impl Default for ContainerRuntime {
    fn default() -> Self {
        Self {
            max_containers: 4,
            registry: "dorian-runtime".to_string(),
            memory_limit_mb: 2048,
            cpu_limit_millicores: 2000,
        }
    }
}

impl ContainerRuntime {
    pub fn new(max_containers: u32) -> Self {
        Self {
            max_containers,
            ..Default::default()
        }
    }
}

#[async_trait::async_trait]
impl Runtime for ContainerRuntime {
    fn kind(&self) -> RuntimeKind {
        RuntimeKind::Container
    }

    fn capabilities(&self) -> RuntimeCapabilities {
        RuntimeCapabilities {
            kind: RuntimeKind::Container,
            supported_languages: vec![
                "python".to_string(),
                "r".to_string(),
                "julia".to_string(),
            ],
            supports_cancellation: true,
            supports_streaming: false,
            max_concurrent_tasks: self.max_containers,
        }
    }

    fn health(&self) -> RuntimeHealth {
        // Stub: always healthy, no active containers.
        RuntimeHealth {
            kind: RuntimeKind::Container,
            healthy: true,
            current_workers: 0,
            max_workers: self.max_containers,
            queue_depth: 0,
            queue_capacity: self.max_containers * 2,
            inflight_tasks: 0,
            cpu_percent: 0.0,
            memory_percent: 0.0,
        }
    }

    async fn submit(&self, task: NodeTask) -> Result<SubmitResponse, RuntimeError> {
        let _ = &task;
        Err(RuntimeError::Internal(
            "Container runtime not yet implemented (Phase 6 stub)".to_string(),
        ))
    }

    async fn wait(&self, task_id: &str) -> Result<NodeResult, RuntimeError> {
        Err(RuntimeError::TaskNotFound(task_id.to_string()))
    }

    async fn cancel(&self, task_id: &str) -> Result<(), RuntimeError> {
        Err(RuntimeError::TaskNotFound(task_id.to_string()))
    }

    fn queue_depth(&self) -> u32 {
        0
    }

    fn queue_capacity(&self) -> u32 {
        self.max_containers * 2
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_container_runtime_defaults() {
        let rt = ContainerRuntime::default();
        assert_eq!(rt.max_containers, 4);
        assert_eq!(rt.memory_limit_mb, 2048);
        assert_eq!(rt.cpu_limit_millicores, 2000);
        assert_eq!(rt.kind(), RuntimeKind::Container);
    }

    #[test]
    fn test_container_capabilities() {
        let rt = ContainerRuntime::new(8);
        let caps = rt.capabilities();
        assert_eq!(caps.kind, RuntimeKind::Container);
        assert!(caps.supported_languages.contains(&"python".to_string()));
        assert!(caps.supports_cancellation);
        assert_eq!(caps.max_concurrent_tasks, 8);
    }

    #[test]
    fn test_container_health() {
        let rt = ContainerRuntime::default();
        let health = rt.health();
        assert!(health.healthy);
        assert_eq!(health.current_workers, 0);
    }

    #[tokio::test]
    async fn test_container_submit_returns_not_implemented() {
        let rt = ContainerRuntime::default();
        let task = NodeTask {
            task_id: "t1".into(),
            run_id: "r1".into(),
            node_id: "n1".into(),
            payload: vec![],
            inputs: vec![],
            context: std::collections::HashMap::new(),
            timeout_seconds: 0.0,
        };
        let result = rt.submit(task).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_container_wait_task_not_found() {
        let rt = ContainerRuntime::default();
        let result = rt.wait("nonexistent").await;
        assert!(result.is_err());
    }

    #[test]
    fn test_container_queue_depth() {
        let rt = ContainerRuntime::new(4);
        assert_eq!(rt.queue_depth(), 0);
        assert_eq!(rt.queue_capacity(), 8);
    }
}
