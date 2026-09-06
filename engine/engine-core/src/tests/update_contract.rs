use super::*;

const ORDER_ID: &str = "eng-update-contract";

struct FiniteUpdates(VecDeque<OrderUpdate>);
impl OrderFeed for FiniteUpdates {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        self.0.pop_front().ok_or(FeedError::Closed)
    }
}

fn prior() -> Vec<WalRecord> {
    vec![
        WalRecord::Names {
            strategies: vec!["buyer".into()],
            symbols: vec!["BTCUSDT".into()],
        },
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: ORDER_ID.into(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 0.01,
                kind: OrderKind::Limit {
                    px: 30_000.0,
                    tif: TimeInForce::Gtc,
                },
                stop: Some(StopSpec {
                    trigger_px: 29_000.0,
                }),
                reduce_only: false,
                exact_terms: None,
                sleeve_effect: None,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 30_000.0,
        },
    ]
}

fn fill(exec_id: &str, qty: f64) -> OrderUpdate {
    OrderUpdate::Fill {
        allocation: None,
        amounts: None,
        exec_id: exec_id.into(),
        client_order_id: ORDER_ID.into(),
        symbol: SymbolId(0),
        side: Side::Buy,
        qty,
        px: 30_000.0,
        fee: Some(qty * 18.0),
        is_maker: false,
        forced_close: None,
        venue_ts_ms: clock::wall_ms(),
        recv_ns: clock::now_ns(),
    }
}

fn state(base: &WalRecord) -> serde_json::Value {
    let WalRecord::SegmentBase {
        attribution,
        logged_exposure,
        intended_stops,
        open_orders,
        recent_execution_ids,
        ..
    } = base
    else {
        panic!("segment base")
    };
    serde_json::json!({
        "attribution": attribution,
        "exposure": logged_exposure,
        "stops": intended_stops,
        "orders": open_orders,
        "executions": recent_execution_ids.iter().map(|row| row.exec_id.as_str()).collect::<Vec<_>>(),
    })
}

fn journaled_updates(records: &Rc<RefCell<Vec<WalRecord>>>) -> Vec<OrderUpdate> {
    records
        .lock()
        .unwrap()
        .iter()
        .filter_map(|record| match record {
            WalRecord::OrderUpdate { update, .. } => Some(update.clone()),
            _ => None,
        })
        .collect()
}

#[tokio::test(start_paused = true)]
async fn late_partial_fills_keep_exact_delivery_order_and_dedup_after_rotation() {
    let (buyer, heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (mut engine, h) = build_with_venue_orders(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &prior(),
        vec![still_working(ORDER_ID, "BTCUSDT", 0.01)],
    )
    .await;
    h.risk_saw.lock().unwrap().clear();
    let first = fill("partial-first", 0.004);
    let late = fill("partial-late", 0.003);
    let cancelled = OrderUpdate::Cancelled {
        client_order_id: ORDER_ID.into(),
        recv_ns: clock::now_ns(),
    };
    let expected = vec![first.clone(), cancelled.clone(), late.clone()];
    let updates = vec![
        first.clone(),
        first.clone(),
        cancelled,
        late.clone(),
        first.clone(),
        late.clone(),
    ];
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut FiniteUpdates(updates.into()),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(*h.risk_saw.lock().unwrap(), expected);
    assert_eq!(journaled_updates(&h.records), expected);
    assert_eq!(
        *heard.lock().unwrap(),
        expected
            .iter()
            .map(|update| format!("{update:?}"))
            .collect::<Vec<_>>()
    );
    assert!(engine.in_flight_ids().is_empty());
    let base = engine.rotation_base(clock::wall_ms());
    let before = state(&base);
    assert_eq!(before["attribution"][0]["signed_qty"], 0.007);
    assert_eq!(before["exposure"][0]["signed_qty"], 0.007);
    assert_eq!(
        before["executions"],
        serde_json::json!(["partial-first", "partial-late"])
    );
    let (buyer, _) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (mut restarted, restart) = build_with_venue_state(
        allow_all(),
        vec![Box::new(buyer)],
        &["BTCUSDT"],
        &[base],
        Vec::new(),
        vec![engine_types::PositionView {
            exact_amounts: None,
            exact_stop_px: None,
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 0.007,
            entry_px: 30_000.0,
            stop_attached: true,
            stop_px: 29_000.0,
            leverage: None,
        }],
    )
    .await;
    restart.risk_saw.lock().unwrap().clear();
    restarted
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut FiniteUpdates(vec![late, first].into()),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert!(restart.risk_saw.lock().unwrap().is_empty());
    assert!(journaled_updates(&restart.records).is_empty());
    assert_eq!(state(&restarted.rotation_base(clock::wall_ms())), before);
}

pub(super) struct FailingUpdateWal {
    pub(super) inner: MockWal,
    pub(super) fail_next: Arc<std::sync::atomic::AtomicBool>,
}
impl Wal for FailingUpdateWal {
    fn append(&mut self, record: &WalRecord) -> Result<u64, WalError> {
        if matches!(record, WalRecord::OrderUpdate { .. })
            && self
                .fail_next
                .swap(false, std::sync::atomic::Ordering::SeqCst)
        {
            return Err(WalError::Io(std::io::Error::other(
                "injected private-update append failure",
            )));
        }
        self.inner.append(record)
    }
    fn barrier(&mut self) -> Result<(), WalError> {
        self.inner.barrier()
    }
    fn barrier_begin(&mut self) -> Result<engine_types::wal::PendingBarrier, WalError> {
        self.inner.barrier_begin()
    }
    fn flush(&mut self) -> Result<(), WalError> {
        self.inner.flush()
    }
}

#[tokio::test(start_paused = true)]
async fn a_failed_fill_append_cannot_change_books_risk_delivery_or_dedup() {
    let tape = tape();
    let (wal, records) = MockWal::new(tape.clone());
    let fail_next = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let (risk, risk_saw) = MockRisk::with(allow_all());
    let (mut venue, _) = MockVenue::new(tape, &["BTCUSDT"]);
    venue.working = vec![still_working(ORDER_ID, "BTCUSDT", 0.01)];
    let (buyer, heard) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let mut engine = Engine::boot(
        &settings(),
        "test",
        FailingUpdateWal {
            inner: wal,
            fail_next: fail_next.clone(),
        },
        risk,
        venue,
        vec![Box::new(buyer)],
        &replay_with_history_boundary(&prior()),
    )
    .await
    .unwrap();
    let before = state(&engine.rotation_base(clock::wall_ms()));
    risk_saw.lock().unwrap().clear();
    fail_next.store(true, std::sync::atomic::Ordering::SeqCst);
    let update = fill("retry-after-append", 0.004);
    let error = engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut FiniteUpdates(vec![update.clone()].into()),
            std::future::pending::<()>(),
        )
        .await
        .unwrap_err();
    assert!(error
        .to_string()
        .contains("injected private-update append failure"));
    assert_eq!(state(&engine.rotation_base(clock::wall_ms())), before);
    assert!(risk_saw.lock().unwrap().is_empty());
    assert!(heard.lock().unwrap().is_empty());
    assert!(journaled_updates(&records).is_empty());
    engine
        .run(
            &mut ScriptFeed::quotes(SymbolId(0), 0, false),
            &mut FiniteUpdates(vec![update.clone(), update.clone()].into()),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    assert_eq!(*risk_saw.lock().unwrap(), vec![update.clone()]);
    assert_eq!(journaled_updates(&records), vec![update]);
    assert_eq!(heard.lock().unwrap().len(), 1);
    let after = state(&engine.rotation_base(clock::wall_ms()));
    assert_eq!(after["exposure"][0]["signed_qty"], 0.004);
    assert_eq!(
        after["executions"],
        serde_json::json!(["retry-after-append"])
    );
}

#[tokio::test(start_paused = true)]
async fn contradictory_portfolio_snapshot_cannot_erase_legacy_quantity_projection() {
    let (buyer, _) = Buyer::new("BTCUSDT", u64::MAX, 0.01);
    let (engine, _) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let mut base = engine.rotation_base(clock::wall_ms());
    if let WalRecord::SegmentBase { attribution, .. } = &mut base {
        attribution.push(engine_types::FilledTotal {
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            signed_qty: 1.0,
        });
    }
    assert!(
        crate::attribution::Attribution::try_from_records(&[base]).is_err(),
        "a current portfolio snapshot silently erased the nonempty quantity projection"
    );
}
