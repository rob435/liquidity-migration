//! The replay, tested for the two things it exists to guarantee — order and
//! determinism — and for the venue physics the report's numbers rest on.

use std::future::Future;
use std::io::Write;
use std::pin::pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::Duration;

use engine_types::{
    BookLevel, Feed, ForcedClose, InstrumentRule, OrderKind, OrderRequest, OrderUpdate, Side,
    StopSpec, StrategyId, Subscription, SymbolId, TimeInForce, VenueError, VenueGateway,
};

use super::feed::Cursor;
use super::scheduler::{Scheduler, VirtualTimer, WaiterKind};
use super::tape::{
    read_instruments, BookBuilder, BookRow, TapeError, TapeReader, TapeRow, TickerRow,
};
use super::venue::{SimulatedVenue, VenueParams};
use crate::engine::{LoopInterval, LoopTimer};
use crate::testpath::temp_path;

fn poll_once<F: Future>(future: &mut std::pin::Pin<&mut F>) -> Poll<F::Output> {
    let waker = std::task::Waker::noop();
    let mut cx = Context::from_waker(waker);
    future.as_mut().poll(&mut cx)
}

// ------------------------------------------------------------ scheduler

#[test]
fn waiters_fire_in_deadline_order_and_hold_time_until_consumed() {
    let scheduler = Scheduler::starting_at(0);
    scheduler.open();
    let mut early = pin!(scheduler.sleep_until(10, WaiterKind::Timer));
    let mut late = pin!(scheduler.sleep_until(20, WaiterKind::Venue));
    assert!(poll_once(&mut early).is_pending());
    assert!(poll_once(&mut late).is_pending());
    assert_eq!(
        scheduler.earliest_pending(&[WaiterKind::Timer, WaiterKind::Venue]),
        Some(10)
    );
    assert_eq!(scheduler.earliest_pending(&[WaiterKind::Venue]), Some(20));

    scheduler.advance_to(25);
    assert_eq!(scheduler.now_ns(), 25);
    assert!(scheduler.has_fired_unconsumed());
    assert!(poll_once(&mut early).is_ready());
    assert!(
        scheduler.has_fired_unconsumed(),
        "the second waiter has not run yet"
    );
    assert!(poll_once(&mut late).is_ready());
    assert!(!scheduler.has_fired_unconsumed());
    assert_eq!(
        scheduler.earliest_pending(&[WaiterKind::Timer, WaiterKind::Venue]),
        None
    );
}

#[test]
fn a_dropped_sleep_leaves_no_trace_pending_or_fired() {
    let scheduler = Scheduler::starting_at(0);
    scheduler.open();
    {
        let mut sleep = pin!(scheduler.sleep_until(10, WaiterKind::Timer));
        assert!(poll_once(&mut sleep).is_pending());
    }
    assert_eq!(scheduler.earliest_pending(&[WaiterKind::Timer]), None);
    let dropped_after_firing = scheduler.sleep_until(10, WaiterKind::Timer);
    scheduler.advance_to(10);
    assert!(scheduler.has_fired_unconsumed());
    drop(dropped_after_firing);
    assert!(
        !scheduler.has_fired_unconsumed(),
        "a lost select! branch must not freeze time"
    );
}

#[test]
fn the_clock_never_moves_backwards() {
    let scheduler = Scheduler::starting_at(100);
    scheduler.advance_to(50);
    assert_eq!(scheduler.now_ns(), 100);
}

#[tokio::test]
async fn the_interval_ticks_at_once_then_one_period_after_each_tick() {
    let scheduler = Scheduler::starting_at(0);
    scheduler.open();
    let timer = VirtualTimer::new(scheduler.clone());
    let mut interval = timer.interval(Duration::from_nanos(250));
    {
        let mut first = pin!(interval.tick());
        assert!(
            poll_once(&mut first).is_ready(),
            "the first tick is due at once"
        );
    }
    {
        let mut second = pin!(interval.tick());
        assert!(poll_once(&mut second).is_pending());
        assert_eq!(scheduler.earliest_pending(&[WaiterKind::Timer]), Some(250));
        // Fired late: the clock jumped past the deadline.
        scheduler.advance_to(900);
        assert!(poll_once(&mut second).is_ready());
    }
    let mut third = pin!(interval.tick());
    assert!(poll_once(&mut third).is_pending());
    assert_eq!(
        scheduler.earliest_pending(&[WaiterKind::Timer]),
        Some(1150),
        "Delay semantics: a full period after the late tick, not after the missed deadline"
    );
}

#[test]
fn before_the_tape_pumps_a_wait_is_free_and_moves_nothing() {
    let scheduler = Scheduler::starting_at(0);
    let mut sleep = pin!(scheduler.sleep_until(500, WaiterKind::Venue));
    assert!(
        poll_once(&mut sleep).is_ready(),
        "boot's venue reads cannot wait on a clock nobody pumps"
    );
    assert_eq!(scheduler.now_ns(), 0);
}

#[test]
fn once_closed_a_wait_completes_by_moving_the_clock_to_its_deadline() {
    let scheduler = Scheduler::starting_at(0);
    scheduler.open();
    scheduler.close();
    let mut sleep = pin!(scheduler.sleep_until(500, WaiterKind::Venue));
    assert!(poll_once(&mut sleep).is_ready());
    assert_eq!(scheduler.now_ns(), 500);
}

// ------------------------------------------------------------- the book

fn book_row(snapshot: bool, update_id: u64, bids: &[(f64, f64)], asks: &[(f64, f64)]) -> BookRow {
    BookRow {
        symbol: "BTCUSDT".into(),
        snapshot,
        depth: 50,
        recv_ns: 1_000,
        exchange_ts_ns: 900,
        bids: bids
            .iter()
            .map(|(px, qty)| BookLevel { px: *px, qty: *qty })
            .collect(),
        asks: asks
            .iter()
            .map(|(px, qty)| BookLevel { px: *px, qty: *qty })
            .collect(),
        update_id,
        cross_sequence: 0,
        sequence_gap: false,
    }
}

fn levels(side: &[BookLevel], len: u8) -> Vec<(f64, f64)> {
    side[..len as usize].iter().map(|l| (l.px, l.qty)).collect()
}

/// The live feed's own fixture (`engine_marketdata::bybit::state` tests),
/// so the replay's book is the live book.
#[test]
fn snapshot_then_delta_matches_the_live_feed_semantics() {
    let mut builder = BookBuilder::default();
    let snapshot = book_row(
        true,
        100,
        &[(100.0, 1.0), (99.0, 2.0), (98.0, 3.0)],
        &[(101.0, 4.0), (102.0, 5.0)],
    );
    assert!(builder.apply(&snapshot).is_some());
    let delta = book_row(
        false,
        101,
        &[(100.0, 0.0), (99.0, 7.0), (99.5, 6.0)],
        &[(100.5, 8.0)],
    );
    let depth = *builder
        .apply(&delta)
        .expect("a chained delta keeps the book good");
    assert_eq!(
        levels(&depth.bids, depth.bid_len),
        vec![(99.5, 6.0), (99.0, 7.0), (98.0, 3.0)]
    );
    assert_eq!(
        levels(&depth.asks, depth.ask_len),
        vec![(100.5, 8.0), (101.0, 4.0), (102.0, 5.0)]
    );
    assert_eq!(depth.update_id, 101);
    assert_eq!(depth.quote().bid_px, 99.5);
    assert_eq!(depth.quote().ask_px, 100.5);
}

#[test]
fn a_gap_makes_the_book_a_guess_until_the_next_snapshot() {
    let mut builder = BookBuilder::default();
    assert!(
        builder
            .apply(&book_row(false, 5, &[(1.0, 1.0)], &[(2.0, 1.0)]))
            .is_none(),
        "no snapshot yet"
    );
    assert!(builder
        .apply(&book_row(true, 10, &[(1.0, 1.0)], &[(2.0, 1.0)]))
        .is_some());
    let mut gapped = book_row(false, 12, &[(1.5, 1.0)], &[]);
    gapped.sequence_gap = true;
    assert!(builder.apply(&gapped).is_none());
    assert!(!builder.is_valid());
    assert!(
        builder
            .apply(&book_row(false, 13, &[(1.6, 1.0)], &[]))
            .is_none(),
        "still bad"
    );
    assert!(builder
        .apply(&book_row(true, 20, &[(1.7, 1.0)], &[(2.0, 1.0)]))
        .is_some());
    assert!(
        builder
            .apply(&book_row(false, 20, &[(1.8, 1.0)], &[]))
            .is_none(),
        "a repeated id is a gap"
    );
}

// ------------------------------------------------------------- the tape

/// Rows exactly as `market_tape/schema.py`'s constructors write them.
fn recorder_rows() -> String {
    [
        r#"{"asks":[["50005.0","10"]],"bids":[["50000.0","10"]],"cross_sequence":0,"depth":50,"exchange_engine_ts_ns":1700000000000000000,"exchange_system_ts_ns":1700000000000000000,"first_update_id":0,"kind":"orderbook_snapshot","local_receive_ts_ns":1700000000000000000,"previous_update_id":0,"restart_snapshot":false,"sequence_gap":false,"symbol":"BTCUSDT","update_id":1,"venue":"bybit-linear"}"#,
        r#"{"kind":"ticker","local_receive_ts_ns":1700000000100000000,"exchange_system_ts_ns":1700000000100000000,"message_type":"delta","cross_sequence":0,"symbol":"BTCUSDT","values":{"mark_price":"50002.5","funding_rate":"0.0001","next_funding_time_ms":1700028800000,"last_price":50002.0},"venue":"bybit-linear"}"#,
        r#"{"exchange_ts_ns":1700000000200000000,"kind":"public_trade","local_receive_ts_ns":1700000000200000000,"price":50002.0,"qty":0.5,"side":"Buy","symbol":"BTCUSDT","trade_id":"1","venue":"bybit-linear"}"#,
        r#"{"kind":"kline","local_receive_ts_ns":1700000000300000000,"symbol":"BTCUSDT","interval":"1"}"#,
        r#"{"asks":[["50006.0","3"]],"bids":[["50000.0","0"],["49999.0","4"]],"cross_sequence":0,"depth":50,"exchange_engine_ts_ns":0,"exchange_system_ts_ns":1700000000400000000,"first_update_id":0,"kind":"orderbook_delta","local_receive_ts_ns":1700000000400000000,"previous_update_id":1,"restart_snapshot":false,"sequence_gap":false,"symbol":"BTCUSDT","update_id":2,"venue":"bybit-linear"}"#,
    ]
    .join("\n")
        + "\n"
}

fn write_temp(tag: &str, text: &str) -> crate::testpath::TempPath {
    let path = temp_path(tag);
    std::fs::File::create(path.path())
        .unwrap()
        .write_all(text.as_bytes())
        .unwrap();
    path
}

#[test]
fn reads_rows_in_the_recorder_contract() {
    let path = write_temp("tape-rows", &recorder_rows());
    let mut reader = TapeReader::open(path.path()).unwrap();
    let (t, row) = reader.next_row().unwrap().unwrap();
    assert_eq!(t, 1_700_000_000_000_000_000);
    let TapeRow::Book(book) = row else {
        panic!("a snapshot")
    };
    assert!(book.snapshot);
    assert_eq!(
        book.bids,
        vec![BookLevel {
            px: 50_000.0,
            qty: 10.0
        }]
    );
    assert_eq!(
        book.asks,
        vec![BookLevel {
            px: 50_005.0,
            qty: 10.0
        }]
    );

    let (_, row) = reader.next_row().unwrap().unwrap();
    let TapeRow::Ticker(ticker) = row else {
        panic!("a ticker")
    };
    assert_eq!(ticker.mark_price, Some(50_002.5));
    assert_eq!(ticker.funding_rate, Some(0.0001));
    assert_eq!(ticker.next_funding_time_ms, Some(1_700_028_800_000));
    assert_eq!(
        ticker.index_price, None,
        "absent means unchanged, never zero"
    );

    let (_, row) = reader.next_row().unwrap().unwrap();
    let TapeRow::Trade(trade) = row else {
        panic!("a trade")
    };
    assert!(trade.buyer_aggressor);
    assert_eq!((trade.price, trade.qty), (50_002.0, 0.5));

    let (_, row) = reader.next_row().unwrap().unwrap();
    let TapeRow::Book(delta) = row else {
        panic!("the kline is skipped, the delta comes next")
    };
    assert!(!delta.snapshot);
    assert_eq!(delta.update_id, 2);
    assert!(reader.next_row().unwrap().is_none());
    assert_eq!(reader.stats.rows, 4);
    assert_eq!(reader.stats.skipped_by_kind.get("kline"), Some(&1));
}

#[test]
fn a_malformed_row_stops_the_tape_with_its_line_number() {
    let text = format!(
        "{}\n{}\n",
        recorder_rows().lines().next().unwrap(),
        r#"{"kind":"orderbook_delta","local_receive_ts_ns":1700000000500000000,"symbol":"BTCUSDT","bids":"not a list","update_id":3,"venue":"bybit"}"#
    );
    let path = write_temp("tape-bad", &text);
    let mut reader = TapeReader::open(path.path()).unwrap();
    reader.next_row().unwrap();
    match reader.next_row() {
        Err(TapeError::Malformed { line: 2, detail }) => {
            assert!(detail.contains("bids"), "{detail}")
        }
        other => panic!("expected a malformed-row error, got {other:?}"),
    }
}

/// Binance brackets each book diff with `first_update_id`/`pu`; this reader
/// chains by Bybit's monotone `update_id`. Reading one as the other builds a
/// plausible book that is not the venue's, so the row is refused instead.
#[test]
fn a_book_row_from_another_venue_is_refused_rather_than_chained() {
    let binance = r#"{"asks":[["50005.0","10"]],"bids":[["50000.0","10"]],"cross_sequence":0,"depth":1000,"exchange_engine_ts_ns":0,"exchange_system_ts_ns":1700000000000000000,"first_update_id":90,"kind":"orderbook_delta","local_receive_ts_ns":1700000000000000000,"previous_update_id":89,"restart_snapshot":false,"sequence_gap":false,"symbol":"BTCUSDT","update_id":100,"venue":"binance"}"#;
    let path = write_temp("tape-foreign", &format!("{binance}\n"));
    let mut reader = TapeReader::open(path.path()).unwrap();
    let error = reader.next_row().expect_err("refused");
    assert!(
        matches!(&error, TapeError::UnsupportedVenue { line: 1, venue } if venue == "binance"),
        "{error}"
    );
    // A trade or ticker carries no chaining, so those are read from any venue.
    let trade = r#"{"exchange_ts_ns":1700000000200000000,"kind":"public_trade","local_receive_ts_ns":1700000000200000000,"price":50002.0,"qty":0.5,"side":"Buy","symbol":"BTCUSDT","trade_id":"1","venue":"binance"}"#;
    let path = write_temp("tape-foreign-trade", &format!("{trade}\n"));
    let mut reader = TapeReader::open(path.path()).unwrap();
    assert!(matches!(
        reader.next_row().unwrap(),
        Some((_, TapeRow::Trade(_)))
    ));
}

#[test]
fn a_row_received_before_the_previous_one_is_refused() {
    let lines: Vec<&str> = recorder_rows()
        .lines()
        .map(str::to_owned)
        .collect::<Vec<_>>()
        .leak()
        .iter()
        .map(String::as_str)
        .collect();
    let text = format!("{}\n{}\n", lines[1], lines[0]);
    let path = write_temp("tape-order", &text);
    let mut reader = TapeReader::open(path.path()).unwrap();
    reader.next_row().unwrap();
    assert!(matches!(
        reader.next_row(),
        Err(TapeError::OutOfOrder { line: 2, .. })
    ));
}

#[test]
fn instruments_are_read_with_the_gateways_four_fields() {
    let payload = r#"{"kind":"instruments_snapshot","venue":"bybit","market":"linear","category":"linear","schema":2,"recorded_at_ns":1,"source":"test","rows":[
        {"symbol":"BTCUSDT","priceFilter":{"minPrice":"0.10","maxPrice":"1999999.80","tickSize":"0.10"},"lotSizeFilter":{"minOrderQty":"0.001","qtyStep":"0.001","minNotionalValue":"5"}},
        {"symbol":"BROKENUSDT","priceFilter":{"tickSize":"0"},"lotSizeFilter":{"minOrderQty":"1","qtyStep":"1"}},
        {"symbol":"NOFILTERUSDT"}
    ]}"#;
    let path = write_temp("instruments", payload);
    let rules = read_instruments(path.path()).unwrap();
    assert_eq!(
        rules,
        vec![(
            "BTCUSDT".to_string(),
            InstrumentRule {
                tick_size: 0.1,
                qty_step: 0.001,
                min_qty: 0.001,
                min_notional: 5.0
            }
        )]
    );
}

// ------------------------------------------------------------ the venue

const RULE: InstrumentRule = InstrumentRule {
    tick_size: 0.5,
    qty_step: 0.001,
    min_qty: 0.001,
    min_notional: 5.0,
};

fn venue(cash: f64, leverage: f64) -> (SimulatedVenue, Scheduler) {
    let scheduler = Scheduler::starting_at(1_000);
    scheduler.open();
    let venue = SimulatedVenue::new(
        VenueParams {
            initial_cash_usdt: cash,
            taker_fee_rate: 0.00055,
            maker_fee_rate: 0.0002,
            order_rtt_ns: 100,
            private_latency_ns: 50,
            default_leverage: leverage,
            maintenance_margin_rate: 0.005,
        },
        vec!["BTCUSDT".into()],
        &[("BTCUSDT".into(), RULE)],
        scheduler.clone(),
    );
    (venue, scheduler)
}

fn book(venue: &mut SimulatedVenue, bids: &[(f64, f64)], asks: &[(f64, f64)]) {
    let mut builder = BookBuilder::default();
    let depth = *builder
        .apply(&book_row(true, 1, bids, asks))
        .expect("both sides");
    venue.on_book(SymbolId(0), &depth);
}

fn order(id: &str, side: Side, qty: f64, kind: OrderKind) -> OrderRequest {
    OrderRequest {
        client_order_id: id.to_string(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side,
        qty,
        kind,
        stop: None,
        reduce_only: false,
        close_position: false,
    }
}

fn limit(px: f64) -> OrderKind {
    OrderKind::Limit {
        px,
        tif: TimeInForce::Gtc,
    }
}

fn ticker(mark: f64, funding_rate: Option<f64>, next_ms: Option<i64>) -> TickerRow {
    TickerRow {
        symbol: "BTCUSDT".into(),
        mark_price: Some(mark),
        funding_rate,
        next_funding_time_ms: next_ms,
        ..TickerRow::default()
    }
}

/// A range cut from the middle of a recording: the deep stream's deltas chain
/// to a snapshot that is not on the tape, while the top-of-book stream is a
/// snapshot every row. The venue matches against the shallow book until a
/// deep snapshot lands, and against the deep one from then on.
#[test]
fn the_venue_matches_against_the_deepest_book_whose_chain_is_intact() {
    let rows = [
        // The deep stream mid-chain: nothing to chain to.
        r#"{"asks":[["50010.0","5"]],"bids":[["49990.0","5"]],"cross_sequence":0,"depth":50,"exchange_engine_ts_ns":0,"exchange_system_ts_ns":1700000000000000000,"first_update_id":0,"kind":"orderbook_delta","local_receive_ts_ns":1700000000000000000,"previous_update_id":6,"restart_snapshot":false,"sequence_gap":false,"symbol":"BTCUSDT","update_id":7,"venue":"bybit-linear"}"#,
        // The top of book: whole every row.
        r#"{"asks":[["50005.0","10"]],"bids":[["50000.0","10"]],"cross_sequence":0,"depth":1,"exchange_engine_ts_ns":0,"exchange_system_ts_ns":1700000000100000000,"first_update_id":0,"kind":"orderbook_snapshot","local_receive_ts_ns":1700000000100000000,"previous_update_id":0,"restart_snapshot":false,"sequence_gap":false,"symbol":"BTCUSDT","update_id":100,"venue":"bybit-linear"}"#,
        // The deep stream restarts: from here it is the venue's book.
        r#"{"asks":[["50020.0","5"],["50030.0","5"]],"bids":[["49980.0","5"]],"cross_sequence":0,"depth":50,"exchange_engine_ts_ns":0,"exchange_system_ts_ns":1700000000200000000,"first_update_id":0,"kind":"orderbook_snapshot","local_receive_ts_ns":1700000000200000000,"previous_update_id":0,"restart_snapshot":true,"sequence_gap":false,"symbol":"BTCUSDT","update_id":8,"venue":"bybit-linear"}"#,
        // A later top of book does not displace it.
        r#"{"asks":[["50006.0","10"]],"bids":[["50001.0","10"]],"cross_sequence":0,"depth":1,"exchange_engine_ts_ns":0,"exchange_system_ts_ns":1700000000300000000,"first_update_id":0,"kind":"orderbook_snapshot","local_receive_ts_ns":1700000000300000000,"previous_update_id":0,"restart_snapshot":false,"sequence_gap":false,"symbol":"BTCUSDT","update_id":101,"venue":"bybit-linear"}"#,
    ];
    let path = write_temp("tape-depths", &(rows.join("\n") + "\n"));
    let (venue, _scheduler) = venue(1_000_000.0, 10.0);
    let venue = Arc::new(Mutex::new(venue));
    let subscriptions = [Subscription {
        symbol: "BTCUSDT".into(),
        feed: Feed::Quote,
    }];
    let mut cursor = Cursor::new(
        TapeReader::open(path.path()).unwrap(),
        venue.clone(),
        &["BTCUSDT".to_string()],
        &subscriptions,
    );
    let mut absorb = || {
        cursor.next_row_at().unwrap().expect("a row");
        cursor.absorb_next();
    };
    let entry_after = |side: Side, qty: f64| -> f64 {
        let mut venue = venue.lock().unwrap();
        let id = format!("o{}", venue.private_pending());
        venue
            .submit(&order(&id, side, qty, OrderKind::Market))
            .unwrap();
        venue.account_view().positions[0].entry_px
    };

    absorb();
    assert!(
        venue
            .lock()
            .unwrap()
            .submit(&order("none", Side::Buy, 1.0, OrderKind::Market))
            .is_err(),
        "an unchained deep book is no book"
    );

    absorb();
    assert_eq!(
        entry_after(Side::Buy, 1.0),
        50_005.0,
        "the top of book fills"
    );

    absorb();
    assert_eq!(
        entry_after(Side::Sell, 2.0),
        49_980.0,
        "the deep book's bid, once it is chained"
    );

    absorb();
    assert_eq!(
        entry_after(Side::Sell, 1.0),
        49_980.0,
        "the deep book stays the venue's when a shallower snapshot lands"
    );
}

#[test]
fn a_market_order_walks_the_book_and_the_rest_is_cancelled() {
    let (mut venue, _) = venue(1_000_000.0, 10.0);
    book(&mut venue, &[(99.0, 1.0)], &[(100.0, 1.0), (101.0, 2.0)]);
    venue
        .submit(&order("m1", Side::Buy, 4.0, OrderKind::Market))
        .unwrap();
    let a = venue.accounting();
    assert_eq!(a.fills, 2, "one execution per level");
    assert_eq!(a.maker_fills, 0);
    let view = venue.account_view();
    let position = &view.positions[0];
    assert_eq!(position.qty, 3.0);
    assert!((position.entry_px - (100.0 + 202.0) / 3.0).abs() < 1e-9);
    let expected_fee = (100.0 * 1.0 + 101.0 * 2.0) * 0.00055;
    assert!((a.fees_paid_usdt - expected_fee).abs() < 1e-9);
    assert_eq!(
        venue.private_pending(),
        3,
        "two fills and the cancel of the unfilled remainder"
    );
    assert!(matches!(
        venue.debug_private()[2],
        OrderUpdate::Cancelled { .. }
    ));
}

#[test]
fn a_resting_limit_fills_only_after_the_displayed_queue_is_eaten() {
    let (mut venue, _) = venue(1_000_000.0, 10.0);
    book(&mut venue, &[(100.0, 5.0)], &[(100.5, 5.0)]);
    venue
        .submit(&order("l1", Side::Buy, 2.0, limit(100.0)))
        .unwrap();
    assert_eq!(venue.accounting().fills, 0, "rests behind five displayed");
    assert_eq!(venue.accounting().resting_orders, 1);
    venue.on_trade(SymbolId(0), 100.0, 3.0, false);
    assert_eq!(
        venue.accounting().fills,
        0,
        "three of the five ahead traded"
    );
    venue.on_trade(SymbolId(0), 100.5, 10.0, true);
    assert_eq!(
        venue.accounting().fills,
        0,
        "a print above our bid is not ours"
    );
    venue.on_trade(SymbolId(0), 100.0, 3.0, false);
    let a = venue.accounting();
    assert_eq!(a.fills, 1, "two more clear the queue, one fills us");
    assert_eq!(a.maker_fills, 1);
    assert_eq!(venue.account_view().positions[0].qty, 1.0);
    assert_eq!(a.resting_orders, 1, "one lot still working");
    venue.on_trade(SymbolId(0), 99.0, 5.0, false);
    let a = venue.accounting();
    assert_eq!(a.fills, 2);
    assert_eq!(a.resting_orders, 0);
    assert!(
        (a.fees_paid_usdt - 200.0 * 0.0002).abs() < 1e-9,
        "both maker"
    );
}

#[test]
fn the_opposite_touch_crossing_a_resting_order_fills_it_at_its_price() {
    let (mut venue, _) = venue(1_000_000.0, 10.0);
    book(&mut venue, &[(100.0, 5.0)], &[(100.5, 5.0)]);
    venue
        .submit(&order("l1", Side::Buy, 1.0, limit(100.0)))
        .unwrap();
    book(&mut venue, &[(99.0, 5.0)], &[(99.5, 5.0)]);
    let a = venue.accounting();
    assert_eq!((a.fills, a.maker_fills), (1, 1));
    assert_eq!(venue.account_view().positions[0].entry_px, 100.0);
}

#[test]
fn a_stop_triggers_on_the_mark_and_fills_through_the_gap() {
    let (mut venue, _) = venue(1_000_000.0, 10.0);
    book(&mut venue, &[(99.5, 5.0)], &[(100.0, 5.0)]);
    let mut entry = order("e", Side::Buy, 1.0, OrderKind::Market);
    entry.stop = Some(StopSpec { trigger_px: 95.0 });
    venue.submit(&entry).unwrap();
    venue.on_ticker(SymbolId(0), &ticker(96.0, None, None));
    assert_eq!(
        venue.accounting().stop_fills,
        0,
        "the mark is above the stop"
    );
    // The book gaps: the best bid is now far below the trigger.
    book(&mut venue, &[(90.0, 0.4), (85.0, 5.0)], &[(96.0, 1.0)]);
    venue.on_ticker(SymbolId(0), &ticker(94.0, None, None));
    let a = venue.accounting();
    assert_eq!(
        a.stop_fills, 2,
        "0.4 at 90 and 0.6 at 85, not 1.0 at the trigger"
    );
    let expected = (90.0 - 100.0) * 0.4 + (85.0 - 100.0) * 0.6;
    assert!(
        (a.realized_pnl_usdt - expected).abs() < 1e-9,
        "{}",
        a.realized_pnl_usdt
    );
    assert!(venue.account_view().positions.is_empty());
}

#[test]
fn funding_settles_once_per_boundary_at_the_quoted_rate() {
    // One time base for the scheduler and the thread's clock, as the runner
    // installs them: the tape's receive stamp in nanoseconds since the epoch.
    let t0_ns: u64 = 1_700_000_000_000_000_000;
    let scheduler = Scheduler::starting_at(t0_ns);
    scheduler.open();
    let mut venue = SimulatedVenue::new(
        VenueParams {
            initial_cash_usdt: 1_000_000.0,
            taker_fee_rate: 0.00055,
            maker_fee_rate: 0.0002,
            order_rtt_ns: 100,
            private_latency_ns: 50,
            default_leverage: 10.0,
            maintenance_margin_rate: 0.005,
        },
        vec!["BTCUSDT".into()],
        &[("BTCUSDT".into(), RULE)],
        scheduler.clone(),
    );
    let _clock = engine_types::clock::install_virtual(t0_ns, t0_ns).unwrap();
    book(&mut venue, &[(99.5, 5.0)], &[(100.0, 5.0)]);
    venue
        .submit(&order("e", Side::Buy, 1.0, OrderKind::Market))
        .unwrap();
    let boundary_ms = 1_700_000_000_000 + 8 * 3_600_000;
    for _ in 0..100 {
        venue.on_ticker(SymbolId(0), &ticker(100.0, Some(0.0001), Some(boundary_ms)));
    }
    assert_eq!(
        venue.accounting().funding_settlements,
        0,
        "the rate is quoted, not charged"
    );
    assert_eq!(venue.accounting().funding_paid_usdt, 0.0);
    // The clock crosses the boundary.
    let after_ns = (boundary_ms as u64 + 1) * 1_000_000;
    scheduler.advance_to(after_ns);
    venue.on_ticker(
        SymbolId(0),
        &ticker(100.0, Some(0.0001), Some(boundary_ms + 8 * 3_600_000)),
    );
    let a = venue.accounting();
    assert_eq!(a.funding_settlements, 1);
    assert!(
        (a.funding_paid_usdt - 100.0 * 0.0001).abs() < 1e-12,
        "long pays 1 bp of 100"
    );
    for _ in 0..100 {
        venue.on_ticker(
            SymbolId(0),
            &ticker(100.0, Some(0.0001), Some(boundary_ms + 8 * 3_600_000)),
        );
    }
    assert_eq!(
        venue.accounting().funding_settlements,
        1,
        "one settlement per boundary"
    );
}

#[test]
fn a_reduce_only_order_cannot_open_or_exceed_the_position() {
    let (mut venue, _) = venue(1_000_000.0, 10.0);
    book(&mut venue, &[(99.5, 5.0)], &[(100.0, 5.0)]);
    let mut exit = order("x", Side::Sell, 1.0, OrderKind::Market);
    exit.reduce_only = true;
    assert!(matches!(
        venue.submit(&exit),
        Err(VenueError::Rejected { code: 110017, .. })
    ));
    venue
        .submit(&order("e", Side::Buy, 1.0, OrderKind::Market))
        .unwrap();
    let mut too_big = order("x2", Side::Sell, 1.5, OrderKind::Market);
    too_big.reduce_only = true;
    assert!(matches!(
        venue.submit(&too_big),
        Err(VenueError::Rejected { code: 110017, .. })
    ));
    let mut exact = order("x3", Side::Sell, 1.0, OrderKind::Market);
    exact.reduce_only = true;
    venue.submit(&exact).unwrap();
    assert!(venue.account_view().positions.is_empty());
    assert_eq!(venue.accounting().rejected_orders, 2);
}

#[test]
fn the_instrument_rules_are_enforced_before_anything_matches() {
    let (mut venue, _) = venue(1_000_000.0, 10.0);
    book(&mut venue, &[(99.5, 5.0)], &[(100.0, 5.0)]);
    assert!(
        matches!(
            venue.submit(&order("q", Side::Buy, 0.0015, OrderKind::Market)),
            Err(VenueError::Rejected { code: 10001, .. })
        ),
        "qty off the step"
    );
    assert!(
        matches!(
            venue.submit(&order("p", Side::Buy, 1.0, limit(99.7))),
            Err(VenueError::Rejected { code: 10001, .. })
        ),
        "price off the tick"
    );
    assert!(
        matches!(
            venue.submit(&order("n", Side::Buy, 0.01, OrderKind::Market)),
            Err(VenueError::Rejected { code: 110094, .. })
        ),
        "one dollar of notional is under the five-dollar minimum"
    );
    let (mut bare, _) = venue_without_rules();
    book(&mut bare, &[(99.5, 5.0)], &[(100.0, 5.0)]);
    assert!(
        matches!(
            bare.submit(&order("r", Side::Buy, 1.0, OrderKind::Market)),
            Err(VenueError::Rejected { code: 10001, .. })
        ),
        "no rule, no trade — never an invented one"
    );
    assert_eq!(venue.accounting().fills, 0);
}

fn venue_without_rules() -> (SimulatedVenue, Scheduler) {
    let scheduler = Scheduler::starting_at(1_000);
    let venue = SimulatedVenue::new(
        VenueParams {
            initial_cash_usdt: 1_000.0,
            taker_fee_rate: 0.0,
            maker_fee_rate: 0.0,
            order_rtt_ns: 0,
            private_latency_ns: 0,
            default_leverage: 1.0,
            maintenance_margin_rate: 0.005,
        },
        vec!["BTCUSDT".into()],
        &[],
        scheduler.clone(),
    );
    (venue, scheduler)
}

#[test]
fn an_order_with_no_book_is_refused_rather_than_priced() {
    let (mut venue, _) = venue(1_000_000.0, 10.0);
    assert!(matches!(
        venue.submit(&order("m", Side::Buy, 1.0, OrderKind::Market)),
        Err(VenueError::Rejected { code: 10001, .. })
    ));
    assert!(venue.account_view().positions.is_empty());
}

#[test]
fn margin_is_committed_and_available_shrinks_with_the_book() {
    let (mut venue, _) = venue(1_000.0, 2.0);
    book(&mut venue, &[(99.5, 100.0)], &[(100.0, 100.0)]);
    venue
        .submit(&order("a", Side::Buy, 10.0, OrderKind::Market))
        .unwrap();
    let view = venue.account_view();
    let fee = 1_000.0 * 0.00055;
    // No mark yet: the position is valued at the book mid, 99.75.
    let mid = 99.75;
    let equity = 1_000.0 - fee + (mid - 100.0) * 10.0;
    assert!(
        (view.equity_usdt - equity).abs() < 1e-9,
        "{}",
        view.equity_usdt
    );
    let margin = 10.0 * mid / 2.0;
    assert!(
        (view.available_usdt - (equity - margin)).abs() < 1e-9,
        "10 coins at the mid, at 2x, post {margin}"
    );
    assert!(
        matches!(
            venue.submit(&order("b", Side::Buy, 12.0, OrderKind::Market)),
            Err(VenueError::Rejected { code: 110007, .. })
        ),
        "another 600 of margin is not there"
    );
    venue
        .submit(&order("c", Side::Buy, 8.0, OrderKind::Market))
        .unwrap();
    assert_eq!(venue.account_view().positions[0].qty, 18.0);
}

#[test]
fn liquidation_closes_everything_when_equity_reaches_maintenance() {
    let (mut venue, _) = venue(1_000.0, 10.0);
    book(&mut venue, &[(99.5, 1_000.0)], &[(100.0, 1_000.0)]);
    venue
        .submit(&order("a", Side::Buy, 90.0, OrderKind::Market))
        .unwrap();
    venue
        .submit(&order("rest", Side::Buy, 1.0, limit(50.0)))
        .unwrap();
    // 9000 of notional at 100; a mark of 89 makes the loss 990 against
    // 995.05 of cash, with 40.05 of maintenance margin still required.
    book(&mut venue, &[(89.0, 1_000.0)], &[(89.5, 1_000.0)]);
    venue.on_ticker(SymbolId(0), &ticker(89.0, None, None));
    let a = venue.accounting();
    assert!(a.liquidated);
    assert_eq!(a.liquidation_fills, 1);
    assert_eq!(a.resting_orders, 0, "working orders are pulled");
    assert!(venue.account_view().positions.is_empty());
    assert!(matches!(
        venue.submit(&order("again", Side::Buy, 1.0, OrderKind::Market)),
        Err(VenueError::Rejected { code: 110007, .. })
    ));
}

#[tokio::test]
async fn the_gateway_answers_a_round_trip_later_against_the_book_of_that_moment() {
    let (venue, scheduler) = venue(1_000_000.0, 10.0);
    let venue = Arc::new(Mutex::new(venue));
    book(&mut venue.lock().unwrap(), &[(99.5, 5.0)], &[(100.0, 5.0)]);
    let mut gateway = super::venue::SimVenueGateway::new(venue.clone(), scheduler.clone(), 100);
    let request = order("m", Side::Buy, 1.0, OrderKind::Market);
    let mut send = pin!(gateway.send_order(&request));
    assert!(poll_once(&mut send).is_pending(), "in flight");
    assert_eq!(
        scheduler.earliest_pending(&[WaiterKind::Venue]),
        Some(1_050)
    );
    // The book moves while the order is in the air.
    book(&mut venue.lock().unwrap(), &[(100.5, 5.0)], &[(101.0, 5.0)]);
    scheduler.advance_to(1_050);
    assert!(
        poll_once(&mut send).is_pending(),
        "matched, reply in flight"
    );
    assert_eq!(
        venue.lock().unwrap().account_view().positions[0].entry_px,
        101.0,
        "the book at arrival, not at send"
    );
    scheduler.advance_to(1_100);
    let Poll::Ready(Ok(ack)) = poll_once(&mut send) else {
        panic!("acked")
    };
    assert_eq!((ack.sent_ns, ack.ack_ns), (1_000, 1_100));
}

#[test]
fn add_symbol_is_idempotent_and_appends_in_order() {
    let (venue, scheduler) = venue(1.0, 1.0);
    let mut gateway = super::venue::SimVenueGateway::new(Arc::new(Mutex::new(venue)), scheduler, 0);
    assert_eq!(gateway.add_symbol("BTCUSDT"), Some(SymbolId(0)));
    assert_eq!(gateway.add_symbol("ETHUSDT"), Some(SymbolId(1)));
    assert_eq!(gateway.add_symbol("BTCUSDT"), Some(SymbolId(0)));
    assert_eq!(gateway.add_symbol("ETHUSDT"), Some(SymbolId(1)));
}

#[test]
fn a_forced_close_is_charged_to_the_position_with_no_order_id() {
    let (mut venue, _) = venue(1_000_000.0, 10.0);
    book(&mut venue, &[(99.5, 5.0)], &[(100.0, 5.0)]);
    let mut entry = order("e", Side::Buy, 1.0, OrderKind::Market);
    entry.stop = Some(StopSpec { trigger_px: 95.0 });
    venue.submit(&entry).unwrap();
    venue.on_ticker(SymbolId(0), &ticker(94.0, None, None));
    // The queue holds the entry fill then the stop fill; the stop fill has
    // the venue's reason and no client order id.
    assert_eq!(venue.private_pending(), 2);
    let stop = venue.debug_private().last().cloned().unwrap();
    match stop {
        OrderUpdate::Fill {
            client_order_id,
            forced_close,
            side,
            ..
        } => {
            assert!(client_order_id.is_empty());
            assert_eq!(forced_close, Some(ForcedClose::StopLoss));
            assert_eq!(side, Side::Sell);
        }
        other => panic!("{other:?}"),
    }
}

// --------------------------------------------------------- end to end

/// A recorder-contract tape: one snapshot, then per second a ticker, a
/// book delta that moves the touch on a slow sine, and a print at the touch
/// on alternating sides. Funding's boundary falls inside the tape.
fn synthetic_tape(path: &std::path::Path, seconds: u64) -> (u64, i64) {
    let t0_ns: u64 = 1_700_000_000_000_000_000;
    let funding_boundary_ms: i64 = 1_700_000_000_000 + (seconds as i64 / 2) * 1_000;
    let tick = |px: f64| (px * 10.0).round() / 10.0;
    let mut out = std::fs::File::create(path).unwrap();
    let level = |px: f64, qty: f64| format!(r#"["{:.1}","{}"]"#, px, qty);
    let sides = |mid: f64| -> (String, String) {
        let bids: Vec<String> = (0..5)
            .map(|k| level(tick(mid - 0.05 - k as f64 * 0.1), 2.0 + k as f64))
            .collect();
        let asks: Vec<String> = (0..5)
            .map(|k| level(tick(mid + 0.05 + k as f64 * 0.1), 2.0 + k as f64))
            .collect();
        (bids.join(","), asks.join(","))
    };
    let mid0 = 50_000.0;
    let (b, a) = sides(mid0);
    writeln!(out, r#"{{"asks":[{a}],"bids":[{b}],"cross_sequence":0,"depth":50,"exchange_engine_ts_ns":{t0_ns},"exchange_system_ts_ns":{t0_ns},"first_update_id":0,"kind":"orderbook_snapshot","local_receive_ts_ns":{t0_ns},"previous_update_id":0,"restart_snapshot":false,"sequence_gap":false,"symbol":"BTCUSDT","update_id":1,"venue":"bybit-linear"}}"#).unwrap();
    let mut update_id = 1u64;
    for s in 1..=seconds {
        let t = t0_ns + s * 1_000_000_000;
        let mid = tick(mid0 + 120.0 * ((s as f64) / 37.0).sin());
        let next_funding = if ((t / 1_000_000) as i64) < funding_boundary_ms {
            funding_boundary_ms
        } else {
            funding_boundary_ms + 8 * 3_600_000
        };
        writeln!(out, r#"{{"kind":"ticker","local_receive_ts_ns":{t},"exchange_system_ts_ns":{t},"message_type":"delta","cross_sequence":0,"symbol":"BTCUSDT","values":{{"mark_price":"{mid:.1}","last_price":"{mid:.1}","funding_rate":"0.0001","next_funding_time_ms":{next_funding}}},"venue":"bybit-linear"}}"#).unwrap();
        // A delta that replaces both sides: every old level is removed and
        // the new five per side are set, so the top moves with the mid.
        update_id += 1;
        let (b, a) = sides(mid);
        let prev_mid = tick(mid0 + 120.0 * (((s - 1) as f64) / 37.0).sin());
        let (ob, oa) = sides(prev_mid);
        let zero = |levels: &str| -> String {
            levels
                .split("],[")
                .map(|l| {
                    let px = l
                        .trim_matches(|c| c == '[' || c == ']')
                        .split(',')
                        .next()
                        .unwrap();
                    format!("[{px},\"0\"]")
                })
                .collect::<Vec<_>>()
                .join(",")
        };
        let t2 = t + 200_000_000;
        writeln!(out, r#"{{"asks":[{},{a}],"bids":[{},{b}],"cross_sequence":0,"depth":50,"exchange_engine_ts_ns":{t2},"exchange_system_ts_ns":{t2},"first_update_id":0,"kind":"orderbook_delta","local_receive_ts_ns":{t2},"previous_update_id":{},"restart_snapshot":false,"sequence_gap":false,"symbol":"BTCUSDT","update_id":{update_id},"venue":"bybit-linear"}}"#, zero(&oa), zero(&ob), update_id - 1).unwrap();
        let t3 = t + 500_000_000;
        // Prints sweep 8 through the touch on alternating sides, so a quote
        // resting a few dollars off the mid is reached and eaten.
        let (px, side) = if s % 2 == 0 {
            (tick(mid + 8.0), "Buy")
        } else {
            (tick(mid - 8.0), "Sell")
        };
        writeln!(out, r#"{{"exchange_ts_ns":{t3},"kind":"public_trade","local_receive_ts_ns":{t3},"price":{px:.1},"qty":3.0,"side":"{side}","symbol":"BTCUSDT","trade_id":"{s}","venue":"bybit-linear"}}"#).unwrap();
    }
    (t0_ns, funding_boundary_ms)
}

fn quoter_config(path: &std::path::Path) {
    std::fs::write(
        path,
        r#"
[engine]
wal_path = "replaced-by-the-runner.wal"
group_flush_ms = 250
account_view_max_age_ms = 5000
max_quote_age_ms = 30000

[risk]
max_account_view_age_s = 120
max_rolling_loss_fraction = 0.1
leverage = 2.0

[risk.envelope]
tracks_equity = true
reference_usdt = 1000000.0
equity_fraction = 1.0
floor_usdt = 100.0
expand_dead_band_fraction = 0.05
gross_notional_multiple = 2.0
disaster_stop_fraction = 0.35
max_component_gross_notional_usdt = 2000000.0
max_initial_margin_usdt = 1000000.0

[[strategy]]
name = "quoter"
sleeve = "quotes"
symbols = ["BTCUSDT"]
half_spread_bps = 1.0
requote_bps = 0.5
qty = 0.1
max_position = 0.3
stop_loss_fraction = 0.35
"#,
    )
    .unwrap();
}

fn instruments_file(path: &std::path::Path) {
    std::fs::write(path, r#"{"kind":"instruments_snapshot","venue":"bybit","market":"linear","category":"linear","schema":2,"recorded_at_ns":1,"source":"test","rows":[{"symbol":"BTCUSDT","priceFilter":{"tickSize":"0.10"},"lotSizeFilter":{"minOrderQty":"0.001","qtyStep":"0.001","minNotionalValue":"5"}}]}"#).unwrap();
}

async fn run_once(
    dir: &std::path::Path,
    tag: &str,
    tape: &std::path::Path,
) -> super::BacktestReport {
    let config = dir.join("engine.toml");
    let instruments = dir.join("instruments.json");
    quoter_config(&config);
    instruments_file(&instruments);
    super::run(super::BacktestOptions {
        engine_config_path: config,
        tape_path: tape.to_path_buf(),
        instruments_path: instruments,
        signals_path: None,
        wal_path: dir.join(format!("{tag}.wal")),
        trades_path: Some(dir.join(format!("{tag}-trades.jsonl"))),
        equity_path: Some(dir.join(format!("{tag}-equity.jsonl"))),
        report_path: Some(dir.join(format!("{tag}-report.json"))),
        initial_capital_usdt: 100_000.0,
        order_rtt_ms: 175,
        private_latency_ms: 60,
        ..super::BacktestOptions::default()
    })
    .await
    .expect("the replay runs")
}

/// The whole promise, on one tape: the loop ticks in tape time, orders fill
/// against the book of their arrival, funding settles at its boundary, the
/// engine's ledger and the venue's books agree, and a second run writes the
/// same log byte for byte.
#[tokio::test]
async fn a_replay_is_ordered_reconciled_and_byte_identical_on_rerun() {
    let dir = std::env::temp_dir().join(format!(
        "engine-backtest-{}-{}",
        std::process::id(),
        line!()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let tape = dir.join("tape.jsonl");
    let (t0_ns, _) = synthetic_tape(&tape, 1_800);

    let first = run_once(&dir, "one", &tape).await;
    assert_eq!(first.start_wall_ms as u64, t0_ns / 1_000_000);
    assert!(first.market_events >= 1_800, "{}", first.market_events);
    assert!(
        first.orders_sent > 2,
        "the quoter keeps working its quotes: {} orders",
        first.orders_sent
    );
    assert!(first.venue.fills > 0, "{:#?}", first.venue);
    assert!(
        first.venue.maker_fills > 0,
        "resting quotes fill as maker: {:#?}",
        first.venue
    );
    assert_eq!(
        first.venue.funding_settlements, 1,
        "one boundary inside the tape: {:#?}",
        first.venue
    );
    assert!(first.venue.funding_paid_usdt.abs() > 0.0);
    assert!(first.venue.fills_without_book == 0);
    assert!(
        first.engine.closed_trips > 0,
        "on_tick wrote the closed trades: {:#?}",
        first.engine
    );
    assert_ne!(
        first.reconciliation.agrees,
        Some(false),
        "{:#?}",
        first.reconciliation
    );
    assert!(dir.join("one-trades.jsonl").exists());
    assert!(dir.join("one-equity.jsonl").exists());

    let second = run_once(&dir, "two", &tape).await;
    assert_eq!(second.venue, first.venue);
    assert_eq!(second.engine, first.engine);
    let one = std::fs::read(dir.join("one.wal")).unwrap();
    let two = std::fs::read(dir.join("two.wal")).unwrap();
    assert!(!one.is_empty());
    assert_eq!(one, two, "two runs of one tape write the same log");
    assert_eq!(
        std::fs::read_to_string(dir.join("one-trades.jsonl")).unwrap(),
        std::fs::read_to_string(dir.join("two-trades.jsonl")).unwrap()
    );
    std::fs::remove_dir_all(&dir).ok();
}

#[tokio::test]
async fn a_log_with_bytes_in_it_is_refused() {
    let dir = std::env::temp_dir().join(format!(
        "engine-backtest-{}-{}",
        std::process::id(),
        line!()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let tape = dir.join("tape.jsonl");
    synthetic_tape(&tape, 5);
    let config = dir.join("engine.toml");
    let instruments = dir.join("instruments.json");
    quoter_config(&config);
    instruments_file(&instruments);
    let wal = dir.join("used.wal");
    std::fs::write(&wal, b"not empty").unwrap();
    let error = super::run(super::BacktestOptions {
        engine_config_path: config,
        tape_path: tape,
        instruments_path: instruments,
        wal_path: wal,
        ..super::BacktestOptions::default()
    })
    .await
    .expect_err("refused");
    assert!(error.to_string().contains("already holds"), "{error}");
    std::fs::remove_dir_all(&dir).ok();
}

/// A short tape the quoter finishes flat on: the venue's realized less its
/// closed-trip fees and the engine's own round trips must be the same money.
#[tokio::test]
async fn a_replay_that_ends_flat_reconciles_the_venue_and_the_ledger_exactly() {
    let dir = std::env::temp_dir().join(format!(
        "engine-backtest-{}-{}",
        std::process::id(),
        line!()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let tape = dir.join("tape.jsonl");
    synthetic_tape(&tape, 4);
    let report = run_once(&dir, "flat", &tape).await;
    assert_eq!(report.venue.open_positions, 0, "{:#?}", report.venue);
    assert!(report.engine.closed_trips >= 2, "{:#?}", report.engine);
    assert_eq!(
        report.reconciliation.agrees,
        Some(true),
        "{:#?}",
        report.reconciliation
    );
    assert!(
        report.reconciliation.difference_usdt.abs() < 1e-9,
        "{:#?}",
        report.reconciliation
    );
    std::fs::remove_dir_all(&dir).ok();
}
