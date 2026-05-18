//! Cross-product engine — gentle cartesian completion of
//! (pipeline × dataset) coverage.
//!
//! Three engines feed Dorian's experiment store:
//!
//!   1. **RL** — explores pipeline structure (which logical
//!      template, which operator at each slot)
//!   2. **AutoML** — optimises hyperparameters within a chosen
//!      template (SMAC3 BO)
//!   3. **Cross-product** (this crate) — gently fills the
//!      cartesian gap so every saved pipeline gets evaluated on
//!      every saved dataset
//!
//! All three write Trials to the same unified `evaluations` table
//! (see `dorian/experiment/trial.py`). The cross-product engine
//! is the lowest-priority producer — it ensures broad coverage
//! without ever starving the targeted exploration the other two
//! engines do.
//!
//! Design notes:
//!
//!   * **Polling, not subscription.** A 30-second poll over the
//!     `pairs_to_complete` view catches new pipelines/datasets
//!     within bounded latency without needing an event-bus
//!     subscriber. Polling is cheap (one indexed query) and
//!     resilient to event-bus outages.
//!
//!   * **Token-bucket pacing.** ``DORIAN_XPRODUCT_RATE`` (default
//!     10 trials/min) caps the enqueue rate. The bucket refills
//!     once per second; bursty postgres queries can't spike the
//!     queue.
//!
//!   * **LOW priority.** Trials are submitted at
//!     ``priority=BACKGROUND_LOW`` so user-driven runs always win
//!     the queue.
//!
//!   * **Idempotent.** Each enqueue records `(pipeline, dataset,
//!     run_id)` in the `evaluations` table on completion. The
//!     `pairs_to_complete` view excludes pairs that already have a
//!     row, so the same pair never gets enqueued twice.

pub mod config;
pub mod engine;
pub mod pairs;
pub mod queue;
pub mod reaper;

pub use config::Config;
pub use engine::Engine;
pub use pairs::{Pair, PairsToComplete};
pub use queue::TrialQueue;
pub use reaper::StreamReaper;
