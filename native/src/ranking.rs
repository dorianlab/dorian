//! Ranking hot paths: Generalized Jensen–Shannon divergence and
//! non-dominated sorting (Pareto fronts).

/// Compute Generalized Jensen–Shannon divergence fitness for each candidate.
///
/// Each candidate's objective score vector is compared to the ideal point
/// (maximum per objective across all candidates). Returns a fitness value
/// per candidate (higher = better).
///
/// `scores` is a flat row-major matrix: `scores[i * n_obj + j]` = candidate i, objective j.
pub fn jensen_divergence_fitness(scores: &[f64], n_candidates: usize, n_obj: usize, alpha: f64) -> Vec<f64> {
    if n_candidates == 0 || n_obj == 0 {
        return vec![0.0; n_candidates];
    }

    let eps = 1e-12;

    // Ideal point: best score per objective
    let mut ideal = vec![f64::NEG_INFINITY; n_obj];
    for i in 0..n_candidates {
        for j in 0..n_obj {
            let v = scores[i * n_obj + j];
            if v > ideal[j] {
                ideal[j] = v;
            }
        }
    }

    let ideal_sum: f64 = ideal.iter().sum();
    if ideal_sum < eps {
        return vec![0.0; n_candidates];
    }

    // Normalize ideal to probability distribution
    let q: Vec<f64> = ideal.iter().map(|v| v / ideal_sum).collect();
    let h_q = shannon_entropy(&q, eps);

    let max_gjd = -alpha * (alpha + eps).ln() - (1.0 - alpha) * (1.0 - alpha + eps).ln();

    let mut fitness = Vec::with_capacity(n_candidates);
    for i in 0..n_candidates {
        let row = &scores[i * n_obj..(i + 1) * n_obj];
        let row_sum: f64 = row.iter().sum();

        if row_sum < eps {
            fitness.push(0.0);
            continue;
        }

        let p: Vec<f64> = row.iter().map(|v| v / row_sum).collect();
        let h_p = shannon_entropy(&p, eps);

        let mixture: Vec<f64> = (0..n_obj)
            .map(|j| alpha * p[j] + (1.0 - alpha) * q[j])
            .collect();
        let h_mix = shannon_entropy(&mixture, eps);

        let gjd = h_mix - alpha * h_p - (1.0 - alpha) * h_q;
        let normalized = if max_gjd > eps { gjd / max_gjd } else { 0.0 };
        fitness.push(1.0 - normalized);
    }

    fitness
}

fn shannon_entropy(dist: &[f64], eps: f64) -> f64 {
    -dist.iter().map(|&p| p * (p + eps).ln()).sum::<f64>()
}

/// Non-dominated sorting: assign each candidate to a Pareto front.
///
/// Returns a Vec<usize> of front indices (0 = Pareto-optimal, lower = better).
/// `scores` is row-major: `scores[i * n_obj + j]`.
///
/// Uses the "fast non-dominated sorting" algorithm from NSGA-II (Deb et al. 2002).
pub fn non_dominated_sort(scores: &[f64], n_candidates: usize, n_obj: usize) -> Vec<usize> {
    if n_candidates == 0 {
        return Vec::new();
    }

    // domination_count[i] = how many solutions dominate i
    let mut domination_count = vec![0usize; n_candidates];
    // dominated_by[i] = list of solutions that i dominates
    let mut dominated_by: Vec<Vec<usize>> = vec![Vec::new(); n_candidates];

    for i in 0..n_candidates {
        for j in (i + 1)..n_candidates {
            match dominance(scores, i, j, n_obj) {
                Dominance::Left => {
                    // i dominates j
                    dominated_by[i].push(j);
                    domination_count[j] += 1;
                }
                Dominance::Right => {
                    // j dominates i
                    dominated_by[j].push(i);
                    domination_count[i] += 1;
                }
                Dominance::Neither => {}
            }
        }
    }

    let mut front_indices = vec![0usize; n_candidates];
    let mut current_front: Vec<usize> = Vec::new();

    // Front 0: all solutions with domination_count == 0
    for i in 0..n_candidates {
        if domination_count[i] == 0 {
            front_indices[i] = 0;
            current_front.push(i);
        }
    }

    let mut front_num = 0;
    while !current_front.is_empty() {
        let mut next_front = Vec::new();
        for &i in &current_front {
            for &j in &dominated_by[i] {
                domination_count[j] -= 1;
                if domination_count[j] == 0 {
                    front_num += 1; // will be set below
                    next_front.push(j);
                }
            }
        }
        let next_num = front_num.min(current_front.len()); // safety
        for &j in &next_front {
            front_indices[j] = next_num;
        }
        current_front = next_front;
    }

    // Re-assign front numbers sequentially
    let mut actual_front = 1usize;
    let mut assigned: Vec<bool> = vec![false; n_candidates];
    let mut result = vec![0usize; n_candidates];

    // front 0 already correct
    for i in 0..n_candidates {
        if front_indices[i] == 0 {
            result[i] = 0;
            assigned[i] = true;
        }
    }

    // BFS-style front assignment
    let mut current: Vec<usize> = (0..n_candidates).filter(|&i| result[i] == 0 && assigned[i]).collect();
    loop {
        let mut next: Vec<usize> = Vec::new();
        for &i in &current {
            for &j in &dominated_by[i] {
                if !assigned[j] {
                    result[j] = actual_front;
                    assigned[j] = true;
                    next.push(j);
                }
            }
        }
        if next.is_empty() {
            break;
        }
        actual_front += 1;
        current = next;
    }

    result
}

enum Dominance {
    Left,   // i dominates j
    Right,  // j dominates i
    Neither,
}

/// Check if candidate i dominates j (all objectives ≥ and at least one >).
fn dominance(scores: &[f64], i: usize, j: usize, n_obj: usize) -> Dominance {
    let mut i_better = false;
    let mut j_better = false;

    for k in 0..n_obj {
        let si = scores[i * n_obj + k];
        let sj = scores[j * n_obj + k];
        if si > sj {
            i_better = true;
        } else if sj > si {
            j_better = true;
        }
        if i_better && j_better {
            return Dominance::Neither;
        }
    }

    if i_better && !j_better {
        Dominance::Left
    } else if j_better && !i_better {
        Dominance::Right
    } else {
        Dominance::Neither
    }
}

/// Produce a ranking permutation given a scores matrix and a strategy.
///
/// Returns indices into the candidates array, sorted best-first.
/// Strategy: "jensen", "nds", or "weighted_sum".
pub fn rank(
    scores: &[f64],
    n_candidates: usize,
    n_obj: usize,
    strategy: &str,
) -> Vec<usize> {
    if n_candidates == 0 {
        return Vec::new();
    }

    match strategy {
        "jensen" => {
            let fitness = jensen_divergence_fitness(scores, n_candidates, n_obj, 0.5);
            let mut indices: Vec<usize> = (0..n_candidates).collect();
            indices.sort_by(|&a, &b| {
                fitness[b]
                    .partial_cmp(&fitness[a])
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
            indices
        }
        "nds" => {
            let fronts = non_dominated_sort(scores, n_candidates, n_obj);
            let sums: Vec<f64> = (0..n_candidates)
                .map(|i| scores[i * n_obj..(i + 1) * n_obj].iter().sum())
                .collect();
            let mut indices: Vec<usize> = (0..n_candidates).collect();
            indices.sort_by(|&a, &b| {
                fronts[a]
                    .cmp(&fronts[b])
                    .then_with(|| {
                        sums[b]
                            .partial_cmp(&sums[a])
                            .unwrap_or(std::cmp::Ordering::Equal)
                    })
            });
            indices
        }
        _ => {
            // weighted_sum fallback
            let sums: Vec<f64> = (0..n_candidates)
                .map(|i| scores[i * n_obj..(i + 1) * n_obj].iter().sum())
                .collect();
            let mut indices: Vec<usize> = (0..n_candidates).collect();
            indices.sort_by(|&a, &b| {
                sums[b]
                    .partial_cmp(&sums[a])
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
            indices
        }
    }
}
