from __future__ import annotations

import pytest

from liquidity_migration.ingestion import generate_fixture_data
from liquidity_migration.storage import read_dataset
from liquidity_migration.trade_lifecycle import _side_return, _stop_price, _take_profit_price
from liquidity_migration.volume_features import build_volume_features


def test_volume_features_build_daily_liquidity_ranks(tmp_path) -> None:
    generate_fixture_data(tmp_path)
    klines = read_dataset(tmp_path, "klines_1h")

    features = build_volume_features(klines)

    assert "volume_change_1d_z" in features.columns
    assert "volume_composite" in features.columns
    assert "liquidity_rank" in features.columns
    assert "liquidity_rank_pct" in features.columns
    assert features["symbol"].n_unique() > 1


def test_trade_lifecycle_linear_long_short_price_helpers() -> None:
    assert _side_return(100.0, 115.0, side="long") == pytest.approx(0.15)
    assert _side_return(100.0, 85.0, side="long") == pytest.approx(-0.15)
    assert _side_return(100.0, 115.0, side="short") == pytest.approx(-0.15)
    assert _side_return(100.0, 85.0, side="short") == pytest.approx(0.15)
    assert _stop_price(100.0, side="long", stop_loss_pct=0.20) == pytest.approx(80.0)
    assert _stop_price(100.0, side="short", stop_loss_pct=0.20) == pytest.approx(120.0)
    assert _take_profit_price(100.0, side="long", take_profit_pct=0.10) == pytest.approx(110.0)
    assert _take_profit_price(100.0, side="short", take_profit_pct=0.10) == pytest.approx(90.0)
