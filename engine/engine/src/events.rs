//! Event sourcing — append-only execution event log.
//!
//! All state transitions produce events. Events are the source of truth:
//! - In-memory log for the current process
//! - Redis stream for durability and frontend communication
//! - Crash recovery: replay event log to reconstruct state
//!
//! Event categories (from proto/events.proto):
//! - ENGINE: execution lifecycle (node started, completed, failed)
//! - DOMAIN: risk analysis, recommendations (future)
//! - SERVICE: persistence, notifications (future)
//! - CANVAS: user interactions (frontend-originated)

use crate::state::{NodeStatus, PipelineRunStatus};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::sync::Arc;
use tokio::sync::{broadcast, RwLock};

// ---------------------------------------------------------------------------
// Event types
// ---------------------------------------------------------------------------

/// Category of an execution event.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum EventCategory {
    Engine,
    Domain,
    Service,
    Canvas,
}

/// An execution event in the append-only log.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionEvent {
    /// Monotonically increasing sequence number within this run.
    pub seq: u64,
    /// Epoch seconds.
    pub timestamp: f64,
    /// Event category.
    pub category: EventCategory,
    /// Event payload.
    pub payload: EventPayload,
    /// Pipeline run ID.
    pub run_id: String,
    /// User ID.
    pub uid: String,
    /// Session ID.
    pub session_id: String,
}

/// Strongly-typed event payloads.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "event")]
pub enum EventPayload {
    // -- Pipeline-level events ---
    #[serde(rename = "pipeline/run/started")]
    PipelineRunStarted {
        node_count: usize,
        node_ids: Vec<String>,
    },

    #[serde(rename = "pipeline/run/completed")]
    PipelineRunCompleted {
        status: PipelineRunStatus,
        duration: Option<f64>,
        node_count: usize,
    },

    #[serde(rename = "pipeline/run/failed")]
    PipelineRunFailed {
        status: PipelineRunStatus,
        error: Option<String>,
        duration: Option<f64>,
    },

    #[serde(rename = "pipeline/run/cancelled")]
    PipelineRunCancelled { duration: Option<f64> },

    // -- Node-level events ---
    #[serde(rename = "pipeline/node/started")]
    NodeStarted {
        node_id: String,
        status: NodeStatus,
    },

    #[serde(rename = "pipeline/node/completed")]
    NodeCompleted {
        node_id: String,
        status: NodeStatus,
        result_ref: Option<String>,
        duration: Option<f64>,
    },

    #[serde(rename = "pipeline/node/failed")]
    NodeFailed {
        node_id: String,
        status: NodeStatus,
        error: String,
        duration: Option<f64>,
    },

    #[serde(rename = "pipeline/node/skipped")]
    NodeSkipped {
        node_id: String,
        status: NodeStatus,
        reason: Option<String>,
    },

    #[serde(rename = "pipeline/node/cancelled")]
    NodeCancelled {
        node_id: String,
        status: NodeStatus,
    },

    // -- Infrastructure events ---
    #[serde(rename = "engine/stale_run_detected")]
    StaleRunDetected { run_id: String },

    #[serde(rename = "engine/graph/built")]
    GraphBuilt {
        node_count: usize,
        edge_count: usize,
        sink_nodes: Vec<String>,
    },

    #[serde(rename = "engine/graph/validation_failed")]
    GraphValidationFailed { errors: Vec<String> },
}

// ---------------------------------------------------------------------------
// Event log (in-memory + broadcast)
// ---------------------------------------------------------------------------

/// In-memory append-only event log with broadcast notifications.
///
/// Used for:
/// 1. Event sourcing: replay events to reconstruct state after crash
/// 2. Real-time streaming: subscribers get events as they happen
/// 3. Audit trail: full history of execution events
pub struct EventLog {
    events: RwLock<VecDeque<ExecutionEvent>>,
    next_seq: RwLock<u64>,
    /// Broadcast channel for real-time subscribers.
    tx: broadcast::Sender<ExecutionEvent>,
    /// Max events to retain in memory (ring buffer).
    max_events: usize,
}

impl EventLog {
    /// Create a new event log.
    pub fn new(max_events: usize) -> Arc<Self> {
        let (tx, _) = broadcast::channel(256);
        Arc::new(EventLog {
            events: RwLock::new(VecDeque::with_capacity(max_events)),
            next_seq: RwLock::new(1),
            tx,
            max_events,
        })
    }

    /// Append an event to the log.
    pub async fn append(&self, event: ExecutionEvent) {
        let mut events = self.events.write().await;
        if events.len() >= self.max_events {
            events.pop_front();
        }
        // Broadcast (ignore error if no receivers).
        let _ = self.tx.send(event.clone());
        events.push_back(event);
    }

    /// Create and append a new event, auto-assigning sequence number and timestamp.
    pub async fn emit(
        &self,
        run_id: &str,
        uid: &str,
        session_id: &str,
        category: EventCategory,
        payload: EventPayload,
    ) -> ExecutionEvent {
        let seq = {
            let mut seq = self.next_seq.write().await;
            let current = *seq;
            *seq += 1;
            current
        };

        let event = ExecutionEvent {
            seq,
            timestamp: epoch_now(),
            category,
            payload,
            run_id: run_id.to_string(),
            uid: uid.to_string(),
            session_id: session_id.to_string(),
        };

        self.append(event.clone()).await;
        event
    }

    /// Subscribe to real-time events.
    pub fn subscribe(&self) -> broadcast::Receiver<ExecutionEvent> {
        self.tx.subscribe()
    }

    /// Get all events for a specific run.
    pub async fn events_for_run(&self, run_id: &str) -> Vec<ExecutionEvent> {
        let events = self.events.read().await;
        events
            .iter()
            .filter(|e| e.run_id == run_id)
            .cloned()
            .collect()
    }

    /// Get all events after a specific sequence number.
    pub async fn events_after(&self, seq: u64) -> Vec<ExecutionEvent> {
        let events = self.events.read().await;
        events.iter().filter(|e| e.seq > seq).cloned().collect()
    }

    /// Total number of events in the log.
    pub async fn len(&self) -> usize {
        self.events.read().await.len()
    }

    /// Whether the log is empty.
    pub async fn is_empty(&self) -> bool {
        self.events.read().await.is_empty()
    }

    /// Clear all events (used in tests).
    pub async fn clear(&self) {
        self.events.write().await.clear();
        *self.next_seq.write().await = 1;
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn epoch_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_event_log_emit() {
        let log = EventLog::new(100);
        let event = log
            .emit(
                "r1",
                "u1",
                "s1",
                EventCategory::Engine,
                EventPayload::PipelineRunStarted {
                    node_count: 3,
                    node_ids: vec!["n1".into(), "n2".into(), "n3".into()],
                },
            )
            .await;

        assert_eq!(event.seq, 1);
        assert_eq!(event.run_id, "r1");
        assert_eq!(log.len().await, 1);
    }

    #[tokio::test]
    async fn test_event_log_sequence_numbers() {
        let log = EventLog::new(100);
        for _ in 0..5 {
            log.emit(
                "r1",
                "u1",
                "s1",
                EventCategory::Engine,
                EventPayload::NodeStarted {
                    node_id: "n1".into(),
                    status: NodeStatus::Running,
                },
            )
            .await;
        }

        let events = log.events_for_run("r1").await;
        assert_eq!(events.len(), 5);
        for (i, e) in events.iter().enumerate() {
            assert_eq!(e.seq, (i + 1) as u64);
        }
    }

    #[tokio::test]
    async fn test_event_log_ring_buffer() {
        let log = EventLog::new(3);
        for i in 0..5 {
            log.emit(
                "r1",
                "u1",
                "s1",
                EventCategory::Engine,
                EventPayload::NodeStarted {
                    node_id: format!("n{i}"),
                    status: NodeStatus::Running,
                },
            )
            .await;
        }

        assert_eq!(log.len().await, 3);
        // Oldest events should be evicted.
        let events = log.events_for_run("r1").await;
        assert_eq!(events[0].seq, 3); // oldest remaining
    }

    #[tokio::test]
    async fn test_events_for_run_filter() {
        let log = EventLog::new(100);
        log.emit(
            "r1",
            "u1",
            "s1",
            EventCategory::Engine,
            EventPayload::NodeStarted {
                node_id: "n1".into(),
                status: NodeStatus::Running,
            },
        )
        .await;
        log.emit(
            "r2",
            "u1",
            "s1",
            EventCategory::Engine,
            EventPayload::NodeStarted {
                node_id: "n2".into(),
                status: NodeStatus::Running,
            },
        )
        .await;

        let r1_events = log.events_for_run("r1").await;
        assert_eq!(r1_events.len(), 1);
        let r2_events = log.events_for_run("r2").await;
        assert_eq!(r2_events.len(), 1);
    }

    #[tokio::test]
    async fn test_events_after_seq() {
        let log = EventLog::new(100);
        for _ in 0..5 {
            log.emit(
                "r1",
                "u1",
                "s1",
                EventCategory::Engine,
                EventPayload::NodeStarted {
                    node_id: "n1".into(),
                    status: NodeStatus::Running,
                },
            )
            .await;
        }

        let after_3 = log.events_after(3).await;
        assert_eq!(after_3.len(), 2); // seq 4 and 5
    }

    #[tokio::test]
    async fn test_broadcast_subscriber() {
        let log = EventLog::new(100);
        let mut rx = log.subscribe();

        log.emit(
            "r1",
            "u1",
            "s1",
            EventCategory::Engine,
            EventPayload::PipelineRunStarted {
                node_count: 1,
                node_ids: vec!["n1".into()],
            },
        )
        .await;

        let received = rx.try_recv();
        assert!(received.is_ok());
        assert_eq!(received.unwrap().seq, 1);
    }

    #[tokio::test]
    async fn test_event_serialization() {
        let event = ExecutionEvent {
            seq: 1,
            timestamp: 1000.0,
            category: EventCategory::Engine,
            payload: EventPayload::NodeCompleted {
                node_id: "n1".into(),
                status: NodeStatus::Success,
                result_ref: Some("redis:result:r1:n1".into()),
                duration: Some(1.5),
            },
            run_id: "r1".into(),
            uid: "u1".into(),
            session_id: "s1".into(),
        };

        let json = serde_json::to_string(&event).unwrap();
        let parsed: ExecutionEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.seq, 1);
        assert_eq!(parsed.run_id, "r1");
    }

    #[tokio::test]
    async fn test_clear_log() {
        let log = EventLog::new(100);
        log.emit(
            "r1",
            "u1",
            "s1",
            EventCategory::Engine,
            EventPayload::NodeStarted {
                node_id: "n1".into(),
                status: NodeStatus::Running,
            },
        )
        .await;

        assert!(!log.is_empty().await);
        log.clear().await;
        assert!(log.is_empty().await);
    }

    #[test]
    fn test_event_payload_node_events() {
        // Verify all node event variants serialize correctly.
        let payloads = vec![
            EventPayload::NodeStarted {
                node_id: "n1".into(),
                status: NodeStatus::Running,
            },
            EventPayload::NodeCompleted {
                node_id: "n1".into(),
                status: NodeStatus::Success,
                result_ref: None,
                duration: Some(2.0),
            },
            EventPayload::NodeFailed {
                node_id: "n1".into(),
                status: NodeStatus::Failed,
                error: "oops".into(),
                duration: Some(0.1),
            },
            EventPayload::NodeSkipped {
                node_id: "n1".into(),
                status: NodeStatus::Skipped,
                reason: Some("upstream failed".into()),
            },
            EventPayload::NodeCancelled {
                node_id: "n1".into(),
                status: NodeStatus::Cancelled,
            },
        ];

        for p in payloads {
            let json = serde_json::to_string(&p).unwrap();
            assert!(json.contains("node_id"));
        }
    }

    #[test]
    fn test_event_payload_pipeline_events() {
        let payloads = vec![
            EventPayload::PipelineRunStarted {
                node_count: 3,
                node_ids: vec!["n1".into()],
            },
            EventPayload::PipelineRunCompleted {
                status: PipelineRunStatus::Success,
                duration: Some(10.0),
                node_count: 3,
            },
            EventPayload::PipelineRunFailed {
                status: PipelineRunStatus::Failed,
                error: Some("crash".into()),
                duration: Some(5.0),
            },
            EventPayload::PipelineRunCancelled {
                duration: Some(2.0),
            },
        ];

        for p in payloads {
            let json = serde_json::to_string(&p).unwrap();
            assert!(!json.is_empty());
        }
    }
}
