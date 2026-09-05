use super::shared_sleeves::{idle, kernel, owned_records, physical_long, spec};
use super::*;
use engine_types::numeric::Exact;
use engine_types::Action;

struct MoveStop {
    name: &'static str,
    trigger: Option<f64>,
}
impl Strategy for MoveStop {
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
        if let (MarketEvent::Quote { symbol, .. }, Some(trigger_px)) = (event, self.trigger.take())
        {
            ctx.emit(Action::SetStop {
                symbol: *symbol,
                trigger_px,
            });
        }
    }
}
fn quote(close_at_end: bool) -> ScriptFeed {
    ScriptFeed {
        events: VecDeque::from([MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: Quote {
                bid_px: 99.9,
                ask_px: 100.1,
                bid_qty: 1.0,
                ask_qty: 1.0,
                recv_ns: clock::now_ns(),
                ..Default::default()
            },
        }]),
        close_at_end,
        symbols: vec!["BTCUSDT".into()],
        admits_wrongly: false,
        admitted: Default::default(),
    }
}
#[tokio::test(start_paused = true)]
async fn a_weaker_native_stop_with_the_same_binary64_projection_is_repaired() {
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    let mut held = physical_long(1.0);
    let weaker = Exact::parse_decimal("89.99999999999999999999").unwrap();
    assert_eq!(weaker.to_f64().unwrap(), held[0].stop_px);
    held[0].exact_stop_px = Some(Box::new(weaker));
    let held = serde_json::from_slice(&serde_json::to_vec(&held).unwrap()).unwrap();
    venue.account_readings.lock().unwrap().push_back(held);
    let exact = venue.exact_stops.clone();
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &owned_records("1", "0"),
    )
    .await
    .unwrap();
    engine
        .run(
            &mut quote(true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(
        exact.lock().unwrap().len(),
        1,
        "equal binary64 projections cannot confirm unequal native stop prices"
    );
    assert_eq!(
        exact.lock().unwrap()[0].1.trigger_price,
        Exact::parse_decimal("90").unwrap()
    );
}
#[tokio::test(start_paused = true)]
async fn an_owned_virtual_stop_is_durable_when_opposing_sleeves_leave_the_venue_flat() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    let exact = venue.exact_stops.clone();
    let prior = owned_records("1", "1");
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![
            Box::new(MoveStop {
                name: "left",
                trigger: Some(95.01),
            }),
            idle("right"),
        ],
        &prior,
    )
    .await
    .unwrap();
    engine
        .run(
            &mut quote(true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let WalRecord::SegmentBase {
        portfolio: Some(snapshot),
        ..
    } = engine.rotation_base(7)
    else {
        panic!()
    };
    assert_eq!(
        snapshot
            .positions
            .iter()
            .find(|p| p.strategy == StrategyId(0))
            .unwrap()
            .stop_px,
        Some(Exact::parse_decimal("95.1").unwrap()),
        "the owner stop must persist even with zero physical position"
    );
    assert_eq!(
        snapshot
            .positions
            .iter()
            .find(|p| p.strategy == StrategyId(1))
            .unwrap()
            .stop_px,
        Some(Exact::parse_decimal("110").unwrap())
    );
    assert!(
        exact.lock().unwrap().is_empty(),
        "no native stop direction exists for physical net zero"
    );
    assert!(records.lock().unwrap().iter().any(|r| matches!(
        r,
        WalRecord::SleeveStopSet {
            strategy: StrategyId(0),
            side: Side::Buy,
            ..
        }
    )));
    let rotated = engine.rotation_base(7);
    let restored = crate::attribution::Attribution::try_from_records(&[rotated]).unwrap();
    assert_eq!(restored.snapshot().positions, snapshot.positions);
}
#[tokio::test(start_paused = true)]
async fn a_stop_move_uses_exact_native_terms_and_the_earliest_surviving_sleeve_stop() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(physical_long(1.0));
    let exact = venue.exact_stops.clone();
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![
            Box::new(MoveStop {
                name: "left",
                trigger: Some(95.01),
            }),
            idle("right"),
        ],
        &owned_records("2", "1"),
    )
    .await
    .unwrap();
    engine
        .run(
            &mut quote(true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let calls = exact.lock().unwrap();
    assert_eq!(
        calls.len(),
        1,
        "native repair must use the exact mutation API: {:?}",
        records.lock().unwrap()
    );
    assert_eq!(
        calls[0].1.trigger_price,
        Exact::parse_decimal("95.1").unwrap()
    );
    assert_eq!(calls[0].1.position_side, Side::Buy);
    let WalRecord::SegmentBase {
        portfolio: Some(portfolio),
        ..
    } = engine.rotation_base(7)
    else {
        panic!()
    };
    assert_eq!(
        portfolio
            .positions
            .iter()
            .find(|p| p.strategy == StrategyId(1))
            .unwrap()
            .stop_px,
        Some(Exact::parse_decimal("110").unwrap())
    );
}

struct NewsAfterStop {
    records: Arc<Mutex<Vec<WalRecord>>>,
    sent: bool,
}
impl OrderFeed for NewsAfterStop {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        if self.sent {
            return std::future::pending().await;
        }
        loop {
            if self
                .records
                .lock()
                .unwrap()
                .iter()
                .any(|r| matches!(r, WalRecord::StopSet { .. }))
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
        self.sent = true;
        Ok(OrderUpdate::Ack(engine_types::OrderAck {
            client_order_id: "stop-private-news".into(),
            venue_order_id: "during-stop-wait".into(),
            sent_ns: clock::now_ns(),
            ack_ns: clock::now_ns(),
        }))
    }
}
#[tokio::test]
async fn stop_disk_and_http_waits_leave_private_updates_live() {
    let tape = tape();
    let (mut wal, records) = MockWal::new(tape.clone());
    wal.defer_barriers();
    wal.barrier_takes = Duration::from_millis(250);
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(physical_long(1.0));
    venue.stop_delay = Duration::from_millis(250);
    let calls = venue.stops.clone();
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![
            Box::new(MoveStop {
                name: "left",
                trigger: Some(95.01),
            }),
            idle("right"),
        ],
        &owned_records("1", "0"),
    )
    .await
    .unwrap();
    let mut feed = NewsAfterStop {
        records: records.clone(),
        sent: false,
    };
    let observer = async {
        tokio::time::timeout(Duration::from_millis(150), async {
            loop {
                if records.lock().unwrap().iter().any(|r| matches!(r, WalRecord::OrderUpdate { update: OrderUpdate::Ack(ack), .. } if ack.venue_order_id == "during-stop-wait")) { break; }
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        }).await.expect("stop durability/HTTP must not stall the private update loop");
        assert!(
            calls.lock().unwrap().is_empty(),
            "physical stop escaped before its durable intent barrier"
        );
    };
    let mut market = quote(false);
    let (run, ()) = tokio::join!(
        engine.run(
            &mut market,
            &mut feed,
            tokio::time::sleep(Duration::from_millis(600))
        ),
        observer
    );
    run.unwrap();
    assert_eq!(calls.lock().unwrap().len(), 1);
}
#[tokio::test(start_paused = true)]
async fn a_failed_stop_intent_barrier_prevents_the_native_mutation() {
    let tape = tape();
    let (mut wal, _) = MockWal::new(tape.clone());
    wal.fail_barrier_after = Some("stop_set");
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(physical_long(1.0));
    let calls = venue.stops.clone();
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![
            Box::new(MoveStop {
                name: "left",
                trigger: Some(95.01),
            }),
            idle("right"),
        ],
        &owned_records("1", "0"),
    )
    .await
    .unwrap();
    let run = engine
        .run(
            &mut quote(true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await;
    assert!(
        calls.lock().unwrap().is_empty(),
        "a native stop call escaped its failing durability barrier"
    );
    assert!(run.is_err());
}

#[tokio::test(start_paused = true)]
async fn typed_boot_repairs_wait_for_current_market_reference_and_use_exact_wire() {
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    let mut held = physical_long(1.0);
    held[0].stop_attached = false;
    held[0].stop_px = 0.0;
    venue.account_readings.lock().unwrap().push_back(held);
    let calls = venue.stops.clone();
    let exact = venue.exact_stops.clone();
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &owned_records("1", "0"),
    )
    .await
    .unwrap();
    assert!(
        calls.lock().unwrap().is_empty(),
        "boot must not send a legacy float stop before a current reference exists"
    );
    engine
        .run(
            &mut quote(true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(exact.lock().unwrap().len(), 1);
    assert_eq!(
        exact.lock().unwrap()[0].1.trigger_price,
        Exact::parse_decimal("90").unwrap()
    );
    let WalRecord::SegmentBase { may_open, .. } = engine.rotation_base(7) else {
        panic!()
    };
    assert!(
        may_open,
        "a successfully repaired stop must not invent a permanent reconciliation fault"
    );
}
#[tokio::test(start_paused = true)]
async fn a_standing_native_stop_needs_no_quote_and_does_not_set_a_reconciliation_latch() {
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .extend([physical_long(1.0), physical_long(1.0)]);
    let exact = venue.exact_stops.clone();
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &owned_records("1", "0"),
    )
    .await
    .unwrap();
    let mut market = ScriptFeed {
        events: VecDeque::new(),
        ..quote(false)
    };
    engine
        .run(
            &mut market,
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset {
                recv_ns: clock::now_ns(),
            }]),
            tokio::time::sleep(Duration::from_millis(30)),
        )
        .await
        .unwrap();
    let WalRecord::SegmentBase { may_open, .. } = engine.rotation_base(7) else {
        panic!()
    };
    assert!(
        may_open,
        "missing quote is a temporary read dependency, not evidence of a protection fault"
    );
    assert!(exact.lock().unwrap().is_empty());
}

#[tokio::test(start_paused = true)]
async fn a_failed_native_repair_preserves_the_missing_stop_and_records_an_emergency() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    let mut held = physical_long(1.0);
    held[0].stop_attached = false;
    held[0].stop_px = 0.0;
    venue.account_readings.lock().unwrap().push_back(held);
    *venue.stop_failures_remaining.lock().unwrap() = 1;
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &owned_records("1", "0"),
    )
    .await
    .unwrap();
    engine
        .run(
            &mut quote(false),
            &mut ScriptOrderFeed::empty(),
            tokio::time::sleep(Duration::from_millis(40)),
        )
        .await
        .unwrap();
    let WalRecord::SegmentBase { may_open, .. } = engine.rotation_base(7) else {
        panic!()
    };
    assert!(!may_open);
    assert!(records.lock().unwrap().iter().any(|r| matches!(r, WalRecord::PortfolioEmergencyChanged { state } if state.reason == engine_types::portfolio_control::PortfolioEmergencyReason::ProtectionUnavailable)), "failed physical protection must retain a durable engine-owned emergency");
}
#[tokio::test(start_paused = true)]
async fn a_successful_exact_repair_preserves_an_unrelated_reconciliation_latch() {
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    let mut held = physical_long(1.0);
    held[0].stop_attached = false;
    held[0].stop_px = 0.0;
    venue.account_readings.lock().unwrap().push_back(held);
    let exact = venue.exact_stops.clone();
    let mut prior = owned_records("1", "0");
    prior.push(WalRecord::Reconciled {
        wall_ts_ms: clock::wall_ms(),
        findings: vec!["unrelated account fault".into()],
        may_open: false,
    });
    let mut engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &prior,
    )
    .await
    .unwrap();
    engine
        .run(
            &mut quote(true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(exact.lock().unwrap().len(), 1);
    let WalRecord::SegmentBase { may_open, .. } = engine.rotation_base(7) else {
        panic!()
    };
    assert!(
        !may_open,
        "native repair may only clear its own symbol's temporary repair obligation"
    );
}

#[tokio::test(start_paused = true)]
async fn a_legacy_working_order_keeps_binary64_stop_authority_under_exact_metadata() {
    let tape = tape();
    let (wal, _) = MockWal::new(tape.clone());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.exact_specs = Some(vec![("BTCUSDT".into(), spec())]);
    venue
        .account_readings
        .lock()
        .unwrap()
        .push_back(physical_long(1.0));
    let id = "eng-legacy-working";
    venue.working.push(VenueOrder {
        client_order_id: id.into(),
        symbol: "BTCUSDT".into(),
        side: Side::Buy,
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
    });
    let mut prior = owned_records("1", "0");
    prior.push(WalRecord::OrderSent {
        dispatch: None,
        request: OrderRequest {
            client_order_id: id.into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.1,
            kind: OrderKind::Limit {
                px: 100.0,
                tif: TimeInForce::Gtc,
            },
            stop: Some(StopSpec { trigger_px: 80.0 }),
            reduce_only: false,
            close_position: false,
            exact_terms: None,
            sleeve_effect: None,
        },
        wire_ns: 3,
        arrival_mid: 100.0,
    });
    let engine = Engine::boot(
        &settings(),
        "0",
        wal,
        kernel(),
        venue,
        vec![idle("left"), idle("right")],
        &prior,
    )
    .await
    .unwrap();
    let WalRecord::SegmentBase { may_open, .. } = engine.rotation_base(7) else {
        panic!()
    };
    assert!(may_open, "legacy stop is explicit binary64 migration authority; missing new terms must not invent an ownership fault");
}
