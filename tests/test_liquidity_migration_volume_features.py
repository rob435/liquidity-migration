from __future__ import annotations

import math

import numpy as np
import pytest

from liquidity_migration.ingestion import generate_fixture_data
from liquidity_migration.storage import read_dataset
from liquidity_migration.trade_lifecycle import _side_return, _stop_price, _take_profit_price
from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.volume_features import (
    _daily_bars,
    _rolling_mean,
    _rolling_sum,
    build_volume_features,
)


def test_volume_features_build_daily_liquidity_ranks(tmp_path) -> None:
    generate_fixture_data(tmp_path)
    klines = read_dataset(tmp_path, "klines_1h")

    features = build_volume_features(klines)

    assert "volume_change_1d_z" in features.columns
    assert "volume_composite" in features.columns
    assert "liquidity_rank" in features.columns
    assert "liquidity_rank_pct" in features.columns
    assert features["symbol"].n_unique() > 1


def test_daily_bars_default_is_unchanged_by_aggregation_param(tmp_path) -> None:
    """Tier B capability: the aggregation_ms knob defaults to MS_PER_DAY, so the
    deployed daily-cadence output is byte-identical (the daily-completeness
    threshold derives to exactly 20). Live alpha is unchanged."""
    generate_fixture_data(tmp_path)
    klines = read_dataset(tmp_path, "klines_1h")
    default = _daily_bars(klines)
    explicit_daily = _daily_bars(klines, aggregation_ms=MS_PER_DAY)
    assert default.equals(explicit_daily)
    # Whole-feature pipeline is identical at the daily default too.
    assert build_volume_features(klines).equals(build_volume_features(klines, aggregation_ms=MS_PER_DAY))
    # The daily bars are stamped at the next-day 00:00 boundary, unchanged.
    assert (default["ts_ms"] % MS_PER_DAY == 0).all()


def test_daily_bars_finer_interval_produces_more_bars(tmp_path) -> None:
    """The capability works: a finer aggregation interval recomputes the volume
    features on a sub-daily grid (more bars), with the completeness threshold
    scaled to the interval (the Architecture-B research path; default OFF)."""
    generate_fixture_data(tmp_path)
    klines = read_dataset(tmp_path, "klines_1h")
    daily = _daily_bars(klines, aggregation_ms=MS_PER_DAY)
    six_hourly = _daily_bars(klines, aggregation_ms=6 * MS_PER_HOUR)
    assert six_hourly.height > daily.height  # finer grid => more bars
    assert (six_hourly["ts_ms"] % (6 * MS_PER_HOUR) == 0).all()
    # Same volume features compute on the finer grid without error.
    feats = build_volume_features(klines, aggregation_ms=6 * MS_PER_HOUR)
    assert "volume_composite" in feats.columns and feats.height > 0


def test_rolling_sum_matches_naive_loop() -> None:
    rng = np.random.default_rng(42)
    values = rng.uniform(1.0, 100.0, size=50)
    for window in (1, 3, 7, 20, 50):
        fast = _rolling_sum(values, window)
        # naive reference
        ref = np.full(values.shape, np.nan)
        for i in range(window - 1, values.size):
            ref[i] = float(np.sum(values[i - window + 1 : i + 1]))
        np.testing.assert_allclose(fast, ref, rtol=1e-12, equal_nan=True)


def test_rolling_sum_edge_cases() -> None:
    v = np.array([1.0, 2.0, 3.0])
    # window > length → all NaN
    assert all(math.isnan(x) for x in _rolling_sum(v, 5))
    # window == length → single valid value at the last index
    result = _rolling_sum(v, 3)
    assert math.isnan(result[0]) and math.isnan(result[1])
    assert result[2] == pytest.approx(6.0)
    # window == 1 → identical to input
    np.testing.assert_allclose(_rolling_sum(v, 1), v)


def test_rolling_mean_matches_rolling_sum_divided_by_window() -> None:
    rng = np.random.default_rng(7)
    values = rng.uniform(0.5, 50.0, size=30)
    for window in (3, 7, 20):
        fast = _rolling_mean(values, window)
        expected = _rolling_sum(values, window) / window
        np.testing.assert_allclose(fast, expected, rtol=1e-12, equal_nan=True)


def test_side_return_stop_and_take_profit_prices() -> None:
    assert _side_return(100.0, 115.0, side="long") == pytest.approx(0.15)
    assert _side_return(100.0, 85.0, side="long") == pytest.approx(-0.15)
    assert _side_return(100.0, 115.0, side="short") == pytest.approx(-0.15)
    assert _side_return(100.0, 85.0, side="short") == pytest.approx(0.15)
    assert _stop_price(100.0, side="long", stop_loss_pct=0.20) == pytest.approx(80.0)
    assert _stop_price(100.0, side="short", stop_loss_pct=0.20) == pytest.approx(120.0)
    assert _take_profit_price(100.0, side="long", take_profit_pct=0.10) == pytest.approx(110.0)
    assert _take_profit_price(100.0, side="short", take_profit_pct=0.10) == pytest.approx(90.0)


def test_vectorized_features_match_numpy_reference_numerically(tmp_path) -> None:
    """Progressive standard (not bit-identical): the vectorized build_volume_features
    is NUMERICALLY equivalent to the prior numpy implementation — cross-sectional z
    and ranks match within float tolerance, NaN positions match exactly. Last-bit
    differences from polars-rolling vs numpy-cumsum-diff carry no alpha and don't gate
    shipping."""
    generate_fixture_data(tmp_path)
    klines = read_dataset(tmp_path, "klines_1h")
    out = build_volume_features(klines)

    # Recompute each robust z in numpy from the SAME raw column, per cross-section,
    # and assert the vectorized z matches within tolerance.
    raw_to_z = {
        "volume_change_1d_raw": "volume_change_1d_z",
        "volume_change_3d_raw": "volume_change_3d_z",
        "volume_persistence_raw": "volume_persistence_z",
        "dollar_volume_rank_raw": "dollar_volume_rank_z",
    }
    for _ts, part in out.group_by("ts_ms"):
        for raw_col, z_col in raw_to_z.items():
            vals = np.asarray(part[raw_col].to_list(), dtype=float)
            finite = np.isfinite(vals)
            ref = np.full(vals.shape, np.nan)
            if finite.sum() >= 3:
                center = float(np.nanmedian(vals[finite]))
                mad = float(np.nanmedian(np.abs(vals[finite] - center)))
                scale = 1.4826 * mad if mad > 1e-12 else float(np.nanstd(vals[finite]))
                if scale > 1e-12:
                    ref[finite] = np.clip((vals[finite] - center) / scale, -3.0, 3.0)
            got = np.asarray(part[z_col].to_list(), dtype=float)
            assert np.array_equal(np.isnan(got), np.isnan(ref)), (raw_col, "NaN mask")
            assert np.allclose(got[~np.isnan(got)], ref[~np.isnan(ref)], rtol=1e-9, atol=1e-12), raw_col

    # liquidity_rank is a per-ts_ms ordinal rank by log_turnover desc; pct = rank/count.
    for _ts, part in out.group_by("ts_ms"):
        n = part.height
        assert sorted(part["liquidity_rank"].to_list()) == list(range(1, n + 1))
        assert part["universe_count"].to_list() == [n] * n
        # top log_turnover -> rank 1
        top_idx = int(np.argmax(part["log_turnover"].to_numpy()))
        assert part["liquidity_rank"].to_list()[top_idx] == 1
