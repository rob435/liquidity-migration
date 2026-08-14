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

#[test]
fn the_effective_spread_is_twice_the_shortfall() {
    // Not a coincidence and not a fudge: the shortfall is measured from the
    // middle, and a spread has two sides of it.
    for side in [Side::Buy, Side::Sell] {
        let shortfall = arrival_shortfall_bps(side, 101.0, 100.0).unwrap();
        let spread = effective_spread_bps(side, 101.0, 100.0).unwrap();
        assert!((spread - 2.0 * shortfall).abs() < 1e-9);
    }
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
    assert_eq!(healthy_mid(&market(99.0, 101.0), BTC), Some(100.0));
    assert_eq!(healthy_mid(&market(101.0, 99.0), BTC), None, "crossed");
    assert_eq!(healthy_mid(&market(0.0, 101.0), BTC), None, "one-sided");
    assert_eq!(healthy_mid(&MarketState::default(), BTC), None, "no symbol");
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
    assert_eq!(total.effective_spread.mean(), Some(200.0));
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
    assert_eq!(total.arrival_shortfall.mean(), None, "nothing to measure it against");
    assert_eq!(total.all_in_arrival_bps(), None, "half a number is not one");
    assert_eq!(total.arrival_coverage(), Some(0.0));
}

#[test]
fn the_maker_share_is_what_rested() {
    let mut fills = Fills::default();
    let mut maker = fill(Side::Buy, 100.0, 1.0, 100.0);
    maker.is_maker = true;
    fills.on_fill(&maker, 0);
    fills.on_fill(&fill(Side::Buy, 100.0, 1.0, 100.0), 0);
    fills.on_fill(&fill(Side::Buy, 100.0, 1.0, 100.0), 0);
    assert_eq!(fills.total().maker_share(), Some(1.0 / 3.0));
    assert_eq!(Costs::default().maker_share(), None, "no fills, no share");
}

#[test]
fn each_sleeves_costs_are_its_own() {
    // One account, two sleeves. A rollup that mixed them would hide the one
    // that is trading badly behind the one that is not.
    let mut fills = Fills::default();
    let mut theirs = fill(Side::Buy, 101.0, 10.0, 100.0);
    theirs.strategy = LONG;
    theirs.symbol = ETH;
    fills.on_fill(&fill(Side::Buy, 100.5, 10.0, 100.0), 0);
    fills.on_fill(&theirs, 0);

    assert_eq!(fills.for_strategy(CARRY).arrival_shortfall.mean(), Some(50.0));
    assert_eq!(fills.for_strategy(LONG).arrival_shortfall.mean(), Some(100.0));
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
    let market = market(100.9, 101.1);

    assert!(fills.due(500 * MS, &market).is_empty(), "1s has not passed");
    let marks = fills.due(1_100 * MS, &market);
    assert_eq!(marks.len(), 1, "only the 1s horizon is due");
    assert_eq!(marks[0].horizon_ms, 1_000);
    assert_eq!(marks[0].mid, Some(101.0));
    assert_eq!(marks[0].signed_markout_bps, Some(100.0), "bought, and it rose");
    assert_eq!(marks[0].actual_horizon_ms, 1_100, "one tick late, and it says so");
    assert_eq!(fills.total().markout[0].mean(), Some(100.0));

    // The later horizons are still owed.
    assert_eq!(fills.pending(), 1);
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

    assert!(fills.due(1_100 * MS, &crossed).is_empty(), "waiting for a book");
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
    let marks = fills.due(3_000 * MS, &market(100.9, 101.1));
    assert_eq!(marks[0].mid, Some(101.0));
    assert_eq!(marks[0].actual_horizon_ms, 3_000, "late, and honest about it");
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
        },
        wire_ns: 1,
        arrival_mid,
    }
}

fn filled(id: &str, px: f64, is_maker: bool) -> WalRecord {
    WalRecord::OrderUpdate {
        update: OrderUpdate::Fill {
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
        sent("eng-1", CARRY, 100.0),
        filled("eng-1", 101.0, true),
        sent("eng-2", LONG, 100.0),
        filled("eng-2", 100.5, false),
    ];
    let off_the_log = Fills::from_records(&log);

    let mut live = Fills::default();
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
    assert_eq!(off_the_log.for_strategy(CARRY).arrival_shortfall.mean(), Some(100.0));
    assert_eq!(off_the_log.for_strategy(LONG).arrival_shortfall.mean(), Some(50.0));
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
