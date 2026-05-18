//! Optimizer-side mirror of `dorian.experiment.trial.Trial`.
//! Lives here (rather than in `dorian/experiment/`) because Rust
//! can't import from Python — but the field set + JSON shape are
//! kept identical so trip-through-redis or trip-through-postgres
//! is a straight serde roundtrip in either direction.

use serde::{Deserialize, Serialize};

/// One observation the optimizer's surrogate trains on.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Trial {
    pub pipeline_id: String,
    pub dataset_id: String,
    pub run_id: String,
    pub source: String,
    pub status: String,
    pub metrics: rustc_hash::FxHashMap<String, f64>,
    pub config: serde_json::Value,
    #[serde(default)]
    pub eval_config: Option<serde_json::Value>,
    #[serde(default)]
    pub wall_clock_s: Option<f64>,
    #[serde(default)]
    pub error_message: Option<String>,
}

impl Trial {
    /// Primary metric value. Convention: first metric in the
    /// `metrics` map (eval_template emits classification metrics
    /// in a stable order: accuracy → f1 → precision → recall).
    /// Returns NaN for trials with no metrics.
    pub fn score(&self) -> f64 {
        self.metrics
            .values()
            .next()
            .copied()
            .unwrap_or(f64::NAN)
    }

    pub fn is_success(&self) -> bool {
        self.status == "success" && !self.score().is_nan()
    }
}

/// Flat numeric encoding of a config — the input the surrogate
/// trains/predicts on. Categorical values are one-hot encoded;
/// log-scaled floats land in log space; constants are dropped.
/// The optimizer caches the encoder so repeated `predict` calls
/// on candidate configs don't repeat the work.
pub type ConfigVec = Vec<f64>;
