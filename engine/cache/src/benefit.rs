//! Benefit-driven scoring for admission + eviction (Derakhshan Ch 4).
//!
//! `benefit(v) = compute_cost(v) / size(v) × Σ redundancy(pipeline_using_v)`
//!
//! We don't evict anything in Tier-1 (in-process cache is bounded by
//! the scheduler's run duration), but we compute + record benefit so:
//!
//!   * eviction policy in Tier-4 has a ground-truth signal to sort on,
//!   * the CLI `dem-map` / `batch-plan` subcommands can report the
//!     "biggest wins" when RL batches are submitted,
//!   * shadow-mode runs can compare Dask vs Rust not just on
//!     correctness but on projected cache effectiveness.
//!
//! The only correctness requirement is *ordering* — absolute benefit
//! values can be coarse.

use rustc_hash::FxHashMap;

use crate::CacheKey;

/// Coarse cost estimate for a single firing. In Tier-1 we populate
/// this from real `compute_secs` observations; Tier-4 feeds these
/// back into the eviction policy.
#[derive(Debug, Clone, Copy, Default)]
pub struct Cost {
    pub compute_secs: f64,
    pub size_bytes: u64,
}

/// Benefit score — higher means "keep this entry". Eviction selects
/// the *smallest* benefit for removal.
#[derive(Debug, Clone, Copy)]
pub struct BenefitScore(pub f64);

impl BenefitScore {
    pub fn new(v: f64) -> Self {
        BenefitScore(v)
    }
}

impl PartialEq for BenefitScore {
    fn eq(&self, other: &Self) -> bool {
        self.0.to_bits() == other.0.to_bits()
    }
}
impl Eq for BenefitScore {}
impl PartialOrd for BenefitScore {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for BenefitScore {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.0
            .partial_cmp(&other.0)
            .unwrap_or(std::cmp::Ordering::Equal)
    }
}

/// Compute benefit for a single entry.
///
/// `redundancy` is the number of pending pipelines in the batch that
/// still reference this entry — the factor that tips the balance for
/// RL-fan-out's shared loader/scaler.
pub fn benefit(cost: Cost, redundancy: usize) -> BenefitScore {
    let size = cost.size_bytes.max(1) as f64;
    let ratio = cost.compute_secs / size;
    let score = ratio * redundancy.max(1) as f64;
    BenefitScore::new(score)
}

/// Record type for per-entry cost profiling. Scheduler appends here
/// as firings complete; Tier-4 eviction reads the running averages.
#[derive(Debug, Default)]
pub struct CostProfile {
    observations: FxHashMap<CacheKey, (u64, Cost)>,
}

impl CostProfile {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record an observation. Running mean for cost; last-seen size.
    pub fn observe(&mut self, key: CacheKey, cost: Cost) {
        let entry = self.observations.entry(key).or_insert((0, Cost::default()));
        let (n, running) = entry;
        let new_n = *n + 1;
        let new_cs = (running.compute_secs * (*n as f64) + cost.compute_secs) / new_n as f64;
        *entry = (
            new_n,
            Cost {
                compute_secs: new_cs,
                size_bytes: cost.size_bytes,
            },
        );
    }

    pub fn get(&self, key: &CacheKey) -> Option<Cost> {
        self.observations.get(key).map(|(_, c)| *c)
    }

    pub fn observation_count(&self, key: &CacheKey) -> u64 {
        self.observations.get(key).map(|(n, _)| *n).unwrap_or(0)
    }

    pub fn len(&self) -> usize {
        self.observations.len()
    }

    pub fn is_empty(&self) -> bool {
        self.observations.is_empty()
    }
}

// ---------------------------------------------------------------------------
// Eviction candidate picker
// ---------------------------------------------------------------------------

/// Given a budget and a population, return keys to evict in
/// smallest-benefit-first order until we're under budget.
///
/// This is the *pure function* half of eviction; the store impl
/// decides when to call it. Tier-1 `MemoryStore` doesn't evict; Tier-4
/// will add a size-tracked variant.
pub fn pick_eviction(
    population: &[(CacheKey, BenefitScore, u64)],
    current_bytes: u64,
    target_bytes: u64,
) -> Vec<CacheKey> {
    if current_bytes <= target_bytes {
        return Vec::new();
    }
    let mut sorted: Vec<&(CacheKey, BenefitScore, u64)> = population.iter().collect();
    sorted.sort_by_key(|(_, s, _)| *s);
    let mut running = current_bytes;
    let mut out = Vec::new();
    for (k, _score, size) in sorted {
        if running <= target_bytes {
            break;
        }
        running = running.saturating_sub(*size);
        out.push(*k);
    }
    out
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn k(n: u8) -> CacheKey {
        CacheKey([n; 32])
    }

    #[test]
    fn benefit_grows_with_redundancy() {
        let cost = Cost {
            compute_secs: 1.0,
            size_bytes: 1000,
        };
        let b1 = benefit(cost, 1);
        let b10 = benefit(cost, 10);
        assert!(b10 > b1);
    }

    #[test]
    fn benefit_higher_for_expensive_small_entries() {
        let expensive_small = Cost {
            compute_secs: 10.0,
            size_bytes: 100,
        };
        let cheap_large = Cost {
            compute_secs: 0.1,
            size_bytes: 10_000,
        };
        assert!(benefit(expensive_small, 1) > benefit(cheap_large, 1));
    }

    #[test]
    fn cost_profile_running_mean() {
        let mut p = CostProfile::new();
        p.observe(
            k(1),
            Cost {
                compute_secs: 1.0,
                size_bytes: 100,
            },
        );
        p.observe(
            k(1),
            Cost {
                compute_secs: 3.0,
                size_bytes: 100,
            },
        );
        let c = p.get(&k(1)).unwrap();
        assert!((c.compute_secs - 2.0).abs() < 1e-9);
        assert_eq!(p.observation_count(&k(1)), 2);
    }

    #[test]
    fn pick_eviction_returns_nothing_under_budget() {
        let pop = vec![(k(1), BenefitScore::new(1.0), 500)];
        let evictions = pick_eviction(&pop, 500, 1000);
        assert!(evictions.is_empty());
    }

    #[test]
    fn pick_eviction_smallest_benefit_first() {
        let pop = vec![
            (k(1), BenefitScore::new(10.0), 400), // expensive — keep
            (k(2), BenefitScore::new(1.0), 400),  // cheap — evict first
            (k(3), BenefitScore::new(5.0), 400),  // medium
        ];
        let evictions = pick_eviction(&pop, 1200, 800);
        assert_eq!(evictions, vec![k(2)]);
    }

    #[test]
    fn pick_eviction_keeps_evicting_until_budget() {
        let pop = vec![
            (k(1), BenefitScore::new(10.0), 400),
            (k(2), BenefitScore::new(1.0), 400),
            (k(3), BenefitScore::new(2.0), 400),
        ];
        let evictions = pick_eviction(&pop, 1200, 400);
        assert_eq!(evictions.len(), 2);
        assert_eq!(evictions[0], k(2));
        assert_eq!(evictions[1], k(3));
    }
}
