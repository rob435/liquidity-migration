//! The order-path probe on the engine's own contracts: when it fires, what it
//! rests, that it pulls, and that a fill is closed rather than kept.

use engine_types::{
    Action, EngineEvent, Feed, InstrumentRule, OrderKind, Side, Strategy, StrategyCtx, StrategyId,
    TimeInForce,
};

use super::probe::{Probe, FIRE, PULL};
use crate::mock_ctx::{Harness, RestingSeed};

const RULE: InstrumentRule = InstrumentRule {
    tick_size: 0.001,
    qty_step: 0.1,
    min_qty: 0.1,
    min_notional: 5.0,
};

fn config(extra: &str) -> toml::Value {
    let src = format!(
        r#"
        symbol = "CAKEUSDT"
        every_s = 900
        rest_ms = 2000
        offset_bps = 300
        notional_usdt = 5.5
        stop_loss_fraction = 0.08
        {extra}
    "#
    );
    toml::from_str(&src).expect("test config parses")
}

fn bench(extra: &str) -> Harness {
    let probe = Probe::from_params(StrategyId(4), &config(extra)).unwrap();
    let mut h = Harness::new(Box::new(probe));
    h.ctx.set_rule("CAKEUSDT", RULE);
    h
}

fn fire(h: &mut Harness) {
    let now_ns = h.ctx.now_ns();
    h.strategy
        .on_event(&EngineEvent::Timer { id: FIRE, now_ns }, &mut h.ctx);
}

fn pull(h: &mut Harness) {
    let now_ns = h.ctx.now_ns();
    h.strategy
        .on_event(&EngineEvent::Timer { id: PULL, now_ns }, &mut h.ctx);
}

#[test]
fn it_claims_only_the_quote_feed_of_its_symbol() {
    let probe = Probe::from_params(StrategyId(0), &config("")).unwrap();
    let subs = probe.subscriptions();
    assert_eq!(subs.len(), 1);
    assert_eq!(subs[0].symbol, "CAKEUSDT");
    assert_eq!(subs[0].feed, Feed::Quote);
    assert!(probe.configured_entries_enabled());
}

#[test]
fn boot_arms_the_first_fire_on_the_next_wall_clock_boundary() {
    let mut h = bench("");
    // 1_700_000_000_000 ms is 2023-11-14 22:13:20 UTC; the next quarter hour
    // is 22:15:00, 100 s later.
    h.ctx.set_wall_ms(1_700_000_000_000);
    h.ctx.set_now(5_000_000_000);
    h.boot();
    let timer = h
        .ctx
        .timers
        .iter()
        .find(|t| t.id == FIRE)
        .expect("fire armed");
    assert_eq!(timer.due_ns - timer.armed_ns, 100_000 * 1_000_000);
    assert!(h.drain_actions().is_empty(), "boot places nothing");
}

#[test]
fn a_boundary_too_close_to_arm_is_skipped_for_the_next_one() {
    let probe = Probe::from_params(StrategyId(0), &config("")).unwrap();
    assert_eq!(probe.lead_ms(1_700_000_099_500), 500 + 900_000);
    assert_eq!(probe.lead_ms(1_700_000_100_000), 900_000);
    assert_eq!(probe.lead_ms(1_700_000_000_000), 100_000);
}

#[test]
fn a_fire_rests_one_post_only_buy_under_the_bid_at_the_venue_minimum_then_pulls() {
    let mut h = bench("");
    h.ctx.set_now(10_000_000_000);
    h.quote("CAKEUSDT", 2.000, 2.001);
    h.drain_actions();

    fire(&mut h);

    let placed = h.drain();
    assert_eq!(placed.len(), 1, "one probe order");
    let intent = &placed[0];
    assert_eq!(intent.side, Side::Buy);
    assert!(!intent.reduce_only);
    assert_eq!(intent.tag, "probe");
    assert_eq!(intent.strategy, StrategyId(4));
    let OrderKind::Limit { px, tif } = intent.kind else {
        panic!("a probe is a limit order, got {:?}", intent.kind);
    };
    assert_eq!(tif, TimeInForce::PostOnly, "a probe that takes is a trade");
    assert_eq!(px, 1.94, "3% under a 2.000 bid, on the tick");
    // 5.5 USDT at 1.94 is 2.83; rounded up to the 0.1 step.
    assert_eq!(intent.qty, 2.9);
    assert!(intent.qty * px >= 5.0, "meets the venue minimum notional");
    let stop = intent
        .stop
        .expect("the risk kernel refuses an entry without a stop");
    assert!(stop.trigger_px < px && stop.trigger_px > 0.0);
    assert_eq!(stop.trigger_px, 1.785);
    assert!(intent.work.is_none() && intent.leverage.is_none());

    let pull = h
        .ctx
        .timers
        .iter()
        .find(|t| t.id == PULL)
        .expect("pull armed");
    assert_eq!(pull.due_ns - pull.armed_ns, 2_000_000_000);
    assert!(
        h.ctx.timers.iter().any(|t| t.id == FIRE),
        "the next boundary is armed before anything else is tried"
    );
    assert!(h.strategy.entry_blockers().is_empty());
}

#[test]
fn the_pull_cancels_the_probe_and_only_the_probe() {
    let mut h = bench("");
    let symbol = h.ctx.id_of("CAKEUSDT");
    h.rest(RestingSeed {
        client_order_id: "probe-1".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit {
            px: 1.94,
            tif: TimeInForce::PostOnly,
        },
        qty: 2.9,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });
    h.rest(RestingSeed {
        client_order_id: "drain-1".into(),
        symbol,
        side: Side::Sell,
        kind: OrderKind::Market,
        qty: 2.9,
        filled_qty: 0.0,
        reduce_only: true,
        acked: true,
    });

    pull(&mut h);

    let actions = h.drain_actions();
    assert_eq!(actions.len(), 1);
    assert!(matches!(
        &actions[0],
        Action::Cancel { client_order_id, .. } if client_order_id == "probe-1"
    ));
}

#[test]
fn a_probe_still_resting_at_the_next_boundary_is_not_doubled() {
    let mut h = bench("");
    let symbol = h.ctx.id_of("CAKEUSDT");
    h.ctx.set_now(10_000_000_000);
    h.quote("CAKEUSDT", 2.000, 2.001);
    h.drain_actions();
    h.rest(RestingSeed {
        client_order_id: "probe-1".into(),
        symbol,
        side: Side::Buy,
        kind: OrderKind::Limit {
            px: 1.94,
            tif: TimeInForce::PostOnly,
        },
        qty: 2.9,
        filled_qty: 0.0,
        reduce_only: false,
        acked: true,
    });

    fire(&mut h);

    assert!(h.drain().is_empty());
    assert_eq!(
        h.strategy.entry_blockers(),
        vec![(
            "CAKEUSDT".to_string(),
            "previous probe still resting".to_string()
        )]
    );
}

#[test]
fn no_quote_or_a_stale_one_skips_and_says_so_but_keeps_the_schedule() {
    let mut h = bench("");
    h.ctx.set_now(10_000_000_000);
    fire(&mut h);
    assert!(h.drain().is_empty());
    assert_eq!(
        h.strategy.entry_blockers(),
        vec![("CAKEUSDT".to_string(), "no quote".to_string())]
    );
    assert!(h.ctx.timers.iter().any(|t| t.id == FIRE));

    h.quote("CAKEUSDT", 2.000, 2.001);
    h.drain_actions();
    h.ctx.set_now(10_000_000_000 + 31_000_000_000);
    fire(&mut h);
    assert!(h.drain().is_empty());
    assert_eq!(
        h.strategy.entry_blockers(),
        vec![("CAKEUSDT".to_string(), "quote stale".to_string())]
    );

    h.ctx.set_now(10_000_000_000 + 40_000_000_000);
    h.quote("CAKEUSDT", 2.000, 2.001);
    h.drain_actions();
    fire(&mut h);
    assert_eq!(h.drain().len(), 1, "a fresh quote probes again");
    assert!(h.strategy.entry_blockers().is_empty());
}

#[test]
fn disabled_by_config_it_never_places_and_reports_why() {
    let mut h = bench("enabled = false");
    assert!(!h.strategy.configured_entries_enabled());
    h.ctx.set_now(10_000_000_000);
    h.quote("CAKEUSDT", 2.000, 2.001);
    h.drain_actions();

    fire(&mut h);

    assert!(h.drain().is_empty());
    assert_eq!(
        h.strategy.entry_blockers(),
        vec![("CAKEUSDT".to_string(), "entries disabled".to_string())]
    );
    assert!(
        h.ctx.timers.iter().any(|t| t.id == FIRE),
        "still on the clock"
    );
}

#[test]
fn a_refusal_by_the_engine_is_reported_as_a_blocker() {
    let mut h = bench("");
    let symbol = h.ctx.id_of("CAKEUSDT");
    h.strategy.on_event(
        &EngineEvent::IntentRefused {
            symbol,
            reduce_only: false,
            reason: "may_open is false".into(),
        },
        &mut h.ctx,
    );
    assert_eq!(
        h.strategy.entry_blockers(),
        vec![(
            "CAKEUSDT".to_string(),
            "refused: may_open is false".to_string()
        )]
    );
    assert!(
        h.strategy.health_error().is_none(),
        "a refusal is not a fault"
    );
}

#[test]
fn a_fill_is_closed_at_market_and_shows_as_a_blocker_never_an_error_until_flat() {
    let mut h = bench("");
    h.ctx.set_now(10_000_000_000);

    h.maker_fill_with_exec("x1", "probe-1", "CAKEUSDT", Side::Buy, 2.9, 1.94);

    let drain = h.drain();
    assert_eq!(drain.len(), 1, "the fill is closed at once");
    assert_eq!(drain[0].side, Side::Sell);
    assert_eq!(drain[0].qty, 2.9);
    assert!(drain[0].reduce_only);
    assert!(matches!(drain[0].kind, OrderKind::Market));
    assert_eq!(drain[0].tag, "probe-drain");
    assert!(
        h.strategy.health_error().is_none(),
        "a filled probe is never a strategy error: those page"
    );
    assert_eq!(
        h.strategy.entry_blockers(),
        vec![(
            "CAKEUSDT".to_string(),
            "closing a filled probe (2.9 held)".to_string()
        )]
    );

    // Another boundary while draining places nothing.
    h.quote("CAKEUSDT", 2.000, 2.001);
    h.drain_actions();
    fire(&mut h);
    assert!(h.drain().is_empty());
    assert_eq!(
        h.strategy.entry_blockers(),
        vec![("CAKEUSDT".to_string(), "closing a filled probe".to_string())]
    );

    h.maker_fill_with_exec("x2", "drain-1", "CAKEUSDT", Side::Sell, 2.9, 1.95);
    assert!(h.drain().is_empty(), "flat again: nothing more to send");
    assert!(
        h.strategy.entry_blockers().is_empty(),
        "flat clears the blocker"
    );
}

#[test]
fn a_refused_drain_is_retried_on_a_timer_not_in_the_same_wake() {
    let mut h = bench("");
    let symbol = h.ctx.id_of("CAKEUSDT");
    h.ctx.set_my_position("CAKEUSDT", 2.9);
    h.boot();
    assert_eq!(
        h.drain().len(),
        1,
        "boot closes inventory a restart left behind"
    );

    h.strategy.on_event(
        &EngineEvent::IntentRefused {
            symbol,
            reduce_only: true,
            reason: "no account view".into(),
        },
        &mut h.ctx,
    );
    assert!(h.drain().is_empty(), "no immediate re-send");
    let retry = h
        .ctx
        .timers
        .iter()
        .find(|t| t.id == engine_types::TimerId(3))
        .expect("drain retry armed");
    let now_ns = retry.due_ns;
    h.ctx.set_now(now_ns);
    h.strategy.on_event(
        &EngineEvent::Timer {
            id: engine_types::TimerId(3),
            now_ns,
        },
        &mut h.ctx,
    );
    assert_eq!(h.drain().len(), 1, "the retry sends the drain again");
}

#[test]
fn params_are_strict_and_named() {
    let err = Probe::from_params(StrategyId(0), &config("colour = \"red\""))
        .err()
        .unwrap();
    assert!(err.to_string().contains("colour"), "{err}");

    let too_often = toml::from_str::<toml::Value>("symbol = \"CAKEUSDT\"\nevery_s = 30").unwrap();
    let err = Probe::from_params(StrategyId(0), &too_often).err().unwrap();
    assert!(err.to_string().contains("every_s"), "{err}");

    let no_symbol = toml::from_str::<toml::Value>("every_s = 900").unwrap();
    let err = Probe::from_params(StrategyId(0), &no_symbol).err().unwrap();
    assert!(err.to_string().contains("symbol"), "{err}");

    let defaults = toml::from_str::<toml::Value>("symbol = \"CAKEUSDT\"\nevery_s = 900").unwrap();
    assert!(Probe::from_params(StrategyId(0), &defaults).is_ok());
}
