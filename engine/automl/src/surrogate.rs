//! Random forest surrogate for SMAC's `mu` / `sigma` predictions.
//!
//! Hand-rolled to keep the AutoML crate's dep footprint minimal —
//! pulling in `linfa-trees` brings ndarray + matrix ops, and we
//! only need a regression forest with leaf-mean predictions and
//! per-tree variance.
//!
//! Algorithm:
//!
//!   * Each tree is fitted on a bootstrap sample of `(X, y)`.
//!   * At each split, `mtry` features are sampled at random and
//!     the one with lowest weighted-MSE is selected. Threshold
//!     is the midpoint between adjacent sorted values.
//!   * Stop when node size ≤ `min_leaf` or all targets are equal.
//!   * Prediction: average of per-tree leaf means; sigma = stdev
//!     of those tree means. SMAC's EI uses both.
//!
//! This is intentionally a v1: a useful surrogate for early BO
//! steps. Future work may swap in a richer regressor (Gaussian
//! process via sparse approximation, neural surrogate via burn,
//! etc.) — the trait surface stays the same so the BO driver
//! doesn't change.

use rand::seq::SliceRandom;
use rand::{Rng, SeedableRng};
use rand::rngs::StdRng;

#[derive(Debug)]
struct TreeNode {
    /// `Some(feature_idx, threshold, left, right)` for internal
    /// nodes; `None` (leaf) carries `prediction` instead.
    split: Option<(usize, f64, Box<TreeNode>, Box<TreeNode>)>,
    prediction: f64,
}

#[derive(Debug)]
struct Tree {
    root: TreeNode,
}

impl Tree {
    fn fit(
        x: &[Vec<f64>], y: &[f64], indices: &[usize],
        rng: &mut StdRng, mtry: usize, min_leaf: usize, max_depth: usize,
    ) -> Self {
        let root = build_node(x, y, indices, rng, mtry, min_leaf, max_depth);
        Tree { root }
    }
    fn predict(&self, x: &[f64]) -> f64 {
        let mut node = &self.root;
        loop {
            match &node.split {
                Some((feat, thresh, left, right)) => {
                    if x.get(*feat).copied().unwrap_or(0.0) <= *thresh {
                        node = left;
                    } else {
                        node = right;
                    }
                }
                None => return node.prediction,
            }
        }
    }
}

fn build_node(
    x: &[Vec<f64>], y: &[f64], indices: &[usize],
    rng: &mut StdRng, mtry: usize, min_leaf: usize, depth: usize,
) -> TreeNode {
    let leaf = || TreeNode {
        split: None,
        prediction: mean(indices.iter().map(|&i| y[i])),
    };
    if indices.len() <= min_leaf || depth == 0 {
        return leaf();
    }
    // All-equal y → no split improves.
    let first_y = y[indices[0]];
    if indices.iter().all(|&i| y[i] == first_y) {
        return leaf();
    }
    let n_features = x.first().map(|v| v.len()).unwrap_or(0);
    if n_features == 0 {
        return leaf();
    }
    // Sample `mtry` features without replacement.
    let mut feature_pool: Vec<usize> = (0..n_features).collect();
    feature_pool.shuffle(rng);
    let try_features = &feature_pool[..mtry.min(n_features)];

    let mut best: Option<(usize, f64, f64)> = None; // (feat, thresh, mse)
    let parent_var = variance(indices.iter().map(|&i| y[i]));
    for &f in try_features {
        let mut vals: Vec<(f64, f64)> = indices
            .iter()
            .map(|&i| (x[i][f], y[i]))
            .collect();
        vals.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
        for w in 0..vals.len().saturating_sub(1) {
            let a = vals[w].0;
            let b = vals[w + 1].0;
            if a == b {
                continue;
            }
            let thresh = (a + b) / 2.0;
            let (left_y, right_y): (Vec<&(f64, f64)>, Vec<&(f64, f64)>) =
                vals.iter().partition(|p| p.0 <= thresh);
            if left_y.is_empty() || right_y.is_empty() {
                continue;
            }
            let left_var = variance(left_y.iter().map(|p| p.1));
            let right_var = variance(right_y.iter().map(|p| p.1));
            let weighted = (left_y.len() as f64 * left_var
                + right_y.len() as f64 * right_var)
                / (left_y.len() + right_y.len()) as f64;
            if weighted < parent_var
                && best.map_or(true, |(_, _, m)| weighted < m)
            {
                best = Some((f, thresh, weighted));
            }
        }
    }
    match best {
        Some((feat, thresh, _)) => {
            let mut left_idx = Vec::new();
            let mut right_idx = Vec::new();
            for &i in indices {
                if x[i][feat] <= thresh {
                    left_idx.push(i);
                } else {
                    right_idx.push(i);
                }
            }
            let left = build_node(x, y, &left_idx, rng, mtry, min_leaf, depth - 1);
            let right = build_node(x, y, &right_idx, rng, mtry, min_leaf, depth - 1);
            TreeNode {
                split: Some((feat, thresh, Box::new(left), Box::new(right))),
                prediction: mean(indices.iter().map(|&i| y[i])),
            }
        }
        None => leaf(),
    }
}

fn mean<I: Iterator<Item = f64>>(it: I) -> f64 {
    let mut sum = 0.0;
    let mut n = 0.0;
    for v in it {
        sum += v;
        n += 1.0;
    }
    if n == 0.0 { 0.0 } else { sum / n }
}

fn variance<I: Iterator<Item = f64>>(it: I) -> f64 {
    let vs: Vec<f64> = it.collect();
    if vs.is_empty() { return 0.0; }
    let m = vs.iter().sum::<f64>() / vs.len() as f64;
    vs.iter().map(|v| (v - m).powi(2)).sum::<f64>() / vs.len() as f64
}

/// Random forest surrogate.
pub struct RandomForest {
    trees: Vec<Tree>,
    pub n_features: usize,
}

#[derive(Debug, Clone, Copy)]
pub struct ForestConfig {
    pub n_trees: usize,
    pub min_leaf: usize,
    pub max_depth: usize,
    pub mtry: Option<usize>, // default: sqrt(n_features)
    pub seed: u64,
}

impl Default for ForestConfig {
    fn default() -> Self {
        Self {
            n_trees: 32,
            min_leaf: 3,
            max_depth: 16,
            mtry: None,
            seed: 0x5A4AC,
        }
    }
}

impl RandomForest {
    pub fn fit(x: &[Vec<f64>], y: &[f64], cfg: ForestConfig) -> Self {
        let n_features = x.first().map(|r| r.len()).unwrap_or(0);
        let mtry = cfg.mtry.unwrap_or_else(|| {
            ((n_features as f64).sqrt().ceil() as usize).max(1)
        });
        let mut rng = StdRng::seed_from_u64(cfg.seed);
        let n = x.len();
        let mut trees = Vec::with_capacity(cfg.n_trees);
        for _ in 0..cfg.n_trees {
            // Bootstrap sample (sample-with-replacement).
            let indices: Vec<usize> =
                (0..n).map(|_| rng.gen_range(0..n.max(1))).collect();
            trees.push(Tree::fit(
                x, y, &indices, &mut rng, mtry, cfg.min_leaf, cfg.max_depth,
            ));
        }
        Self { trees, n_features }
    }

    /// Return `(mu, sigma)` for one query vector. Sigma is the
    /// stdev across per-tree predictions — the proxy SMAC uses
    /// for surrogate uncertainty.
    pub fn predict(&self, x: &[f64]) -> (f64, f64) {
        if self.trees.is_empty() {
            return (0.0, 1.0); // High uncertainty — fall back to random.
        }
        let preds: Vec<f64> = self.trees.iter().map(|t| t.predict(x)).collect();
        let mu = preds.iter().sum::<f64>() / preds.len() as f64;
        let var = preds.iter().map(|p| (p - mu).powi(2)).sum::<f64>()
            / preds.len() as f64;
        (mu, var.sqrt())
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_forest_predicts_constant_with_high_variance() {
        let f = RandomForest { trees: vec![], n_features: 2 };
        let (mu, sigma) = f.predict(&[0.5, 0.5]);
        assert_eq!(mu, 0.0);
        assert!(sigma >= 1.0);
    }

    #[test]
    fn fits_simple_linear_relationship() {
        // y = x[0] + 2 * x[1] + small noise
        let x: Vec<Vec<f64>> = (0..200)
            .map(|i| {
                let a = (i as f64) / 200.0;
                let b = ((i * 7) % 200) as f64 / 200.0;
                vec![a, b]
            })
            .collect();
        let y: Vec<f64> = x.iter().map(|r| r[0] + 2.0 * r[1]).collect();
        let f = RandomForest::fit(
            &x, &y,
            ForestConfig { n_trees: 16, min_leaf: 2, max_depth: 12, mtry: Some(2), seed: 1 },
        );
        let test_input = vec![0.5, 0.5];
        let (mu, _) = f.predict(&test_input);
        // y_true = 0.5 + 1.0 = 1.5
        assert!((mu - 1.5).abs() < 0.3, "mu={mu}, expected ~1.5");
    }

    #[test]
    fn sigma_grows_in_extrapolation_region() {
        // Train on small range, predict outside it. The spread
        // across trees should be wider in the extrapolation region.
        let x: Vec<Vec<f64>> = (0..50)
            .map(|i| vec![(i as f64) / 50.0, 0.0])
            .collect();
        let y: Vec<f64> = x.iter().map(|r| r[0]).collect();
        let f = RandomForest::fit(
            &x, &y,
            ForestConfig { n_trees: 16, min_leaf: 1, max_depth: 8, mtry: Some(1), seed: 2 },
        );
        let (_, sigma_in) = f.predict(&[0.5, 0.0]);
        let (_, sigma_out) = f.predict(&[2.0, 0.0]);
        // Trees are constant outside their training range — variance
        // across trees there reflects which leaves they fall into,
        // typically higher than mid-training-range predictions.
        // Loose check: sigma_out >= sigma_in.
        assert!(sigma_out >= sigma_in, "sigma_in={sigma_in}, sigma_out={sigma_out}");
    }
}
