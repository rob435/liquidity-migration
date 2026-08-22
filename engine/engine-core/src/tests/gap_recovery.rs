//! Fills the private stream never delivered, recovered from the venue's own
//! execution history after a reconnect.

use super::*;

/// Wait for the recovery pass to write its record, so the assertions below are
/// about what the engine did rather than about how fast it did it.
async fn until_recovered(records: Rc<RefCell<Vec<WalRecord>>>) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while tokio::time::Instant::now() < deadline {
        if records
            .borrow()
            .iter()
            .any(|r| matches!(r, WalRecord::RecoveredFill { .. }))
        {
            return;
        }
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
}

/// One buy, then a stream reset with the venue's history carrying that order's
/// fill. Returns what the risk kernel was told, and what the log holds.
async fn recover_one_fill() -> (Rc<RefCell<Vec<OrderUpdate>>>, Rc<RefCell<Vec<WalRecord>>>) {
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build(allow_all(), vec![Box::new(buyer)], &["BTCUSDT"], &[]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();

    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let sent = h.sends.borrow()[0].client_order_id.clone();
    h.risk_saw.borrow_mut().clear();

    // The venue traded it while the stream was away.
    *h.executions.borrow_mut() = Some(vec![VenueExecution {
        exec_id: "e-1".into(),
        client_order_id: sent,
        symbol: "BTCUSDT".into(),
        side: Side::Buy,
        qty: 0.01,
        px: 30_000.0,
        fee: 0.18,
        is_maker: false,
        venue_ts_ms: clock::wall_ms(),
    }]);
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset { recv_ns: 1 }]),
            until_recovered(h.records.clone()),
        )
        .await
        .unwrap();
    (h.risk_saw.clone(), h.records.clone())
}

#[tokio::test]
async fn a_recovered_fill_reaches_the_risk_kernel() {
    // The kernel reserved this order's size when it approved it, and only a
    // fill releases the reservation. A recovered fill that stopped at the log
    // would leave the position counted twice for the life of the process —
    // once as a reservation nothing ever ends, once in the account view — and
    // every later entry would be judged against the sum.
    let (risk_saw, records) = recover_one_fill().await;

    assert!(
        records
            .borrow()
            .iter()
            .any(|r| matches!(r, WalRecord::RecoveredFill { .. })),
        "the recovery itself has to have happened for this test to say anything"
    );
    let fills: Vec<f64> = risk_saw
        .borrow()
        .iter()
        .filter_map(|u| match u {
            OrderUpdate::Fill { qty, .. } => Some(*qty),
            _ => None,
        })
        .collect();
    assert_eq!(fills, vec![0.01], "the kernel was not told what filled");
}
