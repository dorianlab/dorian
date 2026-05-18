//! Handler registry — event-type → async handler. Mirrors
//! ``dorian/event/registry.py``: each handler is a
//! ``Fn(EventEnvelope) -> impl Future<Output = Result<()>>`` registered
//! against an event-type string. Dispatch is one ``HashMap`` lookup
//! per event.
//!
//! Handlers that the python backend used to own get added here one
//! at a time; each port is also a dropped Python registration so the
//! two services don't double-handle the same event.

use std::collections::HashMap;
use std::pin::Pin;
use std::sync::Arc;

use anyhow::Result;
use tracing::{info, warn};

use crate::event::EventEnvelope;
use crate::state::AppState;

pub type BoxFuture<'a, T> = Pin<Box<dyn std::future::Future<Output = T> + Send + 'a>>;
pub type Handler =
    Arc<dyn for<'a> Fn(&'a AppState, &'a EventEnvelope) -> BoxFuture<'a, Result<()>> + Send + Sync>;

#[derive(Clone, Default)]
pub struct Registry {
    handlers: HashMap<String, Vec<Handler>>,
}

impl Registry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a handler for an event type. Multiple handlers per
    /// type are supported — they run in registration order.
    pub fn register<F>(&mut self, event_type: &str, handler: F) -> &mut Self
    where
        F: for<'a> Fn(&'a AppState, &'a EventEnvelope) -> BoxFuture<'a, Result<()>>
            + Send
            + Sync
            + 'static,
    {
        self.handlers
            .entry(event_type.to_string())
            .or_default()
            .push(Arc::new(handler));
        self
    }

    /// Dispatch an event to every handler registered for its type.
    /// Errors from individual handlers are logged but don't block
    /// other handlers — same fault-isolation contract as the
    /// python registry.
    pub async fn dispatch(&self, state: &AppState, event: &EventEnvelope) {
        let bucket = match self.handlers.get(&event.event_type) {
            Some(b) => b,
            None => {
                // No rust handler — the python backend will pick this up
                // from the same stream until that handler is also ported.
                // Log at trace level so we don't spam during the migration.
                tracing::trace!(event_type = %event.event_type, "no rust handler");
                return;
            }
        };
        for handler in bucket {
            if let Err(e) = handler(state, event).await {
                warn!(
                    event_type = %event.event_type,
                    request_id = ?event.request_id,
                    "handler failed: {e:?}"
                );
            }
        }
    }

    pub fn registered_types(&self) -> Vec<&str> {
        let mut out: Vec<&str> = self.handlers.keys().map(|s| s.as_str()).collect();
        out.sort();
        out
    }
}

/// Build the production registry. New ports add their handler here
/// and remove the Python equivalent from
/// ``dorian/event/registry.py`` in the same commit.
pub fn build_default() -> Registry {
    let mut r = Registry::new();
    crate::handlers::heartbeat::register(&mut r);
    crate::handlers::cancel::register(&mut r);
    crate::handlers::custom_nodes::register(&mut r);
    crate::handlers::pipeline::register(&mut r);
    crate::handlers::session_meta::register(&mut r);
    crate::handlers::onboarding::register(&mut r);
    crate::handlers::interactions::register(&mut r);
    crate::handlers::listeners::register(&mut r);
    crate::handlers::dataset_live::register(&mut r);
    crate::handlers::session::register(&mut r);
    crate::handlers::datasets::register(&mut r);
    crate::handlers::data_science_task::register(&mut r);
    crate::handlers::notify::register(&mut r);
    crate::handlers::ranking_objective::register(&mut r);
    crate::handlers::auto_task::register(&mut r);
    crate::handlers::evaluation_procedure::register(&mut r);
    // session_seed registers an InitSession handler alongside the
    // onboarding tour/tooltip one (Registry stores Vec<Handler> per
    // type, so both fire). Owns the Phase 1 + Phase 2 state replay
    // that the SPA needs to render the canvas + sidebar — used to
    // be the python ``seed_session`` and was the load-bearing
    // dependency the recurring python-eventbus stalls broke.
    crate::handlers::session_seed::register(&mut r);
    // Canvas-operator scope handlers (PipelineComposed, PipelineRetrieved,
    // RecommendationPipelineSaved). Replaces the SET-mutation slice of
    // ``dorian/event/handlers/risk_debugger.py``. The python AI Debugger
    // chain itself stays in python until a future port; rust re-emits
    // ``TaskIdentified`` so the chain still runs against the new scope.
    crate::handlers::risk_scope::register(&mut r);
    // AI Debugger chain — first hop ported (TaskIdentified →
    // PotentialRiskIdentified). identify_mitigations / render_suggestion
    // / apply_mitigation still python-subscribed and consume our
    // PotentialRiskIdentified emit. Each new hop adds a handler in
    // ``risk_chain.rs``.
    crate::handlers::risk_chain::register(&mut r);
    // Feedback persistence — replaces
    // ``dorian/event/handlers/lifecycle.handle_feedback`` and
    // ``handle_feedback_edit_requested``. Pure I/O, no python compute
    // on the hot path; per-answer callback keys still live in Redis
    // so the python profiling-pipeline polls keep resuming as before.
    crate::handlers::feedback::register(&mut r);
    // Recommendation interaction handlers (Selected/Upvoted/Downvoted)
    // — record_interaction redis update + selected→meta save +
    // RecommendationPipelineSaved emit. Plus
    // ``handle_pipeline_objectives_switch`` flips the session's ranking
    // objectives to PIPELINE_DEFAULTS on the same triggers. The heavy
    // ``suggest_with_status`` re-rank still fires on the python
    // event-bus until the engine itself ports next.
    crate::handlers::recommendation::register(&mut r);
    // Native rust recommendation engine — replaces python
    // ``attempt_recommendations`` + ``_handle_interaction``. Calls
    // ``optimizer::recommendation::{score_candidates, ranking::rank}``
    // directly; no python KDTree round-trip.
    crate::handlers::recommendations_engine::register(&mut r);
    // Experiment-store write handlers — replaces the postgres-write
    // slice of ``dorian/event/handlers/experiment.py``. Five
    // handlers: DataProfiled (datasets table + doc_datasets),
    // PipelineSaved (pipelines table), PipelineRunCompleted/Failed
    // (evaluations rows), and PipelineRecommendation*
    // (interactions table).
    crate::handlers::experiment_store::register(&mut r);
    // ExecutePipeline → ZADD onto the python runner's priority queue.
    // Operator runtime stays python (the runner pops the queue +
    // executes the pipeline via Dask).
    crate::handlers::execute_pipeline::register(&mut r);
    // Worker-host metrics — push WorkerMetricsCollected entries onto a
    // bounded redis list for the future rust observability endpoint.
    crate::handlers::observability::register(&mut r);
    // Cross-product trial scheduling — DatasetUpserted +
    // PipelineUpserted → enqueue per-gap trial jobs at background
    // priority. Operator runtime stays python (runner pops queue).
    crate::handlers::cross_product_trials::register(&mut r);
    // Pattern-gated retry invalidation — Phase 2 of
    // (internal design note; not in public repo). ``MitigationRewriteApplied`` /
    // ``RLMitigationApplied`` flip
    // ``exception_patterns.active = FALSE`` for every pattern
    // whose ``mitigations`` array contains the just-applied
    // rewrite slug, so xproduct's gate re-enqueues failed pairs
    // on the next tick.
    crate::handlers::pattern_gating::register(&mut r);
    // Pipeline extraction — replaces the python
    // ``dorian/event/handlers/extraction.py::handle_extract_pipeline``.
    // Calls the rust ``extractor`` crate directly (no FFI), runs
    // the curated rule set + the user's saved JSON specs, persists
    // the resulting ``Model`` to ``doc_extractions``, sets the
    // per-session ``extraction:active`` redis key, and pushes a
    // canvas-compatible payload onto the user's WS stream. The
    // rule-suggestion / accept / save-rules flow stays python for
    // now (LLM-driven, application-specific); core extraction is
    // rust-native as of this commit.
    crate::handlers::extraction::register(&mut r);
    // python_dispatch — every former ``subscribe(E.X, handle_X)``
    // line in ``dorian/event/registry.py`` is bridged here as
    // ``rust subscribes → submit ``post:NAME`` job → python worker
    // pops + runs the existing handler body``. The python wrappers
    // live in ``dorian/exec/post_handlers.py``; rewriting any of them
    // as a native rust handler retires the corresponding bridge.
    crate::handlers::python_dispatch::register(&mut r);
    // Slack webhook fan-out — replaces
    // ``dorian/event/handlers/notifications.py``. Subscribes to every
    // error-event in ``slack::SLACK_ERROR_EVENTS`` plus session
    // lifecycle / feedback / backup / contact / tooltip-feedback.
    // ``DORIAN_SLACK_WEBHOOK_URL`` from the env gates the whole module
    // — empty value short-circuits every handler.
    crate::handlers::slack::register(&mut r);
    // KB-mutation broadcast — reloads ``state.kb`` from disk
    // and pushes refreshed catalogs to every active session so
    // SPAs don't have to reconnect after an operator catalog
    // change. See ``handlers/kb_changed.rs`` for the python-side
    // split (cache invalidation stays in python).
    crate::handlers::kb_changed::register(&mut r);
    info!(
        types = ?r.registered_types(),
        "built rust handler registry"
    );
    r
}
