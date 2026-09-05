use super::*;

struct ExitOnQuote;
impl Strategy for ExitOnQuote {
    fn name(&self) -> &str {
        "recovery-exit"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }
    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Market(MarketEvent::Quote { symbol, .. }) = event {
            ctx.place(Intent {
                strategy: StrategyId(0),
                symbol: *symbol,
                side: Side::Sell,
                qty: 0.01,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: true,
                tag: "while-recovery-waits".into(),
                decided_ns: ctx.now_ns(),
                work: None,
                leverage: None,
            });
        }
    }
}

struct QuoteAfterRead {
    control: Arc<MockRecoveryControl>,
    history: bool,
    sent: bool,
}
impl MarketFeed for QuoteAfterRead {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        if self.sent {
            return std::future::pending().await;
        }
        if self.history {
            self.control.history_started.notified().await;
        } else {
            self.control.account_started.notified().await;
        }
        self.sent = true;
        Ok(MarketEvent::Quote {
            symbol: SymbolId(0),
            quote: Quote {
                seq: 1,
                bid_px: 30_000.0,
                bid_qty: 10.0,
                ask_px: 30_001.0,
                ask_qty: 10.0,
                venue_ts_ms: clock::wall_ms(),
                recv_ns: clock::now_ns(),
            },
        })
    }
}

async fn stalled_recovery(history: bool) {
    let (mut engine, h) = super::order_path::build_exit_inventory(
        vec![Box::new(ExitOnQuote)],
        &[(StrategyId(0), Side::Buy, 0.01)],
        None,
    )
    .await;
    let delay = if history {
        &h.recovery_reads.history_delay_ms
    } else {
        &h.recovery_reads.account_delay_ms
    };
    delay.store(300, std::sync::atomic::Ordering::Relaxed);
    let held = engine.account().positions.clone();
    h.account_readings
        .lock()
        .unwrap()
        .extend([held.clone(), held]);
    let mut market = QuoteAfterRead {
        control: h.recovery_reads.clone(),
        history,
        sent: false,
    };
    let update = OrderUpdate::Ack(OrderAck {
        client_order_id: "private-during-recovery".into(),
        venue_order_id: "observed-before-read-return".into(),
        sent_ns: 1,
        ack_ns: clock::now_ns(),
    });
    let mut private = ScriptOrderFeed::playing(vec![
        OrderUpdate::StreamReset {
            recv_ns: clock::now_ns(),
        },
        update,
    ]);
    let deadline = tokio::time::Instant::now() + Duration::from_millis(150);
    engine
        .run(
            &mut market,
            &mut private,
            tokio::time::sleep_until(deadline),
        )
        .await
        .unwrap();
    assert!(h.records.lock().unwrap().iter().any(|row| matches!(row, WalRecord::OrderUpdate { update: OrderUpdate::Ack(ack), .. } if ack.client_order_id == "private-during-recovery")), "a delayed recovery read stopped private order news");
    assert_eq!(
        h.sends
            .lock()
            .unwrap()
            .iter()
            .filter(|request| request.reduce_only && request.side == Side::Sell)
            .count(),
        1,
        "a recovery read held the urgent reducing order behind REST"
    );
}

#[tokio::test]
async fn delayed_account_recovery_keeps_private_news_and_reductions_live() {
    stalled_recovery(false).await;
}

#[tokio::test]
async fn delayed_execution_history_keeps_private_news_and_reductions_live() {
    stalled_recovery(true).await;
}

struct RecoveredObserver(Arc<Mutex<Vec<(f64, f64)>>>);
impl Strategy for RecoveredObserver {
    fn name(&self) -> &str {
        "recovered-observer"
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }]
    }
    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        if let EngineEvent::Order(OrderUpdate::Fill { qty, symbol, .. }) = event {
            self.0.lock().unwrap().push((
                *qty,
                ctx.my_position_facts(*symbol)
                    .unwrap()
                    .attributed_signed_qty,
            ));
        }
    }
}

#[tokio::test]
async fn recovered_fill_callback_observes_committed_inventory_once() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let (mut engine, h) = super::order_path::build_exit_inventory(
        vec![Box::new(RecoveredObserver(seen.clone()))],
        &[(StrategyId(0), Side::Buy, 0.01)],
        None,
    )
    .await;
    *h.executions.lock().unwrap() = Some(vec![VenueExecution {
        exec_id: "missed-stop".into(),
        client_order_id: String::new(),
        symbol: "BTCUSDT".into(),
        side: Side::Sell,
        qty: 0.005,
        px: 29_000.0,
        fee: Some(0.1),
        amounts: None,
        is_maker: false,
        forced_close: Some(engine_types::ForcedClose::StopLoss),
        venue_ts_ms: clock::wall_ms(),
    }]);
    engine.renew_execution_history().await.unwrap();
    assert_eq!(
        *seen.lock().unwrap(),
        [(0.005, 0.005)],
        "recovered execution callback disappeared or observed inventory before commitment"
    );
    engine.renew_execution_history().await.unwrap();
    assert_eq!(
        seen.lock().unwrap().len(),
        1,
        "overlapping execution history repeated the callback"
    );
}

#[tokio::test]
async fn distinct_execution_ids_with_identical_partial_fill_fields_are_both_owned() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let (mut engine, h) = super::order_path::build_exit_inventory(
        vec![Box::new(RecoveredObserver(seen.clone()))],
        &[(StrategyId(0), Side::Buy, 0.02)],
        None,
    )
    .await;
    let stamp = clock::wall_ms();
    *h.executions.lock().unwrap() = Some(vec![VenueExecution {
        exec_id: "rest-distinct-id".into(),
        client_order_id: String::new(),
        symbol: "BTCUSDT".into(),
        side: Side::Sell,
        qty: 0.005,
        px: 29_000.0,
        fee: Some(0.1),
        amounts: None,
        is_maker: false,
        forced_close: Some(engine_types::ForcedClose::StopLoss),
        venue_ts_ms: stamp,
    }]);
    let private_fill = OrderUpdate::Fill {
        exec_id: "private-distinct-id".into(),
        client_order_id: String::new(),
        symbol: SymbolId(0),
        side: Side::Sell,
        qty: 0.005,
        px: 29_000.0,
        fee: Some(0.1),
        amounts: None,
        allocation: None,
        is_maker: false,
        forced_close: Some(engine_types::ForcedClose::StopLoss),
        venue_ts_ms: stamp,
        recv_ns: clock::now_ns(),
    };
    let mut private = ScriptOrderFeed::playing(vec![
        private_fill,
        OrderUpdate::StreamReset {
            recv_ns: clock::now_ns(),
        },
    ]);
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut private,
            tokio::time::sleep(Duration::from_millis(80)),
        )
        .await
        .unwrap();
    assert!(h.records.lock().unwrap().iter().any(|row| matches!(row, WalRecord::RecoveredFill { exec_id, .. } if exec_id == "rest-distinct-id")), "valid execution identity was overwritten by the legacy field heuristic");
    assert_eq!(*seen.lock().unwrap(), [(0.005, 0.015), (0.005, 0.01)]);
}
