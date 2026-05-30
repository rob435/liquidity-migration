from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from liquidity_migration.config import TradeLifecycleConfig
from liquidity_migration.ingestion import generate_fixture_data
from liquidity_migration.trade_lifecycle import _funding_lookup, _perp_funding_return
from liquidity_migration.volume_events import (
    ENTRY_POLICY_FIXED_DELAY,
    POSITION_WEIGHTINGS,
    _PositionSizer,
    _add_liquidity_migration_speed_features,
    _apply_entry_execution_veto,
    _clamp_position_weight,
    _position_sizing_quantity,
    _attach_event_archive_membership,
    _apply_liquidity_migration_crowding_filter,
    _basis_feature_frame,
    _daily_return_frame,
    _enriched_event_features,
    _entry_decision_for_event,
    _event_decay_exit_hit,
    _event_filter,
    _execution_ordered_events,
    _explain_liquidity_migration_rejections,
    _float_or_nan,
    _full_pit_universe_error,
    _full_pit_universe_pass,
    _required_pit_date_symbols,
    _funding_feature_frame,
    _open_interest_feature_frame,
    _signed_flow_feature_frame,
    _simulate_indexed_trade,
    _stop_pressure_active,
    EventScenario,
    VolumeEventResearchConfig,
    _add_rank_fraction,
    _scenario_hold_ms,
    _monthly_returns,
    _promotion_fields,
    _scenario_side,
    _validate_event_config,
    _write_equity_benchmark_chart,
    run_volume_event_research,
)


def _make_symbol_bars(bars: list[dict[str, Any]], *, hour_ms: int = 60 * 60 * 1000) -> dict[str, Any]:
    # Build the indexed-bars layout (numpy arrays + ts -> idx) from a list of
    # bar dicts. Test helper that mirrors what _indexed_price_bars_by_symbol
    # produces for live data.
    ends = [int(bar["bar_end_ts_ms"]) for bar in bars]
    return {
        "ts_ms": np.array([int(bar.get("ts_ms", end - hour_ms)) for bar, end in zip(bars, ends)], dtype=np.int64),
        "bar_end_ts_ms": np.array(ends, dtype=np.int64),
        "open": np.array([float(bar.get("open", bar.get("close", 0.0))) for bar in bars], dtype=np.float64),
        "high": np.array([float(bar.get("high", 0.0)) for bar in bars], dtype=np.float64),
        "low": np.array([float(bar.get("low", 0.0)) for bar in bars], dtype=np.float64),
        "close": np.array([float(bar.get("close", 0.0)) for bar in bars], dtype=np.float64),
        "ends": ends,
        "by_end": {end: idx for idx, end in enumerate(ends)},
    }


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


def test_enriched_event_features_adds_causal_research_columns() -> None:
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


def test_liquidity_rank_improvement_handles_rank_deterioration_without_u32_underflow() -> None:
    # Regression test for the u32-underflow bug surfaced 2026-05-26 reconciling
    # backtest vs. demo. `liquidity_rank` and the `priorN_liquidity_rank`
    # columns come from `volume_features.build_volume_features` as u32. When
    # a symbol's rank gets WORSE in the lookback window (current_rank >
    # prior_rank), the unsigned `(prior - current)` subtraction wraps to a
    # huge value near 2^32 instead of producing a negative delta. That made
    # `improvement >= rank_improvement_min` falsely True for ranks that
    # actually deteriorated — observed on WAVESUSDT prior7=111, current=201,
    # raw subtraction = 4294967206 ≈ 2^32 - 90.
    #
    # _add_liquidity_migration_speed_features now casts to Int64 first so the
    # delta carries its sign. Same Int64 cast lives inside
    # _filter_liquidity_migration's predicate at the strategy filter site.
    rows = pl.DataFrame({
        "liquidity_rank": pl.Series([201, 47, 100], dtype=pl.UInt32),
        "prior1_liquidity_rank": pl.Series([180, 70, 100], dtype=pl.UInt32),
        "prior3_liquidity_rank": pl.Series([150, 90, 110], dtype=pl.UInt32),
        "prior7_liquidity_rank": pl.Series([111, 135, 120], dtype=pl.UInt32),
    })

    result = _add_liquidity_migration_speed_features(rows)
    improvements_1d = result["liquidity_rank_improvement_1d"].to_list()
    improvements_3d = result["liquidity_rank_improvement_3d"].to_list()
    improvements_7d = result["liquidity_rank_improvement_7d"].to_list()

    # Row 0: rank deteriorated everywhere. All improvements must be negative.
    assert improvements_1d[0] == 180 - 201 == -21
    assert improvements_3d[0] == 150 - 201 == -51
    assert improvements_7d[0] == 111 - 201 == -90  # the WAVESUSDT-like case
    # Row 1: rank improved across all windows. Positive deltas.
    assert improvements_1d[1] == 70 - 47 == 23
    assert improvements_3d[1] == 90 - 47 == 43
    assert improvements_7d[1] == 135 - 47 == 88
    # Row 2: mixed. Negative on prior1 (100→100=0), positive elsewhere.
    assert improvements_1d[2] == 0
    assert improvements_3d[2] == 110 - 100 == 10
    assert improvements_7d[2] == 120 - 100 == 20


def test_explain_liquidity_migration_rejections_labels_first_failing_gate() -> None:
    """Per-row gate-rejection trace: each row gets the FIRST gate it fails,
    in the same order _filter_liquidity_migration evaluates them.

    Three symbols, each engineered to fail one specific gate:
    - PASS:  satisfies every gate (first_failing_gate = "")
    - RANK:  liquidity_rank_improvement = 100 < 150 → fails rank_improvement_min
    - RESID: residual_return_1d = 0.02 < 0.08 → fails residual_return_min
    """
    base = pl.DataFrame({
        "symbol": ["PASS", "RANK", "RESID"],
        "ts_ms": [1_700_000_000_000] * 3,
        "liquidity_rank": pl.Series([50, 50, 50], dtype=pl.UInt32),
        "prior7_liquidity_rank": pl.Series([220, 150, 220], dtype=pl.UInt32),
        "dollar_volume_rank_z_rank_frac": [0.85, 0.85, 0.85],
        "prior7_dollar_volume_rank_z_rank_frac": [0.10, 0.10, 0.10],
        "turnover_quote": [1.0e9, 1.0e9, 1.0e9],
        "prior7_turnover_quote_mean": [1.0e8, 1.0e8, 1.0e8],
        "daily_return_1d": [0.10, 0.10, 0.10],
        "residual_return_1d": [0.15, 0.15, 0.02],
        "signal_day_close_location": [0.50, 0.50, 0.50],
        "pit_age_days": [200.0, 200.0, 200.0],
        "market_pct_up_1d": [0.40, 0.40, 0.40],
    })
    config = VolumeEventResearchConfig(
        liquidity_migration_rank_improvement_min=150,
        liquidity_migration_turnover_ratio_min=6.0,
        liquidity_migration_event_rank_fraction_max=0.90,
        liquidity_migration_day_return_min=0.0,
        liquidity_migration_residual_return_min=0.08,
        liquidity_migration_close_location_min=0.30,
        liquidity_migration_pit_age_days_min=90,
    )
    out = _explain_liquidity_migration_rejections(
        base,
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.60,  # threshold 0.4 → top_cut = 1 - 0.4
        config=config,
    )
    by_symbol = {row["symbol"]: row for row in out.to_dicts()}
    assert by_symbol["PASS"]["first_failing_gate"] == ""
    assert by_symbol["RANK"]["first_failing_gate"] == "rank_improvement_min"
    assert by_symbol["RANK"]["first_failing_value"] == 100.0  # 150 - 50
    assert by_symbol["RANK"]["first_failing_threshold"] == 150.0
    assert by_symbol["RESID"]["first_failing_gate"] == "residual_return_min"
    assert by_symbol["RESID"]["first_failing_value"] == pytest.approx(0.02)
    assert by_symbol["RESID"]["first_failing_threshold"] == pytest.approx(0.08)


def test_explain_liquidity_migration_rejections_empty_input_returns_empty_frame() -> None:
    """Edge case: empty input returns an empty result, not an error."""
    base = pl.DataFrame({"symbol": [], "ts_ms": [], "liquidity_rank": [],
                         "prior7_liquidity_rank": [], "dollar_volume_rank_z_rank_frac": [],
                         "prior7_dollar_volume_rank_z_rank_frac": []})
    out = _explain_liquidity_migration_rejections(
        base, score_col="dollar_volume_rank_z", rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.6, config=VolumeEventResearchConfig(),
    )
    assert out.height == 0
    assert "first_failing_gate" in out.columns


def test_filter_liquidity_migration_rejects_negative_rank_delta_after_u32_fix() -> None:
    # Companion to the speed-features test: the filter predicate's own
    # subtraction must also be sign-aware. Without the Int64 cast the
    # 4294967206 wrap value sails through `>= 150` and a rank-deteriorating
    # symbol falsely passes the migration filter.
    from dataclasses import replace as dc_replace
    base = pl.DataFrame({
        "symbol": ["BAD", "GOOD"],
        "date": ["2026-05-26", "2026-05-26"],
        "ts_ms": [1779753600000, 1779753600000],
        "liquidity_rank": pl.Series([201, 30], dtype=pl.UInt32),
        "prior7_liquidity_rank": pl.Series([111, 250], dtype=pl.UInt32),
        # Score gates / rank gates that the predicate also checks:
        "dollar_volume_rank_z": [2.0, 2.0],
        "dollar_volume_rank_z_rank_frac": [0.9, 0.9],
        "prior7_dollar_volume_rank_z_rank_frac": [0.1, 0.1],
        "tradable_membership_flag": [True, True],
        "turnover_quote": [1.0e9, 1.0e9],
        "market_pct_up_1d": [0.50, 0.50],
        "market_median_return_30d_sum": [0.0, 0.0],
        "market_median_return_7d_sum": [0.0, 0.0],
        "market_pct_up_30d_mean": [0.5, 0.5],
        "market_pct_up_7d_mean": [0.5, 0.5],
    })
    # Disable every additional liquidity_migration_* band the strategy might
    # check so the test isolates the rank-delta predicate.
    config = dc_replace(
        VolumeEventResearchConfig(),
        liquidity_migration_rank_improvement_min=150,
        liquidity_migration_turnover_ratio_min=0.0,
        liquidity_migration_current_rank_max=0,
        liquidity_migration_event_rank_fraction_max=0.0,
        liquidity_migration_event_rank_fraction_exclude_min=0.0,
        liquidity_migration_event_rank_fraction_exclude_max=0.0,
        liquidity_migration_score_max=0.0,
        liquidity_migration_day_return_min=-10.0,
        liquidity_migration_day_return_max=10.0,
        liquidity_migration_return_7d_min=-10.0,
        liquidity_migration_return_7d_max=10.0,
        liquidity_migration_residual_return_min=-10.0,
        liquidity_migration_residual_return_max=10.0,
        liquidity_migration_close_to_high_7d_min=-10.0,
        liquidity_migration_close_to_high_30d_min=-10.0,
        liquidity_migration_prior30_max_return_min=-10.0,
        liquidity_migration_prior30_max_return_max=10.0,
        liquidity_migration_prior7_return_volatility_min=0.0,
        liquidity_migration_prior7_return_volatility_max=10.0,
        liquidity_migration_intraday_range_max=10.0,
        liquidity_migration_funding_rate_last_min=-10.0,
        liquidity_migration_funding_rate_last_max=10.0,
        liquidity_migration_funding_3d_sum_min=-10.0,
        liquidity_migration_funding_3d_sum_max=10.0,
        liquidity_migration_funding_7d_sum_min=-10.0,
        liquidity_migration_funding_7d_sum_max=10.0,
        liquidity_migration_open_interest_return_3d_min=-10.0,
        liquidity_migration_open_interest_return_3d_max=10.0,
        liquidity_migration_open_interest_return_7d_min=-10.0,
        liquidity_migration_open_interest_return_7d_max=10.0,
        liquidity_migration_volume_to_oi_quote_min=0.0,
        liquidity_migration_volume_to_oi_quote_max=0.0,
        liquidity_migration_mark_index_basis_3d_mean_min=-10.0,
        liquidity_migration_mark_index_basis_3d_mean_max=10.0,
        liquidity_migration_premium_index_3d_mean_min=-10.0,
        liquidity_migration_premium_index_3d_mean_max=10.0,
        liquidity_migration_taker_imbalance_1d_min=-1.0,
        liquidity_migration_taker_imbalance_1d_max=1.0,
        liquidity_migration_taker_imbalance_3d_min=-1.0,
        liquidity_migration_taker_imbalance_3d_max=1.0,
        liquidity_migration_market_pct_up_max=1.0,
        liquidity_migration_market_median_return_30d_max=10.0,
        liquidity_migration_market_median_return_7d_max=10.0,
        liquidity_migration_market_pct_up_30d_max=1.0,
        liquidity_migration_market_pct_up_7d_max=1.0,
        liquidity_migration_close_location_min=0.0,
        liquidity_migration_close_location_max=1.0,
        liquidity_migration_signal_last6h_turnover_share_max=1.0,
        liquidity_migration_up_volume_concentration_min=0.0,
        liquidity_migration_pit_age_days_min=0,
        liquidity_migration_pit_age_days_max=0,
        liquidity_migration_prior_rank_min=0,
        require_pit_membership=False,
        universe_rank_min=1,
        universe_rank_max=400,
        universe_min_daily_turnover=0.0,
    )
    result = _event_filter(
        base,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.60,
        config=config,
    )
    survivors = result["symbol"].to_list()
    # BAD (prior7=111, current=201) has delta=-90 (rank got worse), must NOT
    # pass the >= 150 improvement gate. GOOD (prior7=250, current=30) has
    # delta=220, must pass.
    assert survivors == ["GOOD"], f"u32-underflow regression: {survivors}"


def test_filter_liquidity_migration_residual_momentum_gate_keeps_low_rmom() -> None:
    """P3 residual-momentum SELECTION gate: with --liquidity-migration-residual-momentum-max active,
    only candidates whose trailing factor-residual momentum is <= the threshold survive (short the
    idiosyncratically-weak names). High-rmom candidates AND candidates with no signal (null) are
    dropped. With the default (10.0 = inactive) the gate is a no-op and needs no signal column."""
    from dataclasses import replace as dc_replace
    base = pl.DataFrame({
        "symbol": ["LOW", "HIGH", "NULLSIG"],
        "date": ["2026-05-26"] * 3,
        "ts_ms": [1779753600000] * 3,
        # all three are large climbers (delta = 250-40 = 210 >= 150) so they pass the rank gate;
        # only residual_momentum differs.
        "liquidity_rank": pl.Series([40, 40, 40], dtype=pl.UInt32),
        "prior7_liquidity_rank": pl.Series([250, 250, 250], dtype=pl.UInt32),
        "dollar_volume_rank_z": [2.0] * 3,
        "dollar_volume_rank_z_rank_frac": [0.9] * 3,
        "prior7_dollar_volume_rank_z_rank_frac": [0.1] * 3,
        "tradable_membership_flag": [True] * 3,
        "turnover_quote": [1.0e9] * 3,
        "residual_momentum": [-0.05, 0.05, None],
    })
    config_on = dc_replace(
        _isolated_liquidity_migration_config(), liquidity_migration_residual_momentum_max=0.0,
    )
    result = _event_filter(
        base, "liquidity_migration", score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac", top_cut=0.60, config=config_on,
    )
    assert result["symbol"].to_list() == ["LOW"], result["symbol"].to_list()

    # Default (10.0) = inactive: no residual_momentum column required, all three survive.
    config_off = _isolated_liquidity_migration_config()
    result_off = _event_filter(
        base.drop("residual_momentum"), "liquidity_migration", score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac", top_cut=0.60, config=config_off,
    )
    assert set(result_off["symbol"].to_list()) == {"LOW", "HIGH", "NULLSIG"}, result_off["symbol"].to_list()


def _isolated_liquidity_migration_config(
    *,
    rank_improvement_min: int = 150,
    rank_direction: str = "improvement",
) -> VolumeEventResearchConfig:
    """Build a config that isolates the rank-delta predicate by disabling every
    additional liquidity_migration_* band. Used by the direction-flag tests so
    only the direction/threshold combination affects survival."""
    from dataclasses import replace as dc_replace
    return dc_replace(
        VolumeEventResearchConfig(),
        liquidity_migration_rank_improvement_min=rank_improvement_min,
        liquidity_migration_rank_direction=rank_direction,
        liquidity_migration_turnover_ratio_min=0.0,
        liquidity_migration_current_rank_max=0,
        liquidity_migration_event_rank_fraction_max=0.0,
        liquidity_migration_event_rank_fraction_exclude_min=0.0,
        liquidity_migration_event_rank_fraction_exclude_max=0.0,
        liquidity_migration_score_max=0.0,
        liquidity_migration_day_return_min=-10.0,
        liquidity_migration_day_return_max=10.0,
        liquidity_migration_return_7d_min=-10.0,
        liquidity_migration_return_7d_max=10.0,
        liquidity_migration_residual_return_min=-10.0,
        liquidity_migration_residual_return_max=10.0,
        liquidity_migration_close_to_high_7d_min=-10.0,
        liquidity_migration_close_to_high_30d_min=-10.0,
        liquidity_migration_prior30_max_return_min=-10.0,
        liquidity_migration_prior30_max_return_max=10.0,
        liquidity_migration_prior7_return_volatility_min=0.0,
        liquidity_migration_prior7_return_volatility_max=10.0,
        liquidity_migration_intraday_range_max=10.0,
        liquidity_migration_funding_rate_last_min=-10.0,
        liquidity_migration_funding_rate_last_max=10.0,
        liquidity_migration_funding_3d_sum_min=-10.0,
        liquidity_migration_funding_3d_sum_max=10.0,
        liquidity_migration_funding_7d_sum_min=-10.0,
        liquidity_migration_funding_7d_sum_max=10.0,
        liquidity_migration_open_interest_return_3d_min=-10.0,
        liquidity_migration_open_interest_return_3d_max=10.0,
        liquidity_migration_open_interest_return_7d_min=-10.0,
        liquidity_migration_open_interest_return_7d_max=10.0,
        liquidity_migration_volume_to_oi_quote_min=0.0,
        liquidity_migration_volume_to_oi_quote_max=0.0,
        liquidity_migration_mark_index_basis_3d_mean_min=-10.0,
        liquidity_migration_mark_index_basis_3d_mean_max=10.0,
        liquidity_migration_premium_index_3d_mean_min=-10.0,
        liquidity_migration_premium_index_3d_mean_max=10.0,
        liquidity_migration_taker_imbalance_1d_min=-1.0,
        liquidity_migration_taker_imbalance_1d_max=1.0,
        liquidity_migration_taker_imbalance_3d_min=-1.0,
        liquidity_migration_taker_imbalance_3d_max=1.0,
        liquidity_migration_market_pct_up_max=1.0,
        liquidity_migration_market_median_return_30d_max=10.0,
        liquidity_migration_market_median_return_7d_max=10.0,
        liquidity_migration_market_pct_up_30d_max=1.0,
        liquidity_migration_market_pct_up_7d_max=1.0,
        liquidity_migration_close_location_min=0.0,
        liquidity_migration_close_location_max=1.0,
        liquidity_migration_signal_last6h_turnover_share_max=1.0,
        liquidity_migration_up_volume_concentration_min=0.0,
        liquidity_migration_pit_age_days_min=0,
        liquidity_migration_pit_age_days_max=0,
        liquidity_migration_prior_rank_min=0,
        require_pit_membership=False,
        universe_rank_min=1,
        universe_rank_max=400,
        universe_min_daily_turnover=0.0,
    )


def _direction_fixture_base() -> pl.DataFrame:
    """4-symbol fixture exercising the four delta corners around threshold 150:
        CLIMB_BIG  : prior=250 -> 30   delta=+220  (large improvement)
        CLIMB_SMALL: prior=110 -> 60   delta= +50  (small improvement)
        DRAIN_SMALL: prior=60  -> 110  delta= -50  (small deterioration)
        DRAIN_BIG  : prior=30  -> 250  delta=-220  (large deterioration)
    All other gate columns are set to values that pass the rank gates
    independently (high score rank, low prior-event-rank, high turnover, etc.)
    so only the rank_delta predicate determines survival."""
    return pl.DataFrame({
        "symbol": ["CLIMB_BIG", "CLIMB_SMALL", "DRAIN_SMALL", "DRAIN_BIG"],
        "date": ["2026-05-26"] * 4,
        "ts_ms": [1779753600000] * 4,
        "liquidity_rank": pl.Series([30, 60, 110, 250], dtype=pl.UInt32),
        "prior7_liquidity_rank": pl.Series([250, 110, 60, 30], dtype=pl.UInt32),
        "dollar_volume_rank_z": [2.0] * 4,
        "dollar_volume_rank_z_rank_frac": [0.9] * 4,
        "prior7_dollar_volume_rank_z_rank_frac": [0.1] * 4,
        "tradable_membership_flag": [True] * 4,
        "turnover_quote": [1.0e9] * 4,
        "market_pct_up_1d": [0.50] * 4,
        "market_median_return_30d_sum": [0.0] * 4,
        "market_median_return_7d_sum": [0.0] * 4,
        "market_pct_up_30d_mean": [0.5] * 4,
        "market_pct_up_7d_mean": [0.5] * 4,
    })


def test_filter_liquidity_migration_direction_improvement_admits_climbers_only() -> None:
    """Default direction=improvement: only deltas >= +150 pass."""
    result = _event_filter(
        _direction_fixture_base(),
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.60,
        config=_isolated_liquidity_migration_config(rank_direction="improvement"),
    )
    assert sorted(result["symbol"].to_list()) == ["CLIMB_BIG"]


def test_filter_liquidity_migration_direction_deterioration_admits_drainers_only() -> None:
    """direction=deterioration: only deltas <= -150 pass. This is the
    Phase 2 H2 cell — rapid rank deterioration as a tradable short signal."""
    result = _event_filter(
        _direction_fixture_base(),
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.60,
        config=_isolated_liquidity_migration_config(rank_direction="deterioration"),
    )
    assert sorted(result["symbol"].to_list()) == ["DRAIN_BIG"]


def test_filter_liquidity_migration_direction_both_admits_either_extreme() -> None:
    """direction=both: |delta| >= 150 — the Phase 2 H3 two-sided event."""
    result = _event_filter(
        _direction_fixture_base(),
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.60,
        config=_isolated_liquidity_migration_config(rank_direction="both"),
    )
    assert sorted(result["symbol"].to_list()) == ["CLIMB_BIG", "DRAIN_BIG"]


def test_explain_liquidity_migration_rejections_gate_name_reflects_direction() -> None:
    """When the strategy switches direction, the rejection trace must report
    the gate name and value of the active constraint — otherwise a Phase 2
    deterioration cell trace would falsely cite 'rank_improvement_min'."""
    base = _direction_fixture_base().head(2)  # CLIMB_BIG (+220), CLIMB_SMALL (+50)

    out_impr = _explain_liquidity_migration_rejections(
        base,
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.60,
        config=_isolated_liquidity_migration_config(rank_direction="improvement"),
    )
    by_impr = {row["symbol"]: row for row in out_impr.to_dicts()}
    assert by_impr["CLIMB_BIG"]["first_failing_gate"] == ""
    assert by_impr["CLIMB_SMALL"]["first_failing_gate"] == "rank_improvement_min"
    assert by_impr["CLIMB_SMALL"]["first_failing_value"] == 50.0
    assert by_impr["CLIMB_SMALL"]["first_failing_threshold"] == 150.0

    out_det = _explain_liquidity_migration_rejections(
        base,
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.60,
        config=_isolated_liquidity_migration_config(rank_direction="deterioration"),
    )
    by_det = {row["symbol"]: row for row in out_det.to_dicts()}
    # Both CLIMB_BIG and CLIMB_SMALL FAIL deterioration; the gate label must
    # be 'rank_deterioration_min' and the reported value is -delta so the
    # threshold comparison reads consistently (value < threshold => fail).
    assert by_det["CLIMB_BIG"]["first_failing_gate"] == "rank_deterioration_min"
    assert by_det["CLIMB_BIG"]["first_failing_value"] == -220.0
    assert by_det["CLIMB_BIG"]["first_failing_threshold"] == 150.0

    out_both = _explain_liquidity_migration_rejections(
        base,
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.60,
        config=_isolated_liquidity_migration_config(rank_direction="both"),
    )
    by_both = {row["symbol"]: row for row in out_both.to_dicts()}
    assert by_both["CLIMB_BIG"]["first_failing_gate"] == ""
    assert by_both["CLIMB_SMALL"]["first_failing_gate"] == "rank_abs_delta_min"
    assert by_both["CLIMB_SMALL"]["first_failing_value"] == 50.0  # |delta|
    assert by_both["CLIMB_SMALL"]["first_failing_threshold"] == 150.0


def test_validate_event_config_rejects_unknown_rank_direction() -> None:
    """Pre-registered values are improvement|deterioration|both. Anything else
    is a typo / silent misconfiguration and must error at config-build time."""
    from dataclasses import replace as dc_replace
    bad = dc_replace(VolumeEventResearchConfig(), liquidity_migration_rank_direction="upward")
    with pytest.raises(ValueError, match="liquidity_migration_rank_direction"):
        _validate_event_config(bad)


def test_bar_extreme_stop_fill_uses_adverse_hourly_extreme_for_short() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = _make_symbol_bars(
        [
            {"bar_end_ts_ms": hour, "high": 101.0, "low": 99.0, "close": 100.0},
            {"bar_end_ts_ms": 2 * hour, "high": 130.0, "low": 95.0, "close": 105.0},
        ]
    )
    base_kwargs = {
        "symbol": "AUSDT",
        "side": "short",
        "score": 1.0,
        "rank": 1,
        "basket_id": "basket",
        "signal_ts_ms": 0,
        "entry_bar": 0,
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


def test_failed_fade_exit_cuts_short_after_unresolved_continuation() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = _make_symbol_bars(
        [
            {"bar_end_ts_ms": hour, "high": 101.0, "low": 99.0, "close": 100.0},
            {"bar_end_ts_ms": 2 * hour, "high": 101.2, "low": 99.7, "close": 101.0},
            {"bar_end_ts_ms": 3 * hour, "high": 103.0, "low": 100.5, "close": 102.8},
            {"bar_end_ts_ms": 4 * hour, "high": 104.0, "low": 100.0, "close": 101.0},
        ]
    )

    trade = _simulate_indexed_trade(
        symbol="AUSDT",
        side="short",
        score=1.0,
        rank=1,
        basket_id="basket",
        signal_ts_ms=0,
        entry_bar=0,
        symbol_bars=symbol_bars,
        planned_exit_ts_ms=4 * hour,
        notional_weight=1.0,
        config=TradeLifecycleConfig(
            take_profit_pct=0.0,
            failed_fade_exit_hours=2,
            failed_fade_min_mfe_pct=0.005,
            failed_fade_loss_pct=0.025,
            failed_fade_close_location_min=0.85,
        ),
        round_trip_cost_bps=0.0,
        stop_pct=0.12,
        rank_lookup={},
        event_decay_threshold=-1.0,
        funding_lookup=None,
        stop_fill_mode="stop",
    )

    assert trade is not None
    assert trade["exit_reason"] == "failed_fade"
    assert trade["exit_ts_ms"] == 3 * hour
    assert trade["exit_price"] == pytest.approx(102.8)


def test_promoted_quality_entry_waits_for_completed_giveback() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = _make_symbol_bars(
        [
            {"bar_end_ts_ms": 0, "high": 101.0, "low": 99.0, "close": 100.0},
            {"bar_end_ts_ms": hour, "high": 101.2, "low": 100.0, "close": 101.1},
            {"bar_end_ts_ms": 2 * hour, "high": 101.6, "low": 101.0, "close": 101.5},
            {"bar_end_ts_ms": 3 * hour, "high": 101.6, "low": 100.9, "close": 101.1},
        ]
    )
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
    symbol_bars = _make_symbol_bars(
        [{"bar_end_ts_ms": hour, "high": 101.0, "low": 99.0, "close": 100.0}]
    )

    decision = _entry_decision_for_event(
        {"ts_ms": 0, "symbol": "AAAUSDT"},
        symbol_bars,
        config=VolumeEventResearchConfig(entry_policy=ENTRY_POLICY_FIXED_DELAY),
        score_col="dollar_volume_rank_z",
        now_ms=hour + 1,
    )

    assert decision["entry_policy"] == ENTRY_POLICY_FIXED_DELAY
    assert decision["entry_ts_ms"] == hour
    assert decision["entry_bar"] == 0
    assert symbol_bars["close"][decision["entry_bar"]] == 100.0


def test_entry_execution_veto_skips_high_close_location() -> None:
    symbol_bars = _make_symbol_bars(
        [{"bar_end_ts_ms": 1, "high": 102.0, "low": 100.0, "close": 101.9}]
    )
    decision = {
        "entry_bar": 0,
        "entry_rule": "quality_fixed_delay",
        "pending": False,
    }

    vetoed = _apply_entry_execution_veto(
        decision,
        symbol_bars=symbol_bars,
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
    # Entry precedes the first known funding stamp -> "partial", but the two
    # covered stamps (day, 2*day) are still charged rather than zeroed.
    assert _perp_funding_return(lookup, symbol="AUSDT", side="short", entry_ts_ms=0, exit_ts_ms=2 * day) == (
        pytest.approx(0.003),
        "partial",
        2,
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
        assert image.size == (1600, 1460)
    assert not (output_dir / "volume_event_best_equity_btc_spy.png").exists()
    assert not (output_dir / "volume_event_best_equity_btc_spy.svg").exists()
    assert not (output_dir / "volume_event_best_equity_benchmarks.csv").exists()
    assert not (output_dir / "volume_event_best_equity_annotations.csv").exists()
    assert chart["series"]["strategy"] == 5
    assert chart["series"]["btc"] == 5


def test_equity_benchmark_chart_honours_custom_png_name(tmp_path: Path) -> None:
    """Other sleeves (long_native) reuse this helper but want their own PNG
    filename so the output dir doesn't collide with the short sleeve's
    `volume_event_best_equity_btc.png`. The `png_name` kwarg overrides
    the filename without changing any other rendering behaviour.
    """
    from PIL import Image

    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    equity = pl.DataFrame(
        [
            {"ts_ms": 1, "date": "2024-01-01", "equity": 1.0, "drawdown": 0.0, "basket_return": 0.0},
            {"ts_ms": 2, "date": "2024-01-02", "equity": 1.05, "drawdown": 0.0, "basket_return": 0.05},
        ]
    )
    raw_klines = pl.DataFrame(
        [
            {"ts_ms": 1, "date": "2024-01-01", "symbol": "BTCUSDT", "close": 100.0},
            {"ts_ms": 2, "date": "2024-01-02", "symbol": "BTCUSDT", "close": 105.0},
        ]
    )

    chart = _write_equity_benchmark_chart(
        output_dir,
        root=tmp_path,
        equity=equity,
        raw_klines=raw_klines,
        png_name="long_native_equity_btc.png",
    )

    assert Path(chart["png"]).name == "long_native_equity_btc.png"
    assert Path(chart["png"]).exists()
    assert not (output_dir / "volume_event_best_equity_btc.png").exists()
    with Image.open(chart["png"]) as image:
        assert image.size == (1600, 1460)
    assert chart["monthly_rows"] == 1
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
    # A genuine MID-HISTORY hole: the coin trades 01-01 and 01-03 (so its traded
    # span is [01-01, 01-03]) but 01-02 is missing -> still required, still flagged.
    klines = pl.DataFrame(
        [{"symbol": "AAAUSDT", "date": "2024-01-01"} for _ in range(24)]
        + [{"symbol": "AAAUSDT", "date": "2024-01-03"} for _ in range(24)]
    )
    manifest = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "date": "2024-01-01"},
            {"symbol": "AAAUSDT", "date": "2024-01-02"},
            {"symbol": "AAAUSDT", "date": "2024-01-03"},
        ]
    )

    message = _full_pit_universe_error(klines, manifest)

    assert "missing_symbols=0" in message
    assert "missing_date_symbols=1" in message
    assert "2024-01-02" in message
    assert _full_pit_universe_pass(klines, manifest) is False


def test_full_pit_universe_excludes_prelisting_and_postdelisting_phantoms() -> None:
    # The coin's real traded span (>=20-bar kline days) is [2024-01-02, 2024-01-03].
    klines = pl.DataFrame(
        [{"symbol": "AAAUSDT", "date": "2024-01-02"} for _ in range(24)]
        + [{"symbol": "AAAUSDT", "date": "2024-01-03"} for _ in range(24)]
    )
    # The trade manifest over-claims an empty-file day on EACH side of that span:
    #   2024-01-01 -> pre-listing (date < first kline)
    #   2024-01-09 -> post-delisting settlement artifact (date > last kline)
    # Neither is tradable, so neither is required and the gate must still PASS.
    manifest = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "date": "2024-01-01"},
            {"symbol": "AAAUSDT", "date": "2024-01-02"},
            {"symbol": "AAAUSDT", "date": "2024-01-03"},
            {"symbol": "AAAUSDT", "date": "2024-01-09"},
        ]
    )

    required = _required_pit_date_symbols(klines, manifest)
    assert ("2024-01-01", "AAAUSDT") not in required  # pre-listing excluded
    assert ("2024-01-09", "AAAUSDT") not in required  # post-delisting excluded
    assert ("2024-01-02", "AAAUSDT") in required
    assert ("2024-01-03", "AAAUSDT") in required
    assert _full_pit_universe_pass(klines, manifest) is True


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

    with pytest.raises(ValueError, match="entry_execution_veto_close_location_max"):
        _validate_event_config(VolumeEventResearchConfig(entry_execution_veto_close_location_max=1.1))

    with pytest.raises(ValueError, match="rank_exit_threshold"):
        _validate_event_config(VolumeEventResearchConfig(rank_exit_threshold=0.0))
    with pytest.raises(ValueError, match="failed_fade_exit_hours"):
        _validate_event_config(VolumeEventResearchConfig(failed_fade_exit_hours=-1))
    with pytest.raises(ValueError, match="failed_fade_min_mfe_pct"):
        _validate_event_config(VolumeEventResearchConfig(failed_fade_min_mfe_pct=-0.1))
    with pytest.raises(ValueError, match="failed_fade_loss_pct"):
        _validate_event_config(VolumeEventResearchConfig(failed_fade_exit_hours=12, failed_fade_loss_pct=0.0))
    with pytest.raises(ValueError, match="failed_fade_close_location_min"):
        _validate_event_config(VolumeEventResearchConfig(failed_fade_close_location_min=1.1))

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


_LEGACY_GATE_SPLITS = (
    ("train_2023_2024", "2023-05-03", "2024-05-03"),
    ("validation_2024_2025", "2024-05-03", "2025-05-03"),
    ("oos_2025_2026", "2025-05-03", "2026-05-03"),
)


def test_volume_event_promotion_requires_all_splits_positive() -> None:
    fields = _promotion_fields(
        [
            {"name": "train_2023_2024", "total_return": 0.10, "max_drawdown": -0.10, "sharpe_like": 1.0},
            {"name": "validation_2024_2025", "total_return": -0.01, "max_drawdown": -0.05, "sharpe_like": 0.8},
            {"name": "oos_2025_2026", "total_return": 0.03, "max_drawdown": -0.12, "sharpe_like": 0.7},
        ],
        config=VolumeEventResearchConfig(splits=_LEGACY_GATE_SPLITS),
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
        config=VolumeEventResearchConfig(splits=_LEGACY_GATE_SPLITS),
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
        config=VolumeEventResearchConfig(splits=_LEGACY_GATE_SPLITS),
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
        config=VolumeEventResearchConfig(splits=_LEGACY_GATE_SPLITS),
        pit_membership_pass=True,
        full_pit_universe_pass=True,
    )

    assert fields["promotion_gate_pass"] is False
    assert "drawdown_fail" in fields["promotion_reason"]


def test_volume_event_promotion_whole_period_gate_binds_on_dd_and_sharpe() -> None:
    """M1: when splits=() the per-split gates degenerate to no-ops, so the
    whole-period max-DD and Sharpe thresholds (from the summary) MUST bind.
    Previously this branch passed unconditionally (PIT-coverage flag mislabeled
    as a quality gate)."""
    config = VolumeEventResearchConfig()  # promotion_max_drawdown=-0.25, min_avg_sharpe=0.50

    # Good whole-period run: shallow DD, healthy Sharpe -> passes.
    good = _promotion_fields(
        [], config=config, pit_membership_pass=True, full_pit_universe_pass=True,
        whole_period_drawdown=-0.20, whole_period_sharpe=0.80,
    )
    assert good["pre_pit_gate_pass"] is True
    assert good["promotion_gate_pass"] is True
    assert good["expected_splits"] == 0

    # Sharpe below threshold -> the gate now (correctly) fails.
    weak_sharpe = _promotion_fields(
        [], config=config, pit_membership_pass=True, full_pit_universe_pass=True,
        whole_period_drawdown=-0.20, whole_period_sharpe=0.30,
    )
    assert weak_sharpe["pre_pit_gate_pass"] is False
    assert weak_sharpe["promotion_gate_pass"] is False

    # Drawdown beyond the cap -> fails.
    blown_dd = _promotion_fields(
        [], config=config, pit_membership_pass=True, full_pit_universe_pass=True,
        whole_period_drawdown=-0.40, whole_period_sharpe=0.80,
    )
    assert blown_dd["pre_pit_gate_pass"] is False
    assert blown_dd["promotion_gate_pass"] is False


def test_attach_event_archive_membership_flags_symbol_dates() -> None:
    # Signals are stamped at 00:00 UTC of the day AFTER the bar they summarise, so
    # ts_ms = 2024-01-01 00:00 is the 2023-12-31 daily close. Post-FIX-A, PIT
    # membership is keyed on that TRADING DAY (date of ts_ms-1ms), so the manifest
    # must list 2023-12-31 for AAAUSDT to be a member. The `date` column itself is
    # the stamp date (2024-01-01), preserved for the age features.
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
                "date": "2023-12-31",
                "url": "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2023-12-31.csv.gz",
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


def test_clamp_position_weight_bounds_the_multiplier() -> None:
    assert _clamp_position_weight(1.0, clamp=4.0) == 1.0
    assert _clamp_position_weight(2.5, clamp=4.0) == 2.5
    assert _clamp_position_weight(99.0, clamp=4.0) == 4.0
    assert _clamp_position_weight(0.001, clamp=4.0) == pytest.approx(0.25)


def test_position_sizing_quantity_inverse_vol_is_one_over_sigma() -> None:
    quantity = _position_sizing_quantity(
        {"prior7_return_volatility": 0.05},
        mode="inverse_vol",
        vol_field="prior7_return_volatility",
        score_col="vol_score",
    )
    assert quantity == pytest.approx(20.0)
    for bad in (
        {},
        {"prior7_return_volatility": 0.0},
        {"prior7_return_volatility": -0.1},
        {"prior7_return_volatility": float("nan")},
    ):
        assert (
            _position_sizing_quantity(
                bad,
                mode="inverse_vol",
                vol_field="prior7_return_volatility",
                score_col="vol_score",
            )
            is None
        )


def test_position_sizing_quantity_signal_rank_and_equal() -> None:
    assert _position_sizing_quantity(
        {"vol_score": 0.8}, mode="signal_rank", vol_field="x", score_col="vol_score"
    ) == pytest.approx(0.8)
    assert (
        _position_sizing_quantity(
            {"vol_score": 0.0}, mode="signal_rank", vol_field="x", score_col="vol_score"
        )
        is None
    )
    # equal mode never derives a quantity -> the sizer short-circuits to a neutral weight.
    assert (
        _position_sizing_quantity(
            {"vol_score": 0.8}, mode="equal", vol_field="x", score_col="vol_score"
        )
        is None
    )


def test_position_sizer_normalizes_by_expanding_mean_of_prior_events() -> None:
    sizer = _PositionSizer(mode="inverse_vol", vol_field="sigma", clamp=4.0, score_col="vol_score")
    # event 1: no prior history -> neutral weight (1/sigma = 10)
    assert sizer.weight({"sigma": 0.10}) == 1.0
    # event 2: prior mean of 1/sigma = 10, this 1/sigma = 10 -> weight 1.0
    assert sizer.weight({"sigma": 0.10}) == pytest.approx(1.0)
    # event 3: lower vol, 1/sigma = 20, prior mean = 10 -> weight 2.0
    assert sizer.weight({"sigma": 0.05}) == pytest.approx(2.0)
    # event 4: higher vol, 1/sigma = 5, prior mean = (10+10+20)/3 -> weight < 1
    assert sizer.weight({"sigma": 0.20}) == pytest.approx(5.0 / (40.0 / 3.0))


def test_position_sizer_clamps_and_ignores_missing_sigma() -> None:
    sizer = _PositionSizer(mode="inverse_vol", vol_field="sigma", clamp=4.0, score_col="s")
    sizer.weight({"sigma": 1.0})  # prior 1/sigma = 1
    # a missing-sigma event takes a neutral weight and must not poison the accumulator
    assert sizer.weight({}) == 1.0
    # extreme low vol -> raw weight 1000, clamped to 4.0 (proves the prior mean stayed 1.0)
    assert sizer.weight({"sigma": 0.001}) == pytest.approx(4.0)
    # extreme high vol relative to a tiny-vol prior -> clamped to the 1/clamp floor
    sizer2 = _PositionSizer(mode="inverse_vol", vol_field="sigma", clamp=4.0, score_col="s")
    sizer2.weight({"sigma": 0.001})  # prior 1/sigma = 1000
    assert sizer2.weight({"sigma": 10.0}) == pytest.approx(0.25)


def test_position_sizer_equal_mode_is_always_neutral() -> None:
    sizer = _PositionSizer(mode="equal", vol_field="sigma", clamp=4.0, score_col="s")
    for sigma in (0.01, 5.0, 0.2):
        assert sizer.weight({"sigma": sigma}) == 1.0


def test_position_sizer_risk_equal_is_absolute_target_over_vol_clamped() -> None:
    """R5: risk_equal returns an ABSOLUTE target_vol/realized_vol weight (clamped),
    NOT normalized by an expanding mean of prior events (that's inverse_vol)."""
    sizer = _PositionSizer(
        mode="risk_equal", vol_field="sigma", clamp=4.0, score_col="s",
        target_vol_per_name=0.02,
    )
    # weight = target / sigma, exact in-range value
    assert sizer.weight({"sigma": 0.04}) == pytest.approx(0.5)   # 0.02 / 0.04
    # absolute, not expanding-mean-relative: same sigma -> same weight every time
    assert sizer.weight({"sigma": 0.04}) == pytest.approx(0.5)
    assert sizer.weight({"sigma": 0.01}) == pytest.approx(2.0)   # 0.02 / 0.01
    # clamp binds at both extremes ([1/4, 4])
    assert sizer.weight({"sigma": 1.0}) == pytest.approx(0.25)   # 0.02/1.0=0.02 -> floor
    assert sizer.weight({"sigma": 0.001}) == pytest.approx(4.0)  # 0.02/0.001=20 -> cap


def test_position_sizer_risk_equal_neutral_on_missing_or_invalid_vol() -> None:
    sizer = _PositionSizer(
        mode="risk_equal", vol_field="sigma", clamp=4.0, score_col="s",
        target_vol_per_name=0.02,
    )
    assert sizer.weight({}) == 1.0                 # missing
    assert sizer.weight({"sigma": 0.0}) == 1.0     # zero
    assert sizer.weight({"sigma": -0.1}) == 1.0    # negative
    assert sizer.weight({"sigma": float("nan")}) == 1.0  # non-finite


def test_position_sizing_quantity_taker_imbalance_weighted() -> None:
    def q(imbalance: float) -> float | None:
        return _position_sizing_quantity(
            {"taker_imbalance_1d": imbalance},
            mode="taker_imbalance_weighted",
            vol_field="x",
            score_col="s",
            size_field="taker_imbalance_1d",
            size_scale=0.03,
        )

    # quantity = exp(-imbalance / scale): 1.0 at zero, decreasing, always positive.
    assert q(0.0) == pytest.approx(1.0)
    q_sell, q_buy = q(-0.03), q(0.03)
    # net aggressive selling -> larger short; net aggressive buying -> smaller short.
    assert q_sell > 1.0 > q_buy > 0.0
    assert q_sell * q_buy == pytest.approx(1.0)
    # missing / non-finite imbalance -> None (neutral weight downstream).
    for bad in ({}, {"taker_imbalance_1d": float("nan")}):
        assert (
            _position_sizing_quantity(
                bad,
                mode="taker_imbalance_weighted",
                vol_field="x",
                score_col="s",
                size_field="taker_imbalance_1d",
                size_scale=0.03,
            )
            is None
        )


def test_position_sizer_taker_imbalance_weighted_tilts_size() -> None:
    sizer = _PositionSizer(
        mode="taker_imbalance_weighted",
        vol_field="x",
        clamp=4.0,
        score_col="s",
        size_field="taker_imbalance_1d",
        size_scale=0.03,
    )
    # first event: no prior history -> neutral weight.
    assert sizer.weight({"taker_imbalance_1d": 0.0}) == 1.0
    # net aggressive selling sizes above the prior mean.
    assert sizer.weight({"taker_imbalance_1d": -0.03}) > 1.0
    # a missing-imbalance event takes a neutral weight and must not poison the accumulator.
    assert sizer.weight({}) == 1.0
    # heavy taker buying sizes below the prior mean.
    assert sizer.weight({"taker_imbalance_1d": 0.06}) < 1.0


def test_position_weighting_config_is_validated() -> None:
    with pytest.raises(ValueError, match="position_weighting"):
        _validate_event_config(VolumeEventResearchConfig(position_weighting="bogus"))
    with pytest.raises(ValueError, match="position_weight_clamp"):
        _validate_event_config(VolumeEventResearchConfig(position_weight_clamp=0.5))
    for mode in POSITION_WEIGHTINGS:
        _validate_event_config(VolumeEventResearchConfig(position_weighting=mode))


def test_simulate_indexed_trade_scales_pnl_by_position_weight() -> None:
    hour = 60 * 60 * 1000
    symbol_bars = _make_symbol_bars(
        [
            {"bar_end_ts_ms": hour, "high": 101.0, "low": 99.0, "close": 100.0},
            {"bar_end_ts_ms": 2 * hour, "high": 100.5, "low": 97.0, "close": 98.0},
        ]
    )
    base_kwargs = {
        "symbol": "AUSDT",
        "side": "short",
        "score": 1.0,
        "rank": 1,
        "basket_id": "basket",
        "signal_ts_ms": 0,
        "entry_bar": 0,
        "symbol_bars": symbol_bars,
        "planned_exit_ts_ms": 2 * hour,
        "notional_weight": 0.2,
        "config": TradeLifecycleConfig(take_profit_pct=0.0),
        "round_trip_cost_bps": 10.0,
        "stop_pct": 0.5,
        "rank_lookup": {},
        "event_decay_threshold": -1.0,
        "funding_lookup": None,
        "stop_fill_mode": "stop",
    }
    flat = _simulate_indexed_trade(**base_kwargs, position_weight=1.0)
    doubled = _simulate_indexed_trade(**base_kwargs, position_weight=2.0)
    assert flat is not None and doubled is not None
    assert flat["position_weight"] == 1.0
    assert doubled["position_weight"] == 2.0
    assert flat["notional_weight"] == pytest.approx(0.2)
    # every P&L component scales linearly with the position weight
    assert doubled["notional_weight"] == pytest.approx(2.0 * flat["notional_weight"])
    assert doubled["net_return"] == pytest.approx(2.0 * flat["net_return"])
    assert doubled["gross_return"] == pytest.approx(2.0 * flat["gross_return"])
    assert doubled["cost_return"] == pytest.approx(2.0 * flat["cost_return"])
    # the per-unit trade return is a market fact, unchanged by sizing
    assert doubled["gross_trade_return"] == pytest.approx(flat["gross_trade_return"])
    assert flat["net_return"] != 0.0


def test_position_weighting_runs_through_full_pipeline(tmp_path: Path) -> None:
    for mode in ("inverse_vol", "signal_rank"):
        root = tmp_path / mode
        root.mkdir()
        generate_fixture_data(root)
        payload = run_volume_event_research(
            root,
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
                position_weighting=mode,
            ),
        )
        assert payload["rows"]["scenarios"] == 1


def test_scenario_hold_ms_daily_default_unchanged() -> None:
    """Architecture-B capability: hold_hours defaults to None = the daily hold,
    byte-identical to hold_days * MS_PER_DAY. scenario_id keeps the legacy hNN tag."""
    from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR

    daily = EventScenario(
        event_type="rocket", threshold=0.9, side_hypothesis="short",
        hold_days=3, stop_loss_pct=0.12, cost_multiplier=3.0,
    )
    assert daily.hold_hours is None
    assert _scenario_hold_ms(daily) == 3 * MS_PER_DAY
    assert "-h3-" in daily.scenario_id  # legacy daily tag, no collision risk

    sub_daily = EventScenario(
        event_type="rocket", threshold=0.9, side_hypothesis="short",
        hold_days=3, stop_loss_pct=0.12, cost_multiplier=3.0, hold_hours=12,
    )
    assert _scenario_hold_ms(sub_daily) == 12 * MS_PER_HOUR
    assert "-h12h-" in sub_daily.scenario_id  # sub-daily tag, distinct id
    assert sub_daily.scenario_id != daily.scenario_id


def test_cooldown_hours_defaults_to_daily() -> None:
    """cooldown_hours defaults to None so the live cooldown is the daily
    cooldown_days; setting it switches to a sub-daily re-entry cooldown."""
    cfg = VolumeEventResearchConfig()
    assert cfg.cooldown_hours is None  # default = daily behavior preserved
    assert cfg.cooldown_days == 5
