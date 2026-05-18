//! Discrete Event domain scheduler — v1 sketch.
//!
//! DE actors fire in response to timestamped events pulled from a
//! priority queue, not on token availability. Today's DE surface in
//! Dorian is small:
//!
//! * `CancelPipeline` — user clicks cancel; downstream cleanup fires.
//! * `MitigationTriggered` — AI Debugger rewrite applies; downstream
//!   SDF subgraph reschedules.
//! * `AiDebuggerRewrite` — the rewrite itself as an actor.
//!
//! Each of these is already served by the Go event-bus handlers
//! (Tier-0 infra from the previous session). This crate gives the
//! Rust engine a consistent view of them: the DE scheduler dequeues
//! events, matches them to DE actors, fires (with cache consultation
//! where the determinism contract permits), and produces cross-domain
//! handoffs that the `compose/` crate turns into SDF triggers.
//!
//! v1 ships:
//!   * `Event` — timestamped payload + target-actor node ID.
//!   * `EventQueue` — min-heap by timestamp, FIFO tie-break.
//!   * `DeScheduler` — dequeue-fire-record loop.
//!   * `CrossDomainHandoff` — emitted when a DE firing should trigger
//!     an SDF subgraph; consumed by the compose layer.

use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::sync::Arc;

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

use cache::{CacheEntry, CacheOutcome, CacheStore};
use graph::dem::{DemAnnotations, DomainKind};
use graph::model::ProcessGraph;

// ---------------------------------------------------------------------------
// Event
// ---------------------------------------------------------------------------

/// A timestamped trigger for a DE actor. The `timestamp` is a
/// logical tick (u64 — monotonic within one scheduler run). In
/// production this maps to Redis-stream entry IDs; tests use synthetic
/// sequences.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    pub timestamp: u64,
    /// Global sequence number, used as a tiebreak so the queue order
    /// is fully deterministic.
    pub seq: u64,
    /// Target actor node id — the scheduler matches this to a DE actor
    /// in the annotated graph.
    pub target: String,
    /// Event kind string (mirrors the Go event-bus naming).
    pub kind: String,
    /// Arbitrary payload passed to the actor's firing.
    pub payload: serde_json::Value,
}

impl Event {
    pub fn new(timestamp: u64, seq: u64, target: impl Into<String>, kind: impl Into<String>, payload: serde_json::Value) -> Self {
        Event {
            timestamp,
            seq,
            target: target.into(),
            kind: kind.into(),
            payload,
        }
    }
}

// Reverse ordering so BinaryHeap (max-heap) becomes a min-heap by timestamp.
impl PartialEq for Event {
    fn eq(&self, other: &Self) -> bool {
        self.timestamp == other.timestamp && self.seq == other.seq
    }
}
impl Eq for Event {}
impl PartialOrd for Event {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Event {
    fn cmp(&self, other: &Self) -> Ordering {
        // Reverse timestamp, then reverse seq (min-heap on (ts, seq)).
        other
            .timestamp
            .cmp(&self.timestamp)
            .then_with(|| other.seq.cmp(&self.seq))
    }
}

// ---------------------------------------------------------------------------
// EventQueue
// ---------------------------------------------------------------------------

/// Priority queue ordered by (timestamp, seq).
#[derive(Debug, Default)]
pub struct EventQueue {
    heap: BinaryHeap<Event>,
    next_seq: u64,
}

impl EventQueue {
    pub fn new() -> Self {
        Self::default()
    }

    /// Push an event. If `seq` is `u64::MAX`, the queue assigns the
    /// next-available sequence automatically — keeps determinism when
    /// callers push without explicit ordering.
    pub fn push(&mut self, mut event: Event) {
        if event.seq == u64::MAX {
            event.seq = self.next_seq;
        }
        if event.seq >= self.next_seq {
            self.next_seq = event.seq + 1;
        }
        self.heap.push(event);
    }

    pub fn pop(&mut self) -> Option<Event> {
        self.heap.pop()
    }

    pub fn peek(&self) -> Option<&Event> {
        self.heap.peek()
    }

    pub fn len(&self) -> usize {
        self.heap.len()
    }

    pub fn is_empty(&self) -> bool {
        self.heap.is_empty()
    }
}

// ---------------------------------------------------------------------------
// Firer + cross-domain handoff
// ---------------------------------------------------------------------------

/// A DE firing may produce a cross-domain handoff — e.g. the AI
/// Debugger's `MitigationTriggered` event should cause the SDF
/// subgraph to re-fire from a specific node. The compose layer
/// consumes these handoffs.
#[derive(Debug, Clone)]
pub enum CrossDomainHandoff {
    /// Trigger an SDF subgraph rooted at `root_node`. Optional
    /// `reason` is echoed back in observability events.
    TriggerSdf { root_node: String, reason: String },
    /// Cancel any in-flight SDF execution (cooperative).
    CancelSdf { reason: String },
    /// No cross-domain effect — the firing was contained within DE.
    Contained,
}

/// Fires a single DE actor. Typically dispatches to the Go event-bus
/// handler; the trait lets tests inject synthetic firings.
pub trait DeFirer {
    fn fire(
        &self,
        run_id: &str,
        event: &Event,
        graph: &ProcessGraph,
    ) -> Result<DeFireResult, DeFireError>;
}

#[derive(Debug, Clone)]
pub struct DeFireResult {
    /// Events produced by this firing — may be fed back into the
    /// queue (causal chain).
    pub emitted: Vec<Event>,
    /// Cross-domain effect this firing intends.
    pub handoff: CrossDomainHandoff,
    /// Wall-clock cost.
    pub compute_secs: f64,
}

#[derive(Debug, thiserror::Error)]
pub enum DeFireError {
    #[error("DE actor {0} failed: {1}")]
    ActorFailed(String, String),
}

// ---------------------------------------------------------------------------
// Scheduler
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
pub enum DeSchedulerError {
    #[error("fire error: {0}")]
    Fire(#[from] DeFireError),
    #[error("queue drained under termination-predicate limit")]
    DrainLimit,
}

/// Observability events emitted by the DE scheduler. `compose/`
/// forwards these to the common event bus.
#[derive(Debug, Clone)]
pub enum DeEvent {
    Fired {
        run_id: String,
        node_id: String,
        event_kind: String,
        timestamp: u64,
        compute_secs: f64,
    },
    Skipped {
        run_id: String,
        node_id: String,
        reason: String,
    },
    Handoff {
        run_id: String,
        node_id: String,
        handoff: HandoffKind,
    },
}

/// Compact alias for observability — the full `CrossDomainHandoff`
/// carries strings we don't need in the trace.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HandoffKind {
    TriggerSdf,
    CancelSdf,
    Contained,
}

impl From<&CrossDomainHandoff> for HandoffKind {
    fn from(h: &CrossDomainHandoff) -> Self {
        match h {
            CrossDomainHandoff::TriggerSdf { .. } => HandoffKind::TriggerSdf,
            CrossDomainHandoff::CancelSdf { .. } => HandoffKind::CancelSdf,
            CrossDomainHandoff::Contained => HandoffKind::Contained,
        }
    }
}

#[derive(Debug, Default)]
pub struct DeReport {
    pub events: Vec<DeEvent>,
    pub handoffs: Vec<CrossDomainHandoff>,
    pub fired: usize,
    pub skipped: usize,
}

/// DE scheduler. Unlike SDF it does NOT consult the cache by default
/// — DE actors are almost always non-deterministic (event-driven,
/// time-sensitive). The cache reference is retained as a handle so
/// individual actors can opt in via the KB (`is_deterministic`).
pub struct DeScheduler<'a> {
    pub cache: &'a dyn CacheStore,
    /// Soft limit on events processed per run — prevents runaway
    /// fixed-points when actors emit causal-chain events.
    pub max_fires: usize,
}

impl<'a> DeScheduler<'a> {
    pub fn new(cache: &'a dyn CacheStore) -> Self {
        DeScheduler {
            cache,
            max_fires: 10_000,
        }
    }

    /// Drain the queue. Returns when the queue is empty or
    /// `max_fires` is hit.
    pub fn run(
        &self,
        run_id: &str,
        graph: &ProcessGraph,
        annotations: &DemAnnotations,
        queue: &mut EventQueue,
        firer: &dyn DeFirer,
    ) -> Result<DeReport, DeSchedulerError> {
        let mut report = DeReport::default();
        let mut fires = 0usize;

        while let Some(event) = queue.pop() {
            if fires >= self.max_fires {
                return Err(DeSchedulerError::DrainLimit);
            }
            let ann = annotations.actor(&event.target);
            if ann.map(|a| a.domain) != Some(DomainKind::De) {
                report.skipped += 1;
                report.events.push(DeEvent::Skipped {
                    run_id: run_id.to_string(),
                    node_id: event.target.clone(),
                    reason: "target is not a DE actor".to_string(),
                });
                continue;
            }
            let result = firer.fire(run_id, &event, graph)?;
            fires += 1;
            report.fired += 1;
            report.events.push(DeEvent::Fired {
                run_id: run_id.to_string(),
                node_id: event.target.clone(),
                event_kind: event.kind.clone(),
                timestamp: event.timestamp,
                compute_secs: result.compute_secs,
            });
            report.events.push(DeEvent::Handoff {
                run_id: run_id.to_string(),
                node_id: event.target.clone(),
                handoff: HandoffKind::from(&result.handoff),
            });
            report.handoffs.push(result.handoff);
            for emitted in result.emitted {
                let mut e = emitted;
                if e.seq == 0 {
                    e.seq = u64::MAX;
                }
                queue.push(e);
            }
        }

        // Retain the cache reference to keep the field live for future
        // cacheable-DE-actor opt-ins. Today no DE actor is cacheable,
        // so we don't touch the store, but we touch the handle to
        // silence dead-code lints and document intent.
        let _ = self.cache.len();

        Ok(report)
    }
}

// ---------------------------------------------------------------------------
// Convenience: seed events for common DE kinds
// ---------------------------------------------------------------------------

/// Mapping of event-kind string → target node id. Populated by the
/// parser when scanning the graph for known DE operators. Used by
/// bus-bridge code that receives a `{Kind}Event` from Redis and
/// needs to know which actor to dispatch to.
pub type DeTargetMap = FxHashMap<String, String>;

pub fn build_de_target_map(graph: &ProcessGraph, annotations: &DemAnnotations) -> DeTargetMap {
    let mut map = DeTargetMap::default();
    for (id, node) in &graph.nodes {
        let ann = match annotations.actor(id) {
            Some(a) => a,
            None => continue,
        };
        if ann.domain != DomainKind::De {
            continue;
        }
        let kind = match node {
            graph::Node::Operator(op) => op.name.clone(),
            _ => continue,
        };
        map.insert(kind, id.clone());
    }
    map
}

// Retain the `Arc<CacheEntry>` + CacheOutcome types in scope so
// downstream modules can use `de::CacheOutcome` where convenient.
pub fn _ensure_cache_types_in_scope() -> Option<Arc<CacheEntry>> {
    let _: Option<CacheOutcome> = None;
    None
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use cache::MemoryStore;
    use graph::dem::{ActorAnnotations, DomainKind};
    use graph::model::{Node, Operator, ProcessGraph};

    struct CancelFirer;

    impl DeFirer for CancelFirer {
        fn fire(
            &self,
            _run_id: &str,
            event: &Event,
            _graph: &ProcessGraph,
        ) -> Result<DeFireResult, DeFireError> {
            // CancelPipeline → CancelSdf handoff; no emitted events.
            let handoff = if event.kind == "CancelPipeline" {
                CrossDomainHandoff::CancelSdf {
                    reason: "user cancel".to_string(),
                }
            } else {
                CrossDomainHandoff::Contained
            };
            Ok(DeFireResult {
                emitted: vec![],
                handoff,
                compute_secs: 0.0,
            })
        }
    }

    fn de_graph() -> (ProcessGraph, DemAnnotations) {
        let mut g = ProcessGraph::new();
        g.add_node(
            "cancel".into(),
            Node::Operator(Operator {
                name: "dorian.cancel".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        let mut dem = DemAnnotations::new();
        let mut ann = ActorAnnotations::de_default();
        ann.domain = DomainKind::De;
        dem.actors.insert("cancel".into(), ann);
        (g, dem)
    }

    #[test]
    fn queue_orders_by_timestamp() {
        let mut q = EventQueue::new();
        q.push(Event::new(
            10,
            u64::MAX,
            "x",
            "A",
            serde_json::Value::Null,
        ));
        q.push(Event::new(
            5,
            u64::MAX,
            "x",
            "B",
            serde_json::Value::Null,
        ));
        q.push(Event::new(
            7,
            u64::MAX,
            "x",
            "C",
            serde_json::Value::Null,
        ));
        assert_eq!(q.pop().unwrap().timestamp, 5);
        assert_eq!(q.pop().unwrap().timestamp, 7);
        assert_eq!(q.pop().unwrap().timestamp, 10);
    }

    #[test]
    fn queue_breaks_ties_by_seq() {
        let mut q = EventQueue::new();
        q.push(Event::new(1, 2, "x", "B", serde_json::Value::Null));
        q.push(Event::new(1, 1, "x", "A", serde_json::Value::Null));
        assert_eq!(q.pop().unwrap().seq, 1);
        assert_eq!(q.pop().unwrap().seq, 2);
    }

    #[test]
    fn cancel_event_fires_and_emits_handoff() {
        let (g, dem) = de_graph();
        let cache = MemoryStore::new();
        let sched = DeScheduler::new(&cache);
        let mut q = EventQueue::new();
        q.push(Event::new(
            0,
            u64::MAX,
            "cancel",
            "CancelPipeline",
            serde_json::json!({"reason": "user"}),
        ));
        let report = sched.run("r1", &g, &dem, &mut q, &CancelFirer).unwrap();
        assert_eq!(report.fired, 1);
        assert_eq!(report.handoffs.len(), 1);
        assert!(matches!(
            report.handoffs[0],
            CrossDomainHandoff::CancelSdf { .. }
        ));
    }

    #[test]
    fn event_to_non_de_actor_is_skipped() {
        // A stray event targeting an SDF actor is dropped with a
        // Skipped observation, not fired.
        let mut g = ProcessGraph::new();
        g.add_node(
            "s".into(),
            Node::Operator(Operator {
                name: "sklearn.preprocessing.StandardScaler".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        let mut dem = DemAnnotations::new();
        dem.actors
            .insert("s".into(), ActorAnnotations::sdf_default());
        let cache = MemoryStore::new();
        let sched = DeScheduler::new(&cache);
        let mut q = EventQueue::new();
        q.push(Event::new(
            0,
            u64::MAX,
            "s",
            "StrayEvent",
            serde_json::Value::Null,
        ));
        let report = sched.run("r1", &g, &dem, &mut q, &CancelFirer).unwrap();
        assert_eq!(report.fired, 0);
        assert_eq!(report.skipped, 1);
    }

    #[test]
    fn causal_emitted_events_are_drained() {
        struct ChainFirer {
            counter: std::cell::Cell<u64>,
        }
        impl DeFirer for ChainFirer {
            fn fire(
                &self,
                _run_id: &str,
                _event: &Event,
                _graph: &ProcessGraph,
            ) -> Result<DeFireResult, DeFireError> {
                let c = self.counter.get();
                self.counter.set(c + 1);
                let emitted = if c < 2 {
                    vec![Event::new(
                        c + 1,
                        u64::MAX,
                        "cancel",
                        "CancelPipeline",
                        serde_json::Value::Null,
                    )]
                } else {
                    vec![]
                };
                Ok(DeFireResult {
                    emitted,
                    handoff: CrossDomainHandoff::Contained,
                    compute_secs: 0.0,
                })
            }
        }
        let (g, dem) = de_graph();
        let cache = MemoryStore::new();
        let sched = DeScheduler::new(&cache);
        let mut q = EventQueue::new();
        q.push(Event::new(
            0,
            u64::MAX,
            "cancel",
            "CancelPipeline",
            serde_json::Value::Null,
        ));
        let firer = ChainFirer {
            counter: std::cell::Cell::new(0),
        };
        let report = sched.run("r1", &g, &dem, &mut q, &firer).unwrap();
        // Initial + 2 emitted = 3 total.
        assert_eq!(report.fired, 3);
    }

    #[test]
    fn drain_limit_errors_on_runaway() {
        struct InfiniteFirer;
        impl DeFirer for InfiniteFirer {
            fn fire(
                &self,
                _run_id: &str,
                _event: &Event,
                _graph: &ProcessGraph,
            ) -> Result<DeFireResult, DeFireError> {
                Ok(DeFireResult {
                    emitted: vec![Event::new(
                        0,
                        u64::MAX,
                        "cancel",
                        "CancelPipeline",
                        serde_json::Value::Null,
                    )],
                    handoff: CrossDomainHandoff::Contained,
                    compute_secs: 0.0,
                })
            }
        }
        let (g, dem) = de_graph();
        let cache = MemoryStore::new();
        let mut sched = DeScheduler::new(&cache);
        sched.max_fires = 5;
        let mut q = EventQueue::new();
        q.push(Event::new(
            0,
            u64::MAX,
            "cancel",
            "CancelPipeline",
            serde_json::Value::Null,
        ));
        let err = sched.run("r1", &g, &dem, &mut q, &InfiniteFirer).unwrap_err();
        assert!(matches!(err, DeSchedulerError::DrainLimit));
    }

    #[test]
    fn build_de_target_map_picks_de_actors_only() {
        let (mut g, mut dem) = de_graph();
        // Add a stray SDF actor that should NOT land in the map.
        g.add_node(
            "s".into(),
            Node::Operator(Operator {
                name: "sklearn.preprocessing.StandardScaler".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        dem.actors
            .insert("s".into(), ActorAnnotations::sdf_default());
        let map = build_de_target_map(&g, &dem);
        assert_eq!(map.len(), 1);
        assert_eq!(map.get("dorian.cancel"), Some(&"cancel".to_string()));
    }
}
