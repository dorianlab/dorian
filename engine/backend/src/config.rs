//! Backend configuration — env-derived, mirrors ``backend.config.config``
//! at the values that matter for the eventbus consumer side.
//!
//! The python ``backend/config.py`` reads from a dynaconf YAML; the
//! rust backend lives or dies by env vars only — same default
//! posture as the gateway. Names match the gateway's vars where
//! the two services share a value (REDIS_URL, EVENTBUS_STREAM_BG,
//! …) so a single ``.env`` configures both.

use anyhow::{Context, Result};

#[derive(Clone)]
pub struct Config {
    pub redis_url: String,
    pub stream_user: String,
    pub stream_bg: String,
    pub consumer_group: String,
    pub consumer_name: String,
    /// XREADGROUP block timeout, milliseconds. Short enough that
    /// SIGTERM gets noticed within ~1s; long enough to avoid a busy
    /// loop when streams are quiet.
    pub block_ms: u64,
    /// Max messages per ``XREADGROUP`` batch.
    pub batch_count: u64,
    /// HTTP bind address for the future axum-served routes.
    /// Empty string = HTTP server disabled (subscriber-only).
    pub http_bind: String,
    /// Filesystem path to the curated extractor rules directory
    /// (the ``dorian/code/parsing/rules/*.json`` set). Read by the
    /// ``ExtractPipeline`` handler. Empty string = no curated rules
    /// loaded; the handler then runs only any user-saved JSON
    /// specs.
    pub extractor_rules_dir: String,
}

impl std::fmt::Debug for Config {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Redact the redis URL — it carries the password in DSN form.
        let redis_redacted = redact_dsn(&self.redis_url);
        f.debug_struct("Config")
            .field("redis_url", &redis_redacted)
            .field("stream_user", &self.stream_user)
            .field("stream_bg", &self.stream_bg)
            .field("consumer_group", &self.consumer_group)
            .field("consumer_name", &self.consumer_name)
            .field("block_ms", &self.block_ms)
            .field("batch_count", &self.batch_count)
            .field("http_bind", &self.http_bind)
            .field("extractor_rules_dir", &self.extractor_rules_dir)
            .finish()
    }
}

fn redact_dsn(s: &str) -> String {
    // Replace the user:password@ chunk with user:****@; leaves the
    // host/db intact for diagnostics.
    if let Some(at) = s.find('@') {
        if let Some(scheme_end) = s.find("://") {
            let head = &s[..scheme_end + 3];
            let tail = &s[at..];
            return format!("{head}<redacted>{tail}");
        }
    }
    s.to_string()
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let redis_url = std::env::var("REDIS_URL").or_else(|_| std::env::var("REDIS_DSN")).context(
            "REDIS_URL (or REDIS_DSN) is required so the subscriber can connect",
        )?;
        Ok(Self {
            redis_url,
            stream_user: env_or("EVENTBUS_STREAM_USER", "events:user"),
            stream_bg: env_or("EVENTBUS_STREAM_BG", "events:bg"),
            consumer_group: env_or("EVENTBUS_GROUP", "dorian-backend-rust"),
            consumer_name: env_or(
                "EVENTBUS_CONSUMER",
                &format!(
                    "rust-{}",
                    std::env::var("HOSTNAME").unwrap_or_else(|_| "local".into())
                ),
            ),
            block_ms: parse_u64("EVENTBUS_BLOCK_MS", 1000),
            batch_count: parse_u64("EVENTBUS_BATCH_COUNT", 32),
            http_bind: env_or("BACKEND_BIND", ""),
            extractor_rules_dir: env_or("DORIAN_EXTRACTOR_RULES_DIR", ""),
        })
    }
}

fn env_or(key: &str, fallback: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| fallback.to_string())
}

fn parse_u64(key: &str, fallback: u64) -> u64 {
    std::env::var(key)
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(fallback)
}
