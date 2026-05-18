//! WASM Runtime — Wasmtime/Wasmer sandbox for user-authored code.
//!
//! Phase 6 stub. Will be implemented when user snippets and ranking
//! objectives need sandbox isolation beyond Python subprocess restrictions.
//!
//! Target use cases:
//! - User-defined snippets (currently Python `exec()` with whitelisted builtins)
//! - Custom ranking objectives (`def score(candidate, ctx) -> float`)
//! - Lightweight pure functions that don't need Python ecosystem
//!
//! Benefits over Python subprocess:
//! - Sub-millisecond cold start (no Python interpreter boot)
//! - Memory-safe sandbox with fine-grained resource limits
//! - Language-agnostic: Rust, C, AssemblyScript, Grain → WASM
//! - Deterministic execution (useful for ranking reproducibility)

use crate::runtime::{
    NodeResult, NodeTask, Runtime, RuntimeCapabilities, RuntimeError,
    RuntimeHealth, RuntimeKind, SubmitResponse,
};

// ---------------------------------------------------------------------------
// WASM Runtime (stub)
// ---------------------------------------------------------------------------

/// WASM Runtime — Wasmtime sandbox for user snippets and objectives.
///
/// This is a Phase 6 stub. The implementation will use `wasmtime` crate
/// to execute `.wasm` modules compiled from user code.
pub struct WasmRuntime {
    /// Maximum concurrent WASM instances.
    pub max_instances: u32,
    /// Memory limit per instance (bytes).
    pub memory_limit_bytes: u64,
    /// CPU instruction limit per invocation.
    pub fuel_limit: u64,
}

impl Default for WasmRuntime {
    fn default() -> Self {
        Self {
            max_instances: 16,
            memory_limit_bytes: 64 * 1024 * 1024, // 64 MB
            fuel_limit: 1_000_000_000,             // ~1 billion instructions
        }
    }
}

impl WasmRuntime {
    pub fn new(max_instances: u32) -> Self {
        Self {
            max_instances,
            ..Default::default()
        }
    }
}

#[async_trait::async_trait]
impl Runtime for WasmRuntime {
    fn kind(&self) -> RuntimeKind {
        RuntimeKind::Wasm
    }

    fn capabilities(&self) -> RuntimeCapabilities {
        RuntimeCapabilities {
            kind: RuntimeKind::Wasm,
            supported_languages: vec![
                "wasm".to_string(),
                "rust".to_string(),
                "assemblyscript".to_string(),
            ],
            supports_cancellation: true,
            supports_streaming: false,
            max_concurrent_tasks: self.max_instances,
        }
    }

    fn health(&self) -> RuntimeHealth {
        // Stub: always healthy, no active workers.
        RuntimeHealth {
            kind: RuntimeKind::Wasm,
            healthy: true,
            current_workers: 0,
            max_workers: self.max_instances,
            queue_depth: 0,
            queue_capacity: self.max_instances * 4,
            inflight_tasks: 0,
            cpu_percent: 0.0,
            memory_percent: 0.0,
        }
    }

    async fn submit(&self, task: NodeTask) -> Result<SubmitResponse, RuntimeError> {
        // Stub: accept but return a placeholder result immediately.
        // Real implementation will compile + instantiate WASM module.
        let _ = &task;
        Err(RuntimeError::Internal(
            "WASM runtime not yet implemented (Phase 6 stub)".to_string(),
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
        self.max_instances * 4
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_wasm_runtime_defaults() {
        let rt = WasmRuntime::default();
        assert_eq!(rt.max_instances, 16);
        assert_eq!(rt.memory_limit_bytes, 64 * 1024 * 1024);
        assert_eq!(rt.kind(), RuntimeKind::Wasm);
    }

    #[test]
    fn test_wasm_capabilities() {
        let rt = WasmRuntime::new(8);
        let caps = rt.capabilities();
        assert_eq!(caps.kind, RuntimeKind::Wasm);
        assert!(caps.supported_languages.contains(&"wasm".to_string()));
        assert!(caps.supports_cancellation);
        assert!(!caps.supports_streaming);
        assert_eq!(caps.max_concurrent_tasks, 8);
    }

    #[test]
    fn test_wasm_health() {
        let rt = WasmRuntime::default();
        let health = rt.health();
        assert!(health.healthy);
        assert_eq!(health.current_workers, 0);
    }

    #[tokio::test]
    async fn test_wasm_submit_returns_not_implemented() {
        let rt = WasmRuntime::default();
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
    async fn test_wasm_wait_task_not_found() {
        let rt = WasmRuntime::default();
        let result = rt.wait("nonexistent").await;
        assert!(result.is_err());
    }

    #[test]
    fn test_wasm_queue_depth() {
        let rt = WasmRuntime::new(4);
        assert_eq!(rt.queue_depth(), 0);
        assert_eq!(rt.queue_capacity(), 16);
    }
}
