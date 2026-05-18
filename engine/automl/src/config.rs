//! Hyperparameter domain types. The optimizer enumerates
//! candidate (op_fqn, param_domain) pairs from the KB at
//! sample-time; this module owns the value types those domains
//! are expressed in.

use serde::{Deserialize, Serialize};

/// One sampled hyperparameter value. Mirrors what the dorian DAG
/// expects in a `Parameter` node — the trial loop converts these
/// to Parameters when binding a template to a concrete pipeline.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ParamValue {
    Int(i64),
    Float(f64),
    Bool(bool),
    Str(String),
}

/// Closed bounds for a numeric domain. Both ends inclusive.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Bounds<T> {
    pub low: T,
    pub high: T,
    /// True when the parameter is sampled in log-space (typical
    /// for learning rates, regularisation strength, etc.). The
    /// optimizer's `Bounds<f64>` exposes this; integer bounds
    /// don't (use a categorical for log-spaced ints).
    #[serde(default)]
    pub log_scale: bool,
}

/// Allowed shapes a parameter domain can take. Matches what the
/// KB exposes: bounded numeric, categorical choice, fixed value.
/// Future: conditional domains (param X only valid when Y == "foo")
/// — SMAC handles them natively, leave the slot.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ParamDomain {
    /// Continuous numeric in [low, high] (optionally log-scaled).
    Float(Bounds<f64>),
    /// Discrete numeric in [low, high] inclusive.
    Int(Bounds<i64>),
    /// One of N categorical strings.
    Categorical(Choice<String>),
    /// Bool — short for Categorical of [false, true].
    Bool,
    /// Constant — already-bound, doesn't enter the search space.
    Constant(ParamValue),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Choice<T> {
    pub options: Vec<T>,
    /// Default option index — the surrogate uses this as the warm
    /// start when no Trial history is available for the slot.
    #[serde(default)]
    pub default: Option<usize>,
}

impl ParamDomain {
    /// Sample one value uniformly at random from the domain. Log
    /// scale is honoured for `Float`. Used by `RandomOptimizer`
    /// and as the warm-start sampler inside `SmacOptimizer` while
    /// the surrogate accrues its first observations.
    pub fn sample(&self, rng: &mut impl rand::Rng) -> ParamValue {
        use rand::seq::SliceRandom;
        match self {
            ParamDomain::Float(b) => {
                if b.log_scale && b.low > 0.0 && b.high > 0.0 {
                    let lo = b.low.ln();
                    let hi = b.high.ln();
                    let u = rng.gen_range(lo..=hi);
                    ParamValue::Float(u.exp())
                } else {
                    ParamValue::Float(rng.gen_range(b.low..=b.high))
                }
            }
            ParamDomain::Int(b) => ParamValue::Int(rng.gen_range(b.low..=b.high)),
            ParamDomain::Categorical(c) => {
                let pick = c.options.choose(rng).cloned().unwrap_or_default();
                ParamValue::Str(pick)
            }
            ParamDomain::Bool => ParamValue::Bool(rng.gen_bool(0.5)),
            ParamDomain::Constant(v) => v.clone(),
        }
    }
}
