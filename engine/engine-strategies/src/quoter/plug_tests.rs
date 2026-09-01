//! The quoting plug on the engine's own contracts: what it emits, and which
//! verb it reaches for. The arithmetic is tested in `plan.rs`; these check the
//! wiring — that a maker can place, move and pull through the engine.

use engine_types::{Action, EngineEvent, Feed, InstrumentRule, OrderKind, OrderUpdate, Side};

use super::plug::Quoter;
use crate::mock_ctx::{Harness, RestingSeed};

const RULE: InstrumentRule = InstrumentRule {
    tick_size: 0.01,
    qty_step: 0.001,
    min_qty: 0.001,
    min_notional: 5.0,
};

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
            matches!(
                intent.kind,
                OrderKind::Limit {
                    tif: engine_types::TimeInForce::PostOnly,
                    ..
                }
            ),
            "a maker that crosses is not a maker"
        );
        assert!(
            intent.stop.is_some(),
            "the kernel refuses an opening order with no stop"
        );
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
        kind: OrderKind::Limit {
            px: 90.0,
            tif: engine_types::TimeInForce::PostOnly,
        },
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
        Action::Amend {
            client_order_id,
            spec,
            ..
        } => {
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
        kind: OrderKind::Limit {
            px: 99.9,
            tif: engine_types::TimeInForce::PostOnly,
        },
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
    assert_eq!(
        cancels.len(),
        1,
        "the side that would breach the ceiling comes out"
    );
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
            kind: OrderKind::Limit {
                px: 100.0,
                tif: engine_types::TimeInForce::PostOnly,
            },
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
        InstrumentRule {
            tick_size: 0.01,
            qty_step: 0.001,
            min_qty: 0.001,
            min_notional: 5000.0,
        },
    );
    h.quote("BTCUSDT", 99.0, 101.0);
    assert!(
        h.drain_actions().is_empty(),
        "1 USDT of notional is not a quote"
    );
}

#[test]
fn a_requote_tolerance_wider_than_the_quote_is_refused() {
    // It would mean a quote that can never be worth moving.
    let bad: toml::Value = toml::from_str(
        r#"
        symbols = ["BTCUSDT"]
        half_spread_bps = 2.0
        requote_bps = 10.0
        qty = 0.1
        max_position = 0.3
        stop_loss_fraction = 0.35
    "#,
    )
    .expect("test config parses");
    let Err(err) = Quoter::from_params(engine_types::StrategyId(0), &bad) else {
        panic!("a tolerance wider than the quote should be refused");
    };
    assert!(format!("{err}").contains("requote_bps"), "{err}");
}

#[test]
fn a_stop_fraction_of_one_is_refused() {
    let bad: toml::Value = toml::from_str(
        r#"
        symbols = ["BTCUSDT"]
        half_spread_bps = 10.0
        requote_bps = 2.0
        qty = 0.1
        max_position = 0.3
        stop_loss_fraction = 1.0
    "#,
    )
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
    h.ctx.set_hand_position("BTCUSDT", Side::Buy, 0.3, 100.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    let intents = placed(&mut h);
    assert_eq!(
        intents.len(),
        2,
        "our own book is flat, so both sides are quoted"
    );
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
    assert!(intents[0].reduce_only);
    assert!(intents[0].stop.is_none());
}

#[test]
fn the_inventory_exit_is_reduce_only_and_capped_at_what_is_held() {
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    h.ctx.set_my_position("BTCUSDT", 0.05);
    h.quote("BTCUSDT", 99.0, 101.0);
    let intents = placed(&mut h);
    let exit = intents
        .iter()
        .find(|intent| intent.side == Side::Sell)
        .expect("long inventory needs an ask");
    assert!(exit.reduce_only);
    assert_eq!(exit.qty, 0.05, "the ask cannot sell through flat");
    assert!(
        exit.stop.is_none(),
        "an exit does not replace the position stop"
    );

    let bid = intents
        .iter()
        .find(|intent| intent.side == Side::Buy)
        .expect("inventory below the ceiling may still quote a bid");
    assert!(!bid.reduce_only);
    assert!(bid.stop.is_some());
}

#[test]
fn an_opening_quote_on_the_new_exit_side_is_cancelled_before_replacement() {
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.set_my_position("BTCUSDT", 0.05);
    h.ctx.resting.push(RestingSeed {
        client_order_id: "flat-book-ask".into(),
        symbol,
        side: Side::Sell,
        kind: OrderKind::Limit {
            px: 100.1,
            tif: engine_types::TimeInForce::PostOnly,
        },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });

    h.quote("BTCUSDT", 99.0, 101.0);
    let actions = h.drain_actions();
    assert_eq!(actions.len(), 1, "replacement waits for the cancel receipt");
    assert!(matches!(
        &actions[0],
        Action::Cancel { client_order_id, .. } if client_order_id == "flat-book-ask"
    ));
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
    assert_eq!(
        after.len(),
        2,
        "quoted again on the fill, with no new price"
    );
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
        assert!(
            price_of(&long, side) < price_of(&flat, side),
            "long leans down on {side:?}"
        );
        assert!(
            price_of(&short, side) > price_of(&flat, side),
            "short leans up on {side:?}"
        );
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
        kind: OrderKind::Limit {
            px: 99.9,
            tif: engine_types::TimeInForce::PostOnly,
        },
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

#[test]
fn one_order_is_asked_to_cancel_once_however_many_prices_arrive() {
    // An order stays in this strategy's own book until the log records it
    // cancelled, and the venue takes a round trip to say so. Without pacing,
    // every price in between asked again -- ten prices, ten signed venue
    // calls for one order, and a liquid symbol delivers far more than ten a
    // second.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.resting.push(RestingSeed {
        client_order_id: "eng-bid".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit {
            px: 99.9,
            tif: engine_types::TimeInForce::PostOnly,
        },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    h.ctx.set_foreign_position("BTCUSDT", Side::Buy, 1.0, 100.0);
    for i in 0..10 {
        h.quote("BTCUSDT", 99.0 + i as f64 * 0.01, 101.0);
    }
    let cancels = h
        .drain_actions()
        .into_iter()
        .filter(|a| matches!(a, Action::Cancel { .. }))
        .count();
    assert_eq!(cancels, 1, "one order, one ask");
}

#[test]
fn a_feed_reset_pulls_every_resting_quote() {
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    let symbol = h.ctx.id_of("BTCUSDT");
    for (id, side, px) in [("eng-bid", Side::Buy, 99.9), ("eng-ask", Side::Sell, 100.1)] {
        h.ctx.resting.push(RestingSeed {
            client_order_id: id.into(),
            symbol,
            side,
            kind: OrderKind::Limit {
                px,
                tif: engine_types::TimeInForce::PostOnly,
            },
            qty: 0.1,
            filled_qty: 0.0,
            reduce_only: false,
            acked: true,
        });
    }
    h.feed_reset();
    assert_eq!(cancel_count(&mut h), 2);
}

#[test]
fn boot_pulls_every_recovered_maker_order_before_market_news() {
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    let symbol = h.ctx.id_of("BTCUSDT");
    for (id, side, px) in [("eng-bid", Side::Buy, 99.9), ("eng-ask", Side::Sell, 100.1)] {
        h.ctx.resting.push(RestingSeed {
            client_order_id: id.into(),
            symbol,
            side,
            kind: OrderKind::Limit {
                px,
                tif: engine_types::TimeInForce::PostOnly,
            },
            qty: 0.1,
            filled_qty: 0.0,
            reduce_only: false,
            acked: true,
        });
    }

    h.boot();

    let actions = h.drain_actions();
    assert_eq!(
        actions
            .iter()
            .filter(|action| matches!(action, Action::Cancel { .. }))
            .count(),
        2
    );
    assert!(
        actions
            .iter()
            .all(|action| !matches!(action, Action::Place(_))),
        "boot waits for fresh market data before quoting"
    );
}

#[test]
fn enabled_boot_preserves_a_recovered_reduce_only_drain() {
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.set_my_position("BTCUSDT", 0.05);
    h.ctx.resting.push(RestingSeed {
        client_order_id: "recovered-drain".into(),
        symbol,
        side: Side::Sell,
        kind: OrderKind::Market,
        qty: 0.05,
        filled_qty: 0.0,
        reduce_only: true,
        acked: false,
    });

    h.boot();

    assert!(
        h.drain_actions().is_empty(),
        "boot keeps the durable drain and waits for fresh market data"
    );
}

#[test]
fn boot_discards_fast_fill_inventory_that_the_durable_ledger_does_not_own() {
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    h.fast_fill(
        "fast-before-restart",
        "eng-bid",
        "BTCUSDT",
        Side::Buy,
        0.3,
        100.0,
    );
    let _ = h.drain_actions();

    h.boot();
    assert!(h.drain_actions().is_empty());
    h.quote("BTCUSDT", 99.0, 101.0);

    let intents = placed(&mut h);
    assert_eq!(
        intents.len(),
        2,
        "restart inventory comes from durable attribution, not the fast-fill bridge"
    );
}

#[test]
fn a_full_side_is_not_asked_to_cancel_on_every_price_either() {
    // The same hole on the path that predates the foreign-name branch: at the
    // inventory ceiling the planner pulls the side on every single requote.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.resting.push(RestingSeed {
        client_order_id: "eng-bid".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit {
            px: 99.9,
            tif: engine_types::TimeInForce::PostOnly,
        },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    h.ctx.set_my_position("BTCUSDT", 0.3);
    for _ in 0..10 {
        h.quote("BTCUSDT", 99.0, 101.0);
    }
    let cancels = h
        .drain_actions()
        .into_iter()
        .filter(|a| matches!(a, Action::Cancel { .. }))
        .count();
    assert_eq!(cancels, 1, "one order, one ask");
}

#[test]
fn a_cancel_the_venue_never_took_is_asked_again_after_a_while() {
    // Paced, not latched. A cancel the venue refused leaves the order resting,
    // and a strategy that asked once and never again would leave it there for
    // good.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.resting.push(RestingSeed {
        client_order_id: "eng-bid".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit {
            px: 99.9,
            tif: engine_types::TimeInForce::PostOnly,
        },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    h.ctx.set_foreign_position("BTCUSDT", Side::Buy, 1.0, 100.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    assert_eq!(cancel_count(&mut h), 1, "still inside the pacing window");

    // The order is still resting a second and a half later: the venue never
    // took it.
    h.ctx
        .set_now(engine_types::StrategyCtx::now_ns(&h.ctx) + 1_500_000_000);
    h.quote("BTCUSDT", 99.0, 101.0);
    assert_eq!(
        cancel_count(&mut h),
        1,
        "asked again once the window passed"
    );
}

#[test]
fn an_order_that_went_away_is_forgotten_rather_than_remembered_for_ever() {
    // The record of who has been asked is pruned to what is still working, so
    // a long-running quoter does not accumulate one entry per order it ever
    // pulled.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.resting.push(RestingSeed {
        client_order_id: "eng-bid".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit {
            px: 99.9,
            tif: engine_types::TimeInForce::PostOnly,
        },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    h.ctx.set_foreign_position("BTCUSDT", Side::Buy, 1.0, 100.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    assert_eq!(cancel_count(&mut h), 1);

    // The venue took it, so the engine's book no longer carries it. A new
    // order with the same id would be a new order, and must be askable.
    h.ctx.resting.clear();
    h.quote("BTCUSDT", 99.0, 101.0);
    h.ctx.resting.push(RestingSeed {
        client_order_id: "eng-bid".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit {
            px: 99.9,
            tif: engine_types::TimeInForce::PostOnly,
        },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    h.quote("BTCUSDT", 99.0, 101.0);
    assert_eq!(
        cancel_count(&mut h),
        1,
        "a fresh order is asked, not skipped"
    );
}

fn cancel_count(h: &mut Harness) -> usize {
    h.drain_actions()
        .into_iter()
        .filter(|a| matches!(a, Action::Cancel { .. }))
        .count()
}

fn amend_count(h: &mut Harness) -> usize {
    h.drain_actions()
        .into_iter()
        .filter(|a| matches!(a, Action::Amend { .. }))
        .count()
}

fn seed_bid(h: &mut Harness, id: &str, symbol: &str, px: f64) {
    let id_of = h.ctx.id_of(symbol);
    h.ctx.resting.push(RestingSeed {
        client_order_id: id.into(),
        symbol: id_of,
        side: Side::Buy,
        kind: OrderKind::Limit {
            px,
            tif: engine_types::TimeInForce::PostOnly,
        },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
}

#[test]
fn an_order_is_asked_to_move_once_for_one_move_of_the_market() {
    // Several market events can arrive before an amend acknowledgement. The
    // strategy remembers its request during that window so one market move
    // cannot fan out into duplicate signed venue calls.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    seed_bid(&mut h, "eng-bid", "BTCUSDT", 99.90);
    for _ in 0..10 {
        // A market that has moved once and then sits still.
        h.quote("BTCUSDT", 99.1, 101.1);
    }
    assert_eq!(amend_count(&mut h), 1, "one move of the market, one ask");
}

#[test]
fn a_market_that_keeps_moving_keeps_the_quote_with_it() {
    // The other half: not asking twice for the same price must not become not
    // asking at all.
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    seed_bid(&mut h, "eng-bid", "BTCUSDT", 99.90);
    h.quote("BTCUSDT", 99.1, 101.1);
    assert_eq!(amend_count(&mut h), 1);
    h.quote("BTCUSDT", 105.0, 107.0);
    assert_eq!(
        amend_count(&mut h),
        1,
        "the market moved again, so we move again"
    );
}

#[test]
fn pacing_one_symbol_does_not_forget_another() {
    // The map of who has been asked is keyed by order id across every symbol
    // this plug quotes, and was pruned against one symbol's slice of the book.
    // So a price in BTCUSDT evicted ETHUSDT's record and the next ETH price
    // asked again -- the cancel loop, back, on a two-symbol config.
    let mut h = quoter_over(&["BTCUSDT", "ETHUSDT"], 0.0);
    seed_bid(&mut h, "eng-eth", "ETHUSDT", 199.0);
    h.ctx.set_foreign_position("ETHUSDT", Side::Buy, 1.0, 200.0);

    h.quote("ETHUSDT", 199.0, 201.0);
    assert_eq!(cancel_count(&mut h), 1, "asked once");
    // A price in the other symbol, interleaved as they are on a live feed.
    h.quote("BTCUSDT", 99.0, 101.0);
    let _ = h.drain_actions();
    h.quote("ETHUSDT", 199.0, 201.0);
    assert_eq!(
        cancel_count(&mut h),
        0,
        "still inside the window, still asked"
    );
}

fn micro_bench(extra: &str) -> Harness {
    let src = format!(
        r#"
        symbols = ["BTCUSDT"]
        half_spread_bps = 5.0
        requote_bps = 1.0
        qty_usdt = 10.0
        max_position_usdt = 30.0
        stop_loss_fraction = 0.35
        signal_half_life_ms = 250.0
        {extra}
        "#
    );
    let config: toml::Value = toml::from_str(&src).expect("micro config parses");
    let quoter = Quoter::from_params(engine_types::StrategyId(0), &config).unwrap();
    let mut h = Harness::new(Box::new(quoter));
    h.ctx.set_rule("BTCUSDT", RULE);
    h
}

fn toxic_bench(extra: &str) -> Harness {
    micro_bench(&format!(
        r#"
        flow_fast_half_life_ms = 250.0
        flow_slow_half_life_ms = 3000.0
        flow_fast_weight = 0.65
        flow_slow_weight = 0.35
        flow_response_bps = 2.0
        flow_max_widen_bps = 8.0
        flow_depth_bps = 10.0
        flow_volatility_depth_multiplier = 2.0
        flow_max_score = 4.0
        {extra}
        "#
    ))
}

fn placed_orders(actions: Vec<Action>) -> Vec<(Side, f64, f64)> {
    actions
        .into_iter()
        .filter_map(|action| match action {
            Action::Place(intent) => match intent.kind {
                OrderKind::Limit { px, .. } => Some((intent.side, px, intent.qty)),
                OrderKind::Market => None,
            },
            _ => None,
        })
        .collect()
}

fn fire_next_timer(h: &mut Harness) {
    let timer = h.ctx.timers.pop().expect("the retry timer is armed");
    h.ctx.set_now(timer.due_ns);
    h.strategy.on_event(
        &EngineEvent::Timer {
            id: timer.id,
            now_ns: timer.due_ns,
        },
        &mut h.ctx,
    );
}

fn side_px(orders: &[(Side, f64, f64)], side: Side) -> f64 {
    orders
        .iter()
        .find(|order| order.0 == side)
        .map(|order| order.1)
        .unwrap_or_else(|| panic!("no {side:?} in {orders:?}"))
}

#[test]
fn the_live_quoter_asks_for_the_touch_the_deep_book_and_aggressor_trades() {
    // Three topics, each for a different job: the touch for the freshest bid
    // and ask, the deep book for fair value and queue pressure, the trades
    // for who is crossing. The venue publishes the touch about twice as
    // often as the deep book, so taking the price from the deep book alone
    // spends up to one publication interval quoting against an old touch.
    let h = micro_bench("maker_fee_bps = 2.0");
    let subscriptions = h.strategy.subscriptions();
    assert_eq!(subscriptions.len(), 3);
    for feed in [Feed::Quote, Feed::Depth, Feed::Trades] {
        assert!(
            subscriptions.iter().any(|sub| sub.feed == feed),
            "{feed:?} is missing from {subscriptions:?}"
        );
    }
}

#[test]
fn enabled_boot_drains_retired_inventory_but_not_an_active_symbol() {
    let mut h = micro_bench("");
    h.ctx.set_my_position("BTCUSDT", 0.02);
    h.ctx.set_my_position("OLDUSDT", -0.04);
    let retired = h.ctx.id_of("OLDUSDT");

    h.boot();

    let exits = placed(&mut h);
    assert_eq!(exits.len(), 1);
    assert_eq!(exits[0].symbol, retired);
    assert_eq!(exits[0].side, Side::Buy);
    assert_eq!(exits[0].qty, 0.04);
    assert!(exits[0].reduce_only);
    assert!(matches!(exits[0].kind, OrderKind::Market));
}

#[test]
fn enabled_feed_reset_drains_retired_inventory_but_not_an_active_symbol() {
    let mut h = micro_bench("");
    h.ctx.set_my_position("BTCUSDT", 0.02);
    h.ctx.set_my_position("OLDUSDT", -0.04);
    let retired = h.ctx.id_of("OLDUSDT");

    h.feed_reset();

    let exits = placed(&mut h);
    assert_eq!(exits.len(), 1);
    assert_eq!(exits[0].symbol, retired);
    assert_eq!(exits[0].side, Side::Buy);
    assert_eq!(exits[0].qty, 0.04);
    assert!(exits[0].reduce_only);
    assert!(matches!(exits[0].kind, OrderKind::Market));
}

#[test]
fn enabled_boot_and_feed_reset_keep_a_recovered_retired_drain() {
    let mut h = micro_bench("");
    h.ctx.set_my_position("OLDUSDT", -0.04);
    let retired = h.ctx.id_of("OLDUSDT");
    h.ctx.resting.push(RestingSeed {
        client_order_id: "recovered-retired-drain".into(),
        symbol: retired,
        side: Side::Buy,
        kind: OrderKind::Market,
        qty: 0.04,
        filled_qty: 0.0,
        reduce_only: true,
        acked: false,
    });

    h.boot();

    assert!(
        h.drain_actions().is_empty(),
        "the durable retired-symbol drain remains the only pending exit"
    );

    h.feed_reset();
    assert!(
        h.drain_actions().is_empty(),
        "a feed reset does not duplicate the durable retired-symbol drain"
    );
}

#[test]
fn enabled_retired_inventory_retries_a_refused_drain_until_flat() {
    let mut h = micro_bench("");
    h.ctx.set_my_position("OLDUSDT", -0.04);
    let retired = h.ctx.id_of("OLDUSDT");
    h.boot();
    let initial = placed(&mut h);
    assert_eq!(initial.len(), 1);
    assert_eq!(initial[0].symbol, retired);

    h.strategy.on_event(
        &EngineEvent::IntentRefused {
            symbol: retired,
            reduce_only: true,
            reason: "test refusal".into(),
        },
        &mut h.ctx,
    );

    assert!(
        h.drain_actions().is_empty(),
        "the refusal cannot feed another exit into the same engine wake"
    );
    assert_eq!(h.ctx.arm_calls.len(), 1);
    assert_eq!(
        h.ctx.arm_calls[0].due_ns - h.ctx.arm_calls[0].armed_ns,
        1_000_000_000
    );
    h.strategy.on_event(
        &EngineEvent::IntentRefused {
            symbol: retired,
            reduce_only: true,
            reason: "duplicate refusal before the retry is due".into(),
        },
        &mut h.ctx,
    );
    assert!(h.drain_actions().is_empty());
    assert_eq!(
        h.ctx.arm_calls.len(),
        1,
        "repeated terminal news coalesces behind the pending timer"
    );

    fire_next_timer(&mut h);
    let retry = placed(&mut h);
    assert_eq!(retry.len(), 1);
    assert_eq!(retry[0].symbol, retired);
    assert_eq!(retry[0].side, Side::Buy);
    assert_eq!(retry[0].qty, 0.04);
    assert!(retry[0].reduce_only);

    h.ctx.set_my_position("OLDUSDT", 0.0);
    h.strategy.on_event(
        &EngineEvent::IntentRefused {
            symbol: retired,
            reduce_only: true,
            reason: "late refusal after the ledger is flat".into(),
        },
        &mut h.ctx,
    );
    assert!(h.drain_actions().is_empty());
    fire_next_timer(&mut h);
    assert!(
        h.drain_actions().is_empty(),
        "a retired symbol leaves management once its attributed inventory is flat"
    );
}

fn active_reduce_only_order(h: &mut Harness, client_order_id: &str) -> engine_types::SymbolId {
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.resting.push(RestingSeed {
        client_order_id: client_order_id.into(),
        symbol,
        side: Side::Sell,
        kind: OrderKind::Limit {
            px: 100.05,
            tif: engine_types::TimeInForce::PostOnly,
        },
        qty: 0.1,
        filled_qty: 0.0,
        reduce_only: true,
        acked: true,
    });
    symbol
}

fn assert_active_reduction_requoted(h: &mut Harness) {
    let retry = placed(h);
    assert!(
        retry
            .iter()
            .any(|intent| intent.side == Side::Sell && intent.reduce_only),
        "the quiet-feed retry must restore the inventory-reducing quote: {retry:?}"
    );
}

#[test]
fn an_enabled_reduce_only_refusal_retries_on_timer_without_market_news() {
    let mut h = micro_bench("");
    h.ctx.set_my_position("BTCUSDT", 0.05);
    h.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let initial = placed(&mut h);
    assert!(initial
        .iter()
        .any(|intent| intent.side == Side::Sell && intent.reduce_only));
    let symbol = h.ctx.id_of("BTCUSDT");

    h.strategy.on_event(
        &EngineEvent::IntentRefused {
            symbol,
            reduce_only: true,
            reason: "test refusal".into(),
        },
        &mut h.ctx,
    );

    assert!(
        h.drain_actions().is_empty(),
        "the refusal wake must not immediately repeat the quote"
    );
    fire_next_timer(&mut h);
    assert_active_reduction_requoted(&mut h);
}

#[test]
fn an_enabled_reduce_only_reject_retries_on_timer_without_market_news() {
    let mut h = micro_bench("");
    h.ctx.set_my_position("BTCUSDT", 0.05);
    h.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let _ = h.drain_actions();
    active_reduce_only_order(&mut h, "rejected-reduction");

    h.strategy.on_event(
        &EngineEvent::Order(OrderUpdate::Reject {
            client_order_id: "rejected-reduction".into(),
            code: 110001,
            reason: "test rejection".into(),
        }),
        &mut h.ctx,
    );

    assert!(
        h.drain_actions().is_empty(),
        "the rejection wake must not immediately repeat the quote"
    );
    h.ctx.resting.clear();
    fire_next_timer(&mut h);
    assert_active_reduction_requoted(&mut h);
}

#[test]
fn an_enabled_reduce_only_cancel_retries_on_timer_without_market_news() {
    let mut h = micro_bench("");
    h.ctx.set_my_position("BTCUSDT", 0.05);
    h.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let _ = h.drain_actions();
    active_reduce_only_order(&mut h, "cancelled-reduction");

    h.strategy.on_event(
        &EngineEvent::Order(OrderUpdate::Cancelled {
            client_order_id: "cancelled-reduction".into(),
            recv_ns: 1_000,
        }),
        &mut h.ctx,
    );

    assert!(
        h.drain_actions().is_empty(),
        "the cancellation wake must not immediately repeat the quote"
    );
    h.ctx.resting.clear();
    fire_next_timer(&mut h);
    assert_active_reduction_requoted(&mut h);
}

#[test]
fn a_disabled_quoter_drains_once_instead_of_leaving_inventory_behind() {
    let mut h = micro_bench("quote_enabled = false");
    h.ctx.set_my_position("BTCUSDT", 0.05);
    h.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    h.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let exits: Vec<_> = h
        .drain_actions()
        .into_iter()
        .filter_map(|action| match action {
            Action::Place(intent) => Some(intent),
            _ => None,
        })
        .collect();
    assert_eq!(
        exits.len(),
        1,
        "one outstanding drain, not one per book update"
    );
    assert!(exits[0].reduce_only);
    assert_eq!(exits[0].side, Side::Sell);
    assert_eq!(exits[0].qty, 0.05);
    assert!(matches!(exits[0].kind, OrderKind::Market));
    assert!(exits[0].stop.is_none());
}

#[test]
fn a_disabled_quoter_drains_recovered_inventory_on_boot_without_market_news() {
    let mut h = micro_bench("quote_enabled = false");
    h.ctx.set_my_position("BTCUSDT", 0.05);

    h.boot();

    let exits: Vec<_> = h
        .drain_actions()
        .into_iter()
        .filter_map(|action| match action {
            Action::Place(intent) => Some(intent),
            _ => None,
        })
        .collect();
    assert_eq!(exits.len(), 1);
    assert_eq!(exits[0].side, Side::Sell);
    assert_eq!(exits[0].qty, 0.05);
    assert!(exits[0].reduce_only);
    assert!(matches!(exits[0].kind, OrderKind::Market));
}

#[test]
fn a_disabled_boot_does_not_duplicate_a_recovered_drain_order() {
    let mut h = micro_bench("quote_enabled = false");
    let symbol = h.ctx.id_of("BTCUSDT");
    h.ctx.set_my_position("BTCUSDT", 0.05);
    h.ctx.resting.push(RestingSeed {
        client_order_id: "recovered-drain".into(),
        symbol,
        side: Side::Sell,
        kind: OrderKind::Market,
        qty: 0.05,
        filled_qty: 0.0,
        reduce_only: true,
        acked: false,
    });

    h.boot();

    let actions = h.drain_actions();
    assert_eq!(
        actions
            .iter()
            .filter(|action| matches!(action, Action::Cancel { .. }))
            .count(),
        0,
        "the recovered reduce-only drain remains live across restart"
    );
    assert!(
        actions
            .iter()
            .all(|action| !matches!(action, Action::Place(_))),
        "the durable reduce-only order is the pending drain"
    );
}

#[test]
fn a_disabled_boot_finds_inventory_outside_the_current_quote_list() {
    let mut h = micro_bench("quote_enabled = false");
    h.ctx.set_my_position("OLDUSDT", -0.04);

    h.boot();

    let exit = h
        .drain_actions()
        .into_iter()
        .find_map(|action| match action {
            Action::Place(intent) => Some(intent),
            _ => None,
        })
        .expect("recovered inventory must be drained");
    assert_eq!(exit.symbol, h.ctx.id_of("OLDUSDT"));
    assert_eq!(exit.side, Side::Buy);
    assert_eq!(exit.qty, 0.04);
    assert!(exit.reduce_only);
    assert!(matches!(exit.kind, OrderKind::Market));
}

#[test]
fn a_refused_recovered_drain_is_retried_outside_the_current_quote_list() {
    let mut h = micro_bench("quote_enabled = false");
    h.ctx.set_my_position("OLDUSDT", -0.04);
    let symbol = h.ctx.id_of("OLDUSDT");
    h.boot();
    let _ = h.drain_actions();

    h.strategy.on_event(
        &EngineEvent::IntentRefused {
            symbol,
            reduce_only: true,
            reason: "test refusal".into(),
        },
        &mut h.ctx,
    );

    assert!(
        h.drain_actions().is_empty(),
        "a recovered drain also leaves the refusal wake before retrying"
    );
    fire_next_timer(&mut h);
    let retry = h
        .drain_actions()
        .into_iter()
        .find_map(|action| match action {
            Action::Place(intent) => Some(intent),
            _ => None,
        })
        .expect("durable recovered inventory must remain managed after refusal");
    assert_eq!(retry.symbol, symbol);
    assert_eq!(retry.side, Side::Buy);
    assert_eq!(retry.qty, 0.04);
    assert!(retry.reduce_only);
}

#[test]
fn boot_forgets_pre_restart_microstructure_flow() {
    let mut restarted = toxic_bench("");
    restarted.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let _ = restarted.drain_actions();
    restarted.trades("BTCUSDT", 10.0, 0.0, 100.0);
    let _ = restarted.drain_actions();
    restarted.boot();
    assert!(restarted.drain_actions().is_empty());
    restarted.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let after_restart = placed_orders(restarted.drain_actions());

    let mut fresh = toxic_bench("");
    fresh.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let fresh_start = placed_orders(fresh.drain_actions());

    assert_eq!(after_restart, fresh_start);
}

#[test]
fn buy_aggressors_move_both_quotes_up() {
    let mut h = micro_bench("trade_lean_bps = 8.0");
    h.depth(
        "BTCUSDT",
        &[(99.9, 1.0), (99.8, 1.0)],
        &[(100.1, 1.0), (100.2, 1.0)],
    );
    let before = placed_orders(h.drain_actions());

    h.ctx
        .set_now(engine_types::StrategyCtx::now_ns(&h.ctx) + 250_000_000);
    h.trades("BTCUSDT", 10.0, 0.0, 100.1);
    let after = placed_orders(h.drain_actions());
    assert!(side_px(&after, Side::Buy) > side_px(&before, Side::Buy));
    assert!(side_px(&after, Side::Sell) > side_px(&before, Side::Sell));
}

#[test]
fn buy_flow_protects_only_the_ask_and_sell_flow_protects_only_the_bid() {
    let initial = || {
        let mut h = toxic_bench("");
        h.depth(
            "BTCUSDT",
            &[(99.9, 1.0), (99.8, 1.0)],
            &[(100.1, 1.0), (100.2, 1.0)],
        );
        let before = placed_orders(h.drain_actions());
        (h, before)
    };

    let (mut buys, before_buy) = initial();
    buys.ctx.set_now(250_000_000);
    buys.trades("BTCUSDT", 2.0, 0.0, 100.1);
    let after_buy = placed_orders(buys.drain_actions());
    assert_eq!(
        side_px(&after_buy, Side::Buy),
        side_px(&before_buy, Side::Buy)
    );
    assert!(side_px(&after_buy, Side::Sell) > side_px(&before_buy, Side::Sell));

    let (mut sells, before_sell) = initial();
    sells.ctx.set_now(250_000_000);
    sells.trades("BTCUSDT", 0.0, 2.0, 99.9);
    let after_sell = placed_orders(sells.drain_actions());
    assert!(side_px(&after_sell, Side::Buy) < side_px(&before_sell, Side::Buy));
    assert_eq!(
        side_px(&after_sell, Side::Sell),
        side_px(&before_sell, Side::Sell)
    );
}

#[test]
fn flow_is_scaled_by_the_nearby_same_side_book() {
    let ask_after = |buy_qty: f64| {
        let mut h = toxic_bench("");
        h.depth("BTCUSDT", &[(99.9, 10.0)], &[(100.1, 10.0)]);
        let _ = h.drain_actions();
        h.ctx.set_now(250_000_000);
        h.trades("BTCUSDT", buy_qty, 0.0, 100.1);
        side_px(&placed_orders(h.drain_actions()), Side::Sell)
    };
    assert!(ask_after(5.0) > ask_after(0.05));
}

#[test]
fn a_large_buy_sweep_pulls_only_the_ask_when_the_config_asks() {
    let mut h = toxic_bench("flow_pull_score = 0.5");
    h.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let _ = h.drain_actions();
    h.ctx.set_now(250_000_000);
    h.trades("BTCUSDT", 2.0, 0.0, 100.1);
    let after = placed_orders(h.drain_actions());
    assert_eq!(after.len(), 1);
    assert_eq!(after[0].0, Side::Buy);
}

#[test]
fn every_quoter_fill_records_the_flow_and_book_state() {
    let mut h = toxic_bench("");
    h.depth("BTCUSDT", &[(99.9, 2.0)], &[(100.1, 3.0)]);
    let _ = h.drain_actions();
    h.ctx.set_now(250_000_000);
    h.trades("BTCUSDT", 1.0, 0.0, 100.1);
    let _ = h.drain_actions();
    h.maker_fill_with_exec("exec-flow", "eng-ask", "BTCUSDT", Side::Sell, 0.1, 100.1);
    let actions = h.drain_actions();
    let features = actions
        .iter()
        .find_map(|action| match action {
            Action::RecordQuoteFill { features } => Some(features),
            _ => None,
        })
        .expect("the fill has a feature receipt");
    assert_eq!(features.exec_id, "exec-flow");
    assert_eq!(features.side, Side::Sell);
    assert!(features.flow_score.is_some_and(|score| score > 0.0));
    assert!(features
        .same_side_depth_usdt
        .is_some_and(|depth| depth > 0.0));
    assert!(features.spread_bps.is_some_and(|spread| spread > 0.0));
    assert!(features.queue_ahead_usdt.is_some());
}

#[test]
fn short_horizon_movement_widens_the_market() {
    let mut h = micro_bench("volatility_multiplier = 2.0");
    h.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let calm = placed_orders(h.drain_actions());
    let calm_width = side_px(&calm, Side::Sell) - side_px(&calm, Side::Buy);

    h.ctx
        .set_now(engine_types::StrategyCtx::now_ns(&h.ctx) + 250_000_000);
    h.depth("BTCUSDT", &[(100.9, 1.0)], &[(101.1, 1.0)]);
    let moving = placed_orders(h.drain_actions());
    let moving_width = side_px(&moving, Side::Sell) - side_px(&moving, Side::Buy);
    assert!(
        moving_width > calm_width * 5.0,
        "{moving_width} vs {calm_width}"
    );
}

#[test]
fn quote_and_position_size_can_be_constant_money_across_names() {
    let mut h = micro_bench("maker_fee_bps = 2.0");
    h.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let orders = placed_orders(h.drain_actions());
    for (_, _, qty) in orders {
        assert!(
            (qty - 0.1).abs() < 1e-12,
            "10 USDT at 100 should be 0.1 base"
        );
    }
}

#[test]
fn a_good_queue_needs_more_edge_before_it_is_abandoned() {
    let mut h = micro_bench("queue_reprice_edge_bps = 4.0");
    seed_bid(&mut h, "eng-bid", "BTCUSDT", 99.93);
    h.depth("BTCUSDT", &[(99.93, 0.1)], &[(100.07, 0.1)]);
    assert_eq!(
        amend_count(&mut h),
        0,
        "two bps is not enough to discard this queue"
    );
}

#[test]
fn the_authoritative_fill_replaces_the_fast_inventory_instead_of_adding_to_it() {
    let mut h = quoter_over(&["BTCUSDT"], 0.0);
    h.quote("BTCUSDT", 99.0, 101.0);
    let _ = h.drain_actions();
    h.fast_fill("exec-1", "eng-bid", "BTCUSDT", Side::Buy, 0.15, 100.0);
    let _ = h.drain_actions();
    h.maker_fill_with_exec("exec-1", "eng-bid", "BTCUSDT", Side::Buy, 0.15, 100.0);
    let orders = placed_orders(h.drain_actions());
    assert!(orders.iter().any(|order| order.0 == Side::Buy));
    assert!(orders.iter().any(|order| order.0 == Side::Sell));
}

#[test]
fn the_touchs_own_topic_moves_the_quotes_without_a_new_deep_book() {
    // The point of taking the touch from its own faster topic: between two
    // deep-book pushes the market moves, the touch topic says so first, and
    // the maker reprices on it instead of holding a quote priced off the
    // older book.
    let mut h = micro_bench("maker_fee_bps = 2.0");
    h.depth("BTCUSDT", &[(99.9, 1.0)], &[(100.1, 1.0)]);
    let first = placed_orders(h.drain_actions());
    let first_bid = side_px(&first, Side::Buy);

    // No new deep book — only the touch, a whole dollar higher, later.
    h.ctx.set_now(2_000_000);
    h.quote("BTCUSDT", 100.9, 101.1);
    let after = h.drain_actions();
    let moved: Vec<_> = after
        .iter()
        .filter_map(|action| match action {
            Action::Amend { spec, .. } => spec.px,
            _ => None,
        })
        .collect();
    let replaced = placed_orders(after.clone());

    let new_bid = moved
        .first()
        .copied()
        .or_else(|| replaced.iter().find(|o| o.0 == Side::Buy).map(|o| o.1))
        .unwrap_or_else(|| panic!("the touch moved a dollar and nothing repriced: {after:?}"));
    assert!(
        new_bid > first_bid + 0.5,
        "repriced to {new_bid} from {first_bid}: the fresher touch did not reach the plan"
    );
}
