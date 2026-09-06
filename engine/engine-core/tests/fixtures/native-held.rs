use engine_types::Strategy;

fn native_config() -> engine_strategies::native_config::NativeConfigRender {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let read = |name: &str| std::fs::read(root.join("configs").join(name)).unwrap();
    engine_strategies::native_config::render_native_config(
        engine_strategies::native_config::NativeConfigSources {
            realm: "mainnet",
            signal_config: &read("signal-worker.mainnet.json"),
            long_rule: &read("long_native_v12.json"),
            carry_rule: &read("lane2_carry_hold_v7.json"),
            exodus_rule: &read("lane2_exodus_short_v1.json"),
            operational_config: &read("operational.json"),
            long_entries_enabled: true,
            carry_entries_enabled: true,
            exodus_entries_enabled: true,
        },
    )
    .unwrap()
}

pub fn held_plug(kind: &str, now: i64, equity: f64) -> Box<dyn Strategy> {
    let config = native_config();
    if kind == "long_native" {
        let state = engine_strategies::native_long::plan::SleeveState {
            symbols: std::collections::BTreeMap::from([(
                "BTCUSDT".into(),
                engine_strategies::native_long::plan::PriorState {
                    requested: true,
                    filled: true,
                    entry_ts_ms: now - 60_000,
                    entry_price: 30_000.0,
                    target_notional_usdt: 300.0,
                    stop_loss_fraction: 0.1,
                    max_hold_deadline_ts_ms: now + 86_400_000,
                    max_hold_duration_ms: 86_400_000,
                    entry_valid_until_ms: now + 3_600_000,
                    attempted_signal_ts_ms: now - 60_000,
                    active_positions: 1,
                    ..Default::default()
                },
            )]),
            ..Default::default()
        };
        Box::new(engine_strategies::native_long::NativeLong::new(config.long, state).unwrap())
    } else {
        let sizing = if config.carry.capital_reference_usdt > 0.0 {
            config.carry.capital_reference_usdt
        } else {
            equity
        };
        let weight = 300.0 / (sizing * config.carry.notional_multiplier);
        let state = engine_strategies::native_carry::plan::SleeveState {
            sizing_anchors: std::collections::BTreeMap::from([(now, sizing)]),
            desired_targets: std::collections::BTreeMap::from([(
                "BTCUSDT".into(),
                engine_strategies::native_carry::plan::StoredTarget {
                    notional_usdt: 300.0,
                    stop_loss_fraction: config.carry.stop_loss_fraction,
                    leverage: config.carry.entry_leverage,
                    entry_valid_until_ms: now + config.carry.execution.signal_validity_ms
                        - config.carry.execution.engine_entry_cutoff_ms,
                },
            )]),
            last_publication_decision_ts_ms: now,
            current_decision: Some(engine_strategies::native_carry::scorer::CarryDecision {
                schema_version: 1,
                decision_ts_ms: now,
                weights: std::collections::BTreeMap::from([("BTCUSDT".into(), weight)]),
                universe_size: 270,
                replay_days: 60,
                gross: weight,
            }),
            ..Default::default()
        };
        Box::new(engine_strategies::native_carry::NativeCarry::new(config.carry, state).unwrap())
    }
}
