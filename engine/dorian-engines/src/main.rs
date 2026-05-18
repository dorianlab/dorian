//! `dorian-engines` — supervisor binary for every Rust engine.
//!
//! Runs the cross-product fill-in and the AutoML BO loop as
//! concurrent tokio tasks within one process. Single Postgres
//! pool, single Redis pool, single resource budget. RL is still
//! Python (`rl-trainer` container) but ports into this binary
//! over time so the final state is one engine container per host.
//!
//! Each engine surfaces a top-level `run_*_loop` async function
//! that runs forever (until cancelled). The supervisor spawns
//! them, joins on the first failure, and propagates shutdown
//! signals.

use anyhow::Result;
use tokio::signal::unix::{signal, SignalKind};
use tracing::{error, info};
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    info!("dorian-engines: starting");

    // Both engines are wired now. Cross-product fills the cartesian
    // gap between every saved pipeline and every saved dataset
    // (broad coverage); AutoML drives template optimisation via the
    // BO library (deep coverage on selected templates). Trial loops
    // for both write to the same `evaluations` table — the unified
    // Trial schema (see ``dorian.experiment.trial``).
    let xproduct_handle = tokio::spawn(run_xproduct());
    let automl_handle = tokio::spawn(automl::run_automl_driver());
    let reaper_handle = tokio::spawn(run_stream_reaper());

    // Graceful shutdown on SIGTERM (compose stop) / SIGINT (Ctrl-C).
    let mut sigterm = signal(SignalKind::terminate())?;
    let mut sigint = signal(SignalKind::interrupt())?;

    tokio::select! {
        _ = sigterm.recv() => info!("SIGTERM received — shutting down"),
        _ = sigint.recv()  => info!("SIGINT received — shutting down"),
        r = xproduct_handle => {
            match r {
                Ok(Ok(())) => info!("xproduct exited cleanly"),
                Ok(Err(e)) => error!(error=%e, "xproduct failed"),
                Err(e) => error!(error=%e, "xproduct task panicked"),
            }
        }
        r = automl_handle => {
            match r {
                Ok(Ok(())) => info!("automl exited cleanly"),
                Ok(Err(e)) => error!(error=%e, "automl failed"),
                Err(e) => error!(error=%e, "automl task panicked"),
            }
        }
        r = reaper_handle => {
            match r {
                Ok(Ok(())) => info!("stream reaper exited cleanly"),
                Ok(Err(e)) => error!(error=%e, "stream reaper failed"),
                Err(e) => error!(error=%e, "stream reaper task panicked"),
            }
        }
    }
    info!("dorian-engines: stopped");
    Ok(())
}


/// Bootstrap + run the cross-product engine.
async fn run_xproduct() -> Result<()> {
    let cfg = xproduct::Config::from_env()?;
    let engine = xproduct::Engine::new(cfg).await?;
    engine.run().await
}


// (AutoML now uses ``automl::run_automl_driver()`` — placeholder
// retired in favour of the real BO loop.)

/// Bootstrap + run the stream reaper (orphan trial-stream cleaner).
async fn run_stream_reaper() -> Result<()> {
    let cfg = xproduct::Config::from_env()?;
    let reaper = xproduct::StreamReaper::from_env(&cfg.redis_url)?;
    reaper.run().await
}
