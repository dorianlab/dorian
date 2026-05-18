//! Engine driver — connects the polling loop to the token bucket
//! to the queue. One `Engine` instance per process; spawns its own
//! tokio tasks for the polling loop and the bucket refill.

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use deadpool_postgres::{Manager, Pool};
use tokio_postgres::NoTls;
use tracing::{info, warn};

use crate::config::Config;
use crate::pairs::PairsToComplete;
use crate::queue::TrialQueue;

/// The cross-product engine. Drop the value to stop the loops
/// gracefully (each spawned task watches a shutdown channel).
pub struct Engine {
    cfg: Config,
    pool: Pool,
    queue: TrialQueue,
    /// Redis client used for backpressure probes (ZCARD on the
    /// task_queue). Separate from the queue's client so we don't
    /// contend on its multiplexed connection during enqueue bursts.
    redis: redis::Client,
    /// Token bucket — refilled at `cfg.rate_per_minute / 60` per
    /// second, capped at `cfg.rate_per_minute`. Each enqueue
    /// decrements; if zero, the polling loop sleeps.
    tokens: Arc<AtomicU32>,
    /// Backpressure flag. Toggled true once `task_queue` exceeds
    /// `queue_high_watermark`; toggled back false once depth drops
    /// to `queue_low_watermark`.
    paused: Arc<std::sync::atomic::AtomicBool>,
}

impl Engine {
    pub async fn new(cfg: Config) -> anyhow::Result<Self> {
        let pg_cfg: tokio_postgres::Config = cfg.postgres_url.parse()?;
        let mgr = Manager::new(pg_cfg, NoTls);
        let pool = Pool::builder(mgr).max_size(4).build()?;

        let queue = TrialQueue::new(&cfg.redis_url, cfg.queue_key.clone())?;
        let redis = redis::Client::open(cfg.redis_url.clone())?;

        let tokens = Arc::new(AtomicU32::new(cfg.rate_per_minute));
        let paused = Arc::new(std::sync::atomic::AtomicBool::new(false));
        Ok(Engine { cfg, pool, queue, redis, tokens, paused })
    }

    /// Drive both loops to completion (or shutdown signal).
    /// Spawns the bucket refill, then runs the polling loop on
    /// the calling task. Doesn't return until shutdown.
    pub async fn run(self) -> anyhow::Result<()> {
        info!(
            poll_interval = ?self.cfg.poll_interval,
            rate_per_minute = self.cfg.rate_per_minute,
            batch_size = self.cfg.batch_size,
            "xproduct engine starting"
        );
        let refill_tokens = self.tokens.clone();
        let refill_cap = self.cfg.rate_per_minute;
        let _refill_task = tokio::spawn(async move {
            // Refill once per second at rate/60 tokens. Caps at
            // `rate_per_minute` so a long idle period doesn't
            // create a flood when work resumes.
            let per_sec = ((refill_cap as f64) / 60.0).ceil() as u32;
            let per_sec = per_sec.max(1);
            let mut ticker = tokio::time::interval(Duration::from_secs(1));
            loop {
                ticker.tick().await;
                let cur = refill_tokens.load(Ordering::Relaxed);
                let next = (cur + per_sec).min(refill_cap);
                refill_tokens.store(next, Ordering::Relaxed);
            }
        });

        let mut ticker = tokio::time::interval(self.cfg.poll_interval);
        loop {
            ticker.tick().await;
            if let Err(e) = self.tick().await {
                // ``%e`` only prints the outermost Display, which for
                // tokio-postgres errors is the useless string "db
                // error". Use the Debug impl to surface the inner
                // error chain (column names / constraint violations
                // / sql syntax errors) so operators can diagnose
                // schema drift without re-running the binary against
                // a debugger.
                warn!(error = ?e, "xproduct tick failed");
            }
        }
    }

    /// One iteration of the polling loop. Reports + enqueues up
    /// to `min(batch_size, available_tokens)` pairs. Tokens are
    /// consumed atomically.
    pub async fn tick(&self) -> anyhow::Result<()> {
        // Backpressure: probe queue depth + flip paused with
        // hysteresis. While paused, skip enqueue but keep ticking
        // so the next probe picks up the drain transition.
        match self.queue_depth().await {
            Ok(depth) => {
                let was_paused = self.paused.load(Ordering::Relaxed);
                if !was_paused && depth >= self.cfg.queue_high_watermark {
                    self.paused.store(true, Ordering::Relaxed);
                    warn!(
                        depth,
                        high = self.cfg.queue_high_watermark,
                        "xproduct: queue full — pausing submissions until drained"
                    );
                } else if was_paused && depth <= self.cfg.queue_low_watermark {
                    self.paused.store(false, Ordering::Relaxed);
                    info!(
                        depth,
                        low = self.cfg.queue_low_watermark,
                        "xproduct: queue drained — resuming submissions"
                    );
                }
            }
            Err(e) => warn!(error = %e, "xproduct: queue depth probe failed"),
        }
        if self.paused.load(Ordering::Relaxed) {
            return Ok(());
        }

        let available = self.tokens.load(Ordering::Relaxed);
        if available == 0 {
            return Ok(());
        }
        let want = (available as i64).min(self.cfg.batch_size);
        let pairs_query = PairsToComplete::new(&self.pool);
        let pairs = pairs_query.fetch(want).await?;
        if pairs.is_empty() {
            // Cache fully populated — nothing to do this tick.
            // Don't burn a log line every poll on the no-work
            // case; only log on transitions / errors.
            return Ok(());
        }
        let mut enqueued = 0u32;
        for pair in &pairs {
            let cur = self.tokens.load(Ordering::Relaxed);
            if cur == 0 {
                break;
            }
            // CAS-decrement so concurrent refill doesn't race the
            // observation.
            if self
                .tokens
                .compare_exchange(cur, cur - 1, Ordering::Relaxed, Ordering::Relaxed)
                .is_err()
            {
                continue;
            }
            match self
                .queue
                .enqueue(
                    &pair.pipeline_id,
                    &pair.dataset_id,
                    pair.dataset_path.as_deref(),
                    pair.task_type.as_deref(),
                    pair.feature_cols.as_ref(),
                    pair.target_cols.as_ref(),
                )
                .await
            {
                Ok(run_id) => {
                    enqueued += 1;
                    tracing::debug!(
                        run_id = %run_id,
                        pipeline = %pair.pipeline_id,
                        dataset = %pair.dataset_id,
                        "xproduct enqueued"
                    );
                }
                Err(e) => {
                    // Enqueue failed — return the token to the
                    // bucket so we don't lose budget on transient
                    // redis errors.
                    self.tokens.fetch_add(1, Ordering::Relaxed);
                    warn!(error = %e, "xproduct enqueue failed");
                }
            }
        }
        if enqueued > 0 {
            info!(enqueued, batch_total = pairs.len(), "xproduct tick");
        }
        Ok(())
    }

    /// Probe `task_queue` ZCARD for the backpressure check.
    async fn queue_depth(&self) -> anyhow::Result<i64> {
        let mut conn = self.redis.get_multiplexed_async_connection().await?;
        let depth: i64 = redis::cmd("ZCARD")
            .arg(&self.cfg.queue_key)
            .query_async(&mut conn)
            .await?;
        Ok(depth)
    }
}
