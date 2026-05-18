//! Dorian gateway — Rust/Tokio port of the Go ``cmd/gateway``.
//!
//! Currently a **skeleton**: health endpoint + structured logging +
//! graceful shutdown. Runs alongside the Go gateway during the
//! migration so both can be compared under real traffic. Subsequent
//! commits add: HMAC auth middleware, reverse proxy to the Python
//! backend, session CRUD (Redis-backed), WebSocket proxy (msgpack
//! binary frames), gRPC pass-through to the engine crate.
//!
//! Design principles (from the architecture-sweep memo):
//!   * One HTTP framework, one async runtime — axum on tokio.
//!   * Routes declared via the router builder; no parallel
//!     dispatcher layer. New endpoints = one route function +
//!     one tower middleware at most.
//!   * Config from env vars with sensible prod defaults; no
//!     hand-maintained YAML.
//!   * Tracing via ``tracing-subscriber`` so logs compose with
//!     the rest of the Rust engine crates without reformatting.

use std::net::SocketAddr;
use std::time::Duration;

use anyhow::Result;
use axum::{
    extract::State,
    http::StatusCode,
    middleware,
    response::IntoResponse,
    routing::{any, get},
    Json, Router,
};
use tokio::net::TcpListener;
use tokio::signal;
use tower_http::cors::{AllowOrigin, CorsLayer};
use tower_http::trace::TraceLayer;
use tracing::info;

mod catalog;
mod config;
mod contact;
mod eventbus;
mod hmac_auth;
mod proxy;
mod session;
mod state;
mod ws;

use config::GatewayConfig;
use state::AppState;

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();

    let cfg = GatewayConfig::from_env()?;
    info!(?cfg, "gateway starting");

    let cors_origins = cfg.cors_origins.clone();
    let state = AppState::new(&cfg).await?;

    let app = build_router(state.clone(), &cors_origins);

    let addr: SocketAddr = cfg.bind_address.parse()?;
    let listener = TcpListener::bind(addr).await?;
    info!(%addr, "gateway listening");

    // ``into_make_service_with_connect_info`` lets the WS handler
    // read the client IP via ``ConnectInfo<SocketAddr>`` for
    // per-IP rate limiting.
    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    info!("gateway stopped");
    Ok(())
}

fn build_router(state: AppState, cors_origins: &[String]) -> Router {
    // Three tiers:
    //   1. Unauthenticated: health probes. MUST stay probeable even
    //      when HMAC misconfigures.
    //   2. HMAC-authenticated gateway endpoints: event bus, future
    //      session / kb / admin. Share the HMAC middleware.
    //   3. Reverse-proxy fallback: anything not matched by (1) or
    //      (2) goes to the Python backend. Also HMAC-authenticated.
    //
    // Adding a new first-party endpoint is "add it to `protected`
    // router"; adding a new health probe is "add it to `public`".
    // No parallel dispatcher layer.
    let public = Router::new()
        .route("/health", get(health))
        .route("/health/ready", get(health))
        .route("/health/live", get(health))
        // /stats is the welcome-screen counter block — hit by the SPA
        // before the user signs in, so HMAC headers can't be attached.
        // Mirrors the python backend's _EXEMPT_PREFIXES list. Proxies
        // through to the backend (no native gateway implementation
        // yet — owns 0 KB queries that need rust optimisation).
        .route("/stats", any(proxy::reverse_proxy))
        // /openapi.json + /docs / /redoc — same exemption rationale,
        // dev-tooling that reads the OpenAPI surface without auth.
        .route("/openapi.json", any(proxy::reverse_proxy))
        .route("/docs", any(proxy::reverse_proxy))
        .route("/docs/", any(proxy::reverse_proxy))
        .route("/redoc", any(proxy::reverse_proxy))
        .route("/redoc/", any(proxy::reverse_proxy))
        // /healthz is the python backend's worker-pool liveness probe
        // (returns 503 when the event-bus worker pool stalls — see
        // ``project_python_eventbus_workers_degrade.md``). Proxy
        // through unauthenticated so docker-compose's healthcheck +
        // any external monitor can read it without HMAC headers.
        .route("/healthz", any(proxy::reverse_proxy))
        // WebSocket endpoint is unauthenticated at the gateway tier —
        // the python backend's WS handshake performs its own
        // NextAuth-cookie / signed-query-param check (see
        // backend/main.py::websocket_endpoint). HMAC headers can't be
        // attached to a browser-initiated WS upgrade anyway. Keep the
        // proxy in the public tier so axum doesn't reject the upgrade
        // at the auth-middleware layer.
        .route("/ws", any(ws::ws_proxy))
        .route("/ws/", any(ws::ws_proxy))
        .route("/ws/*path", any(ws::ws_proxy));

    let protected = eventbus::routes()
        .merge(session::routes())
        .merge(catalog::routes())
        .merge(contact::routes())
        .fallback(any(proxy::reverse_proxy))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            hmac_auth::verify,
        ));

    // CORS layer — applies BEFORE the HMAC middleware so browser
    // preflight (OPTIONS) gets a proper response from the gateway
    // instead of being rejected with "No 'Access-Control-Allow-Origin'
    // header is present". Origins come from the gateway config
    // (`DORIAN_CORS_ORIGINS`); permissive headers/methods so SPA
    // FormData / JSON / URL-encoded posts all work without a
    // per-content-type carve-out.
    let cors = build_cors_layer(&cors_origins);

    Router::new()
        .merge(public)
        .merge(protected)
        .layer(TraceLayer::new_for_http())
        .layer(cors)
        .with_state(state)
}

fn build_cors_layer(origins: &[String]) -> CorsLayer {
    use axum::http::{header, Method};

    let allowed: Vec<axum::http::HeaderValue> = origins
        .iter()
        .filter_map(|o| o.parse().ok())
        .collect();

    CorsLayer::new()
        .allow_origin(AllowOrigin::list(allowed))
        .allow_credentials(true)
        .allow_methods([
            Method::GET, Method::POST, Method::PUT, Method::PATCH,
            Method::DELETE, Method::OPTIONS,
        ])
        .allow_headers([
            header::CONTENT_TYPE,
            header::AUTHORIZATION,
            header::ACCEPT,
            "x-hmac-signature".parse().unwrap(),
            "x-hmac-timestamp".parse().unwrap(),
            "x-hmac-nonce".parse().unwrap(),
            "x-vault-nonce".parse().unwrap(),
        ])
        .expose_headers([
            "x-hmac-signature".parse().unwrap(),
            "x-hmac-timestamp".parse().unwrap(),
            "x-hmac-nonce".parse().unwrap(),
        ])
}

#[derive(serde::Serialize)]
struct HealthResponse {
    status: &'static str,
    service: &'static str,
    version: &'static str,
}

async fn health(State(_state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(HealthResponse {
            status: "ok",
            service: "dorian-gateway",
            version: env!("CARGO_PKG_VERSION"),
        }),
    )
}

fn init_tracing() {
    // Honour RUST_LOG for level + target filtering; default to info-
    // level for the gateway crate, warn elsewhere so third-party
    // crates don't drown the operator's logs.
    use tracing_subscriber::{fmt, EnvFilter};
    // Binary is named ``dorian-gateway`` → Rust crate target is
    // ``dorian_gateway`` (hyphen → underscore). Keep the filter target
    // in sync or startup info-level logs get swallowed and the
    // container looks silent on boot.
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("dorian_gateway=info,tower_http=info,warn"));
    fmt()
        .with_env_filter(filter)
        .with_target(true)
        .compact()
        .init();
}

/// Block until SIGTERM or Ctrl-C arrives. Both are expected in
/// container environments — systemd / podman send SIGTERM, operators
/// hit Ctrl-C during interactive debug. Returning cleanly from here
/// lets axum drain in-flight requests before exit.
async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c().await.expect("install ctrl-c handler");
    };
    #[cfg(unix)]
    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => info!("received Ctrl-C"),
        _ = terminate => info!("received SIGTERM"),
    }
    // Small grace window so logs flush before tokio stops the
    // executor. Without this, the final "gateway stopped" line can
    // be lost under podman's log collector.
    tokio::time::sleep(Duration::from_millis(50)).await;
}
