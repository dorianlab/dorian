//! Experiment-store rust port — the supporting state behind
//! ``SimilarDataPerformance`` and ``PipelinePreferenceRatio``.
//!
//! Two pieces of in-memory state both objectives read at scoring time:
//!
//!   * **Dataset metafeature index**: ``(dataset_id, feature_vec)``
//!     pairs + min-max normalisation bounds. Lookups are k-nearest
//!     queries against the query dataset's metafeature vector.
//!   * **Win-rate cache**: ``pipeline_id → win_rate`` precomputed
//!     from the ``interactions`` table. Mirrors the python
//!     ``preload_win_rates`` aggregate.
//!
//! Pure-data design: this module owns the data structures and the
//! queries, **not** the DB load. The backend crate populates the
//! store from postgres at startup and shares the resulting
//! ``Arc<ExperimentStore>`` with handlers + objectives. Keeps the
//! optimizer crate free of postgres deps.
//!
//! KD-Tree note: the python uses sklearn's ``KDTree`` which beats
//! brute-force above ~1000 points × 30+ dims. At typical Dorian
//! scale (~100-1000 datasets, ~30 features) brute-force is one
//! 3000-flop pass — under 5 µs. We start brute-force; if the
//! dataset corpus grows past 5k we'll add a KD-tree (kiddo crate).

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

/// Stored evaluation: a candidate's per-dataset score.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoredEval {
    pub dataset_id: String,
    pub score: f64,
}

/// In-memory experiment store. Built once at backend lifespan
/// startup, shared via ``Arc<ExperimentStore>`` for the duration of
/// the process.
#[derive(Debug, Default)]
pub struct ExperimentStore {
    /// Parallel arrays — same length, same order.
    /// ``feature_dim = 0`` when no datasets have been loaded.
    pub dataset_ids: Vec<String>,
    pub raw_vecs: Vec<Vec<f64>>,
    /// Min-max normalisation bounds, computed across all stored
    /// vectors. ``None`` when fewer than two vectors are stored
    /// (no spread to normalise against).
    pub mins: Option<Vec<f64>>,
    pub maxs: Option<Vec<f64>>,
    /// ``pipeline_id → win_rate`` from preloaded interactions.
    pub win_rates: FxHashMap<String, f64>,
}

impl ExperimentStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Build from raw inputs. Computes normalisation bounds upfront
    /// so the per-query cost is one normalise + one scan.
    pub fn from_parts(
        datasets: Vec<(String, Vec<f64>)>,
        win_rates: FxHashMap<String, f64>,
    ) -> Self {
        let (dataset_ids, raw_vecs): (Vec<_>, Vec<_>) = datasets.into_iter().unzip();
        let (mins, maxs) = compute_bounds(&raw_vecs);
        Self {
            dataset_ids,
            raw_vecs,
            mins,
            maxs,
            win_rates,
        }
    }

    /// Number of dataset vectors loaded.
    pub fn size(&self) -> usize {
        self.dataset_ids.len()
    }

    pub fn is_empty(&self) -> bool {
        self.dataset_ids.is_empty()
    }

    /// k-nearest dataset IDs by L2 distance on the normalised
    /// vectors. Brute-force: ``O(N·D)`` per query plus a partial
    /// sort. Returns ``(dataset_id, distance)`` pairs sorted by
    /// ascending distance.
    pub fn k_nearest(&self, query: &[f64], k: usize) -> Vec<(String, f64)> {
        if self.is_empty() || k == 0 {
            return Vec::new();
        }
        let normalised_query = self.normalise(query);
        let mut distances: Vec<(usize, f64)> = self
            .raw_vecs
            .iter()
            .enumerate()
            .map(|(i, v)| {
                let d = self.distance_to(&normalised_query, v);
                (i, d)
            })
            .collect();
        // Partial sort — only the top-k matter, but for k close to N
        // a full sort is the same cost. Use full sort for simplicity;
        // selection-sort would beat it only when k << N.
        distances.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
        distances
            .into_iter()
            .take(k.min(self.size()))
            .map(|(i, d)| (self.dataset_ids[i].clone(), d))
            .collect()
    }

    /// Score a candidate by performance on similar datasets. Mirrors
    /// the python ``ExperimentStore.score_by_similar_datasets``:
    ///
    ///   1. Find ``k`` similar datasets to ``query_profile``.
    ///   2. For each of the candidate's evaluations:
    ///       * If on a similar dataset → weight = 1/(1+dist)
    ///       * Else → weight = 0.1 (low weight for unrelated data)
    ///   3. Return the weighted-average score.
    ///
    /// Returns ``0.0`` when the store is empty or the candidate has
    /// no scorable evaluations — same fail-graceful contract as
    /// the python.
    pub fn score_by_similar_datasets(
        &self,
        candidate_evals: &[StoredEval],
        query_profile: &[f64],
        k: usize,
    ) -> f64 {
        if self.is_empty() || candidate_evals.is_empty() {
            return 0.0;
        }
        let similar = self.k_nearest(query_profile, k);
        if similar.is_empty() {
            return 0.0;
        }
        let distance_map: FxHashMap<&str, f64> = similar
            .iter()
            .map(|(did, dist)| (did.as_str(), *dist))
            .collect();
        let mut weighted_sum = 0.0;
        let mut weight_total = 0.0;
        for ev in candidate_evals {
            if !ev.score.is_finite() {
                continue;
            }
            let w = match distance_map.get(ev.dataset_id.as_str()) {
                Some(dist) => 1.0 / (1.0 + dist),
                None => 0.1,
            };
            weighted_sum += ev.score * w;
            weight_total += w;
        }
        if weight_total > 0.0 {
            weighted_sum / weight_total
        } else {
            0.0
        }
    }

    /// Win rate for a pipeline, ``0.0`` for unknowns. Matches the
    /// python ``get_win_rate_sync`` contract.
    pub fn win_rate(&self, pipeline_id: &str) -> f64 {
        self.win_rates.get(pipeline_id).copied().unwrap_or(0.0)
    }

    /// Top-``k`` pipeline IDs by win rate, descending. Used by the
    /// recommendation orchestrator's primary-objective-anchored
    /// retrieval path: when ``PipelinePreferenceRatio`` is the user's
    /// top objective, the candidate pool is the top-K most-preferred
    /// pipelines instead of a random sample. Excludes IDs in
    /// ``exclude``.
    pub fn top_pipelines_by_win_rate(&self, k: usize, exclude: &[String]) -> Vec<String> {
        if k == 0 || self.win_rates.is_empty() {
            return Vec::new();
        }
        let exclude_set: rustc_hash::FxHashSet<&str> =
            exclude.iter().map(|s| s.as_str()).collect();
        let mut pairs: Vec<(&str, f64)> = self
            .win_rates
            .iter()
            .filter(|(id, _)| !exclude_set.contains(id.as_str()))
            .map(|(id, rate)| (id.as_str(), *rate))
            .collect();
        // Partial sort top-k. The win-rate cache typically has 1k-10k
        // entries; full sort is fine at that scale (< 1 ms). At larger
        // scale a partial-quickselect would beat it.
        pairs.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        pairs
            .into_iter()
            .take(k)
            .map(|(id, _)| id.to_string())
            .collect()
    }

    fn normalise(&self, raw: &[f64]) -> Vec<f64> {
        let (Some(mins), Some(maxs)) = (self.mins.as_ref(), self.maxs.as_ref()) else {
            return raw.to_vec();
        };
        let dim = mins.len();
        if raw.len() != dim {
            // Mismatched dimension — pad/truncate to the store's
            // expected width and treat missing components as the
            // midpoint (0.5 after normalisation).
            return (0..dim)
                .map(|i| {
                    let v = raw.get(i).copied().unwrap_or(f64::NAN);
                    normalise_one(v, mins[i], maxs[i])
                })
                .collect();
        }
        raw.iter()
            .zip(mins.iter().zip(maxs.iter()))
            .map(|(v, (lo, hi))| normalise_one(*v, *lo, *hi))
            .collect()
    }

    fn distance_to(&self, normalised_query: &[f64], raw: &[f64]) -> f64 {
        let (Some(mins), Some(maxs)) = (self.mins.as_ref(), self.maxs.as_ref()) else {
            return l2(normalised_query, raw);
        };
        // Normalise the stored vector on the fly — keeps the struct
        // small (we don't hold a second copy of every vector). 30
        // floats per call is negligible vs the postgres load that
        // built the store.
        let mut sum = 0.0;
        for i in 0..normalised_query.len() {
            let q = normalised_query[i];
            let s = normalise_one(
                *raw.get(i).unwrap_or(&f64::NAN),
                mins[i],
                maxs[i],
            );
            let d = q - s;
            sum += d * d;
        }
        sum.sqrt()
    }
}

fn compute_bounds(vecs: &[Vec<f64>]) -> (Option<Vec<f64>>, Option<Vec<f64>>) {
    if vecs.is_empty() {
        return (None, None);
    }
    let dim = vecs[0].len();
    if dim == 0 || vecs.len() < 2 {
        return (None, None);
    }
    let mut mins = vec![f64::INFINITY; dim];
    let mut maxs = vec![f64::NEG_INFINITY; dim];
    for v in vecs {
        for (i, x) in v.iter().enumerate().take(dim) {
            if !x.is_finite() {
                continue;
            }
            if *x < mins[i] {
                mins[i] = *x;
            }
            if *x > maxs[i] {
                maxs[i] = *x;
            }
        }
    }
    (Some(mins), Some(maxs))
}

fn normalise_one(v: f64, lo: f64, hi: f64) -> f64 {
    if !v.is_finite() {
        return 0.5; // python normalises NaN → 0.0; using 0.5 (midpoint)
                    // is more robust under partial profiles where missing
                    // features otherwise pull every distance toward zero.
    }
    let span = hi - lo;
    if span.abs() < 1e-12 {
        0.0
    } else {
        ((v - lo) / span).clamp(0.0, 1.0)
    }
}

fn l2(a: &[f64], b: &[f64]) -> f64 {
    let mut sum = 0.0;
    for i in 0..a.len().min(b.len()) {
        let d = a[i] - b[i];
        sum += d * d;
    }
    sum.sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_store() -> ExperimentStore {
        ExperimentStore::from_parts(
            vec![
                ("d1".into(), vec![10.0, 100.0]),
                ("d2".into(), vec![20.0, 200.0]),
                ("d3".into(), vec![30.0, 300.0]),
            ],
            FxHashMap::from_iter([
                ("p_high".to_string(), 0.8),
                ("p_low".to_string(), 0.2),
            ]),
        )
    }

    #[test]
    fn k_nearest_returns_closest_first() {
        let s = sample_store();
        let near = s.k_nearest(&[12.0, 110.0], 2);
        assert_eq!(near.len(), 2);
        assert_eq!(near[0].0, "d1");
        // Distance is 0 because we're querying ON d1's spot — both
        // dimensions normalise to similar fractions.
        assert!(near[0].1 < near[1].1);
    }

    #[test]
    fn win_rate_lookup() {
        let s = sample_store();
        assert!((s.win_rate("p_high") - 0.8).abs() < 1e-9);
        assert!((s.win_rate("p_low") - 0.2).abs() < 1e-9);
        assert_eq!(s.win_rate("unknown"), 0.0);
    }

    #[test]
    fn score_by_similar_weights_close_evals_more() {
        let s = sample_store();
        let evals = vec![
            StoredEval { dataset_id: "d1".into(), score: 0.9 },
            StoredEval { dataset_id: "d_unrelated".into(), score: 0.1 },
        ];
        let score = s.score_by_similar_datasets(&evals, &[10.0, 100.0], 2);
        // d1 should win heavy weight (we queried at its location,
        // distance is small or 0); d_unrelated gets the 0.1 baseline.
        // → score should be much closer to 0.9 than to 0.5.
        assert!(score > 0.5, "expected score > 0.5, got {score}");
    }

    #[test]
    fn score_with_empty_evals_returns_zero() {
        let s = sample_store();
        assert_eq!(s.score_by_similar_datasets(&[], &[10.0, 100.0], 5), 0.0);
    }

    #[test]
    fn empty_store_returns_zero() {
        let s = ExperimentStore::new();
        assert_eq!(s.size(), 0);
        let evals = vec![StoredEval {
            dataset_id: "d1".into(),
            score: 0.9,
        }];
        assert_eq!(s.score_by_similar_datasets(&evals, &[10.0], 5), 0.0);
    }

    #[test]
    fn top_pipelines_orders_by_win_rate_desc() {
        let s = sample_store();
        let top = s.top_pipelines_by_win_rate(2, &[]);
        assert_eq!(top, vec!["p_high".to_string(), "p_low".to_string()]);
    }

    #[test]
    fn top_pipelines_excludes_listed_ids() {
        let s = sample_store();
        let top = s.top_pipelines_by_win_rate(5, &["p_high".to_string()]);
        assert_eq!(top, vec!["p_low".to_string()]);
    }

    #[test]
    fn top_pipelines_zero_k_or_empty_cache_returns_empty() {
        let s = sample_store();
        assert_eq!(s.top_pipelines_by_win_rate(0, &[]), Vec::<String>::new());
        let empty = ExperimentStore::new();
        assert_eq!(
            empty.top_pipelines_by_win_rate(10, &[]),
            Vec::<String>::new()
        );
    }
}
