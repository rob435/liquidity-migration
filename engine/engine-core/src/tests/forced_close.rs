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
            request: OrderRequest {
                client_order_id: "eng-old-1".to_string(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 10.0,
                kind: OrderKind::Market,
                stop: Some(StopSpec { trigger_px: 90.0 }),
                reduce_only: false,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 100.0,
        },
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
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
