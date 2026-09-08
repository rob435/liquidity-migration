use super::*;
use crate::native_common::OrderEffect;
use crate::position_plan::Held;

fn config() -> StrategyConfig {
    let mut config = super::tests::config();
    config.early_exit_enabled = false;
    config.presettlement_exit_enabled = false;
    config
}

fn input() -> ReducerInput {
    let symbol = "BTCUSDT".to_owned();
    ReducerInput {
        now_ms: 50 * DAY_MS,
        decision: CarryDecision {
            schema_version: 1,
            decision_ts_ms: 50 * DAY_MS,
            weights: BTreeMap::from([(symbol.clone(), 0.1)]),
            universe_size: 100,
            replay_days: 49,
            gross: 0.1,
        },
        upcoming_decision: None,
        settled_funding: vec![],
        presettlement: vec![],
        durable_fires: vec![],
        trail_by_symbol: BTreeMap::new(),
        entry_blockers: BTreeMap::new(),
        account_healthy: true,
        equity_usdt: 1_000.0,
        upcoming_sizing_equity_usdt: None,
        facts: PlannerFacts {
            prices: BTreeMap::from([(symbol.clone(), 100.0)]),
            rules: BTreeMap::from([(
                symbol,
                engine_types::InstrumentRule {
                    tick_size: 0.01,
                    qty_step: 0.1,
                    min_qty: 0.1,
                    min_notional: 5.0,
                },
            )]),
            ..PlannerFacts::default()
        },
        owned_working_symbols: BTreeSet::new(),
        owned_opening_order_ids: BTreeMap::new(),
        checkpoint_fingerprint: None,
        signal_receipt: None,
    }
}

fn holding(input: &mut ReducerInput, qty: f64, px: f64) {
    input.facts.prices.insert("BTCUSDT".into(), px);
    input.facts.held.insert(
        "BTCUSDT".into(),
        Held {
            qty,
            side: Side::Buy,
            px,
            entry_px: 100.0,
            stop_px: 65.0,
        },
    );
}

fn size_steps(output: &ReducerOutput) -> Vec<&Step> {
    output
        .execution
        .effects
        .iter()
        .filter_map(|effect| match effect {
            Effect::Order(OrderEffect { step, .. }) if !matches!(step, Step::Restop { .. }) => {
                Some(step)
            }
            _ => None,
        })
        .collect()
}

#[test]
fn daily_hold_ignores_intraday_funding_and_upcoming_drops() {
    let config = config();
    let mut input = input();
    holding(&mut input, 1.0, 100.0);
    input.now_ms += DAY_MS - 60_000;
    input.settled_funding.push(SettledFundingObservation {
        symbol: "BTCUSDT".into(),
        settlement_ts_ms: input.now_ms - 60_000,
        available_at_ms: Some(input.now_ms - 60_000),
        rate: 0.001,
        funding_interval_min: Some(480),
    });
    input.presettlement.push(PresettlementObservation {
        symbol: "BTCUSDT".into(),
        observed_ts_ms: input.now_ms,
        settlement_ts_ms: input.now_ms + 60_000,
        running_rate: 0.001,
        mark_px: Some(100.0),
    });
    input.upcoming_decision = Some(CarryDecision {
        decision_ts_ms: input.decision.decision_ts_ms + DAY_MS,
        weights: BTreeMap::new(),
        gross: 0.0,
        ..input.decision.clone()
    });
    let output = reduce_lifecycle(input.clone(), SleeveState::default(), &config).unwrap();
    assert!(output.effective_decision.weights.contains_key("BTCUSDT"));
    assert!(size_steps(&output).is_empty());
    assert!(output.settled_exit_fires.is_empty());
    assert!(output.presettlement_fires.is_empty());
    assert!(output.drop_exit_fires.is_empty());

    input.decision = input.upcoming_decision.take().unwrap();
    input.now_ms = input.decision.decision_ts_ms;
    let next = reduce_lifecycle(input, output.next_state, &config).unwrap();
    assert!(matches!(
        size_steps(&next).as_slice(),
        [Step::Exit { qty: 1.0, .. }]
    ));
}

#[test]
fn daily_hold_quantity_survives_price_moves_partial_fills_and_restart() {
    let config = config();
    let mut input = input();
    let opened = reduce_lifecycle(input.clone(), SleeveState::default(), &config).unwrap();
    assert!(matches!(
        size_steps(&opened).as_slice(),
        [Step::Enter { qty: 1.0, .. }]
    ));
    input.now_ms += 60_000;
    holding(&mut input, 0.4, 125.0);
    let prior = serde_json::from_value(serde_json::to_value(opened.next_state).unwrap()).unwrap();
    let partial =
        reduce_lifecycle_with_mode(input.clone(), prior, &config, ReplanMode::BootRecovery)
            .unwrap();
    assert!(matches!(
        size_steps(&partial).as_slice(),
        [Step::Resize {
            qty: 0.6,
            reduce_only: false,
            ..
        }]
    ));

    input.now_ms += 60_000;
    holding(&mut input, 1.0, 125.0);
    let filled = reduce_lifecycle(input.clone(), partial.next_state, &config).unwrap();
    assert!(size_steps(&filled).is_empty());
    input.now_ms = input.decision.decision_ts_ms + DAY_MS;
    input.decision.decision_ts_ms += DAY_MS;
    let next = reduce_lifecycle(input, filled.next_state, &config).unwrap();
    assert!(matches!(
        size_steps(&next).as_slice(),
        [Step::Resize {
            qty: 0.2,
            reduce_only: true,
            ..
        }]
    ));
}

#[test]
fn daily_hold_retains_old_exit_tombstones_until_the_next_decision() {
    let config = config();
    let input = input();
    let prior = SleeveState {
        fired_exits: BTreeMap::from([("BTCUSDT".into(), input.decision.decision_ts_ms)]),
        ..SleeveState::default()
    };
    let output = reduce_lifecycle(input, prior, &config).unwrap();
    assert!(!output.effective_decision.weights.contains_key("BTCUSDT"));
    assert!(size_steps(&output).is_empty());
}

#[test]
fn daily_hold_adopts_a_legacy_checkpoint_without_midday_rebalancing() {
    let config = config();
    let mut input = input();
    let legacy = reduce_lifecycle(
        input.clone(),
        SleeveState::default(),
        &super::tests::config(),
    )
    .unwrap();
    assert_eq!(config.fingerprint(), super::tests::config().fingerprint());
    let json = serde_json::to_value(legacy.next_state).unwrap();
    assert!(json["desired_targets"]["BTCUSDT"]
        .get("target_qty")
        .is_none());
    input.now_ms += 60_000;
    holding(&mut input, 1.0, 125.0);
    let restored = reduce_lifecycle_with_mode(
        input,
        serde_json::from_value(json).unwrap(),
        &config,
        ReplanMode::BootRecovery,
    )
    .unwrap();
    assert!(size_steps(&restored).is_empty());
    assert_eq!(
        restored.next_state.desired_targets["BTCUSDT"].target_qty,
        Some(1.0)
    );
}

#[test]
fn daily_hold_waits_for_a_price_and_does_not_cancel_on_price_changes() {
    let config = config();
    let mut input = input();
    input.facts.prices.clear();
    let missing = reduce_lifecycle(input.clone(), SleeveState::default(), &config).unwrap();
    assert!(size_steps(&missing).is_empty());
    input.facts.prices.insert("BTCUSDT".into(), 100.0);
    let opened = reduce_lifecycle(input.clone(), missing.next_state, &config).unwrap();
    assert!(matches!(
        size_steps(&opened).as_slice(),
        [Step::Enter { qty: 1.0, .. }]
    ));
    input.facts.prices.insert("BTCUSDT".into(), 125.0);
    input.owned_working_symbols.insert("BTCUSDT".into());
    input
        .owned_opening_order_ids
        .insert("BTCUSDT".into(), vec!["daily-entry".into()]);
    let working = reduce_lifecycle(input, opened.next_state, &config).unwrap();
    assert!(working
        .execution
        .effects
        .iter()
        .all(|effect| !matches!(effect, Effect::CancelOwned { .. })));
    assert_eq!(
        working.next_state.desired_targets["BTCUSDT"].target_qty,
        Some(1.0)
    );
}
