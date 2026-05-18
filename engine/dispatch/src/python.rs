//! Python Runtime — Redis-based task dispatch to Python worker processes.
//!
//! The Python runtime sends tasks to the Python runtime bridge via Redis
//! streams. This avoids in-process PyO3 coupling and provides clean
//! isolation: the Rust engine never loads a Python interpreter.
//!
//! Protocol:
//!   Engine → Redis Stream "runtime:python:tasks"  (XADD task fields)
//!   Bridge → Redis Stream "runtime:python:results" (XADD result fields)
//!   Engine reads results, matches by task_id.
//!
//! The Python runtime bridge (`dorian/workers/runtime_bridge.py`) reads
//! tasks from the stream, executes operators/snippets/parameters, and
//! writes results back.
//!
//! Backpressure: The runtime tracks inflight tasks and rejects new
//! submissions when the queue is full. The bridge processes tasks
//! sequentially per worker, so throughput scales with worker count.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use tokio::sync::RwLock;
use uuid::Uuid;

use crate::runtime::{
    InputData, NodeResult, NodeResultStatus, NodeTask, Runtime, RuntimeCapabilities,
    RuntimeError, RuntimeHealth, RuntimeKind, SubmitResponse,
};

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/// Configuration for the Python runtime.
#[derive(Debug, Clone)]
pub struct PythonRuntimeConfig {
    /// Redis URL for task/result streams.
    pub redis_url: String,
    /// Redis stream key for tasks (engine → bridge).
    pub task_stream: String,
    /// Redis stream key for results (bridge → engine).
    pub result_stream: String,
    /// Consumer group name for reading results.
    pub result_group: String,
    /// Maximum concurrent tasks (queue capacity).
    pub max_queue_depth: u32,
    /// Maximum workers (for health reporting).
    pub max_workers: u32,
    /// Task timeout in seconds (0 = no timeout).
    pub default_timeout_seconds: f64,
}

impl Default for PythonRuntimeConfig {
    fn default() -> Self {
        Self {
            // Placeholder — callers always override with the real
// DORIAN_REDIS_URL from env. 6379 is the upstream default,
// not the deploy port (which lives in .env).
redis_url: "redis://localhost:6379".to_string(),
            task_stream: "runtime:python:tasks".to_string(),
            result_stream: "runtime:python:results".to_string(),
            result_group: "engine-results".to_string(),
            max_queue_depth: 64,
            max_workers: 8,
            default_timeout_seconds: 300.0,
        }
    }
}

// ---------------------------------------------------------------------------
// Python Runtime
// ---------------------------------------------------------------------------

/// Python Runtime — dispatches to Python workers via Redis streams.
///
/// This is the production implementation of the Runtime trait for Python.
/// It communicates with `dorian/workers/runtime_bridge.py` using Redis
/// streams as the transport.
pub struct PythonRuntime {
    config: PythonRuntimeConfig,
    healthy: AtomicBool,
    inflight: AtomicU32,
    /// Pending results indexed by task_id. Workers complete tasks async;
    /// results are collected in `wait()`.
    pending: RwLock<HashMap<String, Option<NodeResult>>>,
}

impl PythonRuntime {
    /// Create a new Python runtime with the given configuration.
    pub fn new(config: PythonRuntimeConfig) -> Self {
        Self {
            config,
            healthy: AtomicBool::new(true),
            inflight: AtomicU32::new(0),
            pending: RwLock::new(HashMap::new()),
        }
    }

    /// Convert a NodeTask into Redis stream fields for the bridge.
    fn task_to_fields(task: &NodeTask) -> Vec<(String, String)> {
        // Determine node type from context.
        let node_type = task
            .context
            .get("node_type")
            .cloned()
            .unwrap_or_else(|| "operator".to_string());
        let name = task
            .context
            .get("name")
            .cloned()
            .unwrap_or_default();
        let language = task
            .context
            .get("language")
            .cloned()
            .unwrap_or_else(|| "python".to_string());
        let code = task
            .context
            .get("code")
            .cloned()
            .unwrap_or_default();
        let dtype = task
            .context
            .get("dtype")
            .cloned()
            .unwrap_or_default();
        let value = task
            .context
            .get("value")
            .cloned()
            .unwrap_or_default();

        // Serialize inputs as JSON.
        let inputs_map: HashMap<String, String> = task
            .inputs
            .iter()
            .map(|inp| {
                let data_str = match &inp.data {
                    InputData::Inline(bytes) => String::from_utf8_lossy(bytes).to_string(),
                    InputData::Reference(r) => r.clone(),
                };
                (inp.input_keyword.clone(), data_str)
            })
            .collect();
        let inputs_json = serde_json::to_string(&inputs_map).unwrap_or_else(|_| "{}".to_string());

        let context_json =
            serde_json::to_string(&task.context).unwrap_or_else(|_| "{}".to_string());

        vec![
            ("task_id".to_string(), task.task_id.clone()),
            ("run_id".to_string(), task.run_id.clone()),
            ("node_id".to_string(), task.node_id.clone()),
            ("node_type".to_string(), node_type),
            ("name".to_string(), name),
            ("language".to_string(), language),
            ("code".to_string(), code),
            ("dtype".to_string(), dtype),
            ("value".to_string(), value),
            ("inputs".to_string(), inputs_json),
            ("context".to_string(), context_json),
            ("timeout".to_string(), task.timeout_seconds.to_string()),
        ]
    }
}

#[async_trait::async_trait]
impl Runtime for PythonRuntime {
    fn kind(&self) -> RuntimeKind {
        RuntimeKind::Python
    }

    fn capabilities(&self) -> RuntimeCapabilities {
        RuntimeCapabilities {
            kind: RuntimeKind::Python,
            supported_languages: vec!["python".to_string()],
            supports_cancellation: true,
            supports_streaming: false,
            max_concurrent_tasks: self.config.max_queue_depth,
        }
    }

    fn health(&self) -> RuntimeHealth {
        let inflight = self.inflight.load(Ordering::Relaxed);
        RuntimeHealth {
            kind: RuntimeKind::Python,
            healthy: self.healthy.load(Ordering::Relaxed),
            current_workers: 0, // Managed by scaling controller, not tracked here.
            max_workers: self.config.max_workers,
            queue_depth: inflight,
            queue_capacity: self.config.max_queue_depth,
            inflight_tasks: inflight,
            cpu_percent: 0.0,
            memory_percent: 0.0,
        }
    }

    async fn submit(&self, task: NodeTask) -> Result<SubmitResponse, RuntimeError> {
        let inflight = self.inflight.load(Ordering::Relaxed);

        // Backpressure: reject if queue is full.
        if inflight >= self.config.max_queue_depth {
            return Ok(SubmitResponse::Rejected {
                retry_after_seconds: 1.0,
            });
        }

        // Throttle when approaching capacity (>75%).
        let throttle = inflight > (self.config.max_queue_depth * 3 / 4);

        let task_id = task.task_id.clone();
        let fields = Self::task_to_fields(&task);

        // Publish to Redis task stream.
        let client = redis::Client::open(self.config.redis_url.as_str())
            .map_err(|e| RuntimeError::Internal(format!("redis connect: {e}")))?;
        let mut conn = client
            .get_multiplexed_async_connection()
            .await
            .map_err(|e| RuntimeError::Internal(format!("redis connection: {e}")))?;

        // XADD to task stream.
        redis::cmd("XADD")
            .arg(&self.config.task_stream)
            .arg("*")
            .arg(&fields)
            .query_async::<String>(&mut conn)
            .await
            .map_err(|e| RuntimeError::Internal(format!("xadd failed: {e}")))?;

        // Track inflight.
        self.inflight.fetch_add(1, Ordering::Relaxed);

        // Register pending result slot.
        {
            let mut pending = self.pending.write().await;
            pending.insert(task_id.clone(), None);
        }

        if throttle {
            Ok(SubmitResponse::Throttle {
                task_id,
                retry_after_seconds: 0.5,
            })
        } else {
            Ok(SubmitResponse::Accepted { task_id })
        }
    }

    async fn wait(&self, task_id: &str) -> Result<NodeResult, RuntimeError> {
        // Poll the Redis result stream for this task's result.
        // In production, this would use a more efficient pubsub or callback mechanism.
        let client = redis::Client::open(self.config.redis_url.as_str())
            .map_err(|e| RuntimeError::Internal(format!("redis connect: {e}")))?;
        let mut conn = client
            .get_multiplexed_async_connection()
            .await
            .map_err(|e| RuntimeError::Internal(format!("redis connection: {e}")))?;

        // Ensure consumer group exists.
        let _: Result<(), _> = redis::cmd("XGROUP")
            .arg("CREATE")
            .arg(&self.config.result_stream)
            .arg(&self.config.result_group)
            .arg("0")
            .arg("MKSTREAM")
            .query_async(&mut conn)
            .await;

        let consumer = format!("engine-{}", Uuid::new_v4());
        let timeout_secs = self.config.default_timeout_seconds;
        let deadline = std::time::Instant::now()
            + std::time::Duration::from_secs_f64(timeout_secs);

        loop {
            if std::time::Instant::now() > deadline {
                self.inflight.fetch_sub(1, Ordering::Relaxed);
                return Err(RuntimeError::Timeout(
                    task_id.to_string(),
                    timeout_secs,
                ));
            }

            // XREADGROUP with short block.
            let results: Vec<(String, Vec<(String, HashMap<String, String>)>)> =
                redis::cmd("XREADGROUP")
                    .arg("GROUP")
                    .arg(&self.config.result_group)
                    .arg(&consumer)
                    .arg("COUNT")
                    .arg(10)
                    .arg("BLOCK")
                    .arg(1000) // 1s block
                    .arg("STREAMS")
                    .arg(&self.config.result_stream)
                    .arg(">")
                    .query_async(&mut conn)
                    .await
                    .unwrap_or_default();

            for (_stream, messages) in &results {
                for (msg_id, fields) in messages {
                    // ACK the message.
                    let _: Result<(), _> = redis::cmd("XACK")
                        .arg(&self.config.result_stream)
                        .arg(&self.config.result_group)
                        .arg(msg_id)
                        .query_async(&mut conn)
                        .await;

                    let result_task_id = fields.get("task_id").cloned().unwrap_or_default();
                    let node_id = fields.get("node_id").cloned().unwrap_or_default();
                    let status_str = fields.get("status").cloned().unwrap_or_default();
                    let error_msg = fields.get("error_message").cloned().unwrap_or_default();
                    let error_tb = fields.get("error_traceback").cloned().unwrap_or_default();
                    let duration: f64 = fields
                        .get("duration_seconds")
                        .and_then(|s| s.parse().ok())
                        .unwrap_or(0.0);

                    let status = match status_str.as_str() {
                        "success" => NodeResultStatus::Success,
                        "failed" => NodeResultStatus::Failed,
                        "cancelled" => NodeResultStatus::Cancelled,
                        "timeout" => NodeResultStatus::Timeout,
                        _ => NodeResultStatus::Failed,
                    };

                    let result = NodeResult {
                        task_id: result_task_id.clone(),
                        node_id,
                        status,
                        outputs: vec![],
                        error_message: if error_msg.is_empty() {
                            None
                        } else {
                            Some(error_msg)
                        },
                        error_traceback: if error_tb.is_empty() {
                            None
                        } else {
                            Some(error_tb)
                        },
                        duration_seconds: duration,
                    };

                    // Store result for any task (may be for a different waiter).
                    {
                        let mut pending = self.pending.write().await;
                        if pending.contains_key(&result_task_id) {
                            pending.insert(result_task_id.clone(), Some(result.clone()));
                        }
                    }

                    // Check if this is the one we're waiting for.
                    if result_task_id == task_id {
                        self.inflight.fetch_sub(1, Ordering::Relaxed);
                        let mut pending = self.pending.write().await;
                        pending.remove(task_id);
                        return Ok(result);
                    }
                }
            }
        }
    }

    async fn cancel(&self, task_id: &str) -> Result<(), RuntimeError> {
        // Publish a cancellation message to the task stream.
        // The bridge should check for cancellation before executing.
        let client = redis::Client::open(self.config.redis_url.as_str())
            .map_err(|e| RuntimeError::Internal(format!("redis connect: {e}")))?;
        let mut conn = client
            .get_multiplexed_async_connection()
            .await
            .map_err(|e| RuntimeError::Internal(format!("redis connection: {e}")))?;

        let fields = vec![
            ("task_id", task_id),
            ("node_type", "cancel"),
            ("node_id", ""),
            ("run_id", ""),
        ];

        let _: String = redis::cmd("XADD")
            .arg(&self.config.task_stream)
            .arg("*")
            .arg(&fields)
            .query_async(&mut conn)
            .await
            .map_err(|e| RuntimeError::Internal(format!("cancel xadd: {e}")))?;

        self.inflight.fetch_sub(1, Ordering::Relaxed);
        let mut pending = self.pending.write().await;
        pending.remove(task_id);

        Ok(())
    }

    fn queue_depth(&self) -> u32 {
        self.inflight.load(Ordering::Relaxed)
    }

    fn queue_capacity(&self) -> u32 {
        self.config.max_queue_depth
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn test_config() -> PythonRuntimeConfig {
        PythonRuntimeConfig {
            // Placeholder — callers always override with the real
// DORIAN_REDIS_URL from env. 6379 is the upstream default,
// not the deploy port (which lives in .env).
redis_url: "redis://localhost:6379".to_string(),
            task_stream: "test:python:tasks".to_string(),
            result_stream: "test:python:results".to_string(),
            result_group: "test-engine".to_string(),
            max_queue_depth: 10,
            max_workers: 4,
            default_timeout_seconds: 30.0,
        }
    }

    #[test]
    fn test_python_runtime_kind() {
        let rt = PythonRuntime::new(test_config());
        assert_eq!(rt.kind(), RuntimeKind::Python);
    }

    #[test]
    fn test_python_capabilities() {
        let rt = PythonRuntime::new(test_config());
        let caps = rt.capabilities();
        assert_eq!(caps.kind, RuntimeKind::Python);
        assert!(caps.supported_languages.contains(&"python".to_string()));
        assert!(caps.supports_cancellation);
        assert_eq!(caps.max_concurrent_tasks, 10);
    }

    #[test]
    fn test_python_health() {
        let rt = PythonRuntime::new(test_config());
        let health = rt.health();
        assert!(health.healthy);
        assert_eq!(health.queue_depth, 0);
        assert_eq!(health.queue_capacity, 10);
        assert_eq!(health.max_workers, 4);
    }

    #[test]
    fn test_python_queue_depth() {
        let rt = PythonRuntime::new(test_config());
        assert_eq!(rt.queue_depth(), 0);
        assert_eq!(rt.queue_capacity(), 10);
    }

    #[test]
    fn test_task_to_fields() {
        let task = NodeTask {
            task_id: "t1".to_string(),
            run_id: "r1".to_string(),
            node_id: "n1".to_string(),
            payload: vec![],
            inputs: vec![],
            context: {
                let mut ctx = HashMap::new();
                ctx.insert("node_type".to_string(), "operator".to_string());
                ctx.insert(
                    "name".to_string(),
                    "sklearn.preprocessing.StandardScaler".to_string(),
                );
                ctx
            },
            timeout_seconds: 60.0,
        };

        let fields = PythonRuntime::task_to_fields(&task);
        let field_map: HashMap<String, String> = fields.into_iter().collect();

        assert_eq!(field_map.get("task_id").unwrap(), "t1");
        assert_eq!(field_map.get("run_id").unwrap(), "r1");
        assert_eq!(field_map.get("node_id").unwrap(), "n1");
        assert_eq!(field_map.get("node_type").unwrap(), "operator");
        assert_eq!(
            field_map.get("name").unwrap(),
            "sklearn.preprocessing.StandardScaler"
        );
        assert_eq!(field_map.get("timeout").unwrap(), "60");
    }

    #[test]
    fn test_config_defaults() {
        let cfg = PythonRuntimeConfig::default();
        assert_eq!(cfg.task_stream, "runtime:python:tasks");
        assert_eq!(cfg.result_stream, "runtime:python:results");
        assert_eq!(cfg.max_queue_depth, 64);
        assert_eq!(cfg.max_workers, 8);
        assert_eq!(cfg.default_timeout_seconds, 300.0);
    }

    #[test]
    fn test_inflight_tracking() {
        let rt = PythonRuntime::new(test_config());
        assert_eq!(rt.inflight.load(Ordering::Relaxed), 0);
        rt.inflight.fetch_add(1, Ordering::Relaxed);
        assert_eq!(rt.queue_depth(), 1);
        rt.inflight.fetch_sub(1, Ordering::Relaxed);
        assert_eq!(rt.queue_depth(), 0);
    }
}
