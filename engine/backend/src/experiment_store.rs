//! Build the experiment store (dataset KD-tree + win-rate cache)
//! from postgres at lifespan startup.
//!
//! Two reads off the schema:
//!
//!   * ``datasets(id, profile_vec)`` — every row whose ``profile_vec``
//!     is non-NULL contributes a ``(dataset_id, vec)`` pair to the
//!     KD-tree. Stale ``vec_version`` rows are ignored here; the
//!     python lifespan re-vectorises them and re-runs this load.
//!   * ``interactions(compared_id, preferred_id)`` — one aggregate
//!     produces ``pipeline_id → (wins, total)``, stored as the
//!     win-rate cache.
//!
//! Returns an ``Option`` because postgres may be unreachable at
//! startup; callers degrade gracefully (the experiment-store-backed
//! objectives fall back to mean-score / 0.0).

use std::sync::Arc;

use anyhow::{Context, Result};
use deadpool_postgres::Pool;
use rustc_hash::FxHashMap;

use optimizer::recommendation::ExperimentStore;

/// Load the in-memory experiment store from postgres. Single
/// blocking call — pool acquire + two queries. Use at backend
/// lifespan startup; cache the resulting ``Arc<ExperimentStore>``
/// on ``AppState`` for the rest of the process.
pub async fn load(pool: &Pool) -> Result<Arc<ExperimentStore>> {
    let datasets = load_datasets(pool).await?;
    let win_rates = load_win_rates(pool).await?;
    Ok(Arc::new(ExperimentStore::from_parts(datasets, win_rates)))
}

async fn load_datasets(pool: &Pool) -> Result<Vec<(String, Vec<f64>)>> {
    let client = pool.get().await.with_context(|| "pool.get")?;
    // ``profile_vec`` is ``DOUBLE PRECISION[]`` in postgres. Individual
    // elements may be NULL — postgres stores missing/NaN metafeatures
    // that way (the python writer in ``dorian/experiment/store.py``
    // converts numpy NaN to Python None on insert). Reading directly
    // into ``Vec<f64>`` panics on any NULL element ("error deserializing
    // column 1"), which used to crash the backend at startup and leave
    // the SPA sidebar empty because the subscriber never came up.
    // Read as ``Vec<Option<f64>>`` and project None → NaN; the
    // optimizer's ExperimentStore already treats non-finite entries
    // as missing (see ``compute_bounds`` / ``normalise_one``).
    let rows = client
        .query(
            "SELECT id, profile_vec FROM datasets WHERE profile_vec IS NOT NULL",
            &[],
        )
        .await
        .with_context(|| "select datasets.profile_vec")?;
    let mut out: Vec<(String, Vec<f64>)> = Vec::with_capacity(rows.len());
    for row in rows {
        let id: String = row.get(0);
        let raw: Vec<Option<f64>> = row.get(1);
        if raw.is_empty() {
            continue;
        }
        let vec: Vec<f64> = raw.into_iter().map(|v| v.unwrap_or(f64::NAN)).collect();
        out.push((id, vec));
    }
    Ok(out)
}

async fn load_win_rates(pool: &Pool) -> Result<FxHashMap<String, f64>> {
    let client = pool.get().await.with_context(|| "pool.get")?;
    // Aggregate-once: compute (pipeline_id, wins, total) over the
    // interactions table. Same SQL the python ``preload_win_rates``
    // runs. ``COUNT(*) FILTER (WHERE …)`` is the cleanest way to
    // count wins separately without two scans.
    let rows = client
        .query(
            "SELECT
                p.id,
                COUNT(*) FILTER (WHERE i.preferred_id = p.id) AS wins,
                COUNT(*) AS total
             FROM (
                SELECT DISTINCT compared_id AS id FROM interactions
                UNION
                SELECT DISTINCT preferred_id FROM interactions
             ) p
             LEFT JOIN interactions i
                ON i.compared_id = p.id OR i.preferred_id = p.id
             GROUP BY p.id",
            &[],
        )
        .await
        .with_context(|| "aggregate interactions for win rates")?;

    let mut out: FxHashMap<String, f64> = FxHashMap::default();
    for row in rows {
        let id: String = row.get(0);
        let wins: i64 = row.get(1);
        let total: i64 = row.get(2);
        if total > 0 {
            out.insert(id, wins as f64 / total as f64);
        }
    }
    Ok(out)
}
