//! dorian_native — compiled hot paths for Dorian.
//!
//! Exposes GED computation, BK-Tree search, and ranking algorithms
//! as a Python extension module via PyO3.

mod bktree;
mod ged;
mod ranking;

use pyo3::prelude::*;
use std::sync::Mutex;

// ═══════════════════════════════════════════════════════════════════
// GED functions
// ═══════════════════════════════════════════════════════════════════

/// Compute graph edit distance between two pipeline DAG JSON strings.
///
/// Exact A* for small graphs (≤30 nodes total), fast approximation for larger.
/// `beam_limit` controls the max search nodes before falling back (default 50_000).
#[pyfunction]
#[pyo3(signature = (dag1_json, dag2_json, beam_limit=50_000))]
fn graph_edit_distance(dag1_json: &str, dag2_json: &str, beam_limit: usize) -> PyResult<usize> {
    let v1: serde_json::Value =
        serde_json::from_str(dag1_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let v2: serde_json::Value =
        serde_json::from_str(dag2_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

    let g1 = ged::DagGraph::from_json(&v1);
    let g2 = ged::DagGraph::from_json(&v2);

    Ok(ged::graph_edit_distance(&g1, &g2, beam_limit))
}

/// Fast approximate GED (operator set symmetric difference + edge count diff).
#[pyfunction]
fn fast_distance(dag1_json: &str, dag2_json: &str) -> PyResult<usize> {
    let v1: serde_json::Value =
        serde_json::from_str(dag1_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let v2: serde_json::Value =
        serde_json::from_str(dag2_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

    let g1 = ged::DagGraph::from_json(&v1);
    let g2 = ged::DagGraph::from_json(&v2);

    Ok(ged::fast_distance(&g1, &g2))
}

/// Extract sorted operator names from a DAG JSON string.
#[pyfunction]
fn extract_operator_names(dag_json: &str) -> PyResult<Vec<String>> {
    let v: serde_json::Value =
        serde_json::from_str(dag_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(ged::extract_operator_names(&v))
}

/// Minimum-signal edit path turning ``dag1`` into ``dag2``.
///
/// Returns a JSON string of ``{ops: [...], strategy: "id_diff" | "name_diff",
/// truncated: bool}``. ID-keyed diff is exact in O(|V|+|E|) when the two
/// graphs share node IDs (the common case after a user-correction on the
/// canvas); name-keyed fallback matches on (type, text) fingerprints.
#[pyfunction]
#[pyo3(signature = (dag1_json, dag2_json, max_ops=200))]
fn graph_edit_path(dag1_json: &str, dag2_json: &str, max_ops: usize) -> PyResult<String> {
    let v1: serde_json::Value = serde_json::from_str(dag1_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let v2: serde_json::Value = serde_json::from_str(dag2_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let g1 = ged::DagGraph::from_json(&v1);
    let g2 = ged::DagGraph::from_json(&v2);
    let res = ged::graph_edit_path(&g1, &g2, max_ops);

    let ops_val: Vec<serde_json::Value> =
        res.ops.iter().map(|op| op.to_json_value()).collect();
    let out = serde_json::json!({
        "ops": ops_val,
        "strategy": res.strategy,
        "truncated": res.truncated,
    });
    Ok(out.to_string())
}

// ═══════════════════════════════════════════════════════════════════
// BK-Tree
// ═══════════════════════════════════════════════════════════════════

#[pyclass]
struct BKTree {
    inner: Mutex<bktree::PipelineBKTree>,
}

#[pymethods]
impl BKTree {
    #[new]
    #[pyo3(signature = (use_exact_ged=true, beam_limit=50_000))]
    fn new(use_exact_ged: bool, beam_limit: usize) -> Self {
        BKTree {
            inner: Mutex::new(bktree::PipelineBKTree::new(use_exact_ged, beam_limit)),
        }
    }

    /// Number of pipelines in the tree.
    #[getter]
    fn size(&self) -> usize {
        self.inner.lock().unwrap().len()
    }

    /// Check if a pipeline ID is already in the tree.
    fn contains(&self, pipeline_id: &str) -> bool {
        self.inner.lock().unwrap().contains(pipeline_id)
    }

    /// Add a pipeline (DAG as JSON string). Returns True if added, False if duplicate.
    fn add(&self, pipeline_id: &str, dag_json: &str) -> bool {
        self.inner.lock().unwrap().add(pipeline_id, dag_json)
    }

    /// Find all pipelines within max_distance edits.
    /// Returns list of (pipeline_id, distance) sorted by distance.
    #[pyo3(signature = (dag_json, max_distance=5))]
    fn query(&self, dag_json: &str, max_distance: usize) -> Vec<(String, usize)> {
        self.inner.lock().unwrap().query(dag_json, max_distance)
    }

    /// Find the k nearest pipelines (up to max_distance).
    #[pyo3(signature = (dag_json, k=5, max_distance=10))]
    fn find_nearest(&self, dag_json: &str, k: usize, max_distance: usize) -> Vec<(String, usize)> {
        self.inner
            .lock()
            .unwrap()
            .find_nearest(dag_json, k, max_distance)
    }
}

// ═══════════════════════════════════════════════════════════════════
// Ranking
// ═══════════════════════════════════════════════════════════════════

/// Compute Generalized Jensen–Shannon divergence fitness.
///
/// Args:
///     scores: flat list of floats, row-major (n_candidates × n_objectives)
///     n_candidates: number of candidates
///     n_objectives: number of objectives
///     alpha: mixing parameter (default 0.5)
///
/// Returns: list of fitness values (higher = better), one per candidate.
#[pyfunction]
#[pyo3(signature = (scores, n_candidates, n_objectives, alpha=0.5))]
fn jensen_divergence_fitness(
    scores: Vec<f64>,
    n_candidates: usize,
    n_objectives: usize,
    alpha: f64,
) -> Vec<f64> {
    ranking::jensen_divergence_fitness(&scores, n_candidates, n_objectives, alpha)
}

/// Non-dominated sorting (Pareto fronts).
///
/// Args:
///     scores: flat list of floats, row-major (n_candidates × n_objectives)
///     n_candidates: number of candidates
///     n_objectives: number of objectives
///
/// Returns: list of front indices (0 = Pareto-optimal, lower = better).
#[pyfunction]
fn non_dominated_sort(scores: Vec<f64>, n_candidates: usize, n_objectives: usize) -> Vec<usize> {
    ranking::non_dominated_sort(&scores, n_candidates, n_objectives)
}

/// Rank candidates given a scores matrix and strategy.
///
/// Args:
///     scores: flat list of floats, row-major
///     n_candidates: number of candidates
///     n_objectives: number of objectives
///     strategy: "jensen", "nds", or "weighted_sum"
///
/// Returns: list of candidate indices sorted best-first.
#[pyfunction]
#[pyo3(signature = (scores, n_candidates, n_objectives, strategy="nds"))]
fn rank(
    scores: Vec<f64>,
    n_candidates: usize,
    n_objectives: usize,
    strategy: &str,
) -> Vec<usize> {
    ranking::rank(&scores, n_candidates, n_objectives, strategy)
}

// ═══════════════════════════════════════════════════════════════════
// Module
// ═══════════════════════════════════════════════════════════════════

#[pymodule]
fn dorian_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // GED
    m.add_function(wrap_pyfunction!(graph_edit_distance, m)?)?;
    m.add_function(wrap_pyfunction!(graph_edit_path, m)?)?;
    m.add_function(wrap_pyfunction!(fast_distance, m)?)?;
    m.add_function(wrap_pyfunction!(extract_operator_names, m)?)?;

    // BK-Tree
    m.add_class::<BKTree>()?;

    // Ranking
    m.add_function(wrap_pyfunction!(jensen_divergence_fitness, m)?)?;
    m.add_function(wrap_pyfunction!(non_dominated_sort, m)?)?;
    m.add_function(wrap_pyfunction!(rank, m)?)?;

    Ok(())
}
