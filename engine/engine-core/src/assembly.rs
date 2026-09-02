//! Where the real parts get plugged in.
//!
//! The loop is generic over the traits in `engine-types`; this file names
//! the concrete crates exactly once. Nothing above it knows which venue,
//! which log format, or which kernel it is running.

use std::error::Error;
use std::path::{Path, PathBuf};

use engine_marketdata::MarketFeeds;
use engine_risk::{EnvelopeConfig, Kernel, KernelConfig};
use engine_strategies::build_strategy;
use engine_types::{
    AccountIdentity, Strategy, StrategyId, Subscription, Symbol, VenueError, WalError, WalRecord,
};
use engine_venue::{InventoryProbe, OrderFeeds, Venue, VenueName};
use engine_wal::WalWriter;
use serde::Deserialize;

use crate::config::{EngineSection, StrategyConfig};
use crate::heartbeat::Heartbeat;
use crate::trades::Trades;

/// Open the log and replay what an earlier run left. The log may have been
/// rotated into segments: this opens the newest one boot can trust, whose
/// restatement carries everything the older ones said. The writer truncates
/// a torn tail at the crash point before appending continues; a rotation
/// that crashed half-written fails the trust check instead, and boot falls
/// back to the segment before it.
pub fn wal(path: &Path) -> Result<(WalWriter, Vec<WalRecord>), WalError> {
    let (writer, replayed) = engine_wal::open_current(path)?;
    Ok((writer, replayed.into_iter().map(|(_, r)| r).collect()))
}

/// The symbols the engine trades, in the one order every table uses: the
/// previous run's own table first (the newest `Names` record the log
/// replayed, in its id order), then first appearance across the strategies'
/// subscriptions. The market feed, the venue gateway, the private stream,
/// and the core's own table all intern this same sequence, so a `SymbolId`
/// means the same symbol everywhere.
///
/// Starting from the log's table is what keeps an id meaning the same
/// symbol across a restart. Ids are interning positions, and every join the
/// boot makes against replayed records — whose fills are whose, what
/// exposure the log accounts for, which symbol an in-flight order is in —
/// names the OLD run's numbers. A symbol a book admitted at runtime last
/// run would otherwise come back at a different position (or not at all,
/// leaving its position invisible to reconcile and the stop discipline).
pub fn symbol_order(replayed: &[WalRecord], wanted: &[Subscription]) -> Vec<Symbol> {
    let mut names: Vec<Symbol> = crate::replay::LogNames::of_log(replayed).symbols;
    for sub in wanted {
        if !names.iter().any(|n| n == &sub.symbol) {
            names.push(sub.symbol.clone());
        }
    }
    names
}

/// What the market feed opens with: every strategy subscription, plus a
/// quote subscription for each symbol carried over from the previous run's
/// table — exactly what `admit_wanted` subscribed when it took the name on.
/// Without this a carried-over symbol has no price at boot, and a position
/// recovered in it could not even be exited until a book re-named it.
///
/// EMITTED IN `symbols` ORDER, and that order is load-bearing: the feed
/// interns its symbol table in subscription order, every other component
/// interns `symbols` (the log's table, then new seeds), and nothing
/// translates between them — a quote's `SymbolId` is used as a core id
/// directly. Emitting the seeds first misaligns the two tables the moment
/// a config seed names a symbol the log already carries as a runtime
/// admission: every symbol between the seed block and the old admission
/// shifts by one on the feed side only, prices land in the wrong slots, and
/// what a follower then reads as standing exposure is another symbol's.
pub fn boot_subscriptions(symbols: &[Symbol], wanted: &[Subscription]) -> Vec<Subscription> {
    let mut subs: Vec<Subscription> = Vec::new();
    for name in symbols {
        let mut named = false;
        for sub in wanted.iter().filter(|s| &s.symbol == name) {
            named = true;
            if !subs.contains(sub) {
                subs.push(sub.clone());
            }
        }
        if !named {
            subs.push(Subscription {
                symbol: name.clone(),
                feed: engine_types::Feed::Quote,
            });
        }
    }
    // `symbol_order` already appends every wanted name, so nothing should
    // remain; kept so a caller handing an unrelated list cannot silently
    // drop a subscription.
    for sub in wanted {
        if !subs.contains(sub) {
            subs.push(sub.clone());
        }
    }
    subs
}

/// Read the config's venue name — the switch, turned once.
///
/// Every one of the three constructors below takes the value this returns, so
/// the gateway, the private order stream and the public market feed are all
/// the same venue by construction. None of them is handed a name of its own to
/// get wrong.
///
/// An unknown name is refused rather than defaulted: a typo that quietly fell
/// back to some other venue would be a strategy trading somewhere nobody
/// chose.
pub fn venue_name(name: &str) -> Result<VenueName, VenueError> {
    VenueName::parse(name)
}

/// The chosen venue's public market stream, subscribed to exactly what was
/// asked.
pub fn market_feed(name: VenueName, wanted: &[Subscription]) -> MarketFeeds {
    MarketFeeds::build(name, wanted)
}

/// The chosen venue's private order stream, on the same account the gateway
/// trades — so the stream that reports fills and the gateway that causes them
/// cannot end up on different accounts.
pub fn order_feed(name: VenueName, symbols: Vec<Symbol>) -> Result<OrderFeeds, VenueError> {
    OrderFeeds::build(name, symbols)
}

/// The venue the config named (credentials from the environment). The name
/// picks one of the adapters compiled into the venue crate — it is not an
/// address.
pub fn venue(name: VenueName, symbols: Vec<Symbol>) -> Result<Venue, VenueError> {
    Venue::build(name, symbols)
}

/// A credential-wide read capability with no order or account mutation API.
/// Unlike a trading gateway, this may inspect a disarmed funded account so a
/// generation-changing rollout can prove old exposure absent.
pub fn inventory_probe(name: VenueName) -> Result<InventoryProbe, VenueError> {
    InventoryProbe::build(name)
}

/// The heartbeat writer, but only when the config names a path. No path means
/// no file is written and nothing at all is said about it — an engine nobody
/// asked to report on itself is not a fault, and a line every few seconds
/// saying so would be noise in every log the fleet keeps.
///
/// `account` and `lease_path` are what the run has already learned: whose
/// account these credentials open, and the lock file this process holds. A
/// a run that cannot reach the venue never
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

/// The closed-round-trip file, but only when the config names a path. No path
/// means the engine says nothing about what its positions made, and the trade
/// notifier has no closed-round-trip source.
pub fn trades(settings: &EngineSection) -> Option<Trades> {
    let path = settings.trades_path.as_ref()?;
    tracing::info!(path = %path.display(), "writing a closed-trade file");
    Some(Trades::new(path.clone()))
}

/// The `[risk]` block, exactly as engine.toml spells it. There are no
/// defaults for the capital controls: every number is written down.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RiskSection {
    max_account_view_age_s: u64,
    leverage: f64,
    #[serde(default = "default_qty_tolerance")]
    qty_tolerance: f64,
    max_rolling_loss_fraction: f64,
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
    /// The operational profile to read, e.g. `configs/operational.json`.
    operational_profile_path: PathBuf,
    max_account_view_age_s: u64,
    /// How far a position may move against it before its stop ends it. Not in
    /// the profile — it is `DISASTER_STOP_FRACTION` on the host — so it is
    /// named here rather than guessed.
    disaster_stop_fraction: f64,
}

fn default_qty_tolerance() -> f64 {
    1e-12
}

/// The capital controls, from one of two places.
///
/// Either the caps are written out in `[risk]`, or `[risk]` names the fleet's
/// operational profile and they come from that document. Naming a profile is
/// what the funded account does, so native config rendering and the account
/// kernel are held to one file rather than two copied limits.
pub fn risk(section: &toml::Table) -> Result<Kernel, Box<dyn Error>> {
    if section.contains_key("operational_profile_path") {
        return risk_from_profile(section);
    }
    let parsed: RiskSection = toml::Value::Table(section.clone())
        .try_into()
        .map_err(|e| format!("the [risk] block is wrong: {e}"))?;
    let cfg = KernelConfig {
        max_account_view_age_ns: parsed.max_account_view_age_s.saturating_mul(1_000_000_000),
        envelope: EnvelopeConfig {
            tracks_equity: parsed.envelope.tracks_equity,
            reference_usdt: parsed.envelope.reference_usdt,
            equity_fraction: parsed.envelope.equity_fraction,
            floor_usdt: parsed.envelope.floor_usdt,
            expand_dead_band_fraction: parsed.envelope.expand_dead_band_fraction,
            gross_notional_multiple: parsed.envelope.gross_notional_multiple,
            disaster_stop_fraction: parsed.envelope.disaster_stop_fraction,
            max_component_gross_notional_usdt: parsed.envelope.max_component_gross_notional_usdt,
            max_initial_margin_usdt: parsed.envelope.max_initial_margin_usdt,
        },
        leverage: parsed.leverage,
        qty_tolerance: parsed.qty_tolerance,
        max_rolling_loss_fraction: parsed.max_rolling_loss_fraction,
    };
    Ok(Kernel::new(cfg).map_err(|e| format!("the risk kernel refuses this config: {e}"))?)
}

/// The caps, read from the fleet's own operational profile.
fn risk_from_profile(section: &toml::Table) -> Result<Kernel, Box<dyn Error>> {
    let parsed: ProfileRiskSection = toml::Value::Table(section.clone())
        .try_into()
        .map_err(|e| format!("the [risk] block is wrong: {e}"))?;

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
            max_account_view_age_ns: parsed.max_account_view_age_s.saturating_mul(1_000_000_000),
        },
    )
    .map_err(|e| {
        format!(
            "{} is not a profile this engine can run: {e}",
            parsed.operational_profile_path.display()
        )
    })?;

    tracing::info!(
        profile = %parsed.operational_profile_path.display(),
        reference_usdt = cfg.envelope.reference_usdt,
        account_gross_cap_usdt = cfg.envelope.account_gross_cap_usdt(),
        "capital limits read from the fleet's operational profile"
    );
    Ok(Kernel::new(cfg).map_err(|e| format!("the risk kernel refuses this config: {e}"))?)
}

/// Name plus config block to a live strategy, ids in block order.
pub fn strategies(configured: &[StrategyConfig]) -> Result<Vec<Box<dyn Strategy>>, Box<dyn Error>> {
    one_name_per_sleeve(configured)?;
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
/// This is not the only line of defence, and not the load-bearing one.
/// Signal-driven reducers may admit symbols after boot, where this check
/// cannot see overlap. `StrategyCtx::foreign_position` answers that runtime
/// case. This check still refuses overlap already present in config.
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

/// A sleeve name belongs to one block.
///
/// The name is what the log's id table, the heartbeat and the fill-cost report
/// call this strategy. Two blocks answering to one name make every row in
/// those three ambiguous, and both of this fleet's sleeves run the same plug,
/// so the name is all there is to tell them apart.
fn one_name_per_sleeve(configured: &[StrategyConfig]) -> Result<(), Box<dyn Error>> {
    let mut seen: Vec<&str> = Vec::with_capacity(configured.len());
    for (index, cfg) in configured.iter().enumerate() {
        let name = cfg.sleeve_name();
        if let Some(first) = seen.iter().position(|taken| *taken == name) {
            return Err(format!(
                "two strategy blocks both claim the sleeve \"{name}\" (#{first} and \
                 #{index}); the log, the heartbeat and the cost report all name a sleeve \
                 by this string, so give one of them its own `sleeve = ...`"
            )
            .into());
        }
        seen.push(name);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn quoter(symbol: &str) -> StrategyConfig {
        let params: toml::Table = toml::from_str(&format!(
            r#"
            symbols = ["{symbol}"]
            half_spread_bps = 10.0
            requote_bps = 2.0
            qty = 0.1
            max_position = 0.3
            stop_loss_fraction = 0.35
        "#
        ))
        .expect("test config parses");
        StrategyConfig {
            name: "quoter".into(),
            sleeve: None,
            params,
        }
    }

    /// The shipped engine.toml `[risk]` block.
    const RISK: &str = r#"
max_account_view_age_s = 120
max_rolling_loss_fraction = 0.1
leverage = 2.0

[envelope]
tracks_equity = true
reference_usdt = 100.0
equity_fraction = 1.0
floor_usdt = 100.0
expand_dead_band_fraction = 0.05
gross_notional_multiple = 2.0
disaster_stop_fraction = 0.35
max_component_gross_notional_usdt = 200.0
max_initial_margin_usdt = 100.0
"#;

    fn risk_block(text: &str) -> toml::Table {
        toml::from_str(text).expect("the test block parses as toml")
    }

    #[test]
    fn the_shipped_risk_block_builds_a_kernel() {
        assert!(risk(&risk_block(RISK)).is_ok());
    }

    #[test]
    // A config written before these caps existed names no value for them.
    // Guessing one would be a capital control nobody chose, so it is refused.
    fn a_risk_block_missing_a_capital_cap_is_refused() {
        for key in [
            "max_component_gross_notional_usdt",
            "max_initial_margin_usdt",
        ] {
            let without: String = RISK
                .lines()
                .filter(|line| !line.starts_with(key))
                .collect::<Vec<_>>()
                .join("\n");
            let err = risk(&risk_block(&without))
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
        let mut first = quoter("BTCUSDT");
        first.sleeve = Some("first".into());
        let mut second = quoter("ETHUSDT");
        second.sleeve = Some("second".into());
        let built = strategies(&[first, second]).expect("two strategies, two symbols");
        assert_eq!(built.len(), 2);
    }

    #[test]
    fn two_strategies_on_one_symbol_are_refused() {
        let mut first = quoter("BTCUSDT");
        first.sleeve = Some("first".into());
        let mut second = quoter("BTCUSDT");
        second.sleeve = Some("second".into());
        let Err(err) = strategies(&[first, second]) else {
            panic!("the venue cannot say whose position BTCUSDT is; this must not boot");
        };
        let text = err.to_string();
        assert!(text.contains("BTCUSDT"), "{text}");
        assert!(text.contains("quoter"), "{text}");
    }

    #[test]
    fn the_same_strategy_twice_on_one_symbol_is_refused() {
        assert!(strategies(&[quoter("BTCUSDT"), quoter("BTCUSDT")]).is_err());
    }

    // ----------------------------------------------------------------------
    // Taking the caps from the fleet's operational profile instead
    // ----------------------------------------------------------------------

    /// The `[risk]` block a funded engine would ship: no caps of its own, a
    /// path to the same document native config rendering consumes.
    fn profile_risk(profile: &str) -> toml::Table {
        risk_block(&format!(
            r#"
operational_profile_path = "../../configs/{profile}"
max_account_view_age_s = 120
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

    /// A strategy block that names its own sleeve.
    fn sleeve_strategy(sleeve: &str, symbol: &str) -> StrategyConfig {
        let mut cfg = quoter(symbol);
        cfg.sleeve = Some(sleeve.to_string());
        cfg
    }

    #[test]
    fn the_shipped_mainnet_profile_builds_a_kernel() {
        risk(&profile_risk("operational.json"))
            .expect("the funded account's own profile must build a kernel");
    }

    #[test]
    fn caps_written_beside_a_named_profile_are_refused() {
        // An [envelope] block left in the file when the profile took over
        // would be a set of limits nobody is enforcing, sitting somewhere an
        // operator would read them.
        let mut block = profile_risk("operational.json");
        block.insert("leverage".into(), toml::Value::Float(2.0));
        let err = refusal(risk(&block), "a stray cap beside a profile was accepted");
        assert!(err.to_string().contains("leverage"), "{err}");
    }

    #[test]
    fn two_strategy_blocks_cannot_answer_to_one_name() {
        // The log's id table, the heartbeat and the cost report all name a
        // sleeve by this string, and both of this fleet's sleeves run the same
        // plug — so two blocks under one name make every row ambiguous.
        let Err(err) = strategies(&[
            sleeve_strategy("carry", "BTCUSDT"),
            sleeve_strategy("carry", "ETHUSDT"),
        ]) else {
            panic!("one name was handed to two strategy blocks");
        };
        assert!(err.to_string().contains("carry"), "{err}");
    }

    #[test]
    fn the_sleeve_name_falls_back_to_the_strategy_name() {
        let mut cfg = quoter("BTCUSDT");
        assert_eq!(cfg.sleeve_name(), "quoter");
        cfg.sleeve = Some("carry".into());
        assert_eq!(cfg.sleeve_name(), "carry");
    }

    #[test]
    fn a_missing_profile_file_says_which_file() {
        let err = refusal(
            risk(&profile_risk("operational.does-not-exist.json")),
            "a missing profile was accepted",
        );
        assert!(err.to_string().contains("does-not-exist"), "{err}");
    }

    #[test]
    fn a_seed_naming_a_symbol_the_log_already_carries_keeps_every_feed_id_aligned() {
        // 2026-08-20: the exodus sleeve made DOGEUSDT a config seed while the
        // log already carried it as a runtime admission. The feed interns in
        // subscription order, the core in the log's order, and a quote's id is
        // used as a core id with no translation — so the two tables must be
        // byte-identical or prices land in the wrong slots. This is the boot
        // that broke: the ids between the seed block and DOGE's old position
        // shifted by one on the feed side only.
        let quote = |name: &str| Subscription {
            symbol: name.to_string(),
            feed: engine_types::Feed::Quote,
        };
        let replayed = vec![engine_wal::WalRecord::Names {
            strategies: vec!["carry".into(), "long".into()],
            symbols: vec![
                "BTCUSDT".into(),
                "ETHUSDT".into(),
                "SOLUSDT".into(),
                "XRPUSDT".into(),
                "HOMEUSDT".into(),
                "ACEUSDT".into(),
                "DOGEUSDT".into(),
                "LINKUSDT".into(),
                "HYPEUSDT".into(),
            ],
        }];
        let wanted = vec![
            quote("BTCUSDT"),
            quote("ETHUSDT"),
            quote("SOLUSDT"),
            quote("XRPUSDT"),
            quote("DOGEUSDT"), // the new seed the log already knows
        ];
        let symbols = symbol_order(&replayed, &wanted);
        let feed = market_feed(VenueName::BybitDemo, &boot_subscriptions(&symbols, &wanted));
        for (position, name) in symbols.iter().enumerate() {
            assert_eq!(
                feed.id_of(name),
                Some(engine_types::SymbolId(position as u16)),
                "{name} must sit at the same position in the feed's table as in \
                 the core's, or its prices arrive under another symbol's id"
            );
        }
    }
}

/// The templates the fleet is actually deployed from.
///
/// A config file that ships in this repo and is copied to a host by hand is an
/// artifact like any other, and the only place its mistakes show up otherwise
/// is a unit that will not start. These read the real templates, point the
/// profile path at the repo's own copy, and assemble the whole thing.
#[cfg(test)]
mod deployed_templates {
    use super::*;
    use crate::config::Config;

    /// The template with its host paths made local: the profile it names lives
    /// under /opt on a VPS and under the repo root here.
    fn config_from(template: &str, profile: &str) -> Config {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .expect("the repo root is two above this crate");
        let profile_path = root
            .join("configs")
            .join(profile)
            .to_str()
            .expect("a utf-8 path")
            .to_string();
        let text = std::fs::read_to_string(root.join("deploy").join(template))
            .unwrap_or_else(|e| {
                panic!("{template} is a shipped artifact and must be readable: {e}")
            })
            .replace(
                &format!("/opt/liquidity-migration/configs/{profile}"),
                &profile_path,
            )
            .replace(
                "/etc/liquidity-migration/signal-worker-mainnet-source/operational-profile.json",
                &profile_path,
            )
            .replace(
                "/etc/liquidity-migration/signal-worker-demo-source/operational-profile.json",
                &profile_path,
            );
        toml::from_str::<Config>(&text).unwrap_or_else(|e| panic!("{template} must parse: {e}"))
    }

    fn assemble(template: &str, profile: &str) -> Config {
        let config = config_from(template, profile);
        strategies(&config.strategies)
            .unwrap_or_else(|e| panic!("{template} must build its strategies: {e}"));
        risk(&config.risk).unwrap_or_else(|e| panic!("{template} must build its risk kernel: {e}"));
        config
    }

    #[test]
    fn the_demo_template_assembles_whole() {
        let config = assemble("engine.demo.toml.template", "operational.json");
        assert_eq!(config.engine.venue, "bybit_demo");
        assert!(
            config.engine.heartbeat_path.is_some(),
            "the fleet observer contract requires the engine heartbeat path"
        );
    }

    #[test]
    fn the_mainnet_template_assembles_whole() {
        let config = assemble("engine.mainnet.toml.template", "operational.json");
        assert_eq!(config.engine.venue, "bybit_mainnet");
    }

    #[test]
    fn the_demo_template_runs_carry_then_long_then_exodus() {
        // A strategy's id is its position in the file, and the engine's record
        // of whose position is whose is keyed on it and rebuilt from the log.
        // Reordering these blocks hands one sleeve's fill history to the other.
        // Every new sleeve appends; nothing is ever inserted.
        let config = config_from("engine.demo.toml.template", "operational.json");
        let sleeves: Vec<&str> = config.strategies.iter().map(|s| s.sleeve_name()).collect();
        assert_eq!(sleeves, ["carry", "long", "exodus"]);
    }

    #[test]
    fn the_mainnet_template_appends_the_maker_canary_after_existing_sleeves() {
        let config = config_from("engine.mainnet.toml.template", "operational.json");
        let sleeves: Vec<&str> = config.strategies.iter().map(|s| s.sleeve_name()).collect();
        assert_eq!(sleeves, ["carry", "long", "exodus", "maker_canary"]);
    }

    #[test]
    fn demo_directional_sleeves_use_current_native_inputs() {
        let config = config_from("engine.demo.toml.template", "operational.json");
        let built = strategies(&config.strategies).unwrap();
        assert_eq!(
            built
                .iter()
                .map(|strategy| strategy.requires_signal_feed())
                .collect::<Vec<_>>(),
            [true, true, false],
            "CARRY and LONG consume external observations; Exodus consumes CARRY events"
        );
    }
}
