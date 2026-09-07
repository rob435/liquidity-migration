//! Opening orders an account-level halt pulls when the venue's cancel reply
//! is not the confirmation: refused, lost, or accepted and never followed by
//! the private stream. The order's status is read at the venue instead of
//! ending the run; the run ends only when that read cannot settle it either.

use super::*;
use engine_types::numeric::ExactNumber;
use engine_types::orders::{OrderLookup, OrderLookupRow, TerminalOrderStatus};

fn row(id: &str, filled: &str) -> OrderLookupRow {
    OrderLookupRow {
        symbol: "BTCUSDT".into(),
        client_order_id: id.into(),
        venue_order_id: format!("v-{id}"),
        filled_qty: ExactNumber::venue_decimal(filled).expect("a decimal"),
    }
}

/// One opening order sent and acknowledged, so the private-stream reset of
/// the next run halts it.
async fn one_working_opening() -> (
    Engine<MockWal, MockRisk, MockVenue>,
    Harness,
    String,
    SymbolId,
) {
    let _io = crate::test_io::IoProgress::new();
    let (buyer, _heard) = Buyer::new("BTCUSDT", 1, 0.01);
    let (mut engine, h) = build_with_lookups(vec![Box::new(buyer)], &["BTCUSDT"]).await;
    let symbol = engine.market().table.get("BTCUSDT").unwrap();
    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 1, true),
            &mut ScriptOrderFeed::empty(),
            std::future::pending::<()>(),
        )
        .await
        .unwrap();
    let id = h.sends.lock().unwrap()[0].client_order_id.clone();
    assert_eq!(engine.in_flight_ids().len(), 1, "the entry is working");
    (engine, h, id, symbol)
}

/// Resolves once the log records `id` ending, or after five seconds.
async fn until_ended(records: Rc<RefCell<Vec<WalRecord>>>, id: String) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while tokio::time::Instant::now() < deadline {
        let ended = records.lock().unwrap().iter().any(|record| {
            matches!(
                record,
                WalRecord::OrderUpdate {
                    update: OrderUpdate::Cancelled { client_order_id, .. },
                    ..
                } if *client_order_id == id
            )
        });
        if ended {
            return;
        }
        tokio::time::advance(Duration::from_millis(1)).await;
    }
}

fn reset() -> ScriptOrderFeed {
    ScriptOrderFeed::playing(vec![OrderUpdate::StreamReset { recv_ns: 1 }])
}

#[tokio::test(start_paused = true)]
async fn a_halt_cancel_the_venue_refuses_as_not_working_is_settled_by_a_status_read() {
    // Bybit answers a cancel of an order it no longer works with 110001. The
    // order ended; only its private update is missing. That is a question for
    // the venue, not a reason to exit and let boot ask it.
    let (mut engine, h, id, symbol) = one_working_opening().await;
    h.cancel_replies
        .lock()
        .unwrap()
        .push_back(Err(VenueError::Rejected {
            code: 110001,
            message: "order does not exist".into(),
        }));
    h.lookups.lock().unwrap().push_back(OrderLookup::Terminal {
        status: TerminalOrderStatus::Cancelled,
        row: row(&id, "0"),
    });

    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut reset(),
            until_ended(h.records.clone(), id.clone()),
        )
        .await
        .expect("the halt settles the order instead of ending the run");

    assert_eq!(h.cancels.lock().unwrap().len(), 1, "one cancel, refused");
    assert!(engine.in_flight_ids().is_empty(), "the ending is recorded");
    assert!(note_saying(&h.records, "reading the order's status").contains(&id));
    assert!(note_saying(&h.records, "ended at the venue (Cancelled)").contains(&id));
}

/// The private stream's reset, then the order's ending once the venue has
/// heard `after` cancels.
struct EndingAfterCancels {
    cancels: Rc<RefCell<Vec<(SymbolId, String)>>>,
    after: usize,
    id: String,
    stage: u8,
}

impl OrderFeed for EndingAfterCancels {
    fn learn(&mut self, _symbol: &str, _id: SymbolId) {}

    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        match self.stage {
            0 => {
                self.stage = 1;
                Ok(OrderUpdate::StreamReset { recv_ns: 1 })
            }
            1 => {
                while self.cancels.lock().unwrap().len() < self.after {
                    tokio::time::sleep(Duration::from_millis(1)).await;
                }
                self.stage = 2;
                Ok(OrderUpdate::Cancelled {
                    client_order_id: self.id.clone(),
                    recv_ns: clock::now_ns(),
                })
            }
            _ => std::future::pending().await,
        }
    }
}

#[tokio::test(start_paused = true)]
async fn a_halt_cancel_the_venue_refuses_for_a_working_order_is_sent_again() {
    let (mut engine, h, id, symbol) = one_working_opening().await;
    h.cancel_replies
        .lock()
        .unwrap()
        .push_back(Err(VenueError::Rejected {
            code: 10001,
            message: "params error".into(),
        }));
    h.lookups
        .lock()
        .unwrap()
        .push_back(OrderLookup::Working(row(&id, "0")));

    engine
        .run(
            &mut ScriptFeed::quotes(symbol, 0, false),
            &mut EndingAfterCancels {
                cancels: h.cancels.clone(),
                after: 2,
                id: id.clone(),
                stage: 0,
            },
            until_ended(h.records.clone(), id.clone()),
        )
        .await
        .expect("the second cancel is confirmed by the private stream");

    assert_eq!(
        h.cancels.lock().unwrap().len(),
        2,
        "the refused cancel is sent again once the venue says the order still works"
    );
    assert!(engine.in_flight_ids().is_empty());
    assert!(note_saying(&h.records, "still working at the venue").contains(&id));
}

#[tokio::test(start_paused = true)]
async fn an_accepted_halt_cancel_the_private_stream_never_confirms_is_settled_by_a_status_read() {
    crate::test_clock::with_engine_clock(async {
        // The cancel was taken and the private update carrying the ending was
        // dropped. Half the confirmation window of silence is the cue to ask.
        let (mut engine, h, id, symbol) = one_working_opening().await;
        h.lookups.lock().unwrap().push_back(OrderLookup::Terminal {
            status: TerminalOrderStatus::Cancelled,
            row: row(&id, "0"),
        });

        engine
            .run(
                &mut ScriptFeed::quotes(symbol, 0, false),
                &mut reset(),
                until_ended(h.records.clone(), id.clone()),
            )
            .await
            .expect("the status read stands in for the dropped update");

        assert_eq!(h.cancels.lock().unwrap().len(), 1, "one cancel, accepted");
        assert!(engine.in_flight_ids().is_empty());
        assert!(
            note_saying(&h.records, "has not confirmed the accepted halt cancel").contains(&id)
        );
    })
    .await;
}

#[tokio::test(start_paused = true)]
async fn a_halt_cancel_the_venue_cannot_settle_still_ends_the_run() {
    crate::test_clock::with_engine_clock(async {
        // No script: every status read comes back unknown. The deadline set by
        // the first cancel reply still ends the run for boot to reconcile.
        let (mut engine, h, id, symbol) = one_working_opening().await;
        h.cancel_replies
            .lock()
            .unwrap()
            .push_back(Err(VenueError::Transport("reply lost".into())));

        let error = engine
            .run(
                &mut ScriptFeed::quotes(symbol, 0, false),
                &mut reset(),
                async {
                    for _ in 0..5_000 {
                        tokio::time::advance(Duration::from_millis(1)).await;
                    }
                },
            )
            .await
            .expect_err("an order the venue cannot settle is boot's to reconcile");

        assert!(
            matches!(error, EngineError::Reconcile(ref detail) if detail.contains(&id) && detail.contains("refused or unanswered")),
            "{error}"
        );
        assert!(note_saying(&h.records, "asking again").contains(&id));
    }).await;
}
