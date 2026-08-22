//! Where the real parts get plugged in.
//!
//! The loop is generic over the traits in `engine-types`; this file names
//! the concrete crates exactly once. Nothing above it knows which venue,
//! which log format, or which kernel it is running.

use std::error::Error;
use std::path::{Path, PathBuf};

use engine_marketdata::MarketFeeds;
use engine_risk::{
    EnvelopeConfig, Kernel, KernelConfig,
};
use engine_strategies::build_strategy;
use engine_types::{
    AccountIdentity, Strategy, StrategyId, Subscription, Symbol, VenueError, WalError, WalRecord,
};
use engine_venue::{OrderFeeds, Venue, VenueName};
use engine_wal::WalWriter;
use serde::Deserialize;

use crate::config::{EngineSection, StrategyConfig};
use crate::heartbeat::Heartbeat;
use crate::targets::{TargetBookWatcher, TargetBooks};

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
            subs.push(Subscription { symbol: name.clone(), feed: engine_types::Feed::Quote });
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

/// The `[risk]` block, exactly as engine.toml spells it. There are no
/// defaults for the capital controls: every number is written down, and the
/// Python fleet's values are recorded in engine-risk/PORT_NOTES.md.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RiskSection {
    max_account_view_age_s: u64,
    leverage: f64,
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
/// what the funded account does, so the engine and the Python fleet are held
/// to one file rather than to two copies of it.
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
        StrategyConfig { name: "touch_sniper".into(), sleeve: None, book_path: None, params }
    }

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
        StrategyConfig { name: "quoter".into(), sleeve: None, book_path: None, params }
    }

    /// The shipped engine.toml `[risk]` block.
    const RISK: &str = r#"
max_account_view_age_s = 120
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
        let mut cfg = sniper(symbol);
        cfg.sleeve = Some(sleeve.to_string());
        cfg
    }

    #[test]
    fn the_shipped_mainnet_profile_builds_a_kernel() {
        risk(&profile_risk("operational.mainnet.json"))
            .expect("the funded account's own profile must build a kernel");
    }

    #[test]
    fn caps_written_beside_a_named_profile_are_refused() {
        // An [envelope] block left in the file when the profile took over
        // would be a set of limits nobody is enforcing, sitting somewhere an
        // operator would read them.
        let mut block = profile_risk("operational.mainnet.json");
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
            wal_rotate_mb: 256,
            account_view_max_age_ms: 5_000,
            max_quote_age_ms: 30_000,
            leverage_authority: crate::config::LeverageAuthority::default(),
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
        assert_eq!(cfg.sleeve_name(), "touch_sniper");
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
        let text = std::fs::read_to_string(root.join("deploy").join(template))
            .unwrap_or_else(|e| panic!("{template} is a shipped artifact and must be readable: {e}"))
            .replace(
                &format!("/opt/liquidity-migration/configs/{profile}"),
                root.join("configs").join(profile).to_str().expect("a utf-8 path"),
            );
        toml::from_str::<Config>(&text)
            .unwrap_or_else(|e| panic!("{template} must parse: {e}"))
    }

    fn assemble(template: &str, profile: &str) -> Config {
        let config = config_from(template, profile);
        let built = strategies(&config.strategies)
            .unwrap_or_else(|e| panic!("{template} must build its strategies: {e}"));
        risk(&config.risk)
            .unwrap_or_else(|e| panic!("{template} must build its risk kernel: {e}"));
        target_books(&config.engine, &config.strategies, &built)
            .unwrap_or_else(|e| panic!("{template} must wire its books: {e}"));
        config
    }

    // `target_books` starts a watcher task, so these need a runtime under
    // them the way the engine has one.
    #[tokio::test]
    async fn the_demo_template_assembles_whole() {
        let config = assemble("engine.demo.toml.template", "operational.demo.json");
        assert_eq!(config.engine.venue, "bybit_demo");
        assert!(
            config.engine.heartbeat_path.is_some(),
            "both producers size from the equity in the heartbeat; without a path \
             they block every entry, quietly"
        );
    }

    #[tokio::test]
    async fn the_mainnet_template_assembles_whole() {
        let config = assemble("engine.mainnet.toml.template", "operational.mainnet.json");
        assert_eq!(config.engine.venue, "bybit_mainnet");
    }

    #[test]
    fn the_demo_template_runs_carry_then_long_then_exodus() {
        // A strategy's id is its position in the file, and the engine's record
        // of whose position is whose is keyed on it and rebuilt from the log.
        // Reordering these blocks hands one sleeve's fill history to the other.
        // Every new sleeve appends; nothing is ever inserted.
        let config = config_from("engine.demo.toml.template", "operational.demo.json");
        let sleeves: Vec<&str> = config.strategies.iter().map(|s| s.sleeve_name()).collect();
        assert_eq!(sleeves, ["carry", "long", "exodus"]);
    }

    #[test]
    fn each_sleeve_in_the_demo_template_reads_its_own_book() {
        // Books are routed, not broadcast. Two sleeves sharing one path would
        // each act on the other's decisions.
        let config = config_from("engine.demo.toml.template", "operational.demo.json");
        let paths: Vec<&std::path::Path> = config
            .strategies
            .iter()
            .map(|s| s.book_path.as_deref().expect("every sleeve names a book"))
            .collect();
        assert_eq!(paths.len(), 3);
        for (i, a) in paths.iter().enumerate() {
            for b in paths.iter().skip(i + 1) {
                assert_ne!(a, b, "one file cannot be two sleeves' decisions");
            }
        }
    }
}
