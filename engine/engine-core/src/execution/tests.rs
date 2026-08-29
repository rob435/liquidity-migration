use engine_types::{
    MarketEvent, MarketState, OrderKind, OrderRequest, OrderUpdate, Quote, Side, StrategyId,
    SymbolId, WalRecord,
};

use super::*;

const CARRY: StrategyId = StrategyId(0);
const LONG: StrategyId = StrategyId(1);
const BTC: SymbolId = SymbolId(0);
const ETH: SymbolId = SymbolId(1);

const MS: u64 = 1_000_000;

/// What the ids mean, as the engine writes it down before its first order.
fn names() -> WalRecord {
    WalRecord::Names {
        strategies: vec!["carry".into(), "long".into()],
        symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
    }
}

fn market(bid: f64, ask: f64) -> MarketState {
    let mut market = MarketState::default();
    market.add_symbol("BTCUSDT");
    market.add_symbol("ETHUSDT");
    for symbol in [BTC, ETH] {
        market.apply(&MarketEvent::Quote {
            symbol,
            quote: Quote {
                bid_px: bid,
                ask_px: ask,
                // Stamped, because a markout is only measured against a book
                // that arrived after the fill and a book stamped at zero never
                // did. A live feed always carries one; a test fixture has to
                // say so.
                recv_ns: 1,
                ..Quote::default()
            },
        });
    }
    market
}

fn fill(side: Side, px: f64, qty: f64, arrival_mid: f64) -> Fill {
    Fill {
        client_order_id: "eng-1".into(),
        strategy: CARRY,
        symbol: BTC,
        side,
        qty,
        px,
        fee: 0.0,
        is_maker: false,
        arrival_mid,
        venue_ts_ms: 1_700_000_000_000,
    }
}

// ---------------------------------------------------------------- formulas

#[test]
fn paying_up_reads_adverse_whichever_way_round_the_trade_is() {
    // The sign convention is the whole contract: shortfall, spread and fee
    // are positive when they hurt. A buy that filled above the midpoint and a
    // sell that filled below it are the same event told twice.
    let bought_high = arrival_shortfall_bps(Side::Buy, 101.0, 100.0).unwrap();
    let sold_low = arrival_shortfall_bps(Side::Sell, 99.0, 100.0).unwrap();
    assert!((bought_high - 100.0).abs() < 1e-9, "{bought_high}");
    assert!((sold_low - 100.0).abs() < 1e-9, "{sold_low}");

    // And the other way: filling better than the midpoint is a gain, so it
    // reads negative.
    assert!(arrival_shortfall_bps(Side::Buy, 99.0, 100.0).unwrap() < 0.0);
    assert!(arrival_shortfall_bps(Side::Sell, 101.0, 100.0).unwrap() < 0.0);
}

/// The formula is the doc's and stays; nothing aggregates it, because twice
/// the shortfall against a *send-time* mid is twice the drift of an entry that
/// rested, not a spread — and resting entries are what this fleet ships.
#[test]
fn the_effective_spread_is_twice_the_shortfall() {
    // Hand-computed, not restated from the other function: 20_000 * s *
    // (101 - 100) / 100 is 200 bps adverse for a buy and 200 the good way for
    // a sell. Comparing it against `arrival_shortfall_bps * 2` would be the
    // definition tested against itself.
    assert!((effective_spread_bps(Side::Buy, 101.0, 100.0).unwrap() - 200.0).abs() < 1e-9);
    assert!((effective_spread_bps(Side::Sell, 101.0, 100.0).unwrap() + 200.0).abs() < 1e-9);
    assert!(effective_spread_bps(Side::Buy, 101.0, 0.0).is_none());
}

#[test]
fn a_rebate_reads_as_a_negative_fee() {
    // The venue sends a maker rebate as a negative fee. Positive-is-adverse
    // means it has to stay negative here, or a maker's costs would read
    // higher than a taker's.
    let paid = fee_bps(0.55, 100.0, 10.0).unwrap();
    let earned = fee_bps(-0.20, 100.0, 10.0).unwrap();
    assert!((paid - 5.5).abs() < 1e-9, "{paid}");
    assert!((earned + 2.0).abs() < 1e-9, "{earned}");
}

#[test]
fn a_markout_is_positive_when_the_price_went_our_way() {
    // The one number here with the opposite sign convention, and the reason
    // is that it is the only one that is good news when it is large.
    assert!(signed_markout_bps(Side::Buy, 100.0, 101.0).unwrap() > 0.0);
    assert!(signed_markout_bps(Side::Sell, 100.0, 99.0).unwrap() > 0.0);
    // Bought, and it fell: we were picked off.
    let adverse = signed_markout_bps(Side::Buy, 100.0, 99.0).unwrap();
    assert!((adverse + 100.0).abs() < 1e-9, "{adverse}");
}

#[test]
fn an_anchor_we_never_read_measures_nothing_rather_than_zero() {
    // Zero is what the log holds when the book could not be read. Treating it
    // as a price divides by nothing; treating the answer as "it cost nothing"
    // is worse, because it would flatter every rollup that contained it.
    assert_eq!(arrival_shortfall_bps(Side::Buy, 100.0, 0.0), None);
    assert_eq!(effective_spread_bps(Side::Buy, 100.0, 0.0), None);
    assert_eq!(signed_markout_bps(Side::Buy, 0.0, 100.0), None);
    assert_eq!(signed_markout_bps(Side::Buy, 100.0, 0.0), None);
    assert_eq!(fee_bps(1.0, 0.0, 10.0), None);
    assert_eq!(arrival_shortfall_bps(Side::Buy, f64::NAN, 100.0), None);
}

#[test]
fn a_book_that_is_not_a_book_has_no_midpoint() {
    assert_eq!(
        healthy_mid(&market(99.0, 101.0), BTC).map(|(m, _)| m),
        Some(100.0)
    );
    assert_eq!(healthy_mid(&market(101.0, 99.0), BTC), None, "crossed");
    assert_eq!(healthy_mid(&market(0.0, 101.0), BTC), None, "one-sided");
    assert_eq!(healthy_mid(&MarketState::default(), BTC), None, "no symbol");
}

#[test]
fn a_book_that_has_not_moved_since_the_fill_answers_nothing() {
    // A markout asks where the price went after we traded. If no price has
    // arrived since, there is no answer -- and the book still sitting there is
    // not evidence that nothing moved, it is evidence that nobody told us.
    let mut market = market(99.0, 101.0);
    market.quotes[BTC.0 as usize].recv_ns = 500;
    assert_eq!(
        mid_after(&market, BTC, 400),
        Some(100.0),
        "it arrived after"
    );
    assert_eq!(
        mid_after(&market, BTC, 500),
        None,
        "same instant is not after"
    );
    assert_eq!(
        mid_after(&market, BTC, 900),
        None,
        "the fill is newer than the book"
    );
}

#[test]
fn a_symbol_that_stopped_publishing_is_never_marked_against_its_last_price() {
    // The failure this exists for, and it is invisible without it: a halted or
    // delisted symbol keeps its last quote for ever, so every horizon would
    // mark against the identical mid -- four consistent numbers that read like
    // a measurement and measure nothing. This fleet has met a delisted symbol.
    let mut fills = Fills::default();
    let mut halted = market(99.0, 101.0);
    halted.quotes[BTC.0 as usize].recv_ns = 10;
    fills.on_fill(&fill(Side::Buy, 100.0, 10.0, 100.0), 1_000);

    assert!(
        fills.due(1_000 + 1_100 * MS, &halted).is_empty(),
        "waiting for a price"
    );
    let gave_up = fills.due(1_000 + (1_000 + LATENESS_BOUND_MS) * MS, &halted);
    assert_eq!(gave_up.len(), 1);
    assert_eq!(gave_up[0].mid, None, "never measured, and it says so");
    assert_eq!(fills.total().marks_unmeasurable, 1);
}

#[test]
fn a_mid_from_before_the_horizon_is_not_that_horizon() {
    // A book that spoke once just after the fill and then went quiet: the 1s
    // column must wait for a post-horizon book (and give up honestly), not
    // record the 100ms-old mid as the one-second markout.
    let mut fills = Fills::default();
    let mut quiet = market(99.0, 101.0);
    quiet.quotes[BTC.0 as usize].recv_ns = 1_000 + 100 * MS;
    fills.on_fill(&fill(Side::Buy, 100.0, 10.0, 100.0), 1_000);

    assert!(
        fills.due(1_000 + 1_100 * MS, &quiet).is_empty(),
        "waiting for a post-horizon book"
    );
    let gave_up = fills.due(1_000 + (1_000 + LATENESS_BOUND_MS) * MS, &quiet);
    assert_eq!(gave_up.len(), 1);
    assert_eq!(
        gave_up[0].mid, None,
        "a pre-horizon mid is not the 1s markout"
    );
}

#[test]
fn a_stream_gap_is_remembered_because_the_fills_in_it_are_not() {
    // Nothing can recover them. All this can do is stop the report claiming to
    // be a complete account of what the trading cost.
    let log = vec![
        sent("eng-1", CARRY, 100.0),
        filled("eng-1", 101.0, false),
        WalRecord::OrderUpdate {
            update: OrderUpdate::StreamReset { recv_ns: 1 },
        },
    ];
    let fills = Fills::from_records(&log);
    assert_eq!(fills.stream_gaps, 1);
    assert_eq!(fills.total().fills, 1, "the one we did see still counts");
}

#[test]
fn an_average_of_nothing_is_not_zero() {
    let mut w = Weighted::default();
    assert_eq!(w.mean(), None);
    w.add(4.0, 1.0);
    w.add(2.0, 3.0);
    assert_eq!(w.mean(), Some(2.5), "weighted, not the plain mean of 3");
    // Weightless and unreal values cannot move a mean.
    w.add(1_000.0, 0.0);
    w.add(f64::NAN, 1.0);
    assert_eq!(w.mean(), Some(2.5));
}

// ------------------------------------------------------------------ ledger

#[test]
fn the_ledger_prices_a_fill_against_the_book_when_the_order_left() {
    let mut fills = Fills::default();
    let mut f = fill(Side::Buy, 101.0, 10.0, 100.0);
    f.fee = 0.5555;
    fills.on_fill(&f, 0);

    let total = fills.total();
    assert_eq!(total.fills, 1);
    assert_eq!(total.notional_usdt, 1010.0);
    assert_eq!(total.arrival_shortfall.mean(), Some(100.0));
    assert_eq!(total.fee.mean(), Some(5.5));
    assert_eq!(total.all_in_arrival_bps(), Some(105.5));
    assert_eq!(total.arrival_coverage(), Some(1.0));
}

#[test]
fn a_fill_with_no_anchor_still_counts_as_trading_but_not_as_measurement() {
    // The distinction the coverage number exists for: we traded 1,010 USDT
    // and can speak for none of it.
    let mut fills = Fills::default();
    fills.on_fill(&fill(Side::Buy, 101.0, 10.0, 0.0), 0);
    let total = fills.total();
    assert_eq!(total.fills, 1, "it happened");
    assert_eq!(total.notional_usdt, 1010.0);
    assert_eq!(
        total.arrival_shortfall.mean(),
        None,
        "nothing to measure it against"
    );
    assert_eq!(total.all_in_arrival_bps(), None, "half a number is not one");
    assert_eq!(total.arrival_coverage(), Some(0.0));
}

#[test]
fn the_maker_share_is_notional_that_rested_not_fills_that_rested() {
    // Twenty small maker fills and one large taker fill is 95% of the fills
    // and 9% of the money. The money is the answer: a fill-count share lets
    // the one trade that actually paid the spread hide behind twenty that did
    // not, and this is the number STATE.md says the funded grade waits on.
    let mut fills = Fills::default();
    for _ in 0..20 {
        let mut maker = fill(Side::Buy, 100.0, 0.2, 100.0);
        maker.is_maker = true;
        fills.on_fill(&maker, 0);
    }
    fills.on_fill(&fill(Side::Buy, 100.0, 40.0, 100.0), 0);

    let total = fills.total();
    assert_eq!(total.fills, 21);
    assert_eq!(total.maker_fills, 20, "by count it is twenty of twenty-one");
    let share = total.maker_share().expect("something traded");
    assert!(
        (share - 400.0 / 4400.0).abs() < 1e-12,
        "9% by money, got {share}"
    );
    assert_eq!(
        Costs::default().maker_share(),
        None,
        "nothing traded, no share"
    );
}

#[test]
fn the_all_in_number_is_not_two_means_over_different_fills() {
    // The fee is measured on every fill; the shortfall only on fills whose
    // book could be read. Adding the two means adds numbers taken over
    // different populations, and the rollup is exactly where they diverge.
    let mut fills = Fills::default();
    let mut measured = fill(Side::Buy, 101.0, 10.0, 100.0);
    measured.fee = 0.5555; // 5.5 bp on 1,010
    fills.on_fill(&measured, 0);
    let mut blind = fill(Side::Buy, 101.0, 10.0, 0.0);
    blind.fee = 10.1; // 100 bp, and no book to measure the rest against
    fills.on_fill(&blind, 0);

    let total = fills.total();
    // Both fees counted, so the fee mean is over everything.
    assert_eq!(total.fee.mean(), Some((5.5 + 100.0) / 2.0));
    // The all-in is over the one fill that had both halves, and nothing else.
    assert_eq!(total.all_in_arrival_bps(), Some(105.5));
    assert_eq!(total.arrival_coverage(), Some(0.5));
}

#[test]
fn a_mark_that_arrives_long_after_its_horizon_is_not_that_horizon() {
    // Every mark is a little late -- the engine looks on a 250 ms tick. One
    // that is twenty seconds late is a stall, a paused machine, or a backlog
    // replayed after a reconnect, and averaging it into the one-second column
    // at full weight would read as a one-second fact.
    let mut fills = Fills::default();
    let late = Mark {
        client_order_id: "eng-1".into(),
        strategy: CARRY,
        symbol: BTC,
        fill_ts_ms: 0,
        horizon_ms: 1_000,
        mid: Some(101.0),
        signed_markout_bps: Some(100.0),
        actual_horizon_ms: 1_000 + LATENESS_BOUND_MS + 1,
        notional_usdt: 1_000.0,
    };
    fills.fold_mark(&late);
    let total = fills.total();
    assert_eq!(total.markout[0].mean(), None, "not folded in");
    assert_eq!(total.marks_late, 1, "counted, not hidden");
    assert_eq!(
        total.marks_unmeasurable, 0,
        "the book was read; it was read too late"
    );

    // A mark inside the bound is the horizon it says it is.
    fills.fold_mark(&Mark {
        actual_horizon_ms: 1_250,
        ..late
    });
    assert_eq!(fills.total().markout[0].mean(), Some(100.0));
}

#[test]
fn a_fill_of_nothing_is_never_owed_a_mark_it_cannot_carry() {
    // Zero quantity: the price is real, so a mark would be measured, written
    // to the log, and then dropped by a weight of zero -- counted neither in
    // the average nor in the tally of what could not be measured.
    let mut fills = Fills::default();
    fills.on_fill(&fill(Side::Buy, 100.0, 0.0, 100.0), 0);
    assert_eq!(fills.pending(), 0);
}

#[test]
fn each_sleeves_costs_are_its_own() {
    // One account, two sleeves. A rollup that mixed them would hide the one
    // that is trading badly behind the one that is not.
    let mut fills = Fills::default();
    fills.learn(&names());
    let mut theirs = fill(Side::Buy, 101.0, 10.0, 100.0);
    theirs.strategy = LONG;
    theirs.symbol = ETH;
    fills.on_fill(&fill(Side::Buy, 100.5, 10.0, 100.0), 0);
    fills.on_fill(&theirs, 0);

    assert_eq!(
        fills.for_strategy("carry").arrival_shortfall.mean(),
        Some(50.0)
    );
    assert_eq!(
        fills.for_strategy("long").arrival_shortfall.mean(),
        Some(100.0)
    );
    assert_eq!(fills.rows().count(), 2);
    // The rollup is notional-weighted, so the bigger trade pulls harder: 50 bp
    // over 1,005 USDT against 100 bp over 1,010, not the plain mean of 75.
    let total = fills.total().arrival_shortfall.mean().unwrap();
    let expected = (50.0 * 1005.0 + 100.0 * 1010.0) / (1005.0 + 1010.0);
    assert!((total - expected).abs() < 1e-9, "{total} vs {expected}");
    assert!(total > 75.0, "the dearer fill traded the larger notional");
}

// ----------------------------------------------------------------- markout

#[test]
fn a_markout_comes_due_at_its_horizon_and_not_before() {
    let mut fills = Fills::default();
    fills.on_fill(&fill(Side::Buy, 100.0, 10.0, 100.0), 0);
    // A live book keeps pushing, so the mark taken at each horizon is one
    // that arrived at or after it — the fixture has to say so.
    let mut market = market(100.9, 101.1);
    market.quotes[BTC.0 as usize].recv_ns = 1_050 * MS;

    assert!(fills.due(500 * MS, &market).is_empty(), "1s has not passed");
    let marks = fills.due(1_100 * MS, &market);
    assert_eq!(marks.len(), 1, "only the 1s horizon is due");
    assert_eq!(marks[0].horizon_ms, 1_000);
    assert_eq!(marks[0].mid, Some(101.0));
    assert_eq!(
        marks[0].signed_markout_bps,
        Some(100.0),
        "bought, and it rose"
    );
    assert_eq!(
        marks[0].actual_horizon_ms, 1_100,
        "one tick late, and it says so"
    );
    assert_eq!(fills.total().markout[0].mean(), Some(100.0));

    // The later horizons are still owed.
    assert_eq!(fills.pending(), 1);
    market.quotes[BTC.0 as usize].recv_ns = 400_000 * MS;
    let rest = fills.due(400_000 * MS, &market);
    assert_eq!(rest.len(), 3, "15s, 1m and 5m all came due together");
    assert_eq!(fills.pending(), 0, "nothing left to mark");
}

#[test]
fn a_horizon_waits_for_a_readable_book_but_not_forever() {
    // "The first healthy midpoint at or after h" — so a dark book is worth
    // waiting through. Past the lateness bound it is a horizon that will
    // never be measured, and it says so with an absent midpoint.
    let mut fills = Fills::default();
    fills.on_fill(&fill(Side::Buy, 100.0, 10.0, 100.0), 0);
    let crossed = market(101.0, 99.0);

    assert!(
        fills.due(1_100 * MS, &crossed).is_empty(),
        "waiting for a book"
    );
    let marks = fills.due((1_000 + LATENESS_BOUND_MS) * MS, &crossed);
    assert_eq!(marks.len(), 1);
    assert_eq!(marks[0].mid, None, "terminally missing, never a zero");
    assert_eq!(marks[0].signed_markout_bps, None);
    assert_eq!(fills.total().markout[0].mean(), None);
    assert_eq!(fills.total().marks_unmeasurable, 1, "counted, not hidden");
}

#[test]
fn a_book_that_comes_back_inside_the_bound_is_marked_against_it() {
    let mut fills = Fills::default();
    fills.on_fill(&fill(Side::Buy, 100.0, 10.0, 100.0), 0);
    assert!(fills.due(1_100 * MS, &market(101.0, 99.0)).is_empty());
    // The book that came back is stamped when it came back — two seconds
    // past the horizon — not at the fixture default of one nanosecond.
    let mut back = market(100.9, 101.1);
    back.quotes[BTC.0 as usize].recv_ns = 3_000 * MS;
    let marks = fills.due(3_000 * MS, &back);
    assert_eq!(marks[0].mid, Some(101.0));
    assert_eq!(
        marks[0].actual_horizon_ms, 3_000,
        "late, and honest about it"
    );
}

#[test]
fn a_fill_with_no_usable_price_is_never_owed_a_mark() {
    let mut fills = Fills::default();
    fills.on_fill(&fill(Side::Buy, 0.0, 10.0, 100.0), 0);
    assert_eq!(fills.pending(), 0);
}

#[test]
fn a_mark_at_a_horizon_this_build_does_not_measure_is_not_miscounted() {
    let mut fills = Fills::default();
    fills.fold_mark(&Mark {
        client_order_id: "eng-1".into(),
        strategy: CARRY,
        symbol: BTC,
        fill_ts_ms: 0,
        horizon_ms: 7_777,
        mid: Some(101.0),
        signed_markout_bps: Some(100.0),
        actual_horizon_ms: 7_777,
        notional_usdt: 1_000.0,
    });
    let total = fills.total();
    assert!(total.markout.iter().all(|m| m.mean().is_none()));
}

// -------------------------------------------------------------- off the log

fn sent(id: &str, strategy: StrategyId, arrival_mid: f64) -> WalRecord {
    WalRecord::OrderSent {
        request: OrderRequest {
            client_order_id: id.into(),
            strategy,
            symbol: BTC,
            side: Side::Buy,
            qty: 10.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            close_position: false,
        },
        wire_ns: 1,
        arrival_mid,
    }
}

fn filled(id: &str, px: f64, is_maker: bool) -> WalRecord {
    WalRecord::OrderUpdate {
        update: OrderUpdate::Fill {
            exec_id: String::new(),
            client_order_id: id.into(),
            symbol: BTC,
            side: Side::Buy,
            qty: 10.0,
            px,
            fee: 0.5555,
            is_maker,
            venue_ts_ms: 1_700_000_000_000,
            recv_ns: 1,
        },
    }
}

#[test]
fn the_log_alone_says_what_the_trading_cost() {
    // The property that makes this worth having: the live summary and the
    // report read off a finished log are the same arithmetic, so they cannot
    // disagree about a number the owner is looking at.
    let log = vec![
        names(),
        sent("eng-1", CARRY, 100.0),
        filled("eng-1", 101.0, true),
        sent("eng-2", LONG, 100.0),
        filled("eng-2", 100.5, false),
    ];
    let off_the_log = Fills::from_records(&log);

    let mut live = Fills::default();
    live.learn(&names());
    for (id, strategy, px, is_maker) in
        [("eng-1", CARRY, 101.0, true), ("eng-2", LONG, 100.5, false)]
    {
        live.on_fill(
            &Fill {
                client_order_id: id.into(),
                strategy,
                symbol: BTC,
                side: Side::Buy,
                qty: 10.0,
                px,
                fee: 0.5555,
                is_maker,
                arrival_mid: 100.0,
                venue_ts_ms: 1_700_000_000_000,
            },
            0,
        );
    }
    assert_eq!(off_the_log.total(), live.total());
    assert_eq!(off_the_log.total().maker_fills, 1);
    assert_eq!(
        off_the_log.for_strategy("carry").arrival_shortfall.mean(),
        Some(100.0)
    );
    assert_eq!(
        off_the_log.for_strategy("long").arrival_shortfall.mean(),
        Some(50.0)
    );
}

#[test]
fn a_log_written_before_any_of_this_existed_reads_as_unmeasured() {
    // `arrival_mid` defaults to zero on the way in. The fills are real and
    // are counted; what they cost is unknown, and unknown is not zero.
    let log = vec![sent("eng-1", CARRY, 0.0), filled("eng-1", 101.0, false)];
    let total = Fills::from_records(&log).total();
    assert_eq!(total.fills, 1);
    assert_eq!(total.arrival_shortfall.mean(), None);
    assert_eq!(total.fee.mean(), Some(5.5), "the fee never needed a book");
}

#[test]
fn somebody_elses_fill_is_priced_for_nobody() {
    // A hand trade on the same account. Charging it to a strategy would put
    // another person's execution in our own scorecard.
    let total = Fills::from_records(&[filled("hand-placed", 101.0, false)]).total();
    assert_eq!(total.fills, 0);
    assert_eq!(total.notional_usdt, 0.0);
}

#[test]
fn the_marks_in_the_log_are_read_back_and_not_recomputed() {
    // A log holds no prices, so a markout cannot be derived from one. It has
    // to be read back off the record written when the horizon came due.
    let log = vec![
        sent("eng-1", CARRY, 100.0),
        filled("eng-1", 100.0, false),
        WalRecord::Markout {
            client_order_id: "eng-1".into(),
            strategy: CARRY,
            symbol: BTC,
            fill_ts_ms: 1_700_000_000_000,
            horizon_ms: 60_000,
            mid: Some(101.0),
            signed_markout_bps: Some(100.0),
            actual_horizon_ms: 60_250,
            notional_usdt: 1_000.0,
        },
    ];
    let total = Fills::from_records(&log).total();
    assert_eq!(total.markout[2].mean(), Some(100.0), "the 1m bucket");
    assert!(total.markout[0].mean().is_none(), "1s was never marked");
}

#[test]
fn replaying_a_log_leaves_nothing_owed() {
    // A replay is not trading. Leaving fills owed a future mark would have a
    // report claim it was waiting on prices that will never arrive.
    let log = vec![sent("eng-1", CARRY, 100.0), filled("eng-1", 101.0, false)];
    assert_eq!(Fills::from_records(&log).pending(), 0);
}

#[test]
fn a_mark_survives_the_round_trip_through_a_record() {
    let mark = Mark {
        client_order_id: "eng-1".into(),
        strategy: LONG,
        symbol: ETH,
        fill_ts_ms: 1_700_000_000_000,
        horizon_ms: 15_000,
        mid: Some(101.0),
        signed_markout_bps: Some(100.0),
        actual_horizon_ms: 15_250,
        notional_usdt: 1_000.0,
    };
    let mut folded = Fills::default();
    folded.fold_mark(&mark);
    let mut off_the_record = Fills::default();
    off_the_record.fold_mark(&Mark {
        // Everything a reader would rebuild it from.
        ..match mark.to_record() {
            WalRecord::Markout {
                client_order_id,
                strategy,
                symbol,
                fill_ts_ms,
                horizon_ms,
                mid,
                signed_markout_bps,
                actual_horizon_ms,
                notional_usdt,
            } => Mark {
                client_order_id,
                strategy,
                symbol,
                fill_ts_ms,
                horizon_ms,
                mid,
                signed_markout_bps,
                actual_horizon_ms,
                notional_usdt,
            },
            other => panic!("expected a markout record, got {other:?}"),
        }
    });
    assert_eq!(folded.total(), off_the_record.total());
}

// ------------------------------------------------- ids are not names

/// The tables as a boot that admitted its coins in a different order writes
/// them. `symbols` is what shifts: the seeds hold the low ids and everything
/// after arrives in whatever order a book first named it.
fn names_with(strategies: &[&str], symbols: &[&str]) -> WalRecord {
    WalRecord::Names {
        strategies: strategies.iter().map(|s| s.to_string()).collect(),
        symbols: symbols.iter().map(|s| s.to_string()).collect(),
    }
}

fn sent_for(id: &str, strategy: StrategyId, symbol: SymbolId) -> WalRecord {
    WalRecord::OrderSent {
        request: OrderRequest {
            client_order_id: id.into(),
            strategy,
            symbol,
            side: Side::Buy,
            qty: 10.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            close_position: false,
        },
        wire_ns: 1,
        arrival_mid: 100.0,
    }
}

fn filled_for(id: &str, symbol: SymbolId, px: f64) -> WalRecord {
    WalRecord::OrderUpdate {
        update: OrderUpdate::Fill {
            exec_id: String::new(),
            client_order_id: id.into(),
            symbol,
            side: Side::Buy,
            qty: 10.0,
            px,
            fee: 0.0,
            is_maker: false,
            venue_ts_ms: 1_700_000_000_000,
            recv_ns: 1,
        },
    }
}

#[test]
fn one_id_that_meant_two_coins_is_two_rows() {
    // The demo log does this: the symbol table is rebuilt every boot, seeds
    // first and the rest in the order a book first named them, so id 8 has
    // been both HYPEUSDT and BICOUSDT. Keyed by id, one row would carry both
    // coins' trading under whichever name the last table happened to hold.
    let log = vec![
        names_with(&["carry"], &["BTCUSDT", "HYPEUSDT"]),
        sent_for("eng-1", CARRY, ETH),
        filled_for("eng-1", ETH, 69.0),
        names_with(&["carry"], &["BTCUSDT", "BICOUSDT"]),
        sent_for("eng-2", CARRY, ETH),
        filled_for("eng-2", ETH, 0.02),
    ];
    let fills = Fills::from_records(&log);

    let rows: Vec<(&str, &str, u64)> = fills
        .rows()
        .map(|(sleeve, symbol, costs)| (sleeve, symbol, costs.fills))
        .collect();
    assert_eq!(
        rows,
        vec![("carry", "BICOUSDT", 1), ("carry", "HYPEUSDT", 1)]
    );
}

#[test]
fn a_retired_sleeve_keeps_the_name_it_traded_under() {
    // `llm_gate` was a fourth sleeve for a day and was then taken out of the
    // config. Its fills are still in the log, and the table that names them
    // is the one that was in force when they happened -- not the last table,
    // which no longer reaches id 3.
    const GATE: StrategyId = StrategyId(3);
    let log = vec![
        names_with(&["carry", "long", "exodus", "llm_gate"], &["BTCUSDT"]),
        sent_for("eng-1", GATE, BTC),
        filled_for("eng-1", BTC, 100.0),
        names_with(&["carry", "long", "exodus"], &["BTCUSDT"]),
    ];
    let fills = Fills::from_records(&log);

    let rows: Vec<(&str, &str)> = fills
        .rows()
        .map(|(sleeve, symbol, _)| (sleeve, symbol))
        .collect();
    assert_eq!(rows, vec![("llm_gate", "BTCUSDT")]);
}

#[test]
fn an_id_no_table_ever_named_still_reads_as_a_number() {
    // A log written before the engine recorded its tables. The fills are
    // real and are counted; there is nothing to call them but their ids.
    let log = vec![
        sent_for("eng-1", CARRY, BTC),
        filled_for("eng-1", BTC, 100.0),
    ];
    let fills = Fills::from_records(&log);
    let rows: Vec<(&str, &str)> = fills
        .rows()
        .map(|(sleeve, symbol, _)| (sleeve, symbol))
        .collect();
    assert_eq!(rows, vec![("strategy 0", "symbol 0")]);
}

// ------------------------------------------------ fills the stream missed

fn recovered(id: &str, symbol: SymbolId, px: f64, venue_ts_ms: i64) -> WalRecord {
    WalRecord::RecoveredFill {
        exec_id: format!("venue-{id}"),
        client_order_id: id.into(),
        symbol,
        side: Side::Buy,
        qty: 10.0,
        px,
        fee: 0.5,
        is_maker: false,
        venue_ts_ms,
        recovered_wall_ts_ms: venue_ts_ms + 60_000,
    }
}

#[test]
fn a_fill_the_stream_missed_costs_the_same_as_one_it_delivered() {
    // The stream went down, the venue's own execution history gave the fill
    // back, and it cost exactly what it cost. Leaving it out understates the
    // traded notional and every mean taken over it.
    let log = vec![
        names(),
        sent("eng-1", CARRY, 100.0),
        WalRecord::OrderUpdate {
            update: OrderUpdate::StreamReset { recv_ns: 1 },
        },
        recovered("eng-1", BTC, 101.0, 1_700_000_000_000),
    ];
    let fills = Fills::from_records(&log);
    let total = fills.total();

    assert_eq!(total.fills, 1);
    assert_eq!(fills.recovered, 1, "and said so");
    assert_eq!(fills.stream_gaps, 1);
    assert!(
        (total.notional_usdt - 1010.0).abs() < 1e-9,
        "{}",
        total.notional_usdt
    );
    // Priced against the book its own order left at, like any other fill.
    assert_eq!(total.arrival_shortfall.mean(), Some(100.0));
    let rows: Vec<(&str, &str)> = fills
        .rows()
        .map(|(sleeve, symbol, _)| (sleeve, symbol))
        .collect();
    assert_eq!(rows, vec![("carry", "BTCUSDT")]);
}

#[test]
fn a_fill_recovered_for_no_order_of_ours_is_priced_for_nobody() {
    // A venue-attached stop firing, or a hand trade: the same join as a
    // delivered fill, and the same answer when the join finds nothing.
    let log = vec![
        names(),
        recovered("stranger", BTC, 101.0, 1_700_000_000_000),
    ];
    let fills = Fills::from_records(&log);
    assert_eq!(fills.total().fills, 0);
    assert_eq!(fills.recovered, 0);
}

#[test]
fn a_fill_found_after_its_horizons_passed_is_not_marked_against_a_later_book() {
    // Recovery runs after the gap closes, which can be minutes. Dating the
    // fill to when it was found would mark a five-minute-old trade against
    // the book in front of us and call it a one-second markout.
    let found_ns = 310 * 1_000 * MS;
    let mut market = market(100.0, 101.0);
    // A book that is live right now, so every horizon has a mid to be marked
    // against. That is exactly the trap: it is this minute's price, and the
    // fill is five minutes old.
    market.apply(&MarketEvent::Quote {
        symbol: BTC,
        quote: Quote {
            bid_px: 200.0,
            ask_px: 201.0,
            recv_ns: found_ns,
            ..Quote::default()
        },
    });

    let mut fills = Fills::default();
    fills.learn(&names());
    let five_minutes_ago = 0u64;
    fills.on_recovered_fill(&fill(Side::Buy, 100.5, 10.0, 100.0), Some(five_minutes_ago));

    let marks = fills.due(found_ns, &market);
    assert_eq!(
        marks.len(),
        HORIZONS_MS.len(),
        "every horizon is answered, once"
    );
    assert!(
        marks.iter().all(|mark| mark.mid == Some(200.5)),
        "the book was readable"
    );
    let total = fills.total();
    assert_eq!(total.marks_late as usize, HORIZONS_MS.len());
    assert_eq!(
        total.marks_unmeasurable, 0,
        "the book was there; it was the clock"
    );
    for horizon in &total.markout {
        assert_eq!(horizon.mean(), None, "a late read is not a markout");
    }
}

#[test]
fn a_fill_older_than_the_engine_itself_is_owed_no_mark() {
    // The engine's clock starts with the process, so an execution from before
    // the boot has no instant on it to date from. Dating it to the origin
    // would make a trade from before the restart look brand new and mark it
    // against the book in front of us.
    let mut fills = Fills::default();
    fills.learn(&names());
    fills.on_recovered_fill(&fill(Side::Buy, 100.5, 10.0, 100.0), None);

    let total = fills.total();
    assert_eq!(
        total.fills, 1,
        "it still traded, and it still cost something"
    );
    assert!((total.notional_usdt - 1005.0).abs() < 1e-9);
    assert_eq!(total.arrival_shortfall.mean(), Some(50.0));
    assert_eq!(
        fills.pending(),
        0,
        "nothing is waiting for a horizon that is gone"
    );
    assert_eq!(
        total.marks_late, 0,
        "no mark was attempted, so none was thrown away"
    );
}

/// A restart mid-position. The cost rows are this run's, but the position is
/// not: the sleeve is still holding, and its close has to be priced against
/// the entry the old run made.
#[test]
fn boot_adopts_the_open_positions_a_log_leaves_and_not_its_closed_ones() {
    fn order(id: &str, strategy: StrategyId, symbol: SymbolId) -> WalRecord {
        WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: id.into(),
                strategy,
                symbol,
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: false,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 0.0,
        }
    }
    fn traded(id: &str, symbol: SymbolId, side: Side, px: f64, qty: f64) -> WalRecord {
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: String::new(),
                client_order_id: id.into(),
                symbol,
                side,
                qty,
                px,
                fee: 0.0,
                is_maker: false,
                venue_ts_ms: 1,
                recv_ns: 1,
            },
        }
    }

    let opened = vec![
        names(),
        order("eng-1", CARRY, BTC),
        traded("eng-1", BTC, Side::Buy, 100.0, 2.0),
        order("eng-2", LONG, ETH),
        traded("eng-2", ETH, Side::Buy, 10.0, 5.0),
        order("eng-3", LONG, ETH),
        traded("eng-3", ETH, Side::Sell, 12.0, 5.0),
    ];

    // Boot's own order: adopt the positions, then say what this run's ids
    // mean. A lot is keyed by name for exactly this reason — the id table is
    // rebuilt every boot and the names are what survive it.
    let mut fresh = Fills::default();
    fresh.seed_lots(&opened);
    fresh.learn(&names());
    assert!(
        fresh.take_closed().is_empty(),
        "the trip the old run already closed must not be announced again"
    );

    // Carry is still long 2 BTC at 100. Selling it here is the whole point:
    // without the seed this reads as opening a short and reports nothing.
    fresh.on_fill(
        &Fill {
            client_order_id: "eng-4".into(),
            strategy: CARRY,
            symbol: BTC,
            side: Side::Sell,
            qty: 2.0,
            px: 110.0,
            fee: 0.0,
            is_maker: false,
            arrival_mid: 0.0,
            venue_ts_ms: 9,
        },
        0,
    );
    let closed = fresh.take_closed();
    assert_eq!(closed.len(), 1);
    let rt = closed[0]
        .round_trip
        .as_ref()
        .expect("the entry came off the log");
    assert_eq!(rt.entry_px, 100.0);
    assert!((rt.net_usdt - 20.0).abs() < 1e-9, "{}", rt.net_usdt);
}

/// A log that starts mid-position. Read without the rotation's own account of
/// what each sleeve held, the closing sale reads as opening a short and the
/// next entry closes that phantom for a profit nobody made — which is what
/// the live demo log did, to the tune of a fabricated +101 USDT.
#[test]
fn a_segment_that_starts_mid_position_reports_no_money_for_the_close() {
    let held = WalRecord::SegmentBase {
        wall_ts_ms: 1,
        strategies: vec!["carry".into()],
        symbols: vec!["ONGUSDT".into()],
        may_open: true,
        control_anchors: vec![],
        attribution: vec![engine_types::wal::FilledTotal {
            strategy: CARRY,
            symbol: BTC,
            signed_qty: 5_056.0,
        }],
        logged_exposure: vec![],
        intended_stops: vec![],
        recent_execution_ids: vec![],
        open_orders: vec![],
    };
    fn order(id: &str) -> WalRecord {
        WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: id.into(),
                strategy: CARRY,
                symbol: BTC,
                side: Side::Sell,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: false,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 0.0,
        }
    }
    fn traded(id: &str, side: Side, px: f64, qty: f64) -> WalRecord {
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: String::new(),
                client_order_id: id.into(),
                symbol: BTC,
                side,
                qty,
                px,
                fee: 0.0,
                is_maker: false,
                venue_ts_ms: 1,
                recv_ns: 1,
            },
        }
    }

    let fills = Fills::from_records(&[
        held,
        order("eng-1"),
        traded("eng-1", Side::Sell, 0.0886, 5_056.0),
        order("eng-2"),
        traded("eng-2", Side::Buy, 0.0684, 7_347.0),
    ]);

    let closed = fills.closed();
    assert_eq!(closed.len(), 1, "one close, and the re-entry is not a trip");
    assert_eq!(closed[0].side, "long", "it was long before the rotation");
    assert!(
        closed[0].round_trip.is_none(),
        "priced from an entry this log never held: {:?}",
        closed[0].round_trip
    );
}
