//! HedgePolicy — Rust port of `rl/policy/hedge_policy.py`.
//!
//! Classical Hedge / multiplicative weights (Freund & Schapire 1997)
//! over the persistent action id space. Per step:
//!
//! ```text
//! w[a]  ← w[a] · exp(η · r(a))        (only for the actually chosen a)
//! π(a|s) ∝ mask(a|s) · w[a]
//! ```
//!
//! No gradients, no replay buffer. One O(|candidates|) weighted
//! sample per decision; one O(|trajectory|) scalar update per
//! episode. Classical no-regret guarantee against any adversary.
//!
//! Cross-rollout safety: weights live behind a single `Mutex` so
//! parallel rollout threads can `select` and `update` concurrently
//! without racing.

use parking_lot::Mutex;
use rand::rngs::StdRng;
use rand::SeedableRng;
use rustc_hash::FxHashMap;

use super::base::{
    masked_indices, pick_with_weights, ActionCandidate, Observation, Policy, Transition,
    UpdateMetrics,
};

#[derive(Debug, Clone, Copy)]
pub struct HedgeConfig {
    /// RNG seed for reproducibility.
    pub seed: u64,
    /// Learning rate. Classical Hedge sets η = √(8 ln |A| / T) but
    /// we default to a constant 0.1 for online operation where T
    /// is unknown.
    pub eta: f64,
    /// Safety cap on log(w[a]) to prevent numerical blowup on
    /// sustained high rewards. When a log-weight hits the cap, all
    /// log-weights are rebased by subtracting their max.
    pub max_log_weight: f64,
    /// Weight on a per-action cache-affinity nudge read from
    /// `obs.extras["cache_affinity_per_action"]`. Default 0.0 — the
    /// composition matrix has HybridPolicy or an outer env-level
    /// nudge do this explicitly so the pure-Hedge baseline stays pure.
    pub cache_affinity_scale: f64,
}

impl Default for HedgeConfig {
    fn default() -> Self {
        Self {
            seed: 0,
            eta: 0.1,
            max_log_weight: 20.0,
            cache_affinity_scale: 0.0,
        }
    }
}

pub struct HedgePolicy {
    cfg: HedgeConfig,
    state: Mutex<HedgeState>,
}

struct HedgeState {
    log_weights: FxHashMap<u64, f64>,
    rng: StdRng,
}

impl HedgePolicy {
    pub fn new() -> Self {
        Self::with_config(HedgeConfig::default())
    }

    pub fn with_config(cfg: HedgeConfig) -> Self {
        Self {
            state: Mutex::new(HedgeState {
                log_weights: FxHashMap::default(),
                rng: StdRng::seed_from_u64(cfg.seed),
            }),
            cfg,
        }
    }

    /// Bias the weight distribution toward `action_ids` as if a
    /// prior successful trajectory had landed on each. Same
    /// `lw += eta * strength` update organic rollouts use, so
    /// warm-started weights are on the same scale as learned ones
    /// and degrade under the same rebase.
    pub fn credit_synthetic_trajectory(&self, action_ids: &[u64], strength: f64) {
        if action_ids.is_empty() {
            return;
        }
        let mut s = self.state.lock();
        let seed_lw = geometric_mean_log_weight(&s.log_weights);
        for &aid in action_ids {
            let lw = *s.log_weights.entry(aid).or_insert(seed_lw);
            s.log_weights.insert(aid, lw + self.cfg.eta * strength);
        }
        rebase_if_exceeds_cap(&mut s.log_weights, self.cfg.max_log_weight);
    }

    /// Normalised softmax weight for a given action under the
    /// current state. Useful for tests + dashboards.
    pub fn weight_of(&self, action_id: u64) -> f64 {
        let s = self.state.lock();
        let lw = match s.log_weights.get(&action_id) {
            Some(v) => *v,
            None => return 0.0,
        };
        let max_lw = s.log_weights.values().cloned().fold(f64::NEG_INFINITY, f64::max);
        (lw - max_lw).exp()
    }

    /// Read-only snapshot of the current log-weight map.
    pub fn snapshot(&self) -> FxHashMap<u64, f64> {
        self.state.lock().log_weights.clone()
    }

    pub fn n_actions_seen(&self) -> usize {
        self.state.lock().log_weights.len()
    }
}

impl Default for HedgePolicy {
    fn default() -> Self {
        Self::new()
    }
}

impl Policy for HedgePolicy {
    fn select(
        &mut self,
        obs: &Observation,
        candidates: &[ActionCandidate],
        mask: &[bool],
    ) -> u64 {
        let indices = masked_indices(mask);
        if indices.is_empty() {
            panic!("HedgePolicy::select called with empty mask");
        }
        let mut s = self.state.lock();
        let seed_lw = geometric_mean_log_weight(&s.log_weights);
        // Seed any unseen actions with the geometric mean.
        for &i in &indices {
            let aid = candidates[i].action_id;
            s.log_weights.entry(aid).or_insert(seed_lw);
        }
        // Compute log-scores for the masked candidates.
        let mut log_scores: Vec<f64> = Vec::with_capacity(indices.len());
        for &i in &indices {
            let aid = candidates[i].action_id;
            let mut lw = *s.log_weights.get(&aid).unwrap_or(&seed_lw);
            lw += self.cfg.cache_affinity_scale * read_cache_affinity(obs, &candidates[i]);
            // Suggestion-weight boost: same semantics as the Python.
            let sugg = candidates[i].suggestion_weight;
            if sugg > 1.0 {
                lw += sugg.ln();
            }
            log_scores.push(lw);
        }
        let max_lw = log_scores.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let mut weights = vec![0.0; candidates.len()];
        for (offset, &i) in indices.iter().enumerate() {
            weights[i] = (log_scores[offset] - max_lw).exp();
        }
        let chosen = pick_with_weights(&mut s.rng, &weights, &indices);
        candidates[chosen].action_id
    }

    fn update(&mut self, trajectory: &[Transition]) -> UpdateMetrics {
        let mut metrics: UpdateMetrics = FxHashMap::default();
        if trajectory.is_empty() {
            metrics.insert("trajectory_len".into(), 0.0);
            return metrics;
        }
        // Classical Hedge assigns the terminal reward to every
        // action in the trajectory. Crude credit assignment that
        // matches the thesis's "late reward" setting.
        let terminal = trajectory.last().unwrap().reward;
        let mut s = self.state.lock();
        let seed_lw = geometric_mean_log_weight(&s.log_weights);
        for step in trajectory {
            let lw = *s.log_weights.get(&step.action_id).unwrap_or(&seed_lw);
            s.log_weights
                .insert(step.action_id, lw + self.cfg.eta * terminal);
        }
        rebase_if_exceeds_cap(&mut s.log_weights, self.cfg.max_log_weight);
        let max_lw = s.log_weights.values().cloned().fold(f64::NEG_INFINITY, f64::max);
        let min_lw = s.log_weights.values().cloned().fold(f64::INFINITY, f64::min);
        metrics.insert("trajectory_len".into(), trajectory.len() as f64);
        metrics.insert("terminal_reward".into(), terminal);
        metrics.insert(
            "max_log_weight".into(),
            if max_lw.is_finite() { max_lw } else { 0.0 },
        );
        metrics.insert(
            "min_log_weight".into(),
            if min_lw.is_finite() { min_lw } else { 0.0 },
        );
        metrics.insert("n_actions_seen".into(), s.log_weights.len() as f64);
        metrics
    }
}


// ---------------------------------------------------------------------------
// Internals — mirror the Python helpers.
// ---------------------------------------------------------------------------

fn geometric_mean_log_weight(weights: &FxHashMap<u64, f64>) -> f64 {
    if weights.is_empty() {
        return 0.0;
    }
    weights.values().sum::<f64>() / weights.len() as f64
}

fn rebase_if_exceeds_cap(weights: &mut FxHashMap<u64, f64>, cap: f64) {
    if weights.is_empty() {
        return;
    }
    let peak = weights.values().cloned().fold(f64::NEG_INFINITY, f64::max);
    if peak <= cap {
        return;
    }
    // Subtract peak from every entry — preserves the softmax
    // distribution exactly.
    for v in weights.values_mut() {
        *v -= peak;
    }
}

fn read_cache_affinity(obs: &Observation, cand: &ActionCandidate) -> f64 {
    let aff = match obs.extras.get("cache_affinity_per_action") {
        Some(v) => v,
        None => return 0.0,
    };
    let map = match aff.as_object() {
        Some(m) => m,
        None => return 0.0,
    };
    let key = cand.action_id.to_string();
    map.get(&key)
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0)
}


#[cfg(test)]
mod tests {
    use super::*;

    fn obs(step: u32) -> Observation {
        Observation {
            dag_json: String::new(),
            dataset_embedding: Vec::new(),
            step_idx: step,
            remaining_budget: 100,
            extras: FxHashMap::default(),
        }
    }

    fn cand(action_id: u64) -> ActionCandidate {
        ActionCandidate {
            action_id,
            op_key: format!("op-{action_id}"),
            features: Vec::new(),
            suggestion_weight: 1.0,
        }
    }

    #[test]
    fn empty_mask_panics() {
        let mut p = HedgePolicy::new();
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            p.select(&obs(0), &[cand(1)], &[false])
        }));
        assert!(r.is_err());
    }

    #[test]
    fn select_returns_one_of_masked_candidates() {
        let mut p = HedgePolicy::with_config(HedgeConfig {
            seed: 7, ..HedgeConfig::default()
        });
        let cands = vec![cand(10), cand(20), cand(30)];
        let mask = vec![true, false, true];
        for _ in 0..10 {
            let aid = p.select(&obs(0), &cands, &mask);
            assert!(aid == 10 || aid == 30, "got aid {aid}");
        }
    }

    #[test]
    fn update_with_high_vs_low_reward_diverges_weights() {
        let mut p = HedgePolicy::new();
        // Initialise both actions via select (geometric-mean seed),
        // THEN update one with high reward and the other with low.
        let cands = vec![cand(1), cand(2)];
        let mask = vec![true, true];
        let _ = p.select(&obs(0), &cands, &mask);
        // Action 1 sees high reward; action 2 sees zero reward.
        // Because both pre-existed at the same weight, the eta·r
        // bump pulls them apart.
        for _ in 0..5 {
            p.update(&[Transition {
                obs: obs(0), action_id: 1, reward: 1.0,
                next_obs: None, terminal: true,
            }]);
            p.update(&[Transition {
                obs: obs(0), action_id: 2, reward: 0.0,
                next_obs: None, terminal: true,
            }]);
        }
        // Action 1 should end with higher weight than action 2 — its
        // log-weight got bumped by 0.1 each iteration while
        // action 2 just inherited the running mean.
        let snap = p.snapshot();
        let lw1 = *snap.get(&1).unwrap();
        let lw2 = *snap.get(&2).unwrap();
        assert!(lw1 > lw2,
            "expected log-weight 1 > 2, got lw1={lw1}, lw2={lw2}");
    }

    #[test]
    fn rebase_after_cap_exceeded() {
        let cfg = HedgeConfig { max_log_weight: 5.0, eta: 1.0, ..Default::default() };
        let mut p = HedgePolicy::with_config(cfg);
        // 10 trajectories of reward=10 → log-weight should grow.
        for _ in 0..10 {
            p.update(&[Transition {
                obs: obs(0), action_id: 42, reward: 10.0,
                next_obs: None, terminal: true,
            }]);
        }
        // After rebase, max log-weight must be ≤ 0 (we subtracted
        // the peak from every entry — peak is now 0).
        let snap = p.snapshot();
        let max = snap.values().cloned().fold(f64::NEG_INFINITY, f64::max);
        assert!(max <= 0.0, "max log-weight should be ≤ 0 after rebase, got {max}");
    }

    #[test]
    fn synthetic_trajectory_credit_increases_weights() {
        let p = HedgePolicy::new();
        p.credit_synthetic_trajectory(&[1, 2, 3], 1.0);
        assert_eq!(p.n_actions_seen(), 3);
        assert!(p.weight_of(1) > 0.0);
    }

    #[test]
    fn suggestion_weight_biases_selection() {
        let mut p = HedgePolicy::with_config(HedgeConfig {
            seed: 11, ..HedgeConfig::default()
        });
        let mut a = cand(100);
        let mut b = cand(200);
        b.suggestion_weight = 100.0; // strong boost on b
        let cands = vec![a.clone(), b.clone()];
        let mask = vec![true, true];
        let mut counts = (0u32, 0u32);
        for _ in 0..200 {
            let aid = p.select(&obs(0), &cands, &mask);
            if aid == 100 { counts.0 += 1; } else { counts.1 += 1; }
        }
        // b should win the vast majority because of the 100x boost.
        assert!(counts.1 > counts.0 * 5,
            "suggestion weight should bias selection: a={}, b={}",
            counts.0, counts.1);
        // Silence unused-var warnings in the cands construction.
        a.suggestion_weight = 1.0;
        b.suggestion_weight = 100.0;
    }
}
