//! What this engine's own closed round trips tell the risk kernel, live and
//! across a restart.
//!
//! The bench these run on -- the tape, the mocks and the helpers -- is
//! [`super`].

use super::*;

use engine_types::ForcedClose;

/// A tick short enough that a closed trip reaches the kernel inside a test.
fn quick_tick() -> EngineSection {
    let mut settings = settings();
    settings.group_flush_ms = 5;
    settings
}

fn names() -> WalRecord {
    WalRecord::Names {
        strategies: vec!["buyer".to_string()],
        symbols: vec!["BTCUSDT".to_string()],
    }
}

fn sent(id: &str, side: Side, qty: f64) -> WalRecord {
    WalRecord::OrderSent {
        request: OrderRequest {
            client_order_id: id.to_string(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side,
            qty,
            kind: OrderKind::Market,
            stop: Some(StopSpec { trigger_px: 90.0 }),
            reduce_only: side == Side::Sell,
            close_position: false,
        },
        wire_ns: 1,
        arrival_mid: 100.0,
    }
}

fn filled(id: &str, side: Side, qty: f64, px: f64, venue_ts_ms: i64) -> WalRecord {
    WalRecord::OrderUpdate {
        update: OrderUpdate::Fill {
            exec_id: format!("exec-{id}"),
            client_order_id: id.to_string(),
            symbol: SymbolId(0),
            side,
            qty,
            px,
            fee: Some(0.10),
            is_maker: false,
            forced_close: None,
            venue_ts_ms,
            recv_ns: 2,
        },
    }
}

/// A previous run bought ten at 100 and is still holding them.
fn bought_ten() -> Vec<WalRecord> {
    vec![
        names(),
        sent("eng-old-1", Side::Buy, 10.0),
        filled("eng-old-1", Side::Buy, 10.0, 100.0, recent_replay_ms()),
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

/// The stop firing ten below the entry: the venue names no order of ours and
/// says why it traded.
fn stop_fired(symbol: SymbolId) -> OrderUpdate {
    OrderUpdate::Fill {
        exec_id: "venue-stop".to_string(),
        client_order_id: String::new(),
        symbol,
        side: Side::Sell,
        qty: 10.0,
        px: 90.0,
        fee: Some(0.10),
        is_maker: false,
        forced_close: Some(ForcedClose::StopLoss),
        venue_ts_ms: recent_replay_ms() + 1,
        recv_ns: 3,
    }
}

/// Ten bought at 100 and sold at 90, less the two charges of 0.10.
const LOST: f64 = -100.2;

/// A restatement holding whatever the caller wants restated. Every other
/// field is what a rotation of a quiet engine would write.
fn segment_base(
    attribution: Vec<engine_types::FilledTotal>,
    logged_exposure: Vec<engine_types::SymbolTotal>,
    rolling_loss_rows: Vec<ClosedTradeRow>,
) -> WalRecord {
    WalRecord::SegmentBase {
        wall_ts_ms: recent_replay_ms(),
        strategies: vec!["buyer".to_string()],
        symbols: vec!["BTCUSDT".to_string()],
        may_open: true,
        control_anchors: Vec::new(),
        attribution,
        logged_exposure,
        intended_stops: vec![engine_types::IntendedStop {
            symbol: SymbolId(0),
            side: Some(Side::Buy),
            trigger_px: 90.0,
        }],
        recent_execution_ids: Vec::new(),
        execution_history_through_ms: Some(recent_replay_ms()),
        target_book_latches: Vec::new(),
        strategy_checkpoints: Vec::new(),
        strategy_global_checkpoints: Vec::new(),
        strategy_events: Vec::new(),
        signal_observations: Vec::new(),
        signal_cursors: Vec::new(),
        signal_subscriptions: Vec::new(),
        runtime_control_requests: Vec::new(),
        runtime_control_consumed: Vec::new(),
        open_orders: Vec::new(),
        rolling_loss_rows,
    }
}

fn close(row: &ClosedTradeRow, closed_ms: i64, net_usdt: f64) -> bool {
    row.closed_ms == closed_ms && (row.net_usdt - net_usdt).abs() < 1e-9
}

/// Run the loop long enough for the group-flush tick that drains the closed
/// trips to come round.
async fn feed_the_stop(engine: &mut Engine<MockWal, MockRisk, MockVenue>, symbol: SymbolId) {
    let mut orders = ScriptOrderFeed {
        learned: Rc::new(RefCell::new(Vec::new())),
        updates: VecDeque::from(vec![stop_fired(symbol)]),
    };
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut orders,
            tokio::time::sleep(Duration::from_millis(60)),
        )
        .await
        .unwrap();
}

#[tokio::test]
async fn a_priced_loss_reaches_the_kernel_with_no_trades_file_configured() {
    // The window counts what this engine did, not what somebody chose to
    // file: no `write_trades` here on purpose.
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
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    feed_the_stop(&mut engine, symbol).await;

    let closes = h.risk_rolling.closes();
    assert_eq!(closes.len(), 1, "one trip closed: {closes:?}");
    assert!(
        close(&closes[0], recent_replay_ms() + 1, LOST),
        "the closing fill's stamp and the trip's net: {:?}",
        closes[0]
    );
}

#[tokio::test]
async fn a_close_the_log_cannot_price_is_not_counted() {
    // A position restated across a rotation: the fills that opened it are in
    // a segment this boot never reads, so the close carries no number.
    let file = temp_path("unpriced-close-trades");
    let (idle, _heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (mut engine, h) = build_holding(
        &quick_tick(),
        allow_all(),
        vec![Box::new(idle)],
        &["BTCUSDT"],
        &[segment_base(
            vec![engine_types::FilledTotal {
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                signed_qty: 10.0,
            }],
            vec![engine_types::SymbolTotal {
                symbol: SymbolId(0),
                signed_qty: 10.0,
            }],
            Vec::new(),
        )],
        Vec::new(),
        still_held(),
        None,
    )
    .await;
    engine.write_trades(crate::trades::Trades::new(file.path().to_path_buf()));
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    feed_the_stop(&mut engine, symbol).await;

    let filed: Vec<serde_json::Value> = std::fs::read_to_string(file.path())
        .unwrap_or_default()
        .lines()
        .map(|line| serde_json::from_str(line).expect("a closed trip is one JSON line"))
        .collect();
    assert_eq!(filed.len(), 1, "the trip did close: {filed:?}");
    assert!(filed[0]["round_trip"].is_null(), "and carries no number");
    assert!(
        h.risk_rolling.closes().is_empty(),
        "so the kernel is told nothing: {:?}",
        h.risk_rolling.calls()
    );
}

#[tokio::test]
async fn boot_hands_the_kernel_what_the_log_already_closed_and_then_the_clock() {
    let before = clock::wall_ms();
    let log = vec![
        names(),
        sent("eng-1", Side::Buy, 10.0),
        filled("eng-1", Side::Buy, 10.0, 100.0, recent_replay_ms()),
        sent("eng-2", Side::Sell, 10.0),
        filled("eng-2", Side::Sell, 10.0, 90.0, recent_replay_ms() + 1),
    ];
    let (idle, _heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (_engine, h) = build(allow_all(), vec![Box::new(idle)], &["BTCUSDT"], &log).await;

    let calls = h.risk_rolling.calls();
    assert_eq!(calls.len(), 2, "one close and one clock: {calls:?}");
    let RollingLossCall::Closed(row) = &calls[0] else {
        panic!("the log's own close comes first: {calls:?}");
    };
    assert!(
        close(row, recent_replay_ms() + 1, LOST),
        "the closing fill's stamp and the trip's net: {row:?}"
    );
    let RollingLossCall::Clock(wall_ms) = calls[1] else {
        panic!("the clock is observed after it: {calls:?}");
    };
    assert!(
        wall_ms >= before,
        "a wall stamp taken at boot, not a made-up one: {wall_ms}"
    );
}

#[tokio::test]
async fn boot_restores_the_segments_window_before_the_segments_own_closes() {
    // Restore first, add second: the other order would either count the
    // restated trips twice or lose the one that tripped the limit.
    let restated = ClosedTradeRow {
        closed_ms: recent_replay_ms() - 5_000,
        net_usdt: -40.0,
    };
    let log = vec![
        segment_base(Vec::new(), Vec::new(), vec![restated]),
        sent("eng-1", Side::Buy, 10.0),
        filled("eng-1", Side::Buy, 10.0, 100.0, recent_replay_ms()),
        sent("eng-2", Side::Sell, 10.0),
        filled("eng-2", Side::Sell, 10.0, 90.0, recent_replay_ms() + 1),
    ];
    let (idle, _heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (_engine, h) = build(allow_all(), vec![Box::new(idle)], &["BTCUSDT"], &log).await;

    let calls = h.risk_rolling.calls();
    assert_eq!(calls.len(), 3, "restore, close, clock: {calls:?}");
    assert_eq!(calls[0], RollingLossCall::Restored(vec![restated]));
    let RollingLossCall::Closed(row) = &calls[1] else {
        panic!("this segment's own close comes second: {calls:?}");
    };
    assert!(close(row, recent_replay_ms() + 1, LOST), "{row:?}");
    assert!(matches!(calls[2], RollingLossCall::Clock(_)));
}

#[tokio::test]
async fn every_fresh_account_reading_ages_the_window() {
    // Nothing closes here on purpose: without a clock of its own the window
    // would hold a losing day forever.
    let mut refresh_every_tick = quick_tick();
    refresh_every_tick.account_view_max_age_ms = 0;
    let (idle, _heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (mut engine, h) = build_with(
        &refresh_every_tick,
        allow_all(),
        vec![Box::new(idle)],
        &["BTCUSDT"],
        &[],
        Vec::new(),
    )
    .await;
    let at_boot = h.risk_rolling.calls().len();
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    let after: Vec<RollingLossCall> = h.risk_rolling.calls().split_off(at_boot);
    assert!(
        !after.is_empty()
            && after.iter().all(|call| {
                matches!(call, RollingLossCall::Clock(wall_ms) if *wall_ms > 1_700_000_000_000)
            }),
        "the run's account readings age the window and nothing else: {after:?}"
    );
}

#[tokio::test]
async fn a_rotation_carries_the_kernels_window() {
    let (idle, _heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (engine, h) = build(allow_all(), vec![Box::new(idle)], &["BTCUSDT"], &[]).await;
    let held = vec![
        ClosedTradeRow {
            closed_ms: recent_replay_ms(),
            net_usdt: -12.5,
        },
        ClosedTradeRow {
            closed_ms: recent_replay_ms() + 1,
            net_usdt: 3.0,
        },
    ];
    *h.risk_rolling.rows.lock().unwrap() = held.clone();

    let WalRecord::SegmentBase {
        rolling_loss_rows, ..
    } = engine.rotation_base(recent_replay_ms())
    else {
        panic!("rotation_base must build a SegmentBase record");
    };

    assert_eq!(rolling_loss_rows, held);
}

// ------------------------------------------- the shipped kernel, end to end

/// Sells a slice of what is held, reduce-only, on every quote.
struct Exiter {
    symbol: String,
    qty: f64,
}

impl Strategy for Exiter {
    fn name(&self) -> &str {
        "exiter"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            ctx.place(Intent {
                strategy: StrategyId(1),
                symbol: *symbol,
                side: Side::Sell,
                qty: self.qty,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: true,
                tag: "exit".into(),
                decided_ns: ctx.now_ns(),
                work: None,
                leverage: None,
            });
        }
    }
}

/// Every cap wide enough that this bench's little order never reaches one,
/// and the rolling loss limit at 100 — one cent inside what the log already
/// lost. The only refusal this config can produce is the one under test.
fn only_the_window_binds() -> engine_risk::KernelConfig {
    engine_risk::KernelConfig {
        max_account_view_age_ns: 120_000_000_000,
        envelope: engine_risk::EnvelopeConfig {
            tracks_equity: false,
            reference_usdt: 250_000.0,
            equity_fraction: 1.0,
            floor_usdt: 100.0,
            expand_dead_band_fraction: 0.05,
            gross_notional_multiple: 2.0,
            disaster_stop_fraction: 0.35,
            max_component_gross_notional_usdt: 500_000.0,
            max_initial_margin_usdt: 250_000.0,
        },
        leverage: 2.0,
        qty_tolerance: 1e-12,
        max_rolling_loss_fraction: 0.000_4,
    }
}

#[tokio::test]
async fn the_shipped_kernel_refuses_the_next_entry_and_still_lets_the_exit_out() {
    // The whole path, with the engine's own gate in place of the mock: a log
    // whose closed trips are past the limit, read back at boot.
    let log = vec![
        WalRecord::Names {
            strategies: vec!["buyer".to_string(), "exiter".to_string()],
            symbols: vec!["BTCUSDT".to_string()],
        },
        sent("eng-1", Side::Buy, 10.0),
        filled("eng-1", Side::Buy, 10.0, 100.0, recent_replay_ms()),
        sent("eng-2", Side::Sell, 10.0),
        filled("eng-2", Side::Sell, 10.0, 90.0, recent_replay_ms() + 1),
        WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: "eng-3".to_string(),
                strategy: StrategyId(1),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: Some(StopSpec { trigger_px: 90.0 }),
                reduce_only: false,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 100.0,
        },
        filled("eng-3", Side::Buy, 1.0, 100.0, recent_replay_ms() + 2),
    ];

    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (venue, _sends) = MockVenue::new(tape.clone(), &["BTCUSDT"]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(vec![engine_types::PositionView {
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            entry_px: 100.0,
            stop_attached: true,
            stop_px: 90.0,
            leverage: None,
        }]);
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let mut engine = Engine::boot(
        &quick_tick(),
        "0000000000000000",
        wal,
        engine_risk::Kernel::new(only_the_window_binds()).expect("the kernel takes this config"),
        venue,
        vec![
            Box::new(buyer),
            Box::new(Exiter {
                symbol: "BTCUSDT".to_string(),
                qty: 0.5,
            }),
        ],
        &replay_with_history_boundary(&log),
    )
    .await
    .expect("boot");
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();

    let records = records.lock().unwrap();
    let tripped: Vec<(f64, f64)> = records
        .iter()
        .filter_map(|record| match record {
            WalRecord::Verdict {
                verdict:
                    RiskVerdict::Deny {
                        reason:
                            DenyReason::RollingLossTripped {
                                window_net_usdt,
                                limit_usdt,
                                ..
                            },
                    },
                ..
            } => Some((*window_net_usdt, *limit_usdt)),
            _ => None,
        })
        .collect();
    assert!(
        !tripped.is_empty(),
        "the entry is refused for the day's losses"
    );
    assert!((tripped[0].0 - LOST).abs() < 1e-9, "{:?}", tripped[0]);
    assert!((tripped[0].1 - 100.0).abs() < 1e-9, "{:?}", tripped[0]);

    let sent_reduce_only: Vec<bool> = records
        .iter()
        .filter_map(|record| match record {
            WalRecord::OrderSent { request, .. } => Some(request.reduce_only),
            _ => None,
        })
        .collect();
    assert!(
        sent_reduce_only.iter().any(|only| *only),
        "the exit still goes out: {sent_reduce_only:?}"
    );
    assert!(
        !sent_reduce_only.iter().any(|only| !*only),
        "and nothing opening does: {sent_reduce_only:?}"
    );
}
