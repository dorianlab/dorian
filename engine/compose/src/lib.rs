//! Hierarchical DEM composition — SDF-inside-DE and DE-inside-SDF.
//!
//! The SDF and DE domains are self-contained schedulers in their
//! respective crates. Real pipelines mix both: a "run pipeline" event
//! (DE) triggers an SDF sub-pipeline to fire; mitigation-rewrite
//! events (DE) cause a fresh SDF firing mid-run; cancel events (DE)
//! halt SDF in flight.
//!
//! This crate is the glue. It does not re-implement scheduling — it
//! choreographs handoffs:
//!
//!   * DE `TriggerSdf{root_node}` handoff → run SDF from `root_node`.
//!   * DE `CancelSdf` handoff → signal the SDF scheduler to stop.
//!   * SDF `NodeComputed` event → optional DE event emission
//!     (observability, downstream triggers).
//!
//! v1 ships a `ComposedScheduler` that owns one SDF and one DE
//! scheduler and runs them in turn until both are quiescent.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use cache::CacheStore;
use de::{CrossDomainHandoff, DeEvent, DeFirer, DeReport, DeScheduler, Event, EventQueue};
use graph::dem::DemAnnotations;
use graph::model::ProcessGraph;
use sdf::{Firer, SchedulerEvent, SchedulerReport, SdfScheduler};

// ---------------------------------------------------------------------------
// Composed scheduler
// ---------------------------------------------------------------------------

/// Signals shared between the SDF and DE halves — today just
/// cancellation. Flags are atomic so the SDF scheduler can see a
/// cancel posted from a DE firing in the same thread without needing
/// a channel.
#[derive(Debug, Default)]
pub struct ComposeSignals {
    pub cancelled: AtomicBool,
}

impl ComposeSignals {
    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }

    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
    }
}

/// Composed report — aggregate of one DE drain + one SDF run, with
/// the set of handoffs that crossed the domain boundary.
#[derive(Debug, Default)]
pub struct ComposedReport {
    pub de: Option<DeReport>,
    pub sdf: Option<SchedulerReport>,
    pub handoffs: Vec<CrossDomainHandoff>,
    pub signalled_cancel: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum ComposeError {
    #[error("de scheduler error: {0}")]
    De(#[from] de::DeSchedulerError),
    #[error("sdf scheduler error: {0}")]
    Sdf(#[from] sdf::SchedulerError),
}

/// Runs a DE-triggered SDF pipeline. The canonical flow for Dorian:
///
///   1. The frontend emits `PipelineRunStarted` — a DE event.
///   2. DE scheduler drains its queue; `PipelineRunStarted` fires a
///      DE actor that emits `TriggerSdf { root_node: <entry> }`.
///   3. Compose observes the handoff, runs the SDF scheduler over
///      the SDF-tagged subgraph.
///   4. SDF-computed events are queued back as DE events if their
///      kind requests observability (future — for now SDF events
///      are reported directly).
///
/// Cancel handoffs set a shared flag; the SDF scheduler sees it on
/// the next node boundary and bails out. In v1 we check after the
/// DE drain, not inside SDF — good enough for the Cancel-before-Run
/// path and for mitigation-triggered replays.
pub struct ComposedScheduler<'a> {
    pub de: DeScheduler<'a>,
    pub sdf: SdfScheduler<'a>,
}

impl<'a> ComposedScheduler<'a> {
    pub fn new(cache: &'a dyn CacheStore) -> Self {
        ComposedScheduler {
            de: DeScheduler::new(cache),
            sdf: SdfScheduler::new(cache),
        }
    }

    /// Run one composed pass: drain DE, collect handoffs, run SDF
    /// unless a cancel handoff was seen.
    pub fn run(
        &self,
        run_id: &str,
        graph: &ProcessGraph,
        annotations: &DemAnnotations,
        queue: &mut EventQueue,
        de_firer: &dyn DeFirer,
        sdf_firer: &dyn Firer,
    ) -> Result<ComposedReport, ComposeError> {
        let mut report = ComposedReport::default();
        let signals = Arc::new(ComposeSignals::default());

        // 1. Drain DE.
        let de_report = self.de.run(run_id, graph, annotations, queue, de_firer)?;

        // 2. Collect handoffs.
        for h in &de_report.handoffs {
            match h {
                CrossDomainHandoff::CancelSdf { .. } => {
                    signals.cancel();
                    report.signalled_cancel = true;
                }
                CrossDomainHandoff::TriggerSdf { .. } | CrossDomainHandoff::Contained => {}
            }
        }
        report.handoffs = de_report.handoffs.clone();
        report.de = Some(de_report);

        // 3. Run SDF unless cancel was signalled.
        if !signals.is_cancelled() {
            let sdf_report = self.sdf.execute(run_id, graph, annotations, sdf_firer)?;
            report.sdf = Some(sdf_report);
        }

        Ok(report)
    }
}

// ---------------------------------------------------------------------------
// Observability fanout — map scheduler events to DE events
// ---------------------------------------------------------------------------

/// Translate SDF scheduler events into DE events for fanout on the
/// common observability bus. Used by the engine crate when bridging
/// the Rust engine to the Go event bus.
pub fn sdf_events_to_de(events: &[SchedulerEvent], next_ts: u64) -> Vec<Event> {
    events
        .iter()
        .enumerate()
        .map(|(i, e)| match e {
            SchedulerEvent::NodeComputed {
                run_id,
                node_id,
                cache_key,
                compute_secs,
            } => Event::new(
                next_ts + i as u64,
                u64::MAX,
                node_id,
                "NodeComputed",
                serde_json::json!({
                    "run_id": run_id,
                    "cache_key": cache_key.hex(),
                    "compute_secs": compute_secs,
                }),
            ),
            SchedulerEvent::NodeCacheHit {
                run_id,
                node_id,
                cache_key,
                hits,
            } => Event::new(
                next_ts + i as u64,
                u64::MAX,
                node_id,
                "NodeCacheHit",
                serde_json::json!({
                    "run_id": run_id,
                    "cache_key": cache_key.hex(),
                    "hits": hits,
                }),
            ),
            SchedulerEvent::NodeBypassed {
                run_id,
                node_id,
                reason,
            } => Event::new(
                next_ts + i as u64,
                u64::MAX,
                node_id,
                "NodeBypassed",
                serde_json::json!({"run_id": run_id, "reason": reason}),
            ),
            SchedulerEvent::NodeSkipped {
                run_id,
                node_id,
                reason,
            } => Event::new(
                next_ts + i as u64,
                u64::MAX,
                node_id,
                "NodeSkipped",
                serde_json::json!({"run_id": run_id, "reason": reason}),
            ),
        })
        .collect()
}

/// Same in reverse for DE → common fanout.
pub fn de_events_to_wire(events: &[DeEvent]) -> Vec<serde_json::Value> {
    events
        .iter()
        .map(|e| match e {
            DeEvent::Fired {
                run_id,
                node_id,
                event_kind,
                timestamp,
                compute_secs,
            } => serde_json::json!({
                "kind": "DeFired",
                "run_id": run_id,
                "node_id": node_id,
                "event_kind": event_kind,
                "timestamp": timestamp,
                "compute_secs": compute_secs,
            }),
            DeEvent::Skipped {
                run_id,
                node_id,
                reason,
            } => serde_json::json!({
                "kind": "DeSkipped",
                "run_id": run_id,
                "node_id": node_id,
                "reason": reason,
            }),
            DeEvent::Handoff {
                run_id,
                node_id,
                handoff,
            } => serde_json::json!({
                "kind": "DeHandoff",
                "run_id": run_id,
                "node_id": node_id,
                "handoff": format!("{:?}", handoff),
            }),
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use cache::{Artifact, MemoryStore};
    use graph::dem::{ActorAnnotations, DeterminismClass, DomainKind};
    use graph::model::{DeliveryMode, Edge, Node, Operator, Position};

    struct StubSdfFirer;
    impl Firer for StubSdfFirer {
        fn fire(
            &self,
            _run_id: &str,
            node_id: &str,
            _graph: &ProcessGraph,
            _upstream: &[Arc<cache::CacheEntry>],
        ) -> Result<sdf::FiredResult, sdf::FireError> {
            Ok(sdf::FiredResult {
                payload: serde_json::json!({"computed": node_id}),
                compute_secs: 0.01,
                artifact: Artifact::Feature,
            })
        }
    }

    struct StubDeFirer;
    impl DeFirer for StubDeFirer {
        fn fire(
            &self,
            _run_id: &str,
            event: &Event,
            _graph: &ProcessGraph,
        ) -> Result<de::DeFireResult, de::DeFireError> {
            let handoff = match event.kind.as_str() {
                "CancelPipeline" => CrossDomainHandoff::CancelSdf {
                    reason: "cancel".to_string(),
                },
                "PipelineRunStarted" => CrossDomainHandoff::TriggerSdf {
                    root_node: "a".to_string(),
                    reason: "start".to_string(),
                },
                _ => CrossDomainHandoff::Contained,
            };
            Ok(de::DeFireResult {
                emitted: vec![],
                handoff,
                compute_secs: 0.0,
            })
        }
    }

    fn mixed_graph() -> (ProcessGraph, DemAnnotations) {
        let mut g = ProcessGraph::new();
        g.add_node(
            "cancel".into(),
            Node::Operator(Operator {
                name: "dorian.cancel".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_node(
            "a".into(),
            Node::Operator(Operator {
                name: "pandas.read_csv".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_node(
            "b".into(),
            Node::Operator(Operator {
                name: "sklearn.preprocessing.StandardScaler".into(),
                language: "python".into(),
                tasks: vec![],
            }),
        );
        g.add_edge(Edge {
            source: "a".into(),
            destination: "b".into(),
            position: Position::Index(0),
            output: Position::Index(0),
            delivery_mode: DeliveryMode::Once,
        });
        let mut dem = DemAnnotations::new();
        let mut cancel_ann = ActorAnnotations::de_default();
        cancel_ann.domain = DomainKind::De;
        dem.actors.insert("cancel".into(), cancel_ann);
        for id in ["a", "b"] {
            let mut a = ActorAnnotations::sdf_default();
            a.determinism = DeterminismClass::Deterministic;
            a.operator_version = Some("1.0".into());
            dem.actors.insert(id.into(), a);
        }
        (g, dem)
    }

    #[test]
    fn composed_runs_sdf_after_de_drain() {
        let (g, dem) = mixed_graph();
        let cache = MemoryStore::new();
        let sched = ComposedScheduler::new(&cache);
        let mut queue = EventQueue::new();
        queue.push(Event::new(
            0,
            u64::MAX,
            "cancel",
            "PipelineRunStarted",
            serde_json::Value::Null,
        ));
        let report = sched
            .run(
                "r1",
                &g,
                &dem,
                &mut queue,
                &StubDeFirer,
                &StubSdfFirer,
            )
            .unwrap();
        assert!(report.de.is_some());
        assert!(report.sdf.is_some());
        assert!(!report.signalled_cancel);
        assert_eq!(report.sdf.as_ref().unwrap().misses, 2);
    }

    #[test]
    fn cancel_handoff_skips_sdf_run() {
        let (g, dem) = mixed_graph();
        let cache = MemoryStore::new();
        let sched = ComposedScheduler::new(&cache);
        let mut queue = EventQueue::new();
        queue.push(Event::new(
            0,
            u64::MAX,
            "cancel",
            "CancelPipeline",
            serde_json::Value::Null,
        ));
        let report = sched
            .run(
                "r1",
                &g,
                &dem,
                &mut queue,
                &StubDeFirer,
                &StubSdfFirer,
            )
            .unwrap();
        assert!(report.signalled_cancel);
        assert!(report.sdf.is_none());
    }

    #[test]
    fn sdf_event_translation_produces_matching_payload() {
        let events = vec![
            SchedulerEvent::NodeComputed {
                run_id: "r".into(),
                node_id: "n".into(),
                cache_key: cache::CacheKey([7u8; 32]),
                compute_secs: 0.5,
            },
            SchedulerEvent::NodeCacheHit {
                run_id: "r".into(),
                node_id: "m".into(),
                cache_key: cache::CacheKey([8u8; 32]),
                hits: 3,
            },
        ];
        let translated = sdf_events_to_de(&events, 100);
        assert_eq!(translated.len(), 2);
        assert_eq!(translated[0].kind, "NodeComputed");
        assert_eq!(translated[1].kind, "NodeCacheHit");
        assert_eq!(translated[0].timestamp, 100);
        assert_eq!(translated[1].timestamp, 101);
    }
}
