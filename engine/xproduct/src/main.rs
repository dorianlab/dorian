//! Standalone binary entry point. Run via:
//!
//!   cargo run -p xproduct --release
//!
//! Or as a compose service (see ``docker-compose.yml`` — to be
//! added in the deploy step). Reads all configuration from env
//! vars; see `config::Config::from_env`.

use tracing_subscriber::EnvFilter;
use xproduct::{Config, Engine};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();
    let cfg = Config::from_env()?;
    let engine = Engine::new(cfg).await?;
    engine.run().await
}
