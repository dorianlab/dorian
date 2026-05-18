//! Postgres-side query for "every (pipeline, dataset) pair that
//! hasn't been evaluated yet". Implemented as a parameterised
//! query rather than a materialised view so it stays trivially
//! consistent with concurrent inserts on `evaluations` (no
//! refresh lag, no DDL coupling).
//!
//! Uses NOT EXISTS rather than LEFT JOIN ... IS NULL because
//! postgres optimises the former into an anti-semijoin that
//! short-circuits as soon as any matching row is found — far
//! cheaper on tables with many evaluations per pair.

use deadpool_postgres::Pool;

#[derive(Debug, Clone)]
pub struct Pair {
    pub pipeline_id: String,
    pub dataset_id: String,
    /// Filesystem path of the dataset's CSV. Read from
    /// `datasets.storage->location->>path`. The runner needs this
    /// to expand `dorian.io.dataset`; without it every xproduct
    /// trial fails at expansion with "please upload a dataset" and
    /// floods Slack via the error notifier.
    pub dataset_path: Option<String>,
    /// Task type ("classification", "regression", …) read from
    /// `datasets.task->>type`. Plumbed into session_meta so the
    /// runner's evaluation procedure resolver can pick the right
    /// metric set.
    pub task_type: Option<String>,
    /// Feature column names. Planted into
    /// `dataset:{did}:feature_columns` Redis key so
    /// `dorian.io.state[dataset.features]` resolves.
    pub feature_cols: Option<serde_json::Value>,
    pub target_cols: Option<serde_json::Value>,
}

pub struct PairsToComplete<'a> {
    pool: &'a Pool,
}

impl<'a> PairsToComplete<'a> {
    pub fn new(pool: &'a Pool) -> Self {
        Self { pool }
    }

    /// Return up to `limit` pairs that have at least one row in
    /// `pipelines` × `datasets` but no row in `evaluations`.
    /// Sorted by `(pipelines.created_at DESC, datasets.created_at
    /// DESC)` so freshly-arrived pipelines / datasets fill first
    /// — keeps the engine responsive when new artefacts land.
    pub async fn fetch(&self, limit: i64) -> anyhow::Result<Vec<Pair>> {
        let conn = self.pool.get().await?;
        let rows = conn
            .query(
                r#"
                SELECT p.id AS pipeline_id,
                       d.id AS dataset_id,
                       d.storage->'location'->>'path' AS dataset_path,
                       d.task->>'type' AS task_type,
                       d.columns->'features' AS feature_cols,
                       d.columns->'targets'  AS target_cols
                FROM pipelines p
                CROSS JOIN datasets d
                  -- Skip pairs that already produced a successful
                  -- evaluation. ``status != 'failed'`` covers
                  -- ``success``/``timeout``/``cancelled`` — anything
                  -- that isn't the synthetic failure sentinel that
                  -- pattern-gating wants to re-evaluate.
                WHERE NOT EXISTS (
                    SELECT 1 FROM evaluations e
                    WHERE e.pipeline_id = p.id
                      AND e.dataset_id  = d.id
                      AND e.status != 'failed'
                )
                  AND d.storage->'location'->>'path' IS NOT NULL
                  -- Skip pipelines that contain unbound LogicalTask
                  -- placeholder nodes — those are AutoML templates, not
                  -- runnable artefacts. Submitting them through xproduct
                  -- floods the runner with "Cycle detected" / "cannot
                  -- expand" failures because the materialiser only fires
                  -- on the AutoML driver's submit path.
                  AND NOT jsonb_path_exists(
                      p.dag, '$.nodes.* ? (@.class_type == "LogicalTask")'
                  )
                  -- Skip rl_auto_mitigation rewrites that haven't already
                  -- produced a successful evaluation. The
                  -- ``apply_structural_rewrite_to_dag`` rule for
                  -- categorical encoders has historically generated
                  -- cyclic DAGs, and we don't want to thrash on those
                  -- forever. ``status = 'success'`` is the explicit
                  -- gate now that ``evaluations`` carries the column
                  -- (see ``dorian/experiment/schema.py`` ALTER TABLE).
                  AND (p.provenance != 'rl_auto_mitigation'
                       OR p.id IN (SELECT DISTINCT pipeline_id FROM evaluations
                                   WHERE status = 'success'))
                  -- Pattern-gated retry of failed pairs (Phase 2 of
                  -- (internal design note; not in public repo)). Skip if a failure
                  -- exists AND its ``pattern_id`` is null OR the
                  -- pattern is still ``active = true``. A
                  -- ``MitigationRewriteApplied`` event flips the
                  -- pattern to inactive once a curated rewrite
                  -- lands; on the next tick this gate stops firing
                  -- and the pair re-enters the queue.
                  --
                  -- Two-tier semantics:
                  --   * pattern_id NULL → forever-gated (no theory
                  --     of fix)
                  --   * pattern_id set, active=true → gated
                  --   * pattern_id set, active=false → re-runnable
                  AND NOT EXISTS (
                      SELECT 1 FROM evaluations f
                      LEFT JOIN exception_patterns ep
                          ON ep.id = f.eval_config->>'pattern_id'
                      WHERE f.pipeline_id = p.id
                        AND f.dataset_id  = d.id
                        AND f.status = 'failed'
                        AND (
                            f.eval_config->>'pattern_id' IS NULL
                            OR ep.active = TRUE
                            OR ep.id IS NULL  -- pattern referenced but row missing
                        )
                  )
                ORDER BY p.created_at DESC, d.created_at DESC
                LIMIT $1
                "#,
                &[&limit],
            )
            .await?;
        Ok(rows
            .into_iter()
            .map(|r| Pair {
                pipeline_id: r.get("pipeline_id"),
                dataset_id: r.get("dataset_id"),
                dataset_path: r.try_get("dataset_path").ok(),
                task_type: r.try_get("task_type").ok(),
                feature_cols: r.try_get("feature_cols").ok(),
                target_cols: r.try_get("target_cols").ok(),
            })
            .collect())
    }

    /// Count of uncovered pairs. Cheap (uses the same indexed
    /// query as `fetch` minus the row materialisation). Surfaced
    /// for telemetry — operators can watch the gap shrink as the
    /// engine catches up.
    pub async fn count(&self) -> anyhow::Result<i64> {
        let conn = self.pool.get().await?;
        let row = conn
            .query_one(
                r#"
                SELECT COUNT(*)::bigint AS n
                FROM pipelines p
                CROSS JOIN datasets d
                WHERE NOT EXISTS (
                    SELECT 1 FROM evaluations e
                    WHERE e.pipeline_id = p.id
                      AND e.dataset_id  = d.id
                )
                  AND d.storage->'location'->>'path' IS NOT NULL
                  AND NOT jsonb_path_exists(
                      p.dag, '$.nodes.* ? (@.class_type == "LogicalTask")'
                  )
                  AND (p.provenance != 'rl_auto_mitigation'
                       OR p.id IN (SELECT DISTINCT pipeline_id FROM evaluations))
                "#,
                &[],
            )
            .await?;
        Ok(row.get("n"))
    }
}
