//! HMAC-SHA256 request signing — tower middleware matching the
//! Go gateway's ``internal/auth/hmac.go`` behaviour.
//!
//! Contract:
//!
//!   * Request carries ``X-HMAC-Signature``, ``X-HMAC-Timestamp``,
//!     ``X-HMAC-Nonce`` headers.
//!   * Canonical string:
//!     ``{METHOD}\n{PATH}\n{TIMESTAMP}\n{NONCE}\n{BODY_SHA256}``.
//!   * Signature: hex-encoded HMAC-SHA256 over the canonical string,
//!     keyed by ``DORIAN_HMAC_SECRET``.
//!   * Timestamp skew tolerance: 300s (5 min).
//!   * Nonce replay protection via Redis SETNX with 5-minute TTL.
//!
//! When ``DORIAN_HMAC_SECRET`` is unset / empty, signing is
//! **disabled** and every request passes. Matches the Go gateway's
//! dev-friendly default so ``docker compose up`` without a secret
//! still works.
//!
//! Exempt paths: anything under ``/health`` (liveness / readiness
//! probes happen before auth is configured, and MUST stay probeable
//! for k8s / systemd even when the secret is misconfigured).

use std::sync::Arc;

use axum::{
    body::{to_bytes, Body},
    extract::State,
    http::{header::HeaderMap, Request, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
};
use hmac::{Hmac, Mac};
use redis::AsyncCommands;
use sha2::{Digest, Sha256};
use tracing::warn;

use crate::state::AppState;

const MAX_SKEW_SECS: i64 = 300;
const NONCE_TTL_SECS: u64 = 300;
// Cap the buffered body so a pathological POST can't OOM the
// gateway. Two MiB is far above any JSON payload the Python backend
// handles — bigger payloads should use streaming endpoints the
// gateway doesn't re-buffer.
const MAX_BODY_BYTES: usize = 2 * 1024 * 1024;

type HmacSha256 = Hmac<Sha256>;

/// Axum middleware: reject unsigned / bad-signature requests.
pub async fn verify(
    State(state): State<AppState>,
    req: Request<Body>,
    next: Next,
) -> Response {
    let secret = state.inner.config.hmac_secret.clone();
    let Some(secret) = secret else {
        // HMAC disabled — pass through unchanged. Mirrors the Go
        // gateway's dev default when ``DORIAN_HMAC_SECRET`` is empty.
        return next.run(req).await;
    };

    // Exempt health probes — liveness / readiness can't depend on
    // auth config being correct. Covers ``/health*`` (gateway role)
    // and ``/eventbus/health`` (event-bus role monitored separately
    // by compose so the two roles can degrade independently).
    let path = req.uri().path();
    if path.starts_with("/health") || path == "/eventbus/health" {
        return next.run(req).await;
    }

    let (parts, body) = req.into_parts();
    let path = parts.uri.path().to_string();

    // Buffer the body so (a) we can hash it, (b) we can reconstruct
    // the request for the next layer. Streaming signatures would be
    // ideal but the Go gateway doesn't do that either, and the
    // payloads here top out below MAX_BODY_BYTES in practice.
    let body_bytes = match to_bytes(body, MAX_BODY_BYTES).await {
        Ok(b) => b,
        Err(_) => {
            return (
                StatusCode::PAYLOAD_TOO_LARGE,
                "body exceeds HMAC buffer limit",
            )
                .into_response();
        }
    };

    let check = verify_signature(
        &parts.headers,
        parts.method.as_str(),
        &path,
        &body_bytes,
        &secret,
        state.inner.redis.clone(),
    )
    .await;

    if let Err(reason) = check {
        warn!(path = %path, reason = %reason, "HMAC rejected");
        return (StatusCode::UNAUTHORIZED, reason).into_response();
    }

    // Rebuild the request with the buffered body so the downstream
    // handler (usually the reverse proxy) sees exactly what the
    // caller sent.
    let req = Request::from_parts(parts, Body::from(body_bytes));
    next.run(req).await
}

async fn verify_signature(
    headers: &HeaderMap,
    method: &str,
    path: &str,
    body: &[u8],
    secret: &str,
    mut redis: redis::aio::ConnectionManager,
) -> Result<(), &'static str> {
    let signature = headers
        .get("x-hmac-signature")
        .and_then(|h| h.to_str().ok())
        .ok_or("missing X-HMAC-Signature")?;
    let timestamp = headers
        .get("x-hmac-timestamp")
        .and_then(|h| h.to_str().ok())
        .ok_or("missing X-HMAC-Timestamp")?;
    let nonce = headers
        .get("x-hmac-nonce")
        .and_then(|h| h.to_str().ok())
        .ok_or("missing X-HMAC-Nonce")?;

    // Timestamp skew — protects against replay with a fresh nonce.
    let ts: i64 = timestamp.parse().map_err(|_| "bad timestamp")?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    if (now - ts).abs() > MAX_SKEW_SECS {
        return Err("timestamp skew > 5m");
    }

    // Body hash. Canonical message: METHOD\nPATH\nTS\nNONCE\nBODY_SHA256.
    //
    // Multipart carve-out: the browser serialises ``FormData`` with a
    // randomly-generated boundary token that's set inside ``fetch``
    // *after* the request body is built, so the JS-side signer can't
    // observe the bytes that go on the wire. The python backend
    // (``backend/hmac_auth.py``) and the JS client both agree to hash
    // an empty body for multipart requests; mirroring the carve-out
    // here keeps every multipart POST (createSession, renameSession,
    // upload, import) authenticatable. The body is still buffered up
    // top — only its contribution to the signature is dropped.
    let is_multipart = headers
        .get("content-type")
        .and_then(|h| h.to_str().ok())
        .map(|ct| ct.to_ascii_lowercase().starts_with("multipart/form-data"))
        .unwrap_or(false);
    let body_for_hash: &[u8] = if is_multipart { b"" } else { body };
    let body_hash = {
        let mut h = Sha256::new();
        h.update(body_for_hash);
        hex::encode(h.finalize())
    };
    let canonical = format!("{method}\n{path}\n{timestamp}\n{nonce}\n{body_hash}");

    let mut mac = HmacSha256::new_from_slice(secret.as_bytes())
        .map_err(|_| "HMAC key init failed")?;
    mac.update(canonical.as_bytes());
    let expected = hex::encode(mac.finalize().into_bytes());
    if !constant_time_eq(signature.as_bytes(), expected.as_bytes()) {
        return Err("signature mismatch");
    }

    // Replay defense: SETNX the nonce with a TTL matching the skew
    // tolerance. Any subsequent request reusing the same nonce
    // (within TTL) hits the already-set key and is rejected.
    let key = format!("hmac:nonce:{nonce}");
    let set_result: redis::RedisResult<bool> = redis
        .set_nx(&key, "1")
        .await;
    match set_result {
        Ok(true) => {
            let _: redis::RedisResult<()> =
                redis.expire(&key, NONCE_TTL_SECS as i64).await;
            Ok(())
        }
        Ok(false) => Err("nonce replay"),
        Err(_) => {
            // Redis hiccup — fail open so operators aren't locked out
            // by a transient Redis glitch. Matches the Go gateway's
            // behaviour when the nonce store is unreachable.
            warn!("HMAC nonce store unreachable; passing through");
            Ok(())
        }
    }
}

/// Constant-time comparison to avoid timing side-channels on the
/// signature check.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

// Tame unused-import warnings for the Arc import when HMAC is
// disabled in the default feature set. Keeping the import around so
// future commits don't re-add it.
#[allow(dead_code)]
fn _touch(_state: Arc<crate::state::AppStateInner>) {}
