use super::*;

const SECTION: &str = r#"
[canary]
started_at = "2026-09-11T00:00:00Z"
expires_at = "2026-10-11T00:00:00Z"
accepted_unproven = ["fill-attribution", "protection-place", "protection-trigger", "reconnect-history-recovery"]
strategies = ["long_native"]
max_positions = 3
max_open_orders = 6
max_gross_notional_usdt = 105.0
max_position_notional_usdt = 15.0
max_loss_usdt = 3.0
"#;

/// A config on `venue`, carrying `section` and two strategy blocks named the
/// way the funded templates name theirs.
fn config(venue: &str, section: Option<&str>) -> Config {
    let text = format!(
        r#"
[engine]
wal_path = "engine.wal"
venue = "{venue}"
{}

[[strategy]]
name = "long_native"
sleeve = "long"
enabled = false

[[strategy]]
name = "carry_native"
sleeve = "carry"
enabled = false
"#,
        section.unwrap_or("")
    );
    toml::from_str(&text).expect("the fixture config parses")
}

fn edited(from: &str, to: &str) -> String {
    let text = SECTION.replace(from, to);
    assert_ne!(text, SECTION, "the fixture edit {from:?} matched nothing");
    text
}

#[cfg(feature = "mexc")]
const CANARY_REALM: VenueName = VenueName::MexcMainnet;

#[cfg(feature = "mexc")]
fn compiled(section: &str) -> Result<Option<CanaryPolicy>, String> {
    CanaryPolicy::compile(&config(CANARY_REALM.as_str(), Some(section)), CANARY_REALM)
}

#[cfg(feature = "mexc")]
#[test]
fn a_live_canary_realm_without_a_policy_refuses_and_names_itself() {
    let error = CanaryPolicy::compile(&config(CANARY_REALM.as_str(), None), CANARY_REALM)
        .expect_err("a funded canary runs under a written policy or it does not run");
    assert!(error.contains(CANARY_REALM.as_str()), "{error}");
    assert!(
        error.contains("live-canary") && error.contains("[canary]"),
        "{error}"
    );
}

#[cfg(feature = "bybit")]
#[test]
fn a_policy_on_a_realm_that_is_not_a_canary_refuses() {
    let venue = VenueName::BybitDemo;
    let section = SECTION.replace(
        r#"accepted_unproven = ["fill-attribution", "protection-place", "protection-trigger", "reconnect-history-recovery"]"#,
        "accepted_unproven = []",
    );
    let error = CanaryPolicy::compile(&config(venue.as_str(), Some(&section)), venue)
        .expect_err("a canary policy on a proven realm is a config nobody meant to write");
    assert!(error.contains("live-proven"), "{error}");
    assert!(CanaryPolicy::compile(&config(venue.as_str(), None), venue)
        .expect("a realm that is not a canary needs no policy")
        .is_none());
}

#[cfg(feature = "mexc")]
#[test]
fn the_accepted_set_must_be_exactly_what_the_realm_owes() {
    let owed: Vec<_> = CANARY_REALM
        .unproven_capabilities()
        .into_iter()
        .map(|capability| capability.as_str())
        .collect();
    assert!(compiled(SECTION).is_ok(), "today's row is the accepted set");

    // One name short: a receipt arrived and the policy was not re-read.
    let short = edited(r#", "reconnect-history-recovery"]"#, "]");
    let error = compiled(&short).expect_err("a policy that accepts less than is owed is stale");
    assert!(error.contains("reconnect-history-recovery"), "{error}");
    for name in &owed {
        assert!(error.contains(name), "{error} does not list {name}");
    }

    // One name too many: the realm was promoted and the policy was not.
    let long = edited(
        r#"accepted_unproven = ["fill-attribution""#,
        r#"accepted_unproven = ["cancel", "fill-attribution""#,
    );
    let error = compiled(&long).expect_err("a policy that accepts more than is owed is stale");
    assert!(error.contains("cancel"), "{error}");
}

#[cfg(feature = "mexc")]
#[test]
fn an_unreadable_window_refuses() {
    for (from, to) in [
        (
            r#"expires_at = "2026-10-11T00:00:00Z""#,
            r#"expires_at = "2026-10-11""#,
        ),
        (
            r#"expires_at = "2026-10-11T00:00:00Z""#,
            r#"expires_at = "2026-02-30T00:00:00Z""#,
        ),
        (
            r#"expires_at = "2026-10-11T00:00:00Z""#,
            r#"expires_at = "2026-10-11T00:00:00+01:00""#,
        ),
        (
            r#"expires_at = "2026-10-11T00:00:00Z""#,
            r#"expires_at = "2026-09-10T00:00:00Z""#,
        ),
        (
            r#"started_at = "2026-09-11T00:00:00Z""#,
            r#"started_at = "not a time""#,
        ),
    ] {
        let error = compiled(&edited(from, to)).expect_err("{to} is not a window boundary");
        assert!(
            error.contains("canary.started_at")
                || error.contains("canary.expires_at")
                || error.contains("after canary.started_at"),
            "{error}"
        );
    }
}

#[cfg(feature = "mexc")]
#[test]
fn a_ceiling_that_admits_nothing_or_everything_refuses() {
    for (from, to) in [
        (
            "max_gross_notional_usdt = 105.0",
            "max_gross_notional_usdt = 0.0",
        ),
        (
            "max_position_notional_usdt = 15.0",
            "max_position_notional_usdt = -1.0",
        ),
        ("max_loss_usdt = 3.0", "max_loss_usdt = nan"),
        ("max_positions = 3", "max_positions = 0"),
        ("max_open_orders = 6", "max_open_orders = 0"),
    ] {
        let error = compiled(&edited(from, to)).expect_err("a ceiling has to bound something");
        assert!(error.contains("canary."), "{error}");
    }
}

#[cfg(feature = "mexc")]
#[test]
fn the_allowed_strategies_must_be_configured_blocks() {
    let error = compiled(&edited(
        r#"strategies = ["long_native"]"#,
        r#"strategies = ["long"]"#,
    ))
    .expect_err("the section names blocks, and a sleeve label is not one");
    assert!(
        error.contains("long_native") && error.contains("carry_native"),
        "{error}"
    );

    let error = compiled(&edited(
        r#"strategies = ["long_native"]"#,
        "strategies = []",
    ))
    .expect_err("a policy that allows no sleeve would be a stopped engine, not a canary");
    assert!(error.contains("canary.strategies"), "{error}");
}

#[cfg(feature = "mexc")]
#[test]
fn an_empty_symbol_list_refuses_and_an_absent_one_admits_every_instrument() {
    let error = compiled(&edited(
        "max_positions = 3",
        "symbols = []\nmax_positions = 3",
    ))
    .expect_err("an empty list reads as a policy that allows nothing");
    assert!(error.contains("canary.symbols"), "{error}");
    let policy = compiled(SECTION).unwrap().unwrap();
    assert!(policy.symbols.is_none());
    let listed = compiled(&edited(
        "max_positions = 3",
        "symbols = [\"BTCUSDT\"]\nmax_positions = 3",
    ))
    .unwrap()
    .unwrap();
    assert_eq!(
        listed.symbols,
        Some(BTreeSet::from(["BTCUSDT".to_string()]))
    );
}

/// The templates a deploy installs on the two funded canaries.
#[test]
fn every_deployed_live_canary_template_carries_a_policy_its_realm_accepts() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|path| path.parent())
        .expect("the repo root is two above this crate");
    for template in [
        "engine.demo.toml.template",
        "engine.mainnet.toml.template",
        "engine.mexc.toml.template",
        "engine.hyperliquid.toml.template",
    ] {
        let text = std::fs::read_to_string(root.join("deploy").join(template))
            .unwrap_or_else(|e| panic!("{template} is a shipped artifact: {e}"));
        let config: Config =
            toml::from_str(&text).unwrap_or_else(|e| panic!("{template} must parse: {e}"));
        let Ok(venue) = engine_venue::VenueName::parse(&config.engine.venue) else {
            // Outside this build's feature set; the per-feature CI matrix
            // covers it where it is compiled.
            continue;
        };
        CanaryPolicy::compile(&config, venue)
            .unwrap_or_else(|e| panic!("{template} is deployed and must boot: {e}"));
    }
}

#[test]
fn rfc3339_utc_is_read_exactly_and_nothing_else_is_read_at_all() {
    assert_eq!(unix_ms("1970-01-01T00:00:00Z"), Ok(0));
    assert_eq!(unix_ms("2026-09-11T00:00:00Z"), Ok(1_789_084_800_000));
    assert_eq!(unix_ms("2026-10-11T00:00:00Z"), Ok(1_791_676_800_000));
    assert_eq!(unix_ms("2024-02-29T12:34:56Z"), Ok(1_709_210_096_000));
    for bad in [
        "2026-02-30T00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-09-11T24:00:00Z",
        "2026-09-11T00:60:00Z",
        "2026-09-11 00:00:00Z",
        "2026-09-11T00:00:00+00:00",
        "2026-09-11T00:00:00.000Z",
        "",
    ] {
        assert!(unix_ms(bad).is_err(), "{bad} was read as a time");
    }
}

fn policy() -> CanaryPolicy {
    CanaryPolicy {
        started_ms: unix_ms("2026-09-11T00:00:00Z").unwrap(),
        expires_ms: unix_ms("2026-10-11T00:00:00Z").unwrap(),
        sleeves: BTreeSet::from(["long".to_string()]),
        symbols: None,
        max_positions: 3,
        max_open_orders: 6,
        max_gross_notional_usdt: 105.0,
        max_position_notional_usdt: 15.0,
        max_loss_usdt: 3.0,
        realised_loss_usdt: Exact::zero(),
        unvalued_trips: 0,
    }
}

const INSIDE_MS: i64 = 1_789_171_200_000; // 2026-09-12T00:00:00Z
const AFTER_MS: i64 = 1_791_763_200_000; // 2026-10-12T00:00:00Z

fn opening<'a>(sleeve: &'a str, symbol: &'a str, notional_usdt: f64) -> CanaryIntent<'a> {
    CanaryIntent {
        sleeve,
        symbol,
        notional_usdt,
    }
}

fn book(positions: &[(&str, f64)]) -> CanaryBook {
    CanaryBook {
        positions: positions
            .iter()
            .map(|(name, notional)| ((*name).to_string(), *notional))
            .collect(),
        open_orders: 0,
        loss_usdt: 0.0,
    }
}

#[test]
fn an_opening_inside_every_ceiling_is_admitted() {
    assert_eq!(
        policy().refusal(&opening("long", "BTCUSDT", 10.0), &book(&[]), INSIDE_MS),
        None
    );
}

#[test]
fn a_window_that_has_ended_refuses_every_opening() {
    assert_eq!(
        policy().refusal(&opening("long", "BTCUSDT", 1.0), &book(&[]), AFTER_MS),
        Some(OpeningRefusal::CanaryExpired)
    );
}

#[test]
fn a_sleeve_the_policy_does_not_list_is_refused() {
    assert_eq!(
        policy().refusal(&opening("carry", "BTCUSDT", 1.0), &book(&[]), INSIDE_MS),
        Some(OpeningRefusal::CanaryStrategyNotAllowed)
    );
}

#[test]
fn a_symbol_list_admits_what_it_names_and_nothing_else() {
    let mut listed = policy();
    listed.symbols = Some(BTreeSet::from(["BTCUSDT".to_string()]));
    assert_eq!(
        listed.refusal(&opening("long", "BTCUSDT", 1.0), &book(&[]), INSIDE_MS),
        None
    );
    assert_eq!(
        listed.refusal(&opening("long", "ETHUSDT", 1.0), &book(&[]), INSIDE_MS),
        Some(OpeningRefusal::CanarySymbolNotAllowed)
    );
    assert_eq!(
        policy().refusal(&opening("long", "ETHUSDT", 1.0), &book(&[]), INSIDE_MS),
        None,
        "no list means every admitted instrument"
    );
}

#[test]
fn the_position_count_binds_a_new_symbol_and_not_one_already_held() {
    let held = book(&[("AUSDT", 5.0), ("BUSDT", 5.0), ("CUSDT", 5.0)]);
    assert_eq!(
        policy().refusal(&opening("long", "DUSDT", 1.0), &held, INSIDE_MS),
        Some(OpeningRefusal::CanaryPositionsAtCap)
    );
    assert_eq!(
        policy().refusal(&opening("long", "BUSDT", 1.0), &held, INSIDE_MS),
        None,
        "adding to a symbol the policy already counted opens no new position"
    );
}

#[test]
fn the_opening_order_count_binds() {
    let mut working = book(&[]);
    working.open_orders = 6;
    assert_eq!(
        policy().refusal(&opening("long", "BTCUSDT", 1.0), &working, INSIDE_MS),
        Some(OpeningRefusal::CanaryOpenOrdersAtCap)
    );
    working.open_orders = 5;
    assert_eq!(
        policy().refusal(&opening("long", "BTCUSDT", 1.0), &working, INSIDE_MS),
        None
    );
}

#[test]
fn the_account_gross_ceiling_counts_the_intent_it_is_judging() {
    // Only the account ceiling is in play here; the per-symbol one has its own
    // test below.
    let mut only_gross = policy();
    only_gross.max_position_notional_usdt = 105.0;
    let held = book(&[("AUSDT", 100.0)]);
    assert_eq!(
        only_gross.refusal(&opening("long", "AUSDT", 5.0), &held, INSIDE_MS),
        None,
        "exactly at the ceiling is admitted"
    );
    assert_eq!(
        only_gross.refusal(&opening("long", "AUSDT", 5.01), &held, INSIDE_MS),
        Some(OpeningRefusal::CanaryGrossNotionalAtCap)
    );
}

#[test]
fn the_per_symbol_ceiling_counts_what_that_symbol_already_holds() {
    let held = book(&[("AUSDT", 14.0)]);
    assert_eq!(
        policy().refusal(&opening("long", "AUSDT", 1.0), &held, INSIDE_MS),
        None
    );
    assert_eq!(
        policy().refusal(&opening("long", "AUSDT", 1.5), &held, INSIDE_MS),
        Some(OpeningRefusal::CanaryPositionNotionalAtCap)
    );
    assert_eq!(
        policy().refusal(&opening("long", "BUSDT", 15.0), &held, INSIDE_MS),
        None,
        "another symbol carries its own ceiling"
    );
}

#[test]
fn the_loss_ceiling_refuses_every_opening_once_it_is_reached() {
    let mut losing = book(&[]);
    losing.loss_usdt = 2.99;
    assert_eq!(
        policy().refusal(&opening("long", "BTCUSDT", 1.0), &losing, INSIDE_MS),
        None
    );
    losing.loss_usdt = 3.0;
    assert_eq!(
        policy().refusal(&opening("long", "BTCUSDT", 1.0), &losing, INSIDE_MS),
        Some(OpeningRefusal::CanaryLossCeiling)
    );
}

#[test]
fn the_status_names_what_blocks_every_opening_whatever_it_asks_for() {
    let policy = policy();
    let mut held = book(&[("AUSDT", 30.0)]);
    let status = policy.status(&held, INSIDE_MS);
    assert_eq!(status.blocked, None);
    assert_eq!(status.gross_notional_usdt, 30.0);
    assert_eq!(status.positions, 1);
    assert_eq!(status.expires_in_s, 2_505_600, "29 days from 2026-09-12");

    held.loss_usdt = 3.0;
    assert_eq!(
        policy.status(&held, INSIDE_MS).blocked,
        Some("canary_loss_ceiling")
    );
    assert_eq!(
        policy.status(&held, AFTER_MS).blocked,
        Some("canary_expired")
    );
}

mod through_the_engine {
    use super::*;
    use engine_types::{MarketEvent, PositionView, Quote, StrategyId, SymbolId, WalRecord};

    type TestEngine =
        Engine<crate::tests::MockWal, crate::tests::MockRisk, crate::tests::MockVenue>;
    type Records = std::sync::Arc<std::sync::Mutex<Vec<WalRecord>>>;

    async fn fixture(policy: CanaryPolicy) -> (TestEngine, Records) {
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let strategy = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let (mut engine, records) = crate::tests::callback_test_fixture(vec![strategy]).await;
        engine.books.market.apply(&MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: Quote {
                bid_px: 99.0,
                ask_px: 101.0,
                bid_qty: 10.0,
                ask_qty: 10.0,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        });
        let mut policy = policy;
        policy.sleeves = BTreeSet::from([engine.host.names[0].clone()]);
        engine.enforce_canary(policy);
        (engine, records)
    }

    fn intent(reduce_only: bool, qty: f64) -> Intent {
        Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty,
            kind: OrderKind::Market,
            stop: None,
            reduce_only,
            tag: "canary".into(),
            decided_ns: clock::now_ns(),
            work: None,
            leverage: None,
        }
    }

    fn fill(side: Side, px: f64, venue_ts_ms: i64) -> crate::execution::Fill {
        crate::execution::Fill {
            amounts: None,
            qty: 1.0,
            px,
            fee: Some(0.0),
            side,
            venue_ts_ms,
            is_maker: false,
            client_order_id: format!("eng-{venue_ts_ms}"),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            arrival_mid: 100.0,
        }
    }

    /// Ten bought at 100 and sold at 90, less the two charges of 0.10.
    const LOST_USDT: f64 = 100.2;

    fn sent(id: &str, side: Side) -> WalRecord {
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: id.to_string(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side,
                qty: 10.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: side == Side::Sell,
                exact_terms: None,
                sleeve_effect: None,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 100.0,
        }
    }

    fn filled(id: &str, side: Side, px: f64, venue_ts_ms: i64) -> WalRecord {
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: format!("exec-{id}"),
                client_order_id: id.to_string(),
                symbol: SymbolId(0),
                side,
                qty: 10.0,
                px,
                fee: Some(0.10),
                is_maker: false,
                forced_close: None,
                venue_ts_ms,
                recv_ns: 2,
            },
        }
    }

    const DAY_MS: i64 = 86_400_000;

    /// Three days before this boot, and two.
    fn older_close_ms() -> i64 {
        clock::wall_ms() - 3 * DAY_MS
    }

    fn newer_close_ms() -> i64 {
        clock::wall_ms() - 2 * DAY_MS
    }

    /// A previous run whose two round trips each lost [`LOST_USDT`].
    fn a_run_that_lost_twice() -> Vec<WalRecord> {
        let trip = |n: u32, closed_ms: i64| {
            vec![
                sent(&format!("eng-{n}-in"), Side::Buy),
                filled(&format!("eng-{n}-in"), Side::Buy, 100.0, closed_ms - 1),
                sent(&format!("eng-{n}-out"), Side::Sell),
                filled(&format!("eng-{n}-out"), Side::Sell, 90.0, closed_ms),
            ]
        };
        let mut log = trip(1, older_close_ms());
        log.extend(trip(2, newer_close_ms()));
        log
    }

    /// Boot on that log, then attach a policy whose window began at
    /// `started_ms` — the order `runner::run` does it in.
    async fn booted(started_ms: i64) -> TestEngine {
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let strategy = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let mut engine =
            crate::tests::replayed_test_fixture(vec![strategy], &a_run_that_lost_twice()).await;
        // The kernel's window is a day wide and both trips are older than
        // that. The mock keeps every row it is handed, so age them out by
        // hand.
        engine.risk.restore_rolling_loss_rows(&[]);
        let mut policy = super::policy();
        policy.started_ms = started_ms;
        policy.expires_ms = clock::wall_ms() + DAY_MS;
        policy.sleeves = BTreeSet::from([engine.host.names[0].clone()]);
        engine.enforce_canary(policy);
        engine
    }

    fn held(qty: f64, entry_px: f64) -> PositionView {
        PositionView {
            exact_amounts: None,
            exact_stop_px: None,
            symbol: SymbolId(0),
            side: Side::Buy,
            qty,
            entry_px,
            stop_attached: false,
            stop_px: 0.0,
            leverage: None,
        }
    }

    fn closed(closed_ms: i64, net_usdt: f64) -> engine_types::risk::ClosedTradeRow {
        engine_types::risk::ClosedTradeRow {
            unpriced: None,
            net_usdt_exact: None,
            closed_ms,
            net_usdt,
        }
    }

    fn observe(engine: &mut TestEngine, row: engine_types::risk::ClosedTradeRow) {
        engine
            .canary
            .as_mut()
            .expect("this run is under a policy")
            .observe_closed_trip(&row);
    }

    /// What the policy has accumulated, positive when the window is down.
    fn realised_usdt(engine: &TestEngine) -> f64 {
        engine
            .canary
            .as_ref()
            .expect("this run is under a policy")
            .realised_loss_usdt
            .reporting_f64()
    }

    fn canary_refusals(records: &Records) -> Vec<String> {
        records
            .lock()
            .unwrap()
            .iter()
            .filter_map(|record| match record {
                WalRecord::IntentRefused { code, .. } if code.starts_with("canary_") => {
                    Some(code.clone())
                }
                _ => None,
            })
            .collect()
    }

    /// The mark the engine holds values the intent and the book, so a policy in
    /// absolute money is judged against the same price the risk kernel sees.
    #[tokio::test(start_paused = true)]
    async fn the_intent_and_the_book_are_valued_at_the_marks_the_engine_holds() {
        let (mut engine, _) = fixture(super::policy()).await;
        engine.books.account.positions = vec![held(1.0, 90.0)];
        // 100 held + 15 asked is past the 105 account ceiling.
        assert_eq!(
            engine.canary_refusal(&intent(false, 0.15)),
            Some(OpeningRefusal::CanaryGrossNotionalAtCap)
        );
        // 100 held alone is already past the 15 per-symbol ceiling.
        assert_eq!(
            engine.canary_refusal(&intent(false, 0.01)),
            Some(OpeningRefusal::CanaryPositionNotionalAtCap)
        );
        engine.books.account.positions.clear();
        assert_eq!(engine.canary_refusal(&intent(false, 0.01)), None);
    }

    /// The point of an absolute ceiling: every `[risk]` cap is a ratio of
    /// verified equity, and a deposit enlarges all of them at once.
    #[tokio::test(start_paused = true)]
    async fn a_deposit_does_not_enlarge_the_canary_ceilings() {
        let (mut engine, _) = fixture(super::policy()).await;
        engine.books.account.equity_usdt = 30.0;
        engine.books.account.available_usdt = 30.0;
        engine.books.account.positions = vec![held(1.0, 100.0)];
        let refused = engine.canary_refusal(&intent(false, 0.1));
        assert_eq!(refused, Some(OpeningRefusal::CanaryGrossNotionalAtCap));
        engine.books.account.equity_usdt = 3_000.0;
        engine.books.account.available_usdt = 3_000.0;
        assert_eq!(
            engine.canary_refusal(&intent(false, 0.1)),
            refused,
            "the ceiling is money, not a ratio of equity"
        );
    }

    /// Closed round trips inside the policy's own window, and only those.
    /// The risk kernel's day-wide window is not consulted at all.
    #[tokio::test(start_paused = true)]
    async fn the_loss_ceiling_counts_closed_trips_since_the_policy_started() {
        let policy = super::policy();
        let started_ms = policy.started_ms;
        let (mut engine, _) = fixture(policy).await;
        observe(&mut engine, closed(started_ms - 1, -50.0));
        assert_eq!(
            engine.canary_refusal(&intent(false, 0.01)),
            None,
            "a trip closed before the window began is not this experiment's loss"
        );
        observe(&mut engine, closed(started_ms + 1, -3.0));
        assert_eq!(
            engine.canary_refusal(&intent(false, 0.01)),
            Some(OpeningRefusal::CanaryLossCeiling)
        );
        assert!(
            engine.risk.rolling_loss_rows().is_empty(),
            "the ceiling is the policy's own accumulation, not the kernel's window"
        );
    }

    /// Unrealised loss counts; unrealised profit does not pay for a loss.
    #[tokio::test(start_paused = true)]
    async fn open_loss_counts_toward_the_ceiling_and_open_profit_does_not_offset_it() {
        let policy = super::policy();
        let started_ms = policy.started_ms;
        let (mut engine, _) = fixture(policy).await;
        // Mid is 100; a long entered at 100.4 is 0.04 down on 0.1 units.
        engine.books.account.positions = vec![held(0.1, 130.4)];
        assert_eq!(
            engine.canary_refusal(&intent(false, 0.01)),
            Some(OpeningRefusal::CanaryLossCeiling),
            "3.04 unrealised down is past the 3.0 ceiling"
        );
        engine.books.account.positions = vec![held(0.1, 40.0)];
        observe(&mut engine, closed(started_ms + 1, -3.0));
        assert_eq!(
            engine.canary_refusal(&intent(false, 0.01)),
            Some(OpeningRefusal::CanaryLossCeiling),
            "six USDT of open profit does not pay back a closed loss"
        );
    }

    /// Boot with exposure and an expired policy: the exit flows, the opening
    /// does not, and the refusal is the one the wire word names.
    #[tokio::test(start_paused = true)]
    async fn an_expired_policy_refuses_openings_and_lets_exposure_out() {
        let mut policy = super::policy();
        policy.expires_ms = policy.started_ms + 1;
        let (mut engine, records) = fixture(policy).await;
        engine.books.account.positions = vec![held(0.1, 100.0)];

        let mut protection = std::collections::HashMap::new();
        assert!(engine
            .prepare_intent(
                intent(false, 0.01),
                Some("canary-opening".into()),
                clock::now_ns(),
                None,
                None,
                &mut protection,
            )
            .await
            .expect("a refusal is not an error")
            .is_none());
        assert_eq!(
            canary_refusals(&records),
            vec!["canary_expired".to_string()]
        );

        let _ = engine
            .prepare_intent(
                intent(true, 0.01),
                Some("canary-exit".into()),
                clock::now_ns(),
                None,
                None,
                &mut protection,
            )
            .await;
        assert_eq!(
            canary_refusals(&records),
            vec!["canary_expired".to_string()],
            "taking risk off never meets this policy"
        );
    }

    /// A trip the log could not value is money the ceiling is not counting,
    /// and the status says how many such trips there are.
    #[tokio::test(start_paused = true)]
    async fn a_trip_the_log_could_not_value_is_counted_and_lowers_no_loss() {
        let policy = super::policy();
        let started_ms = policy.started_ms;
        let (mut engine, _) = fixture(policy).await;
        observe(&mut engine, closed(started_ms + 1, -2.0));
        observe(
            &mut engine,
            engine_types::risk::ClosedTradeRow {
                unpriced: Some(engine_types::risk::UnpricedTradeReason::FeeValue),
                net_usdt_exact: None,
                closed_ms: started_ms + 2,
                net_usdt: 0.0,
            },
        );
        let status = engine.canary_status().expect("this run is under a policy");
        assert_eq!(status.unvalued_trips, 1);
        assert_eq!(status.loss_usdt, 2.0, "the priced trip, and only it");
        assert_eq!(realised_usdt(&engine), 2.0);
    }

    /// The fill ledger's own close, through the engine's trade recording.
    #[tokio::test(start_paused = true)]
    async fn a_trip_that_closes_while_the_run_is_up_is_counted_once() {
        let policy = super::policy();
        let started_ms = policy.started_ms;
        let (mut engine, _) = fixture(policy).await;
        engine
            .fills
            .on_fill(&fill(Side::Buy, 100.0, started_ms + 1), clock::now_ns());
        engine
            .fills
            .on_fill(&fill(Side::Sell, 90.0, started_ms + 2), clock::now_ns());
        engine.record_trades();
        assert_eq!(realised_usdt(&engine), 10.0, "one down ten, once");
        engine.record_trades();
        assert_eq!(
            realised_usdt(&engine),
            10.0,
            "the next tick has no trip left to take"
        );
        assert_eq!(
            engine.canary_refusal(&intent(false, 0.01)),
            Some(OpeningRefusal::CanaryLossCeiling)
        );
    }

    /// The kernel's rolling window is a day wide and the policy's window is
    /// the whole experiment: boot seeds the ceiling from the replay itself.
    #[tokio::test(start_paused = true)]
    async fn boot_seeds_the_ceiling_with_trips_the_kernels_window_has_dropped() {
        let engine = booted(older_close_ms() - DAY_MS).await;
        assert!(
            engine.risk.rolling_loss_rows().is_empty(),
            "the kernel's window has nothing left to answer with"
        );
        assert!(
            (realised_usdt(&engine) - 2.0 * LOST_USDT).abs() < 1e-9,
            "both replayed trips: {}",
            realised_usdt(&engine)
        );
        assert_eq!(
            engine.canary_refusal(&intent(false, 0.01)),
            Some(OpeningRefusal::CanaryLossCeiling)
        );
    }

    /// The window's own boundary, seeded: at or after `started_at` counts,
    /// and what closed before it belongs to whatever ran before.
    #[tokio::test(start_paused = true)]
    async fn boot_leaves_out_a_trip_that_closed_before_the_window_began() {
        let engine = booted(newer_close_ms()).await;
        assert!(
            (realised_usdt(&engine) - LOST_USDT).abs() < 1e-9,
            "the trip inside the window, and not the one before it: {}",
            realised_usdt(&engine)
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_run_with_no_policy_is_judged_by_nothing_here() {
        let params = toml::from_str("symbol = 'BTCUSDT'\nevery_s = 60\nenabled = false").unwrap();
        let strategy = engine_strategies::build_strategy("probe", StrategyId(0), &params).unwrap();
        let (engine, _) = crate::tests::callback_test_fixture(vec![strategy]).await;
        assert_eq!(engine.canary_refusal(&intent(false, 1_000_000.0)), None);
        assert!(engine.canary_status().is_none());
    }
}
