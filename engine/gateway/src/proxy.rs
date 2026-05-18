//! Reverse proxy to the Python backend.
//!
//! Catch-all fallback route: anything that didn't match a specific
//! gateway handler (health, eventbus, session, engine, kb) gets
//! proxied to the Python backend at ``DORIAN_BACKEND_URL``. Matches
//! the Go gateway's behaviour where HMAC-verified requests were
//! forwarded with an injected ``X-Gateway-Verified`` header.
//!
//! Streams response bodies through — large payloads (CSV upload,
//! dataset download) don't need to materialise in gateway memory.

use axum::{
    body::Body,
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, Response, StatusCode, Uri},
    response::IntoResponse,
};
use tracing::warn;

use crate::state::AppState;

pub async fn reverse_proxy(
    State(state): State<AppState>,
    req: Request<Body>,
) -> impl IntoResponse {
    let cfg = &state.inner.config;
    let method = req.method().clone();
    let (mut parts, body) = req.into_parts();

    // Rewrite: backend_url + incoming path + query.
    let path_and_query = parts
        .uri
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or("/");
    let target = format!("{}{}", cfg.backend_url.trim_end_matches('/'), path_and_query);
    let target_uri: Uri = match target.parse() {
        Ok(u) => u,
        Err(e) => {
            warn!("proxy uri parse failed: {e}");
            return (StatusCode::BAD_GATEWAY, "bad target URI").into_response();
        }
    };

    // Strip hop-by-hop + HMAC headers — the Python backend should
    // trust the gateway-verified marker instead of re-validating
    // the signature. Stripping HMAC prevents a second (failed)
    // verification downstream.
    strip_hop_headers(&mut parts.headers);
    parts.headers.remove("x-hmac-signature");
    parts.headers.remove("x-hmac-timestamp");
    parts.headers.remove("x-hmac-nonce");
    parts.headers.insert(
        "x-gateway-verified",
        HeaderValue::from_str(&cfg.gateway_verified_token)
            .unwrap_or_else(|_| HeaderValue::from_static("gateway-verified")),
    );

    // Reqwest client: reused across requests for connection pooling.
    // Access via the shared AppState.
    let client = &state.inner.http_client;
    let body_bytes = match axum::body::to_bytes(body, usize::MAX).await {
        Ok(b) => b,
        Err(e) => {
            warn!("proxy buffer body failed: {e}");
            return (StatusCode::BAD_GATEWAY, "bad body").into_response();
        }
    };

    let mut builder = client
        .request(method, target_uri.to_string())
        .body(body_bytes.to_vec());
    for (name, value) in parts.headers.iter() {
        // reqwest takes its own HeaderName + HeaderValue types;
        // convert 1-to-1 since axum's http-1 and reqwest's http-1
        // are compatible.
        if let (Ok(h), Ok(v)) = (
            reqwest::header::HeaderName::from_bytes(name.as_ref()),
            reqwest::header::HeaderValue::from_bytes(value.as_bytes()),
        ) {
            builder = builder.header(h, v);
        }
    }

    let upstream_resp = match builder.send().await {
        Ok(r) => r,
        Err(e) => {
            warn!(target = %target_uri, "upstream request failed: {e}");
            return (StatusCode::BAD_GATEWAY, "upstream unreachable").into_response();
        }
    };

    // Rebuild axum response from reqwest response.
    let status_code = upstream_resp.status().as_u16();
    let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::BAD_GATEWAY);
    let mut response_builder = Response::builder().status(status);
    for (name, value) in upstream_resp.headers() {
        if is_hop_header(name.as_str()) {
            continue;
        }
        response_builder = response_builder.header(name.as_str(), value.as_bytes());
    }
    match upstream_resp.bytes().await {
        Ok(bytes) => response_builder
            .body(Body::from(bytes))
            .unwrap_or_else(|_| Response::new(Body::empty())),
        Err(e) => {
            warn!(target = %target_uri, "upstream body read failed: {e}");
            (StatusCode::BAD_GATEWAY, "upstream body read failed").into_response()
        }
    }
}

// Hop-by-hop headers per RFC 7230 § 6.1 + common WS upgrade headers —
// must not be forwarded across a proxy boundary.
fn is_hop_header(name: &str) -> bool {
    matches!(
        name.to_ascii_lowercase().as_str(),
        "connection"
            | "proxy-connection"
            | "keep-alive"
            | "transfer-encoding"
            | "te"
            | "trailer"
            | "upgrade"
            | "proxy-authorization"
            | "proxy-authenticate"
    )
}

fn strip_hop_headers(headers: &mut HeaderMap) {
    let to_remove: Vec<String> = headers
        .keys()
        .filter(|n| is_hop_header(n.as_str()))
        .map(|n| n.as_str().to_string())
        .collect();
    for name in to_remove {
        headers.remove(name.as_str());
    }
}
