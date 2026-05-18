//! `Optimizer` trait — the modular interface every BO engine
//! implements. AutoML drivers (RL trial loop, dedicated AutoML
//! sweeps, cross-product targeted refinement) consume the trait
//! directly so swapping engines is a one-line decision per call.

use rustc_hash::FxHashMap;

use crate::config::{ParamDomain, ParamValue};
use crate::trial::Trial;

/// One slot in a template — names a logical task and the candidate
/// concrete operators (with their hyperparameter domains) the KB
/// exposes for it. The optimizer reads this when building its
/// search space for a template.
#[derive(Debug, Clone)]
pub struct SlotSpec {
    /// Hierarchical canonical task path, joined with ".".
    pub task_path: String,
    /// Candidate (op_fqn, params) bindings the KB returned. The
    /// optimizer treats `op_fqn` as a categorical variable and
    /// each operator's params as conditional sub-spaces.
    pub candidates: Vec<OperatorCandidate>,
}

#[derive(Debug, Clone)]
pub struct OperatorCandidate {
    pub op_fqn: String,
    pub params: Vec<(String, ParamDomain)>,
}

/// One sample the optimizer proposes. The trial loop binds the
/// template slots from this map: each template slot id maps to
/// a chosen operator + its hyperparameter values.
#[derive(Debug, Clone)]
pub struct Suggestion {
    pub bindings: FxHashMap<String, SlotBinding>,
}

#[derive(Debug, Clone)]
pub struct SlotBinding {
    pub op_fqn: String,
    pub params: FxHashMap<String, ParamValue>,
}

/// The trait every BO engine implements.
pub trait Optimizer: Send + Sync {
    /// Propose `k` suggestions to evaluate. Batch size is up to
    /// the caller — larger batches enable parallel dispatch but
    /// reduce surrogate-update opportunities.
    fn ask(&mut self, slots: &[SlotSpec], k: usize) -> Vec<Suggestion>;

    /// Update the surrogate with completed trials. Failed trials
    /// (Trial::is_success() == false) are still useful — they
    /// teach the surrogate which regions to avoid.
    fn tell(&mut self, trials: &[Trial]);

    /// Warm-start the surrogate with historical trials. Called
    /// once at engine startup with every Trial in the experiment
    /// store (filtered by template / dataset profile / source per
    /// the caller's preference). Same effect as a long sequence
    /// of `tell()` calls; engines may use a more efficient batch
    /// fitting path here.
    fn warm_start(&mut self, history: &[Trial]) {
        if !history.is_empty() {
            self.tell(history);
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OptimizerKind {
    Smac,
    Random,
    // Tpe and Bore land later — slot reserved.
}

impl OptimizerKind {
    pub fn from_env() -> Self {
        match std::env::var("DORIAN_AUTOML_OPTIMIZER")
            .unwrap_or_default()
            .to_lowercase()
            .as_str()
        {
            "random" => OptimizerKind::Random,
            // Default and explicit "smac" both pick the SMAC engine.
            _ => OptimizerKind::Smac,
        }
    }
}

/// Construct the configured optimizer. Trial-loop callers
/// generally let this default to SMAC; explicit kinds are useful
/// for benchmarking against the random baseline.
pub fn build_optimizer(kind: OptimizerKind) -> Box<dyn Optimizer> {
    match kind {
        OptimizerKind::Smac => Box::new(crate::SmacOptimizer::new()),
        OptimizerKind::Random => Box::new(crate::RandomOptimizer::new()),
    }
}
