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
/// Returns a ``Vec<usize>`` of front indices (``0`` = Pareto-optimal,
/// lower = better). ``scores`` is row-major: ``scores[i * n_obj + j]``,
/// higher is better on every objective.
///
/// **Algorithm: ENS-SS** (Efficient Non-dominated Sort via Sequential
/// Search; Zhang et al., IEEE TEVC 2015). Same ``O(M·N²)`` worst case
/// as NSGA-II's "fast non-dominated sorting" (Deb et al. 2002) but
/// typically 3–10× faster on real data because:
///
///   * Pre-sort candidates by their first objective (descending).
///     Anything appearing later in the sorted order can only be
///     dominated by something earlier — half the dominance checks
///     don't have to run.
///   * For each candidate, walk the existing fronts in order; the
///     first front whose members don't dominate the candidate is its
///     front. Most candidates land in front 0–2 in practice, so the
///     inner loop short-circuits early.
///   * No ``domination_count`` / ``dominated_by`` adjacency lists —
///     each candidate visits ``O(F · k)`` already-placed candidates
///     where ``F`` is the front count and ``k`` is the average front
///     size, vs the NSGA-II ``O(N²)`` pre-pass that builds the full
///     adjacency.
///
/// Stable: candidates with identical scores land on the same front
/// in their original input order.
pub fn non_dominated_sort(scores: &[f64], n_candidates: usize, n_obj: usize) -> Vec<usize> {
    if n_candidates == 0 {
        return Vec::new();
    }
    if n_obj == 0 {
        // Degenerate: no objectives → everyone tied at front 0.
        return vec![0usize; n_candidates];
    }

    // 1. Pre-sort indices by the first objective (descending).
    //    On ties, fall back to lexicographic ordering on the rest of
    //    the row so duplicates stay clustered — gives ENS-SS its
    //    short-circuit advantage on populations with many duplicates.
    let mut order: Vec<usize> = (0..n_candidates).collect();
    order.sort_by(|&a, &b| {
        let row_a = &scores[a * n_obj..(a + 1) * n_obj];
        let row_b = &scores[b * n_obj..(b + 1) * n_obj];
        for k in 0..n_obj {
            match row_b[k].partial_cmp(&row_a[k]).unwrap_or(std::cmp::Ordering::Equal) {
                std::cmp::Ordering::Equal => continue,
                ord => return ord,
            }
        }
        std::cmp::Ordering::Equal
    });

    // 2. Sequential-search front assignment.
    //    ``fronts[f]`` holds the indices of candidates already placed on
    //    front ``f``. Each new candidate scans fronts in order; the
    //    first front where no member dominates the candidate is its
    //    front. Vec<Vec<>> wins over a flat structure here because
    //    fronts are usually small (<100 members each).
    let mut fronts: Vec<Vec<usize>> = Vec::new();
    let mut result = vec![0usize; n_candidates];

    for &cand in &order {
        // Try existing fronts in ascending order.
        let mut placed = false;
        for (f_idx, front) in fronts.iter_mut().enumerate() {
            // ENS-SS: scan members in REVERSE — the most recently added
            // member tends to be the closest "neighbour" in objective
            // space (because we pre-sorted), so a dominator usually
            // shows up at the back of the front. Cheap heuristic; saves
            // a constant factor on the inner loop.
            let dominated = front.iter().rev().any(|&placed_idx| {
                matches!(
                    dominance(scores, placed_idx, cand, n_obj),
                    Dominance::Left
                )
            });
            if !dominated {
                front.push(cand);
                result[cand] = f_idx;
                placed = true;
                break;
            }
        }
        if !placed {
            // Beyond every existing front — start a new one.
            result[cand] = fronts.len();
            fronts.push(vec![cand]);
        }
    }

    result
}

enum Dominance {
    Left,   // i dominates j
    Right,  // j dominates i
    Neither,
}

/// Lexicographic comparison on score rows (higher first). Used by
/// the ``"lexicographic"`` strategy and the ``"nds_lex"`` tie-break.
fn lex_cmp(scores: &[f64], a: usize, b: usize, n_obj: usize) -> std::cmp::Ordering {
    let row_a = &scores[a * n_obj..(a + 1) * n_obj];
    let row_b = &scores[b * n_obj..(b + 1) * n_obj];
    for k in 0..n_obj {
        match row_b[k]
            .partial_cmp(&row_a[k])
            .unwrap_or(std::cmp::Ordering::Equal)
        {
            std::cmp::Ordering::Equal => continue,
            ord => return ord,
        }
    }
    std::cmp::Ordering::Equal
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
/// Strategies:
///
///   * ``"lexicographic"`` — sort by ``obj[0]`` desc; ties broken by
///     ``obj[1]`` desc; ties broken by ``obj[2]`` desc; … Honours the
///     user's curated objective order strictly: the top objective in
///     the sidebar is the primary key, the next is the tie-breaker,
///     etc. Use this whenever the UX promises "drag to set priority".
///   * ``"jensen"`` — Generalised Jensen-Shannon divergence fitness;
///     order-symmetric (rearranging columns gives the same result).
///   * ``"nds"`` — Pareto front via ENS-SS; tie-broken by score sum.
///     Order-symmetric.
///   * anything else — weighted sum (every objective equal). Order-
///     symmetric.
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
        "lexicographic" => {
            let mut indices: Vec<usize> = (0..n_candidates).collect();
            indices.sort_by(|&a, &b| lex_cmp(scores, a, b, n_obj));
            indices
        }
        "nds_lex" => {
            // Pareto front (semantic correctness on trade-offs)
            // tied-broken by user-curated order (so the sidebar's
            // priority promise actually controls within-front order).
            // The right default for most interactive UX.
            let fronts = non_dominated_sort(scores, n_candidates, n_obj);
            let mut indices: Vec<usize> = (0..n_candidates).collect();
            indices.sort_by(|&a, &b| {
                fronts[a]
                    .cmp(&fronts[b])
                    .then_with(|| lex_cmp(scores, a, b, n_obj))
            });
            indices
        }
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

#[cfg(test)]
mod tests {
    use super::*;

    const EPS: f64 = 1e-9;

    // ========== Jensen divergence fitness ==========

    #[test]
    fn jensen_identical_distributions_returns_max_fitness() {
        // Two candidates with identical scores → both should get high (equal) fitness
        let scores = vec![0.5, 0.5, 0.5, 0.5];
        let fitness = jensen_divergence_fitness(&scores, 2, 2, 0.5);
        assert_eq!(fitness.len(), 2);
        // Identical to ideal → divergence is 0 → fitness is 1.0
        assert!((fitness[0] - 1.0).abs() < EPS);
        assert!((fitness[1] - 1.0).abs() < EPS);
    }

    #[test]
    fn jensen_different_distributions() {
        // Candidate 0 is ideal (high in both), candidate 1 is skewed
        let scores = vec![
            1.0, 1.0, // candidate 0: balanced
            1.0, 0.0, // candidate 1: skewed
        ];
        let fitness = jensen_divergence_fitness(&scores, 2, 2, 0.5);
        assert_eq!(fitness.len(), 2);
        // candidate 0 matches ideal better → higher fitness
        assert!(fitness[0] > fitness[1]);
    }

    #[test]
    fn jensen_uniform_distribution() {
        // All candidates have uniform scores across objectives
        let n = 4;
        let n_obj = 3;
        let scores: Vec<f64> = vec![1.0; n * n_obj];
        let fitness = jensen_divergence_fitness(&scores, n, n_obj, 0.5);
        // All identical → all get same fitness
        for i in 1..n {
            assert!((fitness[i] - fitness[0]).abs() < EPS);
        }
    }

    #[test]
    fn jensen_empty_candidates() {
        let fitness = jensen_divergence_fitness(&[], 0, 3, 0.5);
        assert!(fitness.is_empty());
    }

    #[test]
    fn jensen_zero_objectives() {
        let fitness = jensen_divergence_fitness(&[], 2, 0, 0.5);
        assert_eq!(fitness.len(), 2);
        assert!((fitness[0]).abs() < EPS);
    }

    #[test]
    fn jensen_all_zero_scores() {
        let scores = vec![0.0, 0.0, 0.0, 0.0];
        let fitness = jensen_divergence_fitness(&scores, 2, 2, 0.5);
        // ideal_sum < eps → returns all zeros
        assert_eq!(fitness.len(), 2);
        assert!((fitness[0]).abs() < EPS);
        assert!((fitness[1]).abs() < EPS);
    }

    #[test]
    fn jensen_single_candidate() {
        let scores = vec![3.0, 7.0];
        let fitness = jensen_divergence_fitness(&scores, 1, 2, 0.5);
        assert_eq!(fitness.len(), 1);
        // Only candidate IS the ideal → divergence = 0 → fitness = 1.0
        assert!((fitness[0] - 1.0).abs() < EPS);
    }

    #[test]
    fn jensen_single_objective() {
        // With 1 objective, all distributions are trivially [1.0] → divergence = 0
        let scores = vec![5.0, 3.0, 1.0];
        let fitness = jensen_divergence_fitness(&scores, 3, 1, 0.5);
        assert_eq!(fitness.len(), 3);
        // candidate 0 is the ideal (highest) so divergence from ideal should be 0
        // But all normalize to [1.0] so all should have fitness 1.0 (except zeros)
        assert!((fitness[0] - 1.0).abs() < EPS);
    }

    // ========== Non-dominated sorting ==========

    #[test]
    fn nds_empty() {
        let result = non_dominated_sort(&[], 0, 2);
        assert!(result.is_empty());
    }

    #[test]
    fn nds_single_candidate() {
        let scores = vec![1.0, 2.0];
        let result = non_dominated_sort(&scores, 1, 2);
        assert_eq!(result, vec![0]); // front 0
    }

    #[test]
    fn nds_two_nondominated() {
        // Neither dominates the other: (1,0) vs (0,1)
        let scores = vec![1.0, 0.0, 0.0, 1.0];
        let result = non_dominated_sort(&scores, 2, 2);
        // Both should be on front 0
        assert_eq!(result[0], 0);
        assert_eq!(result[1], 0);
    }

    #[test]
    fn nds_one_dominates_other() {
        // Candidate 0 dominates candidate 1: (2,2) > (1,1)
        let scores = vec![2.0, 2.0, 1.0, 1.0];
        let result = non_dominated_sort(&scores, 2, 2);
        assert_eq!(result[0], 0); // front 0
        assert_eq!(result[1], 1); // front 1
    }

    #[test]
    fn nds_all_equal_scores() {
        // All candidates are equal → nobody dominates → all front 0
        let scores = vec![1.0, 1.0, 1.0, 1.0, 1.0, 1.0];
        let result = non_dominated_sort(&scores, 3, 2);
        assert_eq!(result, vec![0, 0, 0]);
    }

    #[test]
    fn nds_three_candidates_chain() {
        // A > B > C. Correct Pareto fronts: A=0, B=1, C=2 (B is
        // dominated by A; C is dominated by B even after A is
        // removed, so C must be on a strictly later front than B).
        // The previous NSGA-II implementation collapsed B and C onto
        // front 1 due to a BFS bug — that test was matching the bug.
        let scores = vec![
            3.0, 3.0, // A
            2.0, 2.0, // B
            1.0, 1.0, // C
        ];
        let result = non_dominated_sort(&scores, 3, 2);
        assert_eq!(result[0], 0);
        assert_eq!(result[1], 1);
        assert_eq!(result[2], 2);
    }

    #[test]
    fn nds_three_objectives() {
        let scores = vec![
            3.0, 1.0, 2.0, // candidate 0
            1.0, 3.0, 2.0, // candidate 1 — non-dominated w.r.t. 0
            1.0, 1.0, 1.0, // candidate 2 — dominated by both
        ];
        let result = non_dominated_sort(&scores, 3, 3);
        assert_eq!(result[0], 0); // front 0
        assert_eq!(result[1], 0); // front 0
        assert_eq!(result[2], 1); // front 1
    }

    // ========== Rank dispatch ==========

    #[test]
    fn rank_empty() {
        assert!(rank(&[], 0, 2, "jensen").is_empty());
        assert!(rank(&[], 0, 2, "nds").is_empty());
        assert!(rank(&[], 0, 2, "weighted_sum").is_empty());
    }

    #[test]
    fn rank_single_candidate() {
        let scores = vec![1.0, 2.0];
        assert_eq!(rank(&scores, 1, 2, "jensen"), vec![0]);
        assert_eq!(rank(&scores, 1, 2, "nds"), vec![0]);
        assert_eq!(rank(&scores, 1, 2, "weighted_sum"), vec![0]);
    }

    #[test]
    fn rank_jensen_dispatches_correctly() {
        let scores = vec![
            2.0, 2.0, // candidate 0: balanced, matches ideal
            2.0, 0.1, // candidate 1: skewed
        ];
        let indices = rank(&scores, 2, 2, "jensen");
        assert_eq!(indices.len(), 2);
        // candidate 0 should rank first (higher fitness)
        assert_eq!(indices[0], 0);
    }

    #[test]
    fn rank_nds_dispatches_correctly() {
        // A dominates B
        let scores = vec![3.0, 3.0, 1.0, 1.0];
        let indices = rank(&scores, 2, 2, "nds");
        assert_eq!(indices[0], 0); // A is front 0
        assert_eq!(indices[1], 1); // B is front 1
    }

    #[test]
    fn rank_weighted_sum_fallback() {
        let scores = vec![
            1.0, 1.0, // sum=2
            3.0, 3.0, // sum=6
        ];
        let indices = rank(&scores, 2, 2, "weighted_sum");
        assert_eq!(indices[0], 1); // higher sum first
        assert_eq!(indices[1], 0);
    }

    #[test]
    fn rank_unknown_strategy_uses_weighted_sum() {
        let scores = vec![
            1.0, 1.0, // sum=2
            3.0, 3.0, // sum=6
        ];
        let indices = rank(&scores, 2, 2, "unknown_strategy");
        assert_eq!(indices[0], 1);
        assert_eq!(indices[1], 0);
    }

    #[test]
    fn rank_all_zero_scores() {
        let scores = vec![0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
        // Should not panic for any strategy
        let _j = rank(&scores, 3, 2, "jensen");
        let _n = rank(&scores, 3, 2, "nds");
        let _w = rank(&scores, 3, 2, "weighted_sum");
        // All return permutations of [0,1,2]
        assert_eq!(_j.len(), 3);
        assert_eq!(_n.len(), 3);
        assert_eq!(_w.len(), 3);
    }

    #[test]
    fn rank_lexicographic_orders_by_first_objective() {
        // Three candidates, two objectives.
        // c0: obj_0=0.9, obj_1=0.0
        // c1: obj_0=0.5, obj_1=1.0
        // c2: obj_0=0.5, obj_1=0.5
        // Lexicographic: c0 wins by obj_0 alone; c1 beats c2 on obj_1.
        let scores = vec![
            0.9, 0.0,
            0.5, 1.0,
            0.5, 0.5,
        ];
        let order = rank(&scores, 3, 2, "lexicographic");
        assert_eq!(order, vec![0, 1, 2]);
    }

    #[test]
    fn rank_lexicographic_breaks_ties_by_second_objective() {
        // c0 and c1 tie on obj_0; c1 wins on obj_1.
        let scores = vec![
            0.5, 0.4, // c0
            0.5, 0.9, // c1
            0.4, 0.0, // c2
        ];
        let order = rank(&scores, 3, 2, "lexicographic");
        assert_eq!(order, vec![1, 0, 2]);
    }

    #[test]
    fn rank_lexicographic_respects_user_curated_order() {
        // Same matrix, two different "user orderings": permuting the
        // column meaning changes who wins. Demonstrates the property
        // ``nds`` / ``jensen`` / ``weighted_sum`` lack.
        let scores_obj0_first = vec![
            0.9, 0.1, // c0
            0.1, 0.9, // c1
        ];
        // With objective 0 as primary, c0 wins.
        assert_eq!(rank(&scores_obj0_first, 2, 2, "lexicographic"), vec![0, 1]);
        // Swap the columns: now objective 1 (orig column) is primary.
        let scores_obj1_first = vec![
            0.1, 0.9, // c0
            0.9, 0.1, // c1
        ];
        assert_eq!(rank(&scores_obj1_first, 2, 2, "lexicographic"), vec![1, 0]);
    }

    #[test]
    fn rank_nds_tiebreak_by_sum() {
        // Two non-dominated candidates: NDS puts both in front 0,
        // then tiebreaks by sum (higher sum first)
        let scores = vec![
            1.0, 0.0, // sum=1
            0.0, 1.0, // sum=1
            0.5, 0.5, // sum=1, but non-dominated
        ];
        let indices = rank(&scores, 3, 2, "nds");
        assert_eq!(indices.len(), 3);
        // All are front 0 (non-dominated), so order is by sum descending
        // All sums are equal so any permutation is valid — just check it's a permutation
        let mut sorted = indices.clone();
        sorted.sort();
        assert_eq!(sorted, vec![0, 1, 2]);
    }

    // ========== Edge cases ==========

    #[test]
    fn jensen_fitness_between_0_and_1() {
        let scores = vec![
            10.0, 1.0,
            1.0, 10.0,
            5.0, 5.0,
        ];
        let fitness = jensen_divergence_fitness(&scores, 3, 2, 0.5);
        for &f in &fitness {
            assert!((-EPS..=1.0 + EPS).contains(&f), "fitness out of range: {}", f);
        }
    }

    #[test]
    fn rank_nds_lex_pareto_first_then_lex() {
        // Front 0: candidates 0 and 1 (both non-dominated; on the
        // Pareto frontier of {(2,0), (0,2)}). Candidate 2 is (1,1),
        // dominated by neither — so it joins front 0.
        // Add a clearly dominated candidate (3) so we can see the
        // front separation.
        let scores = vec![
            2.0, 0.0, // 0: high obj1
            0.0, 2.0, // 1: high obj2
            1.0, 1.0, // 2: balanced; non-dominated relative to 0/1
            0.5, 0.5, // 3: dominated by 2
        ];
        let indices = rank(&scores, 4, 2, "nds_lex");
        // 3 must come last (dominated front).
        assert_eq!(indices[3], 3);
        // Among front 0, lex by user order (obj1 first):
        // 0 has obj1=2 (highest), so 0 ranks first.
        // 1 has obj1=0 — last in front 0.
        assert_eq!(indices[0], 0);
        assert_eq!(indices[2], 1);
    }

    #[test]
    fn rank_nds_lex_swapping_objective_order_changes_winner() {
        // Same scores as above, but the user re-orders objectives
        // (obj2 first). Now lex tie-break favours candidate 1.
        let scores_swapped = vec![
            0.0, 2.0, // 0
            2.0, 0.0, // 1
            1.0, 1.0, // 2
            0.5, 0.5, // 3
        ];
        let indices = rank(&scores_swapped, 4, 2, "nds_lex");
        assert_eq!(indices[0], 1);
        assert_eq!(indices[3], 3);
    }

    #[test]
    fn nds_chain_produces_distinct_fronts() {
        // Full dominance chain — every candidate is on its own
        // front. Correct semantics: Pareto fronts are recursive
        // (front k = non-dominated after removing fronts < k), so
        // a chain of length N produces N distinct fronts.
        let scores = vec![
            5.0, 5.0,
            4.0, 4.0,
            3.0, 3.0,
            2.0, 2.0,
        ];
        let result = non_dominated_sort(&scores, 4, 2);
        assert_eq!(result, vec![0, 1, 2, 3]);
    }
}
