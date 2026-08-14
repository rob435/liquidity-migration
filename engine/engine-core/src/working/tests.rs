//! The bookkeeping around the decision: which orders are still worth working,
//! and what the venue's answers do to their state.

use super::*;
use engine_types::{
    OrderKind, OrderRequest, OrderUpdate, Side, StrategyId, TimeInForce, WalRecord,
};

const RULE: InstrumentRule = InstrumentRule {
    tick_size: 0.5,
    qty_step: 0.001,
    min_qty: 0.001,
    min_notional: 5.0,
};

const SECOND: u64 = 1_000_000_000;
const SYMBOL: SymbolId = SymbolId(0);

fn rules() -> Vec<Option<InstrumentRule>> {
    vec![Some(RULE)]
}

/// A market picture holding one book.
fn market(bid_px: f64, ask_px: f64) -> MarketState {
    let mut market = MarketState::default();
    let id = market.add_symbol("BTCUSDT");
    market.apply(&engine_types::MarketEvent::Quote {
        symbol: id,
        quote: Quote {
            bid_px,
            ask_px,
            ..Quote::default()
        },
    });
    market
}

fn sent(id: &str) -> WalRecord {
    WalRecord::OrderSent {
        request: OrderRequest {
            client_order_id: id.into(),
            strategy: StrategyId(0),
            symbol: SYMBOL,
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Limit {
                px: 99.0,
                tif: TimeInForce::Gtc,
            },
            stop: None,
            reduce_only: false,
        },
        wire_ns: 1,
    }
}

/// A supervisor already working one buy resting at 99.
fn working_one() -> (WorkingOrders, LedgerOfOrders) {
    let mut working = WorkingOrders::default();
    let state = plan::WorkState::new(Side::Buy, 99.0, 0.0, 0);
    working.take_on("a", SYMBOL, WorkPolicy::default(), state);
    (working, LedgerOfOrders::from_records(&[sent("a")]))
}

fn one_pass(
    working: &mut WorkingOrders,
    ledger: &LedgerOfOrders,
    market: &MarketState,
    now_ns: u64,
) -> Vec<Action> {
    let mut out = VecDeque::new();
    working.pass(now_ns, market, &rules(), ledger, &mut out);
    out.into_iter().collect()
}

#[test]
fn an_order_the_log_has_ended_stops_being_worked() {
    let (mut working, _) = working_one();
    let ledger = LedgerOfOrders::from_records(&[
        sent("a"),
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                client_order_id: "a".into(),
                symbol: SYMBOL,
                side: Side::Buy,
                qty: 1.0,
                px: 99.0,
                fee: 0.0,
                venue_ts_ms: 0,
                recv_ns: 0,
            },
        },
    ]);
    let market = market(100.0, 102.0);
    assert!(one_pass(&mut working, &ledger, &market, 4 * SECOND).is_empty());
    assert!(working.is_empty(), "a filled order is not something to reprice");
}

#[test]
fn an_order_the_log_never_heard_of_is_dropped() {
    let (mut working, _) = working_one();
    let empty = LedgerOfOrders::default();
    let market = market(100.0, 102.0);
    assert!(one_pass(&mut working, &empty, &market, 4 * SECOND).is_empty());
    assert!(working.is_empty());
}

#[test]
fn a_move_reaches_the_queue_as_a_price_only_amend() {
    // Price only. An amend that raised the size would have to be made durable
    // before the wire, and that fsync would land on every reprice.
    let (mut working, ledger) = working_one();
    let market = market(100.0, 102.0);
    let out = one_pass(&mut working, &ledger, &market, 4 * SECOND);
    assert_eq!(
        out,
        vec![Action::Amend {
            symbol: SYMBOL,
            client_order_id: "a".into(),
            spec: AmendSpec { px: Some(100.0), qty: None },
        }]
    );
}

#[test]
fn a_move_the_venue_took_counts_against_the_budget_and_moves_the_price() {
    let (mut working, ledger) = working_one();
    let market = market(100.0, 102.0);
    one_pass(&mut working, &ledger, &market, 4 * SECOND);
    working.amended("a", Some(100.0), true, 4 * SECOND);

    // At the new price there is nothing left to do about this book.
    let out = one_pass(&mut working, &ledger, &market, 8 * SECOND);
    assert!(out.is_empty(), "it is already where it belongs");
}

#[test]
fn a_move_the_venue_refused_leaves_the_order_where_it_was() {
    let (mut working, ledger) = working_one();
    let market = market(100.0, 102.0);
    one_pass(&mut working, &ledger, &market, 4 * SECOND);
    working.amended("a", Some(100.0), false, 4 * SECOND);

    // Still at 99, so the next look asks for the same move again.
    let out = one_pass(&mut working, &ledger, &market, 8 * SECOND);
    assert_eq!(
        out,
        vec![Action::Amend {
            symbol: SYMBOL,
            client_order_id: "a".into(),
            spec: AmendSpec { px: Some(100.0), qty: None },
        }]
    );
}

#[test]
fn a_failed_cancel_does_not_latch() {
    // This is the only cancel in the working path. A failure that latched
    // would leave a marketable limit resting at the venue with nothing left
    // to take it down.
    let (mut working, ledger) = working_one();
    let market = market(99.0, 101.0);
    let policy = WorkPolicy::default();
    let window_over = policy.window_ms * 1_000_000;
    let grace_over = window_over + policy.cross_grace_ms * 1_000_000;

    // Window end: cross, and the venue takes it.
    let out = one_pass(&mut working, &ledger, &market, window_over);
    assert!(matches!(out[0], Action::Amend { .. }));
    working.amended("a", Some(102.0), true, window_over);

    // Grace end: the cancel goes out and the venue refuses it.
    let out = one_pass(&mut working, &ledger, &market, window_over + grace_over);
    assert_eq!(
        out,
        vec![Action::Cancel { symbol: SYMBOL, client_order_id: "a".into() }]
    );
    working.cancelled("a", false);

    // It must come round again, on the retry pacing.
    let later = window_over + grace_over + 3 * SECOND;
    assert_eq!(
        one_pass(&mut working, &ledger, &market, later),
        vec![Action::Cancel { symbol: SYMBOL, client_order_id: "a".into() }],
        "a refused cancel has to be asked for again"
    );
}

#[test]
fn a_cancel_the_venue_took_is_not_asked_for_twice() {
    let (mut working, ledger) = working_one();
    let market = market(99.0, 101.0);
    let policy = WorkPolicy::default();
    let window_over = policy.window_ms * 1_000_000;
    let grace_over = window_over + policy.cross_grace_ms * 1_000_000;

    one_pass(&mut working, &ledger, &market, window_over);
    working.amended("a", Some(102.0), true, window_over);
    one_pass(&mut working, &ledger, &market, window_over + grace_over);
    working.cancelled("a", true);

    let later = window_over + grace_over + 10 * SECOND;
    assert!(one_pass(&mut working, &ledger, &market, later).is_empty());
}

#[test]
fn a_cross_the_venue_refused_is_retried_and_does_not_count_as_crossed() {
    let (mut working, ledger) = working_one();
    let market = market(99.0, 101.0);
    let window_over = WorkPolicy::default().window_ms * 1_000_000;

    one_pass(&mut working, &ledger, &market, window_over);
    working.amended("a", Some(102.0), false, window_over);

    // Paced, then tried again — still a cross, not a reprice.
    assert!(one_pass(&mut working, &ledger, &market, window_over + SECOND).is_empty());
    let retry = one_pass(&mut working, &ledger, &market, window_over + 3 * SECOND);
    assert_eq!(
        retry,
        vec![Action::Amend {
            symbol: SYMBOL,
            client_order_id: "a".into(),
            spec: AmendSpec { px: Some(102.0), qty: None },
        }]
    );
}

#[test]
fn a_dark_book_at_the_window_end_starts_the_grace_clock_without_an_action() {
    // Nothing can be priced, but the cancel that bounds the order still needs
    // its deadline running.
    let (mut working, ledger) = working_one();
    let dark = MarketState::default();
    let policy = WorkPolicy::default();
    let window_over = policy.window_ms * 1_000_000;
    assert!(one_pass(&mut working, &ledger, &dark, window_over).is_empty());

    // The grace expires on schedule and the order is pulled.
    let grace_over = window_over + policy.cross_grace_ms * 1_000_000;
    assert_eq!(
        one_pass(&mut working, &ledger, &dark, grace_over),
        vec![Action::Cancel { symbol: SYMBOL, client_order_id: "a".into() }]
    );
}

#[test]
fn an_answer_about_an_order_nobody_is_working_changes_nothing() {
    let (mut working, _) = working_one();
    working.amended("someone-elses", Some(5.0), true, SECOND);
    working.cancelled("someone-elses", true);
    assert_eq!(working.len(), 1);
}
