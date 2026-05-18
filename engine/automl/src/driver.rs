//! AutoML driver loop — turns the BO library into a running engine.
//!
//! Lifecycle (one tick per `poll_interval`):
//!
//!   1. **Discover templates.** Scan `pipelines` table for DAGs
//!      containing `LogicalTask` placeholder nodes. Templates are
//!      what AutoML optimises; concrete pipelines are what RL / users
//!      run directly. (No `is_template` column yet — we filter the
//!      JSONB; future: add a generated column.)
//!   2. **Pick (template, dataset) pairs to optimise.** Up to a
//!      configurable budget: prefer pairs with the fewest trials
//!      so far, weighted by recency of dataset arrival.
//!   3. **Build the search space.** For each LogicalTask in the
//!      template, query the KB for operator candidates and their
//!      parameter domains. Convert KB `ParameterSpec` records into
//!      AutoML `ParamDomain` (Float/Int/Categorical/Bool) values.
//!   4. **Warm-start the optimizer.** Pull the (possibly empty)
//!      history of `Trial`s for this (template, dataset) pair from
//!      `evaluations` and feed them to `SmacOptimizer::warm_start`.
//!   5. **Ask K configs.** SMAC's RF surrogate + EI ranks
//!      candidates; the driver materialises each into a concrete
//!      pipeline by replacing every `LogicalTask` node with the
//!      chosen `Operator` + its hyperparameter `Parameter`s.
//!   6. **Submit each materialised pipeline to `task_queue`.** Same
//!      envelope shape `xproduct` uses — bridge worker dispatches
//!      it through the standard execution path.
//!   7. **(Async) ingest results.** A separate poll reads new
//!      `evaluations` rows tagged `source='automl'` and feeds them
//!      back via `Optimizer::tell` so the next ask is informed.
//!
//! v1 ships steps 1-3 + a placeholder for 4-7. Stage 2 wires the
//! materialiser + submission. Stage 3 wires the ingestion poll.

use std::sync::Arc;
use std::time::Duration;

use deadpool_postgres::{Manager, Pool};
use redis::AsyncCommands;
use rustc_hash::FxHashMap;
use tokio_postgres::NoTls;
use tracing::{debug, info, warn};
use uuid::Uuid;

use crate::config::{Bounds, Choice, ParamDomain, ParamValue};
use crate::optimizer::{OperatorCandidate, Optimizer, SlotSpec, Suggestion};
use optimizer::kb::{KbSnapshot, ParameterSpec};

/// Driver-side configuration. Read once from env at startup.
#[derive(Debug, Clone)]
pub struct DriverConfig {
    pub postgres_url: String,
    pub redis_url: String,
    pub queue_key: String,
    pub poll_interval: Duration,
    /// Max (template, dataset) pairs to start per tick. Caps the
    /// per-cycle BO budget.
    pub max_starts_per_tick: usize,
    /// How many configs to ask SMAC for per started pair.
    pub ask_batch_size: usize,
    /// Below this number of trials per (template, dataset), we keep
    /// optimising. Above, the driver moves on.
    pub trials_per_pair_target: usize,
    /// Path to the rust KB snapshot JSON. The supervisor binary
    /// expects this to exist at startup; the driver loads it once
    /// and reuses it across ticks.
    pub kb_snapshot_path: Option<String>,
    /// Pause submission when `task_queue` ZCARD exceeds this. Keeps
    /// the engine from outrunning the consumer when the runner can't
    /// keep up — without this, the queue grows unboundedly and Redis
    /// memory pressure mounts.
    pub queue_high_watermark: i64,
    /// Resume submission once depth drops back below this (sticky
    /// hysteresis around the high-watermark prevents flapping at the
    /// exact threshold).
    pub queue_low_watermark: i64,
}

impl DriverConfig {
    pub fn from_env() -> anyhow::Result<Self> {
        let postgres_url = match std::env::var("DORIAN_POSTGRES_URL") {
            Ok(s) if !s.trim().is_empty() => s,
            _ => assemble_postgres_url()?,
        };
        let redis_url = std::env::var("DORIAN_REDIS_URL")
            .unwrap_or_else(|_| "redis://redis:6379".into());
        let queue_key = std::env::var("DORIAN_AUTOML_QUEUE")
            .unwrap_or_else(|_| "task_queue".into());
        let poll_interval = parse_secs("DORIAN_AUTOML_POLL_SECS", 60);
        let max_starts_per_tick =
            parse_usize("DORIAN_AUTOML_STARTS_PER_TICK", 4);
        let ask_batch_size = parse_usize("DORIAN_AUTOML_ASK_K", 4);
        let trials_per_pair_target =
            parse_usize("DORIAN_AUTOML_TRIALS_PER_PAIR", 25);
        let kb_snapshot_path = std::env::var("DORIAN_KB_SNAPSHOT")
            .ok()
            .filter(|s| !s.is_empty());
        let queue_high_watermark = std::env::var("DORIAN_QUEUE_HIGH_WATERMARK")
            .ok()
            .and_then(|v| v.parse::<i64>().ok())
            .unwrap_or(500);
        let queue_low_watermark = std::env::var("DORIAN_QUEUE_LOW_WATERMARK")
            .ok()
            .and_then(|v| v.parse::<i64>().ok())
            .unwrap_or(100);
        Ok(DriverConfig {
            postgres_url, redis_url, queue_key, poll_interval,
            max_starts_per_tick, ask_batch_size, trials_per_pair_target,
            kb_snapshot_path,
            queue_high_watermark, queue_low_watermark,
        })
    }
}

fn assemble_postgres_url() -> anyhow::Result<String> {
    let host = std::env::var("DORIAN_POSTGRES_HOST")
        .unwrap_or_else(|_| "postgres".into());
    let port = std::env::var("DORIAN_POSTGRES_PORT")
        .unwrap_or_else(|_| "5432".into());
    let user = std::env::var("DORIAN_POSTGRES_USER")
        .unwrap_or_else(|_| "dorian".into());
    let password = std::env::var("DORIAN_POSTGRES_PASSWORD")
        .map_err(|_| anyhow::anyhow!("DORIAN_POSTGRES_PASSWORD must be set"))?;
    let database = std::env::var("DORIAN_POSTGRES_DATABASE")
        .unwrap_or_else(|_| "dorian".into());
    Ok(format!(
        "postgresql://{user}:{password}@{host}:{port}/{database}"
    ))
}

fn parse_secs(var: &str, default_secs: u64) -> Duration {
    std::env::var(var)
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or_else(|| Duration::from_secs(default_secs))
}

fn parse_usize(var: &str, default: usize) -> usize {
    std::env::var(var)
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(default)
}

/// First-letter capitalisation, ASCII-only. Used to convert
/// `datasets.task->>type` ("classification") into the runner's
/// expected display form ("Classification") for
/// ``selectedDataScienceTask.name``.
fn capitalise(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => first.to_ascii_uppercase().to_string() + chars.as_str(),
    }
}

// ---------------------------------------------------------------------------
// Template + slot discovery
// ---------------------------------------------------------------------------

/// One template-on-dataset pair the driver should optimise.
#[derive(Debug, Clone)]
pub struct TemplateTarget {
    pub template_id: String,
    pub template_dag: serde_json::Value,
    pub dataset_id: String,
    /// Filesystem path the runner's loader operator opens. Read from
    /// `datasets.storage->location->>path`. Always present (the
    /// discover query filters out rows where it's NULL).
    pub dataset_path: String,
    /// Feature column names (JSON array). Planted into the
    /// `dataset:{did}:feature_columns` Redis key at submit time so
    /// `dorian.io.state[dataset.features]` can resolve.
    pub feature_cols: serde_json::Value,
    /// Target column names (JSON array). Mirrors above for
    /// `dataset.target` resolution.
    pub target_cols: serde_json::Value,
    /// Data-science task type (e.g. "classification"). Read from
    /// `datasets.task->>type`. Planted into session_meta as
    /// `selectedDataScienceTask.name` (capitalised) so the
    /// runner's evaluation procedure can resolve which metrics
    /// to compute (`get_metrics_for_task`). Without it,
    /// `_evaluate_pipeline_sync` returns `{}` and the run completes
    /// with no metrics recorded — surrogate gets no learning signal.
    pub task_type: String,
    pub trials_so_far: i64,
}

/// Walk the template's `nodes` map and surface every LogicalTask
/// node by `(node_id, canonical_path_dotted)`.
pub fn logical_task_slots(dag: &serde_json::Value) -> Vec<(String, String)> {
    let nodes = match dag.get("nodes").and_then(|v| v.as_object()) {
        Some(n) => n,
        None => return Vec::new(),
    };
    let mut out = Vec::new();
    for (node_id, node) in nodes {
        if node.get("class_type").and_then(|v| v.as_str()) != Some("LogicalTask") {
            continue;
        }
        let path: Vec<String> = node
            .get("path")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|p| p.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default();
        out.push((node_id.clone(), path.join(".")));
    }
    out
}

/// Convert KB `ParameterSpec` → AutoML `ParamDomain`. Falls back
/// to `ParamDomain::Constant` when the KB row doesn't carry enough
/// metadata to materialise a search range — that operator's slot
/// stays at the default value.
pub fn param_spec_to_domain(spec: &ParameterSpec) -> ParamDomain {
    match spec.dtype.as_str() {
        "float" | "Float" => match (spec.low, spec.high) {
            (Some(lo), Some(hi)) if hi > lo => {
                ParamDomain::Float(Bounds {
                    low: lo, high: hi,
                    log_scale: spec.log_scale.unwrap_or(false),
                })
            }
            _ => ParamDomain::Constant(default_param_value(spec)),
        },
        "int" | "Int" | "integer" => match (spec.low, spec.high) {
            (Some(lo), Some(hi)) if hi > lo => {
                ParamDomain::Int(Bounds {
                    low: lo as i64, high: hi as i64, log_scale: false,
                })
            }
            _ => ParamDomain::Constant(default_param_value(spec)),
        },
        "bool" | "Bool" => ParamDomain::Bool,
        "categorical" | "Categorical" | "string" | "String" => {
            match &spec.choices {
                Some(opts) if !opts.is_empty() => {
                    let default_idx = spec.default.as_ref().and_then(|d| {
                        opts.iter().position(|o| o == d)
                    });
                    ParamDomain::Categorical(Choice {
                        options: opts.clone(),
                        default: default_idx,
                    })
                }
                _ => ParamDomain::Constant(default_param_value(spec)),
            }
        }
        _ => ParamDomain::Constant(default_param_value(spec)),
    }
}

fn default_param_value(spec: &ParameterSpec) -> ParamValue {
    let v = spec.default.clone().unwrap_or_default();
    match spec.dtype.as_str() {
        "int" | "Int" | "integer" => v
            .parse::<i64>().map(ParamValue::Int)
            .unwrap_or_else(|_| ParamValue::Str(v)),
        "float" | "Float" => v
            .parse::<f64>().map(ParamValue::Float)
            .unwrap_or_else(|_| ParamValue::Str(v)),
        "bool" | "Bool" => match v.to_lowercase().as_str() {
            "true" => ParamValue::Bool(true),
            "false" => ParamValue::Bool(false),
            _ => ParamValue::Str(v),
        },
        _ => ParamValue::Str(v),
    }
}

/// Build a `SlotSpec` for one logical task path by querying the
/// KB. Returns None when the KB has no operators for the path
/// (typo in template? out-of-date snapshot?).
///
/// Skippable preprocessing slots get a synthetic `__identity__`
/// candidate appended so the optimizer can choose to bypass that
/// step entirely. The materialiser detects `op_fqn == "__identity__"`
/// and rewires around the LogicalTask instead of binding it to a
/// concrete operator. Mandatory slots (estimator, etc.) skip this
/// extension so the search space stays correct.
pub fn slot_from_kb(
    task_path: &str, kb: &KbSnapshot,
) -> Option<SlotSpec> {
    // KB stores task names by leaf — try the dotted path first
    // then fall back to the leaf component for hierarchical
    // matches.
    let leaf = task_path.rsplit('.').next().unwrap_or(task_path);
    let mut fqns = kb.operators_for_task(task_path);
    if fqns.is_empty() && leaf != task_path {
        fqns = kb.operators_for_task(leaf);
    }
    if fqns.is_empty() {
        return None;
    }
    let mut candidates: Vec<OperatorCandidate> = fqns
        .into_iter()
        .map(|fqn| {
            let params: Vec<(String, ParamDomain)> = kb
                .operator_parameters(&fqn)
                .into_iter()
                .map(|spec| (spec.name.clone(), param_spec_to_domain(&spec)))
                .collect();
            OperatorCandidate { op_fqn: fqn, params }
        })
        .collect();
    if is_skippable_task(task_path) || is_skippable_task(leaf) {
        candidates.push(OperatorCandidate {
            op_fqn: IDENTITY_OP_FQN.to_string(),
            params: Vec::new(),
        });
    }
    Some(SlotSpec {
        task_path: task_path.to_string(),
        candidates,
    })
}

/// Sentinel FQN the optimizer picks when it wants to skip a slot.
/// The materialiser treats this as a no-op: drop the LogicalTask and
/// rewire its incoming data edge to whatever the slot's outgoing
/// edges target. Pure preprocessing only — `Classification` etc.
/// are mandatory and won't get this candidate appended.
pub const IDENTITY_OP_FQN: &str = "__identity__";

/// Tasks where the optimizer is allowed to choose "skip this step".
/// Mirrors the user-facing model: imputers / scalers / encoders are
/// optional preprocessing and the BO search includes the identity
/// option to let the surrogate decide whether the step helps. Tasks
/// not on this list (Classification, Regression) are mandatory.
fn is_skippable_task(name: &str) -> bool {
    matches!(
        name,
        "Missing Data Imputation"
            | "Data Normalization"
            | "Data Encoding"
            | "Feature Selection"
            | "Dimensionality Reduction"
    )
}

// ---------------------------------------------------------------------------
// Driver supervisor
// ---------------------------------------------------------------------------

/// AutoML driver state. Single instance per supervisor process.
pub struct Driver {
    cfg: DriverConfig,
    pool: Pool,
    redis: redis::Client,
    /// KB snapshot for operator-candidate enumeration. Loaded once
    /// at startup; ticks reuse it.
    kb: Arc<KbSnapshot>,
    /// Per-(template, dataset) optimizer instances. Keeps the
    /// surrogate warm across ticks — same template's BO history
    /// accumulates rather than restarting on every poll.
    optimizers: tokio::sync::Mutex<FxHashMap<(String, String), crate::SmacOptimizer>>,
    /// In-flight trial registry: concrete `pipeline_id` →
    /// `(template_id, dataset_id, Suggestion)`. Populated when the
    /// driver submits a trial; consulted by the ingest loop when
    /// matching evaluations rows back to the optimizer that asked
    /// for the config. Cleared after the result is fed to tell().
    in_flight: tokio::sync::Mutex<FxHashMap<String, InFlightTrial>>,
    /// Last-seen evaluations.created_at watermark for the ingest
    /// poll. Filters the SELECT so we don't re-feed the surrogate
    /// the same trial on every tick.
    ingest_watermark: tokio::sync::Mutex<chrono::DateTime<chrono::Utc>>,
    /// Backpressure flag. Toggled true once `task_queue` exceeds
    /// `queue_high_watermark`; toggled back false once depth drops
    /// to `queue_low_watermark`. While paused the driver still
    /// ingests results but skips ask + submit, so completed trials
    /// drain the queue and bring it back below the low water mark.
    paused: std::sync::atomic::AtomicBool,
}

#[derive(Debug, Clone)]
struct InFlightTrial {
    template_id: String,
    dataset_id: String,
    suggestion: Suggestion,
}

impl Driver {
    pub async fn new(cfg: DriverConfig) -> anyhow::Result<Self> {
        let pg_cfg: tokio_postgres::Config = cfg.postgres_url.parse()?;
        let mgr = Manager::new(pg_cfg, NoTls);
        let pool = Pool::builder(mgr).max_size(2).build()?;
        let redis_client = redis::Client::open(cfg.redis_url.clone())?;
        // Load the KB snapshot. Without it, slot enumeration falls
        // back to "no candidates" and the driver becomes a no-op.
        let kb = match cfg.kb_snapshot_path.as_deref() {
            Some(path) => {
                let body = std::fs::read_to_string(path)
                    .map_err(|e| anyhow::anyhow!("failed to read KB snapshot at {path}: {e}"))?;
                Arc::new(KbSnapshot::from_json(&body)
                    .map_err(|e| anyhow::anyhow!("KB snapshot parse: {e}"))?)
            }
            None => {
                warn!("automl: DORIAN_KB_SNAPSHOT not set — driver will run with empty KB and skip every target");
                Arc::new(KbSnapshot::default())
            }
        };
        Ok(Driver {
            cfg, pool,
            redis: redis_client,
            kb,
            optimizers: tokio::sync::Mutex::new(FxHashMap::default()),
            in_flight: tokio::sync::Mutex::new(FxHashMap::default()),
            // Start at unix epoch — the first tick picks up every
            // existing automl-source eval row (intentional cold-
            // start warm-up of the surrogate from any history that
            // accumulated while the driver was offline).
            ingest_watermark: tokio::sync::Mutex::new(
                chrono::DateTime::<chrono::Utc>::from_timestamp(0, 0)
                    .unwrap_or_else(chrono::Utc::now),
            ),
            paused: std::sync::atomic::AtomicBool::new(false),
        })
    }

    /// Drive forever. Each tick: discover targets, pick top-N, run
    /// one BO iteration per target.
    pub async fn run(self) -> anyhow::Result<()> {
        info!(
            poll = ?self.cfg.poll_interval,
            starts_per_tick = self.cfg.max_starts_per_tick,
            ask_k = self.cfg.ask_batch_size,
            "automl driver starting"
        );
        let mut ticker = tokio::time::interval(self.cfg.poll_interval);
        loop {
            ticker.tick().await;
            if let Err(e) = self.tick().await {
                // ``?e`` walks the anyhow error chain — tokio-postgres
                // bare-string ``"db error"`` is uselessly opaque under
                // ``%e``.
                warn!(error = ?e, "automl tick failed");
            }
        }
    }

    /// One tick of the driver loop:
    /// 1. Ingest any newly-completed trial results into the
    ///    matching optimizer's `tell()` so the next ask uses fresh
    ///    surrogate state.
    /// 2. Discover (template, dataset) pairs needing trials and
    ///    ask K configs each, materialise + submit.
    async fn tick(&self) -> anyhow::Result<usize> {
        // 1. Ingest first so the surrogate is up-to-date before we
        // ask for the next batch.
        let ingested = match self.ingest_completed_trials().await {
            Ok(n) => n,
            Err(e) => {
                warn!(error = ?e, "automl: ingest failed");
                0
            }
        };
        if ingested > 0 {
            info!(ingested, "automl: trials told to surrogate");
        }

        // 2. Backpressure: probe the queue depth and toggle the
        // paused flag with hysteresis. While paused, we skip
        // discover/submit but keep ingesting (so results draining
        // the queue can flip us back to active).
        match self.queue_depth().await {
            Ok(depth) => {
                use std::sync::atomic::Ordering;
                let was_paused = self.paused.load(Ordering::Relaxed);
                if !was_paused && depth >= self.cfg.queue_high_watermark {
                    self.paused.store(true, Ordering::Relaxed);
                    warn!(
                        depth,
                        high = self.cfg.queue_high_watermark,
                        "automl: queue full — pausing submissions until drained"
                    );
                } else if was_paused && depth <= self.cfg.queue_low_watermark {
                    self.paused.store(false, Ordering::Relaxed);
                    info!(
                        depth,
                        low = self.cfg.queue_low_watermark,
                        "automl: queue drained — resuming submissions"
                    );
                }
            }
            Err(e) => warn!(error = %e, "automl: queue depth probe failed"),
        }
        if self.paused.load(std::sync::atomic::Ordering::Relaxed) {
            return Ok(0);
        }

        // 3. Discover targets + submit.
        let targets = self.discover_targets(self.cfg.max_starts_per_tick as i64 * 2).await?;
        if targets.is_empty() {
            debug!("automl: no template targets to optimise this tick");
            return Ok(0);
        }
        let mut submitted = 0;
        for target in targets.into_iter().take(self.cfg.max_starts_per_tick) {
            match self.optimise_one(&target).await {
                Ok(n) => submitted += n,
                Err(e) => warn!(
                    template_id = %target.template_id,
                    dataset_id = %target.dataset_id,
                    error = %e,
                    "automl: target optimise failed"
                ),
            }
        }
        if submitted > 0 {
            info!(submitted, "automl tick");
        }
        Ok(submitted)
    }

    /// Probe `task_queue` ZCARD. Used by the backpressure check
    /// each tick to decide whether to pause submissions. A failure
    /// returns the error so the tick logs it but otherwise treats
    /// "unknown depth" as not-overloaded (errs on the side of
    /// continuing to submit if Redis is briefly unreachable —
    /// pausing on a transient error would stall progress).
    async fn queue_depth(&self) -> anyhow::Result<i64> {
        let mut conn = self.redis.get_multiplexed_async_connection().await?;
        let depth: i64 = redis::cmd("ZCARD")
            .arg(&self.cfg.queue_key)
            .query_async(&mut conn)
            .await?;
        Ok(depth)
    }

    /// Read any newly-completed automl-source trials from
    /// `evaluations`, match each against the in-flight tracking
    /// map, and feed the result to the right optimizer's tell().
    /// Updates the watermark so the next tick only sees fresh
    /// rows.
    ///
    /// Crash-safety: if the driver dies between submit and ingest,
    /// the in-flight map is empty on restart and we silently miss
    /// telling those trials. Trials that match by run_id can still
    /// be discovered via doc_pipelines (pipeline_doc carries the
    /// template_id); the suggestion-config can be reconstructed
    /// from the bound DAG. v2 ships that recovery path; v1 accepts
    /// the rare data-loss window in exchange for simpler state.
    async fn ingest_completed_trials(&self) -> anyhow::Result<usize> {
        let watermark = *self.ingest_watermark.lock().await;
        let conn = self.pool.get().await?;
        // Pull the primary metric per (pipeline_id, run_id) — we
        // collapse multi-metric trials into one row by taking the
        // first metric in the lexically-sorted order. The optimizer
        // only learns from one scalar; downstream consumers
        // (leaderboard) read the full evaluations rows directly.
        let rows = conn
            .query(
                r#"
                SELECT pipeline_id, dataset_id, run_id, status,
                       wall_clock_s, error_message,
                       (SELECT metric_value FROM evaluations e2
                        WHERE e2.pipeline_id = e.pipeline_id
                          AND e2.run_id = e.run_id
                          AND e2.metric_name <> '__failed__'
                          AND e2.metric_value = e2.metric_value
                        ORDER BY metric_name LIMIT 1) AS primary_metric,
                       MAX(created_at) AS latest_at
                FROM evaluations e
                WHERE source = 'automl'
                  AND created_at > $1
                GROUP BY pipeline_id, dataset_id, run_id, status, wall_clock_s, error_message
                ORDER BY latest_at ASC
                LIMIT 256
                "#,
                &[&watermark],
            )
            .await?;

        let mut new_watermark = watermark;
        let mut fed = 0usize;

        // Group telling by (template_id, dataset_id) so a single
        // tell() call can absorb multiple trials sharing a target.
        let mut buckets: FxHashMap<(String, String), Vec<crate::Trial>> = FxHashMap::default();
        let mut to_clear: Vec<String> = Vec::new();
        {
            let mut in_flight = self.in_flight.lock().await;
            for row in &rows {
                let pipeline_id: String = row.get("pipeline_id");
                let info = match in_flight.remove(&pipeline_id) {
                    Some(v) => v,
                    None => continue, // not ours / already told
                };
                let status: String = row.get("status");
                let metric: Option<f64> = row.try_get("primary_metric").ok();
                let dataset_id: String = row.get("dataset_id");
                let run_id: String = row.get("run_id");
                let wall_clock_s: Option<f64> = row.try_get("wall_clock_s").ok();
                let err: Option<String> = row.try_get("error_message").ok();
                let latest: chrono::DateTime<chrono::Utc> = row.get("latest_at");
                if latest > new_watermark {
                    new_watermark = latest;
                }
                let mut metrics: FxHashMap<String, f64> = FxHashMap::default();
                if let Some(v) = metric {
                    if v.is_finite() {
                        metrics.insert("primary".into(), v);
                    }
                }
                let trial = crate::Trial {
                    pipeline_id: pipeline_id.clone(),
                    dataset_id,
                    run_id,
                    source: "automl".into(),
                    status,
                    metrics,
                    config: serde_json::json!({"bindings": suggestion_to_bindings(&info.suggestion)}),
                    eval_config: None,
                    wall_clock_s,
                    error_message: err,
                };
                buckets
                    .entry((info.template_id.clone(), info.dataset_id.clone()))
                    .or_default()
                    .push(trial);
                to_clear.push(pipeline_id);
            }
        }

        // Tell each (template, dataset) optimizer in one batch.
        if !buckets.is_empty() {
            let mut opts = self.optimizers.lock().await;
            for (key, trials) in buckets {
                let opt = opts
                    .entry(key)
                    .or_insert_with(crate::SmacOptimizer::new);
                opt.tell(&trials);
                fed += trials.len();
            }
        }

        // Advance the watermark so we don't re-feed the same rows.
        if new_watermark > watermark {
            *self.ingest_watermark.lock().await = new_watermark;
        }
        Ok(fed)
    }

    /// Run one BO iteration on one (template, dataset) pair: build
    /// SlotSpecs from KB, ask K configs, materialise each into a
    /// concrete pipeline, save it, and submit on task_queue.
    async fn optimise_one(&self, target: &TemplateTarget) -> anyhow::Result<usize> {
        // Step 1: extract slot paths from the template DAG.
        let slot_pairs = logical_task_slots(&target.template_dag);
        if slot_pairs.is_empty() {
            return Ok(0); // somehow not a template — skip
        }

        // Step 2: build SlotSpec per logical task, querying KB for
        // candidate operators + their parameter domains. Skip the
        // target if any slot has no candidates (KB out-of-date or
        // template references a task the catalogue doesn't expose).
        let mut slots: Vec<SlotSpec> = Vec::with_capacity(slot_pairs.len());
        for (_node_id, task_path) in &slot_pairs {
            match slot_from_kb(task_path, &self.kb) {
                Some(s) => slots.push(s),
                None => {
                    debug!(
                        template_id = %target.template_id,
                        task_path = %task_path,
                        "automl: KB has no operators for slot — skipping target"
                    );
                    return Ok(0);
                }
            }
        }

        // Step 3: get-or-create the per-pair optimizer + ask K.
        let key = (target.template_id.clone(), target.dataset_id.clone());
        let suggestions: Vec<Suggestion> = {
            let mut opts = self.optimizers.lock().await;
            let opt = opts.entry(key).or_insert_with(crate::SmacOptimizer::new);
            opt.ask(&slots, self.cfg.ask_batch_size)
        };

        // Step 4: materialise + persist + enqueue each suggestion.
        let mut submitted = 0;
        for suggestion in suggestions {
            match self.submit_trial(target, &suggestion).await {
                Ok(()) => submitted += 1,
                Err(e) => warn!(
                    template_id = %target.template_id,
                    error = %e,
                    "automl: submit_trial failed"
                ),
            }
        }
        Ok(submitted)
    }

    async fn submit_trial(
        &self, target: &TemplateTarget, suggestion: &Suggestion,
    ) -> anyhow::Result<()> {
        // Materialise the suggestion into a concrete DAG.
        let bound_dag = crate::materialise::materialise(&target.template_dag, suggestion)
            .map_err(|e| anyhow::anyhow!("materialise: {e}"))?;
        let pipeline_id = format!("automl-{}", Uuid::new_v4());
        let run_id = format!("automl-{}", Uuid::new_v4());

        // Register in-flight before the row hits the queue. If
        // we crash between persist + enqueue and ingest, the entry
        // simply ages out — the worst case is the surrogate misses
        // one trial's signal, not a runtime fault.
        {
            let mut in_flight = self.in_flight.lock().await;
            in_flight.insert(
                pipeline_id.clone(),
                InFlightTrial {
                    template_id: target.template_id.clone(),
                    dataset_id: target.dataset_id.clone(),
                    suggestion: suggestion.clone(),
                },
            );
        }

        // Persist the bound pipeline to doc_pipelines so the
        // bridge worker can find it by id when the trial pops off
        // task_queue. The trigger mirrors it into the typed
        // `pipelines` table for the BK-Tree / cross-product engine.
        let conn = self.pool.get().await?;
        let pipeline_doc = serde_json::json!({
            "_id": pipeline_id,
            "task": "classification",
            "nodes": bound_dag.get("nodes").cloned().unwrap_or(serde_json::json!({})),
            "edges": bound_dag.get("edges").cloned().unwrap_or(serde_json::json!([])),
            "provenance": "automl",
            "source": "automl",
            "template_id": target.template_id,
        });
        // Pass the JSON as `serde_json::Value` so tokio-postgres'
        // `with-serde_json-1` codec serialises it directly into the
        // `jsonb` column. Sending a String here triggers
        // "error serializing parameter 1" because postgres prepares
        // $2 as `jsonb` from the column type and `String`'s ToSql
        // only emits `text`.
        conn.execute(
            r#"
            INSERT INTO doc_pipelines (id, data, created_at, updated_at)
            VALUES ($1, $2, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
            "#,
            &[&pipeline_id, &pipeline_doc],
        ).await?;

        // Seed the synthetic session's meta in Redis BEFORE enqueue
        // so `handle_pipeline_execution` can resolve the dataset
        // when it pops the envelope. The runner reads
        // `dataset.fpath` out of `session:{session}:meta` to expand
        // `dorian.io.dataset` into a concrete loader chain — without
        // this, the post-expansion guard fails the trial with
        // "please upload a dataset before running the pipeline".
        let session = format!("automl:{run_id}");
        let meta_key = format!("session:{session}:meta");
        // Resolve dataset path against `/app/data/` (the runner's
        // canonical data root inside the backend container) when the
        // stored path is relative. Absolute paths pass through.
        let fpath = if target.dataset_path.starts_with('/') {
            target.dataset_path.clone()
        } else {
            format!("/app/data/{}", target.dataset_path)
        };
        // ``selectedDataScienceTask`` is what the runner's
        // ``_evaluate_pipeline_sync`` reads to resolve which metrics
        // (accuracy, f1, …) to compute via the KB's
        // ``get_metrics_for_task``. Without it, evaluation silently
        // returns an empty dict and the surrogate never gets a score.
        // The python ``_ensure_synthetic_session_dataset`` helper does
        // the same capitalise + populate dance for RL sessions.
        let task_name = capitalise(&target.task_type);
        let meta_payload = serde_json::json!({
            "dataset": {
                "did": target.dataset_id,
                "fpath": fpath,
                "mime": "text/csv",
            },
            "selectedDataScienceTask": {
                "id": null,
                "name": task_name,
            },
            "uid": "automl",
            "session": session,
        }).to_string();
        let mut rconn = self.redis.get_multiplexed_async_connection().await?;
        // 1-hour TTL so abandoned meta doesn't leak indefinitely.
        let _: () = redis::cmd("SET")
            .arg(&meta_key)
            .arg(&meta_payload)
            .arg("EX")
            .arg(3600_i64)
            .query_async(&mut rconn)
            .await?;

        // Plant the per-dataset column-name lists the runner's
        // ``dorian.io.state`` resolver reads. Without these,
        // ``state.expand`` returns None and ``project_columns``
        // sees the literal string ``"dataset.features"`` as its
        // ``columns`` kwarg, then fails with KeyError. The keys
        // persist (no TTL) since they're dataset-scoped — multiple
        // trials for the same dataset all read the same value.
        let feat_key = format!("dataset:{}:feature_columns", target.dataset_id);
        let tgt_key = format!("dataset:{}:target_columns", target.dataset_id);
        let _: () = redis::cmd("SET")
            .arg(&feat_key)
            .arg(target.feature_cols.to_string())
            .query_async(&mut rconn)
            .await?;
        let _: () = redis::cmd("SET")
            .arg(&tgt_key)
            .arg(target.target_cols.to_string())
            .query_async(&mut rconn)
            .await?;

        // Enqueue on task_queue. The bridge consumer expects
        // `pipelineId` (camelCase) — the snake_case `pipeline_id`
        // mirrors are kept too so downstream observability handlers
        // that read either form stay informed.
        let envelope = serde_json::json!({
            "uid": "automl",
            "session": session,
            "run_id": run_id,
            "runId": run_id,
            "pipelineId": pipeline_id,
            "pipeline_id": pipeline_id,
            "datasetId": target.dataset_id,
            "dataset_id": target.dataset_id,
            "_source": "automl",
        });
        let _: i64 = rconn
            .zadd(&self.cfg.queue_key, envelope.to_string(), 50_i64)
            .await?;
        Ok(())
    }

    /// Find templates × datasets pairs needing more BO iterations.
    /// A template = a pipeline whose DAG contains at least one
    /// LogicalTask node (probed via JSONB filter so we don't pull
    /// every pipeline blob).
    ///
    /// Targets without a resolvable storage path are filtered out so
    /// the runner's dataset expansion has an `fpath` to plant in
    /// session meta. (Datasets with no `storage.location.path` value
    /// can't be loaded anyway — there's no point sending trials for
    /// them.)
    pub async fn discover_targets(&self, limit: i64) -> anyhow::Result<Vec<TemplateTarget>> {
        let conn = self.pool.get().await?;
        let rows = conn
            .query(
                r#"
                WITH templates AS (
                    SELECT id, dag
                    FROM pipelines
                    WHERE jsonb_path_exists(
                        dag, '$.nodes.* ? (@.class_type == "LogicalTask")'
                    )
                ),
                trial_counts AS (
                    SELECT template_id, dataset_id, COUNT(*) AS n
                    FROM (
                        SELECT pipeline_id AS template_id,
                               dataset_id,
                               run_id
                        FROM evaluations
                        WHERE source = 'automl'
                        GROUP BY pipeline_id, dataset_id, run_id
                    ) t
                    GROUP BY template_id, dataset_id
                )
                SELECT t.id           AS template_id,
                       t.dag          AS template_dag,
                       d.id           AS dataset_id,
                       d.storage->'location'->>'path' AS dataset_path,
                       d.columns->'features' AS feature_cols,
                       d.columns->'targets'  AS target_cols,
                       d.task->>'type'  AS task_type,
                       COALESCE(tc.n, 0)::bigint AS trials_so_far
                FROM templates t
                CROSS JOIN datasets d
                LEFT JOIN trial_counts tc
                    ON tc.template_id = t.id AND tc.dataset_id = d.id
                WHERE COALESCE(tc.n, 0) < $1
                  AND d.storage->'location'->>'path' IS NOT NULL
                  AND d.columns->'features' IS NOT NULL
                ORDER BY trials_so_far ASC, t.id, d.id
                LIMIT $2
                "#,
                &[&(self.cfg.trials_per_pair_target as i64), &limit],
            )
            .await?;
        Ok(rows
            .into_iter()
            .map(|r| TemplateTarget {
                template_id: r.get("template_id"),
                template_dag: r.get("template_dag"),
                dataset_id: r.get("dataset_id"),
                dataset_path: r.get("dataset_path"),
                feature_cols: r.try_get("feature_cols").ok().unwrap_or(serde_json::json!([])),
                target_cols: r.try_get("target_cols").ok().unwrap_or(serde_json::json!([])),
                task_type: r
                    .try_get::<_, Option<String>>("task_type")
                    .ok()
                    .flatten()
                    .unwrap_or_else(|| "classification".to_string()),
                trials_so_far: r.get("trials_so_far"),
            })
            .collect())
    }
}

// ---------------------------------------------------------------------------
// Suggestion ↔ JSON helpers — produce the shape `SmacOptimizer::tell`
// (`trial_to_suggestion` in `smac.rs`) reads back. Param values land
// as flat JSON (bool / number / string), NOT the serde-tagged
// `{"kind": "...", "value": ...}` form, because that's the contract
// the surrogate decoder expects.
// ---------------------------------------------------------------------------

fn param_value_to_json(v: &ParamValue) -> serde_json::Value {
    match v {
        ParamValue::Int(i) => serde_json::Value::from(*i),
        ParamValue::Float(f) => {
            // serde_json refuses NaN/Inf; clamp to 0.0 so the
            // round-trip never panics (the surrogate already drops
            // non-finite metrics, so the configs stay well-formed).
            serde_json::Number::from_f64(*f)
                .map(serde_json::Value::Number)
                .unwrap_or(serde_json::Value::Number(serde_json::Number::from(0)))
        }
        ParamValue::Bool(b) => serde_json::Value::Bool(*b),
        ParamValue::Str(s) => serde_json::Value::String(s.clone()),
    }
}

fn suggestion_to_bindings(s: &Suggestion) -> serde_json::Value {
    let mut out = serde_json::Map::new();
    for (slot, b) in &s.bindings {
        let mut params = serde_json::Map::new();
        for (pname, pv) in &b.params {
            params.insert(pname.clone(), param_value_to_json(pv));
        }
        let mut entry = serde_json::Map::new();
        entry.insert("op_fqn".into(), serde_json::Value::String(b.op_fqn.clone()));
        entry.insert("params".into(), serde_json::Value::Object(params));
        out.insert(slot.clone(), serde_json::Value::Object(entry));
    }
    serde_json::Value::Object(out)
}

// ---------------------------------------------------------------------------
// Top-level entry — what the supervisor binary calls.
// ---------------------------------------------------------------------------

pub async fn run_automl_driver() -> anyhow::Result<()> {
    let cfg = DriverConfig::from_env()?;
    let driver = Driver::new(cfg).await?;
    driver.run().await
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slot_extraction_finds_logical_tasks() {
        let dag = serde_json::json!({
            "nodes": {
                "n1": {
                    "class_type": "Operator",
                    "name": "pandas.read_csv", "language": "python",
                },
                "n2": {
                    "class_type": "LogicalTask",
                    "path": ["Preprocessing", "Imputation"],
                    "name": "Preprocessing.Imputation",
                },
                "n3": {
                    "class_type": "LogicalTask",
                    "path": ["Modeling", "Classification", "LinearModels"],
                    "name": "Modeling.Classification.LinearModels",
                },
            },
            "edges": [],
        });
        let slots = logical_task_slots(&dag);
        assert_eq!(slots.len(), 2);
        assert!(slots.contains(&("n2".into(), "Preprocessing.Imputation".into())));
        assert!(slots.contains(&("n3".into(), "Modeling.Classification.LinearModels".into())));
    }

    #[test]
    fn param_spec_float_with_bounds_becomes_float_domain() {
        let spec = ParameterSpec {
            name: "C".into(),
            dtype: "float".into(),
            default: Some("1.0".into()),
            low: Some(1e-3), high: Some(10.0),
            choices: None,
            log_scale: Some(true),
            method: None,
        };
        match param_spec_to_domain(&spec) {
            ParamDomain::Float(b) => {
                assert_eq!(b.low, 1e-3);
                assert_eq!(b.high, 10.0);
                assert!(b.log_scale);
            }
            other => panic!("expected Float, got {other:?}"),
        }
    }

    #[test]
    fn param_spec_categorical_uses_choices() {
        let spec = ParameterSpec {
            name: "kernel".into(),
            dtype: "categorical".into(),
            default: Some("rbf".into()),
            low: None, high: None,
            choices: Some(vec!["linear".into(), "rbf".into(), "poly".into()]),
            log_scale: None,
            method: None,
        };
        match param_spec_to_domain(&spec) {
            ParamDomain::Categorical(c) => {
                assert_eq!(c.options.len(), 3);
                assert_eq!(c.default, Some(1)); // "rbf" is index 1
            }
            other => panic!("expected Categorical, got {other:?}"),
        }
    }

    #[test]
    fn param_spec_bool_becomes_bool_domain() {
        let spec = ParameterSpec {
            name: "shuffle".into(),
            dtype: "bool".into(),
            default: Some("True".into()),
            low: None, high: None, choices: None, log_scale: None, method: None,
        };
        assert!(matches!(param_spec_to_domain(&spec), ParamDomain::Bool));
    }

    #[test]
    fn suggestion_to_bindings_emits_flat_param_values() {
        use crate::optimizer::SlotBinding;
        let mut params: FxHashMap<String, ParamValue> = FxHashMap::default();
        params.insert("C".into(), ParamValue::Float(0.5));
        params.insert("max_iter".into(), ParamValue::Int(100));
        params.insert("fit_intercept".into(), ParamValue::Bool(true));
        params.insert("solver".into(), ParamValue::Str("lbfgs".into()));
        let mut bindings: FxHashMap<String, SlotBinding> = FxHashMap::default();
        bindings.insert(
            "Modeling.Classification".into(),
            SlotBinding {
                op_fqn: "sklearn.linear_model.LogisticRegression".into(),
                params,
            },
        );
        let s = Suggestion { bindings };
        let v = suggestion_to_bindings(&s);
        let slot = v.get("Modeling.Classification").expect("slot present");
        assert_eq!(
            slot.get("op_fqn").and_then(|x| x.as_str()),
            Some("sklearn.linear_model.LogisticRegression"),
        );
        let params = slot.get("params").and_then(|x| x.as_object()).unwrap();
        // Flat JSON, not tagged.
        assert_eq!(params.get("C").and_then(|x| x.as_f64()), Some(0.5));
        assert_eq!(params.get("max_iter").and_then(|x| x.as_i64()), Some(100));
        assert_eq!(params.get("fit_intercept").and_then(|x| x.as_bool()), Some(true));
        assert_eq!(params.get("solver").and_then(|x| x.as_str()), Some("lbfgs"));
    }

    #[test]
    fn param_value_to_json_handles_non_finite_floats() {
        let v = param_value_to_json(&ParamValue::Float(f64::NAN));
        assert_eq!(v, serde_json::Value::Number(serde_json::Number::from(0)));
    }

    #[test]
    fn param_spec_no_metadata_falls_back_to_constant() {
        let spec = ParameterSpec {
            name: "weird".into(),
            dtype: "callable".into(),  // Unknown dtype
            default: Some("foo".into()),
            low: None, high: None, choices: None, log_scale: None, method: None,
        };
        match param_spec_to_domain(&spec) {
            ParamDomain::Constant(ParamValue::Str(s)) => assert_eq!(s, "foo"),
            other => panic!("expected Constant(Str(\"foo\")), got {other:?}"),
        }
    }
}
