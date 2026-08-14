//! Where the real parts get plugged in.
//!
//! The loop is generic over the traits in `engine-types`; this file names
//! the concrete crates exactly once. Nothing above it knows which venue,
//! which log format, or which kernel it is running.

use std::error::Error;
use std::path::{Path, PathBuf};

use engine_marketdata::BybitPublicFeed;
use engine_risk::{
    EnvelopeConfig, Kernel, KernelConfig, LossGuardConfig, PartitionConfig, StrategyAllocation,
};
use engine_strategies::build_strategy;
use engine_types::{
    AccountIdentity, Strategy, StrategyId, Subscription, Symbol, VenueError, WalError, WalRecord,
};
use engine_venue::{BybitOrderFeed, Venue, VenueRealm};
use engine_wal::WalWriter;
use serde::Deserialize;

use crate::config::{EngineSection, StrategyConfig};
use crate::heartbeat::Heartbeat;
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

/// The venue's private order stream, on the same account the gateway trades.
///
/// The realm is taken from the built venue rather than re-read from config,
/// so the stream that reports fills and the gateway that causes them cannot
/// end up on different accounts.
pub fn order_feed(realm: VenueRealm, symbols: Vec<Symbol>) -> Result<BybitOrderFeed, VenueError> {
    BybitOrderFeed::new(realm, symbols)
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

/// The heartbeat writer, but only when the config names a path. No path means
/// no file is written and nothing at all is said about it — an engine nobody
/// asked to report on itself is not a fault, and a line every few seconds
/// saying so would be noise in every log the fleet keeps.
///
/// `account` and `lease_path` are what the run has already learned: whose
/// account these credentials open, and the lock file this process holds. A
/// shadow run holds no lease, and a run that cannot reach the venue never
/// learns the account; both are written into the file as null.
pub fn heartbeat(
    settings: &EngineSection,
    account: Option<AccountIdentity>,
    lease_path: Option<PathBuf>,
) -> Option<Heartbeat> {
    let path = settings.heartbeat_path.as_ref()?;
    tracing::info!(path = %path.display(), "writing a heartbeat file");
    Some(Heartbeat::new(path.clone(), account, lease_path))
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
    max_symbol_notional_usdt: f64,
    max_component_gross_notional_usdt: f64,
    max_initial_margin_usdt: f64,
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
            max_symbol_notional_usdt: parsed.envelope.max_symbol_notional_usdt,
            max_component_gross_notional_usdt: parsed.envelope.max_component_gross_notional_usdt,
            max_initial_margin_usdt: parsed.envelope.max_initial_margin_usdt,
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
    one_owner_per_symbol(&out)?;
    Ok(out)
}

/// Refuse a config where two strategies want the same symbol.
///
/// The venue holds one position per symbol and keeps no note of who asked for
/// it, so `StrategyCtx::position` reports the account's holding, not the
/// caller's. Two strategies on one symbol therefore each read the other's
/// fills as their own and size against them — the second one enters on top of
/// a position it did not open, and both try to exit it. There is no way for a
/// strategy to tell the difference, so the config is refused here instead.
///
/// Two behaviours on one symbol is one strategy with two branches.
fn one_owner_per_symbol(built: &[Box<dyn Strategy>]) -> Result<(), Box<dyn Error>> {
    let mut claimed: Vec<(String, &str)> = Vec::new();
    for strategy in built {
        let mut mine: Vec<String> = Vec::new();
        for sub in strategy.subscriptions() {
            // A strategy may want two feeds on one symbol; that is one claim.
            if mine.iter().any(|s| s == &sub.symbol) {
                continue;
            }
            mine.push(sub.symbol.clone());
        }
        for symbol in mine {
            if let Some((_, first)) = claimed.iter().find(|(name, _)| name == &symbol) {
                return Err(format!(
                    "{symbol} is claimed by both \"{first}\" and \"{}\": the venue holds one \
                     position per symbol and cannot say which strategy it belongs to, so each \
                     would read the other's fills as its own",
                    strategy.name()
                )
                .into());
            }
            claimed.push((symbol, strategy.name()));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sniper(symbol: &str) -> StrategyConfig {
        let params: toml::Table = toml::from_str(&format!(
            r#"
            symbol = "{symbol}"
            side = "buy"
            trigger_px = 100.0
            qty = 0.01
            stop_px = 90.0
        "#
        ))
        .expect("test config parses");
        StrategyConfig { name: "touch_sniper".into(), capital_usdt: 50.0, params }
    }

    fn quoter(symbol: &str) -> StrategyConfig {
        let params: toml::Table = toml::from_str(&format!(
            r#"
            symbol = "{symbol}"
            half_spread_bps = 10.0
            requote_bps = 2.0
            qty = 0.1
            max_position = 0.3
            stop_loss_fraction = 0.35
        "#
        ))
        .expect("test config parses");
        StrategyConfig { name: "quoter".into(), capital_usdt: 50.0, params }
    }

    /// The shipped engine.toml `[risk]` block.
    const RISK: &str = r#"
max_account_view_age_s = 120
max_daily_loss_usdt = 10.0
leverage = 2.0
min_order_notional_usdt = 1.0

[envelope]
tracks_equity = true
reference_usdt = 100.0
equity_fraction = 1.0
floor_usdt = 100.0
expand_dead_band_fraction = 0.05
gross_notional_multiple = 2.0
disaster_stop_fraction = 0.35
max_symbol_notional_usdt = 100.0
max_component_gross_notional_usdt = 200.0
max_initial_margin_usdt = 100.0
"#;

    fn risk_block(text: &str) -> toml::Table {
        toml::from_str(text).expect("the test block parses as toml")
    }

    #[test]
    fn the_shipped_risk_block_builds_a_kernel() {
        assert!(risk(&risk_block(RISK), &[sniper("BTCUSDT")]).is_ok());
    }

    #[test]
    // A config written before these caps existed names no value for them.
    // Guessing one would be a capital control nobody chose, so it is refused.
    fn a_risk_block_missing_a_capital_cap_is_refused() {
        for key in [
            "max_symbol_notional_usdt",
            "max_component_gross_notional_usdt",
            "max_initial_margin_usdt",
        ] {
            let without: String = RISK
                .lines()
                .filter(|line| !line.starts_with(key))
                .collect::<Vec<_>>()
                .join("\n");
            let err = risk(&risk_block(&without), &[sniper("BTCUSDT")])
                .err()
                .unwrap_or_else(|| panic!("a block with no {key} must not boot"));
            let text = err.to_string();
            assert!(text.contains(key), "{key}: got {text}");
            // Refused for being absent, not for having been filled in with
            // something. A default would also be refused here — by the cap's
            // own "must be positive" check — and that would leave this test
            // passing while the key quietly had a value nobody chose.
            assert!(
                text.contains("missing field"),
                "{key} must be refused as missing, not defaulted: got {text}"
            );
        }
    }

    #[test]
    fn strategies_on_different_symbols_are_fine() {
        let built = strategies(&[sniper("BTCUSDT"), quoter("ETHUSDT")])
            .expect("two strategies, two symbols");
        assert_eq!(built.len(), 2);
    }

    #[test]
    fn two_strategies_on_one_symbol_are_refused() {
        let Err(err) = strategies(&[sniper("BTCUSDT"), quoter("BTCUSDT")]) else {
            panic!("the venue cannot say whose position BTCUSDT is; this must not boot");
        };
        let text = err.to_string();
        assert!(text.contains("BTCUSDT"), "{text}");
        assert!(text.contains("touch_sniper"), "{text}");
        assert!(text.contains("quoter"), "{text}");
    }

    #[test]
    fn the_same_strategy_twice_on_one_symbol_is_refused() {
        assert!(strategies(&[sniper("BTCUSDT"), sniper("BTCUSDT")]).is_err());
    }
}
