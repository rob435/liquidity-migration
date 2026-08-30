use engine_types::{Action, OrderKind, Side, StrategyCheckpoint, StrategyId, TimerId};

use super::NAME;
use crate::build_strategy;
use crate::mock_ctx::{Harness, RestingSeed};

const SYM: &str = "BTCUSDT";
const ID: StrategyId = StrategyId(7);

/// Level 100, size 2, stop 5 away on the losing side. `extra` adds the
/// optional lines a case needs.
fn build(side: &str, extra: &str) -> Harness {
    let stop_px = if side == "buy" { 95.0 } else { 105.0 };
    let src = format!(
        "symbol = \"{SYM}\"\n\
         side = \"{side}\"\n\
         trigger_px = 100.0\n\
         qty = 2.0\n\
         stop_px = {stop_px}\n\
         {extra}\n"
    );
    let params: toml::Value = toml::from_str(&src).expect("test config parses");
    Harness::new(build_strategy(NAME, ID, &params).expect("test config builds"))
}

/// Arrive in Holding: touch the level, acknowledge, fill.
fn entered(side: &str, extra: &str) -> Harness {
    let mut h = build(side, extra);
    let (bid, ask) = if side == "buy" {
        (99.9, 100.0)
    } else {
        (100.0, 100.1)
    };
    h.quote(SYM, bid, ask);
    assert_eq!(h.drain().len(), 1, "the entry should have been sent");
    h.ack("c1");
    let filled = if side == "buy" { Side::Buy } else { Side::Sell };
    h.fill("c1", SYM, filled, 2.0, 100.0);
    assert!(
        h.drain().is_empty(),
        "a fill alone should not emit anything"
    );
    h
}

fn consumed_checkpoint(extra: &str) -> StrategyCheckpoint {
    let mut h = build("buy", extra);
    h.quote(SYM, 99.9, 100.0);
    match h.drain_actions().remove(0) {
        Action::SetStrategyCheckpoint { checkpoint, .. } => checkpoint,
        other => panic!("checkpoint must precede entry, got {other:?}"),
    }
}

fn exit_checkpoint_before_cancel(h: &mut Harness, client_order_id: &str) -> StrategyCheckpoint {
    let mut actions = h.drain_actions();
    assert!(matches!(
        actions.as_slice(),
        [Action::SetStrategyCheckpoint { .. }, Action::Cancel {
            client_order_id: actual,
            ..
        }] if actual == client_order_id
    ));
    match actions.remove(0) {
        Action::SetStrategyCheckpoint { checkpoint, .. } => checkpoint,
        other => panic!("exit checkpoint must precede cancel, got {other:?}"),
    }
}

fn exit_checkpoint_before_place(h: &mut Harness) -> StrategyCheckpoint {
    let mut actions = h.drain_actions();
    assert!(matches!(
        actions.as_slice(),
        [Action::SetStrategyCheckpoint { .. }, Action::Place(intent)] if intent.reduce_only
    ));
    match actions.remove(0) {
        Action::SetStrategyCheckpoint { checkpoint, .. } => checkpoint,
        other => panic!("exit checkpoint must precede exit, got {other:?}"),
    }
}

#[test]
fn the_consumed_arm_is_queued_before_the_entry() {
    let mut h = build("buy", "");
    h.quote(SYM, 99.9, 100.0);
    let actions = h.drain_actions();
    assert!(matches!(
        actions.as_slice(),
        [Action::SetStrategyCheckpoint { .. }, Action::Place(_)]
    ));
}

#[test]
fn a_consumed_checkpoint_cannot_rearm_after_restart() {
    let checkpoint = consumed_checkpoint("");
    let mut restarted = build("buy", "");
    restarted.ctx.set_strategy_checkpoint(SYM, checkpoint);
    restarted.quote(SYM, 99.9, 100.0);
    assert!(
        restarted.drain_actions().is_empty(),
        "a durable consumed arm is done even when no order or position survived"
    );
}

#[test]
fn an_attributed_position_blocks_reentry_without_a_checkpoint() {
    let mut restarted = build("buy", "");
    restarted.ctx.set_my_position(SYM, 2.0);
    restarted.quote(SYM, 99.9, 100.0);
    let actions = restarted.drain_actions();
    assert!(actions
        .iter()
        .any(|action| matches!(action, Action::SetStrategyCheckpoint { .. })));
    assert!(!actions
        .iter()
        .any(|action| { matches!(action, Action::Place(intent) if !intent.reduce_only) }));
}

#[test]
fn a_resting_entry_blocks_reentry_without_a_checkpoint() {
    let mut restarted = build("buy", "");
    let symbol = restarted.ctx.id_of(SYM);
    restarted.ctx.resting.push(RestingSeed {
        client_order_id: "surviving-entry".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Market,
        qty: 2.0,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    restarted.quote(SYM, 99.9, 100.0);
    assert!(!restarted
        .drain_actions()
        .iter()
        .any(|action| { matches!(action, Action::Place(intent) if !intent.reduce_only) }));
}

#[test]
fn a_fingerprint_change_rearms_only_when_flat_and_orderless() {
    let mut old = consumed_checkpoint("");
    old.decision_fingerprint = "different-config".into();
    let mut restarted = build("buy", "");
    restarted.ctx.set_strategy_checkpoint(SYM, old);
    restarted.quote(SYM, 99.9, 100.0);
    assert!(matches!(
        restarted.drain_actions().as_slice(),
        [Action::SetStrategyCheckpoint { .. }, Action::Place(_)]
    ));

    let mut old_with_position = consumed_checkpoint("");
    old_with_position.decision_fingerprint = "different-config".into();
    let mut holding = build("buy", "");
    holding.ctx.set_strategy_checkpoint(SYM, old_with_position);
    holding.ctx.set_my_position(SYM, 2.0);
    holding.quote(SYM, 99.9, 100.0);
    assert!(!holding
        .drain_actions()
        .iter()
        .any(|action| { matches!(action, Action::Place(intent) if !intent.reduce_only) }));
}

#[test]
fn a_restored_ttl_position_without_a_durable_deadline_exits_now() {
    let checkpoint = consumed_checkpoint("ttl_s = 60");
    let mut restarted = build("buy", "ttl_s = 60");
    restarted.ctx.set_strategy_checkpoint(SYM, checkpoint);
    restarted.ctx.set_my_position(SYM, 2.0);
    restarted.quote(SYM, 99.9, 100.0);
    let actions = restarted.drain_actions();
    assert_eq!(
        actions
            .iter()
            .filter(|action| matches!(action, Action::Place(_)))
            .count(),
        1
    );
    assert!(actions.iter().any(|action| {
        matches!(action, Action::Place(intent) if intent.reduce_only && intent.side == Side::Sell)
    }));
}

#[test]
fn a_fill_as_the_first_restart_wake_is_not_counted_twice() {
    let checkpoint = consumed_checkpoint("take_px = 110.0");
    let mut restarted = build("buy", "take_px = 110.0");
    restarted.ctx.set_strategy_checkpoint(SYM, checkpoint);

    // The harness charges attribution before delivery, matching the engine.
    restarted.fill("surviving-entry", SYM, Side::Buy, 2.0, 100.0);
    assert!(restarted.drain_actions().is_empty());
    restarted.quote(SYM, 110.0, 110.2);
    let exit = restarted.one_intent();
    assert_eq!(
        exit.qty, 2.0,
        "the restored fill was already in attribution"
    );
    assert!(exit.reduce_only);
}

#[test]
fn a_restored_partial_entry_is_cancelled_before_the_exit_is_sized() {
    let checkpoint = consumed_checkpoint("take_px = 110.0");
    let mut restarted = build("buy", "take_px = 110.0");
    restarted.ctx.set_strategy_checkpoint(SYM, checkpoint);
    restarted.ctx.set_my_position(SYM, 0.75);
    let symbol = restarted.ctx.id_of(SYM);
    restarted.ctx.resting.push(RestingSeed {
        client_order_id: "part-filled-entry".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Market,
        qty: 2.0,
        filled_qty: 0.75,
        reduce_only: false,
        acked: true,
    });

    restarted.quote(SYM, 110.0, 110.2);
    exit_checkpoint_before_cancel(&mut restarted, "part-filled-entry");

    // A fill racing the cancel becomes part of the final exit size.
    restarted.fill("part-filled-entry", SYM, Side::Buy, 0.25, 100.0);
    assert!(restarted.drain_actions().is_empty());
    restarted.cancelled("part-filled-entry");
    let exit = restarted.one_intent();
    assert!(exit.reduce_only);
    assert_eq!(exit.qty, 1.0);
}

#[test]
fn a_pending_partial_entry_exit_resumes_after_restart_below_the_take() {
    let mut live = build("buy", "take_px = 110.0");
    live.quote(SYM, 99.9, 100.0);
    live.drain_actions();
    live.ack("entry");
    live.fill("entry", SYM, Side::Buy, 0.5, 100.0);
    live.quote(SYM, 110.0, 110.2);
    let checkpoint = exit_checkpoint_before_cancel(&mut live, "entry");

    let mut restarted = build("buy", "take_px = 110.0");
    restarted.ctx.set_strategy_checkpoint(SYM, checkpoint);
    restarted.ctx.set_my_position(SYM, 0.5);
    let symbol = restarted.ctx.id_of(SYM);
    restarted.ctx.resting.push(RestingSeed {
        client_order_id: "entry".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Market,
        qty: 2.0,
        filled_qty: 0.5,
        reduce_only: false,
        acked: true,
    });

    restarted.quote(SYM, 105.0, 105.2);
    assert!(matches!(
        restarted.one_action(),
        Action::Cancel {
            client_order_id,
            ..
        } if client_order_id == "entry"
    ));
}

#[test]
fn a_refused_or_rejected_exit_restarts_as_an_exit_below_the_take() {
    for venue_reject in [false, true] {
        let mut live = entered("buy", "take_px = 110.0");
        live.quote(SYM, 110.0, 110.2);
        let checkpoint = exit_checkpoint_before_place(&mut live);
        if venue_reject {
            live.ack("exit");
            live.reject("exit", "test rejection");
        } else {
            let symbol = live.ctx.id_of(SYM);
            live.refuse(symbol, true);
        }

        let mut restarted = build("buy", "take_px = 110.0");
        restarted.ctx.set_strategy_checkpoint(SYM, checkpoint);
        restarted.ctx.set_my_position(SYM, 2.0);
        restarted.quote(SYM, 105.0, 105.2);
        let exit = restarted.one_intent();
        assert!(exit.reduce_only);
        assert_eq!(exit.qty, 2.0);
    }
}

#[test]
fn an_unconfirmed_entry_cancel_is_retried_on_a_timer() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    h.drain_actions();
    h.ack("entry");
    h.fill("entry", SYM, Side::Buy, 0.5, 100.0);
    h.quote(SYM, 110.0, 110.2);
    exit_checkpoint_before_cancel(&mut h, "entry");

    assert!(h.fire_next_timer());
    assert!(matches!(
        h.one_action(),
        Action::Cancel {
            client_order_id,
            ..
        } if client_order_id == "entry"
    ));
}

#[test]
fn a_matching_but_invalid_checkpoint_fails_closed() {
    let valid = consumed_checkpoint("");
    for checkpoint in [
        StrategyCheckpoint {
            payload: b"not-json".to_vec(),
            ..valid.clone()
        },
        StrategyCheckpoint {
            schema_version: valid.schema_version + 1,
            ..valid.clone()
        },
        StrategyCheckpoint {
            payload: br#"{"consumed":false,"ttl_due_wall_ms":null}"#.to_vec(),
            ..valid.clone()
        },
    ] {
        let mut restarted = build("buy", "");
        restarted.ctx.set_strategy_checkpoint(SYM, checkpoint);
        restarted.quote(SYM, 99.9, 100.0);
        let actions = restarted.drain_actions();
        assert!(actions
            .iter()
            .any(|action| matches!(action, Action::SetStrategyCheckpoint { .. })));
        assert!(!actions
            .iter()
            .any(|action| matches!(action, Action::Place(intent) if !intent.reduce_only)));
    }
}

#[test]
fn uncertain_checkpoint_identity_closes_a_partial_entry_below_the_take() {
    let valid = consumed_checkpoint("take_px = 110.0");
    let mut mismatch = valid.clone();
    mismatch.decision_fingerprint = "different-config".into();
    let malformed = StrategyCheckpoint {
        payload: b"not-json".to_vec(),
        ..valid
    };

    for checkpoint in [mismatch, malformed] {
        let mut restarted = build("buy", "take_px = 110.0");
        restarted.ctx.set_strategy_checkpoint(SYM, checkpoint);
        restarted.ctx.set_my_position(SYM, 0.5);
        let symbol = restarted.ctx.id_of(SYM);
        restarted.ctx.resting.push(RestingSeed {
            client_order_id: "entry".into(),
            symbol,
            side: Side::Buy,
            kind: OrderKind::Market,
            qty: 2.0,
            filled_qty: 0.5,
            reduce_only: false,
            acked: true,
        });

        restarted.quote(SYM, 105.0, 105.2);
        exit_checkpoint_before_cancel(&mut restarted, "entry");
    }
}

#[test]
fn a_version_one_checkpoint_without_the_exit_latch_still_restores() {
    let mut checkpoint = consumed_checkpoint("");
    checkpoint.payload = br#"{"consumed":true,"ttl_due_wall_ms":null}"#.to_vec();
    let mut restarted = build("buy", "");
    restarted.ctx.set_strategy_checkpoint(SYM, checkpoint);
    restarted.quote(SYM, 99.9, 100.0);
    assert!(restarted.drain_actions().is_empty());
}

#[test]
fn a_missing_book_side_is_never_a_touch() {
    // An empty side renders as price 0. Zero is below every buy level, so
    // without the guard a buy would fire into a book with nothing to hit.
    let mut h = build("buy", "");
    h.quote(SYM, 99.0, 0.0);
    assert!(h.drain().is_empty(), "a buy fired on an empty ask side");

    let mut h = entered("sell", "take_px = 95.0");
    h.quote(SYM, 96.0, 0.0);
    assert!(
        h.drain().is_empty(),
        "a sell's profit exit fired on an empty ask side"
    );
}

#[test]
fn name_is_the_registry_name() {
    let h = build("buy", "");
    assert_eq!(h.strategy.name(), "touch_sniper");
}

#[test]
fn subscriptions_ask_for_quotes_on_its_symbol() {
    let h = build("buy", "");
    let subs = h.strategy.subscriptions();
    assert_eq!(subs.len(), 1);
    assert_eq!(subs[0].symbol, SYM);
    assert_eq!(subs[0].feed, engine_types::Feed::Quote);
}

#[test]
fn entry_triggers_on_the_touch_and_not_before() {
    struct Case {
        what: &'static str,
        side: &'static str,
        bid: f64,
        ask: f64,
        fires: bool,
    }
    let cases = [
        Case {
            what: "buy: ask exactly at the level",
            side: "buy",
            bid: 99.9,
            ask: 100.0,
            fires: true,
        },
        Case {
            what: "buy: ask gapped through the level",
            side: "buy",
            bid: 98.0,
            ask: 99.5,
            fires: true,
        },
        Case {
            what: "buy: ask a tick above the level",
            side: "buy",
            bid: 99.9,
            ask: 100.1,
            fires: false,
        },
        Case {
            what: "buy: bid below, ask still above",
            side: "buy",
            bid: 99.0,
            ask: 100.5,
            fires: false,
        },
        Case {
            what: "sell: bid exactly at the level",
            side: "sell",
            bid: 100.0,
            ask: 100.1,
            fires: true,
        },
        Case {
            what: "sell: bid gapped through the level",
            side: "sell",
            bid: 100.5,
            ask: 100.6,
            fires: true,
        },
        Case {
            what: "sell: bid a tick below the level",
            side: "sell",
            bid: 99.9,
            ask: 100.0,
            fires: false,
        },
        Case {
            what: "sell: ask above, bid still below",
            side: "sell",
            bid: 99.5,
            ask: 100.4,
            fires: false,
        },
    ];

    for case in cases {
        let mut h = build(case.side, "");
        h.quote(SYM, case.bid, case.ask);
        let intents = h.drain();
        if !case.fires {
            assert!(
                intents.is_empty(),
                "{}: expected no order, got {intents:?}",
                case.what
            );
            continue;
        }
        assert_eq!(intents.len(), 1, "{}", case.what);
        let intent = &intents[0];
        let want_side = if case.side == "buy" {
            Side::Buy
        } else {
            Side::Sell
        };
        assert_eq!(intent.side, want_side, "{}", case.what);
        assert_eq!(intent.strategy, ID, "{}", case.what);
        assert_eq!(intent.kind, OrderKind::Market, "{}", case.what);
        assert_eq!(intent.qty, 2.0, "{}", case.what);
        assert!(!intent.reduce_only, "{}", case.what);
        assert_eq!(intent.tag, "touch-entry", "{}", case.what);
        // An entry always carries its stop.
        let stop = intent
            .stop
            .unwrap_or_else(|| panic!("{}: entry had no stop", case.what));
        let want_stop = if case.side == "buy" { 95.0 } else { 105.0 };
        assert_eq!(stop.trigger_px, want_stop, "{}", case.what);
    }
}

#[test]
fn entry_stamps_the_moment_it_decided() {
    let mut h = build("buy", "");
    h.ctx.set_now(4_242_000);
    h.quote(SYM, 99.9, 100.0);
    assert_eq!(h.one_intent().decided_ns, 4_242_000);
}

#[test]
fn entry_is_sent_once_however_often_the_level_is_touched() {
    let mut h = build("buy", "");
    h.quote(SYM, 99.9, 100.0);
    assert_eq!(h.drain().len(), 1);
    h.quote(SYM, 99.0, 99.5);
    h.quote(SYM, 98.0, 98.5);
    h.quote(SYM, 99.9, 100.0);
    assert!(
        h.drain().is_empty(),
        "the entry must not be sent again while it is in flight"
    );
}

#[test]
fn a_held_position_does_not_re_enter() {
    let mut h = entered("buy", "");
    h.quote(SYM, 98.0, 98.5);
    h.quote(SYM, 90.0, 90.5);
    assert!(h.drain().is_empty(), "holding is not a reason to buy again");
}

#[test]
fn quotes_for_another_symbol_are_ignored() {
    let mut h = build("buy", "");
    h.ctx.add_symbol("ETHUSDT");
    h.quote("ETHUSDT", 10.0, 10.1);
    assert!(h.drain().is_empty());
}

#[test]
fn take_profit_exits_on_the_touch_and_not_before() {
    struct Case {
        what: &'static str,
        side: &'static str,
        take: &'static str,
        bid: f64,
        ask: f64,
        exits: bool,
    }
    let cases = [
        Case {
            what: "buy: bid reaches the take level",
            side: "buy",
            take: "take_px = 110.0",
            bid: 110.0,
            ask: 110.2,
            exits: true,
        },
        Case {
            what: "buy: bid past the take level",
            side: "buy",
            take: "take_px = 110.0",
            bid: 112.0,
            ask: 112.2,
            exits: true,
        },
        Case {
            what: "buy: bid short of the take level",
            side: "buy",
            take: "take_px = 110.0",
            bid: 109.9,
            ask: 110.1,
            exits: false,
        },
        Case {
            what: "sell: ask reaches the take level",
            side: "sell",
            take: "take_px = 90.0",
            bid: 89.8,
            ask: 90.0,
            exits: true,
        },
        Case {
            what: "sell: ask past the take level",
            side: "sell",
            take: "take_px = 90.0",
            bid: 87.8,
            ask: 88.0,
            exits: true,
        },
        Case {
            what: "sell: ask short of the take level",
            side: "sell",
            take: "take_px = 90.0",
            bid: 89.9,
            ask: 90.1,
            exits: false,
        },
        Case {
            what: "buy: no take level configured",
            side: "buy",
            take: "",
            bid: 200.0,
            ask: 200.2,
            exits: false,
        },
        Case {
            what: "sell: no take level configured",
            side: "sell",
            take: "",
            bid: 1.0,
            ask: 1.2,
            exits: false,
        },
    ];

    for case in cases {
        let mut h = entered(case.side, case.take);
        h.quote(SYM, case.bid, case.ask);
        let intents = h.drain();
        if !case.exits {
            assert!(
                intents.is_empty(),
                "{}: expected no exit, got {intents:?}",
                case.what
            );
            continue;
        }
        assert_eq!(intents.len(), 1, "{}", case.what);
        let intent = &intents[0];
        let want_side = if case.side == "buy" {
            Side::Sell
        } else {
            Side::Buy
        };
        assert_eq!(intent.side, want_side, "{}", case.what);
        assert_eq!(intent.kind, OrderKind::Market, "{}", case.what);
        assert!(intent.reduce_only, "{}: an exit may only reduce", case.what);
        assert_eq!(intent.stop, None, "{}: an exit carries no stop", case.what);
        assert_eq!(intent.tag, "touch-exit", "{}", case.what);
        assert_eq!(intent.qty, 2.0, "{}", case.what);
    }
}

#[test]
fn exit_size_follows_the_fill_not_the_config() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.ack("c1");
    h.fill("c1", SYM, Side::Buy, 1.25, 100.0);
    h.quote(SYM, 110.0, 110.2);
    exit_checkpoint_before_cancel(&mut h, "c1");
    h.cancelled("c1");
    assert_eq!(h.one_intent().qty, 1.25);
}

#[test]
fn partial_entry_fills_add_up() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.ack("c1");
    h.fill("c1", SYM, Side::Buy, 0.5, 100.0);
    h.fill("c1", SYM, Side::Buy, 1.5, 100.0);
    h.quote(SYM, 110.0, 110.2);
    assert_eq!(h.one_intent().qty, 2.0);
}

#[test]
fn a_concurrent_entry_fill_gets_a_residual_exit() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.ack("c1");
    h.fill("c1", SYM, Side::Buy, 0.5, 100.0);
    h.quote(SYM, 110.0, 110.2);
    exit_checkpoint_before_cancel(&mut h, "c1");
    h.fill("c1", SYM, Side::Buy, 1.5, 100.0);
    assert_eq!(h.one_intent().qty, 2.0);
    h.ack("x1");
    h.fill("x1", SYM, Side::Sell, 0.5, 110.0);
    h.cancelled("x1");
    h.quote(SYM, 109.0, 109.2);
    assert_eq!(h.one_intent().qty, 1.5);
}

#[test]
fn a_cancelled_partial_exit_retries_only_the_residual() {
    let mut h = entered("buy", "take_px = 110.0");
    h.quote(SYM, 110.0, 110.2);
    h.drain();
    h.ack("x1");
    h.fill("x1", SYM, Side::Sell, 0.75, 110.0);
    h.cancelled("x1");

    h.quote(SYM, 109.0, 109.2);
    let retry = h.one_intent();
    assert!(retry.reduce_only);
    assert_eq!(retry.qty, 1.25);
}

#[test]
fn a_late_entry_cancel_is_not_mistaken_for_the_unacked_exit() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.ack("c1");
    h.fill("c1", SYM, Side::Buy, 0.5, 100.0);
    h.quote(SYM, 110.0, 110.2);
    exit_checkpoint_before_cancel(&mut h, "c1");

    h.cancelled("c1");
    assert_eq!(h.one_intent().qty, 0.5);
    h.quote(SYM, 109.0, 109.2);
    assert!(
        h.drain().is_empty(),
        "the first exit is still with the venue"
    );
}

#[test]
fn an_engine_refused_exit_is_retried() {
    let mut h = entered("buy", "take_px = 110.0");
    h.quote(SYM, 110.0, 110.2);
    h.drain();
    let symbol = h.ctx.id_of(SYM);
    h.refuse(symbol, true);
    h.quote(SYM, 109.0, 109.2);
    assert_eq!(h.one_intent().qty, 2.0);
}

#[test]
fn ttl_is_armed_at_the_fill_and_exits_when_it_expires() {
    let mut h = build("buy", "ttl_s = 60");
    h.ctx.set_now(1_000_000_000);
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    assert!(
        h.ctx.arm_calls.is_empty(),
        "the timer is armed at the fill, not at the send"
    );

    h.ack("c1");
    h.fill("c1", SYM, Side::Buy, 2.0, 100.0);
    assert_eq!(h.ctx.arm_calls.len(), 1);
    let armed = h.ctx.arm_calls[0];
    assert_eq!(armed.id, TimerId(1));
    assert_eq!(armed.due_ns, 1_000_000_000 + 60_000_000_000);

    assert!(h.fire_next_timer());
    let exit = h.one_intent();
    assert_eq!(exit.side, Side::Sell);
    assert!(exit.reduce_only);
    assert_eq!(exit.tag, "touch-exit");
    assert_eq!(exit.decided_ns, armed.due_ns);
}

#[test]
fn no_ttl_param_means_no_timer() {
    let mut h = entered("buy", "");
    assert!(h.ctx.arm_calls.is_empty());
    assert!(!h.fire_next_timer(), "nothing should be waiting to fire");
}

#[test]
fn a_ttl_that_expires_after_the_take_exit_does_nothing() {
    let mut h = build("buy", "take_px = 110.0\nttl_s = 60");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.ack("c1");
    h.fill("c1", SYM, Side::Buy, 2.0, 100.0);
    h.quote(SYM, 110.0, 110.2);
    assert_eq!(
        h.drain().len(),
        1,
        "the take level should have sent the exit"
    );

    assert!(h.fire_next_timer());
    assert!(h.drain().is_empty(), "the exit is already with the venue");
}

#[test]
fn a_timer_the_plug_never_armed_is_ignored() {
    let mut h = entered("buy", "take_px = 110.0");
    h.fire_timer(TimerId(99));
    assert!(h.drain().is_empty());
}

#[test]
fn a_rejected_exit_is_resent_on_the_next_quote() {
    let mut h = entered("buy", "take_px = 110.0");
    h.quote(SYM, 110.0, 110.2);
    assert_eq!(h.drain().len(), 1);

    h.ack("x1");
    h.reject("x1", "reduce-only mismatch");
    assert!(h.drain().is_empty(), "the resend waits for a fresh quote");

    h.quote(SYM, 109.0, 109.2);
    let resent = h.one_intent();
    assert_eq!(resent.tag, "touch-exit");
    assert!(resent.reduce_only);
    assert_eq!(resent.qty, 2.0);
}

#[test]
fn repeated_rejected_exits_keep_de_risking_the_open_position() {
    let mut h = entered("buy", "take_px = 110.0");
    h.quote(SYM, 110.0, 110.2);
    h.drain();
    h.ack("x1");
    h.reject("x1", "first refusal");
    h.quote(SYM, 109.0, 109.2);
    assert_eq!(h.drain().len(), 1, "the one resend");

    h.ack("x2");
    h.reject("x2", "second refusal");
    h.quote(SYM, 108.0, 108.2);
    let third = h.one_intent();
    assert!(third.reduce_only);
    assert_eq!(third.qty, 2.0);
}

#[test]
fn a_filled_exit_ends_the_plug() {
    let mut h = entered("buy", "take_px = 110.0");
    h.quote(SYM, 110.0, 110.2);
    h.drain();
    h.ack("x1");
    h.fill("x1", SYM, Side::Sell, 2.0, 110.0);

    h.quote(SYM, 99.9, 100.0);
    h.quote(SYM, 120.0, 120.2);
    assert!(h.drain().is_empty(), "a finished plug emits nothing");
}

#[test]
fn a_rejected_entry_ends_the_plug() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    assert_eq!(h.drain().len(), 1);
    h.ack("c1");
    h.reject("c1", "insufficient margin");

    h.quote(SYM, 99.0, 99.5);
    h.quote(SYM, 90.0, 90.5);
    h.quote(SYM, 110.0, 110.2);
    assert!(h.drain().is_empty(), "a refused touch is not chased");
}

#[test]
fn a_cancelled_entry_ends_the_plug_without_chasing() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.ack("c1");
    h.cancelled("c1");

    h.quote(SYM, 99.0, 99.5);
    h.quote(SYM, 110.0, 110.2);
    assert!(h.drain().is_empty());
}

/// Not chasing is only half of it: the plug has to be finished, so that late
/// news about the refused order cannot start it holding a position it never
/// meant to have.
#[test]
fn a_late_fill_after_an_entry_reject_is_still_exited() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.ack("c1");
    h.reject("c1", "insufficient margin");

    h.fill("c1", SYM, Side::Buy, 2.0, 100.0);
    h.quote(SYM, 110.0, 110.2);
    let exit = h.one_intent();
    assert!(exit.reduce_only);
    assert_eq!(exit.qty, 2.0);
}

#[test]
fn a_rejected_entry_without_an_ack_first_ends_the_plug() {
    let mut h = build("buy", "");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.reject("c1", "insufficient margin");
    h.quote(SYM, 98.0, 98.5);
    assert!(h.drain().is_empty());
}

#[test]
fn a_feed_reset_leaves_the_entry_armed() {
    let mut h = build("buy", "");
    h.feed_reset();
    assert!(h.drain().is_empty(), "a reset is not a reason to trade");
    h.quote(SYM, 99.9, 100.0);
    assert_eq!(
        h.one_intent().tag,
        "touch-entry",
        "the level is absolute, so it still counts"
    );
}

#[test]
fn a_feed_reset_does_not_resend_an_entry_in_flight() {
    let mut h = build("buy", "");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.feed_reset();
    h.quote(SYM, 99.0, 99.5);
    assert!(h.drain().is_empty());

    // Order news comes from the order feed, which the reset did not touch.
    h.ack("c1");
    h.fill("c1", SYM, Side::Buy, 2.0, 100.0);
    assert!(h.drain().is_empty());
}

#[test]
fn a_feed_reset_while_holding_changes_nothing() {
    let mut h = entered("buy", "take_px = 110.0");
    h.feed_reset();
    assert!(h.drain().is_empty());
    h.quote(SYM, 110.0, 110.2);
    assert_eq!(h.one_intent().tag, "touch-exit");
}

#[test]
fn a_feed_reset_while_an_exit_is_in_flight_changes_nothing() {
    let mut h = entered("buy", "take_px = 110.0");
    h.quote(SYM, 110.0, 110.2);
    h.drain();
    h.feed_reset();
    h.quote(SYM, 111.0, 111.2);
    assert!(h.drain().is_empty(), "the exit is already with the venue");
}

#[test]
fn updates_for_another_order_are_ignored_once_ours_is_known() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.ack("c1");

    // Someone else's fill: the stop that the venue holds, say.
    h.fill("other", SYM, Side::Buy, 2.0, 100.0);
    h.quote(SYM, 110.0, 110.2);
    assert!(h.drain().is_empty(), "we are not holding anything yet");

    h.fill("c1", SYM, Side::Buy, 2.0, 100.0);
    h.quote(SYM, 110.0, 110.2);
    assert_eq!(h.one_intent().tag, "touch-exit");
}

#[test]
fn a_fill_that_beats_the_ack_is_taken_as_ours() {
    let mut h = build("buy", "take_px = 110.0");
    h.quote(SYM, 99.9, 100.0);
    h.drain();
    h.fill("c1", SYM, Side::Buy, 2.0, 100.0);
    h.quote(SYM, 110.0, 110.2);
    assert_eq!(h.one_intent().tag, "touch-exit");
}
