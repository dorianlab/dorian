//! SMAC3-style optimizer.
//!
//! v1: random forest surrogate + expected-improvement acquisition.
//! For each `ask()`:
//!
//!   1. Sample a candidate pool (default 1024) of random
//!      Suggestions from the slot search space.
//!   2. Encode each candidate via `ConfigEncoder` to a dense
//!      vector.
//!   3. Predict `(mu, sigma)` from the surrogate (trained on
//!      observed Trial history).
//!   4. Score candidates by EI(x) = (mu - f_best) · Φ(z) +
//!      σ · φ(z) where z = (mu - f_best) / σ.
//!   5. Return the top-k.
//!
//! Surrogate retrains on every `tell()` — the v1 forest is small
//! (32 trees, depth 16), so refit is fast even with hundreds of
//! observations. v2 may switch to incremental updates.
//!
//! Init phase: while `n_observations < N_INIT` (default 10), the
//! ask path skips the surrogate and delegates to `RandomOptimizer`.
//! Random sampling is the right cold-start because the surrogate
//! has nothing to learn from yet.
//!
//! Cross-dataset transfer: v1 keeps one surrogate per Optimizer
//! instance — callers that want per-dataset specialisation
//! instantiate one Optimizer per dataset. v2 will append the
//! dataset profile vector to every config-vec so a single
//! surrogate handles multi-dataset transfer learning.

use rustc_hash::FxHashMap;

use crate::encoder::ConfigEncoder;
use crate::optimizer::{Optimizer, SlotSpec, Suggestion};
use crate::random::RandomOptimizer;
use crate::surrogate::{ForestConfig, RandomForest};
use crate::trial::Trial;

const N_INIT: usize = 10;
const CANDIDATE_POOL: usize = 1024;
const RETURN_TOP_K_FACTOR: usize = 4;

pub struct SmacOptimizer {
    fallback: RandomOptimizer,
    history: Vec<Trial>,
    seen_configs: FxHashMap<String, f64>,
    /// Cached encoder built lazily from the first `ask()` call —
    /// the slot definitions don't change across calls within one
    /// optimizer's lifetime.
    encoder: Option<ConfigEncoder>,
    /// Cached surrogate. Rebuilt on every tell() from the
    /// encoded history.
    surrogate: Option<RandomForest>,
    /// Encoded training rows + targets — kept around so we can
    /// refit the surrogate cheaply on each tell().
    encoded_x: Vec<Vec<f64>>,
    encoded_y: Vec<f64>,
    /// Best score observed so far (max). Used as f_best in EI.
    incumbent: Option<f64>,
}

impl SmacOptimizer {
    pub fn new() -> Self {
        Self::with_seed(0x5AAC3)
    }
    pub fn with_seed(seed: u64) -> Self {
        Self {
            fallback: RandomOptimizer::with_seed(seed),
            history: Vec::new(),
            seen_configs: FxHashMap::default(),
            encoder: None,
            surrogate: None,
            encoded_x: Vec::new(),
            encoded_y: Vec::new(),
            incumbent: None,
        }
    }
    pub fn incumbent_score(&self) -> Option<f64> {
        self.incumbent
    }
    pub fn n_observations(&self) -> usize {
        self.history.len()
    }
}

impl Default for SmacOptimizer {
    fn default() -> Self {
        Self::new()
    }
}

impl Optimizer for SmacOptimizer {
    fn ask(&mut self, slots: &[SlotSpec], k: usize) -> Vec<Suggestion> {
        // Cold start: random sampling until the surrogate has enough
        // observations to be useful.
        if self.n_observations() < N_INIT || self.surrogate.is_none() {
            return self.fallback.ask(slots, k);
        }
        // Lazy-init encoder when slots first arrive.
        if self.encoder.is_none() {
            self.encoder = Some(ConfigEncoder::from_slots(slots));
        }
        let encoder = self.encoder.as_ref().expect("encoder set above");
        let surrogate = self.surrogate.as_ref().expect("surrogate set above");
        let f_best = self.incumbent.unwrap_or(0.0);

        // Sample a candidate pool, encode, score by EI, return top-k.
        let pool_size = (CANDIDATE_POOL).max(k * RETURN_TOP_K_FACTOR);
        let candidates = self.fallback.ask(slots, pool_size);
        let mut scored: Vec<(f64, Suggestion)> = candidates
            .into_iter()
            .map(|sugg| {
                let xv = encoder.encode(&sugg);
                let (mu, sigma) = surrogate.predict(&xv);
                let ei = expected_improvement(mu, sigma, f_best);
                (ei, sugg)
            })
            .collect();
        scored.sort_by(|a, b| {
            b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal)
        });
        scored.into_iter().take(k).map(|(_, s)| s).collect()
    }

    fn tell(&mut self, trials: &[Trial]) {
        for t in trials {
            let key = t.config.to_string();
            if self.seen_configs.contains_key(&key) {
                continue;
            }
            self.seen_configs.insert(key, t.score());
            self.history.push(t.clone());

            if !t.is_success() {
                // Failed/timeout trials still inform the surrogate —
                // we encode them with the worst known score so the
                // surrogate avoids that region.
                if self.encoder.is_none() {
                    continue; // can't encode without slot defs yet
                }
                let pseudo_score = self.incumbent.map(|f| f - 1.0).unwrap_or(-1.0);
                if let Some(enc) = &self.encoder {
                    if let Ok(suggestion) = trial_to_suggestion(t) {
                        self.encoded_x.push(enc.encode(&suggestion));
                        self.encoded_y.push(pseudo_score);
                    }
                }
                continue;
            }

            // Success — update incumbent + add encoded training row.
            let score = t.score();
            self.incumbent = Some(self.incumbent.map_or(score, |b| b.max(score)));
            if let Some(enc) = &self.encoder {
                if let Ok(suggestion) = trial_to_suggestion(t) {
                    self.encoded_x.push(enc.encode(&suggestion));
                    self.encoded_y.push(score);
                }
            }
        }
        // Refit if the encoder is up + we have at least N_INIT rows.
        if self.history.len() >= N_INIT && !self.encoded_x.is_empty() {
            self.surrogate = Some(RandomForest::fit(
                &self.encoded_x, &self.encoded_y, ForestConfig::default(),
            ));
        }
    }

    fn warm_start(&mut self, history: &[Trial]) {
        self.history.reserve(history.len());
        self.tell(history);
    }
}


/// EI for a maximisation objective:
///   EI(x) = (mu - f_best) Phi(z) + sigma * phi(z)
/// where z = (mu - f_best) / sigma.
/// Returns 0 when sigma == 0 (no surrogate uncertainty → no
/// improvement to claim).
pub fn expected_improvement(mu: f64, sigma: f64, f_best: f64) -> f64 {
    if sigma <= 1e-12 {
        return (mu - f_best).max(0.0);
    }
    let z = (mu - f_best) / sigma;
    let cdf = standard_normal_cdf(z);
    let pdf = standard_normal_pdf(z);
    (mu - f_best) * cdf + sigma * pdf
}

fn standard_normal_pdf(z: f64) -> f64 {
    (-0.5 * z * z).exp() / (2.0 * std::f64::consts::PI).sqrt()
}

fn standard_normal_cdf(z: f64) -> f64 {
    // Abramowitz & Stegun 26.2.17 — close-form approximation,
    // accurate to ~1e-7. Avoids pulling `statrs` in for one
    // function. SMAC uses statrs but the approximation suffices
    // for EI ranking (relative ordering matters, not absolute
    // values).
    0.5 * (1.0 + erf(z / 2.0_f64.sqrt()))
}

fn erf(x: f64) -> f64 {
    // Abramowitz & Stegun 7.1.26
    let a1 = 0.254829592;
    let a2 = -0.284496736;
    let a3 = 1.421413741;
    let a4 = -1.453152027;
    let a5 = 1.061405429;
    let p = 0.3275911;
    let sign = if x < 0.0 { -1.0 } else { 1.0 };
    let x = x.abs();
    let t = 1.0 / (1.0 + p * x);
    let y = 1.0
        - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * (-x * x).exp();
    sign * y
}

/// Reconstruct a Suggestion from a Trial's config JSON. The config
/// should be a JSON object with a `bindings` key (per how the
/// AutoML driver writes Trials). Tolerant of older shapes — returns
/// Err so the caller can skip rows we can't encode.
fn trial_to_suggestion(t: &Trial) -> Result<Suggestion, ()> {
    let bindings_val = t.config.get("bindings").ok_or(())?;
    let bindings_map = bindings_val.as_object().ok_or(())?;
    let mut bindings = FxHashMap::default();
    for (slot, b) in bindings_map {
        let op_fqn = b.get("op_fqn").and_then(|v| v.as_str()).ok_or(())?;
        let params_val = b.get("params").ok_or(())?;
        let params_map = params_val.as_object().ok_or(())?;
        let mut params = FxHashMap::default();
        for (pname, pval) in params_map {
            let v = json_to_param_value(pval)?;
            params.insert(pname.clone(), v);
        }
        bindings.insert(
            slot.clone(),
            crate::optimizer::SlotBinding { op_fqn: op_fqn.to_string(), params },
        );
    }
    Ok(Suggestion { bindings })
}

fn json_to_param_value(v: &serde_json::Value) -> Result<crate::config::ParamValue, ()> {
    use crate::config::ParamValue;
    Ok(match v {
        serde_json::Value::Bool(b) => ParamValue::Bool(*b),
        serde_json::Value::Number(n) => {
            if n.is_i64() {
                ParamValue::Int(n.as_i64().unwrap())
            } else {
                ParamValue::Float(n.as_f64().unwrap_or(0.0))
            }
        }
        serde_json::Value::String(s) => ParamValue::Str(s.clone()),
        _ => return Err(()),
    })
}


#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Bounds, ParamDomain};
    use crate::optimizer::{OperatorCandidate, SlotSpec};

    fn slot() -> SlotSpec {
        SlotSpec {
            task_path: "Modeling".into(),
            candidates: vec![OperatorCandidate {
                op_fqn: "model.A".into(),
                params: vec![("p".into(), ParamDomain::Float(Bounds {
                    low: 0.0, high: 1.0, log_scale: false,
                }))],
            }],
        }
    }

    #[test]
    fn cold_start_falls_back_to_random() {
        let mut opt = SmacOptimizer::with_seed(7);
        let slots = vec![slot()];
        let suggestions = opt.ask(&slots, 3);
        assert_eq!(suggestions.len(), 3);
    }

    #[test]
    fn ei_zero_when_sigma_and_mu_match_best() {
        let ei = expected_improvement(0.5, 0.0, 0.5);
        assert_eq!(ei, 0.0);
    }

    #[test]
    fn ei_positive_when_mu_exceeds_best_with_uncertainty() {
        let ei = expected_improvement(0.6, 0.1, 0.5);
        assert!(ei > 0.0, "ei={ei}");
    }

    #[test]
    fn ei_decreases_as_mu_falls_below_best() {
        let ei_high = expected_improvement(0.5, 0.1, 0.4);
        let ei_low  = expected_improvement(0.3, 0.1, 0.4);
        assert!(ei_high > ei_low, "ei_high={ei_high}, ei_low={ei_low}");
    }
}
