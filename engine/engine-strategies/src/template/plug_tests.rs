//! The wiring, on the engine's own contracts. The arithmetic is tested in
//! `plan.rs`; these check that the plug reaches for the right verb, reads the
//! right things, and stays quiet when it should.
//!
//! Copy this file with the rest and rewrite the bodies. The cases below are
//! the ones every strategy should have, whatever it does:
//! it acts, it holds, it does not act twice, it respects the venue minimums,
//! and its config is refused when it is wrong.

use engine_types::{Action, InstrumentRule, OrderKind, Side, StrategyId};

use super::plug::Template;
use crate::mock_ctx::{Harness, RestingSeed};

const RULE: InstrumentRule = InstrumentRule {
    tick_size: 0.01,
    qty_step: 0.001,
    min_qty: 0.001,
    min_notional: 5.0,
};

fn config() -> toml::Value {
    toml::from_str(
        r#"
        symbol = "BTCUSDT"
        side = "buy"
        target_qty = 1.0
        tolerance_fraction = 0.05
        stop_loss_fraction = 0.2
    "#,
    )
    .expect("test config parses")
}

fn bench() -> Harness {
    let plug = Template::from_params(StrategyId(0), &config()).expect("the sample config builds");
    let mut h = Harness::new(Box::new(plug));
    h.ctx.set_rule("BTCUSDT", RULE);
    h
}

#[test]
fn flat_it_enters_with_a_stop() {
    let mut h = bench();
    h.quote("BTCUSDT", 99.0, 101.0);
    let intent = h.one_intent();
    assert_eq!(intent.side, Side::Buy);
    assert!((intent.qty - 1.0).abs() < 1e-12);
    assert!(
        intent.stop.is_some(),
        "the kernel refuses an opening order with no stop"
    );
    assert!(!intent.reduce_only);
    let stop = intent.stop.expect("checked above");
    assert!((stop.trigger_px - 80.0).abs() < 1e-9, "{}", stop.trigger_px);
}

#[test]
fn holding_the_target_it_sends_nothing() {
    let mut h = bench();
    h.ctx.set_position("BTCUSDT", Side::Buy, 1.0, 100.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    assert!(h.drain_actions().is_empty(), "there is nothing to correct");
}

#[test]
fn overweight_it_reduces_without_a_stop() {
    let mut h = bench();
    h.ctx.set_position("BTCUSDT", Side::Buy, 1.5, 100.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    let intent = h.one_intent();
    assert_eq!(intent.side, Side::Sell);
    assert!(
        intent.reduce_only,
        "taking exposure off can never be allowed to add any"
    );
    assert!(
        intent.stop.is_none(),
        "a reducing order does not open anything to stop"
    );
    assert!(
        matches!(intent.kind, OrderKind::Market),
        "an exit that rests is exposure nobody wanted, still on the book"
    );
}

#[test]
fn an_entry_rests_rather_than_crossing() {
    // The other half of the rule above: an entry can afford to wait.
    let mut h = bench();
    h.quote("BTCUSDT", 99.0, 101.0);
    let intent = h.one_intent();
    assert!(
        matches!(intent.kind, OrderKind::Limit { .. }),
        "{:?}",
        intent.kind
    );
}

#[test]
fn an_order_already_working_stops_it_deciding_again() {
    // The account reading lags a fill by a couple of seconds. Without this,
    // the same gap is seen twice and filled twice.
    let mut h = bench();
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.resting.push(RestingSeed {
        client_order_id: "eng-1".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit {
            px: 100.0,
            tif: engine_types::TimeInForce::Gtc,
        },
        qty: 1.0,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    h.quote("BTCUSDT", 99.0, 101.0);
    assert!(
        h.drain_actions().is_empty(),
        "the working order is the previous answer"
    );
}

#[test]
fn a_symbol_with_no_venue_rule_is_left_alone() {
    let plug = Template::from_params(StrategyId(0), &config()).expect("the sample config builds");
    let mut h = Harness::new(Box::new(plug));
    h.ctx.add_symbol("BTCUSDT");
    // No set_rule: nothing can be quantized, so nothing can be sent.
    h.quote("BTCUSDT", 99.0, 101.0);
    assert!(h.drain_actions().is_empty());
}

#[test]
fn an_order_under_the_venue_minimum_is_not_emitted() {
    let plug = Template::from_params(StrategyId(0), &config()).expect("the sample config builds");
    let mut h = Harness::new(Box::new(plug));
    h.ctx.set_rule(
        "BTCUSDT",
        InstrumentRule {
            tick_size: 0.01,
            qty_step: 0.001,
            min_qty: 0.001,
            min_notional: 5_000.0,
        },
    );
    h.quote("BTCUSDT", 99.0, 101.0);
    assert!(
        h.drain_actions().is_empty(),
        "100 USDT of notional is under a 5,000 floor"
    );
}

#[test]
fn a_broken_book_decides_nothing() {
    let mut h = bench();
    h.quote("BTCUSDT", 0.0, 0.0);
    assert!(
        h.drain_actions().is_empty(),
        "no price is not a price of zero"
    );
}

#[test]
fn every_emitted_action_is_a_place() {
    // This strategy has no use for cancel or amend. A copy that grows one
    // should replace this test rather than delete it.
    let mut h = bench();
    h.quote("BTCUSDT", 99.0, 101.0);
    for action in h.drain_actions() {
        assert!(matches!(action, Action::Place(_)), "unexpected {action:?}");
    }
}

#[test]
fn a_side_that_is_not_buy_or_sell_is_refused() {
    let bad: toml::Value = toml::from_str(
        r#"
        symbol = "BTCUSDT"
        side = "sideways"
        target_qty = 1.0
        tolerance_fraction = 0.05
        stop_loss_fraction = 0.2
    "#,
    )
    .expect("test config parses");
    let Err(err) = Template::from_params(StrategyId(0), &bad) else {
        panic!("a side the venue has never heard of should be refused");
    };
    assert!(format!("{err}").contains("side"), "{err}");
}

#[test]
fn a_misspelled_parameter_is_refused_rather_than_ignored() {
    // Defaulting here would mean a config that says one thing and trades
    // another, which is the worst way to answer a typo.
    let bad: toml::Value = toml::from_str(
        r#"
        symbol = "BTCUSDT"
        side = "buy"
        target_qty = 1.0
        tolerance_fraction = 0.05
        stop_loss_fractionn = 0.2
    "#,
    )
    .expect("test config parses");
    assert!(Template::from_params(StrategyId(0), &bad).is_err());
}

#[test]
fn a_stop_fraction_of_one_is_refused() {
    let bad: toml::Value = toml::from_str(
        r#"
        symbol = "BTCUSDT"
        side = "buy"
        target_qty = 1.0
        tolerance_fraction = 0.05
        stop_loss_fraction = 1.0
    "#,
    )
    .expect("test config parses");
    assert!(Template::from_params(StrategyId(0), &bad).is_err());
}

#[test]
fn the_template_is_not_something_a_config_can_run() {
    // It is here to be copied, not deployed. Registering yours is a step the
    // author takes deliberately; see the module docs.
    assert!(
        !crate::known_strategies().contains(&super::plug::NAME),
        "the template must stay out of the registry"
    );
    assert!(crate::build_strategy(super::plug::NAME, StrategyId(0), &config()).is_err());
}
