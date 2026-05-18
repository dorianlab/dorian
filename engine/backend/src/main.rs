//! Dorian backend — Rust replacement for ``backend/`` + python
//! ``dorian/event/handlers/*``. Subscribes to the Redis Streams the
//! gateway publishes to; dispatches each event through a registry of
//! rust handlers.
//!
//! At this commit it's a skeleton: subscriber loop + registry + one
//! demo handler (``RustBackendHeartbeat``). Each subsequent port
//! adds a handler to ``handlers/`` and removes the python equivalent
//! from ``dorian/event/registry.py`` in the same commit. Both
//! services share the eventbus stream during the migration —
//! consumer-group semantics guarantee each entry is delivered to
//! one rust OR one python consumer, never both, so the migration
//! is incremental and reversible.

use std::time::Duration;

use anyhow::Result;
use tokio::signal;
use tokio::sync::watch;
use tracing::info;

mod config;
mod emit;
mod event;
mod exec_jobs;
mod experiment_store;
mod handlers;
mod kb;
mod keys;
mod pg;
mod registry;
mod session;
mod state;
mod subscriber;

use config::Config;
use registry::build_default;
use state::AppState;

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();

    let cfg = Config::from_env()?;
    info!(?cfg, "backend starting");

    let state = AppState::new(cfg).await?;
    let registry = build_default();

    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    let subscriber_task = {
        let state = state.clone();
        let rx = shutdown_rx.clone();
        tokio::spawn(async move {
            if let Err(e) = subscriber::run(state, registry, rx).await {
                tracing::error!("subscriber failed: {e:?}");
            }
        })
    };

    shutdown_signal().await;
    info!("shutdown signal received; stopping subscriber");
    let _ = shutdown_tx.send(true);

    // Give the subscriber up to 5 s to finish in-flight handlers and
    // ack outstanding entries before the process exits.
    if let Err(_) = tokio::time::timeout(Duration::from_secs(5), subscriber_task).await {
        tracing::warn!("subscriber did not stop within 5s; exiting anyway");
    }
    info!("backend stopped");
    Ok(())
}

fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    // Crate target is ``backend`` (no hyphenation in the name).
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("backend=info,dorian_backend=info,warn"));
    fmt().with_env_filter(filter).with_target(true).compact().init();
}

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
}
