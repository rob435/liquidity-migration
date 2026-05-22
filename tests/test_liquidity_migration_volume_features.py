from __future__ import annotations

import math

import numpy as np
import pytest

from liquidity_migration.ingestion import generate_fixture_data
from liquidity_migration.storage import read_dataset
from liquidity_migration.trade_lifecycle import _side_return, _stop_price, _take_profit_price
from liquidity_migration.volume_features import _rolling_mean, _rolling_sum, build_volume_features


def test_volume_features_build_daily_liquidity_ranks(tmp_path) -> None:
    generate_fixture_data(tmp_path)
    klines = read_dataset(tmp_path, "klines_1h")

    features = build_volume_features(klines)

    assert "volume_change_1d_z" in features.columns
    assert "volume_composite" in features.columns
    assert "liquidity_rank" in features.columns
    assert "liquidity_rank_pct" in features.columns
    assert features["symbol"].n_unique() > 1


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
