from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from aggression_carry.config import TradeLifecycleConfig
from aggression_carry.ingestion import generate_fixture_data
from aggression_carry.trade_lifecycle import _funding_lookup, _perp_funding_return
from aggression_carry.volume_events import (
    ENTRY_POLICY_EXECUTION_PULLBACK_GUARD,
    ENTRY_POLICY_FIXED_DELAY,
    ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
    _apply_entry_execution_veto,
    _attach_event_archive_membership,
    _apply_liquidity_migration_crowding_filter,
    _basis_feature_frame,
    _daily_return_frame,
    _enriched_event_features,
    _entry_decision_for_event,
    _event_decay_exit_hit,
    _event_filter,
    _execution_ordered_events,
    _float_or_nan,
    _full_pit_universe_error,
    _funding_feature_frame,
    _open_interest_feature_frame,
    _signed_flow_feature_frame,
    _simulate_indexed_trade,
    _stop_pressure_active,
    VolumeEventResearchConfig,
    _add_rank_fraction,
    _monthly_returns,
    _promotion_fields,
    _scenario_side,
    _validate_event_config,
    _write_equity_benchmark_chart,
    run_volume_event_research,
)


def _migration_unit_config(**overrides: object) -> VolumeEventResearchConfig:
    defaults = {
        "liquidity_migration_close_location_min": 0.0,
        "liquidity_migration_pit_age_days_min": 0,
        "liquidity_migration_crowding_filter": "none",
    }
    defaults.update(overrides)
    return VolumeEventResearchConfig(**defaults)


def test_add_rank_fraction_scales_cross_section_to_zero_one() -> None:
    frame = pl.DataFrame(
        [
            {"ts_ms": 1, "symbol": "A", "score": 10.0},
            {"ts_ms": 1, "symbol": "B", "score": 20.0},
            {"ts_ms": 1, "symbol": "C", "score": 30.0},
        ]
    )

    ranked = _add_rank_fraction(frame, "score", "score_rank_frac").sort("symbol")

    assert ranked["score_rank_frac"].to_list() == pytest.approx([0.0, 0.5, 1.0])


def test_model_v1_crowding_filter_keeps_only_idiosyncratic_events() -> None:
    events = pl.DataFrame(
        [
            {
                "symbol": "IDIOUSDT",
                "ts_ms": 1_700_000_000_000,
                "market_pct_up_1d": 0.45,
                "btc_return_1d": 0.01,
                "daily_return_1d": 0.13,
                "residual_return_1d": 0.11,
                "signal_day_last6h_turnover_share": 0.25,
                "signal_day_last6h_return": 0.02,
                "liquidity_migration_turnover_ratio": 8.0,
                "pit_age_days": 120.0,
            },
            {
                "symbol": "MARKET1USDT",
                "ts_ms": 1_700_003_600_000,
                "market_pct_up_1d": 0.82,
                "btc_return_1d": 0.04,
                "daily_return_1d": 0.08,
                "residual_return_1d": 0.03,
                "signal_day_last6h_turnover_share": 0.20,
                "signal_day_last6h_return": 0.01,
                "liquidity_migration_turnover_ratio": 7.0,
                "pit_age_days": 120.0,
            },
            {
                "symbol": "MARKET2USDT",
                "ts_ms": 1_700_003_600_000,
                "market_pct_up_1d": 0.82,
                "btc_return_1d": 0.04,
                "daily_return_1d": 0.09,
                "residual_return_1d": 0.04,
                "signal_day_last6h_turnover_share": 0.24,
                "signal_day_last6h_return": 0.01,
                "liquidity_migration_turnover_ratio": 7.0,
                "pit_age_days": 120.0,
            },
            {
                "symbol": "ARTUSDT",
                "ts_ms": 1_700_007_200_000,
                "market_pct_up_1d": 0.50,
                "btc_return_1d": 0.00,
                "daily_return_1d": 0.14,
                "residual_return_1d": 0.10,
                "signal_day_last6h_turnover_share": 0.96,
                "signal_day_last6h_return": 0.08,
                "liquidity_migration_turnover_ratio": 100.0,
                "pit_age_days": 120.0,
            },
        ]
    )

    filtered = _apply_liquidity_migration_crowding_filter(
        events,
        config=_migration_unit_config(liquidity_migration_crowding_filter="model_v1"),
    )

    assert filtered["symbol"].to_list() == ["IDIOUSDT"]
    assert filtered["crowding_class"].to_list() == ["isolated_idiosyncratic_event"]


def test_float_or_nan_handles_missing_context_values() -> None:
    assert _float_or_nan(None) != _float_or_nan(None)
    assert _float_or_nan("bad") != _float_or_nan("bad")
    assert _float_or_nan("0.25") == pytest.approx(0.25)


def test_orthogonal_feature_frames_are_point_in_time_daily() -> None:
    day = 24 * 60 * 60 * 1000
    funding = pl.DataFrame(
        [
            {"symbol": "AUSDT", "ts_ms": day, "funding_rate_8h_equiv": 0.001},
            {"symbol": "AUSDT", "ts_ms": day + 8 * 60 * 60 * 1000, "funding_rate_8h_equiv": 0.002},
            {"symbol": "AUSDT", "ts_ms": day + 16 * 60 * 60 * 1000, "funding_rate_8h_equiv": -0.001},
        ]
    )
    daily_returns = pl.DataFrame(
        [
            {"symbol": "AUSDT", "ts_ms": day, "daily_close": 10.0},
            {"symbol": "AUSDT", "ts_ms": 2 * day, "daily_close": 12.0},
            {"symbol": "AUSDT", "ts_ms": 3 * day, "daily_close": 15.0},
            {"symbol": "AUSDT", "ts_ms": 4 * day, "daily_close": 18.0},
        ]
    )
    open_interest = pl.DataFrame(
        [
            {"symbol": "AUSDT", "ts_ms": day, "open_interest": 100.0},
            {"symbol": "AUSDT", "ts_ms": 2 * day, "open_interest": 120.0},
            {"symbol": "AUSDT", "ts_ms": 3 * day, "open_interest": 150.0},
            {"symbol": "AUSDT", "ts_ms": 4 * day, "open_interest": 180.0},
        ]
    )
    flow = pl.DataFrame(
        [
            {"symbol": "AUSDT", "ts_ms": day + 23 * 60 * 60 * 1000, "buy_quote": 70.0, "sell_quote": 30.0, "signed_quote": 40.0, "total_quote": 100.0},
            {"symbol": "AUSDT", "ts_ms": 2 * day + 23 * 60 * 60 * 1000, "buy_quote": 20.0, "sell_quote": 80.0, "signed_quote": -60.0, "total_quote": 100.0},
        ]
    )

    funding_features = _funding_feature_frame(funding).sort("ts_ms")
    oi_features = _open_interest_feature_frame(open_interest, daily_returns).sort("ts_ms")
    flow_features = _signed_flow_feature_frame(flow).sort("ts_ms")

    assert funding_features.filter(pl.col("ts_ms") == day)["funding_rate_1d_sum"][0] == pytest.approx(0.001)
    assert funding_features.filter(pl.col("ts_ms") == 2 * day)["funding_rate_1d_sum"][0] == pytest.approx(0.001)
    assert oi_features.filter(pl.col("ts_ms") == 4 * day)["open_interest_return_3d"][0] == pytest.approx(0.8)
    assert oi_features.filter(pl.col("ts_ms") == 4 * day)["open_interest_quote"][0] == pytest.approx(3240.0)
    assert flow_features.filter(pl.col("ts_ms") == 2 * day)["taker_imbalance_1d"][0] == pytest.approx(0.4)
    assert flow_features.filter(pl.col("ts_ms") == 3 * day)["taker_imbalance_3d"][0] == pytest.approx(-0.1)


def test_basis_feature_frame_maps_hourly_basis_to_signal_day() -> None:
    day = 24 * 60 * 60 * 1000
    mark = pl.DataFrame(
        [
            {"symbol": "AUSDT", "ts_ms": day + 23 * 60 * 60 * 1000, "close": 102.0},
            {"symbol": "AUSDT", "ts_ms": 2 * day + 23 * 60 * 60 * 1000, "close": 103.0},
        ]
    )
    index = pl.DataFrame(
        [
            {"symbol": "AUSDT", "ts_ms": day + 23 * 60 * 60 * 1000, "close": 100.0},
            {"symbol": "AUSDT", "ts_ms": 2 * day + 23 * 60 * 60 * 1000, "close": 100.0},
        ]
    )
    premium = pl.DataFrame(
        [
            {"symbol": "AUSDT", "ts_ms": day + 23 * 60 * 60 * 1000, "close": 0.002},
            {"symbol": "AUSDT", "ts_ms": 2 * day + 23 * 60 * 60 * 1000, "close": 0.003},
        ]
    )

    basis = _basis_feature_frame(mark, index, premium).sort("ts_ms")
    second_day = basis.filter(pl.col("ts_ms") == 2 * day).to_dicts()[0]

    assert second_day["mark_index_basis_last"] == pytest.approx(0.02)
    assert second_day["premium_index_last"] == pytest.approx(0.002)


def test_enriched_event_features_adds_feature_factory_columns() -> None:
    day = 24 * 60 * 60 * 1000
    hour = 60 * 60 * 1000
    symbols = ("AUSDT", "BUSDT", "CUSDT")
    feature_rows = []
    kline_rows = []
    mark_rows = []
    index_rows = []
    premium_rows = []
    for day_index in range(10):
        day_start = day_index * day
        ts_ms = (day_index + 1) * day
        for symbol_index, symbol in enumerate(symbols):
            rank = (220 - 20 * day_index) if symbol == "AUSDT" else 60 + symbol_index * 20
            turnover = 1_000_000.0 + day_index * 50_000.0 + symbol_index * 10_000.0
            feature_rows.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "turnover_quote": turnover,
                    "log_turnover": float(symbol_index + day_index),
                    "volume_change_1d_z": float(symbol_index + 1),
                    "volume_change_3d_z": float(symbol_index),
                    "volume_persistence_z": float(day_index - symbol_index),
                    "dollar_volume_rank_z": float(3 - symbol_index),
                    "liquidity_rank": rank,
                    "liquidity_rank_pct": rank / 300.0,
                    "volume_composite": float(symbol_index + 1),
                }
            )
            open_price = 100.0 + symbol_index
            close_price = open_price * (1.0 + 0.01 * day_index + (0.03 if symbol == "AUSDT" else 0.0))
            for hour_index in range(24):
                kline_rows.append(
                    {
                        "symbol": symbol,
                        "ts_ms": day_start + hour_index * hour,
                        "open": open_price,
                        "high": close_price * 1.04,
                        "low": open_price * 0.96,
                        "close": close_price,
                        "turnover_quote": turnover / 24.0,
                        "date": "2024-01-01",
                    }
                )
            mark_rows.append({"symbol": symbol, "ts_ms": day_start + 23 * hour, "close": close_price * 1.002})
            index_rows.append({"symbol": symbol, "ts_ms": day_start + 23 * hour, "close": close_price})
            premium_rows.append({"symbol": symbol, "ts_ms": day_start + 23 * hour, "close": 0.001 * (symbol_index + 1)})

    enriched = _enriched_event_features(
        pl.DataFrame(feature_rows),
        pl.DataFrame(kline_rows),
        pl.DataFrame(),
        mark_price_1h=pl.DataFrame(mark_rows),
        index_price_1h=pl.DataFrame(index_rows),
        premium_index_1h=pl.DataFrame(premium_rows),
    )
    last_a = enriched.filter((pl.col("symbol") == "AUSDT") & (pl.col("ts_ms") == 10 * day)).to_dicts()[0]

    assert last_a["liquidity_rank_improvement_1d"] == pytest.approx(20.0)
    assert last_a["liquidity_rank_improvement_3d"] == pytest.approx(60.0)
    assert last_a["liquidity_rank_speed_3d"] == pytest.approx(20.0)
    assert last_a["intraday_range_expansion_7d"] > 0.0
    assert 0.0 <= last_a["event_uniqueness_score"] <= 1.0
    assert last_a["mark_index_basis_last"] == pytest.approx(0.002)


def test_bar_extreme_stop_fill_uses_adverse_hourly_extreme_for_short() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = {
        "rows": [
            {"bar_end_ts_ms": hour, "high": 101.0, "low": 99.0, "close": 100.0},
            {"bar_end_ts_ms": 2 * hour, "high": 130.0, "low": 95.0, "close": 105.0},
        ],
        "ends": [hour, 2 * hour],
        "by_end": {},
    }
    base_kwargs = {
        "symbol": "AUSDT",
        "side": "short",
        "score": 1.0,
        "rank": 1,
        "basket_id": "basket",
        "signal_ts_ms": 0,
        "entry_bar": symbol_bars["rows"][0],
        "symbol_bars": symbol_bars,
        "planned_exit_ts_ms": 2 * hour,
        "notional_weight": 1.0,
        "config": TradeLifecycleConfig(take_profit_pct=0.0),
        "round_trip_cost_bps": 0.0,
        "stop_pct": 0.12,
        "rank_lookup": {},
        "event_decay_threshold": -1.0,
        "funding_lookup": None,
    }

    normal = _simulate_indexed_trade(**base_kwargs, stop_fill_mode="stop")
    stressed = _simulate_indexed_trade(**base_kwargs, stop_fill_mode="bar_extreme")

    assert normal is not None
    assert stressed is not None
    assert normal["exit_price"] == pytest.approx(112.0)
    assert stressed["exit_price"] == pytest.approx(130.0)
    assert stressed["gross_trade_return"] == pytest.approx(-0.30)


def test_promoted_quality_entry_waits_for_completed_giveback() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = {
        "rows": [],
        "ends": [0, hour, 2 * hour, 3 * hour],
        "by_end": {
            0: {"bar_end_ts_ms": 0, "high": 101.0, "low": 99.0, "close": 100.0},
            hour: {"bar_end_ts_ms": hour, "high": 101.2, "low": 100.0, "close": 101.1},
            2 * hour: {"bar_end_ts_ms": 2 * hour, "high": 101.6, "low": 101.0, "close": 101.5},
            3 * hour: {"bar_end_ts_ms": 3 * hour, "high": 101.6, "low": 100.9, "close": 101.1},
        },
    }
    event = {
        "ts_ms": 0,
        "symbol": "AAAUSDT",
        "dollar_volume_rank_z_rank_frac": 0.85,
        "liquidity_rank": 50,
        "prior7_liquidity_rank": 225,
        "turnover_quote": 7_000_000.0,
        "prior7_turnover_quote_mean": 1_000_000.0,
        "daily_return_1d": 0.02,
        "residual_return_1d": 0.09,
        "market_pct_up_1d": 0.55,
        "signal_day_close_location": 0.70,
        "pit_age_days": 120.0,
    }

    pending = _entry_decision_for_event(
        event,
        symbol_bars,
        config=VolumeEventResearchConfig(),
        score_col="dollar_volume_rank_z",
        now_ms=2 * hour + 30_000,
    )
    ready = _entry_decision_for_event(
        event,
        symbol_bars,
        config=VolumeEventResearchConfig(),
        score_col="dollar_volume_rank_z",
        now_ms=3 * hour + 30_000,
    )

    assert pending["pending"] is True
    assert ready["entry_ts_ms"] == 3 * hour
    assert ready["entry_rule"] == "quality_squeeze_giveback"
    assert ready["actual_entry_delay_hours"] == pytest.approx(3.0)


def test_fixed_entry_policy_keeps_plain_delay() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = {
        "rows": [],
        "ends": [hour],
        "by_end": {hour: {"bar_end_ts_ms": hour, "high": 101.0, "low": 99.0, "close": 100.0}},
    }

    decision = _entry_decision_for_event(
        {"ts_ms": 0, "symbol": "AAAUSDT"},
        symbol_bars,
        config=VolumeEventResearchConfig(entry_policy=ENTRY_POLICY_FIXED_DELAY),
        score_col="dollar_volume_rank_z",
        now_ms=hour + 1,
    )

    assert decision["entry_policy"] == ENTRY_POLICY_FIXED_DELAY
    assert decision["entry_ts_ms"] == hour
    assert decision["entry_bar"]["close"] == 100.0


def test_execution_pullback_guard_waits_for_micro_pullback() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = {
        "rows": [],
        "ends": [0, hour, 2 * hour],
        "by_end": {
            0: {"bar_end_ts_ms": 0, "high": 101.0, "low": 99.0, "close": 100.0, "turnover_quote": 1_000_000.0},
            hour: {
                "bar_end_ts_ms": hour,
                "high": 102.0,
                "low": 100.6,
                "close": 101.9,
                "turnover_quote": 1_000_000.0,
            },
            2 * hour: {
                "bar_end_ts_ms": 2 * hour,
                "high": 102.1,
                "low": 100.7,
                "close": 101.0,
                "turnover_quote": 1_000_000.0,
            },
        },
    }

    decision = _entry_decision_for_event(
        {"ts_ms": 0, "symbol": "AAAUSDT"},
        symbol_bars,
        config=VolumeEventResearchConfig(
            entry_policy=ENTRY_POLICY_EXECUTION_PULLBACK_GUARD,
            entry_execution_wait_hours=2,
            entry_execution_unresolved_move_bps_max=150.0,
        ),
        score_col="dollar_volume_rank_z",
        side="short",
        now_ms=2 * hour + 1,
    )

    assert decision["entry_policy"] == ENTRY_POLICY_EXECUTION_PULLBACK_GUARD
    assert decision["entry_ts_ms"] == 2 * hour
    assert decision["entry_rule"] == "execution_guard_pullback"
    assert decision["entry_bar"]["close"] == pytest.approx(101.0)
    assert decision["entry_bar_close_location"] < 0.70


def test_execution_pullback_guard_skips_runaway_continuation() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = {
        "rows": [],
        "ends": [0, hour],
        "by_end": {
            0: {"bar_end_ts_ms": 0, "high": 101.0, "low": 99.0, "close": 100.0, "turnover_quote": 1_000_000.0},
            hour: {
                "bar_end_ts_ms": hour,
                "high": 104.5,
                "low": 101.0,
                "close": 104.0,
                "turnover_quote": 1_000_000.0,
            },
        },
    }

    decision = _entry_decision_for_event(
        {"ts_ms": 0, "symbol": "AAAUSDT"},
        symbol_bars,
        config=VolumeEventResearchConfig(
            entry_policy=ENTRY_POLICY_EXECUTION_PULLBACK_GUARD,
            entry_execution_wait_hours=1,
            entry_execution_unresolved_move_bps_max=150.0,
        ),
        score_col="dollar_volume_rank_z",
        side="short",
        now_ms=hour + 1,
    )

    assert decision["entry_bar"] is None
    assert decision["entry_rule"] == "execution_guard_skip_moved_too_far"
    assert decision["entry_continuation_bps"] > 300.0


def test_tiered_execution_sniper_waits_for_standard_pop() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = {
        "rows": [],
        "ends": [0, hour, 2 * hour],
        "by_end": {
            0: {"bar_end_ts_ms": 0, "high": 101.0, "low": 99.0, "close": 100.0, "turnover_quote": 1_000_000.0},
            hour: {
                "bar_end_ts_ms": hour,
                "high": 100.8,
                "low": 99.8,
                "close": 100.7,
                "turnover_quote": 1_000_000.0,
            },
            2 * hour: {
                "bar_end_ts_ms": 2 * hour,
                "high": 101.3,
                "low": 100.2,
                "close": 101.0,
                "turnover_quote": 1_000_000.0,
            },
        },
    }

    decision = _entry_decision_for_event(
        {"ts_ms": 0, "symbol": "AAAUSDT"},
        symbol_bars,
        config=VolumeEventResearchConfig(
            entry_policy=ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
            entry_execution_wait_hours=2,
            entry_execution_pop_bps=100.0,
            entry_execution_unresolved_move_bps_max=50.0,
        ),
        score_col="dollar_volume_rank_z",
        side="short",
        now_ms=2 * hour + 1,
    )

    assert decision["entry_policy"] == ENTRY_POLICY_TIERED_EXECUTION_SNIPER
    assert decision["entry_ts_ms"] == 2 * hour
    assert decision["entry_rule"] == "tiered_standard_pop"
    assert decision["entry_pop_bps"] >= 100.0


def test_tiered_execution_sniper_fallback_is_deadline_bar() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = {
        "rows": [],
        "ends": [0, hour, 2 * hour],
        "by_end": {
            0: {"bar_end_ts_ms": 0, "high": 101.0, "low": 99.0, "close": 100.0, "turnover_quote": 1_000_000.0},
            hour: {
                "bar_end_ts_ms": hour,
                "high": 102.0,
                "low": 100.2,
                "close": 101.8,
                "turnover_quote": 1_000_000.0,
            },
            2 * hour: {
                "bar_end_ts_ms": 2 * hour,
                "high": 102.5,
                "low": 100.3,
                "close": 101.2,
                "turnover_quote": 1_000_000.0,
            },
        },
    }

    decision = _entry_decision_for_event(
        {"ts_ms": 0, "symbol": "AAAUSDT"},
        symbol_bars,
        config=VolumeEventResearchConfig(
            entry_policy=ENTRY_POLICY_TIERED_EXECUTION_SNIPER,
            entry_execution_wait_hours=2,
            entry_execution_pop_bps=300.0,
        ),
        score_col="dollar_volume_rank_z",
        side="short",
        now_ms=2 * hour + 1,
    )

    assert decision["entry_ts_ms"] == 2 * hour
    assert decision["entry_rule"] == "tiered_standard_deadline_fallback"
    assert decision["entry_bar"]["close"] == pytest.approx(101.2)


def test_entry_execution_veto_skips_high_close_location() -> None:
    decision = {
        "entry_bar": {"bar_end_ts_ms": 1, "high": 102.0, "low": 100.0, "close": 101.9},
        "entry_rule": "quality_fixed_delay",
        "pending": False,
    }

    vetoed = _apply_entry_execution_veto(
        decision,
        config=VolumeEventResearchConfig(entry_execution_veto_close_location_max=0.90),
    )

    assert vetoed["entry_bar"] is None
    assert vetoed["entry_rule"] == "quality_fixed_delay_veto_high_close_location"
    assert vetoed["entry_bar_close_location"] == pytest.approx(0.95)


def test_funding_lookup_marks_symbol_or_date_gaps_as_missing_or_partial() -> None:
    day = 24 * 60 * 60 * 1000
    funding = pl.DataFrame(
        [
            {"symbol": "AUSDT", "ts_ms": day, "funding_rate": 0.001},
            {"symbol": "AUSDT", "ts_ms": 2 * day, "funding_rate": 0.002},
        ]
    )

    lookup = _funding_lookup(funding)

    assert lookup is not None
    assert _perp_funding_return(lookup, symbol="BUSDT", side="short", entry_ts_ms=day, exit_ts_ms=2 * day) == (
        0.0,
        "missing",
        0,
    )
    assert _perp_funding_return(lookup, symbol="AUSDT", side="short", entry_ts_ms=0, exit_ts_ms=2 * day) == (
        0.0,
        "partial",
        0,
    )
    assert _perp_funding_return(lookup, symbol="AUSDT", side="short", entry_ts_ms=day, exit_ts_ms=2 * day) == (
        pytest.approx(0.002),
        "modeled",
        1,
    )


def test_volume_event_research_writes_reports_on_fixture(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    payload = run_volume_event_research(
        tmp_path,
        event_config=VolumeEventResearchConfig(
            event_types=("fresh_volume_spike",),
            thresholds=(0.5,),
            hold_days=(1,),
            side_hypotheses=("continuation",),
            stop_loss_pcts=(0.0,),
            cost_multipliers=(1.0,),
            max_active_symbols=4,
            cooldown_days=0,
            require_full_pit_universe=False,
        ),
    )

    assert payload["rows"]["scenarios"] == 1
    assert (tmp_path / "reports" / "volume_event_research" / "volume_event_research_report.md").exists()
    assert (tmp_path / "reports" / "volume_event_research" / "volume_event_scenario_summary.csv").exists()
    assert "best_equity_chart" in payload


def test_equity_benchmark_chart_writes_overlays_without_annotations(tmp_path: Path) -> None:
    from PIL import Image

    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    equity = pl.DataFrame(
        [
            {"ts_ms": 1, "date": "2024-01-01", "equity": 1.0, "drawdown": 0.0, "basket_return": 0.0},
            {"ts_ms": 2, "date": "2024-01-02", "equity": 0.9, "drawdown": -0.10, "basket_return": -0.10},
            {"ts_ms": 3, "date": "2024-01-03", "equity": 1.08, "drawdown": 0.0, "basket_return": 0.20},
            {"ts_ms": 4, "date": "2024-01-04", "equity": 1.0, "drawdown": -0.074, "basket_return": -0.074},
            {"ts_ms": 5, "date": "2024-01-05", "equity": 1.14, "drawdown": 0.0, "basket_return": 0.14},
        ]
    )
    raw_klines = pl.DataFrame(
        [
            {"ts_ms": 1, "date": "2024-01-01", "symbol": "BTCUSDT", "close": 100.0},
            {"ts_ms": 2, "date": "2024-01-02", "symbol": "BTCUSDT", "close": 110.0},
            {"ts_ms": 3, "date": "2024-01-03", "symbol": "BTCUSDT", "close": 120.0},
            {"ts_ms": 4, "date": "2024-01-04", "symbol": "BTCUSDT", "close": 115.0},
            {"ts_ms": 5, "date": "2024-01-05", "symbol": "BTCUSDT", "close": 122.0},
        ]
    )

    chart = _write_equity_benchmark_chart(output_dir, root=tmp_path, equity=equity, raw_klines=raw_klines)

    assert Path(chart["png"]).exists()
    assert Path(chart["png"]).name == "volume_event_best_equity_btc.png"
    with Image.open(chart["png"]) as image:
        assert image.size == (1600, 940)
    assert not (output_dir / "volume_event_best_equity_btc_spy.png").exists()
    assert not (output_dir / "volume_event_best_equity_btc_spy.svg").exists()
    assert not (output_dir / "volume_event_best_equity_benchmarks.csv").exists()
    assert not (output_dir / "volume_event_best_equity_annotations.csv").exists()
    assert chart["series"]["strategy"] == 5
    assert chart["series"]["btc"] == 5
    assert "spy" not in chart["series"]
    assert "spy_status" not in chart
    assert chart["annotations"] == []


def test_volume_event_research_requires_full_pit_by_default(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    with pytest.raises(RuntimeError, match="requires full PIT archive membership"):
        run_volume_event_research(
            tmp_path,
            event_config=VolumeEventResearchConfig(
                event_types=("fresh_volume_spike",),
                thresholds=(0.5,),
                hold_days=(1,),
                side_hypotheses=("continuation",),
                stop_loss_pcts=(0.0,),
                cost_multipliers=(1.0,),
            ),
        )


def test_full_pit_universe_error_reports_missing_symbols() -> None:
    klines = pl.DataFrame([{"symbol": "AAAUSDT", "date": "2024-01-01"} for _ in range(24)])
    manifest = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "date": "2024-01-01"},
            {"symbol": "BBBUSDT", "date": "2024-01-01"},
        ]
    )

    message = _full_pit_universe_error(klines, manifest)

    assert "missing_symbols=1" in message
    assert "BBBUSDT" in message


def test_full_pit_universe_error_reports_missing_symbol_dates() -> None:
    klines = pl.DataFrame([{"symbol": "AAAUSDT", "date": "2024-01-01"} for _ in range(24)])
    manifest = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "date": "2024-01-01"},
            {"symbol": "AAAUSDT", "date": "2024-01-02"},
        ]
    )

    message = _full_pit_universe_error(klines, manifest)

    assert "missing_symbols=0" in message
    assert "missing_date_symbols=1" in message
    assert "2024-01-02" in message


def test_event_filter_excludes_default_stable_and_peg_symbols() -> None:
    features = pl.DataFrame(
        [
            {
                "ts_ms": 1,
                "symbol": "AAAUSDT",
                "score": 1.0,
                "score_rank_frac": 0.9,
                "prior_score_rank_frac": 0.1,
                "turnover_quote": 10_000_000.0,
                "liquidity_rank": 40,
            },
            {
                "ts_ms": 1,
                "symbol": "XRPUSDT",
                "score": 1.0,
                "score_rank_frac": 0.9,
                "prior_score_rank_frac": 0.1,
                "turnover_quote": 10_000_000.0,
                "liquidity_rank": 41,
            },
            {
                "ts_ms": 1,
                "symbol": "USDCUSDT",
                "score": 1.0,
                "score_rank_frac": 0.9,
                "prior_score_rank_frac": 0.1,
                "turnover_quote": 10_000_000.0,
                "liquidity_rank": 42,
            },
        ]
    )

    filtered = _event_filter(
        features,
        "fresh_volume_spike",
        score_col="score",
        rank_col="score_rank_frac",
        top_cut=0.7,
        config=VolumeEventResearchConfig(require_pit_membership=False),
    )

    assert filtered["symbol"].to_list() == ["AAAUSDT", "XRPUSDT"]


def test_volume_event_config_validates_new_research_knobs() -> None:
    with pytest.raises(ValueError, match="entry_delay_hours"):
        _validate_event_config(VolumeEventResearchConfig(entry_delay_hours=-1))

    with pytest.raises(ValueError, match="entry_policy"):
        _validate_event_config(VolumeEventResearchConfig(entry_policy="bad_policy"))

    with pytest.raises(ValueError, match="entry_quality_squeeze_h1_close_location_min"):
        _validate_event_config(VolumeEventResearchConfig(entry_quality_squeeze_h1_close_location_min=1.1))

    with pytest.raises(ValueError, match="entry_quality_squeeze_wait_hours"):
        _validate_event_config(VolumeEventResearchConfig(entry_quality_squeeze_wait_hours=0))

    with pytest.raises(ValueError, match="entry_execution_wait_hours"):
        _validate_event_config(
            VolumeEventResearchConfig(
                entry_policy=ENTRY_POLICY_EXECUTION_PULLBACK_GUARD,
                entry_execution_wait_hours=0,
            )
        )

    with pytest.raises(ValueError, match="entry_execution_pullback_close_location_max"):
        _validate_event_config(VolumeEventResearchConfig(entry_execution_pullback_close_location_max=1.1))

    with pytest.raises(ValueError, match="entry_execution_unresolved_move_bps_max"):
        _validate_event_config(VolumeEventResearchConfig(entry_execution_unresolved_move_bps_max=-1.0))

    with pytest.raises(ValueError, match="entry_execution_max_range_bps"):
        _validate_event_config(VolumeEventResearchConfig(entry_execution_max_range_bps=-1.0))

    with pytest.raises(ValueError, match="entry_execution_veto_close_location_max"):
        _validate_event_config(VolumeEventResearchConfig(entry_execution_veto_close_location_max=1.1))

    with pytest.raises(ValueError, match="rank_exit_threshold"):
        _validate_event_config(VolumeEventResearchConfig(rank_exit_threshold=0.0))

    with pytest.raises(ValueError, match="tail_rank_min"):
        _validate_event_config(VolumeEventResearchConfig(tail_rank_min=200, tail_rank_max=100))

    with pytest.raises(ValueError, match="liquidity_migration_rank_improvement_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_rank_improvement_min=-1))

    with pytest.raises(ValueError, match="liquidity_migration_turnover_ratio_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_turnover_ratio_min=-1.0))

    with pytest.raises(ValueError, match="liquidity_migration_prior_rank_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_prior_rank_min=-1))

    with pytest.raises(ValueError, match="liquidity_migration_current_rank_max"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_current_rank_max=-1))

    with pytest.raises(ValueError, match="liquidity_migration_event_rank_fraction_max"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_event_rank_fraction_max=1.1))
    with pytest.raises(ValueError, match="take profit pcts"):
        _validate_event_config(VolumeEventResearchConfig(take_profit_pcts=(-0.1,)))
    with pytest.raises(ValueError, match="liquidity_migration_event_rank_fraction_exclude_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_event_rank_fraction_exclude_min=0.9))
    with pytest.raises(ValueError, match="liquidity_migration_event_rank_fraction_exclude_min"):
        _validate_event_config(
            VolumeEventResearchConfig(
                liquidity_migration_event_rank_fraction_exclude_min=0.86,
                liquidity_migration_event_rank_fraction_exclude_max=0.84,
            )
        )

    with pytest.raises(ValueError, match="liquidity_migration_score_max"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_score_max=-0.1))
    with pytest.raises(ValueError, match="liquidity_migration_day_return_min"):
        _validate_event_config(
            VolumeEventResearchConfig(liquidity_migration_day_return_min=0.2, liquidity_migration_day_return_max=0.1)
        )
    with pytest.raises(ValueError, match="liquidity_migration_return_7d_min"):
        _validate_event_config(
            VolumeEventResearchConfig(liquidity_migration_return_7d_min=0.2, liquidity_migration_return_7d_max=0.1)
        )
    with pytest.raises(ValueError, match="liquidity_migration_residual_return_min"):
        _validate_event_config(
            VolumeEventResearchConfig(
                liquidity_migration_residual_return_min=0.08,
                liquidity_migration_residual_return_max=0.03,
            )
        )
    with pytest.raises(ValueError, match="liquidity_migration_close_to_high_7d_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_close_to_high_7d_min=0.01))
    with pytest.raises(ValueError, match="liquidity_migration_prior30_max_return_min"):
        _validate_event_config(
            VolumeEventResearchConfig(
                liquidity_migration_prior30_max_return_min=0.4,
                liquidity_migration_prior30_max_return_max=0.2,
            )
        )
    with pytest.raises(ValueError, match="liquidity_migration_mark_index_basis_3d_mean_min"):
        _validate_event_config(
            VolumeEventResearchConfig(
                liquidity_migration_mark_index_basis_3d_mean_min=0.002,
                liquidity_migration_mark_index_basis_3d_mean_max=0.001,
            )
        )
    with pytest.raises(ValueError, match="liquidity_migration_premium_index_3d_mean_min"):
        _validate_event_config(
            VolumeEventResearchConfig(
                liquidity_migration_premium_index_3d_mean_min=0.002,
                liquidity_migration_premium_index_3d_mean_max=0.001,
            )
        )
    with pytest.raises(ValueError, match="liquidity_migration_prior7_return_volatility"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_prior7_return_volatility_min=-0.01))
    with pytest.raises(ValueError, match="liquidity_migration_intraday_range_max"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_intraday_range_max=-0.01))
    with pytest.raises(ValueError, match="liquidity_migration_market_pct_up_max"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_market_pct_up_max=1.1))
    with pytest.raises(ValueError, match="liquidity_migration_hot_market_day_return_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_hot_market_day_return_min=-0.1))

    with pytest.raises(ValueError, match="market_median_return_1d_min"):
        _validate_event_config(
            VolumeEventResearchConfig(market_median_return_1d_min=0.05, market_median_return_1d_max=0.01)
        )

    with pytest.raises(ValueError, match="market_pct_up_1d_max"):
        _validate_event_config(VolumeEventResearchConfig(market_pct_up_1d_max=1.1))

    with pytest.raises(ValueError, match="btc_return_1d_min"):
        _validate_event_config(VolumeEventResearchConfig(btc_return_1d_min=0.1, btc_return_1d_max=0.0))

    with pytest.raises(ValueError, match="stop_pressure_window_days"):
        _validate_event_config(VolumeEventResearchConfig(stop_pressure_window_days=-1))

    with pytest.raises(ValueError, match="stop_pressure_stop_count"):
        _validate_event_config(VolumeEventResearchConfig(stop_pressure_stop_count=-1))

    with pytest.raises(ValueError, match="dryup_prior_volume_rank_max"):
        _validate_event_config(VolumeEventResearchConfig(dryup_prior_volume_rank_max=1.5))


def test_volume_event_promotion_requires_all_splits_positive() -> None:
    fields = _promotion_fields(
        [
            {"name": "train_2023_2024", "total_return": 0.10, "max_drawdown": -0.10, "sharpe_like": 1.0},
            {"name": "validation_2024_2025", "total_return": -0.01, "max_drawdown": -0.05, "sharpe_like": 0.8},
            {"name": "oos_2025_2026", "total_return": 0.03, "max_drawdown": -0.12, "sharpe_like": 0.7},
        ],
        config=VolumeEventResearchConfig(),
    )

    assert fields["promotion_gate_pass"] is False
    assert "positive_split_fail" in fields["promotion_reason"]


def test_volume_event_promotion_requires_pit_membership() -> None:
    fields = _promotion_fields(
        [
            {"name": "train_2023_2024", "total_return": 0.10, "max_drawdown": -0.10, "sharpe_like": 1.0},
            {"name": "validation_2024_2025", "total_return": 0.08, "max_drawdown": -0.12, "sharpe_like": 0.8},
            {"name": "oos_2025_2026", "total_return": 0.03, "max_drawdown": -0.15, "sharpe_like": 0.7},
        ],
        config=VolumeEventResearchConfig(),
        pit_membership_pass=False,
    )

    assert fields["pre_pit_gate_pass"] is True
    assert fields["promotion_gate_pass"] is False
    assert "pit_membership_fail" in fields["promotion_reason"]
    assert "full_pit_universe_fail" in fields["promotion_reason"]


def test_volume_event_promotion_requires_full_pit_universe() -> None:
    fields = _promotion_fields(
        [
            {"name": "train_2023_2024", "total_return": 0.10, "max_drawdown": -0.10, "sharpe_like": 1.0},
            {"name": "validation_2024_2025", "total_return": 0.08, "max_drawdown": -0.12, "sharpe_like": 0.8},
            {"name": "oos_2025_2026", "total_return": 0.03, "max_drawdown": -0.15, "sharpe_like": 0.7},
        ],
        config=VolumeEventResearchConfig(),
        pit_membership_pass=True,
        full_pit_universe_pass=False,
    )

    assert fields["pre_pit_gate_pass"] is True
    assert fields["pit_membership_pass"] is True
    assert fields["promotion_gate_pass"] is False
    assert fields["promotion_reason"] == "full_pit_universe_fail"


def test_volume_event_promotion_uses_documented_drawdown_gate() -> None:
    fields = _promotion_fields(
        [
            {"name": "train_2023_2024", "total_return": 0.10, "max_drawdown": -0.24, "sharpe_like": 1.0},
            {"name": "validation_2024_2025", "total_return": 0.08, "max_drawdown": -0.26, "sharpe_like": 0.8},
            {"name": "oos_2025_2026", "total_return": 0.03, "max_drawdown": -0.15, "sharpe_like": 0.7},
        ],
        config=VolumeEventResearchConfig(),
        pit_membership_pass=True,
        full_pit_universe_pass=True,
    )

    assert fields["promotion_gate_pass"] is False
    assert "drawdown_fail" in fields["promotion_reason"]


def test_attach_event_archive_membership_flags_symbol_dates() -> None:
    features = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "ts_ms": 1_704_067_200_000},
            {"symbol": "BBBUSDT", "ts_ms": 1_704_067_200_000},
        ]
    )
    manifest = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "date": "2024-01-01",
                "url": "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2024-01-01.csv.gz",
            }
        ]
    )

    joined = _attach_event_archive_membership(features, manifest).sort("symbol")

    assert joined.select(["symbol", "date", "tradable_membership_flag"]).to_dicts() == [
        {"symbol": "AAAUSDT", "date": "2024-01-01", "tradable_membership_flag": True},
        {"symbol": "BBBUSDT", "date": "2024-01-01", "tradable_membership_flag": False},
    ]


def test_event_decay_exit_fires_at_scenario_threshold() -> None:
    assert _event_decay_exit_hit(
        symbol="AAAUSDT",
        bar_end_ts_ms=2,
        rank_lookup={("AAAUSDT", 2): 0.79},
        threshold=0.80,
    )
    assert not _event_decay_exit_hit(
        symbol="AAAUSDT",
        bar_end_ts_ms=2,
        rank_lookup={("AAAUSDT", 2): 0.80},
        threshold=0.80,
    )


def test_execution_order_prioritizes_strongest_same_timestamp_events() -> None:
    events = pl.DataFrame(
        [
            {"ts_ms": 1, "symbol": "AAAUSDT", "event_rank": 3},
            {"ts_ms": 1, "symbol": "BBBUSDT", "event_rank": 1},
            {"ts_ms": 1, "symbol": "CCCUSDT", "event_rank": 2},
            {"ts_ms": 2, "symbol": "DDDUSDT", "event_rank": 1},
        ]
    )

    ordered = _execution_ordered_events(events)

    assert ordered["symbol"].to_list() == ["BBBUSDT", "CCCUSDT", "AAAUSDT", "DDDUSDT"]


def test_stop_pressure_throttle_uses_only_realized_recent_stops() -> None:
    config = VolumeEventResearchConfig(stop_pressure_window_days=7, stop_pressure_stop_count=2)
    signal_ts_ms = 10 * 24 * 60 * 60 * 1000
    recent_stop = signal_ts_ms - 2 * 24 * 60 * 60 * 1000
    old_stop = signal_ts_ms - 9 * 24 * 60 * 60 * 1000
    future_stop = signal_ts_ms + 1

    assert not _stop_pressure_active([recent_stop, old_stop, future_stop], signal_ts_ms=signal_ts_ms, config=config)
    assert _stop_pressure_active([recent_stop, signal_ts_ms, future_stop], signal_ts_ms=signal_ts_ms, config=config)


def test_selloff_exhaustion_side_hypotheses_are_directional() -> None:
    assert _scenario_side("volume_exhaustion", "continuation") == "long"
    assert _scenario_side("volume_exhaustion", "reversal") == "short"
    assert _scenario_side("selloff_exhaustion", "continuation") == "short"
    assert _scenario_side("selloff_exhaustion", "reversal") == "long"


def test_tail_liquidity_jump_requires_pit_membership_flag() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 2.0,
                "dollar_volume_rank_z_rank_frac": 0.95,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 100,
                "prior7_liquidity_rank": 130,
                "turnover_quote": 1_000_000.0,
                "tradable_membership_flag": False,
            },
            {
                "symbol": "BBBUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 2.1,
                "dollar_volume_rank_z_rank_frac": 0.96,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 100,
                "prior7_liquidity_rank": 130,
                "turnover_quote": 1_000_000.0,
                "tradable_membership_flag": True,
            },
        ]
    )

    filtered = _event_filter(
        frame,
        "tail_liquidity_jump",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=VolumeEventResearchConfig(),
    )

    assert filtered["symbol"].to_list() == ["BBBUSDT"]


def test_creative_event_filters_select_distinct_pit_events() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "ABSUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 3.0,
                "volume_change_1d_z_rank_frac": 0.95,
                "prior_volume_change_1d_z_rank_frac": 0.40,
                "prior7_volume_persistence_rank_max": 0.25,
                "prior7_abs_daily_return_mean": 0.010,
                "dollar_volume_rank_z": 1.0,
                "dollar_volume_rank_z_rank_frac": 0.70,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 40,
                "prior7_liquidity_rank": 115,
                "daily_return_1d": 0.006,
                "daily_return_rank_frac": 0.55,
                "turnover_quote": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "DRYUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 2.8,
                "volume_change_1d_z_rank_frac": 0.92,
                "prior_volume_change_1d_z_rank_frac": 0.30,
                "prior7_volume_persistence_rank_max": 0.20,
                "prior7_abs_daily_return_mean": 0.008,
                "dollar_volume_rank_z": 0.8,
                "dollar_volume_rank_z_rank_frac": 0.65,
                "prior7_dollar_volume_rank_z_rank_frac": 0.15,
                "liquidity_rank": 140,
                "prior7_liquidity_rank": 145,
                "daily_return_1d": 0.025,
                "daily_return_rank_frac": 0.70,
                "turnover_quote": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "MIGUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 0.5,
                "volume_change_1d_z_rank_frac": 0.55,
                "prior_volume_change_1d_z_rank_frac": 0.45,
                "prior7_volume_persistence_rank_max": 0.80,
                "prior7_abs_daily_return_mean": 0.040,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.97,
                "prior7_dollar_volume_rank_z_rank_frac": 0.10,
                "liquidity_rank": 35,
                "prior7_liquidity_rank": 100,
                "daily_return_1d": 0.018,
                "daily_return_rank_frac": 0.62,
                "turnover_quote": 2_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "SELLUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 3.4,
                "volume_change_1d_z_rank_frac": 0.99,
                "prior_volume_change_1d_z_rank_frac": 0.35,
                "prior7_volume_persistence_rank_max": 0.60,
                "prior7_abs_daily_return_mean": 0.030,
                "dollar_volume_rank_z": 1.2,
                "dollar_volume_rank_z_rank_frac": 0.75,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 90,
                "prior7_liquidity_rank": 95,
                "daily_return_1d": -0.070,
                "daily_return_rank_frac": 0.03,
                "turnover_quote": 1_500_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "NOISEUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 3.2,
                "volume_change_1d_z_rank_frac": 0.96,
                "prior_volume_change_1d_z_rank_frac": 0.91,
                "prior7_volume_persistence_rank_max": 0.85,
                "prior7_abs_daily_return_mean": 0.070,
                "dollar_volume_rank_z": 2.8,
                "dollar_volume_rank_z_rank_frac": 0.96,
                "prior7_dollar_volume_rank_z_rank_frac": 0.95,
                "liquidity_rank": 120,
                "prior7_liquidity_rank": 130,
                "daily_return_1d": 0.055,
                "daily_return_rank_frac": 0.98,
                "turnover_quote": 1_500_000.0,
                "tradable_membership_flag": True,
            },
        ]
    )

    config = _migration_unit_config(
        liquidity_migration_rank_improvement_min=50,
        liquidity_migration_turnover_ratio_min=0.0,
        liquidity_migration_event_rank_fraction_max=0.0,
        liquidity_migration_residual_return_min=-10.0,
        liquidity_migration_market_pct_up_max=1.0,
        liquidity_migration_hot_market_day_return_min=10.0,
        absorption_max_abs_day_return=0.015,
        dryup_prior_volume_rank_max=0.35,
        dryup_prior_abs_day_return_max=0.02,
        selloff_exhaustion_min_abs_day_return=0.05,
    )

    absorption = _event_filter(
        frame,
        "volume_absorption",
        score_col="volume_change_1d_z",
        rank_col="volume_change_1d_z_rank_frac",
        top_cut=0.80,
        config=config,
    )
    dryup = _event_filter(
        frame,
        "dryup_reacceleration",
        score_col="volume_change_1d_z",
        rank_col="volume_change_1d_z_rank_frac",
        top_cut=0.80,
        config=config,
    )
    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=config,
    )
    selloff = _event_filter(
        frame,
        "selloff_exhaustion",
        score_col="volume_change_1d_z",
        rank_col="volume_change_1d_z_rank_frac",
        top_cut=0.80,
        config=config,
    )

    assert absorption["symbol"].to_list() == ["ABSUSDT"]
    assert dryup["symbol"].to_list() == ["ABSUSDT", "DRYUSDT"]
    assert migration["symbol"].to_list() == ["MIGUSDT"]
    assert selloff["symbol"].to_list() == ["SELLUSDT"]


def test_liquidity_migration_quality_controls_require_rank_and_turnover_expansion() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "GOODUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.95,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 50,
                "prior7_liquidity_rank": 220,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "NOGROWTHUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.96,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 45,
                "prior7_liquidity_rank": 210,
                "turnover_quote": 1_500_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "ALREADYLIQUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.2,
                "dollar_volume_rank_z_rank_frac": 0.97,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 30,
                "prior7_liquidity_rank": 110,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "NOTCURRENTUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.3,
                "dollar_volume_rank_z_rank_frac": 0.98,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 90,
                "prior7_liquidity_rank": 260,
                "turnover_quote": 4_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=_migration_unit_config(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.0,
            liquidity_migration_prior_rank_min=150,
            liquidity_migration_current_rank_max=80,
            liquidity_migration_day_return_min=-1.0,
            liquidity_migration_residual_return_min=-10.0,
            liquidity_migration_market_pct_up_max=1.0,
            liquidity_migration_hot_market_day_return_min=10.0,
        ),
    )

    assert migration["symbol"].to_list() == ["GOODUSDT"]


def test_liquidity_migration_can_require_positive_event_day_return() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "GOODUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.86,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 80,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.22,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "TOOFLATUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.87,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 75,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.05,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=_migration_unit_config(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.90,
            liquidity_migration_day_return_min=0.20,
            liquidity_migration_residual_return_min=-10.0,
            liquidity_migration_market_pct_up_max=1.0,
            liquidity_migration_hot_market_day_return_min=10.0,
        ),
    )

    assert migration["symbol"].to_list() == ["GOODUSDT"]


def test_liquidity_migration_can_require_idiosyncratic_residual_return() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "IDIOUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.86,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 80,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.14,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "BETAUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.87,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 75,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.14,
                "residual_return_1d": 0.03,
                "market_pct_up_1d": 0.55,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=_migration_unit_config(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.90,
            liquidity_migration_residual_return_min=0.08,
            liquidity_migration_market_pct_up_max=1.0,
            liquidity_migration_hot_market_day_return_min=10.0,
        ),
    )

    assert migration["symbol"].to_list() == ["IDIOUSDT"]


def test_liquidity_migration_can_reject_late_day_turnover_concentration() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "ORDERLYUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.86,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 80,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.14,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "signal_day_last6h_turnover_share": 0.42,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "LATEBLOWOFFUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.87,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 75,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.14,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "signal_day_last6h_turnover_share": 0.82,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=_migration_unit_config(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.90,
            liquidity_migration_residual_return_min=0.08,
            liquidity_migration_market_pct_up_max=1.0,
            liquidity_migration_hot_market_day_return_min=10.0,
            liquidity_migration_signal_last6h_turnover_share_max=0.70,
        ),
    )

    assert migration["symbol"].to_list() == ["ORDERLYUSDT"]


def test_daily_return_frame_adds_quant_state_features() -> None:
    rows = []
    start = 1_704_067_200_000
    for day in range(35):
        day_start = start + day * 24 * 60 * 60 * 1000
        open_price = 100.0 + day
        close_price = open_price * (1.02 if day == 34 else 1.0)
        for hour in range(24):
            rows.append(
                {
                    "symbol": "AAAUSDT",
                    "ts_ms": day_start + hour * 60 * 60 * 1000,
                    "open": open_price,
                    "high": close_price * 1.01,
                    "low": open_price * 0.99,
                    "close": close_price,
                    "turnover_quote": 1_000_000.0,
                    "date": "2024-01-01",
                }
            )
    frame = _daily_return_frame(pl.DataFrame(rows)).sort("ts_ms")
    last = frame.tail(1).to_dicts()[0]

    assert last["return_7d"] > 0.0
    assert last["close_to_high_7d"] <= 0.0
    assert last["close_to_high_30d"] <= 0.0
    assert last["prior30_max_daily_return"] is not None
    assert last["prior7_return_volatility"] is not None
    assert last["intraday_range_1d"] > 0.0


def test_liquidity_migration_quant_state_filters_are_pit_safe() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "GOODUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.86,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 80,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.15,
                "return_7d": 0.45,
                "close_to_high_7d": -0.01,
                "close_to_high_30d": -0.03,
                "prior30_max_daily_return": 0.28,
                "prior7_return_volatility": 0.07,
                "intraday_range_1d": 0.18,
                "residual_return_1d": 0.10,
                "market_pct_up_1d": 0.45,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "WEAKMOMUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.87,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 82,
                "prior7_liquidity_rank": 250,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.15,
                "return_7d": 0.05,
                "close_to_high_7d": -0.01,
                "close_to_high_30d": -0.03,
                "prior30_max_daily_return": 0.28,
                "prior7_return_volatility": 0.07,
                "intraday_range_1d": 0.18,
                "residual_return_1d": 0.10,
                "market_pct_up_1d": 0.45,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "FARFROMHIGHUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.2,
                "dollar_volume_rank_z_rank_frac": 0.88,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 84,
                "prior7_liquidity_rank": 260,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.15,
                "return_7d": 0.45,
                "close_to_high_7d": -0.20,
                "close_to_high_30d": -0.03,
                "prior30_max_daily_return": 0.28,
                "prior7_return_volatility": 0.07,
                "intraday_range_1d": 0.18,
                "residual_return_1d": 0.10,
                "market_pct_up_1d": 0.45,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "NOLOTTERYUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.3,
                "dollar_volume_rank_z_rank_frac": 0.89,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 86,
                "prior7_liquidity_rank": 270,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.15,
                "return_7d": 0.45,
                "close_to_high_7d": -0.01,
                "close_to_high_30d": -0.03,
                "prior30_max_daily_return": 0.03,
                "prior7_return_volatility": 0.07,
                "intraday_range_1d": 0.18,
                "residual_return_1d": 0.10,
                "market_pct_up_1d": 0.45,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=_migration_unit_config(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.90,
            liquidity_migration_day_return_min=0.10,
            liquidity_migration_return_7d_min=0.20,
            liquidity_migration_close_to_high_7d_min=-0.05,
            liquidity_migration_close_to_high_30d_min=-0.10,
            liquidity_migration_prior30_max_return_min=0.20,
            liquidity_migration_prior7_return_volatility_min=0.02,
            liquidity_migration_prior7_return_volatility_max=0.15,
            liquidity_migration_intraday_range_max=0.25,
            liquidity_migration_residual_return_min=0.08,
            liquidity_migration_market_pct_up_max=1.0,
            liquidity_migration_hot_market_day_return_min=10.0,
        ),
    )

    assert migration["symbol"].to_list() == ["GOODUSDT"]


def test_liquidity_migration_can_skip_middle_event_rank_band() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "LOWBANDUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 2.8,
                "dollar_volume_rank_z_rank_frac": 0.74,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 80,
                "prior7_liquidity_rank": 180,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.05,
                "market_pct_up_1d": 0.40,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "MIDBANDUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 2.9,
                "dollar_volume_rank_z_rank_frac": 0.80,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 82,
                "prior7_liquidity_rank": 182,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.05,
                "market_pct_up_1d": 0.40,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "HIGHBANDUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.86,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 84,
                "prior7_liquidity_rank": 184,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.05,
                "market_pct_up_1d": 0.40,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.70,
        config=_migration_unit_config(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_event_rank_fraction_exclude_min=0.75,
            liquidity_migration_event_rank_fraction_exclude_max=0.85,
            liquidity_migration_residual_return_min=-10.0,
        ),
    )

    assert migration["symbol"].to_list() == ["LOWBANDUSDT", "HIGHBANDUSDT"]


def test_liquidity_migration_market_gate_allows_hot_coin_exception() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "COOLMKTUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.86,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 80,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_pct_up_1d": 0.55,
                "daily_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "BLOWOFFUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.87,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 75,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_pct_up_1d": 0.85,
                "daily_return_1d": 0.18,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "HOTFLATUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.2,
                "dollar_volume_rank_z_rank_frac": 0.88,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 70,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_pct_up_1d": 0.85,
                "daily_return_1d": 0.06,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=_migration_unit_config(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.90,
            liquidity_migration_residual_return_min=-10.0,
            liquidity_migration_market_pct_up_max=0.60,
            liquidity_migration_hot_market_day_return_min=0.15,
        ),
    )

    assert migration["symbol"].to_list() == ["COOLMKTUSDT", "BLOWOFFUSDT"]


def test_liquidity_migration_overheated_and_regime_filters_are_pit_safe() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "GOODUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 1.8,
                "dollar_volume_rank_z_rank_frac": 0.88,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 70,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_median_return_1d": 0.01,
                "market_pct_up_1d": 0.55,
                "btc_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "TOORANKEDUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 1.7,
                "dollar_volume_rank_z_rank_frac": 0.96,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 65,
                "prior7_liquidity_rank": 230,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_median_return_1d": 0.01,
                "market_pct_up_1d": 0.55,
                "btc_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "TOOSCOREDUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 2.7,
                "dollar_volume_rank_z_rank_frac": 0.88,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 60,
                "prior7_liquidity_rank": 230,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_median_return_1d": 0.01,
                "market_pct_up_1d": 0.55,
                "btc_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "TOOHOTUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 1.8,
                "dollar_volume_rank_z_rank_frac": 0.88,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 55,
                "prior7_liquidity_rank": 230,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_median_return_1d": 0.04,
                "market_pct_up_1d": 0.55,
                "btc_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=_migration_unit_config(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.90,
            liquidity_migration_score_max=2.0,
            liquidity_migration_day_return_min=-1.0,
            liquidity_migration_residual_return_min=-10.0,
            liquidity_migration_market_pct_up_max=1.0,
            liquidity_migration_hot_market_day_return_min=10.0,
            market_median_return_1d_max=0.03,
            market_pct_up_1d_max=0.70,
            btc_return_1d_max=0.05,
        ),
    )

    assert migration["symbol"].to_list() == ["GOODUSDT"]


def test_monthly_returns_are_written_from_baskets() -> None:
    baskets = pl.DataFrame(
        [
            {
                "exit_ts_ms": 1_704_067_200_000,
                "basket_return": 0.10,
                "long_return": 0.10,
                "short_return": 0.0,
                "cost_return": -0.001,
                "funding_return": 0.0,
                "trades": 1,
            },
            {
                "exit_ts_ms": 1_704_153_600_000,
                "basket_return": -0.05,
                "long_return": -0.05,
                "short_return": 0.0,
                "cost_return": -0.001,
                "funding_return": 0.0,
                "trades": 2,
            },
        ]
    )

    monthly = _monthly_returns(baskets)

    assert monthly["month"].to_list() == ["2024-01"]
    assert monthly["strategy_return"].to_list()[0] == pytest.approx(0.045)
    assert monthly["trades"].to_list()[0] == 3
