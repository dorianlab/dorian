//! Policy core interface — Rust port of `rl/policy/base.py`.
//!
//! Every policy architecture lives in its own module
//! (`hedge`, `memory` (later), `hybrid` (later)) and implements
//! the `Policy` trait below. Ablation harnesses can swap policies
//! without touching the env, encoder, memory, dispatch, or
//! isolation layers — mirrors the Python contract.

use rand::Rng;
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

/// What the policy sees at a decision step.
///
/// The fields are the superset of what any current policy reads;
/// individual policies only touch what they need. Adding a field
/// is a non-breaking change (policies ignore unknown fields).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Observation {
    /// Current partial pipeline DAG as a JSON string.
    pub dag_json: String,
    /// Compact dataset fingerprint (8-dim profile vector). Fixed
    /// length across the run; used by memory-based policies for
    /// kNN retrieval and by Hedge's context bucketing.
    pub dataset_embedding: Vec<f64>,
    /// 0-indexed step count within the current episode.
    pub step_idx: u32,
    /// Remaining step budget the env will allow. -1 = unbounded.
    pub remaining_budget: i32,
    /// Optional extra context (task type, user flags). Policies
    /// should not rely on keys they didn't negotiate with the env.
    pub extras: FxHashMap<String, serde_json::Value>,
}

/// One candidate action a policy can pick from on this step.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActionCandidate {
    /// Integer id from the persistent action map. Stable across
    /// episodes and across catalog changes within a catalog version.
    pub action_id: u64,
    /// Operator key (FQN for atomic, "composite::<hash>" for mined).
    pub op_key: String,
    /// Compact per-action features. Policies that don't consume
    /// feature vectors ignore this.
    pub features: Vec<f64>,
    /// Optional deterministic suggestion boost — values >1.0 add
    /// log(boost) to the candidate's policy log-weight at select
    /// time. Default 1.0 (no boost). Mirrors the Python
    /// ``ActionCandidate.suggestion_weight`` attribute.
    pub suggestion_weight: f64,
}

impl Default for ActionCandidate {
    fn default() -> Self {
        Self {
            action_id: 0,
            op_key: String::new(),
            features: Vec::new(),
            suggestion_weight: 1.0,
        }
    }
}

/// One step in a trajectory.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Transition {
    pub obs: Observation,
    pub action_id: u64,
    pub reward: f64,
    pub next_obs: Option<Observation>,
    pub terminal: bool,
}

/// Observability metrics returned by `Policy::update`.
pub type UpdateMetrics = FxHashMap<String, f64>;

/// Abstract contract for a policy core.
pub trait Policy: Send {
    /// Pick one action_id from the masked-True candidates. Must be
    /// deterministic given the policy's internal RNG state.
    fn select(
        &mut self,
        obs: &Observation,
        candidates: &[ActionCandidate],
        mask: &[bool],
    ) -> u64;

    /// Process a completed trajectory. Returns observability
    /// metrics for dashboard consumption (empty map is valid).
    fn update(&mut self, trajectory: &[Transition]) -> UpdateMetrics;
}

// ---------------------------------------------------------------------------
// Shared utilities — direct ports of the Python helpers.
// ---------------------------------------------------------------------------

/// Indices of True entries in a mask.
pub fn masked_indices(mask: &[bool]) -> Vec<usize> {
    mask.iter()
        .enumerate()
        .filter_map(|(i, m)| if *m { Some(i) } else { None })
        .collect()
}

/// Weighted sample from `indices` using `weights[indices]`.
/// Falls back to uniform when all weights are zero/NaN, matching
/// the Python helper's "at least one valid action" safeguard.
pub fn pick_with_weights<R: Rng + ?Sized>(
    rng: &mut R,
    weights: &[f64],
    indices: &[usize],
) -> usize {
    if indices.is_empty() {
        panic!("no candidate indices to pick from");
    }
    let total: f64 = indices.iter().map(|&i| weights[i]).sum();
    if !total.is_finite() || total <= 0.0 {
        // Degenerate distribution → uniform.
        let pick = rng.gen_range(0..indices.len());
        return indices[pick];
    }
    let r: f64 = rng.gen::<f64>() * total;
    let mut cum = 0.0;
    for &i in indices {
        cum += weights[i];
        if r < cum {
            return i;
        }
    }
    *indices.last().unwrap()
}

/// Unit-safe cosine similarity over equal-length vectors.
pub fn cosine_similarity(a: &[f64], b: &[f64]) -> f64 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let dot: f64 = a.iter().zip(b).map(|(x, y)| x * y).sum();
    let na: f64 = a.iter().map(|x| x * x).sum::<f64>().sqrt();
    let nb: f64 = b.iter().map(|y| y * y).sum::<f64>().sqrt();
    if na == 0.0 || nb == 0.0 {
        return 0.0;
    }
    dot / (na * nb)
}


#[cfg(test)]
mod tests {
    use super::*;
    use rand::SeedableRng;
    use rand::rngs::StdRng;

    #[test]
    fn masked_indices_only_returns_true_positions() {
        let mask = vec![true, false, true, true, false];
        assert_eq!(masked_indices(&mask), vec![0, 2, 3]);
    }

    #[test]
    fn pick_with_weights_uniform_on_zero_weights() {
        let mut rng = StdRng::seed_from_u64(1);
        let weights = vec![0.0, 0.0, 0.0];
        let indices = vec![0, 1, 2];
        // Should not panic, should return one of indices.
        let pick = pick_with_weights(&mut rng, &weights, &indices);
        assert!(indices.contains(&pick));
    }

    #[test]
    fn pick_with_weights_respects_distribution() {
        let mut rng = StdRng::seed_from_u64(7);
        let weights = vec![1.0, 0.0, 99.0]; // index 2 dominates
        let indices = vec![0, 1, 2];
        let mut counts = [0u32; 3];
        for _ in 0..1000 {
            counts[pick_with_weights(&mut rng, &weights, &indices)] += 1;
        }
        assert!(counts[2] > 950, "expected ~99%, got {counts:?}");
    }

    #[test]
    fn cosine_similarity_is_symmetric() {
        let a = vec![1.0, 2.0, 3.0];
        let b = vec![4.0, 5.0, 6.0];
        let ab = cosine_similarity(&a, &b);
        let ba = cosine_similarity(&b, &a);
        assert!((ab - ba).abs() < 1e-12);
    }

    #[test]
    fn cosine_similarity_orthogonal_is_zero() {
        let sim = cosine_similarity(&[1.0, 0.0], &[0.0, 1.0]);
        assert!(sim.abs() < 1e-12);
    }

    #[test]
    fn cosine_similarity_aligned_is_one() {
        let sim = cosine_similarity(&[3.0, 4.0], &[6.0, 8.0]);
        assert!((sim - 1.0).abs() < 1e-12);
    }
}
