use super::*;
use serde::Deserialize;

#[derive(Deserialize)]
struct LongGolden {
    base_ts_ms: i64,
    days: usize,
    observed_ts_ms: i64,
    rows: Vec<LongFeatureRow>,
}

#[derive(Deserialize)]
struct CarryGolden {
    base_ts_ms: i64,
    days: usize,
    decision_ts_ms: i64,
    rows: Vec<CarryFeatureRow>,
}

#[test]
fn sample_std_matches_ddof_one() {
    assert!((sample_std(&[1.0, 2.0, 3.0]).unwrap() - 1.0).abs() < 1e-12);
}

#[test]
fn daily_bars_accept_the_configured_minimum_without_a_contiguity_gate() {
    let day = 100 * DAY_MS;
    let history: BTreeMap<_, _> = (0..24)
        .filter(|hour| *hour != 7)
        .map(|hour| {
            let open_ts_ms = day + hour * HOUR_MS;
            (
                open_ts_ms,
                HourlyKline {
                    symbol: "BTCUSDT".into(),
                    open_ts_ms,
                    available_at_ms: day + DAY_MS,
                    open: 1.0,
                    high: 1.0,
                    low: 1.0,
                    close: 1.0,
                    volume_base: 1.0,
                    turnover_quote: 1.0,
                },
            )
        })
        .collect();
    let bars = daily_bars(&history, day + DAY_MS, 20);
    assert_eq!(bars.len(), 1);
    assert_eq!(bars[0].turnover_quote, 23.0);
}

#[test]
fn whale_is_nulled_before_the_three_day_shift() {
    let day = 100 * DAY_MS;
    let mut rows = BTreeMap::new();
    rows.insert(
        day - 6 * DAY_MS,
        BinanceWhaleObservation {
            symbol: "BTCUSDT".into(),
            day_end_ms: day - 6 * DAY_MS,
            available_at_ms: day,
            long_short_ratio: Some(2.0),
        },
    );
    rows.insert(
        day,
        BinanceWhaleObservation {
            symbol: "BTCUSDT".into(),
            day_end_ms: day,
            available_at_ms: day,
            long_short_ratio: Some(1.0),
        },
    );
    assert_eq!(fresh_whale(&rows, day, day, 48), Some(1.0));
    assert_eq!(fresh_whale(&rows, day - 3 * DAY_MS, day, 48), None);
}

#[test]
fn future_available_funding_and_whales_are_invisible() {
    let day = 100 * DAY_MS;
    let mut funding = BTreeMap::new();
    funding.insert(
        day,
        SettledFunding {
            symbol: "BTCUSDT".into(),
            settlement_ts_ms: day,
            available_at_ms: day + 10,
            rate: -0.01,
            funding_interval_min: 480,
        },
    );
    assert_eq!(
        crowd_persistence(Some(&funding), day, day, Some(1), 10.0),
        None
    );

    let mut whales = BTreeMap::new();
    whales.insert(
        day,
        BinanceWhaleObservation {
            symbol: "BTCUSDT".into(),
            day_end_ms: day,
            available_at_ms: day + 10,
            long_short_ratio: Some(1.2),
        },
    );
    assert_eq!(fresh_whale(&whales, day, day, 48), None);
}

#[test]
fn funding_trails_require_each_expected_settlement_in_both_windows() {
    let decision = 100 * DAY_MS;
    let mut history = BTreeMap::new();
    for open_ts_ms in (decision - 96 * HOUR_MS..decision).step_by(HOUR_MS as usize) {
        history.insert(
            open_ts_ms,
            HourlyKline {
                symbol: "BTCUSDT".into(),
                open_ts_ms,
                available_at_ms: decision,
                open: 1.0,
                high: 1.0,
                low: 1.0,
                close: 1.0,
                volume_base: 1.0,
                turnover_quote: 1.0,
            },
        );
    }
    let mut funding = BTreeMap::new();
    for settlement_ts_ms in (decision - 96 * HOUR_MS..=decision).step_by(HOUR_MS as usize) {
        funding.insert(
            settlement_ts_ms,
            SettledFunding {
                symbol: "BTCUSDT".into(),
                settlement_ts_ms,
                available_at_ms: decision,
                rate: -0.001,
                funding_interval_min: 60,
            },
        );
    }
    close_option(
        trail_funding_at(&history, Some(&funding), decision, 24, decision),
        Some(-0.024),
    );
    funding.remove(&(decision - 22 * HOUR_MS));
    assert_eq!(
        trail_funding_at(&history, Some(&funding), decision, 24, decision),
        None
    );
    funding.insert(
        decision - 22 * HOUR_MS,
        SettledFunding {
            symbol: "BTCUSDT".into(),
            settlement_ts_ms: decision - 22 * HOUR_MS,
            available_at_ms: decision,
            rate: -0.001,
            funding_interval_min: 60,
        },
    );
    let shifted = decision - 48 * HOUR_MS;
    funding.remove(&(shifted - 22 * HOUR_MS));
    assert_eq!(
        trail_funding_at(&history, Some(&funding), shifted, 24, decision),
        None
    );
}

#[test]
fn long_features_match_recorded_golden_with_identical_nulls() {
    let golden: LongGolden = serde_json::from_str(include_str!(
        "../../tests/fixtures/long_feature_golden.json"
    ))
    .unwrap();
    let mut history = KlineHistory::new();
    let symbols = ["AAAUSDT", "BTCUSDT", "ETHUSDT"];
    for (symbol_index, symbol) in symbols.iter().enumerate() {
        let mut prior = 100.0 * (symbol_index + 1) as f64;
        for hour in 0..golden.days * 24 {
            let h = hour as f64;
            let close = 100.0
                * (symbol_index + 1) as f64
                * ((0.00008 + symbol_index as f64 * 0.00001) * h
                    + (0.004 + symbol_index as f64 * 0.001) * (h / 19.0).sin())
                .exp();
            let open_ts_ms = golden.base_ts_ms + hour as i64 * HOUR_MS;
            Arc::make_mut(history.entry((*symbol).to_owned()).or_default()).insert(
                open_ts_ms,
                HourlyKline {
                    symbol: (*symbol).to_owned(),
                    open_ts_ms,
                    available_at_ms: golden.observed_ts_ms,
                    open: prior,
                    high: prior.max(close) * 1.01,
                    low: prior.min(close) * 0.99,
                    close,
                    volume_base: 10.0 + symbol_index as f64,
                    turnover_quote: (symbol_index + 1) as f64 * 1000.0 + (hour % 24) as f64 * 10.0,
                },
            );
            prior = close;
        }
    }
    let config = LongFeatureConfig {
        profile_name: "v12".into(),
        execution_strategy_id: "long_native_v12_wide_stop".into(),
        exclude_symbols: Vec::new(),
        universe_size: 50,
        universe_volume_window_days: 90,
        min_listing_history_days: 30,
        regime_symbol: "BTCUSDT".into(),
        regime_sma_days: 30,
        vol_estimate_window_days: 30,
        daily_min_hourly_bars: 20,
        cold_start_lookback_days: 100,
        pump_lookback_days: [3, 7],
        atr_window_days: 14,
        atr_min_samples: 7,
        btc_rv_window_days: 30,
        btc_rv_min_samples: 20,
        btc_rv_null_value: 0.8,
        regime_missing_is_on: false,
        median_fallback_to_daily_turnover: true,
    };
    let actual = build_long_features(
        &history,
        &symbols
            .iter()
            .map(|value| (*value).to_owned())
            .collect::<Vec<_>>(),
        golden.observed_ts_ms,
        &config,
    );
    assert_eq!(actual.rows.len(), golden.rows.len());
    for (actual, expected) in actual.rows.iter().zip(&golden.rows) {
        assert_eq!(actual.symbol, expected.symbol);
        assert_eq!(actual.ts_ms, expected.ts_ms);
        assert_eq!(actual.today_volume_rank, expected.today_volume_rank);
        assert_eq!(actual.universe_rank, expected.universe_rank);
        assert_eq!(actual.in_universe, expected.in_universe);
        assert_eq!(actual.regime_on, expected.regime_on);
        assert_eq!(actual.eth_regime_on, expected.eth_regime_on);
        assert_eq!(actual.symbol_age_days, expected.symbol_age_days);
        close(actual.close, expected.close);
        close(actual.turnover_quote, expected.turnover_quote);
        close_option(actual.log_return, expected.log_return);
        close_option(actual.realized_vol, expected.realized_vol);
        close_option(actual.sigma_daily_30d, expected.sigma_daily_30d);
        close_option(actual.turnover_median_90d, expected.turnover_median_90d);
        close_option(actual.pump_3d_log, expected.pump_3d_log);
        close_option(actual.pump_7d_log, expected.pump_7d_log);
        close(actual.close_location, expected.close_location);
        close_option(actual.close_loc_3d, expected.close_loc_3d);
        close_option(actual.close_loc_7d, expected.close_loc_7d);
        close_option(actual.atr_14d_pct, expected.atr_14d_pct);
        close(actual.btc_rv_30, expected.btc_rv_30);
    }
    engine_strategies::native_common::validate_exact_symbol_coverage(
        &symbols
            .iter()
            .map(|symbol| (*symbol).to_owned())
            .collect::<Vec<_>>(),
        &actual
            .rows
            .iter()
            .map(|row| row.symbol.clone())
            .collect::<Vec<_>>(),
        &actual
            .rejections
            .iter()
            .map(|row| row.symbol.clone())
            .collect::<Vec<_>>(),
    )
    .expect("LONG producer emits an exact sleeve partition");

    let mut gapped = history.clone();
    let missing = golden.observed_ts_ms - 10 * HOUR_MS;
    Arc::make_mut(gapped.get_mut("AAAUSDT").unwrap()).remove(&missing);
    let gapped = build_long_features(
        &gapped,
        &symbols
            .iter()
            .map(|value| (*value).to_owned())
            .collect::<Vec<_>>(),
        golden.observed_ts_ms,
        &config,
    );
    assert!(gapped.rows.iter().all(|row| row.symbol != "AAAUSDT"));
    assert_eq!(
        gapped
            .rejections
            .iter()
            .filter(|row| row.symbol == "AAAUSDT")
            .count(),
        1,
        "a gapped symbol is rejected instead of accepted and rejected"
    );

    let cold = build_long_features(
        &KlineHistory::new(),
        &["AAAUSDT".to_owned()],
        golden.observed_ts_ms,
        &config,
    );
    assert_eq!(
        cold.rejections
            .iter()
            .map(|row| row.symbol.as_str())
            .collect::<Vec<_>>(),
        vec!["AAAUSDT"],
        "support-only regime symbols never escape into sleeve rejections"
    );
}

#[test]
fn carry_features_match_recorded_golden_with_identical_nulls() {
    let golden: CarryGolden = serde_json::from_str(include_str!(
        "../../tests/fixtures/carry_feature_golden.json"
    ))
    .unwrap();
    let symbols = ["AAAUSDT", "BBBUSDT"];
    let mut klines = KlineHistory::new();
    let mut funding = FundingHistory::new();
    let mut whales = WhaleHistory::new();
    for (symbol_index, symbol) in symbols.iter().enumerate() {
        for hour in 0..golden.days * 24 {
            let h = (hour + 1) as f64;
            let close = 100.0
                * (symbol_index + 1) as f64
                * ((0.00005 + symbol_index as f64 * 0.00001) * h
                    + (0.003 + symbol_index as f64 * 0.001) * (h / 23.0).sin())
                .exp();
            let open_ts_ms = golden.base_ts_ms + hour as i64 * HOUR_MS;
            Arc::make_mut(klines.entry((*symbol).to_owned()).or_default()).insert(
                open_ts_ms,
                HourlyKline {
                    symbol: (*symbol).to_owned(),
                    open_ts_ms,
                    available_at_ms: golden.decision_ts_ms,
                    open: close,
                    high: close,
                    low: close,
                    close,
                    volume_base: 1.0,
                    turnover_quote: (symbol_index + 1) as f64 * 1000.0 + (hour % 24) as f64 * 17.0,
                },
            );
        }
        for settlement in 0..=golden.days * 3 {
            let settlement_ts_ms = golden.base_ts_ms + settlement as i64 * 8 * HOUR_MS;
            let rate = -0.0015 + 0.0003 * (settlement as f64 / 5.0 + symbol_index as f64).sin();
            Arc::make_mut(funding.entry((*symbol).to_owned()).or_default()).insert(
                settlement_ts_ms,
                SettledFunding {
                    symbol: (*symbol).to_owned(),
                    settlement_ts_ms,
                    available_at_ms: golden.decision_ts_ms,
                    rate,
                    funding_interval_min: 480,
                },
            );
        }
        for day in 0..=golden.days {
            let day_end_ms = golden.base_ts_ms + day as i64 * DAY_MS;
            Arc::make_mut(whales.entry((*symbol).to_owned()).or_default()).insert(
                day_end_ms,
                BinanceWhaleObservation {
                    symbol: (*symbol).to_owned(),
                    day_end_ms,
                    available_at_ms: golden.decision_ts_ms,
                    long_short_ratio: Some(
                        1.5 + symbol_index as f64 * 0.1
                            + 0.02 * (day as f64 / 3.0 + symbol_index as f64).sin(),
                    ),
                },
            );
        }
    }
    let config = CarryFeatureConfig {
        config_id: "lane2_carry_hold_v7".into(),
        universe_top_n: 100,
        enter_bp: 10.0,
        persistence_window_settlements: Some(20),
        momentum_lookback_hours: 168,
        adv_window_hours: 24,
        return_lookback_hours: 72,
        vol_window_hours: 720,
        vol_return_lag_hours: 24,
        vol_required_finite_samples: 720,
        trail_window_hours: 24,
        trail_change_lookback_hours: 48,
        turn_growth_lookback_hours: 72,
        whale_change_lookback_hours: 72,
        whale_freshness_hours: 48,
        whale_feed_days: 6,
        settlement_age_reset_threshold_hours: 0.5,
        decision_phase_ms: 0,
        decision_kline_lag_ms: 1_200_000,
        minimum_replay_days: 90,
        minimum_decision_symbols: 50,
        minimum_funding_coverage: 0.5,
        standing_funding_max_age_hours: 25.0,
        presettlement_window_ms: 900_000,
        missing_conditioning: "fail_open".into(),
        missing_depth: "floor".into(),
        stale_whale: "null_fail_open".into(),
    };
    let actual = build_carry_features_at(
        &klines,
        &funding,
        &whales,
        &symbols
            .iter()
            .map(|value| (*value).to_owned())
            .collect::<Vec<_>>(),
        golden.decision_ts_ms,
        golden.decision_ts_ms,
        &config,
    );
    assert_eq!(actual.rows.len(), golden.rows.len());
    for (actual, expected) in actual.rows.iter().zip(&golden.rows) {
        assert_eq!(actual.symbol, expected.symbol);
        assert_eq!(actual.bar_ts_ms, expected.bar_ts_ms);
        assert_eq!(actual.adv_rank, expected.adv_rank);
        assert_eq!(actual.in_universe, expected.in_universe);
        close(actual.by_close, expected.by_close);
        close(actual.by_turnover_quote, expected.by_turnover_quote);
        close_option(actual.by_funding, expected.by_funding);
        close_option(actual.by_funding_age_h, expected.by_funding_age_h);
        close_option(actual.adv24, expected.adv24);
        close_option(actual.trail_fund_24h, expected.trail_fund_24h);
        close_option(actual.momentum, expected.momentum);
        close_option(actual.ret_3d, expected.ret_3d);
        close_option(actual.vol_30d_daily, expected.vol_30d_daily);
        close_option(actual.dtrail_2d, expected.dtrail_2d);
        close_option(actual.crowd_persistence, expected.crowd_persistence);
        close_option(actual.turn_growth_3d, expected.turn_growth_3d);
        close_option(actual.d_tt_ls_3d, expected.d_tt_ls_3d);
    }
    engine_strategies::native_common::validate_exact_symbol_coverage(
        &symbols
            .iter()
            .map(|symbol| (*symbol).to_owned())
            .collect::<Vec<_>>(),
        &actual
            .rows
            .iter()
            .filter(|row| row.bar_ts_ms == golden.decision_ts_ms)
            .map(|row| row.symbol.clone())
            .collect::<Vec<_>>(),
        &actual
            .rejections
            .iter()
            .map(|row| row.symbol.clone())
            .collect::<Vec<_>>(),
    )
    .expect("CARRY producer emits an exact sleeve partition");

    let mut gapped = klines.clone();
    let missing = golden.decision_ts_ms - 10 * HOUR_MS;
    Arc::make_mut(gapped.get_mut("AAAUSDT").unwrap()).remove(&missing);
    let gapped = build_carry_features_at(
        &gapped,
        &funding,
        &whales,
        &symbols
            .iter()
            .map(|value| (*value).to_owned())
            .collect::<Vec<_>>(),
        golden.decision_ts_ms,
        golden.decision_ts_ms,
        &config,
    );
    assert!(gapped.rows.iter().all(|row| row.symbol != "AAAUSDT"));
    assert_eq!(
        gapped
            .rejections
            .iter()
            .filter(|row| row.symbol == "AAAUSDT")
            .count(),
        1,
        "a gapped symbol is rejected instead of accepted and rejected"
    );
}

#[test]
fn realistic_150_symbol_90_day_payload_fits_cap_and_replays_native() {
    use engine_strategies::native_carry::scorer::{
        score_decision, CarryFeatureRow as NativeCarryFeatureRow, CarryRuleConfig, ScorerState,
    };

    let first_ts_ms = 10 * DAY_MS;
    let decision_ts_ms = first_ts_ms + 90 * DAY_MS;
    let mut rows = Vec::new();
    for symbol_index in 0..150 {
        let symbol = format!("S{symbol_index:03}USDT");
        for day in 0..=90 {
            rows.push(CarryFeatureRow {
                symbol: symbol.clone(),
                bar_ts_ms: first_ts_ms + day * DAY_MS,
                by_close: 100.0 + symbol_index as f64,
                by_turnover_quote: 1_000_000.0 + symbol_index as f64,
                by_funding: Some(-0.002),
                by_funding_age_h: Some(0.0),
                adv24: Some(10_000_000.0 + symbol_index as f64),
                trail_fund_24h: Some(-0.012),
                momentum: Some(0.1),
                ret_3d: Some(0.1),
                vol_30d_daily: Some(0.1),
                dtrail_2d: Some(0.0),
                crowd_persistence: Some(1.0),
                turn_growth_3d: Some(1.0),
                d_tt_ls_3d: Some(1.0),
                adv_rank: Some(symbol_index + 1),
                in_universe: true,
            });
        }
    }
    rows.sort_by(|left, right| {
        (&left.symbol, left.bar_ts_ms).cmp(&(&right.symbol, right.bar_ts_ms))
    });
    let encoded_rows = serde_json::to_vec(&rows).unwrap();
    assert_eq!(rows.len(), 150 * 91);
    assert!(encoded_rows.len() < engine_types::MAX_SIGNAL_OBSERVATION_BYTES);
    let native_rows: Vec<NativeCarryFeatureRow> = serde_json::from_slice(&encoded_rows).unwrap();
    let rule = CarryRuleConfig {
        config_id: "lane2_carry_hold_v7".into(),
        universe_top_n: 100,
        enter_bp: 10.0,
        exit_bp: 3.0,
        per_name_cap: 0.1,
        gross_cap: 1.0,
        depth_ref_bp_per_day: 120.0,
        depth_floor: 0.25,
        depth_exponent: 1.5,
        toxic_band_ret3d_lo: -0.30,
        toxic_band_ret3d_hi: 0.0,
        min_vol30_daily: 0.05,
        trail_recovery_exit_bp_2d: 30.0,
        persistence_cut: 0.10,
        persistence_lo: 0.0,
        flow_cut: 0.40,
        flow_lo: 0.5,
        whale_cut: -0.26,
        whale_lo: 0.5,
    };
    let (decision, state) =
        score_decision(&native_rows, decision_ts_ms, &ScorerState::default(), &rule)
            .expect("worker cold replay must satisfy the native 45-day floor");
    assert_eq!(decision.replay_days, 90);
    assert_eq!(decision.universe_size, 100);
    assert_eq!(state.last_decision_ts_ms, decision_ts_ms);

    let upcoming_worker_rows: Vec<CarryFeatureRow> = rows
        .iter()
        .filter(|row| row.bar_ts_ms == decision_ts_ms)
        .cloned()
        .map(|mut row| {
            row.bar_ts_ms += DAY_MS;
            row
        })
        .collect();
    let symbols = (0..150)
        .map(|symbol_index| format!("S{symbol_index:03}USDT"))
        .collect::<Vec<_>>();
    let envelope = crate::model::SignalPayloadEnvelope {
        schema_version: crate::SCHEMA_VERSION,
        config: crate::config::ConfigIdentity {
            schema_version: crate::SCHEMA_VERSION,
            signal_config_id: "realistic_150_symbol_replay".into(),
            long_profile: "v12".into(),
            long_execution_strategy_id: "long_native_v12_wide_stop".into(),
            signal_config_sha256: "1".repeat(64),
            long_rule_sha256: "2".repeat(64),
            long_feature_contract_sha256: "8".repeat(64),
            carry_config_id: "lane2_carry_hold_v7".into(),
            carry_rule_sha256: "3".repeat(64),
            carry_feature_contract_sha256: "9".repeat(64),
            operational_profile_sha256: "4".repeat(64),
            engine_config_sha256: "5".repeat(64),
            long_decision_fingerprint: "6".repeat(64),
            carry_decision_fingerprint: "7".repeat(64),
        },
        universe: Some(crate::model::UniverseIdentity {
            mode: crate::model::UniverseMode::Pit,
            environment: "demo".into(),
            endpoint: "api-demo.bybit.com".into(),
            snapshot_ts_ms: first_ts_ms,
            available_at_ms: first_ts_ms,
            artifact_sha256: "8".repeat(64),
            file_sha256: "9".repeat(64),
            symbols: symbols.clone(),
            long_symbols: symbols.clone(),
            carry_symbols: symbols,
        }),
        payload: crate::model::ObservationPayload::CarryFeatureBatch {
            decision_ts_ms,
            rows: rows.clone(),
            upcoming_rows: upcoming_worker_rows.clone(),
            settled_funding: Vec::new(),
            presettlement: Vec::new(),
            marks: Vec::new(),
            rejections: Vec::new(),
        },
    };
    let full_payload = serde_json::to_vec(&envelope).unwrap();
    assert!(full_payload.len() < engine_types::MAX_SIGNAL_OBSERVATION_BYTES);
    let upcoming_native_rows: Vec<NativeCarryFeatureRow> =
        serde_json::from_value(serde_json::to_value(&upcoming_worker_rows).unwrap()).unwrap();
    let (upcoming, _) = score_decision(
        &upcoming_native_rows,
        decision_ts_ms + DAY_MS,
        &state,
        &rule,
    )
    .expect("causally frozen upcoming worker rows must advance the native scorer");
    assert_eq!(upcoming.decision_ts_ms, decision_ts_ms + DAY_MS);
}

fn close(actual: f64, expected: f64) {
    let tolerance = 1e-12_f64.max(expected.abs() * 1e-12);
    assert!(
        (actual - expected).abs() <= tolerance,
        "{actual} differs from {expected} by more than {tolerance}"
    );
}

fn close_option(actual: Option<f64>, expected: Option<f64>) {
    assert_eq!(
        actual.is_none(),
        expected.is_none(),
        "null position differs"
    );
    if let (Some(actual), Some(expected)) = (actual, expected) {
        close(actual, expected);
    }
}
