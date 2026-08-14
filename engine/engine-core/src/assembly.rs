//! Where the real parts get plugged in.
//!
//! The loop is generic over the traits in `engine-types`; this file names
//! the concrete crates exactly once. Nothing above it knows which venue,
//! which log format, or which kernel it is running.

use std::error::Error;
use std::path::Path;

use engine_marketdata::BybitPublicFeed;
use engine_risk::{
    EnvelopeConfig, Kernel, KernelConfig, LossGuardConfig, PartitionConfig, StrategyAllocation,
};
use engine_strategies::build_strategy;
use engine_types::{
    Strategy, StrategyId, Subscription, Symbol, VenueError, WalError, WalRecord,
};
use engine_venue::{BybitOrderFeed, Venue};
use engine_wal::WalWriter;
use serde::Deserialize;

use crate::config::{EngineSection, StrategyConfig};
use crate::targets::TargetBookWatcher;

/// Open the log and replay what an earlier run left. The writer truncates a
/// torn tail at the crash point before appending continues.
pub fn wal(path: &Path) -> Result<(WalWriter, Vec<WalRecord>), WalError> {
    let (writer, replayed) = WalWriter::open(path)?;
    Ok((writer, replayed.into_iter().map(|(_, r)| r).collect()))
}

/// The symbols the engine trades, in the one order every table uses: first
/// appearance across the strategies' subscriptions. The market feed, the
/// venue gateway, the private stream, and the core's own table all intern
/// this same sequence, so a `SymbolId` means the same symbol everywhere.
pub fn symbol_order(wanted: &[Subscription]) -> Vec<Symbol> {
    let mut names: Vec<Symbol> = Vec::new();
    for sub in wanted {
        if !names.iter().any(|n| n == &sub.symbol) {
            names.push(sub.symbol.clone());
        }
    }
    names
}

/// Bybit's public market stream, subscribed to exactly what was asked.
pub fn market_feed(wanted: &[Subscription]) -> BybitPublicFeed {
    BybitPublicFeed::new(wanted)
}

/// The venue's private order stream (demo host, credentials from the
/// environment).
pub fn order_feed(symbols: Vec<Symbol>) -> Result<BybitOrderFeed, VenueError> {
    BybitOrderFeed::new(symbols)
}

/// The venue the config named (credentials from the environment). The name
/// picks one of the adapters compiled into the venue crate — it is not an
/// address, and an unknown name is refused rather than defaulted to
/// somewhere nobody chose.
pub fn venue(name: &str, symbols: Vec<Symbol>) -> Result<Venue, VenueError> {
    Venue::by_name(name, symbols)
}

/// The target book watcher, but only when the config names a path. No path
/// means no watcher runs, and no book ever reaches a strategy — *no
/// decision*, which is not the same as an empty book.
pub fn target_book(settings: &EngineSection) -> Option<TargetBookWatcher> {
    let path = settings.target_book_path.as_ref()?;
    tracing::info!(path = %path.display(), "watching for a target book");
    Some(TargetBookWatcher::start(path.clone()))
}

/// The `[risk]` block, exactly as engine.toml spells it. There are no
/// defaults for the capital controls: every number is written down, and the
/// Python fleet's values are recorded in engine-risk/PORT_NOTES.md.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RiskSection {
    max_account_view_age_s: u64,
    /// Absent means no daily ceiling; the guard still refuses on blindness.
    max_daily_loss_usdt: Option<f64>,
    leverage: f64,
    min_order_notional_usdt: f64,
    #[serde(default = "default_qty_tolerance")]
    qty_tolerance: f64,
    envelope: EnvelopeSection,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EnvelopeSection {
    tracks_equity: bool,
    reference_usdt: f64,
    equity_fraction: f64,
    floor_usdt: f64,
    expand_dead_band_fraction: f64,
    gross_notional_multiple: f64,
    disaster_stop_fraction: f64,
}

fn default_qty_tolerance() -> f64 {
    1e-12
}

/// The four capital controls. Each strategy's `capital_usdt` is its margin
/// share of the partition; its gross share is that times the account
/// leverage. `Kernel::new` still proves the shares fit inside the account
/// caps and refuses a config that does not.
pub fn risk(
    section: &toml::Table,
    strategies: &[StrategyConfig],
) -> Result<Kernel, Box<dyn Error>> {
    let parsed: RiskSection = toml::Value::Table(section.clone())
        .try_into()
        .map_err(|e| format!("the [risk] block is wrong: {e}"))?;
    let allocations = strategies
        .iter()
        .enumerate()
        .map(|(index, s)| StrategyAllocation {
            strategy: StrategyId(index as u16),
            max_gross_notional_usdt: s.capital_usdt * parsed.leverage,
            max_initial_margin_usdt: s.capital_usdt,
        })
        .collect();
    let cfg = KernelConfig {
        max_account_view_age_ns: parsed.max_account_view_age_s.saturating_mul(1_000_000_000),
        loss_guard: LossGuardConfig {
            max_daily_loss_usdt: parsed.max_daily_loss_usdt,
        },
        envelope: EnvelopeConfig {
            tracks_equity: parsed.envelope.tracks_equity,
            reference_usdt: parsed.envelope.reference_usdt,
            equity_fraction: parsed.envelope.equity_fraction,
            floor_usdt: parsed.envelope.floor_usdt,
            expand_dead_band_fraction: parsed.envelope.expand_dead_band_fraction,
            gross_notional_multiple: parsed.envelope.gross_notional_multiple,
            disaster_stop_fraction: parsed.envelope.disaster_stop_fraction,
        },
        partition: PartitionConfig {
            allocations,
            leverage: parsed.leverage,
            min_order_notional_usdt: parsed.min_order_notional_usdt,
        },
        qty_tolerance: parsed.qty_tolerance,
    };
    Ok(Kernel::new(cfg).map_err(|e| format!("the risk kernel refuses this config: {e}"))?)
}

/// Name plus config block to a live strategy, ids in block order — the same
/// order the partition shares were derived in.
pub fn strategies(configured: &[StrategyConfig]) -> Result<Vec<Box<dyn Strategy>>, Box<dyn Error>> {
    let mut out: Vec<Box<dyn Strategy>> = Vec::with_capacity(configured.len());
    for (index, cfg) in configured.iter().enumerate() {
        let id = StrategyId(u16::try_from(index).map_err(|_| "more than 65535 strategies")?);
        let params = toml::Value::Table(cfg.params.clone());
        out.push(build_strategy(&cfg.name, id, &params).map_err(|e| e.to_string())?);
    }
    Ok(out)
}
