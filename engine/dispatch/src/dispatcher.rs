//! Dispatcher — routes node execution to the appropriate runtime.
//!
//! The dispatcher:
//! 1. Resolves which runtime a node requires (based on operator type)
//! 2. Submits to that runtime with backpressure handling
//! 3. Tracks circuit breaker state per runtime
//! 4. Exposes aggregate health for the scaling controller

use crate::runtime::{
    NodeResult, NodeTask, Runtime, RuntimeError, RuntimeHealth, RuntimeKind, SubmitResponse,
};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

// ---------------------------------------------------------------------------
// Circuit breaker
// ---------------------------------------------------------------------------

/// Per-runtime circuit breaker state.
#[derive(Debug, Clone)]
pub struct CircuitBreaker {
    pub consecutive_failures: u32,
    pub state: CircuitState,
    pub last_failure_time: Option<std::time::Instant>,
    /// Max consecutive failures before opening the circuit.
    pub failure_threshold: u32,
    /// How long to wait before attempting a probe after opening.
    pub recovery_timeout: std::time::Duration,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CircuitState {
    /// Normal operation — all submissions routed to runtime.
    Closed,
    /// Circuit open — submissions rejected, waiting for recovery probe.
    Open,
    /// Probing — one submission allowed through to test recovery.
    HalfOpen,
}

impl CircuitBreaker {
    pub fn new(failure_threshold: u32, recovery_timeout: std::time::Duration) -> Self {
        CircuitBreaker {
            consecutive_failures: 0,
            state: CircuitState::Closed,
            last_failure_time: None,
            failure_threshold,
            recovery_timeout,
        }
    }

    /// Record a successful execution — resets the breaker.
    pub fn record_success(&mut self) {
        self.consecutive_failures = 0;
        self.state = CircuitState::Closed;
        self.last_failure_time = None;
    }

    /// Record a failure — may trip the breaker.
    pub fn record_failure(&mut self) {
        self.consecutive_failures += 1;
        self.last_failure_time = Some(std::time::Instant::now());
        if self.consecutive_failures >= self.failure_threshold {
            self.state = CircuitState::Open;
        }
    }

    /// Check if the breaker allows a submission.
    pub fn allows_request(&mut self) -> bool {
        match self.state {
            CircuitState::Closed => true,
            CircuitState::Open => {
                // Check if recovery timeout has elapsed.
                if let Some(last) = self.last_failure_time {
                    if last.elapsed() >= self.recovery_timeout {
                        self.state = CircuitState::HalfOpen;
                        true // allow one probe
                    } else {
                        false
                    }
                } else {
                    false
                }
            }
            CircuitState::HalfOpen => false, // only one probe at a time
        }
    }
}

// ---------------------------------------------------------------------------
// Dispatcher
// ---------------------------------------------------------------------------

/// Routes node execution to the appropriate runtime.
pub struct Dispatcher {
    /// Registered runtimes by kind.
    runtimes: HashMap<RuntimeKind, Arc<dyn Runtime>>,
    /// Circuit breakers per runtime.
    circuit_breakers: RwLock<HashMap<RuntimeKind, CircuitBreaker>>,
    /// Engine-level admission control.
    max_inflight_pipelines: u32,
    current_inflight_pipelines: RwLock<u32>,
}

impl Dispatcher {
    /// Create a new dispatcher with no runtimes registered.
    pub fn new(max_inflight_pipelines: u32) -> Self {
        Dispatcher {
            runtimes: HashMap::new(),
            circuit_breakers: RwLock::new(HashMap::new()),
            max_inflight_pipelines,
            current_inflight_pipelines: RwLock::new(0),
        }
    }

    /// Register a runtime.
    pub async fn register_runtime(&mut self, runtime: Arc<dyn Runtime>) {
        let kind = runtime.kind();
        self.runtimes.insert(kind, runtime);
        self.circuit_breakers.write().await.insert(
            kind,
            CircuitBreaker::new(5, std::time::Duration::from_secs(30)),
        );
    }

    /// Check if the engine is accepting new pipeline submissions.
    pub async fn accepting_submissions(&self) -> bool {
        let current = *self.current_inflight_pipelines.read().await;
        current < self.max_inflight_pipelines
    }

    /// Increment inflight pipeline count (called when a pipeline starts).
    pub async fn pipeline_started(&self) {
        let mut count = self.current_inflight_pipelines.write().await;
        *count += 1;
    }

    /// Decrement inflight pipeline count (called when a pipeline finishes).
    pub async fn pipeline_finished(&self) {
        let mut count = self.current_inflight_pipelines.write().await;
        *count = count.saturating_sub(1);
    }

    /// Submit a node to the appropriate runtime.
    ///
    /// The dispatcher:
    /// 1. Looks up the runtime by kind
    /// 2. Checks the circuit breaker
    /// 3. Submits to the runtime
    /// 4. Updates circuit breaker on result
    pub async fn submit(
        &self,
        kind: RuntimeKind,
        task: NodeTask,
    ) -> Result<SubmitResponse, RuntimeError> {
        // Check if runtime exists.
        let runtime = self
            .runtimes
            .get(&kind)
            .ok_or_else(|| RuntimeError::Internal(format!("no runtime registered for {kind}")))?;

        // Check circuit breaker.
        {
            let mut breakers = self.circuit_breakers.write().await;
            if let Some(breaker) = breakers.get_mut(&kind) {
                if !breaker.allows_request() {
                    return Err(RuntimeError::Backpressure(format!(
                        "circuit breaker open for {kind}"
                    )));
                }
            }
        }

        // Check runtime health.
        let health = runtime.health();
        if !health.healthy {
            return Err(RuntimeError::Unhealthy(kind));
        }

        // Submit to runtime.
        let result = runtime.submit(task).await;

        // Update circuit breaker based on result.
        {
            let mut breakers = self.circuit_breakers.write().await;
            if let Some(breaker) = breakers.get_mut(&kind) {
                match &result {
                    Ok(SubmitResponse::Accepted { .. }) => breaker.record_success(),
                    Ok(SubmitResponse::Throttle { .. }) => {} // don't trip breaker on throttle
                    Ok(SubmitResponse::Rejected { .. }) => breaker.record_failure(),
                    Err(_) => breaker.record_failure(),
                }
            }
        }

        result
    }

    /// Wait for a task to complete on the specified runtime.
    pub async fn wait(
        &self,
        kind: RuntimeKind,
        task_id: &str,
    ) -> Result<NodeResult, RuntimeError> {
        let runtime = self
            .runtimes
            .get(&kind)
            .ok_or_else(|| RuntimeError::Internal(format!("no runtime registered for {kind}")))?;
        runtime.wait(task_id).await
    }

    /// Cancel a task on the specified runtime.
    pub async fn cancel(&self, kind: RuntimeKind, task_id: &str) -> Result<(), RuntimeError> {
        let runtime = self
            .runtimes
            .get(&kind)
            .ok_or_else(|| RuntimeError::Internal(format!("no runtime registered for {kind}")))?;
        runtime.cancel(task_id).await
    }

    /// Get health reports for all registered runtimes.
    pub fn all_runtime_health(&self) -> Vec<RuntimeHealth> {
        self.runtimes.values().map(|r| r.health()).collect()
    }

    /// Get circuit breaker states.
    pub async fn circuit_breaker_states(&self) -> HashMap<RuntimeKind, CircuitState> {
        self.circuit_breakers
            .read()
            .await
            .iter()
            .map(|(&k, v)| (k, v.state))
            .collect()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_circuit_breaker_closed_allows_requests() {
        let mut cb = CircuitBreaker::new(3, std::time::Duration::from_secs(30));
        assert!(cb.allows_request());
        assert_eq!(cb.state, CircuitState::Closed);
    }

    #[test]
    fn test_circuit_breaker_opens_after_threshold() {
        let mut cb = CircuitBreaker::new(3, std::time::Duration::from_secs(30));
        cb.record_failure();
        cb.record_failure();
        assert!(cb.allows_request()); // not yet at threshold
        cb.record_failure();
        assert_eq!(cb.state, CircuitState::Open);
        assert!(!cb.allows_request()); // circuit open
    }

    #[test]
    fn test_circuit_breaker_resets_on_success() {
        let mut cb = CircuitBreaker::new(3, std::time::Duration::from_secs(30));
        cb.record_failure();
        cb.record_failure();
        cb.record_success();
        assert_eq!(cb.consecutive_failures, 0);
        assert_eq!(cb.state, CircuitState::Closed);
    }

    #[test]
    fn test_circuit_breaker_half_open_after_timeout() {
        let mut cb = CircuitBreaker::new(1, std::time::Duration::from_millis(1));
        cb.record_failure(); // trips to Open
        assert_eq!(cb.state, CircuitState::Open);

        // Wait for recovery timeout.
        std::thread::sleep(std::time::Duration::from_millis(5));
        assert!(cb.allows_request()); // transitions to HalfOpen
        assert_eq!(cb.state, CircuitState::HalfOpen);

        // Second request blocked while probing.
        assert!(!cb.allows_request());
    }

    #[tokio::test]
    async fn test_dispatcher_rejects_when_no_runtime() {
        let dispatcher = Dispatcher::new(10);
        let task = NodeTask {
            task_id: "t1".into(),
            run_id: "r1".into(),
            node_id: "n1".into(),
            payload: vec![],
            inputs: vec![],
            context: HashMap::new(),
            timeout_seconds: 0.0,
        };
        let result = dispatcher.submit(RuntimeKind::Python, task).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_dispatcher_admission_control() {
        let dispatcher = Dispatcher::new(2);
        assert!(dispatcher.accepting_submissions().await);

        dispatcher.pipeline_started().await;
        dispatcher.pipeline_started().await;
        assert!(!dispatcher.accepting_submissions().await);

        dispatcher.pipeline_finished().await;
        assert!(dispatcher.accepting_submissions().await);
    }
}
