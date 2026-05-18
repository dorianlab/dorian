//! Ranking objectives — scoring functions for pipeline recommendation.
//!
//! Ports `dorian/pipeline/recommendation/objectives.py` to Rust.
//!
//! Each objective scores pipeline candidates on a single axis. The recommendation
//! engine combines multiple objectives via non-dominated sorting (Pareto fronts)
//! or Jensen divergence (already in `ranking.rs`).
//!
//! Built-in objectives:
//! - **GeneralPerformance**: Mean evaluation score from stored results
//! - **PreviouslyUnseen**: Penalizes already-suggested candidates
//! - **AtomicChanges**: Jaccard similarity with current pipeline operators
//!
//! Objectives that require Python runtime (not ported):
//! - **SimilarDataPerformance**: KD-Tree queries in Experiment Store
//! - **PipelinePreferenceRatio**: Win rate from pairwise interactions
//! - **UserDefinedObjective**: Compiled user code (sandbox)
//!
//! Dependency system:
//! - Each objective declares required context fields (`requires`)
//! - Missing fields → objective returns 0.0 (graceful degradation)
//! - `check_dependencies` produces status metadata for the frontend

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use crate::recommendation::store::{ExperimentStore, StoredEval};

// ---------------------------------------------------------------------------
// Context — immutable snapshot for one ranking round
// ---------------------------------------------------------------------------

/// Immutable context snapshot for one recommendation round.
///
/// Built from session meta (Redis) + interaction history before scoring.
/// All fields are optional to support graceful degradation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecommendationContext {
    /// User ID.
    pub uid: String,
    /// Session ID.
    pub session: String,
    /// Current pipeline on the canvas (if any).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub current_pipeline: Option<PipelineSnapshot>,
    /// Dataset metafeature profile (if profiled).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dataset_profile: Option<serde_json::Value>,
    /// Pre-vectorised dataset profile in the canonical feature
    /// order. Set by the python seed_session before handing the
    /// context to the rust scoring pass — saves us re-deriving the
    /// feature ordering on the rust side. ``None`` falls back to
    /// the json profile (extracts numeric scalars best-effort).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dataset_profile_vec: Option<Vec<f64>>,
    /// Pipeline IDs the user upvoted.
    #[serde(default)]
    pub upvoted: Vec<String>,
    /// Pipeline IDs the user downvoted.
    #[serde(default)]
    pub downvoted: Vec<String>,
    /// Pipeline IDs the user selected (applied to canvas).
    #[serde(default)]
    pub selected: Vec<String>,
    /// Pipeline IDs that were previously suggested.
    #[serde(default)]
    pub suggested: Vec<String>,
    /// Names of active objectives.
    #[serde(default)]
    pub objective_names: Vec<String>,
    /// Selected data science task (e.g., "Classification").
    #[serde(skip_serializing_if = "Option::is_none")]
    pub task: Option<String>,
}

/// Minimal pipeline snapshot for operator extraction.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PipelineSnapshot {
    /// Pipeline nodes (operator name → node data).
    #[serde(default)]
    pub nodes: HashMap<String, serde_json::Value>,
}

impl RecommendationContext {
    /// Whether the context has a current pipeline.
    pub fn has_pipeline(&self) -> bool {
        self.current_pipeline.is_some()
    }

    /// Whether the user has any interactions.
    pub fn has_interactions(&self) -> bool {
        !self.upvoted.is_empty() || !self.downvoted.is_empty() || !self.selected.is_empty()
    }
}

// ---------------------------------------------------------------------------
// Candidate representation
// ---------------------------------------------------------------------------

/// A pipeline candidate from the database.
///
/// Carries the fields needed for scoring AND the fields the
/// frontend needs to render the pipeline on the canvas. The full
/// document is passed through to the frontend after ranking; any
/// field missing here gets silently dropped during the round-trip
/// (a recurring bug — the canvas renders nodes from
/// ``Candidate.nodes`` but edges have to round-trip through this
/// struct too, otherwise selecting a recommendation produces a
/// disconnected node soup on the canvas).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candidate {
    /// Database ID (docstore ObjectId string).
    #[serde(rename = "_id", default)]
    pub id: String,
    /// Pipeline nodes for operator extraction.
    #[serde(default)]
    pub nodes: HashMap<String, serde_json::Value>,
    /// Pipeline edges — preserved verbatim so the frontend can
    /// render them on the canvas after the user accepts a
    /// recommendation. Every edge dict carries ``source``,
    /// ``destination``, ``position`` (int or kwarg name), and
    /// ``output``. Stored as raw JSON so legacy seeded shapes
    /// (e.g. extra fields on edges) survive the round-trip.
    #[serde(default)]
    pub edges: Vec<serde_json::Value>,
    /// Alternative: operator list if nodes not available.
    #[serde(default)]
    pub operators: Vec<serde_json::Value>,
    /// Stored evaluation results.
    #[serde(default)]
    pub evaluations: Vec<Evaluation>,
    /// Data science task (e.g., "Classification").
    #[serde(skip_serializing_if = "Option::is_none")]
    pub task: Option<String>,
}

/// An evaluation result for a candidate pipeline.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Evaluation {
    /// Dataset this evaluation was produced on. Optional because
    /// the python source's ``Evaluation`` shape didn't carry it
    /// historically — when missing, ``SimilarDataPerformance``
    /// falls back to the unrelated-dataset baseline weight (0.1).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dataset_id: Option<String>,
    /// Evaluation score (higher = better).
    #[serde(default)]
    pub score: f64,
    /// Evaluation metric name (e.g., "accuracy", "f1").
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metric: Option<String>,
}

// ---------------------------------------------------------------------------
// Objective trait
// ---------------------------------------------------------------------------

/// Scoring objective for pipeline ranking.
///
/// Each objective scores candidates on a single axis. The recommendation
/// engine combines scores via ranking algorithms in `ranking.rs`.
pub trait Objective: Send + Sync {
    /// Human-readable objective name.
    fn name(&self) -> &str;

    /// Context fields this objective requires (for dependency checking).
    fn requires(&self) -> &[&str];

    /// Score a candidate in the given context. Returns 0.0..=1.0 ideally.
    fn score(&self, candidate: &Candidate, ctx: &RecommendationContext) -> f64;
}

// ---------------------------------------------------------------------------
// Built-in objectives
// ---------------------------------------------------------------------------

/// Ranks candidates by their stored evaluation performance (mean score).
///
/// No context dependencies — always active.
pub struct GeneralPerformance;

impl Objective for GeneralPerformance {
    fn name(&self) -> &str {
        "Good General Performance"
    }

    fn requires(&self) -> &[&str] {
        &[]
    }

    fn score(&self, candidate: &Candidate, _ctx: &RecommendationContext) -> f64 {
        if candidate.evaluations.is_empty() {
            return 0.0;
        }
        let sum: f64 = candidate.evaluations.iter().map(|e| e.score).sum();
        sum / candidate.evaluations.len() as f64
    }
}

/// Penalizes candidates that were already suggested but not selected.
///
/// Returns 0.0 for selected candidates (don't re-suggest what they chose).
/// Returns negative count for previously suggested candidates (penalty).
pub struct PreviouslyUnseen;

impl Objective for PreviouslyUnseen {
    fn name(&self) -> &str {
        "Previously Unseen"
    }

    fn requires(&self) -> &[&str] {
        &[]
    }

    fn score(&self, candidate: &Candidate, ctx: &RecommendationContext) -> f64 {
        if ctx.selected.contains(&candidate.id) {
            return 0.0;
        }
        // Negative count of times suggested (penalty).
        let count = ctx.suggested.iter().filter(|s| *s == &candidate.id).count();
        -(count as f64)
    }
}

/// Prefers candidates that are cheaper to execute.
///
/// Proxy: `1 / (1 + n_operators)`. If the candidate carries an evaluation
/// whose metric is `duration` / `runtime_ms` / `elapsed_ms`, the stored time
/// overrides the proxy.
pub struct FasterExecution;

impl Objective for FasterExecution {
    fn name(&self) -> &str {
        "Faster Execution"
    }

    fn requires(&self) -> &[&str] {
        &[]
    }

    fn score(&self, candidate: &Candidate, _ctx: &RecommendationContext) -> f64 {
        // Prefer stored duration-like metric if present.
        let durations: Vec<f64> = candidate
            .evaluations
            .iter()
            .filter_map(|e| {
                let m = e.metric.as_deref().unwrap_or("").to_ascii_lowercase();
                let is_duration = matches!(
                    m.as_str(),
                    "duration" | "runtime_ms" | "runtime" | "elapsed_ms"
                );
                if is_duration && e.score > 0.0 {
                    Some(e.score)
                } else {
                    None
                }
            })
            .collect();

        if !durations.is_empty() {
            let mean: f64 = durations.iter().sum::<f64>() / durations.len() as f64;
            return 1.0 / (1.0 + mean);
        }

        // Proxy via operator count.
        let mut n = extract_operator_names(candidate).len();
        if n == 0 {
            n = candidate.nodes.len() + candidate.operators.len();
        }
        1.0 / (1.0 + n as f64)
    }
}

/// Prefers candidates whose operators overlap with the current pipeline (Jaccard).
///
/// Requires `current_pipeline` in context. Returns 0.0 when degraded.
pub struct AtomicChanges;

impl Objective for AtomicChanges {
    fn name(&self) -> &str {
        "Atomic Changes"
    }

    fn requires(&self) -> &[&str] {
        &["current_pipeline"]
    }

    fn score(&self, candidate: &Candidate, ctx: &RecommendationContext) -> f64 {
        let current = match &ctx.current_pipeline {
            Some(p) => extract_operator_names_from_nodes(&p.nodes),
            None => return 0.0,
        };

        let candidate_ops = extract_operator_names(candidate);

        let current_set: HashSet<&str> = current.iter().map(|s| s.as_str()).collect();
        let candidate_set: HashSet<&str> = candidate_ops.iter().map(|s| s.as_str()).collect();

        let union: HashSet<&str> = current_set.union(&candidate_set).copied().collect();
        if union.is_empty() {
            return 0.0;
        }

        let intersection = current_set.intersection(&candidate_set).count();
        intersection as f64 / union.len() as f64
    }
}

// ---------------------------------------------------------------------------
// Experiment-store-backed objectives
// ---------------------------------------------------------------------------

/// Ranks candidates by performance on datasets similar to the
/// query dataset's metafeature profile. Mirrors the python
/// ``SimilarDataPerformance`` but keeps the entire scoring path
/// in rust — no pyo3 round-trip per candidate, no async hop, the
/// ``ExperimentStore`` lookup is one in-memory ``k_nearest`` walk.
///
/// Falls back to ``GeneralPerformance``-style mean score when:
///   * the store is empty (cold start, no datasets profiled yet);
///   * the context has no ``dataset_profile_vec`` (the query
///     pipeline runs in a non-dataset session).
///
/// Holds an ``Arc<ExperimentStore>`` so cloning the objective into
/// rayon workers is cheap — every worker shares the same data.
pub struct SimilarDataPerformance {
    pub store: Arc<ExperimentStore>,
    pub k: usize,
}

impl SimilarDataPerformance {
    pub fn new(store: Arc<ExperimentStore>) -> Self {
        Self { store, k: 5 }
    }

    fn fallback_mean(candidate: &Candidate) -> f64 {
        if candidate.evaluations.is_empty() {
            return 0.0;
        }
        let mut total = 0.0;
        let mut n = 0usize;
        for ev in &candidate.evaluations {
            if ev.score.is_finite() {
                total += ev.score;
                n += 1;
            }
        }
        if n > 0 {
            total / n as f64
        } else {
            0.0
        }
    }

    fn evaluations_to_stored(candidate: &Candidate) -> Vec<StoredEval> {
        candidate
            .evaluations
            .iter()
            .filter(|ev| ev.score.is_finite())
            .map(|ev| StoredEval {
                dataset_id: ev.dataset_id.clone().unwrap_or_default(),
                score: ev.score,
            })
            .collect()
    }
}

impl Objective for SimilarDataPerformance {
    fn name(&self) -> &str {
        "Good Performance On Similar Data"
    }

    fn requires(&self) -> &[&'static str] {
        &["dataset_profile"]
    }

    fn score(&self, candidate: &Candidate, ctx: &RecommendationContext) -> f64 {
        let Some(query_vec) = ctx.dataset_profile_vec.as_ref() else {
            return Self::fallback_mean(candidate);
        };
        if self.store.is_empty() {
            return Self::fallback_mean(candidate);
        }
        let evals = Self::evaluations_to_stored(candidate);
        let s = self.store.score_by_similar_datasets(&evals, query_vec, self.k);
        if s == 0.0 {
            // No useful weighting — fall back to mean rather than
            // surface a hard zero, mirrors the python's behaviour
            // when every eval lands on the "unknown" weight.
            Self::fallback_mean(candidate)
        } else {
            s
        }
    }
}

/// Ranks candidates by their pairwise win rate from user
/// interactions. ``win_rate = times_preferred / times_compared``.
/// The python preloads the win-rate cache once before scoring;
/// here we hold an ``Arc<ExperimentStore>`` whose ``win_rates``
/// map was populated at backend startup. Hash lookup per candidate.
///
/// Returns ``0.0`` for candidates with no interaction history —
/// graceful cold-start, same as python.
pub struct PipelinePreferenceRatio {
    pub store: Arc<ExperimentStore>,
}

impl PipelinePreferenceRatio {
    pub fn new(store: Arc<ExperimentStore>) -> Self {
        Self { store }
    }
}

impl Objective for PipelinePreferenceRatio {
    fn name(&self) -> &str {
        "Pipeline Preference Ratio"
    }

    fn requires(&self) -> &[&'static str] {
        &[]
    }

    fn score(&self, candidate: &Candidate, _ctx: &RecommendationContext) -> f64 {
        if candidate.id.is_empty() {
            return 0.0;
        }
        self.store.win_rate(&candidate.id)
    }
}

// ---------------------------------------------------------------------------
// Objective registry
// ---------------------------------------------------------------------------

/// Known built-in objective names.
pub const BUILTIN_OBJECTIVES: &[&str] = &[
    "Good General Performance",
    "Good Performance On Similar Data",
    "Previously Unseen",
    "Pipeline Preference Ratio",
    "Atomic Changes",
    "Faster Execution",
];

/// Create a built-in objective by name.
///
/// State-free objectives are constructible from this entry directly;
/// the experiment-store-backed ones (``SimilarDataPerformance``,
/// ``PipelinePreferenceRatio``) need a populated
/// ``Arc<ExperimentStore>`` and are built via
/// :func:`create_builtin_objective_with_store` instead.
pub fn create_builtin_objective(name: &str) -> Option<Box<dyn Objective>> {
    match name {
        "Good General Performance" => Some(Box::new(GeneralPerformance)),
        "Previously Unseen" => Some(Box::new(PreviouslyUnseen)),
        "Atomic Changes" => Some(Box::new(AtomicChanges)),
        "Faster Execution" => Some(Box::new(FasterExecution)),
        _ => None,
    }
}

/// Create a built-in objective by name, with the experiment store
/// available. Use from the ``recommend`` flow that already holds a
/// store reference; falls through to the state-free version
/// otherwise.
pub fn create_builtin_objective_with_store(
    name: &str,
    store: &Arc<ExperimentStore>,
) -> Option<Box<dyn Objective>> {
    match name {
        "Good Performance On Similar Data" => {
            Some(Box::new(SimilarDataPerformance::new(Arc::clone(store))))
        }
        "Pipeline Preference Ratio" => {
            Some(Box::new(PipelinePreferenceRatio::new(Arc::clone(store))))
        }
        other => create_builtin_objective(other),
    }
}

// ---------------------------------------------------------------------------
// Dependency checking
// ---------------------------------------------------------------------------

/// Status of an objective in the current context.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObjectiveStatus {
    /// Objective name.
    pub name: String,
    /// Whether the objective is active or degraded.
    pub status: String,
    /// Missing context fields (empty if active).
    #[serde(default)]
    pub missing: Vec<String>,
}

/// Check which objectives are active vs degraded given the current context.
///
/// An objective is "degraded" when one or more of its required context fields
/// are missing. The scoring behavior doesn't change (objectives return 0.0 for
/// missing deps), but this metadata helps the frontend show status indicators.
pub fn check_dependencies(
    objectives: &[Box<dyn Objective>],
    ctx: &RecommendationContext,
) -> Vec<ObjectiveStatus> {
    objectives
        .iter()
        .map(|obj| {
            let missing: Vec<String> = obj
                .requires()
                .iter()
                .filter(|dep| {
                    match **dep {
                        "current_pipeline" => ctx.current_pipeline.is_none(),
                        "dataset_profile" => ctx.dataset_profile.is_none(),
                        "task" => ctx.task.is_none(),
                        _ => true, // Unknown dep → always missing.
                    }
                })
                .map(|s| s.to_string())
                .collect();

            ObjectiveStatus {
                name: obj.name().to_string(),
                status: if missing.is_empty() {
                    "active".to_string()
                } else {
                    "degraded".to_string()
                },
                missing,
            }
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Extract operator names from a candidate pipeline.
pub fn extract_operator_names(candidate: &Candidate) -> Vec<String> {
    let mut names = extract_operator_names_from_nodes(&candidate.nodes);

    // Also check "operators" field (alternative shape).
    for op in &candidate.operators {
        if let Some(name) = op
            .as_object()
            .and_then(|o| o.get("name").or(o.get("operator")))
            .and_then(|v| v.as_str())
        {
            names.push(name.to_string());
        }
    }

    names
}

/// Extract operator names from a nodes map.
fn extract_operator_names_from_nodes(
    nodes: &HashMap<String, serde_json::Value>,
) -> Vec<String> {
    nodes
        .values()
        .filter_map(|v| {
            v.as_object()
                .and_then(|o| o.get("name").or(o.get("operator")))
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
        })
        .collect()
}

/// Score all candidates with all objectives, producing an N×M score
/// matrix (row-major: ``scores[i * n_obj + j]``). Each candidate's
/// row is computed independently — rayon splits the candidate axis
/// across worker threads so the per-row objective evaluations run in
/// parallel. Single-threaded fallback under a small-batch threshold
/// where rayon's split overhead dominates.
///
/// The current ``Objective`` impls are pure and ``Send + Sync`` (the
/// trait declares both bounds), so the parallel walk is safe without
/// any locking. Each row writes only to its own slice of the output
/// vec; there's zero cross-thread contention.
pub fn score_candidates(
    candidates: &[Candidate],
    objectives: &[Box<dyn Objective>],
    ctx: &RecommendationContext,
) -> Vec<f64> {
    use rayon::prelude::*;

    let n_obj = objectives.len();
    let n_cand = candidates.len();
    let mut scores = vec![0.0f64; n_cand * n_obj];
    if n_cand == 0 || n_obj == 0 {
        return scores;
    }

    // Below ~64 candidates the per-task overhead beats the parallel
    // gain — for the AI Debugger's per-edit recommendation refresh
    // (single-digit candidate counts) we don't want to spin up
    // workers. The RL trainer + corpus rerank cases are 1k+
    // candidates where parallelism dominates.
    const PAR_THRESHOLD: usize = 64;
    if n_cand < PAR_THRESHOLD {
        for (row, candidate) in scores.chunks_exact_mut(n_obj).zip(candidates.iter()) {
            for (slot, obj) in row.iter_mut().zip(objectives.iter()) {
                *slot = obj.score(candidate, ctx);
            }
        }
        return scores;
    }

    scores
        .par_chunks_exact_mut(n_obj)
        .zip(candidates.par_iter())
        .for_each(|(row, candidate)| {
            for (slot, obj) in row.iter_mut().zip(objectives.iter()) {
                *slot = obj.score(candidate, ctx);
            }
        });
    scores
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_context() -> RecommendationContext {
        RecommendationContext {
            uid: "user1".to_string(),
            session: "sess1".to_string(),
            current_pipeline: None,
            dataset_profile: None,
            dataset_profile_vec: None,
            upvoted: Vec::new(),
            downvoted: Vec::new(),
            selected: Vec::new(),
            suggested: Vec::new(),
            objective_names: Vec::new(),
            task: None,
        }
    }

    fn sample_candidate(id: &str, scores: &[f64]) -> Candidate {
        Candidate {
            id: id.to_string(),
            nodes: HashMap::new(),
            operators: Vec::new(),
            evaluations: scores
                .iter()
                .map(|&s| Evaluation {
                    dataset_id: None,
                    score: s,
                    metric: Some("accuracy".to_string()),
                })
                .collect(),
            task: None,
        }
    }

    #[test]
    fn test_general_performance_mean() {
        let obj = GeneralPerformance;
        let ctx = sample_context();
        let candidate = sample_candidate("c1", &[0.8, 0.9, 0.7]);
        let score = obj.score(&candidate, &ctx);
        assert!((score - 0.8).abs() < 1e-10);
    }

    #[test]
    fn test_general_performance_empty() {
        let obj = GeneralPerformance;
        let ctx = sample_context();
        let candidate = sample_candidate("c1", &[]);
        assert_eq!(obj.score(&candidate, &ctx), 0.0);
    }

    #[test]
    fn test_previously_unseen_fresh() {
        let obj = PreviouslyUnseen;
        let ctx = sample_context();
        let candidate = sample_candidate("c1", &[]);
        // Fresh candidate — no penalty.
        assert_eq!(obj.score(&candidate, &ctx), 0.0);
    }

    #[test]
    fn test_previously_unseen_suggested() {
        let obj = PreviouslyUnseen;
        let mut ctx = sample_context();
        ctx.suggested = vec!["c1".to_string(), "c1".to_string()];
        let candidate = sample_candidate("c1", &[]);
        // Suggested twice → penalty of -2.
        assert_eq!(obj.score(&candidate, &ctx), -2.0);
    }

    #[test]
    fn test_previously_unseen_selected() {
        let obj = PreviouslyUnseen;
        let mut ctx = sample_context();
        ctx.selected = vec!["c1".to_string()];
        ctx.suggested = vec!["c1".to_string()];
        let candidate = sample_candidate("c1", &[]);
        // Selected → score 0 (don't re-suggest).
        assert_eq!(obj.score(&candidate, &ctx), 0.0);
    }

    #[test]
    fn test_atomic_changes_no_pipeline() {
        let obj = AtomicChanges;
        let ctx = sample_context(); // No current pipeline.
        let candidate = sample_candidate("c1", &[]);
        assert_eq!(obj.score(&candidate, &ctx), 0.0);
    }

    #[test]
    fn test_atomic_changes_identical_pipeline() {
        let obj = AtomicChanges;
        let mut nodes = HashMap::new();
        nodes.insert(
            "n1".to_string(),
            serde_json::json!({"name": "sklearn.svm.SVC"}),
        );
        nodes.insert(
            "n2".to_string(),
            serde_json::json!({"name": "sklearn.preprocessing.StandardScaler"}),
        );

        let mut ctx = sample_context();
        ctx.current_pipeline = Some(PipelineSnapshot { nodes: nodes.clone() });

        let mut candidate = sample_candidate("c1", &[]);
        candidate.nodes = nodes;

        // Identical pipeline → Jaccard = 1.0.
        assert_eq!(obj.score(&candidate, &ctx), 1.0);
    }

    #[test]
    fn test_atomic_changes_partial_overlap() {
        let obj = AtomicChanges;
        let mut current_nodes = HashMap::new();
        current_nodes.insert(
            "n1".to_string(),
            serde_json::json!({"name": "sklearn.svm.SVC"}),
        );
        current_nodes.insert(
            "n2".to_string(),
            serde_json::json!({"name": "sklearn.preprocessing.StandardScaler"}),
        );

        let mut ctx = sample_context();
        ctx.current_pipeline = Some(PipelineSnapshot {
            nodes: current_nodes,
        });

        let mut candidate_nodes = HashMap::new();
        candidate_nodes.insert(
            "n1".to_string(),
            serde_json::json!({"name": "sklearn.svm.SVC"}),
        );
        candidate_nodes.insert(
            "n3".to_string(),
            serde_json::json!({"name": "sklearn.ensemble.RandomForestClassifier"}),
        );

        let mut candidate = sample_candidate("c1", &[]);
        candidate.nodes = candidate_nodes;

        // 1 overlap / 3 union = 0.333...
        let score = obj.score(&candidate, &ctx);
        assert!((score - 1.0 / 3.0).abs() < 1e-10);
    }

    #[test]
    fn test_create_builtin_objective() {
        assert!(create_builtin_objective("Good General Performance").is_some());
        assert!(create_builtin_objective("Previously Unseen").is_some());
        assert!(create_builtin_objective("Atomic Changes").is_some());
        // Python-only objectives return None.
        assert!(create_builtin_objective("Good Performance On Similar Data").is_none());
        assert!(create_builtin_objective("Pipeline Preference Ratio").is_none());
        assert!(create_builtin_objective("Unknown Objective").is_none());
    }

    #[test]
    fn test_check_dependencies_all_active() {
        let objectives: Vec<Box<dyn Objective>> = vec![
            Box::new(GeneralPerformance),
            Box::new(PreviouslyUnseen),
        ];
        let ctx = sample_context();
        let status = check_dependencies(&objectives, &ctx);

        assert_eq!(status.len(), 2);
        assert_eq!(status[0].status, "active");
        assert_eq!(status[1].status, "active");
    }

    #[test]
    fn test_check_dependencies_degraded() {
        let objectives: Vec<Box<dyn Objective>> = vec![
            Box::new(AtomicChanges), // Requires current_pipeline.
        ];
        let ctx = sample_context(); // No pipeline.
        let status = check_dependencies(&objectives, &ctx);

        assert_eq!(status.len(), 1);
        assert_eq!(status[0].status, "degraded");
        assert_eq!(status[0].missing, vec!["current_pipeline"]);
    }

    #[test]
    fn test_check_dependencies_active_with_pipeline() {
        let objectives: Vec<Box<dyn Objective>> = vec![
            Box::new(AtomicChanges),
        ];
        let mut ctx = sample_context();
        ctx.current_pipeline = Some(PipelineSnapshot {
            nodes: HashMap::new(),
        });
        let status = check_dependencies(&objectives, &ctx);

        assert_eq!(status[0].status, "active");
        assert!(status[0].missing.is_empty());
    }

    #[test]
    fn test_score_candidates() {
        let objectives: Vec<Box<dyn Objective>> = vec![
            Box::new(GeneralPerformance),
            Box::new(PreviouslyUnseen),
        ];
        let ctx = sample_context();
        let candidates = vec![
            sample_candidate("c1", &[0.8, 0.9]),
            sample_candidate("c2", &[0.7]),
        ];

        let scores = score_candidates(&candidates, &objectives, &ctx);

        // 2 candidates × 2 objectives = 4 scores.
        assert_eq!(scores.len(), 4);
        // c1: GeneralPerformance = 0.85, PreviouslyUnseen = 0.0.
        assert!((scores[0] - 0.85).abs() < 1e-10);
        assert_eq!(scores[1], 0.0);
        // c2: GeneralPerformance = 0.7, PreviouslyUnseen = 0.0.
        assert!((scores[2] - 0.7).abs() < 1e-10);
        assert_eq!(scores[3], 0.0);
    }

    #[test]
    fn test_extract_operator_names() {
        let mut nodes = HashMap::new();
        nodes.insert(
            "n1".to_string(),
            serde_json::json!({"name": "sklearn.svm.SVC"}),
        );
        nodes.insert(
            "n2".to_string(),
            serde_json::json!({"operator": "pandas.read_csv"}),
        );
        nodes.insert(
            "n3".to_string(),
            serde_json::json!({"value": "100"}), // No name or operator.
        );

        let mut candidate = Candidate {
            id: "test".to_string(),
            nodes,
            operators: Vec::new(),
            evaluations: Vec::new(),
            task: None,
        };

        let names = extract_operator_names(&candidate);
        assert_eq!(names.len(), 2);
        assert!(names.contains(&"sklearn.svm.SVC".to_string()));
        assert!(names.contains(&"pandas.read_csv".to_string()));

        // Also check operators field.
        candidate.operators = vec![
            serde_json::json!({"name": "extra_op"}),
        ];
        let names = extract_operator_names(&candidate);
        assert_eq!(names.len(), 3);
    }

    #[test]
    fn test_context_serialization() {
        let ctx = sample_context();
        let json = serde_json::to_string(&ctx).unwrap();
        let decoded: RecommendationContext = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.uid, "user1");
        assert_eq!(decoded.session, "sess1");
        assert!(!decoded.has_pipeline());
        assert!(!decoded.has_interactions());
    }

    #[test]
    fn test_context_with_interactions() {
        let mut ctx = sample_context();
        ctx.upvoted = vec!["p1".to_string()];
        assert!(ctx.has_interactions());
    }

    #[test]
    fn test_objective_status_serialization() {
        let status = ObjectiveStatus {
            name: "Test".to_string(),
            status: "active".to_string(),
            missing: Vec::new(),
        };
        let json = serde_json::to_string(&status).unwrap();
        assert!(json.contains("\"status\":\"active\""));
    }

    #[test]
    fn test_candidate_serialization() {
        let candidate = sample_candidate("c1", &[0.8]);
        let json = serde_json::to_string(&candidate).unwrap();
        assert!(json.contains("\"_id\":\"c1\""));
    }
}
