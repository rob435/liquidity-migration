//! `engine run`: the full assembly.
//!
//! Every part comes from `assembly.rs`. While a crate is still empty its
//! constructor returns "not wired yet", so this command starts, names the
//! missing part, and stops without writing to the log or touching a venue.

use std::error::Error;
use std::path::Path;

use engine_types::Strategy;

use crate::assembly;
use crate::config;
use crate::engine::Engine;

pub async fn run(config_path: &Path, live: bool) -> Result<(), Box<dyn Error>> {
    let loaded = config::load(config_path)?;
    let mut settings = loaded.config.engine.clone();
    if live {
        settings.shadow = false;
    }
    tracing::info!(
        config = %config_path.display(),
        hash = %loaded.sha256,
        shadow = settings.shadow,
        strategies = loaded.config.strategies.len(),
        "starting"
    );
    if !settings.shadow {
        tracing::warn!("live: orders will be sent, and the risk kernel gates every one of them");
    }

    let strategies: Vec<Box<dyn Strategy>> = assembly::strategies(&loaded.config.strategies)?;
    let wanted: Vec<_> = strategies
        .iter()
        .flat_map(|s| s.subscriptions())
        .collect::<Vec<_>>();
    let symbols = assembly::symbol_order(&wanted);
    let risk = assembly::risk(&loaded.config.risk, &loaded.config.strategies)?;
    let venue = assembly::venue(symbols.clone())?;
    let mut market_feed = assembly::market_feed(&wanted);
    let mut order_feed = assembly::order_feed(symbols)?;

    let (wal, replayed) = assembly::wal(&settings.wal_path)?;

    let mut engine = Engine::boot(
        &settings,
        &loaded.sha256,
        wal,
        risk,
        venue,
        strategies,
        &replayed,
    )
    .await?;

    if let Some(watcher) = assembly::target_book(&settings) {
        engine.watch_targets(watcher);
    }

    let outcome = engine
        .run(&mut market_feed, &mut order_feed, async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    tracing::info!(?outcome, "stopped");
    Ok(())
}
