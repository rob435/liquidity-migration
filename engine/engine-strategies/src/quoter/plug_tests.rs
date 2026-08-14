//! The quoting plug on the engine's own contracts: what it emits, and which
//! verb it reaches for. The arithmetic is tested in `plan.rs`; these check the
//! wiring — that a maker can place, move and pull through the engine.

use engine_types::{Action, InstrumentRule, OrderKind, Side};

use super::plug::Quoter;
use crate::mock_ctx::{Harness, RestingSeed};

const RULE: InstrumentRule =
    InstrumentRule { tick_size: 0.01, qty_step: 0.001, min_qty: 0.001, min_notional: 5.0 };

fn config() -> toml::Value {
    let src = r#"
        symbol = "BTCUSDT"
        half_spread_bps = 10.0
        requote_bps = 2.0
        qty = 0.1
        max_position = 0.3
        stop_loss_fraction = 0.35
    "#;
    toml::from_str(src).expect("test config parses")
}

fn bench() -> Harness {
    let quoter = Quoter::from_params(engine_types::StrategyId(0), &config()).unwrap();
    let mut h = Harness::new(Box::new(quoter));
    h.ctx.set_rule("BTCUSDT", RULE);
    h
}

#[test]
fn it_quotes_both_sides_post_only_with_a_stop() {
    let mut h = bench();
    h.quote("BTCUSDT", 99.0, 101.0);
    let placed: Vec<_> = h
        .drain_actions()
        .into_iter()
        .filter_map(|a| match a {
            Action::Place(intent) => Some(intent),
            _ => None,
        })
        .collect();
    assert_eq!(placed.len(), 2, "a maker quotes both sides");
    for intent in &placed {
        assert!(
            matches!(intent.kind, OrderKind::Limit { tif: engine_types::TimeInForce::PostOnly, .. }),
            "a maker that crosses is not a maker"
        );
        assert!(intent.stop.is_some(), "the kernel refuses an opening order with no stop");
        assert!(!intent.reduce_only);
    }
    assert!(placed.iter().any(|i| i.side == Side::Buy));
    assert!(placed.iter().any(|i| i.side == Side::Sell));
}

#[test]
fn a_quote_that_has_drifted_is_moved_not_replaced() {
    // Amending keeps the order's identity, and where the venue allows it, its
    // place in the queue. Cancel-and-replace would give both away.
    let mut h = bench();
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.resting.push(RestingSeed {
        client_order_id: "eng-1".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit { px: 90.0, tif: engine_types::TimeInForce::PostOnly },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    h.quote("BTCUSDT", 99.0, 101.0);
    let amends: Vec<_> = h
        .drain_actions()
        .into_iter()
        .filter(|a| matches!(a, Action::Amend { .. }))
        .collect();
    assert_eq!(amends.len(), 1, "the stale bid is moved");
    match &amends[0] {
        Action::Amend { client_order_id, spec, .. } => {
            assert_eq!(client_order_id, "eng-1");
            assert_eq!(spec.px, Some(99.9));
            assert_eq!(spec.qty, None, "only the price changed");
        }
        other => panic!("expected an amend, got {other:?}"),
    }
}

#[test]
fn a_full_side_is_pulled_rather_than_left_in_the_market() {
    // Long at the ceiling: the bid is what keeps filling, so it comes out.
    let mut h = bench();
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.set_position("BTCUSDT", Side::Buy, 0.3, 100.0);
    h.ctx.resting.push(RestingSeed {
        client_order_id: "eng-bid".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit { px: 99.9, tif: engine_types::TimeInForce::PostOnly },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    h.quote("BTCUSDT", 99.0, 101.0);
    let cancels: Vec<_> = h
        .drain_actions()
        .into_iter()
        .filter(|a| matches!(a, Action::Cancel { .. }))
        .collect();
    assert_eq!(cancels.len(), 1, "the side that would breach the ceiling comes out");
}

#[test]
fn an_empty_book_pulls_the_quotes() {
    let mut h = bench();
    let symbol = h.ctx.id_of("BTCUSDT");
    for (id, side) in [("eng-b", Side::Buy), ("eng-a", Side::Sell)] {
        h.ctx.resting.push(RestingSeed {
            client_order_id: id.into(),
            symbol,
            side,
            kind: OrderKind::Limit { px: 100.0, tif: engine_types::TimeInForce::PostOnly },
            qty: 0.1,
            filled_qty: 0.0,
            reduce_only: false,
            acked: true,
        });
    }
    h.quote("BTCUSDT", 0.0, 0.0);
    let cancels = h
        .drain_actions()
        .into_iter()
        .filter(|a| matches!(a, Action::Cancel { .. }))
        .count();
    assert_eq!(cancels, 2, "a broken feed is not something to quote around");
}

#[test]
fn a_quote_under_the_venue_minimum_is_not_emitted() {
    let quoter = Quoter::from_params(engine_types::StrategyId(0), &config()).unwrap();
    let mut h = Harness::new(Box::new(quoter));
    h.ctx.set_rule(
        "BTCUSDT",
        InstrumentRule { tick_size: 0.01, qty_step: 0.001, min_qty: 0.001, min_notional: 5000.0 },
    );
    h.quote("BTCUSDT", 99.0, 101.0);
    assert!(h.drain_actions().is_empty(), "1 USDT of notional is not a quote");
}

#[test]
fn a_requote_tolerance_wider_than_the_quote_is_refused() {
    // It would mean a quote that can never be worth moving.
    let bad: toml::Value = toml::from_str(r#"
        symbol = "BTCUSDT"
        half_spread_bps = 2.0
        requote_bps = 10.0
        qty = 0.1
        max_position = 0.3
        stop_loss_fraction = 0.35
    "#)
    .expect("test config parses");
    let Err(err) = Quoter::from_params(engine_types::StrategyId(0), &bad) else {
        panic!("a tolerance wider than the quote should be refused");
    };
    assert!(format!("{err}").contains("requote_bps"), "{err}");
}

#[test]
fn a_stop_fraction_of_one_is_refused() {
    let bad: toml::Value = toml::from_str(r#"
        symbol = "BTCUSDT"
        half_spread_bps = 10.0
        requote_bps = 2.0
        qty = 0.1
        max_position = 0.3
        stop_loss_fraction = 1.0
    "#)
    .expect("test config parses");
    assert!(Quoter::from_params(engine_types::StrategyId(0), &bad).is_err());
}
