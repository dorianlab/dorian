//! Gateway configuration from environment variables.
//!
//! Mirrors the knobs the Go gateway reads today so compose / deploy
//! env files don't need to change during the swap-over. Defaults
//! match Go gateway's production defaults; unset values don't
//! trigger subtle behaviour changes.

use std::env;
use std::fmt;

use anyhow::Result;
use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

#[derive(Clone)]
pub struct GatewayConfig {
    pub bind_address: String,
    pub redis_url: String,
    pub backend_url: String,
    pub hmac_secret: Option<String>,
    pub gateway_verified_token: String,
    // Event-bus parameters — mirrored from the Go ``eventbus.LoadConfig``
    // so compose / deploy env vars don't change when the service role
    // consolidates into this Rust binary.
    pub stream_user: String,
    pub stream_bg: String,
    pub stream_maxlen: u64,
    pub backpressure_threshold: f64,
    /// Origins allowed to call gateway endpoints from a browser.
    /// Read from `DORIAN_CORS_ORIGINS` (comma-separated) and surfaced
    /// here so `main.rs` can build the `tower_http::cors::CorsLayer`.
    /// Without this layer, gateway-served routes (`/session/*`, etc.)
    /// don't carry `Access-Control-Allow-Origin` and the SPA at :3000
    /// can't reach them — every preflight fails. The reverse-proxy
    /// fallback inherits backend's CORS headers via response
    /// pass-through, but native gateway routes have no upstream to
    /// inherit from.
    pub cors_origins: Vec<String>,
}

// Hand-rolled Debug so ``info!(?cfg, ...)`` at startup never prints
// the Redis password (embedded in redis_url) or the HMAC secret to
// stdout / the container log. Derived Debug leaks both.
impl fmt::Debug for GatewayConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("GatewayConfig")
            .field("bind_address", &self.bind_address)
            .field("redis_url", &redact_redis_url(&self.redis_url))
            .field("backend_url", &self.backend_url)
            .field("hmac_secret", &self.hmac_secret.as_ref().map(|_| "***"))
            .field("gateway_verified_token", &"***")
            .field("stream_user", &self.stream_user)
            .field("stream_bg", &self.stream_bg)
            .field("stream_maxlen", &self.stream_maxlen)
            .field("backpressure_threshold", &self.backpressure_threshold)
            .finish()
    }
}

/// Compute ``hex(HMAC-SHA256(secret, "gateway-verified"))`` —
/// matches the python backend's ``HMACAuthMiddleware`` derivation
/// in ``backend/hmac_auth.py``.
fn derive_gateway_verified_token(secret: &str) -> String {
    let mut mac = HmacSha256::new_from_slice(secret.as_bytes())
        .expect("HMAC accepts arbitrary key length");
    mac.update(b"gateway-verified");
    hex::encode(mac.finalize().into_bytes())
}

/// Redact the userinfo portion of a ``redis://user:pass@host:port``
/// URL for logging. Keeps host + port so operators can still see
/// where the gateway is connecting.
fn redact_redis_url(url: &str) -> String {
    if let Some((scheme_sep, rest)) = url.split_once("://") {
        if let Some((_userinfo, host_tail)) = rest.split_once('@') {
            return format!("{scheme_sep}://***@{host_tail}");
        }
    }
    url.to_string()
}

impl GatewayConfig {
    pub fn from_env() -> Result<Self> {
        Ok(Self {
            bind_address: env::var("DORIAN_GATEWAY_BIND")
                .unwrap_or_else(|_| "0.0.0.0:8080".to_string()),
            redis_url: env::var("DORIAN_REDIS_URL")
                .or_else(|_| build_redis_url_from_parts())
                .map_err(|_| anyhow::anyhow!(
                    "DORIAN_REDIS_URL (or DORIAN_REDIS_HOST + \
                     DORIAN_REDIS_PORT + DORIAN_REDIS_PASSWORD) must be \
                     set. The port comes from .env's DORIAN_REDIS_PORT \
                     and is substituted into the compose-baked URL — \
                     a silent localhost fallback once hid a wrong-password \
                     breakage (2026-04-28)."
                ))?,
            backend_url: env::var("DORIAN_BACKEND_URL")
                .map_err(|_| anyhow::anyhow!(
                    "DORIAN_BACKEND_URL must be set"
                ))?,
            hmac_secret: {
                let secret = env::var("DORIAN_HMAC_SECRET").ok().filter(|s| !s.is_empty());
                let env = env::var("DORIAN_ENV").unwrap_or_default().to_lowercase();
                if secret.is_none() && !matches!(env.as_str(), "" | "dev" | "test") {
                    anyhow::bail!(
                        "DORIAN_HMAC_SECRET empty in DORIAN_ENV={env} — gateway \
                         would accept unsigned requests. Set the secret or set \
                         DORIAN_ENV=dev to allow the unsigned dev path."
                    );
                }
                secret
            },
            // Gateway-verified token: the python backend's HMAC
            // middleware accepts ``HMAC-SHA256(HMAC_SECRET, b"gateway-verified")``
            // hex as proof that the gateway already validated the
            // request signature. We derive that here from the same
            // HMAC_SECRET so deploys don't have to set a third env
            // var that has to stay in sync with the secret. The
            // explicit env override is kept for the rare case where
            // backend + gateway run with a non-default literal.
            gateway_verified_token: env::var("DORIAN_GATEWAY_VERIFIED_TOKEN")
                .ok()
                .filter(|s| !s.is_empty())
                .or_else(|| {
                    env::var("DORIAN_HMAC_SECRET")
                        .ok()
                        .filter(|s| !s.is_empty())
                        .map(|secret| derive_gateway_verified_token(&secret))
                })
                .unwrap_or_else(|| "gateway-verified".to_string()),
            stream_user: env::var("DORIAN_EVENTBUS_STREAM_USER")
                .unwrap_or_else(|_| "events:user".to_string()),
            stream_bg: env::var("DORIAN_EVENTBUS_STREAM_BG")
                .unwrap_or_else(|_| "events:bg".to_string()),
            stream_maxlen: env::var("DORIAN_EVENTBUS_STREAM_MAXLEN")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(100_000),
            backpressure_threshold: env::var("DORIAN_EVENTBUS_BACKPRESSURE")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(0.90),
            cors_origins: env::var("DORIAN_CORS_ORIGINS")
                .ok()
                .filter(|s| !s.trim().is_empty())
                .map(|s| {
                    s.split(',')
                        .map(|o| o.trim().to_string())
                        .filter(|o| !o.is_empty())
                        .collect()
                })
                .unwrap_or_else(|| {
                    vec![
                        "http://localhost:3000".to_string(),
                        "http://127.0.0.1:3000".to_string(),
                    ]
                }),
        })
    }
}

/// Assemble a redis:// URL from individual env vars when
/// ``DORIAN_REDIS_URL`` isn't set — matches the Go gateway's legacy
/// config path where host / port / password were provided
/// separately. Keeps backward-compat with existing compose files.
fn build_redis_url_from_parts() -> Result<String> {
    let host = env::var("DORIAN_REDIS_HOST")?;
    let port = env::var("DORIAN_REDIS_PORT")?;
    let password = env::var("DORIAN_REDIS_PASSWORD").ok().filter(|s| !s.is_empty());
    let user = env::var("DORIAN_REDIS_USER").ok().filter(|s| !s.is_empty());
    let auth = match (user.as_deref(), password.as_deref()) {
        (Some(u), Some(p)) => format!("{}:{}@", u, p),
        (None, Some(p)) => format!(":{}@", p),
        _ => String::new(),
    };
    Ok(format!("redis://{}{}:{}", auth, host, port))
}
