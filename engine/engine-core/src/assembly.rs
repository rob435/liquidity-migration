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
use crate::targets::{TargetBookWatcher, TargetBooks};

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

/// Every target book this engine watches, each bound to the one strategy that
/// named its path. No paths means no watcher runs and no book ever reaches a
/// strategy — *no decision*, which is not the same as an empty book.
///
/// Two ways to say it, and they cannot be mixed:
///
/// - `book_path` inside a `[[strategy]]` block. This is how an account with
///   more than one sleeve is configured, because it says which book is whose.
/// - `target_book_path` in `[engine]`, which predates routing and means "the
///   one follower's book". Kept because it is what existing configs say, and
///   refused the moment there is more than one follower to give it to.
///
/// Both directions of the wiring are checked. A path on a strategy that
/// ignores books is a file nobody reads; a follower with no path is a sleeve
/// that will never be told what to hold, and would sit there looking healthy.
pub fn target_books(
    settings: &EngineSection,
    configured: &[StrategyConfig],
    built: &[Box<dyn Strategy>],
) -> Result<TargetBooks, Box<dyn Error>> {
    let followers: Vec<usize> = built
        .iter()
        .enumerate()
        .filter(|(_, s)| s.follows_a_target_book())
        .map(|(index, _)| index)
        .collect();

    let mut paths: Vec<(usize, PathBuf)> = Vec::new();
    for (index, cfg) in configured.iter().enumerate() {
        let Some(path) = cfg.book_path.as_ref() else {
            continue;
        };
        if !followers.contains(&index) {
            return Err(format!(
                "strategy \"{}\" names a book_path, but it does not act on target books \
                 at all — that file would never be read",
                cfg.name
            )
            .into());
        }
        paths.push((index, path.clone()));
    }

    if let Some(engine_path) = settings.target_book_path.as_ref() {
        if !paths.is_empty() {
            return Err("[engine] target_book_path and a strategy's own book_path are two \
                        ways of saying the same thing; keep the per-strategy one, which \
                        says whose book it is"
                .to_string()
                .into());
        }
        match followers.as_slice() {
            [] => {
                return Err(
                    "[engine] target_book_path is set, but no strategy acts on target books"
                        .to_string()
                        .into(),
                )
            }
            [only] => paths.push((*only, engine_path.clone())),
            many => {
                return Err(format!(
                    "[engine] target_book_path cannot say which of the {} book-following \
                     strategies it belongs to; give each one its own book_path",
                    many.len()
                )
                .into())
            }
        }
    }

    for index in &followers {
        if !paths.iter().any(|(at, _)| at == index) {
            return Err(format!(
                "strategy \"{}\" follows a target book but no path was given for it; it \
                 would run, and hold nothing, for ever",
                configured
                    .get(*index)
                    .map(|c| c.name.as_str())
                    .unwrap_or("?")
            )
            .into());
        }
    }

    let watchers = paths
        .into_iter()
        .map(|(index, path)| {
            tracing::info!(
                strategy = index,
                path = %path.display(),
                "watching for a target book"
            );
            (
                StrategyId(index as u16),
                TargetBookWatcher::start(path),
            )
        })
        .collect();
    Ok(TargetBooks::new(watchers))
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

/// The other way to say what the caps are: name the fleet's own profile.
///
/// `deny_unknown_fields` is doing real work here. It means an `[risk.envelope]`
/// block or a stray `leverage` left beside the profile path is refused rather
/// than silently ignored — one document decides the caps, not two.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileRiskSection {
    /// The operational profile to read, e.g. `configs/operational.mainnet.json`.
    operational_profile_path: PathBuf,
    max_account_view_age_s: u64,
    min_order_notional_usdt: f64,
    /// How far a position may move against it before its stop ends it. Not in
    /// the profile — it is `DISASTER_STOP_FRACTION` on the host — so it is
    /// named here rather than guessed.
    disaster_stop_fraction: f64,
}

fn default_qty_tolerance() -> f64 {
    1e-12
}

/// The four capital controls, from one of two places.
///
/// Either the caps are written out in `[risk]`, and each strategy's
/// `capital_usdt` is its margin share of the partition; or `[risk]` names the
/// fleet's operational profile and the caps and the sleeve shares come from
/// that document. Naming a profile is what the funded account does, so the
/// engine and the Python fleet are held to one file rather than to two copies
/// of it.
///
/// Either way `Kernel::new` proves the shares fit inside the account caps and
/// refuses a config that does not.
pub fn risk(
    section: &toml::Table,
    strategies: &[StrategyConfig],
) -> Result<Kernel, Box<dyn Error>> {
    if section.contains_key("operational_profile_path") {
        return risk_from_profile(section, strategies);
    }
    let parsed: RiskSection = toml::Value::Table(section.clone())
        .try_into()
        .map_err(|e| format!("the [risk] block is wrong: {e}"))?;
    let mut allocations = Vec::with_capacity(strategies.len());
    for (index, s) in strategies.iter().enumerate() {
        let Some(capital_usdt) = s.capital_usdt else {
            return Err(format!(
                "strategy \"{}\" has no capital_usdt, and [risk] names no \
                 operational_profile_path — one of the two has to say how much of the \
                 account this strategy may use",
                s.name
            )
            .into());
        };
        if s.sleeve.is_some() {
            return Err(format!(
                "strategy \"{}\" names a sleeve, but [risk] names no \
                 operational_profile_path — there is no profile for that sleeve to be in",
                s.name
            )
            .into());
        }
        allocations.push(StrategyAllocation {
            strategy: StrategyId(index as u16),
            max_gross_notional_usdt: capital_usdt * parsed.leverage,
            max_initial_margin_usdt: capital_usdt,
        });
    }
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

/// The caps, read from the fleet's own operational profile.
///
/// Every strategy must appear in the profile's `sleeve_limits`, and every
/// sleeve in the profile must be a strategy the engine runs. Both directions
/// matter and they fail differently:
///
/// - A sleeve with no strategy is caught by the profile loader. Its share
///   would be counted in the sums that prove the partition fits, so the other
///   sleeves would look smaller than they are.
/// - A strategy with no sleeve is caught here. The kernel refuses every order
///   from a strategy the partition does not name, so it would start, subscribe,
///   decide, and have every order denied — a sleeve that is running and cannot
///   trade, which reads from the outside like a quiet market.
fn risk_from_profile(
    section: &toml::Table,
    strategies: &[StrategyConfig],
) -> Result<Kernel, Box<dyn Error>> {
    let parsed: ProfileRiskSection = toml::Value::Table(section.clone())
        .try_into()
        .map_err(|e| format!("the [risk] block is wrong: {e}"))?;

    for s in strategies {
        if s.capital_usdt.is_some() {
            return Err(format!(
                "strategy \"{}\" sets capital_usdt, but [risk] takes the sleeve shares from \
                 {}. Two answers to one question: take the capital_usdt line out.",
                s.name,
                parsed.operational_profile_path.display()
            )
            .into());
        }
    }

    let mut sleeves: Vec<(&str, StrategyId)> = Vec::with_capacity(strategies.len());
    for (index, s) in strategies.iter().enumerate() {
        let name = s.sleeve_name();
        if let Some((_, first)) = sleeves.iter().find(|(seen, _)| *seen == name) {
            return Err(format!(
                "two strategy blocks both claim the sleeve \"{name}\" (the first is #{}); \
                 one sleeve is one share of the account, so give one of them its own \
                 `sleeve = ...`",
                first.0
            )
            .into());
        }
        sleeves.push((name, StrategyId(index as u16)));
    }

    let text = std::fs::read_to_string(&parsed.operational_profile_path).map_err(|e| {
        format!(
            "cannot read the operational profile {}: {e}",
            parsed.operational_profile_path.display()
        )
    })?;
    let cfg = engine_risk::kernel_config_from_profile(
        &text,
        &engine_risk::ProfileInputs {
            disaster_stop_fraction: parsed.disaster_stop_fraction,
            min_order_notional_usdt: parsed.min_order_notional_usdt,
            max_account_view_age_ns: parsed.max_account_view_age_s.saturating_mul(1_000_000_000),
            sleeves: &sleeves,
        },
    )
    .map_err(|e| {
        format!(
            "{} is not a profile this engine can run: {e}",
            parsed.operational_profile_path.display()
        )
    })?;

    // The other direction. An empty partition means the profile named no
    // sleeves at all, which is a legitimate unpartitioned profile — there the
    // account caps bind and no strategy is left unable to trade.
    if !cfg.partition.allocations.is_empty() {
        for (name, id) in &sleeves {
            if cfg.partition.share(*id).is_none() {
                return Err(format!(
                    "the engine runs the sleeve \"{name}\", but {} gives it no share of the \
                     account. It would start and then have every order refused.",
                    parsed.operational_profile_path.display()
                )
                .into());
            }
        }
    }

    tracing::info!(
        profile = %parsed.operational_profile_path.display(),
        reference_usdt = cfg.envelope.reference_usdt,
        account_gross_cap_usdt = cfg.envelope.account_gross_cap_usdt(),
        sleeves = cfg.partition.allocations.len(),
        "capital limits read from the fleet's operational profile"
    );
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
/// caller's. Two strategies claiming one symbol is a config saying two things
/// at once, and it is refused here rather than resolved at run time.
///
/// This is not the only line of defence, and no longer the load-bearing one.
/// A target book may name a symbol no config listed, and the engine takes that
/// name on while it runs, so overlap can arrive hours after boot where this
/// check cannot see it. `StrategyCtx::foreign_position` is what answers it
/// then: a plug leaves alone any name another strategy is holding. This stays
/// because a config that means to overlap should be corrected, not tolerated.
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
        StrategyConfig { name: "touch_sniper".into(), capital_usdt: Some(50.0), sleeve: None, book_path: None, params }
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
        StrategyConfig { name: "quoter".into(), capital_usdt: Some(50.0), sleeve: None, book_path: None, params }
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

    // ----------------------------------------------------------------------
    // Taking the caps from the fleet's operational profile instead
    // ----------------------------------------------------------------------

    /// The `[risk]` block a funded engine would ship: no caps of its own, a
    /// path to the document the Python fleet already reads.
    fn profile_risk(profile: &str) -> toml::Table {
        risk_block(&format!(
            r#"
operational_profile_path = "../../configs/{profile}"
max_account_view_age_s = 120
min_order_notional_usdt = 1.0
disaster_stop_fraction = 0.35
"#
        ))
    }

    /// `Kernel` has no Debug, so `expect_err` cannot be used on these.
    fn refusal(result: Result<Kernel, Box<dyn Error>>, what: &str) -> String {
        match result {
            Ok(_) => panic!("{what}"),
            Err(err) => err.to_string(),
        }
    }

    /// A strategy block in profile mode: no capital of its own, a sleeve name.
    fn sleeve_strategy(sleeve: &str, symbol: &str) -> StrategyConfig {
        let mut cfg = sniper(symbol);
        cfg.capital_usdt = None;
        cfg.sleeve = Some(sleeve.to_string());
        cfg
    }

    #[test]
    fn the_shipped_mainnet_profile_builds_a_kernel() {
        let sleeves = [
            sleeve_strategy("carry", "BTCUSDT"),
            sleeve_strategy("long", "ETHUSDT"),
        ];
        risk(&profile_risk("operational.mainnet.json"), &sleeves)
            .expect("the funded account's own profile must build a kernel");
    }

    #[test]
    fn a_strategy_that_also_states_its_capital_is_refused() {
        // Two answers to one question. Which one binds would depend on which
        // line someone read.
        let mut sleeves = vec![
            sleeve_strategy("carry", "BTCUSDT"),
            sleeve_strategy("long", "ETHUSDT"),
        ];
        sleeves[0].capital_usdt = Some(40.0);
        let err = refusal(risk(&profile_risk("operational.mainnet.json"), &sleeves), "capital_usdt beside a profile was accepted");
        assert!(err.to_string().contains("capital_usdt"), "{err}");
    }

    #[test]
    fn caps_written_beside_a_named_profile_are_refused() {
        // The same worry from the other side: an [envelope] block left in the
        // file when the profile took over would be a set of limits nobody is
        // enforcing, sitting somewhere an operator would read them.
        let mut block = profile_risk("operational.mainnet.json");
        block.insert("leverage".into(), toml::Value::Float(2.0));
        let err = refusal(risk(&block, &[sleeve_strategy("carry", "BTCUSDT")]), "a stray cap beside a profile was accepted");
        assert!(err.to_string().contains("leverage"), "{err}");
    }

    #[test]
    fn a_sleeve_the_profile_does_not_fund_is_refused() {
        // It would boot, subscribe, decide, and have every order denied by the
        // partition -- which from outside looks like a quiet market.
        let sleeves = [
            sleeve_strategy("carry", "BTCUSDT"),
            sleeve_strategy("long", "ETHUSDT"),
            sleeve_strategy("hedge", "SOLUSDT"),
        ];
        let err = refusal(risk(&profile_risk("operational.mainnet.json"), &sleeves), "an unfunded sleeve was accepted");
        assert!(err.to_string().contains("hedge"), "{err}");
    }

    #[test]
    fn a_profile_sleeve_with_no_strategy_is_refused() {
        // The other direction. Its share is counted in the sums that prove the
        // partition fits inside the account, so leaving it out would make the
        // sleeves that are running look smaller than they are.
        let err = refusal(risk(
            &profile_risk("operational.mainnet.json"),
            &[sleeve_strategy("carry", "BTCUSDT")],
        ), "a profile naming a sleeve nobody runs was accepted");
        assert!(err.to_string().contains("long"), "{err}");
    }

    #[test]
    fn two_strategies_cannot_claim_one_sleeve() {
        let sleeves = [
            sleeve_strategy("carry", "BTCUSDT"),
            sleeve_strategy("carry", "ETHUSDT"),
        ];
        let err = refusal(risk(&profile_risk("operational.mainnet.json"), &sleeves), "one share of the account was handed to two strategies");
        assert!(err.to_string().contains("carry"), "{err}");
    }

    // ----------------------------------------------------------------------
    // Which book belongs to which strategy
    // ----------------------------------------------------------------------

    /// A strategy that follows books, and one that does not, without
    /// depending on any real plug's parameter schema.
    struct Listener {
        follows: bool,
    }

    impl engine_types::Strategy for Listener {
        fn name(&self) -> &str {
            "listener"
        }
        fn subscriptions(&self) -> Vec<Subscription> {
            Vec::new()
        }
        fn on_event(
            &mut self,
            _event: &engine_types::EngineEvent,
            _ctx: &mut dyn engine_types::StrategyCtx,
        ) {
        }
        fn follows_a_target_book(&self) -> bool {
            self.follows
        }
    }

    fn built(follows: &[bool]) -> Vec<Box<dyn Strategy>> {
        follows
            .iter()
            .map(|f| Box::new(Listener { follows: *f }) as Box<dyn Strategy>)
            .collect()
    }

    fn blocks(paths: &[Option<&str>]) -> Vec<StrategyConfig> {
        paths
            .iter()
            .enumerate()
            .map(|(i, path)| StrategyConfig {
                name: format!("s{i}"),
                capital_usdt: Some(10.0),
                sleeve: None,
                book_path: path.map(PathBuf::from),
                params: toml::Table::new(),
            })
            .collect()
    }

    fn settings(engine_level_book: Option<&str>) -> EngineSection {
        EngineSection {
            wal_path: PathBuf::from("engine.wal"),
            venue: "bybit_demo".into(),
            group_flush_ms: 250,
            account_view_max_age_ms: 5_000,
            shadow: true,
            target_book_path: engine_level_book.map(PathBuf::from),
            heartbeat_path: None,
        }
    }

    fn books_error(
        engine_level: Option<&str>,
        paths: &[Option<&str>],
        follows: &[bool],
        what: &str,
    ) -> String {
        match target_books(&settings(engine_level), &blocks(paths), &built(follows)) {
            Ok(_) => panic!("{what}"),
            Err(err) => err.to_string(),
        }
    }

    #[tokio::test]
    async fn two_sleeves_each_get_their_own_book() {
        let books = target_books(
            &settings(None),
            &blocks(&[Some("carry.json"), Some("long.json")]),
            &built(&[true, true]),
        )
        .expect("two sleeves with two books is the funded shape");
        assert_eq!(books.len(), 2);
    }

    #[tokio::test]
    async fn the_engine_level_path_still_works_for_a_single_follower() {
        // What configs written before routing say. It still means what it
        // meant, because there is only one strategy it could have meant.
        let books = target_books(
            &settings(Some("carry.json")),
            &blocks(&[None]),
            &built(&[true]),
        )
        .expect("one follower, one book");
        assert_eq!(books.len(), 1);
    }

    #[tokio::test]
    async fn the_engine_level_path_is_refused_once_there_are_two_followers() {
        // It has no way to say whose book it is, and guessing would hand one
        // sleeve's decisions to whichever plug happened to be first.
        let err = books_error(
            Some("carry.json"),
            &[None, None],
            &[true, true],
            "an ambiguous book path was accepted",
        );
        assert!(err.contains("book_path"), "{err}");
    }

    #[tokio::test]
    async fn saying_it_both_ways_at_once_is_refused() {
        let err = books_error(
            Some("carry.json"),
            &[Some("carry.json")],
            &[true],
            "two ways of naming one book were accepted",
        );
        assert!(err.contains("target_book_path"), "{err}");
    }

    #[tokio::test]
    async fn a_book_given_to_a_strategy_that_ignores_books_is_refused() {
        // The file would never be read, and the operator would be watching a
        // producer write into nothing.
        let err = books_error(
            None,
            &[Some("carry.json")],
            &[false],
            "a book for a strategy that ignores books was accepted",
        );
        assert!(err.contains("never be read"), "{err}");
    }

    #[tokio::test]
    async fn a_follower_with_no_book_is_refused() {
        // It would boot, subscribe, and hold nothing for ever -- which from
        // outside looks exactly like a sleeve with nothing to do.
        let err = books_error(
            None,
            &[None],
            &[true],
            "a follower with no book was accepted",
        );
        assert!(err.contains("no path was given"), "{err}");
    }

    #[tokio::test]
    async fn no_books_at_all_is_a_normal_config() {
        // Nothing here follows a book, so nothing is watched. That is the
        // touch-sniper shape, and it is not a fault.
        let books = target_books(&settings(None), &blocks(&[None]), &built(&[false]))
            .expect("a config with no books at all is fine");
        assert!(books.is_empty());
    }

    #[test]
    fn the_sleeve_name_falls_back_to_the_strategy_name() {
        let mut cfg = sniper("BTCUSDT");
        cfg.capital_usdt = None;
        assert_eq!(cfg.sleeve_name(), "touch_sniper");
        cfg.sleeve = Some("carry".into());
        assert_eq!(cfg.sleeve_name(), "carry");
    }

    #[test]
    fn a_missing_profile_file_says_which_file() {
        let err = refusal(risk(
            &profile_risk("operational.does-not-exist.json"),
            &[sleeve_strategy("carry", "BTCUSDT")],
        ), "a missing profile was accepted");
        assert!(err.to_string().contains("does-not-exist"), "{err}");
    }

    #[test]
    fn without_a_profile_a_strategy_must_still_state_its_capital() {
        let mut cfg = sniper("BTCUSDT");
        cfg.capital_usdt = None;
        let err = refusal(risk(&risk_block(RISK), &[cfg]), "a strategy with no capital booted");
        assert!(err.to_string().contains("capital_usdt"), "{err}");
    }

    #[test]
    fn without_a_profile_a_sleeve_name_means_nothing_and_is_refused() {
        let mut cfg = sniper("BTCUSDT");
        cfg.sleeve = Some("carry".into());
        let err = refusal(risk(&risk_block(RISK), &[cfg]), "a sleeve with no profile booted");
        assert!(err.to_string().contains("sleeve"), "{err}");
    }
}
