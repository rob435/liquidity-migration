use super::*;
use engine_risk::{EnvelopeConfig, Kernel, KernelConfig};
use engine_types::numeric::{AssetId, Exact, ExactInstrumentSpec, PricePrecision};

pub(crate) fn kernel() -> Kernel {
    Kernel::new(KernelConfig {
        max_account_view_age_ns: 120_000_000_000,
        envelope: EnvelopeConfig {
            tracks_equity: false,
            reference_usdt: 1000.0,
            equity_fraction: 1.0,
            floor_usdt: 100.0,
            expand_dead_band_fraction: 0.05,
            gross_notional_multiple: 2.0,
            disaster_stop_fraction: 0.35,
            max_component_gross_notional_usdt: 2000.0,
            max_symbol_notional_usdt: 2000.0,
            max_initial_margin_usdt: 1000.0,
        },
        leverage: 2.0,
        qty_tolerance: 1e-12,
        max_rolling_loss_fraction: 0.1,
    })
    .unwrap()
}

pub(crate) fn spec() -> ExactInstrumentSpec {
    let d = |s| Some(Exact::parse_decimal(s).unwrap());
    ExactInstrumentSpec {
        native_symbol: "BTCUSDT".into(),
        base_asset: AssetId::Named("BTC".into()),
        quote_asset: AssetId::Named("USDT".into()),
        settlement_asset: AssetId::Named("USDT".into()),
        tick_size: d("0.1"),
        min_price: None,
        max_price: None,
        price_precision: PricePrecision::Tick,
        qty_step: d("0.1"),
        min_qty: d("0.1"),
        market_qty_step: d("0.1"),
        market_min_qty: d("0.1"),
        max_qty: None,
        max_market_qty: None,
        min_notional: d("5"),
        contract_multiplier: d("1"),
        fee_assets: None,
        fee_step: None,
    }
}

struct Once {
    name: &'static str,
    side: Side,
    qty: f64,
    stop: Option<f64>,
    done: bool,
}
impl Strategy for Once {
    fn name(&self) -> &str {
        self.name
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }
    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let MarketEvent::Quote { symbol, .. } = event else {
            return;
        };
        if self.done {
            return;
        }
        self.done = true;
        ctx.place(Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: *symbol,
            side: self.side,
            qty: self.qty,
            kind: OrderKind::Market,
            stop: self.stop.map(|trigger_px| StopSpec { trigger_px }),
            reduce_only: self.stop.is_none(),
            tag: self.name.into(),
            decided_ns: ctx.now_ns(),
            work: None,
            leverage: None,
        });
    }
}
fn sleeve(name: &'static str, side: Side, stop: Option<f64>) -> Box<dyn Strategy> {
    Box::new(Once {
        name,
        side,
        qty: 1.0,
        stop,
        done: false,
    })
}

fn quote() -> ScriptFeed {
    ScriptFeed {
        events: VecDeque::from([MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: Quote {
                bid_px: 99.9,
                ask_px: 100.1,
                bid_qty: 10.0,
                ask_qty: 10.0,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        }]),
        close_at_end: true,
        symbols: vec!["BTCUSDT".into()],
        admits_wrongly: false,
        admitted: Default::default(),
    }
}

#[tokio::test(start_paused = true)]
async fn same_ticker_sleeves_keep_separate_logical_stops_and_one_native_stop() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![
            sleeve("left", Side::Buy, Some(90.0)),
            sleeve("right", Side::Buy, Some(85.0)),
        ],
        &[],
    )
    .await
    .unwrap();
    engine
        .run(
            &mut quote(),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let sends = sends.lock().unwrap();
    assert_eq!(
        sends.len(),
        2,
        "both durable sleeves must be admitted on the same ticker: {:?}",
        *records.lock().unwrap()
    );
    assert_eq!(
        sends.iter().map(|r| r.strategy).collect::<Vec<_>>(),
        [StrategyId(0), StrategyId(1)]
    );
    assert_eq!(sends[0].sleeve_stop().unwrap().trigger_px, 90.0);
    assert_eq!(sends[1].sleeve_stop().unwrap().trigger_px, 85.0);
    assert_eq!(sends[0].stop.unwrap().trigger_px, 90.0);
    assert_eq!(sends[1].stop.unwrap().trigger_px, 90.0);
    assert!(sends.iter().all(|r| !r.reduce_only));
    for request in sends.iter() {
        request
            .exact_terms
            .as_ref()
            .unwrap()
            .validate_projection(request)
            .unwrap();
    }
}

pub(crate) fn idle(name: &'static str) -> Box<dyn Strategy> {
    Box::new(Once {
        name,
        side: Side::Buy,
        qty: 1.0,
        stop: Some(90.0),
        done: true,
    })
}
pub(crate) fn owned_records(left: &str, right: &str) -> Vec<WalRecord> {
    use engine_types::numeric::{AssetAmount, ExactNumber, ExecutionAmounts};
    use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
    use engine_types::orders::SleeveOrderEffect;
    let mut records = vec![WalRecord::Retained(
        engine_types::wal::RetainedWalRecord::Names {
            strategies: vec!["left".into(), "right".into()],
            symbols: vec!["BTCUSDT".into()],
        },
    )];
    for (id, quantity, side, stop) in [(0, left, Side::Buy, "90"), (1, right, Side::Sell, "110")] {
        if quantity == "0" {
            continue;
        }
        let qty = Exact::parse_decimal(quantity).unwrap();
        let stop = Exact::parse_decimal(stop).unwrap();
        let mut request = OrderRequest {
            client_order_id: format!("eng-prior-{id}"),
            strategy: StrategyId(id),
            symbol: SymbolId(0),
            side,
            qty: qty.to_f64().unwrap(),
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            close_position: false,
            sleeve_effect: Some(SleeveOrderEffect::Increase {
                stop: StopSpec {
                    trigger_px: stop.to_f64().unwrap(),
                },
            }),
            exact_terms: None,
        };
        ExactOrderTerms {
            quantity: qty,
            limit_price: None,
            stop_trigger_price: Some(stop.clone()),
            physical_stop_trigger_price: Some(stop),
            input_policy: OrderInputPolicy::StrategyShortestDecimal,
        }
        .apply_projection(&mut request)
        .unwrap();
        records.push(WalRecord::OrderSent {
            dispatch: None,
            request: request.clone(),
            wire_ns: 1,
            arrival_mid: 100.0,
        });
        let amounts = ExecutionAmounts {
            quantity: ExactNumber::venue_decimal(quantity).unwrap(),
            price: ExactNumber::venue_decimal("100").unwrap(),
            fee: Some(AssetAmount {
                asset: AssetId::Named("USDT".into()),
                amount: ExactNumber::venue_decimal("0").unwrap(),
            }),
            settlement_asset: AssetId::Named("USDT".into()),
        };
        records.push(WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: Some(Box::new(amounts)),
                exec_id: format!("prior-fill-{id}"),
                client_order_id: request.client_order_id,
                symbol: SymbolId(0),
                side,
                qty: request.qty,
                px: 100.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: recent_replay_ms(),
                recv_ns: 2,
            },
        });
    }
    records.push(WalRecord::ExecutionHistoryCheckpoint {
        through_wall_ts_ms: recent_replay_ms(),
    });
    records
}
pub(crate) fn physical_long(qty: f64) -> Vec<engine_types::PositionView> {
    vec![engine_types::PositionView {
        exact_amounts: None,
        exact_stop_px: None,
        symbol: SymbolId(0),
        side: Side::Buy,
        qty,
        entry_px: 100.0,
        stop_attached: true,
        stop_px: 90.0,
        leverage: None,
    }]
}
#[tokio::test(start_paused = true)]
async fn an_opposing_sleeve_entry_reduces_native_net_and_retains_its_own_stop() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(physical_long(1.0));
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), sleeve("right", Side::Sell, Some(110.0))],
        &owned_records("1", "0"),
    )
    .await
    .unwrap();
    engine
        .run(
            &mut quote(),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let sends = sends.lock().unwrap();
    assert_eq!(sends.len(), 1, "{:?}", records.lock().unwrap());
    assert!(sends[0].reduce_only);
    assert!(!sends[0].is_sleeve_reduction());
    assert!(sends[0].stop.is_none());
    assert_eq!(sends[0].sleeve_stop().unwrap().trigger_px, 110.0);
    sends[0]
        .exact_terms
        .as_ref()
        .unwrap()
        .validate_projection(&sends[0])
        .unwrap();
}
#[tokio::test(start_paused = true)]
async fn native_stop_closure_settles_offsets_without_inventing_venue_executions() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(physical_long(1.0));
    let prior = owned_records("2", "1");
    let mut engine = Engine::boot(
        &portfolio_settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &prior,
    )
    .await
    .unwrap();
    let stop = OrderUpdate::Fill {
        allocation: None,
        amounts: None,
        exec_id: "native-net-stop".into(),
        client_order_id: String::new(),
        symbol: SymbolId(0),
        side: Side::Sell,
        qty: 1.0,
        px: 89.0,
        fee: Some(0.0),
        is_maker: false,
        forced_close: Some(engine_types::ForcedClose::StopLoss),
        venue_ts_ms: clock::wall_ms(),
        recv_ns: clock::now_ns(),
    };
    let mut market = quote();
    market.close_at_end = false;
    engine
        .run(
            &mut market,
            &mut ScriptOrderFeed::playing(vec![stop]),
            tokio::time::sleep(Duration::from_millis(150)),
        )
        .await
        .unwrap();
    let mut all = prior;
    all.extend(records.lock().unwrap().iter().cloned());
    let state = crate::attribution::Attribution::try_from_records(&all)
        .unwrap()
        .snapshot();
    assert!(
        state.positions.is_empty(),
        "native flat left virtual holdings: {:?}",
        state.positions
    );
    assert_eq!(state.internal_settlements.len(), 2);
    assert_eq!(
        state
            .internal_settlements
            .iter()
            .fold(Exact::zero(), |sum, row| sum + &row.cash_flow),
        Exact::zero()
    );
    assert_eq!(
        records
            .lock()
            .unwrap()
            .iter()
            .filter(|record| matches!(
                record,
                WalRecord::OrderUpdate {
                    update: OrderUpdate::Fill { .. },
                    ..
                }
            ))
            .count(),
        1
    );
    assert!(
        sends.lock().unwrap().is_empty(),
        "balanced offsets need no venue order"
    );
    let base = engine.rotation_base(clock::wall_ms());
    assert!(crate::attribution::Attribution::try_from_records(&[base])
        .unwrap()
        .snapshot()
        .positions
        .is_empty());
}

pub(crate) async fn balanced_engine() -> Engine<MockWal, Kernel, MockVenue> {
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    Engine::boot(
        &portfolio_settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &owned_records("1", "1"),
    )
    .await
    .unwrap()
}

#[tokio::test(start_paused = true)]
async fn native_offset_settlement_resumes_each_durable_crash_cut_without_repeating_a_fill() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(physical_long(1.0));
    let prior = owned_records("2", "1");
    let mut engine = Engine::boot(
        &portfolio_settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &prior,
    )
    .await
    .unwrap();
    let fill = OrderUpdate::Fill {
        allocation: None,
        amounts: None,
        exec_id: "native-crash-cut".into(),
        client_order_id: String::new(),
        symbol: SymbolId(0),
        side: Side::Sell,
        qty: 1.0,
        px: 89.0,
        fee: Some(0.0),
        is_maker: false,
        forced_close: Some(engine_types::ForcedClose::StopLoss),
        venue_ts_ms: clock::wall_ms(),
        recv_ns: clock::now_ns(),
    };
    let mut market = quote();
    market.close_at_end = false;
    engine
        .run(
            &mut market,
            &mut ScriptOrderFeed::playing(vec![fill.clone()]),
            tokio::time::sleep(Duration::from_millis(80)),
        )
        .await
        .unwrap();
    let records = records.lock().unwrap().clone();
    let cuts: Vec<_> = records
        .iter()
        .enumerate()
        .filter_map(|(index, record)| {
            matches!(
                record,
                WalRecord::OrderUpdate {
                    update: OrderUpdate::Fill { .. },
                    ..
                } | WalRecord::PortfolioEmergencyChanged { .. }
                    | WalRecord::PortfolioOffsetSettled { .. }
                    | WalRecord::PortfolioEmergencyCompleted { .. }
            )
            .then_some(index + 1)
        })
        .collect();
    assert!(
        cuts.len() >= 6,
        "the test must cross the fill, every emergency phase and settlement completion"
    );
    for cut in cuts {
        let replay: Vec<_> = prior.iter().chain(records[..cut].iter()).cloned().collect();
        let tape = super::tape();
        let (wal, suffix) = MockWal::new(tape.clone());
        let (mut venue, sent) = MockVenue::new(tape, &["BTCUSDT"]);
        venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
        let mut restart = Engine::boot(
            &portfolio_settings(),
            "0",
            wal,
            kernel(),
            venue,
            vec![idle("left"), idle("right")],
            &replay,
        )
        .await
        .unwrap();
        let mut market = quote();
        market.close_at_end = false;
        restart
            .run(
                &mut market,
                &mut ScriptOrderFeed::playing(vec![fill.clone()]),
                tokio::time::sleep(Duration::from_millis(80)),
            )
            .await
            .unwrap();
        let all: Vec<_> = replay
            .into_iter()
            .chain(suffix.lock().unwrap().iter().cloned())
            .collect();
        let state = crate::attribution::Attribution::try_from_records(&all)
            .unwrap()
            .snapshot();
        assert!(
            state.positions.is_empty(),
            "crash cut {cut} stranded opposing sleeves"
        );
        assert_eq!(state.internal_settlements.len(), 2, "crash cut {cut}");
        assert_eq!(
            all.iter()
                .filter(|record| matches!(record, WalRecord::PortfolioOffsetSettled { .. }))
                .count(),
            1,
            "crash cut {cut} settled twice"
        );
        assert_eq!(all.iter().filter(|record| matches!(record, WalRecord::OrderUpdate { update: OrderUpdate::Fill { exec_id, .. }, .. } if exec_id == "native-crash-cut")).count(), 1, "crash cut {cut} replayed the venue fill twice");
        assert!(
            sent.lock().unwrap().is_empty(),
            "crash cut {cut} invented a physical order for net zero"
        );
        let base = restart.rotation_base(clock::wall_ms());
        assert!(crate::attribution::Attribution::try_from_records(&[base])
            .unwrap()
            .snapshot()
            .positions
            .is_empty());
    }
}

fn portfolio_settings() -> EngineSection {
    let mut configured = settings();
    configured.group_flush_ms = 5;
    configured
}

pub(crate) async fn fragmented_engine() -> Engine<MockWal, Kernel, MockVenue> {
    let mut prior = owned_records("0.4", "0.6");
    for record in &mut prior {
        match record {
            WalRecord::OrderSent { request, .. } if request.strategy == StrategyId(1) => {
                request.side = Side::Buy;
                request.sleeve_effect = Some(engine_types::orders::SleeveOrderEffect::Increase {
                    stop: StopSpec { trigger_px: 90.0 },
                });
                let mut terms = *request.exact_terms.take().unwrap();
                terms.stop_trigger_price = Some(Exact::parse_decimal("90").unwrap());
                terms.physical_stop_trigger_price = terms.stop_trigger_price.clone();
                terms.apply_projection(request).unwrap();
            }
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        client_order_id,
                        side,
                        ..
                    },
                ..
            } if client_order_id == "eng-prior-1" => *side = Side::Buy,
            _ => (),
        }
    }
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    let mut rules = spec();
    rules.min_qty = Some(Exact::parse_decimal("1").unwrap());
    rules.market_min_qty = rules.min_qty.clone();
    venue.exact_specs = Some(vec![("BTCUSDT".into(), rules)]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(physical_long(1.0));
    Engine::boot(
        &portfolio_settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &prior,
    )
    .await
    .unwrap()
}

pub(crate) async fn restart_portfolio(
    records: &[WalRecord],
    positions: Vec<engine_types::PositionView>,
) -> Engine<MockWal, Kernel, MockVenue> {
    let (wal, _) = MockWal::new(tape());
    restart_portfolio_with_wal(wal, records, positions).await
}

pub(crate) async fn restart_portfolio_with_wal<W: Wal>(
    wal: W,
    records: &[WalRecord],
    positions: Vec<engine_types::PositionView>,
) -> Engine<W, Kernel, MockVenue> {
    let tape = tape();
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    venue.account_readings.lock().unwrap().push_back(positions);
    Engine::boot(
        &portfolio_settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        records,
    )
    .await
    .unwrap()
}

pub(crate) async fn exact_single_sleeve_engine(
    quantity: &str,
    max: Option<&str>,
) -> Engine<MockWal, Kernel, MockVenue> {
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    let mut rules = spec();
    rules.qty_step = Some(Exact::parse_decimal("0.000000000000000001").unwrap());
    rules.market_qty_step = rules.qty_step.clone();
    rules.min_qty = rules.qty_step.clone();
    rules.market_min_qty = rules.qty_step.clone();
    rules.min_notional = None;
    rules.max_market_qty = max.map(|value| Exact::parse_decimal(value).unwrap());
    venue.exact_specs = Some(vec![("BTCUSDT".into(), rules)]);
    let mut positions = physical_long(Exact::parse_decimal(quantity).unwrap().to_f64().unwrap());
    positions[0].exact_amounts = Some(Box::new(engine_types::risk::PositionAmounts {
        liquidation_price: None,
        mark_price: None,
        quantity: engine_types::numeric::ExactNumber::venue_decimal(quantity).unwrap(),
        entry_price: engine_types::numeric::ExactNumber::venue_decimal("100").unwrap(),
    }));
    venue.account_readings.lock().unwrap().push_back(positions);
    Engine::boot(
        &portfolio_settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &owned_records(quantity, "0"),
    )
    .await
    .unwrap()
}

pub(crate) fn fail_private_updates(engine: &mut Engine<MockWal, Kernel, MockVenue>, fail: bool) {
    engine.wal.fail_on = fail.then(|| "order_update".into());
}

pub(crate) async fn legacy_single_sleeve_engine(
    legacy_quantity: f64,
    native_quantity: &str,
    step: &str,
) -> Engine<MockWal, Kernel, MockVenue> {
    legacy_single_sleeve_recovery(legacy_quantity, native_quantity, step, vec![], None, None)
        .await
        .unwrap()
}

pub(crate) async fn legacy_single_sleeve_recovery(
    legacy_quantity: f64,
    native_quantity: &str,
    step: &str,
    history: Vec<VenueExecution>,
    prefix: Option<Vec<WalRecord>>,
    fail_barrier: Option<&'static str>,
) -> Result<Engine<MockWal, Kernel, MockVenue>, crate::engine::EngineError> {
    let tape = tape();
    let (mut wal, _) = MockWal::new(tape.clone());
    wal.fail_barrier_after = fail_barrier;
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    *venue.executions.lock().unwrap() = Some(history);
    let mut rules = spec();
    rules.qty_step = Some(Exact::parse_decimal(step).unwrap());
    rules.market_qty_step = rules.qty_step.clone();
    rules.min_qty = rules.qty_step.clone();
    rules.market_min_qty = rules.qty_step.clone();
    rules.min_notional = None;
    venue.exact_specs = Some(vec![("BTCUSDT".into(), rules)]);
    let mut positions = physical_long(native_quantity.parse().unwrap());
    positions[0].exact_amounts = Some(Box::new(engine_types::risk::PositionAmounts {
        liquidation_price: None,
        mark_price: None,
        quantity: engine_types::numeric::ExactNumber::venue_decimal(native_quantity).unwrap(),
        entry_price: engine_types::numeric::ExactNumber::venue_decimal("100").unwrap(),
    }));
    if native_quantity == "0" {
        positions.clear();
    }
    venue.account_readings.lock().unwrap().push_back(positions);
    let prior = serde_json::from_value(serde_json::json!({
        "kind":"segment_base", "wall_ts_ms":recent_replay_ms(),
        "strategies":["left","right"], "symbols":["BTCUSDT"],
        "may_open":true, "control_anchors":[], "open_orders":[],
        "intended_stops":[{"symbol":0,"trigger_px":90.0}],
        "attribution":[{"strategy":0,"symbol":0,"signed_qty":legacy_quantity}],
        "logged_exposure":[{"symbol":0,"signed_qty":legacy_quantity}],
        "execution_history_through_ms":recent_replay_ms()
    }))
    .unwrap();
    Engine::boot(
        &portfolio_settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &prefix.unwrap_or_else(|| vec![prior]),
    )
    .await
}

#[tokio::test(start_paused = true)]
async fn a_foreign_short_latch_cannot_block_the_owned_long_exit_or_clear_the_hand_stop() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, sends) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    let mut physical = physical_long(2.0);
    physical[0].side = Side::Sell;
    physical[0].stop_px = 110.0;
    venue.account_readings.lock().unwrap().push_back(physical);
    let mut prior = owned_records("1", "0");
    prior.push(WalRecord::OrderUpdate {
        callbacks: None,
        update: OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: "hand-short-3".into(),
            client_order_id: "manual".into(),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 3.0,
            px: 100.0,
            fee: Some(0.0),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: recent_replay_ms(),
            recv_ns: 3,
        },
    });
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![sleeve("left", Side::Sell, None), idle("right")],
        &prior,
    )
    .await
    .unwrap();
    engine
        .run(
            &mut quote(),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let requests = sends.lock().unwrap();
    assert_eq!(requests.len(), 1, "{:?}", records.lock().unwrap());
    let request = &requests[0];
    assert!(request.is_sleeve_reduction());
    assert!(
        !request.reduce_only,
        "the venue order grows the physical short"
    );
    assert_eq!(request.side, Side::Sell);
    assert_eq!(request.qty, 1.0);
    assert_eq!(request.stop.unwrap().trigger_px, 110.0);
    assert!(records.lock().unwrap().iter().any(|r| matches!(
        r,
        WalRecord::Reconciled {
            may_open: false,
            ..
        }
    )));
}
