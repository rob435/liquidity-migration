use super::*;
use crate::config::{
    CarryFeatureConfig, LiveAcquisitionConfig, LongFeatureConfig, SignalRouting, SourceContract,
};
use crate::model::{
    BinanceWhaleObservation, BinanceWhaleWire, BybitFundingWire, BybitInstrumentWire,
    BybitTickerWire, HourlyKline, Readiness, SourceCoverage, UniverseMode,
};
use crate::store::{AtomicJsonStore, SpoolWriter};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

#[test]
fn a_contract_at_its_delivery_clock_is_not_trading_whatever_status_says() {
    let mut row = InstrumentObservation {
        symbol: "BTCUSDT".into(),
        observed_ts_ms: 10 * DAY_MS,
        available_at_ms: 10 * DAY_MS,
        contract_type: Some("LinearPerpetual".into()),
        symbol_type: None,
        status: Some("Trading".into()),
        base_coin: Some("BTC".into()),
        quote_coin: Some("USDT".into()),
        settle_coin: Some("USDT".into()),
        launch_time_ms: Some(1),
        delivery_time_ms: Some(0),
        tick_size: Some(0.1),
        qty_step: Some(0.001),
        min_order_qty: Some(0.001),
        min_notional_value: Some(5.0),
        max_order_qty: None,
        max_market_order_qty: None,
        funding_interval_min: Some(480),
        is_prelisting: false,
    };
    assert!(
        instrument_is_trading(&row, "USDT"),
        "a perpetual's clock is zero"
    );
    row.delivery_time_ms = None;
    assert!(instrument_is_trading(&row, "USDT"));
    row.delivery_time_ms = Some(11 * DAY_MS);
    assert!(
        instrument_is_trading(&row, "USDT"),
        "a dated contract trades until its clock"
    );
    row.delivery_time_ms = Some(10 * DAY_MS);
    assert!(
        !instrument_is_trading(&row, "USDT"),
        "at the clock it has stopped"
    );
    row.delivery_time_ms = Some(9 * DAY_MS);
    assert!(!instrument_is_trading(&row, "USDT"));
}

#[test]
fn directional_symbols_request_quote_and_ticker_exactly_once() {
    let subscriptions = market_subscriptions(&[
        "ZUSDT".to_owned(),
        "BTCUSDT".to_owned(),
        "BTCUSDT".to_owned(),
    ])
    .unwrap();
    assert_eq!(
        subscriptions,
        vec![
            Subscription {
                symbol: "BTCUSDT".to_owned(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "BTCUSDT".to_owned(),
                feed: Feed::Ticker,
            },
            Subscription {
                symbol: "ZUSDT".to_owned(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "ZUSDT".to_owned(),
                feed: Feed::Ticker,
            },
        ]
    );
}

#[test]
fn directional_quote_and_ticker_pairs_obey_the_observation_limit() {
    let at_limit = (0..MAX_SIGNAL_SUBSCRIPTIONS / 2)
        .map(|index| format!("S{index:03}USDT"))
        .collect::<Vec<_>>();
    assert_eq!(
        market_subscriptions(&at_limit).unwrap().len(),
        MAX_SIGNAL_SUBSCRIPTIONS
    );

    let over_limit = (0..=MAX_SIGNAL_SUBSCRIPTIONS / 2)
        .map(|index| format!("S{index:03}USDT"))
        .collect::<Vec<_>>();
    let error = market_subscriptions(&over_limit).unwrap_err();
    assert!(error.to_string().contains("quote/ticker subscriptions"));
}

fn test_config() -> SignalWorkerConfig {
    SignalWorkerConfig {
        long: LongFeatureConfig {
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
        },
        carry: CarryFeatureConfig {
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
        },
        routing: SignalRouting {
            source: "directional_public_v1".into(),
            long_sleeve: "long".into(),
            carry_sleeve: "carry".into(),
        },
        sources: SourceContract {
            bybit_category: "linear".into(),
            bybit_settle_coin: "USDT".into(),
            bybit_mainnet_host: "api.bybit.com".into(),
            bybit_demo_host: "api-demo.bybit.com".into(),
            binance_host: "fapi.binance.com".into(),
            kline_interval_minutes: 60,
            funding_event_kind: "settlement".into(),
            whale_source: "binance_toptrader_position_long_short_ratio".into(),
            whale_period: "5m_eod".into(),
            mark_max_age_ms: 30_000,
            universe_identity_required: true,
        },
        live: LiveAcquisitionConfig {
            environment: "demo".into(),
            public_market_realm: "mainnet".into(),
            request_timeout_ms: 10_000,
            request_retries: 3,
            retry_base_ms: 500,
            ticker_cadence_ms: 5_000,
            instrument_cadence_ms: 3_600_000,
            funding_cadence_ms: 60_000,
            kline_cadence_ms: 60_000,
            whale_cadence_ms: 3_600_000,
            max_parallel_requests: 4,
            kline_page_limit: 1_000,
            funding_page_limit: 200,
            whale_page_limit: 500,
            instrument_max_pages: 10,
        },
        universe: crate::universe::UniverseRules::default(),
        llm_gate: crate::config::LlmGateConfig::default(),
        identity: ConfigIdentity {
            schema_version: SCHEMA_VERSION,
            signal_config_id: "test".into(),
            long_profile: "v12".into(),
            long_execution_strategy_id: "long_native_v12_wide_stop".into(),
            signal_config_sha256: "a".repeat(64),
            long_rule_sha256: "9".repeat(64),
            long_feature_contract_sha256: "8".repeat(64),
            carry_config_id: "lane2_carry_hold_v7".into(),
            carry_rule_sha256: "b".repeat(64),
            carry_feature_contract_sha256: "7".repeat(64),
            operational_profile_sha256: "c".repeat(64),
            engine_config_sha256: "d".repeat(64),
            long_decision_fingerprint: "e".repeat(64),
            carry_decision_fingerprint: "f".repeat(64),
        },
        long_destination: 1,
        carry_destination: 0,
        signal_path: PathBuf::from("signal.json"),
        long_rule_path: PathBuf::from("long.json"),
        carry_path: PathBuf::from("carry.json"),
        operational_path: PathBuf::from("operational.json"),
        engine_path: PathBuf::from("engine.toml"),
    }
}

fn test_universe() -> UniverseIdentity {
    UniverseIdentity {
        mode: UniverseMode::Pit,
        environment: "demo".into(),
        endpoint: "api-demo.bybit.com".into(),
        snapshot_ts_ms: DAY_MS,
        available_at_ms: DAY_MS + 1,
        artifact_sha256: "1".repeat(64),
        file_sha256: "2".repeat(64),
        symbols: vec!["BTCUSDT".into()],
        long_symbols: vec!["BTCUSDT".into()],
        carry_symbols: vec!["BTCUSDT".into()],
    }
}

fn readiness(reason: String) -> ObservationPayload {
    ObservationPayload::Readiness {
        readiness: Readiness {
            long_ready: false,
            carry_ready: false,
            universe_ready: true,
            reason,
            long_feature_ts_ms: None,
            carry_feature_ts_ms: None,
            rejected_symbols: Vec::new(),
        },
    }
}

fn empty_readiness() -> ObservationPayload {
    readiness("test".into())
}

fn ticker_wire(symbol: &str, price: f64) -> BybitTickerWire {
    let price = Some(serde_json::Value::from(price.to_string()));
    BybitTickerWire {
        symbol: symbol.to_owned(),
        mark_observed_ts_ms: None,
        funding_observed_ts_ms: None,
        schedule_observed_ts_ms: None,
        last_price: price.clone(),
        mark_price: price.clone(),
        index_price: price.clone(),
        bid1_price: price.clone(),
        ask1_price: price,
        bid1_size: Some(serde_json::Value::from("1")),
        ask1_size: Some(serde_json::Value::from("1")),
        open_interest: None,
        open_interest_value: None,
        turnover24h: None,
        volume24h: None,
        funding_rate: None,
        next_funding_time: None,
    }
}

fn install_trading_instrument(worker: &mut SignalWorker, symbol: &str) {
    worker.state.instruments.insert(
        symbol.to_owned(),
        InstrumentObservation {
            symbol: symbol.to_owned(),
            observed_ts_ms: DAY_MS,
            available_at_ms: DAY_MS,
            contract_type: Some("LinearPerpetual".into()),
            symbol_type: None,
            status: Some("Trading".into()),
            base_coin: Some(symbol.trim_end_matches("USDT").into()),
            quote_coin: Some("USDT".into()),
            settle_coin: Some("USDT".into()),
            launch_time_ms: Some(1),
            delivery_time_ms: None,
            tick_size: Some(0.01),
            qty_step: Some(0.001),
            min_order_qty: Some(0.001),
            min_notional_value: Some(5.0),
            max_order_qty: None,
            max_market_order_qty: None,
            funding_interval_min: Some(480),
            is_prelisting: false,
        },
    );
    worker.state.instrument_trading_intervals.insert(
        symbol.to_owned(),
        vec![InstrumentTradingInterval {
            trading_from_ms: 1,
            trading_through_ms: None,
        }],
    );
}

fn compact_feature_config() -> SignalWorkerConfig {
    let mut config = test_config();
    config.long.universe_size = 1;
    config.long.universe_volume_window_days = 2;
    config.long.min_listing_history_days = 1;
    config.long.regime_sma_days = 2;
    config.long.vol_estimate_window_days = 2;
    config.long.daily_min_hourly_bars = 1;
    config.long.cold_start_lookback_days = 3;
    config.long.pump_lookback_days = [1, 2];
    config.long.atr_window_days = 2;
    config.long.atr_min_samples = 1;
    config.long.btc_rv_window_days = 2;
    config.long.btc_rv_min_samples = 1;
    config.carry.universe_top_n = 1;
    config.carry.persistence_window_settlements = None;
    config.carry.momentum_lookback_hours = 2;
    config.carry.adv_window_hours = 1;
    config.carry.return_lookback_hours = 1;
    config.carry.vol_window_hours = 4;
    config.carry.vol_return_lag_hours = 1;
    config.carry.vol_required_finite_samples = 4;
    config.carry.trail_window_hours = 1;
    config.carry.trail_change_lookback_hours = 2;
    config.carry.turn_growth_lookback_hours = 2;
    config.carry.minimum_replay_days = 0;
    config.carry.minimum_decision_symbols = 1;
    config.carry.minimum_funding_coverage = 1.0;
    config.carry.decision_kline_lag_ms = 0;
    config
}

fn install_compact_history(worker: &mut SignalWorker, through_day: i64) {
    install_trading_instrument(worker, "BTCUSDT");
    let klines = worker.state.klines.entry("BTCUSDT".into()).or_default();
    for open_ts_ms in (DAY_MS..through_day * DAY_MS).step_by(HOUR_MS as usize) {
        klines.insert(
            open_ts_ms,
            HourlyKline {
                symbol: "BTCUSDT".into(),
                open_ts_ms,
                available_at_ms: open_ts_ms + HOUR_MS,
                open: 100.0,
                high: 102.0,
                low: 99.0,
                close: 100.0 + open_ts_ms as f64 / DAY_MS as f64,
                volume_base: 1.0,
                turnover_quote: 100.0,
            },
        );
    }
    let funding = worker.state.funding.entry("BTCUSDT".into()).or_default();
    for settlement_ts_ms in (DAY_MS..=through_day * DAY_MS).step_by(HOUR_MS as usize) {
        funding.insert(
            settlement_ts_ms,
            SettledFunding {
                symbol: "BTCUSDT".into(),
                settlement_ts_ms,
                available_at_ms: settlement_ts_ms,
                rate: -0.001,
                funding_interval_min: 60,
            },
        );
    }
}

fn compact_kline_rows(day: i64) -> Vec<Vec<Value>> {
    ((day - 1) * DAY_MS..day * DAY_MS)
        .step_by(HOUR_MS as usize)
        .map(|open_ts_ms| {
            vec![
                Value::from(open_ts_ms),
                Value::from("100"),
                Value::from("200"),
                Value::from("1"),
                Value::from((100.0 + open_ts_ms as f64 / DAY_MS as f64).to_string()),
                Value::from("1"),
                Value::from("100"),
            ]
        })
        .collect()
}

fn compact_funding_rows(day: i64) -> Vec<BybitFundingWire> {
    (((day - 1) * DAY_MS + HOUR_MS)..=day * DAY_MS)
        .step_by(HOUR_MS as usize)
        .map(|settlement_ts_ms| BybitFundingWire {
            funding_rate_timestamp: Value::from(settlement_ts_ms),
            funding_rate: Value::from("-0.001"),
            funding_interval_hour: Some(Value::from(1)),
        })
        .collect()
}

fn trading_instrument_wire(symbol: &str, launch_time_ms: i64) -> BybitInstrumentWire {
    BybitInstrumentWire {
        symbol: symbol.to_owned(),
        contract_type: Some("LinearPerpetual".into()),
        symbol_type: None,
        status: Some("Trading".into()),
        base_coin: Some(symbol.trim_end_matches("USDT").into()),
        quote_coin: Some("USDT".into()),
        settle_coin: Some("USDT".into()),
        launch_time: Some(Value::from(launch_time_ms)),
        delivery_time: None,
        price_filter: BTreeMap::new(),
        lot_size_filter: BTreeMap::new(),
        funding_interval: Some(Value::from(60)),
        is_pre_listing: false,
    }
}

fn closed_instrument_wire(
    symbol: &str,
    launch_time_ms: i64,
    delivery_time_ms: i64,
) -> BybitInstrumentWire {
    let mut row = trading_instrument_wire(symbol, launch_time_ms);
    row.status = Some("Closed".into());
    row.delivery_time = Some(Value::from(delivery_time_ms));
    row
}

fn clone_symbol_history(worker: &mut SignalWorker, source: &str, target: &str) {
    let klines = worker.state.klines[source]
        .values()
        .cloned()
        .map(|mut row| {
            row.symbol = target.to_owned();
            (row.open_ts_ms, row)
        })
        .collect();
    worker.state.klines.insert(target.to_owned(), klines);
    let funding = worker.state.funding[source]
        .values()
        .cloned()
        .map(|mut row| {
            row.symbol = target.to_owned();
            (row.settlement_ts_ms, row)
        })
        .collect();
    worker.state.funding.insert(target.to_owned(), funding);
}

fn worker_with_newer_prunable_state() -> SignalWorker {
    let newer_at_ms = 200 * DAY_MS;
    let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
    worker.state.last_observed_ts_ms = newer_at_ms;
    let kline_ts_ms = newer_at_ms - HOUR_MS;
    worker
        .state
        .klines
        .entry("BTCUSDT".into())
        .or_default()
        .insert(
            kline_ts_ms,
            HourlyKline {
                symbol: "BTCUSDT".into(),
                open_ts_ms: kline_ts_ms,
                available_at_ms: newer_at_ms,
                open: 100.0,
                high: 101.0,
                low: 99.0,
                close: 100.0,
                volume_base: 1.0,
                turnover_quote: 100.0,
            },
        );
    let funding_ts_ms = newer_at_ms - HOUR_MS;
    worker
        .state
        .funding
        .entry("BTCUSDT".into())
        .or_default()
        .insert(
            funding_ts_ms,
            SettledFunding {
                symbol: "BTCUSDT".into(),
                settlement_ts_ms: funding_ts_ms,
                available_at_ms: newer_at_ms,
                rate: -0.001,
                funding_interval_min: 60,
            },
        );
    let whale_ts_ms = newer_at_ms - DAY_MS;
    worker
        .state
        .whales
        .entry("BTCUSDT".into())
        .or_default()
        .insert(
            whale_ts_ms,
            BinanceWhaleObservation {
                symbol: "BTCUSDT".into(),
                day_end_ms: whale_ts_ms,
                available_at_ms: newer_at_ms,
                long_short_ratio: Some(1.0),
            },
        );
    worker.state.instrument_trading_intervals.insert(
        "BTCUSDT".into(),
        vec![InstrumentTradingInterval {
            trading_from_ms: newer_at_ms - 2 * DAY_MS,
            trading_through_ms: Some(newer_at_ms - DAY_MS),
        }],
    );
    worker
}

fn assert_stale_source_event_preserves_newer_state(label: &str, event: WireEvent) {
    let newer_at_ms = 200 * DAY_MS;
    let mut worker = worker_with_newer_prunable_state();
    worker.apply(event).unwrap();

    assert_eq!(worker.state.last_observed_ts_ms, newer_at_ms, "{label}");
    assert!(
        worker.state.klines["BTCUSDT"].contains_key(&(newer_at_ms - HOUR_MS)),
        "{label}"
    );
    assert!(
        worker.state.funding["BTCUSDT"].contains_key(&(newer_at_ms - HOUR_MS)),
        "{label}"
    );
    assert!(
        worker.state.whales["BTCUSDT"].contains_key(&(newer_at_ms - DAY_MS)),
        "{label}"
    );
    assert_eq!(
        worker.state.instrument_trading_intervals["BTCUSDT"],
        vec![InstrumentTradingInterval {
            trading_from_ms: newer_at_ms - 2 * DAY_MS,
            trading_through_ms: Some(newer_at_ms - DAY_MS),
        }],
        "{label}"
    );
}

fn temporary_root(label: &str) -> PathBuf {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    std::env::temp_dir().join(format!(
        "signal-worker-{label}-{}-{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ))
}

#[test]
fn duplicate_public_rows_must_be_byte_equivalent() {
    let mut rows = BTreeMap::new();
    insert_exact(&mut rows, 1, 2_u64, "row").unwrap();
    insert_exact(&mut rows, 1, 2_u64, "row").unwrap();
    assert!(insert_exact(&mut rows, 1, 3_u64, "row").is_err());
}

#[test]
fn kline_replacement_clears_coverage_before_frontier_validation_and_restart() {
    let from = 10 * DAY_MS;
    let through = 11 * DAY_MS;
    let cases = [
        (true, None, None, true, false),
        (false, None, None, true, true),
        (true, Some(from), None, false, false),
        (false, Some(from), None, false, true),
        (true, Some(from), Some(through + HOUR_MS), false, false),
        (true, Some(from + 1), Some(through), false, false),
    ];
    for (replace, checked_from_ms, checked_through_ms, succeeds, retained) in cases {
        let config = test_config();
        let mut worker = SignalWorker::with_universe(config.clone(), test_universe()).unwrap();
        let event = |sequence, checked_from_ms, checked_through_ms, replace_coverage| {
            WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "BTCUSDT".into(),
                available_at_ms: through,
                checked_from_ms,
                checked_through_ms,
                replace_coverage,
                rows: Vec::new(),
            }
        };
        worker
            .apply(event(1, Some(from), Some(through), false))
            .unwrap();
        let result = worker.apply(event(2, checked_from_ms, checked_through_ms, replace));
        assert_eq!(
            result.is_ok(),
            succeeds,
            "{replace:?} {checked_from_ms:?} {checked_through_ms:?}"
        );
        let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
        for state in [&worker.state, &restored.state] {
            assert_eq!(
                state.kline_checked_from_ms.get("BTCUSDT").copied(),
                retained.then_some(from)
            );
            assert_eq!(
                state.kline_checked_through_ms.get("BTCUSDT").copied(),
                retained.then_some(through)
            );
            assert_eq!(
                state.kline_coverage_intervals.contains_key("BTCUSDT"),
                retained
            );
        }
    }
}

#[test]
fn funding_replacement_without_frontier_retains_coverage() {
    let from = 10 * DAY_MS;
    let through = 11 * DAY_MS;
    for replace_coverage in [false, true] {
        let config = test_config();
        let mut worker = SignalWorker::with_universe(config.clone(), test_universe()).unwrap();
        for (sequence, checked_from_ms, checked_through_ms) in
            [(1, Some(from), Some(through)), (2, None, None)]
        {
            worker
                .apply(WireEvent::BybitFundingBatch {
                    schema_version: SCHEMA_VERSION,
                    sequence,
                    symbol: "BTCUSDT".into(),
                    available_at_ms: through,
                    checked_from_ms,
                    checked_through_ms,
                    replace_coverage,
                    emit_lifecycle: false,
                    rows: Vec::new(),
                })
                .unwrap();
        }
        let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
        for state in [&worker.state, &restored.state] {
            assert_eq!(
                state.funding_checked_from_ms.get("BTCUSDT").copied(),
                Some(from)
            );
            assert_eq!(
                state.funding_checked_through_ms.get("BTCUSDT").copied(),
                Some(through)
            );
            assert_eq!(state.funding_coverage_intervals["BTCUSDT"].len(), 1);
        }
    }
}

#[test]
fn disjoint_kline_windows_survive_restart_without_claiming_the_gap() {
    let config = test_config();
    let universe = test_universe();
    let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
    for (sequence, checked_from_ms) in [(1, 10 * DAY_MS), (2, 100 * DAY_MS)] {
        worker
            .apply(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "BTCUSDT".into(),
                available_at_ms: checked_from_ms + DAY_MS,
                checked_from_ms: Some(checked_from_ms),
                checked_through_ms: Some(checked_from_ms + DAY_MS),
                replace_coverage: false,
                rows: Vec::new(),
            })
            .unwrap();
    }
    assert_eq!(worker.state.kline_coverage_intervals["BTCUSDT"].len(), 2);
    assert!(!worker.state.kline_checked_from_ms.contains_key("BTCUSDT"));
    assert!(!worker
        .state
        .kline_checked_through_ms
        .contains_key("BTCUSDT"));

    let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
    let intervals = &restored.state.kline_coverage_intervals["BTCUSDT"];
    assert_eq!(intervals.len(), 2);
    assert_eq!(intervals[0].checked_through_ms, 11 * DAY_MS);
    assert_eq!(intervals[1].checked_from_ms, 100 * DAY_MS);
}

#[test]
fn fragmented_source_coverage_survives_and_repeated_fetch_converges() {
    let config = test_config();
    let universe = test_universe();
    let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
    let base = 10 * DAY_MS;
    let available_at_ms = 11 * DAY_MS;
    let mut sequence = 1;

    for index in 0..6 {
        let checked_from_ms = base + index * 2 * HOUR_MS;
        worker
            .apply(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "BTCUSDT".into(),
                available_at_ms,
                checked_from_ms: Some(checked_from_ms),
                checked_through_ms: Some(checked_from_ms + HOUR_MS),
                replace_coverage: false,
                rows: Vec::new(),
            })
            .unwrap();
        sequence += 1;
    }
    for index in 0..6 {
        let checked_from_ms = base + index * 2 * HOUR_MS;
        worker
            .apply(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "BTCUSDT".into(),
                available_at_ms,
                checked_from_ms: Some(checked_from_ms),
                checked_through_ms: Some(checked_from_ms + HOUR_MS),
                replace_coverage: false,
                emit_lifecycle: false,
                rows: Vec::new(),
            })
            .unwrap();
        sequence += 1;
    }
    for index in 0..6 {
        let checked_from_ms = base + index * 2 * HOUR_MS;
        worker
            .apply(WireEvent::BinanceWhaleBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                available_at_ms,
                coverage: vec![SourceCoverage {
                    symbol: "BTCUSDT".into(),
                    checked_from_ms,
                    checked_through_ms: checked_from_ms + HOUR_MS,
                    replace_coverage: false,
                }],
                rows: Vec::new(),
            })
            .unwrap();
        sequence += 1;
    }

    for intervals in [
        &worker.state.kline_coverage_intervals["BTCUSDT"],
        &worker.state.funding_coverage_intervals["BTCUSDT"],
        &worker.state.whale_coverage_intervals["BTCUSDT"],
    ] {
        assert_eq!(intervals.len(), 6);
        assert_eq!(intervals[0].checked_from_ms, base);
        assert_eq!(intervals[1].checked_from_ms, base + 2 * HOUR_MS);
        assert!(intervals
            .windows(2)
            .all(|pair| pair[0].checked_through_ms < pair[1].checked_from_ms));
    }

    let before = (
        worker.state.kline_coverage_intervals.clone(),
        worker.state.funding_coverage_intervals.clone(),
        worker.state.whale_coverage_intervals.clone(),
    );
    let repeated_from_ms = base + 4 * HOUR_MS;
    worker
        .apply(WireEvent::BybitKlineBatch {
            schema_version: SCHEMA_VERSION,
            sequence,
            symbol: "BTCUSDT".into(),
            available_at_ms,
            checked_from_ms: Some(repeated_from_ms),
            checked_through_ms: Some(repeated_from_ms + HOUR_MS),
            replace_coverage: false,
            rows: Vec::new(),
        })
        .unwrap();
    sequence += 1;
    worker
        .apply(WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence,
            symbol: "BTCUSDT".into(),
            available_at_ms,
            checked_from_ms: Some(repeated_from_ms),
            checked_through_ms: Some(repeated_from_ms + HOUR_MS),
            replace_coverage: false,
            emit_lifecycle: false,
            rows: Vec::new(),
        })
        .unwrap();
    sequence += 1;
    worker
        .apply(WireEvent::BinanceWhaleBatch {
            schema_version: SCHEMA_VERSION,
            sequence,
            available_at_ms,
            coverage: vec![SourceCoverage {
                symbol: "BTCUSDT".into(),
                checked_from_ms: repeated_from_ms,
                checked_through_ms: repeated_from_ms + HOUR_MS,
                replace_coverage: false,
            }],
            rows: Vec::new(),
        })
        .unwrap();
    sequence += 1;
    assert_eq!(
        before,
        (
            worker.state.kline_coverage_intervals.clone(),
            worker.state.funding_coverage_intervals.clone(),
            worker.state.whale_coverage_intervals.clone(),
        )
    );

    let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
    assert_eq!(restored.state.last_input_sequence, sequence - 1);
    assert_eq!(restored.state.whale_coverage_intervals["BTCUSDT"].len(), 6);
}

#[test]
fn prune_splits_source_coverage_and_restart_cannot_overclaim_the_gap() {
    let config = test_config();
    let universe = test_universe();
    let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
    worker.state.last_carry_decision_ts_ms = Some(10 * DAY_MS);
    let broad = vec![CoverageInterval {
        checked_from_ms: DAY_MS,
        checked_through_ms: 200 * DAY_MS,
    }];
    worker
        .state
        .funding_coverage_intervals
        .insert("BTCUSDT".into(), broad.clone());
    worker
        .state
        .whale_coverage_intervals
        .insert("BTCUSDT".into(), broad);
    worker.prune(200 * DAY_MS);

    for intervals in [
        &worker.state.funding_coverage_intervals["BTCUSDT"],
        &worker.state.whale_coverage_intervals["BTCUSDT"],
    ] {
        assert_eq!(intervals.len(), 2);
        assert!(intervals[0].checked_through_ms <= 11 * DAY_MS);
        assert!(intervals[1].checked_from_ms > 100 * DAY_MS);
        assert!(intervals[0].checked_through_ms < intervals[1].checked_from_ms);
    }
    assert!(!worker.state.funding_checked_from_ms.contains_key("BTCUSDT"));
    assert!(!worker.state.whale_checked_from_ms.contains_key("BTCUSDT"));

    let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
    assert_eq!(
        restored.state.funding_coverage_intervals["BTCUSDT"].len(),
        2
    );
    assert_eq!(restored.state.whale_coverage_intervals["BTCUSDT"].len(), 2);
    assert!(!restored
        .state
        .funding_checked_through_ms
        .contains_key("BTCUSDT"));
    assert!(!restored
        .state
        .whale_checked_through_ms
        .contains_key("BTCUSDT"));
}

#[test]
fn stale_kline_availability_cannot_roll_back_pruning() {
    assert_stale_source_event_preserves_newer_state(
        "kline",
        WireEvent::BybitKlineBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            symbol: "BTCUSDT".into(),
            available_at_ms: 20 * DAY_MS,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            rows: Vec::new(),
        },
    );
}

#[test]
fn stale_funding_availability_cannot_roll_back_pruning() {
    assert_stale_source_event_preserves_newer_state(
        "funding",
        WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            symbol: "BTCUSDT".into(),
            available_at_ms: 20 * DAY_MS,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            emit_lifecycle: false,
            rows: Vec::new(),
        },
    );
}

#[test]
fn stale_instrument_availability_cannot_roll_back_pruning() {
    assert_stale_source_event_preserves_newer_state(
        "instrument",
        WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 20 * DAY_MS,
            available_at_ms: 20 * DAY_MS,
            rows: Vec::new(),
        },
    );
}

#[test]
fn stale_whale_availability_cannot_roll_back_pruning() {
    assert_stale_source_event_preserves_newer_state(
        "whale",
        WireEvent::BinanceWhaleBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            available_at_ms: 20 * DAY_MS,
            coverage: Vec::new(),
            rows: Vec::new(),
        },
    );
}

#[test]
fn source_ingestion_stays_bounded_when_no_watermark_can_complete() {
    let config = test_config();
    let mut universe = test_universe();
    universe.symbols = vec!["AAAUSDT".into(), "BTCUSDT".into()];
    universe.long_symbols = vec!["AAAUSDT".into()];
    universe.carry_symbols = vec!["AAAUSDT".into()];
    let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
    let mut sequence = 1_u64;
    for day in 1..=180_i64 {
        let start = day * DAY_MS;
        let end = start + DAY_MS;
        let kline_rows = (0..24_i64)
            .map(|hour| {
                vec![
                    Value::from(start + hour * HOUR_MS),
                    Value::from("1"),
                    Value::from("1"),
                    Value::from("1"),
                    Value::from("1"),
                    Value::from("1"),
                    Value::from("1"),
                ]
            })
            .collect();
        worker
            .apply(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "AAAUSDT".into(),
                available_at_ms: end,
                checked_from_ms: Some(start),
                checked_through_ms: Some(end),
                replace_coverage: false,
                rows: kline_rows,
            })
            .unwrap();
        sequence += 1;
        let funding_rows = [8_i64, 16, 24]
            .into_iter()
            .map(|hour| BybitFundingWire {
                funding_rate_timestamp: Value::from(start + hour * HOUR_MS),
                funding_rate: Value::from("-0.001"),
                funding_interval_hour: Some(Value::from(8)),
            })
            .collect();
        worker
            .apply(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "AAAUSDT".into(),
                available_at_ms: end,
                checked_from_ms: Some(start),
                checked_through_ms: Some(end),
                replace_coverage: false,
                emit_lifecycle: false,
                rows: funding_rows,
            })
            .unwrap();
        sequence += 1;
        worker
            .apply(WireEvent::BinanceWhaleBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                available_at_ms: end,
                coverage: vec![SourceCoverage {
                    symbol: "AAAUSDT".into(),
                    checked_from_ms: start,
                    checked_through_ms: end,
                    replace_coverage: false,
                }],
                rows: vec![BinanceWhaleWire {
                    symbol: "AAAUSDT".into(),
                    day_end_ms: Value::from(end),
                    long_short_ratio: Some(Value::from("1.1")),
                }],
            })
            .unwrap();
        sequence += 1;
    }

    let carry_hours = required_carry_history_hours(&config, &worker.state) as usize;
    assert!(worker.state.klines["AAAUSDT"].len() <= carry_hours + 1);
    assert!(worker.state.funding["AAAUSDT"].len() <= carry_hours / 8 + 2);
    assert!(worker.state.whales["AAAUSDT"].len() <= 8);
    assert!(worker.state.kline_coverage_intervals["AAAUSDT"].len() <= 2);
    assert!(worker.state.funding_coverage_intervals["AAAUSDT"].len() <= 2);
    assert!(worker.state.whale_coverage_intervals["AAAUSDT"].len() <= 2);
    assert!(!worker.state.klines.contains_key("BTCUSDT"));

    let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
    assert_eq!(
        restored.state.klines["AAAUSDT"].len(),
        worker.state.klines["AAAUSDT"].len()
    );
    assert_eq!(
        restored.state.funding["AAAUSDT"].len(),
        worker.state.funding["AAAUSDT"].len()
    );
    assert_eq!(
        restored.state.whales["AAAUSDT"].len(),
        worker.state.whales["AAAUSDT"].len()
    );
}

#[test]
fn restore_rejects_noncanonical_source_coverage_intervals() {
    let config = test_config();
    let universe = test_universe();
    let mut state = SignalWorker::with_universe(config.clone(), universe.clone())
        .unwrap()
        .state
        .clone();
    state.funding_coverage_intervals.insert(
        "BTCUSDT".into(),
        vec![
            CoverageInterval {
                checked_from_ms: 2 * DAY_MS,
                checked_through_ms: 4 * DAY_MS,
            },
            CoverageInterval {
                checked_from_ms: 3 * DAY_MS,
                checked_through_ms: 5 * DAY_MS,
            },
        ],
    );
    assert!(SignalWorker::restore(config, state).is_err());
}

#[test]
fn long_fast_forward_records_the_exact_skipped_range() {
    let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
    worker.state.last_long_feature_ts_ms = Some(10 * DAY_MS);
    worker.record_long_fast_forward(14 * DAY_MS);
    assert_eq!(worker.state.long_skipped_generation_count, 3);
    assert_eq!(
        worker.state.last_long_skipped_first_ts_ms,
        Some(11 * DAY_MS)
    );
    assert_eq!(worker.state.last_long_skipped_last_ts_ms, Some(13 * DAY_MS));
    worker.state.last_long_feature_ts_ms = Some(14 * DAY_MS);
    worker.record_long_fast_forward(15 * DAY_MS);
    assert_eq!(worker.state.long_skipped_generation_count, 3);
}

#[test]
fn paused_engine_coalesces_actionable_generations_and_republishes_current_state() {
    let config = compact_feature_config();
    let universe = test_universe();
    let root = temporary_root("paused-engine-current");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let checkpoint = AtomicJsonStore::new(state_dir.join("checkpoint.json"));
    let spool = SpoolWriter::new(&spool_dir).unwrap();

    let mut seeded = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
    install_compact_history(&mut seeded, 9);
    let long = seeded
        .apply(WireEvent::LongWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 8 * DAY_MS,
            data_through_ms: 8 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    let carry = seeded
        .apply(WireEvent::CarryWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: 8 * DAY_MS,
            data_through_ms: 8 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    assert_eq!(long.len(), 1);
    assert_eq!(long[0].kind, "long_feature_batch");
    assert_eq!(carry.len(), 1);
    assert_eq!(carry[0].kind, "carry_feature_batch");
    checkpoint.save(seeded.state()).unwrap();
    let old_long_path = spool.write(&long[0]).unwrap();
    let old_carry_path = spool.write(&carry[0]).unwrap();

    let mut durable = DurableSignalWorker::open_with_universe(
        config.clone(),
        universe.clone(),
        &state_dir,
        &spool_dir,
    )
    .unwrap();
    let mut sequence = 3;
    for day in [9, 10] {
        durable
            .apply_and_commit(WireEvent::BybitKlineBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "BTCUSDT".into(),
                available_at_ms: day * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                rows: compact_kline_rows(day),
            })
            .unwrap();
        sequence += 1;
        durable
            .apply_and_commit(WireEvent::BybitFundingBatch {
                schema_version: SCHEMA_VERSION,
                sequence,
                symbol: "BTCUSDT".into(),
                available_at_ms: day * DAY_MS,
                checked_from_ms: None,
                checked_through_ms: None,
                replace_coverage: false,
                emit_lifecycle: false,
                rows: compact_funding_rows(day),
            })
            .unwrap();
        sequence += 1;
        durable
            .apply_and_commit(WireEvent::LongWatermark {
                schema_version: SCHEMA_VERSION,
                sequence,
                observed_ts_ms: day * DAY_MS,
                data_through_ms: day * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        sequence += 1;
        durable
            .apply_and_commit(WireEvent::CarryWatermark {
                schema_version: SCHEMA_VERSION,
                sequence,
                observed_ts_ms: day * DAY_MS,
                data_through_ms: day * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        sequence += 1;
    }
    assert_eq!(
        durable.worker.state.last_long_feature_ts_ms,
        Some(10 * DAY_MS)
    );
    assert_eq!(
        durable.worker.state.pending_long_refresh_feature_ts_ms,
        Some(10 * DAY_MS)
    );
    assert_eq!(durable.worker.state.long_skipped_generation_count, 2);
    assert_eq!(
        durable.worker.state.last_long_skipped_first_ts_ms,
        Some(8 * DAY_MS)
    );
    assert_eq!(
        durable.worker.state.last_long_skipped_last_ts_ms,
        Some(9 * DAY_MS)
    );
    assert_eq!(
        durable.worker.state.last_carry_decision_ts_ms,
        Some(8 * DAY_MS)
    );
    assert_eq!(
        durable.worker.state.last_carry_scorer_ts_ms,
        Some(10 * DAY_MS)
    );
    drop(durable);

    let mut durable =
        DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir).unwrap();
    assert_eq!(durable.worker.state.last_input_sequence, 10);
    assert_eq!(
        durable.worker.state.last_carry_decision_ts_ms,
        Some(8 * DAY_MS)
    );
    assert_eq!(
        durable.worker.state.last_carry_scorer_ts_ms,
        Some(10 * DAY_MS)
    );
    std::fs::remove_file(old_long_path).unwrap();
    std::fs::remove_file(old_carry_path).unwrap();

    let refreshed_long = durable
        .apply_and_commit(WireEvent::LongWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 11,
            observed_ts_ms: 10 * DAY_MS + 1,
            data_through_ms: 10 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    assert_eq!(refreshed_long.len(), 1);
    assert_eq!(refreshed_long[0].kind, "long_feature_batch");
    let refreshed_carry = durable
        .apply_and_commit(WireEvent::CarryWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 12,
            observed_ts_ms: 10 * DAY_MS + 2,
            data_through_ms: 10 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    assert_eq!(refreshed_carry.len(), 1);
    assert_eq!(refreshed_carry[0].kind, "carry_feature_batch");
    assert_eq!(refreshed_carry[0].observed_wall_ts_ms, 10 * DAY_MS + 2);
    assert_eq!(refreshed_carry[0].available_wall_ts_ms, 10 * DAY_MS + 2);
    assert_eq!(
        durable.worker.state.last_carry_decision_ts_ms,
        Some(10 * DAY_MS)
    );
    assert_eq!(
        durable.worker.state.last_carry_scorer_ts_ms,
        Some(10 * DAY_MS)
    );

    let duplicate_long = durable
        .apply_and_commit(WireEvent::LongWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 13,
            observed_ts_ms: 10 * DAY_MS + 3,
            data_through_ms: 10 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    assert!(duplicate_long.is_empty());
    let duplicate_carry = durable
        .apply_and_commit(WireEvent::CarryWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 14,
            observed_ts_ms: 10 * DAY_MS + 4,
            data_through_ms: 10 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    assert!(duplicate_carry
        .iter()
        .all(|observation| observation.kind == "readiness"));
    let inventory = spool.inventory().unwrap();
    assert!(inventory
        .replaceable_paths
        .contains_key("long_feature_batch"));
    assert!(inventory
        .replaceable_paths
        .contains_key("carry_feature_batch"));
    assert!(inventory.files <= 5);
    assert!(inventory.classes["current"].files <= 3);
    assert_eq!(inventory.classes["catchup"].files, 2);
    assert!(inventory.classes["current"]
        .newest_path
        .as_ref()
        .is_some_and(|path| path.exists()));
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn one_spool_class_at_cap_does_not_block_unrelated_source_commits() {
    let root = temporary_root("class-backpressure");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let mut durable = DurableSignalWorker::open_with_universe(
        test_config(),
        test_universe(),
        &state_dir,
        &spool_dir,
    )
    .unwrap();
    durable.spool_classes.insert(
        "lifecycle".into(),
        SpoolClassInventory {
            files: LIFECYCLE_SPOOL_FILE_CAP,
            bytes: 0,
            oldest_path: None,
            newest_path: None,
        },
    );
    durable.worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
    let blocked = durable
        .apply_and_commit(WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            symbol: "BTCUSDT".into(),
            available_at_ms: 3 * DAY_MS,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            emit_lifecycle: true,
            rows: vec![BybitFundingWire {
                funding_rate_timestamp: Value::from(3 * DAY_MS),
                funding_rate: Value::from("-0.001"),
                funding_interval_hour: Some(Value::from(1)),
            }],
        })
        .unwrap();
    assert!(blocked.is_empty());
    assert_eq!(durable.worker.state.last_input_sequence, 0);
    assert!(durable.spool_backpressured_for("lifecycle"));
    assert!(!durable.spool_backpressured_for("current"));

    durable
        .apply_and_commit(WireEvent::BybitKlineBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            symbol: "BTCUSDT".into(),
            available_at_ms: 2 * DAY_MS,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            rows: Vec::new(),
        })
        .unwrap();
    assert_eq!(durable.worker.state.last_input_sequence, 1);
    let metrics = durable.durability_metrics().unwrap();
    assert_eq!(
        metrics.spool_class_file_caps["lifecycle"],
        LIFECYCLE_SPOOL_FILE_CAP
    );
    assert_eq!(
        metrics.spool_class_files["lifecycle"],
        LIFECYCLE_SPOOL_FILE_CAP
    );
    assert_eq!(metrics.spool_backpressured_classes, vec!["lifecycle"]);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn batch_receipt_exposes_the_uncommitted_suffix_for_exact_retry() {
    let root = temporary_root("batch-backpressure-receipt");
    let mut durable = DurableSignalWorker::open_with_universe(
        test_config(),
        test_universe(),
        root.join("state"),
        root.join("spool"),
    )
    .unwrap();
    durable.worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
    durable.spool_classes.insert(
        "lifecycle".into(),
        SpoolClassInventory {
            files: LIFECYCLE_SPOOL_FILE_CAP - 1,
            bytes: 0,
            oldest_path: None,
            newest_path: None,
        },
    );
    let event = |sequence, settlement_ts_ms| WireEvent::BybitFundingBatch {
        schema_version: SCHEMA_VERSION,
        sequence,
        symbol: "BTCUSDT".into(),
        available_at_ms: settlement_ts_ms,
        checked_from_ms: None,
        checked_through_ms: None,
        replace_coverage: false,
        emit_lifecycle: true,
        rows: vec![BybitFundingWire {
            funding_rate_timestamp: Value::from(settlement_ts_ms),
            funding_rate: Value::from("-0.001"),
            funding_interval_hour: Some(Value::from(1)),
        }],
    };
    let receipt = durable
        .apply_many_and_commit(vec![event(1, 3 * DAY_MS), event(2, 4 * DAY_MS)])
        .unwrap();
    assert_eq!(receipt.attempted_events, 2);
    assert_eq!(receipt.committed_events, 1);
    assert!(!receipt.fully_committed());
    assert_eq!(durable.worker.state.last_input_sequence, 1);
    assert!(durable.worker.state.funding["BTCUSDT"].contains_key(&(3 * DAY_MS)));
    assert!(!durable.worker.state.funding["BTCUSDT"].contains_key(&(4 * DAY_MS)));

    let sentinel = durable.spool_classes["lifecycle"]
        .oldest_path
        .clone()
        .unwrap();
    std::fs::remove_file(sentinel).unwrap();
    durable.refresh_spool_backpressure().unwrap();
    let retried = durable
        .apply_many_and_commit(vec![event(2, 4 * DAY_MS)])
        .unwrap();
    assert!(retried.fully_committed());
    assert_eq!(durable.worker.state.last_input_sequence, 2);
    assert!(durable.worker.state.funding["BTCUSDT"].contains_key(&(4 * DAY_MS)));
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn deleting_the_oldest_class_file_immediately_releases_backpressure() {
    let root = temporary_root("class-oldest-sentinel");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let config = test_config();
    let universe = test_universe();
    let mut durable = DurableSignalWorker::open_with_universe(
        config.clone(),
        universe.clone(),
        &state_dir,
        &spool_dir,
    )
    .unwrap();
    for sequence in 1..=2 {
        let observation = make_observation(
            &config,
            &universe,
            REPLAY_SOURCE_GENERATION,
            false,
            sequence,
            "funding_update",
            2 * DAY_MS + sequence as i64,
            2 * DAY_MS + sequence as i64,
            ObservationPayload::FundingUpdate {
                decision_ts_ms: 2 * DAY_MS,
                settled_funding: Vec::new(),
            },
            Vec::new(),
        )
        .unwrap();
        durable.spool.write(&observation).unwrap();
    }
    let inventory = durable.spool.inventory().unwrap();
    let lifecycle = inventory.classes["lifecycle"].clone();
    let oldest = lifecycle.oldest_path.clone().unwrap();
    let newest = lifecycle.newest_path.clone().unwrap();
    assert_ne!(oldest, newest);
    durable.spool_files = inventory.files;
    durable.spool_bytes = inventory.bytes;
    durable.spool_classes = inventory.classes;
    durable.spool_classes.get_mut("lifecycle").unwrap().files = LIFECYCLE_SPOOL_FILE_CAP;
    durable.worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
    std::fs::remove_file(&oldest).unwrap();
    assert!(newest.exists());

    let emitted = durable
        .apply_and_commit(WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            symbol: "BTCUSDT".into(),
            available_at_ms: 3 * DAY_MS,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            emit_lifecycle: true,
            rows: vec![BybitFundingWire {
                funding_rate_timestamp: Value::from(3 * DAY_MS),
                funding_rate: Value::from("-0.001"),
                funding_interval_hour: Some(Value::from(1)),
            }],
        })
        .unwrap();
    assert_eq!(emitted.len(), 1);
    assert_eq!(durable.worker.state.last_input_sequence, 1);
    assert!(!durable.spool_backpressured_for("lifecycle"));
    assert_eq!(durable.spool_classes["lifecycle"].files, 2);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn carry_watermark_preflight_reserves_only_the_state_relevant_class() {
    let root = temporary_root("carry-class-preflight-current");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let mut durable = DurableSignalWorker::open_with_universe(
        test_config(),
        test_universe(),
        &state_dir,
        &spool_dir,
    )
    .unwrap();
    durable.pending_replaceable_paths.insert(
        "carry_feature_batch".into(),
        state_dir.join("checkpoint.json"),
    );
    durable.spool_classes.insert(
        "catchup".into(),
        SpoolClassInventory {
            files: CATCHUP_SPOOL_FILE_CAP,
            bytes: 0,
            oldest_path: None,
            newest_path: None,
        },
    );
    durable.worker.state.last_carry_decision_ts_ms = Some(DAY_MS);
    durable.worker.state.last_carry_scorer_ts_ms = Some(DAY_MS);
    let current = durable
        .apply_and_commit(WireEvent::CarryWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 2 * DAY_MS,
            data_through_ms: 2 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    assert!(current
        .iter()
        .all(|observation| observation.kind == "readiness"));
    assert_eq!(durable.worker.state.last_input_sequence, 1);

    let blocked_root = temporary_root("carry-class-preflight-catchup");
    let blocked_state = blocked_root.join("state");
    let mut blocked = DurableSignalWorker::open_with_universe(
        test_config(),
        test_universe(),
        &blocked_state,
        blocked_root.join("spool"),
    )
    .unwrap();
    blocked.pending_replaceable_paths.insert(
        "carry_feature_batch".into(),
        blocked_state.join("checkpoint.json"),
    );
    blocked.spool_classes.insert(
        "catchup".into(),
        SpoolClassInventory {
            files: CATCHUP_SPOOL_FILE_CAP,
            bytes: 0,
            oldest_path: None,
            newest_path: None,
        },
    );
    blocked.worker.state.last_carry_decision_ts_ms = Some(DAY_MS);
    blocked.worker.state.last_carry_scorer_ts_ms = Some(0);
    assert!(blocked
        .apply_and_commit(WireEvent::CarryWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 2 * DAY_MS,
            data_through_ms: 2 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap()
        .is_empty());
    assert_eq!(blocked.worker.state.last_input_sequence, 0);
    assert!(blocked.spool_backpressured_for("catchup"));
    std::fs::remove_dir_all(root).unwrap();
    std::fs::remove_dir_all(blocked_root).unwrap();
}

#[test]
fn projected_class_caps_do_not_overshoot_at_the_file_or_byte_edge() {
    let root = temporary_root("projected-class-file-cap");
    let mut durable = DurableSignalWorker::open_with_universe(
        test_config(),
        test_universe(),
        root.join("state"),
        root.join("spool"),
    )
    .unwrap();
    durable.spool_classes.insert(
        "current".into(),
        SpoolClassInventory {
            files: CURRENT_SPOOL_FILE_CAP - 1,
            bytes: 0,
            oldest_path: None,
            newest_path: None,
        },
    );
    assert_eq!(
        durable
            .apply_and_commit(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: 2 * DAY_MS,
                available_at_ms: 2 * DAY_MS,
                rows: vec![ticker_wire("BTCUSDT", 100.0)],
            })
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        durable.spool_classes["current"].files,
        CURRENT_SPOOL_FILE_CAP
    );
    assert!(durable
        .apply_and_commit(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: 2 * DAY_MS + 1,
            available_at_ms: 2 * DAY_MS + 1,
            rows: vec![ticker_wire("BTCUSDT", 101.0)],
        })
        .unwrap()
        .is_empty());
    assert_eq!(durable.worker.state.last_input_sequence, 2);
    assert_eq!(
        durable.spool_classes["current"].files,
        CURRENT_SPOOL_FILE_CAP
    );

    let byte_root = temporary_root("projected-class-byte-cap");
    let mut byte_blocked = DurableSignalWorker::open_with_universe(
        test_config(),
        test_universe(),
        byte_root.join("state"),
        byte_root.join("spool"),
    )
    .unwrap();
    byte_blocked.spool_classes.insert(
        "current".into(),
        SpoolClassInventory {
            files: 0,
            bytes: CURRENT_SPOOL_BYTE_SOFT_THRESHOLD.saturating_sub(1),
            oldest_path: None,
            newest_path: None,
        },
    );
    let allowed = byte_blocked
        .apply_and_commit(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 2 * DAY_MS,
            available_at_ms: 2 * DAY_MS,
            rows: vec![ticker_wire("BTCUSDT", 100.0)],
        })
        .unwrap();
    assert_eq!(allowed.len(), 1);
    assert!(byte_blocked.spool_classes["current"].bytes <= CURRENT_SPOOL_BYTE_CAP);
    byte_blocked.pending_replaceable_paths.clear();
    assert!(byte_blocked
        .apply_and_commit(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: 2 * DAY_MS + 1,
            available_at_ms: 2 * DAY_MS + 1,
            rows: vec![ticker_wire("BTCUSDT", 101.0)],
        })
        .unwrap()
        .is_empty());
    assert_eq!(byte_blocked.worker.state.last_input_sequence, 1);
    std::fs::remove_dir_all(root).unwrap();
    std::fs::remove_dir_all(byte_root).unwrap();
}

#[test]
fn small_lifecycle_and_catchup_items_reach_real_thresholds_not_file_worst_cases() {
    let lifecycle_root = temporary_root("small-lifecycle-spool-items");
    let mut lifecycle = DurableSignalWorker::open_with_universe(
        test_config(),
        test_universe(),
        lifecycle_root.join("state"),
        lifecycle_root.join("spool"),
    )
    .unwrap();
    lifecycle.worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
    lifecycle.spool_classes.insert(
        "lifecycle".into(),
        SpoolClassInventory {
            files: 100,
            bytes: LIFECYCLE_SPOOL_BYTE_SOFT_THRESHOLD - 1,
            oldest_path: None,
            newest_path: None,
        },
    );
    lifecycle.spool_files = 100;
    lifecycle.spool_bytes = LIFECYCLE_SPOOL_BYTE_SOFT_THRESHOLD - 1;
    let rows = lifecycle
        .apply_and_commit(WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            symbol: "BTCUSDT".into(),
            available_at_ms: 3 * DAY_MS,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            emit_lifecycle: true,
            rows: vec![BybitFundingWire {
                funding_rate_timestamp: Value::from(3 * DAY_MS),
                funding_rate: Value::from("-0.001"),
                funding_interval_hour: Some(Value::from(1)),
            }],
        })
        .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(lifecycle.spool_classes["lifecycle"].files, 101);
    assert!(lifecycle.spool_classes["lifecycle"].bytes <= LIFECYCLE_SPOOL_BYTE_CAP);

    let catchup_root = temporary_root("small-catchup-spool-items");
    let config = compact_feature_config();
    let mut catchup = DurableSignalWorker::open_with_universe(
        config,
        test_universe(),
        catchup_root.join("state"),
        catchup_root.join("spool"),
    )
    .unwrap();
    install_compact_history(&mut catchup.worker, 16);
    catchup.worker.state.last_carry_decision_ts_ms = Some(8 * DAY_MS);
    catchup.worker.state.last_carry_scorer_ts_ms = Some(8 * DAY_MS);
    catchup.spool_classes.insert(
        "catchup".into(),
        SpoolClassInventory {
            files: 100,
            bytes: CATCHUP_SPOOL_BYTE_SOFT_THRESHOLD - 1,
            oldest_path: None,
            newest_path: None,
        },
    );
    catchup.spool_files = 100;
    catchup.spool_bytes = CATCHUP_SPOOL_BYTE_SOFT_THRESHOLD - 1;
    let rows = catchup
        .apply_and_commit(WireEvent::CarryScorerCatchupWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 16 * DAY_MS,
            decision_through_ms: 15 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    assert_eq!(rows.len(), MAX_CARRY_SCORER_CATCHUP_DAYS as usize);
    assert_eq!(
        catchup.spool_classes["catchup"].files,
        100 + MAX_CARRY_SCORER_CATCHUP_DAYS as u64
    );
    assert!(catchup.spool_classes["catchup"].bytes <= CATCHUP_SPOOL_BYTE_CAP);
    assert_eq!(
        catchup.durability_metrics().unwrap().spool_class_byte_caps["catchup"],
        CATCHUP_SPOOL_BYTE_CAP
    );
    std::fs::remove_dir_all(lifecycle_root).unwrap();
    std::fs::remove_dir_all(catchup_root).unwrap();
}

#[test]
fn pending_replaceable_watermarks_do_not_reserve_files_they_cannot_emit() {
    let root = temporary_root("suppressed-watermark-preflight");
    let state_dir = root.join("state");
    let mut durable = DurableSignalWorker::open_with_universe(
        compact_feature_config(),
        test_universe(),
        &state_dir,
        root.join("spool"),
    )
    .unwrap();
    install_compact_history(&mut durable.worker, 10);
    durable.worker.state.last_carry_decision_ts_ms = Some(10 * DAY_MS);
    durable.worker.state.last_carry_scorer_ts_ms = Some(10 * DAY_MS);
    let sentinel = state_dir.join("checkpoint.json");
    for kind in [
        "market_snapshot",
        "readiness",
        "long_feature_batch",
        "carry_feature_batch",
    ] {
        durable
            .pending_replaceable_paths
            .insert(kind.to_owned(), sentinel.clone());
    }
    durable.spool_classes.insert(
        "current".into(),
        SpoolClassInventory {
            files: CURRENT_SPOOL_FILE_CAP,
            bytes: CURRENT_SPOOL_BYTE_SOFT_THRESHOLD,
            oldest_path: None,
            newest_path: None,
        },
    );
    durable.spool_files = CURRENT_SPOOL_FILE_CAP;
    durable.spool_bytes = CURRENT_SPOOL_BYTE_SOFT_THRESHOLD;

    assert!(durable
        .apply_and_commit(WireEvent::LongWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 10 * DAY_MS,
            data_through_ms: 10 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap()
        .is_empty());
    assert_eq!(durable.worker.state.last_input_sequence, 1);
    assert!(durable
        .apply_and_commit(WireEvent::Watermark {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: 10 * DAY_MS + 1,
        })
        .unwrap()
        .is_empty());
    assert_eq!(durable.worker.state.last_input_sequence, 2);
    assert_eq!(
        durable.spool_classes["current"].files,
        CURRENT_SPOOL_FILE_CAP
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn duplicate_history_keeps_first_availability_and_rejects_rewrite() {
    let mut rows = BTreeMap::new();
    let row = BinanceWhaleObservation {
        symbol: "BTCUSDT".into(),
        day_end_ms: 86_400_000,
        available_at_ms: 86_400_100,
        long_short_ratio: Some(1.0),
    };
    merge_row(&mut rows, row.clone()).unwrap();
    let mut later = row.clone();
    later.available_at_ms += 100;
    merge_row(&mut rows, later).unwrap();
    assert_eq!(rows[&row.day_end_ms].available_at_ms, row.available_at_ms);
    let mut conflict = row;
    conflict.long_short_ratio = Some(2.0);
    assert!(merge_row(&mut rows, conflict).is_err());
}

#[test]
fn feature_marks_keep_the_ticker_clock_and_expire() {
    let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
    worker.state.tickers.insert(
        "BTCUSDT".into(),
        TickerObservation {
            symbol: "BTCUSDT".into(),
            observed_ts_ms: 2 * DAY_MS,
            available_at_ms: 2 * DAY_MS + 10,
            mark_observed_ts_ms: Some(2 * DAY_MS),
            funding_observed_ts_ms: None,
            schedule_observed_ts_ms: None,
            last_price: Some(100.0),
            mark_price: Some(101.0),
            index_price: Some(100.5),
            bid1_price: Some(100.0),
            ask1_price: Some(102.0),
            bid1_size: Some(1.0),
            ask1_size: Some(1.0),
            open_interest: None,
            open_interest_value: None,
            turnover_24h: None,
            volume_24h: None,
            funding_rate: None,
            next_funding_time_ms: None,
        },
    );
    let symbols = vec!["BTCUSDT".to_owned()];
    let marks = worker.current_marks(&symbols, 2 * DAY_MS + 30_000);
    assert_eq!(marks.len(), 1);
    assert_eq!(marks[0].observed_ts_ms, 2 * DAY_MS);
    assert!(worker
        .current_marks(&symbols, 2 * DAY_MS + 30_001)
        .is_empty());
}

#[test]
fn presettlement_expiry_uses_the_older_schedule_clock() {
    let worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
    let schedule_clock = 10 * DAY_MS;
    let observed_ts_ms = schedule_clock + worker.config.sources.mark_max_age_ms - 1;
    let row = TickerObservation {
        symbol: "BTCUSDT".into(),
        observed_ts_ms,
        available_at_ms: observed_ts_ms,
        mark_observed_ts_ms: Some(observed_ts_ms),
        funding_observed_ts_ms: Some(observed_ts_ms),
        schedule_observed_ts_ms: Some(schedule_clock),
        last_price: Some(100.0),
        mark_price: Some(100.0),
        index_price: Some(100.0),
        bid1_price: Some(99.0),
        ask1_price: Some(101.0),
        bid1_size: Some(1.0),
        ask1_size: Some(1.0),
        open_interest: None,
        open_interest_value: None,
        turnover_24h: None,
        volume_24h: None,
        funding_rate: Some(-0.001),
        next_funding_time_ms: Some(observed_ts_ms + 1),
    };

    let (_, presettlement) = worker.public_market_rows(&[row], observed_ts_ms);

    assert_eq!(presettlement.len(), 1);
    assert_eq!(presettlement[0].observed_ts_ms, schedule_clock);
    assert_eq!(
        presettlement[0]
            .observed_ts_ms
            .saturating_add(worker.config.sources.mark_max_age_ms),
        schedule_clock + worker.config.sources.mark_max_age_ms
    );
    assert!(
        presettlement[0]
            .observed_ts_ms
            .saturating_add(worker.config.sources.mark_max_age_ms)
            < observed_ts_ms.saturating_add(worker.config.sources.mark_max_age_ms)
    );
}

#[test]
fn late_rest_ticker_cannot_roll_back_ws_state_or_output_clock() {
    let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
    let mut ws = ticker_wire("BTCUSDT", 110.0);
    ws.mark_observed_ts_ms = Some(3_000);
    let first = worker
        .apply(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 3_500,
            available_at_ms: 3_500,
            rows: vec![ws],
        })
        .unwrap();
    assert_eq!(first.len(), 1);

    let second = worker
        .apply(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: 2_000,
            available_at_ms: 4_000,
            rows: vec![ticker_wire("BTCUSDT", 90.0)],
        })
        .unwrap();
    assert_eq!(worker.state.tickers["BTCUSDT"].mark_price, Some(110.0));
    assert_eq!(worker.state.tickers["BTCUSDT"].observed_ts_ms, 3_500);
    assert_eq!(second.len(), 1);
    assert_eq!(second[0].observed_wall_ts_ms, 3_500);
    let envelope: SignalPayloadEnvelope = serde_json::from_slice(&second[0].payload).unwrap();
    let ObservationPayload::MarketSnapshot { tickers, marks, .. } = envelope.payload else {
        panic!("expected market snapshot");
    };
    assert_eq!(tickers[0].mark_price, Some(110.0));
    assert_eq!(tickers[0].observed_ts_ms, 3_500);
    assert_eq!(marks[0].mark_px, 110.0);
}

#[test]
fn ticker_snapshot_already_expired_at_delivery_is_not_sequenced() {
    let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
    let observed_ts_ms = 2 * DAY_MS;
    let output = worker
        .apply(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms,
            available_at_ms: observed_ts_ms + 30_001,
            rows: vec![ticker_wire("BTCUSDT", 100.0)],
        })
        .unwrap();
    assert!(output.is_empty());
    assert_eq!(worker.state.carry_output_sequence, 0);
    assert_eq!(worker.state.tickers["BTCUSDT"].mark_price, Some(100.0));
}

#[test]
fn duplicate_funding_is_stored_once_and_not_reemitted() {
    let config = test_config();
    let universe = test_universe();
    let mut worker = SignalWorker::with_universe(config, universe).unwrap();
    worker.state.last_carry_decision_ts_ms = Some(DAY_MS - 1);
    let wire = BybitFundingWire {
        funding_rate_timestamp: serde_json::Value::from(DAY_MS),
        funding_rate: serde_json::Value::from("-0.001"),
        funding_interval_hour: Some(serde_json::Value::from(8)),
    };
    let first = worker
        .apply(WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            symbol: "BTCUSDT".into(),
            available_at_ms: 2 * DAY_MS,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            emit_lifecycle: true,
            rows: vec![wire.clone()],
        })
        .unwrap();
    assert_eq!(first.len(), 1);
    let payload: SignalPayloadEnvelope = serde_json::from_slice(&first[0].payload).unwrap();
    let ObservationPayload::FundingUpdate {
        decision_ts_ms,
        settled_funding,
    } = payload.payload
    else {
        panic!("expected funding update");
    };
    assert_eq!(decision_ts_ms, DAY_MS - 1);
    assert_eq!(settled_funding.len(), 1);
    let duplicate = worker
        .apply(WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            symbol: "BTCUSDT".into(),
            available_at_ms: 2 * DAY_MS + 1,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            emit_lifecycle: true,
            rows: vec![wire],
        })
        .unwrap();
    assert!(duplicate.is_empty());
    assert_eq!(worker.state.funding["BTCUSDT"].len(), 1);
    assert_eq!(
        worker.state.funding["BTCUSDT"][&DAY_MS].available_at_ms,
        2 * DAY_MS
    );
}

#[test]
fn funding_lifecycle_emits_only_rows_after_the_bound_decision() {
    let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
    worker.state.last_carry_decision_ts_ms = Some(2 * DAY_MS);
    let observations = worker
        .apply(WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            symbol: "BTCUSDT".into(),
            available_at_ms: 4 * DAY_MS,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            emit_lifecycle: true,
            rows: vec![
                BybitFundingWire {
                    funding_rate_timestamp: Value::from(DAY_MS),
                    funding_rate: Value::from("-0.001"),
                    funding_interval_hour: Some(Value::from(8)),
                },
                BybitFundingWire {
                    funding_rate_timestamp: Value::from(3 * DAY_MS),
                    funding_rate: Value::from("-0.002"),
                    funding_interval_hour: Some(Value::from(8)),
                },
            ],
        })
        .unwrap();
    assert_eq!(observations.len(), 1);
    assert_eq!(observations[0].kind, "funding_update");
    assert_eq!(observations[0].observed_wall_ts_ms, 3 * DAY_MS);
    let payload: SignalPayloadEnvelope = serde_json::from_slice(&observations[0].payload).unwrap();
    let ObservationPayload::FundingUpdate {
        decision_ts_ms,
        settled_funding,
    } = payload.payload
    else {
        panic!("expected funding update");
    };
    assert_eq!(decision_ts_ms, 2 * DAY_MS);
    assert_eq!(settled_funding.len(), 1);
    assert_eq!(settled_funding[0].settlement_ts_ms, 3 * DAY_MS);
    assert_eq!(worker.state.funding["BTCUSDT"].len(), 2);
}

#[test]
fn carry_scorer_catchup_is_bounded_ordered_and_has_no_market_payload() {
    let mut config = test_config();
    config.carry.minimum_decision_symbols = 1;
    let mut worker = SignalWorker::with_universe(config, test_universe()).unwrap();
    install_trading_instrument(&mut worker, "BTCUSDT");
    worker.state.last_carry_decision_ts_ms = Some(40 * DAY_MS);
    let history = worker.state.klines.entry("BTCUSDT".into()).or_default();
    for open_ts_ms in (5 * DAY_MS..50 * DAY_MS).step_by(HOUR_MS as usize) {
        history.insert(
            open_ts_ms,
            HourlyKline {
                symbol: "BTCUSDT".into(),
                open_ts_ms,
                available_at_ms: 50 * DAY_MS,
                open: 100.0,
                high: 101.0,
                low: 99.0,
                close: 100.0 + open_ts_ms as f64 / DAY_MS as f64,
                volume_base: 1.0,
                turnover_quote: 100.0,
            },
        );
    }
    let funding = worker.state.funding.entry("BTCUSDT".into()).or_default();
    for settlement_ts_ms in (30 * DAY_MS..=42 * DAY_MS).step_by((8 * HOUR_MS) as usize) {
        funding.insert(
            settlement_ts_ms,
            SettledFunding {
                symbol: "BTCUSDT".into(),
                settlement_ts_ms,
                available_at_ms: settlement_ts_ms,
                rate: -0.001,
                funding_interval_min: 480,
            },
        );
    }

    let observations = worker
        .apply(WireEvent::CarryScorerCatchupWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 50 * DAY_MS,
            decision_through_ms: 42 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    assert_eq!(observations.len(), 2);
    assert_eq!(worker.state.last_carry_decision_ts_ms, Some(40 * DAY_MS));
    assert_eq!(worker.state.last_carry_scorer_ts_ms, Some(42 * DAY_MS));
    for (index, observation) in observations.iter().enumerate() {
        assert_eq!(observation.kind, "carry_scorer_catchup");
        let envelope: SignalPayloadEnvelope = serde_json::from_slice(&observation.payload).unwrap();
        let ObservationPayload::CarryScorerCatchup {
            decision_ts_ms,
            rows,
            rejections,
        } = envelope.payload
        else {
            panic!("expected scorer catch-up payload");
        };
        assert_eq!(decision_ts_ms, (41 + index as i64) * DAY_MS);
        assert_eq!(rows.len(), 1);
        assert!(rejections.is_empty());
    }
}

#[test]
fn optional_whale_absence_keeps_carry_live_with_a_null_feature() {
    let mut worker =
        SignalWorker::with_universe(compact_feature_config(), test_universe()).unwrap();
    install_compact_history(&mut worker, 10);
    assert!(worker.state.whales.is_empty());
    let observations = worker
        .apply(WireEvent::CarryWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 10 * DAY_MS,
            data_through_ms: 10 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    let carry = observations
        .iter()
        .find(|observation| observation.kind == "carry_feature_batch")
        .expect("optional whale absence must not suppress the CARRY decision");
    let envelope: SignalPayloadEnvelope = serde_json::from_slice(&carry.payload).unwrap();
    let ObservationPayload::CarryFeatureBatch { rows, .. } = envelope.payload else {
        panic!("expected CARRY feature payload");
    };
    assert!(!rows.is_empty());
    assert!(rows.iter().all(|row| row.d_tt_ls_3d.is_none()));
    assert_eq!(worker.state.last_carry_decision_ts_ms, Some(10 * DAY_MS));
}

#[test]
fn carry_catchup_uses_instrument_status_at_each_historical_decision() {
    let config = compact_feature_config();
    let mut universe = test_universe();
    universe.symbols = vec!["BTCUSDT".into(), "ETHUSDT".into()];
    universe.carry_symbols = universe.symbols.clone();
    let mut worker = SignalWorker::with_universe(config, universe).unwrap();
    worker
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 2 * DAY_MS,
            available_at_ms: 2 * DAY_MS,
            rows: vec![
                trading_instrument_wire("BTCUSDT", DAY_MS),
                trading_instrument_wire("ETHUSDT", DAY_MS),
            ],
        })
        .unwrap();
    worker
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: 10 * DAY_MS,
            available_at_ms: 10 * DAY_MS,
            rows: vec![
                closed_instrument_wire("BTCUSDT", DAY_MS, 10 * DAY_MS),
                trading_instrument_wire("ETHUSDT", DAY_MS),
            ],
        })
        .unwrap();
    assert_eq!(
        worker.state.instrument_trading_intervals["BTCUSDT"][0].trading_through_ms,
        Some(10 * DAY_MS)
    );
    worker
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 3,
            observed_ts_ms: 11 * DAY_MS,
            available_at_ms: 11 * DAY_MS,
            rows: vec![
                closed_instrument_wire("BTCUSDT", DAY_MS, 10 * DAY_MS),
                trading_instrument_wire("ETHUSDT", DAY_MS),
            ],
        })
        .unwrap();
    assert_eq!(
        worker.state.instruments["BTCUSDT"].status.as_deref(),
        Some("Closed")
    );
    assert!(!worker
        .state
        .instrument_status_unknown_since_ms
        .contains_key("BTCUSDT"));
    assert_eq!(
        worker.state.instrument_trading_intervals["BTCUSDT"],
        vec![InstrumentTradingInterval {
            trading_from_ms: DAY_MS,
            trading_through_ms: Some(10 * DAY_MS),
        }]
    );
    install_compact_history(&mut worker, 13);
    clone_symbol_history(&mut worker, "BTCUSDT", "ETHUSDT");
    worker.state.instruments.remove("BTCUSDT");
    worker.state.instrument_trading_intervals.insert(
        "BTCUSDT".into(),
        vec![InstrumentTradingInterval {
            trading_from_ms: DAY_MS,
            trading_through_ms: Some(10 * DAY_MS),
        }],
    );
    worker.state.instrument_trading_intervals.insert(
        "ETHUSDT".into(),
        vec![InstrumentTradingInterval {
            trading_from_ms: DAY_MS,
            trading_through_ms: None,
        }],
    );
    worker.state.last_carry_decision_ts_ms = Some(7 * DAY_MS);
    worker.state.last_carry_scorer_ts_ms = Some(7 * DAY_MS);

    let observations = worker
        .apply(WireEvent::CarryScorerCatchupWatermark {
            schema_version: SCHEMA_VERSION,
            sequence: 4,
            observed_ts_ms: 12 * DAY_MS,
            decision_through_ms: 11 * DAY_MS,
            gap_symbols: Vec::new(),
        })
        .unwrap();
    assert_eq!(observations.len(), 4);
    for (index, observation) in observations.iter().enumerate() {
        let envelope: SignalPayloadEnvelope = serde_json::from_slice(&observation.payload).unwrap();
        let ObservationPayload::CarryScorerCatchup {
            decision_ts_ms,
            rows,
            rejections,
        } = envelope.payload
        else {
            panic!("expected scorer catch-up payload");
        };
        let day = 8 + index as i64;
        assert_eq!(decision_ts_ms, day * DAY_MS);
        if day < 10 {
            assert_eq!(rows.len(), 2);
            assert!(rejections.is_empty());
        } else {
            assert_eq!(rows.len(), 1);
            assert_eq!(rows[0].symbol, "ETHUSDT");
            assert_eq!(rejections.len(), 1);
            assert_eq!(rejections[0].symbol, "BTCUSDT");
            assert_eq!(rejections[0].reason, "instrument_not_trading");
        }
        assert!(rows.iter().all(|row| row.d_tt_ls_3d.is_none()));
    }
    assert_eq!(worker.state.last_carry_decision_ts_ms, Some(7 * DAY_MS));
    assert_eq!(worker.state.last_carry_scorer_ts_ms, Some(11 * DAY_MS));
}

#[test]
fn missing_then_recovered_instrument_preserves_the_unknown_historical_gap() {
    let config = compact_feature_config();
    let universe = test_universe();
    let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
    worker
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 2 * DAY_MS,
            available_at_ms: 2 * DAY_MS,
            rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
        })
        .unwrap();
    worker
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: 5 * DAY_MS,
            available_at_ms: 5 * DAY_MS,
            rows: Vec::new(),
        })
        .unwrap();
    let interval = &worker.state.instrument_trading_intervals["BTCUSDT"][0];
    assert_eq!(interval.trading_from_ms, DAY_MS);
    assert_eq!(interval.trading_through_ms, Some(5 * DAY_MS));
    assert_eq!(
        worker.state.instrument_status_unknown_since_ms["BTCUSDT"],
        5 * DAY_MS
    );
    assert!(worker.was_trading_instrument_at("BTCUSDT", 4 * DAY_MS));
    assert!(!worker.was_trading_instrument_at("BTCUSDT", 5 * DAY_MS));
    assert!(!worker.is_trading_instrument("BTCUSDT"));

    let mut legacy_state = worker.state.clone();
    legacy_state
        .instrument_trading_intervals
        .get_mut("BTCUSDT")
        .unwrap()[0]
        .trading_through_ms = None;
    let restored_legacy = SignalWorker::restore(config.clone(), legacy_state).unwrap();
    assert_eq!(
        restored_legacy.state.instrument_trading_intervals["BTCUSDT"][0].trading_through_ms,
        Some(5 * DAY_MS)
    );

    worker
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 3,
            observed_ts_ms: 7 * DAY_MS,
            available_at_ms: 7 * DAY_MS,
            rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
        })
        .unwrap();
    assert!(!worker
        .state
        .instrument_status_unknown_since_ms
        .contains_key("BTCUSDT"));
    assert_eq!(
        worker.state.instrument_trading_intervals["BTCUSDT"],
        vec![
            InstrumentTradingInterval {
                trading_from_ms: DAY_MS,
                trading_through_ms: Some(5 * DAY_MS),
            },
            InstrumentTradingInterval {
                trading_from_ms: 7 * DAY_MS,
                trading_through_ms: None,
            },
        ]
    );
    assert!(!worker.was_trading_instrument_at("BTCUSDT", 6 * DAY_MS));
    assert!(worker.was_trading_instrument_at("BTCUSDT", 7 * DAY_MS));
    assert!(worker.is_trading_instrument("BTCUSDT"));

    let restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
    assert!(!restored.was_trading_instrument_at("BTCUSDT", 6 * DAY_MS));
    assert!(restored.was_trading_instrument_at("BTCUSDT", 7 * DAY_MS));
}

#[test]
fn repeated_instrument_omission_recovery_survives_past_the_old_cap_and_restart() {
    let config = compact_feature_config();
    let universe = test_universe();
    let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
    let base = 10 * DAY_MS;
    worker
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: base,
            available_at_ms: base,
            rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
        })
        .unwrap();

    let mut sequence = 2;
    for cycle in 0..40 {
        let missing_at_ms = base + (2 * cycle + 1) * HOUR_MS;
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence,
                observed_ts_ms: missing_at_ms,
                available_at_ms: missing_at_ms,
                rows: Vec::new(),
            })
            .unwrap();
        sequence += 1;
        let recovered_at_ms = missing_at_ms + HOUR_MS;
        worker
            .apply(WireEvent::BybitInstrumentSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence,
                observed_ts_ms: recovered_at_ms,
                available_at_ms: recovered_at_ms,
                rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
            })
            .unwrap();
        sequence += 1;
        assert!(!worker.was_trading_instrument_at("BTCUSDT", missing_at_ms));
        assert!(worker.was_trading_instrument_at("BTCUSDT", recovered_at_ms));
    }

    let expected = worker.state.instrument_trading_intervals["BTCUSDT"].clone();
    assert_eq!(expected.len(), 41);
    assert_eq!(expected[0].trading_through_ms, Some(base + HOUR_MS));
    assert_eq!(expected[40].trading_from_ms, base + 80 * HOUR_MS);
    assert_eq!(expected[40].trading_through_ms, None);

    let mut restored = SignalWorker::restore(config, worker.state.clone()).unwrap();
    assert_eq!(
        restored.state.instrument_trading_intervals["BTCUSDT"],
        expected
    );
    let missing_at_ms = base + 81 * HOUR_MS;
    restored
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence,
            observed_ts_ms: missing_at_ms,
            available_at_ms: missing_at_ms,
            rows: Vec::new(),
        })
        .unwrap();
    sequence += 1;
    let recovered_at_ms = missing_at_ms + HOUR_MS;
    restored
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence,
            observed_ts_ms: recovered_at_ms,
            available_at_ms: recovered_at_ms,
            rows: vec![trading_instrument_wire("BTCUSDT", DAY_MS)],
        })
        .unwrap();
    assert_eq!(
        restored.state.instrument_trading_intervals["BTCUSDT"].len(),
        42
    );
    assert!(!restored.was_trading_instrument_at("BTCUSDT", missing_at_ms));
    assert!(restored.was_trading_instrument_at("BTCUSDT", recovered_at_ms));
}

#[test]
fn restore_preserves_history_across_operational_changes_and_resets_only_changed_physics() {
    let config = test_config();
    let universe = test_universe();
    let mut state = SignalWorker::with_universe(config.clone(), universe.clone())
        .unwrap()
        .state
        .clone();
    state.last_input_sequence = 17;
    state.long_output_sequence = 3;
    state.carry_output_sequence = 5;
    state.last_long_feature_ts_ms = Some(9 * DAY_MS);
    state.last_carry_decision_ts_ms = Some(9 * DAY_MS);
    state.last_carry_upcoming_ts_ms = Some(10 * DAY_MS);

    let mut operational = config.clone();
    operational.identity.operational_profile_sha256 = "3".repeat(64);
    operational.identity.engine_config_sha256 = "4".repeat(64);
    let restored = SignalWorker::restore(operational, state.clone()).unwrap();
    assert_eq!(restored.state.last_input_sequence, 17);
    assert_eq!(restored.state.long_output_sequence, 3);
    assert_eq!(restored.state.carry_output_sequence, 5);
    assert_eq!(restored.state.last_long_feature_ts_ms, Some(9 * DAY_MS));
    assert_eq!(restored.state.last_carry_decision_ts_ms, Some(9 * DAY_MS));
    assert_eq!(restored.state.last_carry_scorer_ts_ms, Some(9 * DAY_MS));
    assert_eq!(restored.state.last_carry_upcoming_ts_ms, Some(10 * DAY_MS));

    let mut mark_physics = config.clone();
    mark_physics.sources.mark_max_age_ms += 1;
    mark_physics.identity.long_decision_fingerprint = "1".repeat(64);
    mark_physics.identity.carry_decision_fingerprint = "2".repeat(64);
    let restored = SignalWorker::restore(mark_physics, state.clone()).unwrap();
    assert_eq!(restored.state.last_input_sequence, 17);
    assert_eq!(restored.state.long_output_sequence, 3);
    assert_eq!(restored.state.carry_output_sequence, 5);
    assert_eq!(restored.state.last_long_feature_ts_ms, None);
    assert_eq!(restored.state.last_carry_decision_ts_ms, None);
    assert_eq!(restored.state.last_carry_scorer_ts_ms, None);
    assert_eq!(restored.state.last_carry_upcoming_ts_ms, None);

    let mut long_changed = config.clone();
    long_changed.long.regime_sma_days += 1;
    let restored = SignalWorker::restore(long_changed, state.clone()).unwrap();
    assert_eq!(restored.state.last_long_feature_ts_ms, None);
    assert_eq!(restored.state.last_carry_decision_ts_ms, Some(9 * DAY_MS));
    assert_eq!(restored.state.last_carry_scorer_ts_ms, Some(9 * DAY_MS));
    assert_eq!(restored.state.last_carry_upcoming_ts_ms, Some(10 * DAY_MS));

    let mut carry_changed = config;
    carry_changed.identity.carry_decision_fingerprint = "0".repeat(64);
    let restored = SignalWorker::restore(carry_changed, state.clone()).unwrap();
    assert_eq!(restored.state.last_long_feature_ts_ms, Some(9 * DAY_MS));
    assert_eq!(restored.state.last_carry_decision_ts_ms, None);
    assert_eq!(restored.state.last_carry_scorer_ts_ms, None);
    assert_eq!(restored.state.last_carry_upcoming_ts_ms, None);
    assert_eq!(restored.state.last_input_sequence, 17);

    let mut source_changed = test_config();
    source_changed.routing.source = "different_source".into();
    let error = match SignalWorker::restore(source_changed, state) {
        Ok(_) => panic!("a new source cannot inherit another source's sequence"),
        Err(error) => error,
    };
    assert!(error.to_string().contains("source contract has drifted"));
}

#[test]
fn pending_multi_output_transaction_recovers_every_crash_boundary() {
    let config = test_config();
    let universe = test_universe();
    let initial = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
    let prior_json = serde_json::to_vec(initial.state()).unwrap();
    let mut next_state = initial.state().clone();
    next_state.last_input_sequence = 1;
    next_state.long_output_sequence = 1;
    next_state.carry_output_sequence = 1;
    next_state.last_observed_ts_ms = 2 * DAY_MS;
    let next_json = serde_json::to_vec(&next_state).unwrap();
    let observations = [
        make_observation(
            &config,
            &universe,
            &initial.state.source_generation,
            true,
            1,
            "long_feature_batch",
            2 * DAY_MS,
            2 * DAY_MS + 1,
            empty_readiness(),
            Vec::new(),
        )
        .unwrap(),
        make_observation(
            &config,
            &universe,
            &initial.state.source_generation,
            false,
            1,
            "carry_feature_batch",
            2 * DAY_MS,
            2 * DAY_MS + 1,
            empty_readiness(),
            Vec::new(),
        )
        .unwrap(),
    ];
    let observation_json = observations
        .iter()
        .map(|row| serde_json::to_string(row).unwrap())
        .collect::<Vec<_>>();
    let transaction = PendingTransaction {
        schema_version: SCHEMA_VERSION,
        prior_state_sha256: sha256_hex(&prior_json),
        next_state_sha256: sha256_hex(&next_json),
        observation_json: observation_json.clone(),
    };

    for phase in 0..=5 {
        let root = temporary_root(&format!("crash-{phase}"));
        let state_dir = root.join("state");
        let spool_dir = root.join("spool");
        let checkpoint = AtomicJsonStore::new(state_dir.join("checkpoint.json"));
        let pending = AtomicJsonStore::new(state_dir.join("pending-transaction.json"));
        let pending_next = AtomicJsonStore::new(state_dir.join("pending-next-state.json"));
        let spool = SpoolWriter::new(&spool_dir).unwrap();
        checkpoint.save_bytes(&prior_json).unwrap();
        if phase > 0 {
            pending_next.save_bytes(&next_json).unwrap();
        }
        if phase > 1 {
            pending.save(&transaction).unwrap();
        }
        if phase > 2 {
            spool.write_encoded(observation_json[0].as_bytes()).unwrap();
        }
        if phase > 3 {
            spool.write_encoded(observation_json[1].as_bytes()).unwrap();
        }
        if phase > 4 {
            checkpoint.replace_from(&pending_next).unwrap();
        }

        let durable = DurableSignalWorker::open_with_universe(
            config.clone(),
            universe.clone(),
            &state_dir,
            &spool_dir,
        )
        .unwrap();
        if phase <= 1 {
            assert_eq!(durable.worker.state.last_input_sequence, 0);
            assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 0);
            assert!(!pending_next.path().exists());
        } else {
            assert_eq!(durable.worker.state.last_input_sequence, 1);
            assert_eq!(durable.worker.state.long_output_sequence, 1);
            assert_eq!(durable.worker.state.carry_output_sequence, 1);
            assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 2);
            assert!(!pending.path().exists());
            assert_eq!(checkpoint.load_bytes().unwrap().unwrap(), next_json);
            drop(durable);
            let reopened = DurableSignalWorker::open_with_universe(
                config.clone(),
                universe.clone(),
                &state_dir,
                &spool_dir,
            )
            .unwrap();
            assert_eq!(reopened.worker.state.last_input_sequence, 1);
            assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 2);
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn five_second_samples_journal_without_rewriting_the_full_checkpoint() {
    let config = test_config();
    let universe = test_universe();
    let root = temporary_root("hot-journal");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let checkpoint = AtomicJsonStore::new(state_dir.join("checkpoint.json"));

    let mut durable = DurableSignalWorker::open_with_universe(
        config.clone(),
        universe.clone(),
        &state_dir,
        &spool_dir,
    )
    .unwrap();
    let initial = durable.durability_metrics().unwrap();
    let checkpoint_hash = checkpoint.sha256().unwrap().unwrap();
    for sequence in 1..=12_u64 {
        let observed_ts_ms = 2 * DAY_MS + i64::try_from(sequence).unwrap() * 5_000;
        durable
            .apply_and_commit(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence,
                observed_ts_ms,
                available_at_ms: observed_ts_ms + 1,
                rows: vec![ticker_wire("BTCUSDT", 100.0 + sequence as f64)],
            })
            .unwrap();
    }
    let hot = durable.durability_metrics().unwrap();
    assert_eq!(
        hot.checkpoint_writes_session,
        initial.checkpoint_writes_session
    );
    assert_eq!(checkpoint.sha256().unwrap().unwrap(), checkpoint_hash);
    assert_eq!(hot.journal_entries_retained, 12);
    assert!(hot.journal_bytes > 0);
    assert_eq!(hot.spool_files, 1);
    assert_eq!(hot.replaceable_outputs_coalesced, 11);
    assert_eq!(
        checkpoint
            .load::<WorkerState>()
            .unwrap()
            .unwrap()
            .last_input_sequence,
        0
    );
    drop(durable);

    let restored =
        DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir).unwrap();
    assert_eq!(restored.worker.state.last_input_sequence, 12);
    assert_eq!(restored.durability_metrics().unwrap().journal_bytes, 0);
    assert_eq!(std::fs::read_dir(&spool_dir).unwrap().count(), 1);
    assert_eq!(
        restored
            .durability_metrics()
            .unwrap()
            .replaceable_outputs_coalesced,
        0,
        "session metrics reset after recovery"
    );
    assert_eq!(
        checkpoint
            .load::<WorkerState>()
            .unwrap()
            .unwrap()
            .last_input_sequence,
        12
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn ticker_field_clock_survives_journal_replay_without_extending_ttl() {
    let config = test_config();
    let universe = test_universe();
    let root = temporary_root("ticker-field-clock");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let field_ts = 2 * DAY_MS;
    let mut wire = ticker_wire("BTCUSDT", 100.0);
    wire.mark_observed_ts_ms = Some(field_ts);
    {
        let mut durable = DurableSignalWorker::open_with_universe(
            config.clone(),
            universe.clone(),
            &state_dir,
            &spool_dir,
        )
        .unwrap();
        durable
            .apply_and_commit(WireEvent::BybitTickerSnapshot {
                schema_version: SCHEMA_VERSION,
                sequence: 1,
                observed_ts_ms: field_ts + 20_000,
                available_at_ms: field_ts + 20_001,
                rows: vec![wire],
            })
            .unwrap();
    }
    let restored =
        DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir).unwrap();
    let symbols = vec!["BTCUSDT".to_owned()];
    assert_eq!(
        restored
            .worker()
            .current_marks(&symbols, field_ts + 30_000)
            .len(),
        1
    );
    assert!(restored
        .worker()
        .current_marks(&symbols, field_ts + 30_001)
        .is_empty());
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn bounded_source_deltas_compact_into_one_checkpoint() {
    let config = test_config();
    let universe = test_universe();
    let root = temporary_root("journal-compaction");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let mut durable =
        DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir).unwrap();
    let initial_writes = durable
        .durability_metrics()
        .unwrap()
        .checkpoint_writes_session;
    durable
        .apply_and_commit(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 2 * DAY_MS,
            available_at_ms: 2 * DAY_MS + 1,
            rows: vec![ticker_wire("BTCUSDT", 100.0)],
        })
        .unwrap();
    durable
        .apply_and_commit(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: 2 * DAY_MS + 2,
            available_at_ms: 2 * DAY_MS + 3,
            rows: Vec::new(),
        })
        .unwrap();
    let journaled = durable.durability_metrics().unwrap();
    assert_eq!(journaled.checkpoint_writes_session, initial_writes);
    assert_eq!(journaled.journal_entries_retained, 2);
    assert!(journaled.journal_bytes > 0);
    durable.compact_current_checkpoint(&[]).unwrap();
    let compacted = durable.durability_metrics().unwrap();
    assert_eq!(compacted.checkpoint_writes_session, initial_writes + 1);
    assert_eq!(compacted.journal_bytes, 0);
    assert_eq!(
        AtomicJsonStore::new(state_dir.join("checkpoint.json"))
            .load::<WorkerState>()
            .unwrap()
            .unwrap()
            .last_input_sequence,
        2
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn durable_checkpoint_owns_the_output_source_generation() {
    let config = test_config();
    let universe = test_universe();
    let root = temporary_root("source-generation");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");

    let mut first = DurableSignalWorker::open_with_universe(
        config.clone(),
        universe.clone(),
        &state_dir,
        &spool_dir,
    )
    .unwrap();
    let generation = first.worker.state.source_generation.clone();
    let first_rows = first
        .apply_and_commit(WireEvent::Watermark {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 2 * DAY_MS,
        })
        .unwrap();
    assert_eq!(first_rows.len(), 1);
    assert_eq!(first_rows[0].sequence, 1);
    assert!(first_rows[0].source.contains(&generation));
    drop(first);

    let mut restored = DurableSignalWorker::open_with_universe(
        config.clone(),
        universe.clone(),
        &state_dir,
        &spool_dir,
    )
    .unwrap();
    assert_eq!(restored.worker.state.source_generation, generation);
    let restored_rows = restored
        .apply_and_commit(WireEvent::Watermark {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: 2 * DAY_MS + 1,
        })
        .unwrap();
    assert!(restored_rows.is_empty());
    assert_eq!(restored.worker.state.carry_output_sequence, 1);
    drop(restored);

    std::fs::remove_dir_all(&state_dir).unwrap();
    std::fs::remove_dir_all(&spool_dir).unwrap();
    let mut replacement =
        DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir).unwrap();
    let replacement_generation = replacement.worker.state.source_generation.clone();
    assert_ne!(replacement_generation, generation);
    let replacement_rows = replacement
        .apply_and_commit(WireEvent::Watermark {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 2 * DAY_MS,
        })
        .unwrap();
    assert_eq!(replacement_rows.len(), 1);
    assert_eq!(replacement_rows[0].sequence, 1);
    assert!(replacement_rows[0].source.contains(&replacement_generation));
    assert_ne!(replacement_rows[0].source, first_rows[0].source);

    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn checkpoint_without_a_generation_adopts_a_new_output_namespace() {
    let config = test_config();
    let universe = test_universe();
    let root = temporary_root("adopt-source-generation");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let checkpoint = AtomicJsonStore::new(state_dir.join("checkpoint.json"));
    let mut state = SignalWorker::with_universe(config.clone(), universe.clone())
        .unwrap()
        .state
        .clone();
    state.last_input_sequence = 17;
    state.long_output_sequence = 3;
    state.carry_output_sequence = 5;
    state.last_long_feature_ts_ms = Some(9 * DAY_MS);
    state.last_carry_decision_ts_ms = Some(9 * DAY_MS);
    state.last_carry_upcoming_ts_ms = Some(10 * DAY_MS);
    let mut legacy = serde_json::to_value(state).unwrap();
    legacy.as_object_mut().unwrap().remove("source_generation");
    checkpoint.save(&legacy).unwrap();

    let durable =
        DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir).unwrap();
    assert_ne!(
        durable.worker.state.source_generation,
        REPLAY_SOURCE_GENERATION
    );
    assert_eq!(durable.worker.state.last_input_sequence, 17);
    assert_eq!(durable.worker.state.long_output_sequence, 0);
    assert_eq!(durable.worker.state.carry_output_sequence, 0);
    assert_eq!(durable.worker.state.last_long_feature_ts_ms, None);
    assert_eq!(durable.worker.state.last_carry_decision_ts_ms, None);
    assert_eq!(durable.worker.state.last_carry_upcoming_ts_ms, None);
    let persisted: WorkerState = checkpoint.load().unwrap().unwrap();
    assert_eq!(
        persisted.source_generation,
        durable.worker.state.source_generation
    );

    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn signal_payload_accepts_exactly_16_mib_and_refuses_the_next_byte() {
    let config = test_config();
    let universe = test_universe();
    let baseline = make_observation(
        &config,
        &universe,
        REPLAY_SOURCE_GENERATION,
        false,
        1,
        "readiness",
        2 * DAY_MS,
        2 * DAY_MS + 1,
        readiness(String::new()),
        Vec::new(),
    )
    .unwrap();
    let fill = MAX_SIGNAL_OBSERVATION_BYTES - baseline.payload.len();
    let exact = make_observation(
        &config,
        &universe,
        REPLAY_SOURCE_GENERATION,
        false,
        1,
        "readiness",
        2 * DAY_MS,
        2 * DAY_MS + 1,
        readiness("x".repeat(fill)),
        Vec::new(),
    )
    .unwrap();
    assert_eq!(MAX_SIGNAL_OBSERVATION_BYTES, 16 * 1024 * 1024);
    assert_eq!(exact.payload.len(), MAX_SIGNAL_OBSERVATION_BYTES);
    assert!(
        u64::try_from(serde_json::to_vec(&exact).unwrap().len()).unwrap()
            <= MAX_SPOOL_OBSERVATION_FILE_BYTES
    );
    drop(exact);
    let error = make_observation(
        &config,
        &universe,
        REPLAY_SOURCE_GENERATION,
        false,
        1,
        "readiness",
        2 * DAY_MS,
        2 * DAY_MS + 1,
        readiness("x".repeat(fill + 1)),
        Vec::new(),
    )
    .unwrap_err();
    assert!(error.to_string().contains("16777216 bytes"));
}

fn gate_row(symbol: &str, score: f64, trigger_ts_ms: i64) -> crate::model::LlmGateCandidate {
    crate::model::LlmGateCandidate {
        symbol: symbol.into(),
        score,
        band: "core".into(),
        trigger_ts_ms,
        trigger_price: 100.0,
        atr_pct: 0.05,
        sigma_daily_30d: Some(0.04),
        turnover_rank: Some(4.0),
        trigger_window_h: Some(4),
    }
}

#[test]
fn an_unresolved_worker_refuses_every_input_until_a_universe_snapshot_arrives() {
    let mut worker = SignalWorker::new(test_config()).unwrap();
    assert!(!crate::universe::universe_is_resolved(
        &worker.state.universe
    ));
    assert_eq!(worker.state.universe.environment, "demo");
    assert_eq!(worker.state.universe.endpoint, "api-demo.bybit.com");
    let refused = worker.apply(WireEvent::BybitTickerSnapshot {
        schema_version: SCHEMA_VERSION,
        sequence: 1,
        observed_ts_ms: DAY_MS,
        available_at_ms: DAY_MS,
        rows: vec![ticker_wire("BTCUSDT", 100.0)],
    });
    assert!(refused.is_err());
    let resolved = worker
        .apply(WireEvent::UniverseSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            universe: test_universe(),
        })
        .unwrap();
    assert!(resolved.is_empty());
    assert!(crate::universe::universe_is_resolved(
        &worker.state.universe
    ));
    let accepted = worker
        .apply(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: DAY_MS + 2,
            available_at_ms: DAY_MS + 2,
            rows: vec![ticker_wire("BTCUSDT", 100.0)],
        })
        .unwrap();
    assert_eq!(accepted.len(), 1);
}

#[test]
fn a_universe_with_new_membership_replaces_the_old_one_and_drops_its_symbols() {
    let mut first = test_universe();
    first.symbols = vec!["AAAUSDT".into(), "BTCUSDT".into()];
    first.long_symbols = vec!["AAAUSDT".into(), "BTCUSDT".into()];
    first.carry_symbols = vec!["AAAUSDT".into(), "BTCUSDT".into()];
    let mut worker = SignalWorker::with_universe(test_config(), first).unwrap();
    worker
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: DAY_MS + 1,
            available_at_ms: DAY_MS + 1,
            rows: vec![
                trading_instrument_wire("AAAUSDT", 1),
                trading_instrument_wire("BTCUSDT", 1),
                trading_instrument_wire("ETHUSDT", 1),
            ],
        })
        .unwrap();
    worker
        .apply(WireEvent::BybitTickerSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: DAY_MS + 2,
            available_at_ms: DAY_MS + 2,
            rows: vec![ticker_wire("AAAUSDT", 1.0), ticker_wire("BTCUSDT", 100.0)],
        })
        .unwrap();
    assert!(worker.state.tickers.contains_key("AAAUSDT"));
    assert!(worker.state.instruments.contains_key("AAAUSDT"));

    let mut second = test_universe();
    second.snapshot_ts_ms = DAY_MS + 3;
    second.available_at_ms = DAY_MS + 3;
    second.artifact_sha256 = "3".repeat(64);
    let out = worker
        .apply(WireEvent::UniverseSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 3,
            universe: second.clone(),
        })
        .unwrap();
    assert!(out.is_empty());
    assert_eq!(worker.state.universe, second);
    assert!(!worker.state.tickers.contains_key("AAAUSDT"));
    assert!(!worker.state.instruments.contains_key("AAAUSDT"));
    assert!(worker.state.tickers.contains_key("BTCUSDT"));

    // The same membership with a newer clock is installed without pruning
    // work, and a checkpoint from it restores under a config that names no
    // universe at all.
    let mut third = second.clone();
    third.snapshot_ts_ms = DAY_MS + 4;
    third.available_at_ms = DAY_MS + 4;
    worker
        .apply(WireEvent::UniverseSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 4,
            universe: third.clone(),
        })
        .unwrap();
    assert_eq!(worker.state.universe.snapshot_ts_ms, DAY_MS + 4);
    let restored = SignalWorker::restore(test_config(), worker.state.clone()).unwrap();
    assert_eq!(restored.state.universe, third);
}

/// The spool preflight must count the gate's row, or every gate
/// publication is refused as an underestimated batch and the worker exits.
#[test]
fn a_gate_publication_passes_the_spool_preflight() {
    let root = temporary_root("gate-preflight");
    let mut durable = DurableSignalWorker::open_with_universe(
        test_config(),
        test_universe(),
        root.join("state"),
        root.join("spool"),
    )
    .unwrap();
    let read_at_ms = 10 * DAY_MS;
    let decision_ts_ms = read_at_ms - 60_000;
    let out = durable
        .apply_and_commit(WireEvent::LlmGateCandidates {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: decision_ts_ms,
            available_at_ms: read_at_ms,
            decision_ts_ms,
            valid_until_ms: decision_ts_ms + HOUR_MS,
            rows: vec![gate_row("BTCUSDT", 7.0, read_at_ms - 20 * 60_000)],
        })
        .unwrap();
    assert_eq!(out.len(), 1);
    assert_eq!(out[0].kind, "llm_gate_candidates");
    assert_eq!(
        std::fs::read_dir(root.join("spool"))
            .unwrap()
            .filter(|entry| {
                entry
                    .as_ref()
                    .is_ok_and(|entry| entry.path().extension().is_some_and(|e| e == "json"))
            })
            .count(),
        1
    );
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn a_gate_publication_becomes_one_long_observation_over_the_tradable_set() {
    let mut worker = SignalWorker::with_universe(test_config(), test_universe()).unwrap();
    let read_at_ms = 10 * DAY_MS;
    let decision_ts_ms = read_at_ms - 60_000;
    let out = worker
        .apply(WireEvent::LlmGateCandidates {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: decision_ts_ms,
            available_at_ms: read_at_ms,
            decision_ts_ms,
            valid_until_ms: decision_ts_ms + HOUR_MS,
            rows: vec![
                gate_row("BTCUSDT", 7.0, read_at_ms - 20 * 60_000),
                // Not in the tradable set: dropped, never an error.
                gate_row("OTHERUSDT", 9.0, read_at_ms - 60_000),
                // Below the score bar.
                gate_row("BTCUSDT", 5.0, read_at_ms - 60_000),
            ],
        })
        .unwrap();
    assert_eq!(out.len(), 1);
    let observation = &out[0];
    assert_eq!(observation.kind, "llm_gate_candidates");
    assert_eq!(
        observation.destination,
        StrategyId(test_config().long_destination)
    );
    assert_eq!(observation.observed_wall_ts_ms, decision_ts_ms);
    assert_eq!(observation.available_wall_ts_ms, read_at_ms);
    assert_eq!(observation.sequence, 1);
    assert_eq!(
        observation
            .subscriptions
            .iter()
            .map(|row| row.symbol.as_str())
            .collect::<BTreeSet<_>>(),
        BTreeSet::from(["BTCUSDT"])
    );
    let envelope: SignalPayloadEnvelope = serde_json::from_slice(&observation.payload).unwrap();
    let ObservationPayload::LlmGateCandidates {
        decision_ts_ms: published,
        valid_until_ms,
        rows,
        ..
    } = envelope.payload
    else {
        panic!("expected gate candidates");
    };
    assert_eq!(published, decision_ts_ms);
    assert_eq!(valid_until_ms, decision_ts_ms + HOUR_MS);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].symbol, "BTCUSDT");
    assert_eq!(rows[0].score, 7.0);
    assert_eq!(worker.state.long_output_sequence, 1);

    // A stale trigger is filtered, and an empty publication still travels:
    // it is how the ledger withdraws standing candidates.
    let out = worker
        .apply(WireEvent::LlmGateCandidates {
            schema_version: SCHEMA_VERSION,
            sequence: 2,
            observed_ts_ms: decision_ts_ms + 60_000,
            available_at_ms: read_at_ms + 60_000,
            decision_ts_ms: decision_ts_ms + 60_000,
            valid_until_ms: decision_ts_ms + 60_000 + HOUR_MS,
            rows: vec![gate_row("BTCUSDT", 8.0, read_at_ms - 2 * HOUR_MS)],
        })
        .unwrap();
    assert_eq!(out.len(), 1);
    let envelope: SignalPayloadEnvelope = serde_json::from_slice(&out[0].payload).unwrap();
    let ObservationPayload::LlmGateCandidates { rows, .. } = envelope.payload else {
        panic!("expected gate candidates");
    };
    assert!(rows.is_empty());
    assert_eq!(out[0].sequence, 2);
}

fn insert_exact<T: PartialEq>(
    rows: &mut BTreeMap<i64, T>,
    key: i64,
    value: T,
    label: &str,
) -> Result<(), WorkerError> {
    if let Some(existing) = rows.get(&key) {
        if existing == &value {
            return Ok(());
        }
        return Err(WorkerError::input(format!(
            "{label} history rewrote timestamp {key}"
        )));
    }
    rows.insert(key, value);
    Ok(())
}

#[test]
fn rejected_durable_batch_keeps_memory_and_restart_at_same_input() {
    let root = temporary_root("rejected-batch");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let config = test_config();
    let universe = test_universe();
    let mut durable = DurableSignalWorker::open_with_universe(
        config.clone(),
        universe.clone(),
        &state_dir,
        &spool_dir,
    )
    .unwrap();
    let before = serde_json::to_vec(durable.worker().state()).unwrap();
    let first = WireEvent::BybitKlineBatch {
        schema_version: SCHEMA_VERSION,
        sequence: 1,
        symbol: "BTCUSDT".into(),
        available_at_ms: 11 * DAY_MS,
        checked_from_ms: Some(10 * DAY_MS),
        checked_through_ms: Some(11 * DAY_MS),
        replace_coverage: false,
        rows: vec![],
    };
    let invalid = WireEvent::BybitKlineBatch {
        schema_version: SCHEMA_VERSION + 1,
        sequence: 2,
        symbol: "BTCUSDT".into(),
        available_at_ms: 11 * DAY_MS,
        checked_from_ms: None,
        checked_through_ms: None,
        replace_coverage: false,
        rows: vec![],
    };
    assert!(durable
        .apply_many_and_commit([first.clone(), invalid])
        .is_err());
    assert_eq!(
        serde_json::to_vec(durable.worker().state()).unwrap(),
        before,
        "rejected unjournaled batch must not advance memory or coverage"
    );
    drop(durable);
    let mut reopened =
        DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir).unwrap();
    assert_eq!(
        serde_json::to_vec(reopened.worker().state()).unwrap(),
        before
    );
    reopened.apply_and_commit(first).unwrap();
    assert_eq!(reopened.worker().state().last_input_sequence, 1);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn worker_error_categories_preserve_recovery_and_display() {
    let cases = [
        (
            WorkerError::config("bad"),
            WorkerErrorCategory::Config,
            "config: bad",
            false,
        ),
        (
            WorkerError::input("bad"),
            WorkerErrorCategory::Input,
            "input: bad",
            true,
        ),
        (
            WorkerError::state("bad"),
            WorkerErrorCategory::State,
            "state: bad",
            false,
        ),
        (
            WorkerError::network("bad"),
            WorkerErrorCategory::Network,
            "network: bad",
            true,
        ),
    ];
    for (error, category, text, lane_local) in cases {
        assert_eq!(error.category(), category);
        assert_eq!(error.to_string(), text);
        assert_eq!(error.is_lane_local_source_failure(), lane_local);
    }
    let error = WorkerError::io("read", std::io::Error::other("bad"));
    assert_eq!(error.category(), WorkerErrorCategory::Io);
    assert_eq!(error.to_string(), "io: read: bad");
    assert!(!error.is_lane_local_source_failure());
    let parse_error = serde_json::from_str::<u64>("no").unwrap_err();
    let error = WorkerError::json("decode", parse_error);
    assert_eq!(error.category(), WorkerErrorCategory::Json);
    assert!(error.to_string().starts_with("json: decode: "));
    assert!(!error.is_lane_local_source_failure());
}

#[test]
fn producer_readiness_echoes_each_boot_nonce_and_recovers_published_frontiers() {
    use engine_types::{SignalReadinessRequest, SignalReadinessResponse};
    let root = temporary_root("producer-readiness");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    let config = test_config();
    let universe = test_universe();
    let durable = DurableSignalWorker::open_with_universe(
        config.clone(),
        universe.clone(),
        &state_dir,
        &spool_dir,
    )
    .unwrap();
    let request = AtomicJsonStore::new(spool_dir.join("input-readiness-request.json"));
    let response = AtomicJsonStore::new(spool_dir.join("input-readiness-response.json"));
    durable.respond_to_readiness_request().unwrap();
    assert!(response
        .load::<SignalReadinessResponse>()
        .unwrap()
        .is_none());
    request
        .save(&SignalReadinessRequest {
            schema_version: 1,
            boot_nonce: "boot-one".into(),
        })
        .unwrap();
    durable.respond_to_readiness_request().unwrap();
    let first = response.load::<SignalReadinessResponse>().unwrap().unwrap();
    assert_eq!(first.boot_nonce, "boot-one");
    assert_eq!(first.sources.len(), 2);
    assert_eq!(first.sources[0].published_through, 0);
    assert_eq!(first.sources[1].published_through, 0);
    assert!(first.sources[0].source.ends_with(".long"));
    assert!(first.sources[1].source.ends_with(".carry"));
    let initial_generation = durable.worker().state().source_generation.clone();
    let mut committed_state = durable.worker().state().clone();
    committed_state.long_output_sequence = 7;
    committed_state.carry_output_sequence = 11;
    durable.checkpoint.save(&committed_state).unwrap();
    drop(durable);
    let reopened =
        DurableSignalWorker::open_with_universe(config, universe, &state_dir, &spool_dir).unwrap();
    request
        .save(&SignalReadinessRequest {
            schema_version: 1,
            boot_nonce: "boot-two".into(),
        })
        .unwrap();
    assert_eq!(
        response
            .load::<SignalReadinessResponse>()
            .unwrap()
            .unwrap()
            .boot_nonce,
        "boot-one"
    );
    reopened.respond_to_readiness_request().unwrap();
    let second = response.load::<SignalReadinessResponse>().unwrap().unwrap();
    assert_eq!(second.boot_nonce, "boot-two");
    assert_eq!(second.sources[0].source, first.sources[0].source);
    assert!(second.sources[0].source.contains(&initial_generation));
    assert_eq!(second.sources[0].published_through, 7);
    assert_eq!(second.sources[1].published_through, 11);
    assert_eq!(reopened.spool.inventory().unwrap().files, 0);
    std::fs::remove_dir_all(root).unwrap();
}
