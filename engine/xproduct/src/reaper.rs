//! Orphan-stream reaper.
//!
//! Backend writes per-pipeline event streams under
//! ``{uid}:{session}:stream`` (Redis Streams). User-driven runs
//! have a frontend WebSocket reading + acking those events; trial
//! runs (xproduct, RL, AutoML) have no consumer, so the streams
//! grow unbounded. A 12k-stream-deep accumulation cost ~9.5 GiB
//! of redis memory in the prior incident.
//!
//! This reaper periodically scans for orphan trial streams and
//! deletes them. Two heuristics for "orphan":
//!
//!   * Session prefix is one of the trial-source markers
//!     (``xproduct:``, ``rl:``, ``automl:``).
//!   * Stream's last entry is older than ``DORIAN_STREAM_REAP_AGE_S``
//!     (default 1h) — gives in-flight trials enough time to
//!     finish + the consumer (when wired) enough time to ack.
//!
//! When the future "trial event consumer" task lands, it can
//! issue the delete itself on the run-completed event; this
//! reaper then becomes the safety net for runs that crashed
//! mid-execution.

use std::time::Duration;

use redis::AsyncCommands;
use tracing::{info, warn};

const REAP_PATTERNS: &[&str] = &[
    "xproduct:*",
    "rl:*",
    "automl:*",
];

pub struct StreamReaper {
    client: redis::Client,
    interval: Duration,
    max_age: Duration,
}

impl StreamReaper {
    pub fn from_env(redis_url: &str) -> anyhow::Result<Self> {
        let interval_s = std::env::var("DORIAN_STREAM_REAP_INTERVAL_S")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(300);
        let max_age_s = std::env::var("DORIAN_STREAM_REAP_AGE_S")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(3600);
        Ok(Self {
            client: redis::Client::open(redis_url)?,
            interval: Duration::from_secs(interval_s),
            max_age: Duration::from_secs(max_age_s),
        })
    }

    pub async fn run(self) -> anyhow::Result<()> {
        info!(
            interval_s = self.interval.as_secs(),
            max_age_s = self.max_age.as_secs(),
            "stream reaper starting"
        );
        let mut ticker = tokio::time::interval(self.interval);
        loop {
            ticker.tick().await;
            if let Err(e) = self.tick().await {
                warn!(error=%e, "stream reaper tick failed");
            }
        }
    }

    async fn tick(&self) -> anyhow::Result<()> {
        let mut conn = self.client.get_multiplexed_async_connection().await?;
        let max_age_ms = self.max_age.as_millis() as u64;
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_millis() as u64;
        let cutoff_ms = now_ms.saturating_sub(max_age_ms);

        let mut total_deleted = 0u64;
        for pattern in REAP_PATTERNS {
            let mut cursor: u64 = 0;
            loop {
                // SCAN with MATCH + COUNT. redis-rs' iter helper would
                // hide the cursor, but we want explicit batching so a
                // long-running scan doesn't hold a long-lived
                // multiplexed connection alive.
                let (next_cursor, keys): (u64, Vec<String>) = redis::cmd("SCAN")
                    .arg(cursor)
                    .arg("MATCH")
                    .arg(pattern)
                    .arg("COUNT")
                    .arg(2000)
                    .query_async(&mut conn)
                    .await?;
                cursor = next_cursor;
                let mut to_delete = Vec::new();
                for k in &keys {
                    // Only address Stream-typed keys — leftover sets,
                    // hashes, etc. under the same prefix are someone
                    // else's data.
                    let kind: String = redis::cmd("TYPE")
                        .arg(k)
                        .query_async(&mut conn)
                        .await
                        .unwrap_or_else(|_| "none".to_string());
                    if kind != "stream" {
                        continue;
                    }
                    if stream_is_old(&mut conn, k, cutoff_ms).await {
                        to_delete.push(k.clone());
                    }
                }
                if !to_delete.is_empty() {
                    let _: i64 = conn.unlink(to_delete.as_slice()).await.unwrap_or(0);
                    total_deleted += to_delete.len() as u64;
                }
                if cursor == 0 {
                    break;
                }
            }
        }
        if total_deleted > 0 {
            info!(deleted = total_deleted, "stream reaper: cleaned orphan trial streams");
        }
        Ok(())
    }
}


/// True when the stream's last entry id has a timestamp older than
/// `cutoff_ms`. Stream entry ids are of the form `<unix_ms>-<seq>`.
async fn stream_is_old(
    conn: &mut redis::aio::MultiplexedConnection,
    key: &str,
    cutoff_ms: u64,
) -> bool {
    // XINFO STREAM returns a flat key/value list (in resp2) where
    // we want `last-generated-id`.
    let info_res: Result<Vec<redis::Value>, _> =
        redis::cmd("XINFO").arg("STREAM").arg(key).query_async(conn).await;
    let last_id = match info_res {
        Ok(items) => extract_last_generated_id(&items),
        Err(_) => return false,
    };
    let stream_ms = match last_id.split('-').next().and_then(|s| s.parse::<u64>().ok()) {
        Some(ms) => ms,
        None => return false,
    };
    stream_ms < cutoff_ms
}

fn extract_last_generated_id(items: &[redis::Value]) -> String {
    // XINFO STREAM returns alternating key/value pairs.
    let mut iter = items.iter();
    while let Some(k) = iter.next() {
        let v = match iter.next() { Some(v) => v, None => break };
        if let redis::Value::BulkString(b) = k {
            if b == b"last-generated-id" {
                if let redis::Value::BulkString(vb) = v {
                    return String::from_utf8_lossy(vb).into_owned();
                }
            }
        }
    }
    "0-0".to_string()
}
