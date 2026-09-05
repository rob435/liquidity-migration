//! A close the venue itself started, arriving live on the private stream.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

use engine_types::ForcedClose;

/// What a previous run left: the sleeve bought ten at 100, and the venue is
/// still holding them.
fn bought_ten() -> Vec<WalRecord> {
    vec![
        WalRecord::Names {
            strategies: vec!["buyer".to_string(), "probe".to_string()],
            symbols: vec!["BTCUSDT".to_string()],
        },
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: "eng-old-1".to_string(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 10.0,
                kind: OrderKind::Market,
                stop: Some(StopSpec { trigger_px: 90.0 }),
                reduce_only: false,
                exact_terms: None,
                sleeve_effect: None,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 100.0,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: "old-exec".to_string(),
                client_order_id: "eng-old-1".to_string(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 10.0,
                px: 100.0,
                fee: Some(0.10),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: recent_replay_ms(),
                recv_ns: 2,
            },
        },
    ]
}

fn still_held() -> Vec<engine_types::PositionView> {
    vec![engine_types::PositionView {
        symbol: SymbolId(0),
        side: Side::Buy,
        qty: 10.0,
        entry_px: 100.0,
        stop_attached: true,
        stop_px: 90.0,
        leverage: None,
    }]
}

/// The stop firing under that position: the venue names no order of ours, and
/// says why it traded.
fn stop_fired(symbol: SymbolId, side: Side, forced_close: Option<ForcedClose>) -> OrderUpdate {
    OrderUpdate::Fill {
        allocation: None,
        amounts: None,
        exec_id: "venue-stop".to_string(),
        client_order_id: String::new(),
        symbol,
        side,
        qty: 10.0,
        px: 110.0,
        fee: Some(0.10),
        is_maker: false,
        forced_close,
        venue_ts_ms: recent_replay_ms() + 1,
        recv_ns: 3,
    }
}

/// Asks whether somebody else is holding its symbol, on every quote.
struct Probe {
    saw: Rc<RefCell<Vec<bool>>>,
}

impl Strategy for Probe {
    fn name(&self) -> &str {
        "probe"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".to_string(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            self.saw.lock().unwrap().push(ctx.foreign_position(*symbol));
        }
    }
}

/// A tick short enough that a closed trip reaches the file inside a test.
fn quick_tick() -> EngineSection {
    let mut settings = settings();
    settings.group_flush_ms = 5;
    settings
}

fn latched(records: &Rc<RefCell<Vec<WalRecord>>>) -> bool {
    records.lock().unwrap().iter().any(|r| {
        matches!(
            r,
            WalRecord::Reconciled {
                may_open: false,
                ..
            }
        )
    })
}

fn trades_at(path: &std::path::Path) -> Vec<serde_json::Value> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .map(|line| serde_json::from_str(line).expect("a closed trip is one JSON line"))
        .collect()
}

#[tokio::test]
async fn a_venue_stop_closes_the_sleeves_position_and_prices_the_trip() {
    let file = temp_path("forced-close-trades");
    let (idle, _heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let saw = Rc::new(RefCell::new(Vec::new()));
    let (mut engine, h) = build_holding(
        &quick_tick(),
        allow_all(),
        vec![Box::new(idle), Box::new(Probe { saw: saw.clone() })],
        &["BTCUSDT"],
        &bought_ten(),
        Vec::new(),
        still_held(),
        None,
    )
    .await;
    engine.write_trades(crate::trades::Trades::new(file.path().to_path_buf()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    // Asked before the stop, then after it, so the answer cannot depend on
    // which feed the loop happened to read first.
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(30)),
        )
        .await
        .unwrap();
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![stop_fired(
            symbol,
            Side::Sell,
            Some(ForcedClose::StopLoss),
        )]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(30)),
        )
        .await
        .unwrap();

    let trades = trades_at(file.path());
    assert_eq!(trades.len(), 1, "the stop closed one trip: {trades:?}");
    assert_eq!(trades[0]["sleeve"], "buyer");
    assert_eq!(trades[0]["symbol"], "BTCUSDT");
    assert_eq!(trades[0]["side"], "long");
    let net = trades[0]["round_trip"]["net_usdt"]
        .as_f64()
        .expect("the trip is priced");
    // Ten lots of the ten the price rose, less the two charges of 0.10.
    assert!((net - 99.8).abs() < 1e-9, "{net}");

    assert!(
        !latched(&h.records),
        "a close of our own position is not a stranger's"
    );
    let saw = saw.lock().unwrap();
    assert_eq!(
        saw.first(),
        Some(&true),
        "the sleeve was holding it before the stop"
    );
    assert_eq!(
        saw.last(),
        Some(&false),
        "the stop left the claim flat: {saw:?}"
    );
}

/// The same fill with no reason from the venue is a hand close, and stays a
/// stranger's.
#[tokio::test]
async fn a_blank_fill_with_no_venue_reason_still_stops_the_engine_opening() {
    let file = temp_path("hand-close-trades");
    let (idle, _heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (mut engine, h) = build_holding(
        &quick_tick(),
        allow_all(),
        vec![Box::new(idle)],
        &["BTCUSDT"],
        &bought_ten(),
        Vec::new(),
        still_held(),
        None,
    )
    .await;
    engine.write_trades(crate::trades::Trades::new(file.path().to_path_buf()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![stop_fired(symbol, Side::Sell, None)]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();

    assert!(
        latched(&h.records),
        "a fill nobody here ordered stops it opening"
    );
    assert!(
        trades_at(file.path()).is_empty(),
        "a stranger's trade is not this sleeve's trip"
    );
}

#[tokio::test]
async fn a_forced_close_in_a_symbol_no_sleeve_holds_stays_a_strangers() {
    let file = temp_path("unheld-forced-close-trades");
    let (idle, _heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (mut engine, h) = build_holding(
        &quick_tick(),
        allow_all(),
        vec![Box::new(idle)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
        Vec::new(),
        None,
    )
    .await;
    engine.write_trades(crate::trades::Trades::new(file.path().to_path_buf()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![stop_fired(
            symbol,
            Side::Sell,
            Some(ForcedClose::StopLoss),
        )]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();

    assert!(
        latched(&h.records),
        "nobody here held it, so nobody owns the close"
    );
    assert!(trades_at(file.path()).is_empty());
}

#[tokio::test]
async fn a_forced_close_that_would_grow_the_claim_stays_a_strangers() {
    let file = temp_path("growing-forced-close-trades");
    let (idle, _heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (mut engine, h) = build_holding(
        &quick_tick(),
        allow_all(),
        vec![Box::new(idle)],
        &["BTCUSDT"],
        &bought_ten(),
        Vec::new(),
        still_held(),
        None,
    )
    .await;
    engine.write_trades(crate::trades::Trades::new(file.path().to_path_buf()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    // A purchase against a long is not a close of it, whatever the venue
    // calls the row.
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![stop_fired(
            symbol,
            Side::Buy,
            Some(ForcedClose::StopLoss),
        )]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();

    assert!(
        latched(&h.records),
        "it grew the position rather than closing it"
    );
    assert!(trades_at(file.path()).is_empty());
}

fn two_sleeves_held() -> Vec<WalRecord> {
    let mut records = bought_ten();
    let mut second = records[1..].to_vec();
    for record in &mut second {
        match record {
            WalRecord::OrderSent { request, .. } => {
                request.strategy = StrategyId(1);
                request.client_order_id = "eng-old-2".into();
            }
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        exec_id,
                        client_order_id,
                        ..
                    },
            } => {
                *exec_id = "old-exec-2".into();
                *client_order_id = "eng-old-2".into();
            }
            _ => unreachable!(),
        }
    }
    records.extend(second);
    records
}

#[tokio::test]
async fn shared_forced_fill_allocates_once_and_replays_exactly_after_rotation() {
    let prior = two_sleeves_held();
    let mut positions = still_held();
    positions[0].qty = 20.0;
    let (idle, _) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (mut engine, h) = build_holding(
        &quick_tick(),
        allow_all(),
        vec![
            Box::new(idle),
            Box::new(Probe {
                saw: Rc::new(RefCell::new(Vec::new())),
            }),
        ],
        &["BTCUSDT"],
        &prior,
        Vec::new(),
        positions,
        None,
    )
    .await;
    let mut fill = stop_fired(SymbolId(0), Side::Sell, Some(ForcedClose::StopLoss));
    if let OrderUpdate::Fill { qty, fee, .. } = &mut fill {
        *qty = 15.0;
        *fee = Some(0.15);
    }
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from([fill.clone(), fill]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();
    let base = engine.rotation_base(clock::wall_ms());
    let WalRecord::SegmentBase {
        attribution,
        portfolio: Some(portfolio),
        logged_exposure,
        ..
    } = &base
    else {
        panic!("current portfolio snapshot");
    };
    assert_eq!(
        attribution.len(),
        1,
        "the real net reduction must be allocated across both owners"
    );
    assert_eq!(attribution[0].strategy, StrategyId(1));
    assert_eq!(attribution[0].signed_qty, 5.0);
    assert_eq!(
        logged_exposure[0].signed_qty, 5.0,
        "physical exposure changes exactly once"
    );
    assert!(
        !latched(&h.records),
        "known shared holdings own the venue emergency close"
    );
    let mut records = prior;
    records.extend(h.records.lock().unwrap().clone());
    assert_eq!(
        crate::attribution::Attribution::try_from_records(&records)
            .unwrap()
            .snapshot(),
        *portfolio
    );
    assert_eq!(
        crate::attribution::Attribution::try_from_records(std::slice::from_ref(&base))
            .unwrap()
            .snapshot(),
        *portfolio
    );
    let journal = h.records.lock().unwrap();
    let allocations: Vec<_> = journal
        .iter()
        .filter_map(|record| match record {
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        exec_id,
                        allocation,
                        ..
                    },
            } if exec_id == "venue-stop" => allocation.as_ref(),
            _ => None,
        })
        .collect();
    assert_eq!(
        allocations.len(),
        1,
        "duplicate execution must not allocate twice"
    );
    assert_eq!(
        allocations[0]
            .slices
            .iter()
            .map(|slice| slice.strategy_key.as_str())
            .collect::<Vec<_>>(),
        ["buyer", "probe"]
    );
    assert_eq!(allocations[0].slices[0].quantity.to_f64().unwrap(), 10.0);
    assert_eq!(allocations[0].slices[1].quantity.to_f64().unwrap(), 5.0);
}

fn portfolio_of(base: &WalRecord) -> engine_types::portfolio::PortfolioState {
    let WalRecord::SegmentBase {
        portfolio: Some(state),
        ..
    } = base
    else {
        panic!("portfolio snapshot");
    };
    state.clone()
}

#[tokio::test]
async fn failed_shared_fill_append_keeps_every_sleeve_and_fee_unchanged_until_retry() {
    use std::sync::atomic::{AtomicBool, Ordering};
    let prior = two_sleeves_held();
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let fail_next = Arc::new(AtomicBool::new(false));
    let (risk, risk_saw) = MockRisk::with(allow_all());
    let (venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    let mut held = still_held();
    held[0].qty = 20.0;
    venue.account_readings.lock().unwrap().push_back(held);
    let (idle, heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let mut engine = Engine::boot(
        &settings(),
        "test",
        super::update_contract::FailingUpdateWal {
            inner: wal,
            fail_next: fail_next.clone(),
        },
        risk,
        venue,
        vec![
            Box::new(idle),
            Box::new(Probe {
                saw: Rc::new(RefCell::new(Vec::new())),
            }),
        ],
        &replay_with_history_boundary(&prior),
    )
    .await
    .unwrap();
    let before = portfolio_of(&engine.rotation_base(clock::wall_ms()));
    let mut fill = stop_fired(SymbolId(0), Side::Sell, Some(ForcedClose::StopLoss));
    if let OrderUpdate::Fill { qty, fee, .. } = &mut fill {
        *qty = 15.0;
        *fee = Some(0.15);
    }
    let feed = |updates| ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates,
    };
    risk_saw.lock().unwrap().clear();
    fail_next.store(true, Ordering::SeqCst);
    let error = engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut feed(VecDeque::from([fill.clone()])),
            tokio::time::sleep(Duration::from_millis(30)),
        )
        .await
        .unwrap_err();
    assert!(error
        .to_string()
        .contains("injected private-update append failure"));
    assert_eq!(
        portfolio_of(&engine.rotation_base(clock::wall_ms())),
        before
    );
    assert!(risk_saw.lock().unwrap().is_empty());
    assert!(heard.lock().unwrap().is_empty());
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut feed(VecDeque::from([fill.clone(), fill])),
            tokio::time::sleep(Duration::from_millis(30)),
        )
        .await
        .unwrap();
    assert_eq!(risk_saw.lock().unwrap().len(), 1);
    assert_eq!(heard.lock().unwrap().len(), 1);
    let after = portfolio_of(&engine.rotation_base(clock::wall_ms()));
    assert_eq!(after.positions.len(), 1);
    assert_eq!(after.positions[0].signed_qty.to_f64().unwrap(), 5.0);
    assert!((engine.fills().for_strategy("buyer").fee_usdt.unwrap() - 0.1).abs() < 1e-12);
    assert!((engine.fills().for_strategy("probe").fee_usdt.unwrap() - 0.05).abs() < 1e-12);
    let log: Vec<_> = prior
        .into_iter()
        .chain(records.lock().unwrap().iter().cloned())
        .collect();
    assert_eq!(
        crate::attribution::Attribution::try_from_records(&log)
            .unwrap()
            .snapshot(),
        after
    );
    let replayed = crate::execution::Fills::try_from_records(&log).unwrap();
    assert!((replayed.for_strategy("buyer").fee_usdt.unwrap() - 0.2).abs() < 1e-12);
    assert!((replayed.for_strategy("probe").fee_usdt.unwrap() - 0.15).abs() < 1e-12);
}

#[tokio::test]
async fn boot_history_allocates_shared_emergency_fill_before_account_reconciliation() {
    let prior = two_sleeves_held();
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (risk, _) = MockRisk::with(allow_all());
    let (venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    let mut held = still_held();
    held[0].qty = 5.0;
    venue.account_readings.lock().unwrap().push_back(held);
    *venue.executions.lock().unwrap() = Some(vec![VenueExecution {
        amounts: None,
        exec_id: "recovered-shared-stop".into(),
        client_order_id: String::new(),
        symbol: "BTCUSDT".into(),
        side: Side::Sell,
        qty: 15.0,
        px: 110.0,
        fee: Some(0.15),
        is_maker: false,
        forced_close: Some(ForcedClose::StopLoss),
        venue_ts_ms: recent_replay_ms() + 1,
    }]);
    let (idle, _) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let engine = Engine::boot(
        &settings(),
        "test",
        wal,
        risk,
        venue,
        vec![
            Box::new(idle),
            Box::new(Probe {
                saw: Rc::new(RefCell::new(Vec::new())),
            }),
        ],
        &replay_with_history_boundary(&prior),
    )
    .await
    .unwrap();
    assert!(
        !latched(&records),
        "recovered owned emergency must reconcile to the venue net"
    );
    let base = engine.rotation_base(clock::wall_ms());
    let after = portfolio_of(&base);
    assert_eq!(after.positions.len(), 1);
    assert_eq!(after.positions[0].strategy, StrategyId(1));
    assert_eq!(after.positions[0].signed_qty.to_f64().unwrap(), 5.0);
    let log: Vec<_> = prior
        .into_iter()
        .chain(records.lock().unwrap().iter().cloned())
        .collect();
    assert_eq!(
        crate::attribution::Attribution::try_from_records(&log)
            .unwrap()
            .snapshot(),
        after
    );
    let replayed = crate::execution::Fills::try_from_records(&log).unwrap();
    assert!((replayed.for_strategy("buyer").fee_usdt.unwrap() - 0.2).abs() < 1e-12);
    assert!((replayed.for_strategy("probe").fee_usdt.unwrap() - 0.15).abs() < 1e-12);
    assert_eq!(
        crate::attribution::Attribution::try_from_records(&[base])
            .unwrap()
            .snapshot(),
        after
    );
}
