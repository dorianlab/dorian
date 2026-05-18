//! Dorian engine — composition root and gRPC server.
//!
//! This binary starts the Rust engine process, which:
//! 1. Initializes the engine state (executions, event log, cancel flags)
//! 2. Starts the gRPC server for the Go gateway to connect to
//! 3. (Phase 3.2) Initializes runtime dispatch (Python subprocess pool)
//! 4. (Phase 3.2) Starts the scaling controller
//!
//! Configuration via environment variables:
//!   DORIAN_ENGINE_PORT  — gRPC listen port (default: 50051)
//!   RUST_LOG            — tracing filter (default: info)

use std::net::SocketAddr;
use std::sync::Arc;

use engine::grpc::{start_grpc_server, EngineState};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize tracing (structured logging).
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    // Parse configuration.
    let port: u16 = std::env::var("DORIAN_ENGINE_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(50051);
    let addr: SocketAddr = format!("0.0.0.0:{port}").parse()?;

    // Initialize shared engine state.
    let state = Arc::new(EngineState::new());

    tracing::info!(port = %port, "dorian engine starting");

    // Start gRPC server (blocks until shutdown).
    start_grpc_server(addr, state).await?;

    Ok(())
}
