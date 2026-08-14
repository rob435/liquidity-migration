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
        symbols = ["BTCUSDT"]
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
    // The position is this strategy's own, from its own fills -- not the
    // account reading, which is somebody else's business and seconds old.
    let mut h = bench();
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.set_my_position("BTCUSDT", 0.3);
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
        symbols = ["BTCUSDT"]
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
        symbols = ["BTCUSDT"]
        half_spread_bps = 10.0
        requote_bps = 2.0
        qty = 0.1
        max_position = 0.3
        stop_loss_fraction = 1.0
    "#)
    .expect("test config parses");
    assert!(Quoter::from_params(engine_types::StrategyId(0), &bad).is_err());
}

// ------------------------------------------- what it reads, and when it acts

/// A quoter over the symbols named, leaning `skew_bps` against inventory.
fn quoter_over(symbols: &[&str], skew_bps: f64) -> Harness {
    let list = symbols
        .iter()
        .map(|s| format!("{s:?}"))
        .collect::<Vec<_>>()
        .join(", ");
    // Left out entirely for no lean, which is how this crate spells "off".
    let lean = if skew_bps > 0.0 {
        format!("skew_bps = {skew_bps}")
    } else {
        String::new()
    };
    let src = format!(
        r#"
        symbols = [{list}]
        half_spread_bps = 10.0
        requote_bps = 2.0
        {lean}
        qty = 0.1
        max_position = 0.3
        stop_loss_fraction = 0.35
        "#
    );
    let config: toml::Value = toml::from_str(&src).expect("test config parses");
    let quoter = Quoter::from_params(engine_types::StrategyId(0), &config).unwrap();
    let mut h = Harness::new(Box::new(quoter));
    for symbol in symbols {
        h.ctx.set_rule(symbol, RULE);
    }
    h
}

fn placed(h: &mut Harness) -> Vec<engine_types::Intent> {
    h.drain_actions()
        .into_iter()
        .filter_map(|a| match a {
            Action::Place(intent) => Some(intent),
            _ => None,
        })
        .collect()
}

fn price_of(intents: &[engine_types::Intent], want: Side) -> f64 {
    intents
        .iter()
        .find(|i| i.side == want)
        .and_then(|i| match i.kind {
            OrderKind::Limit { px, .. } => Some(px),
            OrderKind::Market => None,
        })
        .unwrap_or_else(|| panic!("no {want:?} quote in {intents:?}"))
}

#[test]
fn the_account_reading_does_not_decide_this_makers_inventory() {
    // The account view lags by seconds and, on a two-sleeve account, is the
    // sum of both. A maker that believed it would stop quoting a side it is
    // nowhere near the ceiling on.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    h.ctx.set_position("BTCUSDT", Side::Buy, 0.3, 100.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    let intents = placed(&mut h);
    assert_eq!(intents.len(), 2, "our own book is flat, so both sides are quoted");
}

#[test]
fn our_own_fills_do_decide_it() {
    // The other half of the same fact: the ceiling still binds, and it binds
    // on the position this strategy actually opened.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    h.ctx.set_my_position("BTCUSDT", 0.3);
    h.quote("BTCUSDT", 99.0, 101.0);
    let intents = placed(&mut h);
    assert_eq!(intents.len(), 1, "the buy side is at its ceiling");
    assert_eq!(intents[0].side, Side::Sell);
}

#[test]
fn a_fill_is_a_wake_and_the_quotes_are_rebuilt_on_it() {
    // A maker that waited for the next price to notice its own fill would sit
    // one-sided through exactly the quiet market it can least afford it in.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    let first = placed(&mut h);
    assert_eq!(first.len(), 2);

    // The bid filled. Nothing is resting for it any more, and we are long.
    h.maker_fill("eng-bid", "BTCUSDT", Side::Buy, 0.1, 99.9);
    let after = placed(&mut h);
    assert_eq!(after.len(), 2, "quoted again on the fill, with no new price");
}

#[test]
fn a_long_book_leans_its_quotes_down_and_a_short_one_leans_them_up() {
    // Both quotes move, so the side that would sell us out of the position
    // gets easier to hit and the side that would add to it gets harder.
    let flat = {
        let mut h = quoter_over(&["BTCUSDT"], 5.0);
        h.quote("BTCUSDT", 99.0, 101.0);
        placed(&mut h)
    };
    let long = {
        let mut h = quoter_over(&["BTCUSDT"], 5.0);
        h.ctx.set_my_position("BTCUSDT", 0.15);
        h.quote("BTCUSDT", 99.0, 101.0);
        placed(&mut h)
    };
    let short = {
        let mut h = quoter_over(&["BTCUSDT"], 5.0);
        h.ctx.set_my_position("BTCUSDT", -0.15);
        h.quote("BTCUSDT", 99.0, 101.0);
        placed(&mut h)
    };
    for side in [Side::Buy, Side::Sell] {
        assert!(price_of(&long, side) < price_of(&flat, side), "long leans down on {side:?}");
        assert!(price_of(&short, side) > price_of(&flat, side), "short leans up on {side:?}");
    }
}

#[test]
fn one_plug_quotes_every_symbol_it_was_given() {
    let mut h = quoter_over(&["BTCUSDT", "ETHUSDT"], 0.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    // Priced well clear of the venue's 5 USDT minimum on both sides: at 50 a
    // 0.1 bid is 4.995, which the plug is right to refuse and which would
    // make this test about the minimum instead of about two symbols.
    h.quote("ETHUSDT", 199.0, 201.0);
    let intents = placed(&mut h);
    assert_eq!(intents.len(), 4, "two sides each");
    let btc = h.ctx.id_of("BTCUSDT");
    let eth = h.ctx.id_of("ETHUSDT");
    assert_eq!(intents.iter().filter(|i| i.symbol == btc).count(), 2);
    assert_eq!(intents.iter().filter(|i| i.symbol == eth).count(), 2);
}

#[test]
fn a_price_in_a_symbol_this_plug_was_not_given_is_ignored() {
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    h.ctx.set_rule("ETHUSDT", RULE);
    h.quote("ETHUSDT", 49.0, 51.0);
    assert!(h.drain_actions().is_empty(), "not this plug's symbol");
}

#[test]
fn a_name_another_sleeve_is_holding_is_left_alone() {
    // There is one venue stop per position, so two sleeves in one symbol
    // would have one stop between them, set by whoever opened last. Ours come
    // out and nothing new goes in.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    let symbol = h.ctx.id_of("BTCUSDT");
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
    h.ctx.set_foreign_position("BTCUSDT", Side::Buy, 1.0, 100.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    let actions = h.drain_actions();
    assert_eq!(actions.len(), 1, "one cancel and nothing else: {actions:?}");
    assert!(matches!(actions[0], Action::Cancel { .. }));
}

#[test]
fn a_lean_wider_than_the_quote_itself_is_refused() {
    // At full inventory it would put the far side of the quote through the
    // market, which is a taker wearing a maker's config.
    let bad: toml::Value = toml::from_str(
        r#"
        symbols = ["BTCUSDT"]
        half_spread_bps = 10.0
        requote_bps = 2.0
        skew_bps = 20.0
        qty = 0.1
        max_position = 0.3
        stop_loss_fraction = 0.35
        "#,
    )
    .expect("test config parses");
    let Err(err) = Quoter::from_params(engine_types::StrategyId(0), &bad) else {
        panic!("a lean wider than the half-spread should be refused");
    };
    assert!(format!("{err}").contains("skew_bps"), "{err}");
}

#[test]
fn an_empty_symbol_list_is_refused() {
    let bad: toml::Value = toml::from_str(
        r#"
        symbols = []
        half_spread_bps = 10.0
        requote_bps = 2.0
        qty = 0.1
        max_position = 0.3
        stop_loss_fraction = 0.35
        "#,
    )
    .expect("test config parses");
    assert!(Quoter::from_params(engine_types::StrategyId(0), &bad).is_err());
}
