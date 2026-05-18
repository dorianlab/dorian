//! Runtime configuration. All knobs live in env vars so the
//! cross-product engine can be retuned without rebuilding the
//! crate. Defaults are conservative (rare polling, low rate) so a
//! freshly-deployed engine doesn't surprise an under-provisioned
//! cluster.

use std::time::Duration;

/// Engine configuration. Built once at startup from the
/// environment + sensible defaults.
#[derive(Debug, Clone)]
pub struct Config {
    /// Postgres connection string. Required —
    /// ``DORIAN_POSTGRES_URL`` (preferred) or assembled from the
    /// `DORIAN_POSTGRES_HOST` / `_PORT` / `_USER` / `_PASSWORD` /
    /// `_DATABASE` quartet so the same env shape that backend.envs
    /// reads applies here.
    pub postgres_url: String,

    /// Redis connection string for trial enqueueing. Defaults to
    /// `redis://redis:6379` (compose hostname).
    pub redis_url: String,

    /// Redis stream key the trials are enqueued onto. Should match
    /// the consumer group the exec-worker subscribes to.
    pub queue_key: String,

    /// How often the engine polls `pairs_to_complete`. Default 30s
    /// — fresh pipelines/datasets are picked up within that window.
    pub poll_interval: Duration,

    /// Trials per minute the bucket refills at. Caps the enqueue
    /// rate so a backlog of 10k uncovered pairs doesn't flood the
    /// queue all at once. Default 10/min.
    pub rate_per_minute: u32,

    /// Maximum pairs read per poll. Caps memory + query cost.
    /// Default 256.
    pub batch_size: i64,

    /// Pause submission when `task_queue` ZCARD exceeds this. The
    /// driver still polls so it picks up the depth-drain transition,
    /// but skips the enqueue step until the queue clears.
    pub queue_high_watermark: i64,
    /// Resume submission once depth drops back below this (sticky
    /// hysteresis around the high-watermark prevents flapping).
    pub queue_low_watermark: i64,
}

impl Config {
    pub fn from_env() -> anyhow::Result<Self> {
        let postgres_url = match std::env::var("DORIAN_POSTGRES_URL") {
            Ok(s) if !s.trim().is_empty() => s,
            _ => assemble_postgres_url()?,
        };
        let redis_url = std::env::var("DORIAN_REDIS_URL")
            .unwrap_or_else(|_| "redis://redis:6379".into());
        let queue_key = std::env::var("DORIAN_XPRODUCT_QUEUE")
            .unwrap_or_else(|_| "task_queue".into());
        let poll_interval = parse_duration_secs(
            "DORIAN_XPRODUCT_POLL_SECS",
            30,
        );
        let rate_per_minute = parse_u32("DORIAN_XPRODUCT_RATE", 10);
        let batch_size = parse_u32("DORIAN_XPRODUCT_BATCH", 256) as i64;
        let queue_high_watermark = std::env::var("DORIAN_QUEUE_HIGH_WATERMARK")
            .ok()
            .and_then(|v| v.parse::<i64>().ok())
            .unwrap_or(500);
        let queue_low_watermark = std::env::var("DORIAN_QUEUE_LOW_WATERMARK")
            .ok()
            .and_then(|v| v.parse::<i64>().ok())
            .unwrap_or(100);
        Ok(Config {
            postgres_url,
            redis_url,
            queue_key,
            poll_interval,
            rate_per_minute,
            batch_size,
            queue_high_watermark,
            queue_low_watermark,
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
    let password = std::env::var("DORIAN_POSTGRES_PASSWORD").map_err(|_| {
        anyhow::anyhow!(
            "DORIAN_POSTGRES_PASSWORD must be set (or pass DORIAN_POSTGRES_URL directly)"
        )
    })?;
    let database = std::env::var("DORIAN_POSTGRES_DATABASE")
        .unwrap_or_else(|_| "dorian".into());
    Ok(format!(
        "postgresql://{user}:{password}@{host}:{port}/{database}"
    ))
}

fn parse_duration_secs(var: &str, default_secs: u64) -> Duration {
    std::env::var(var)
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or_else(|| Duration::from_secs(default_secs))
}

fn parse_u32(var: &str, default: u32) -> u32 {
    std::env::var(var)
        .ok()
        .and_then(|v| v.parse::<u32>().ok())
        .unwrap_or(default)
}
