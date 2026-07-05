from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.long_native import (
    BTC_MONTH_REGIME_MODE_DAILY_30D,
    BTC_MONTH_REGIME_MODE_HOURLY_EXACT_MONTH,
    BTC_MONTH_REGIME_MODE_SMART_MONTH,
    LongNativeConfig,
    _classify_entry,
    build_long_features,
)


def _date_for_hour(hour: int) -> str:
    ts = dt.datetime.fromtimestamp(hour * MS_PER_HOUR / 1000, tz=dt.timezone.utc)
    return ts.strftime("%Y-%m-%d")


def _btc_month_klines(*, days: int = 35) -> pl.DataFrame:
    rows = []
    target_anchor_hour = 32 * 24 - 1
    for hour in range(days * 24):
        for symbol in ("BTCUSDT", "ALTUSDT"):
            close = 100.0
            if symbol == "BTCUSDT":
                if hour == 47:
                    close = 200.0
                elif hour == target_anchor_hour:
                    close = 300.0
            rows.append(
                {
                    "ts_ms": hour * MS_PER_HOUR,
                    "date": _date_for_hour(hour),
                    "symbol": symbol,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "turnover_quote": 1_000_000.0,
                    "volume_base": 10_000.0,
                }
            )
    return pl.DataFrame(rows)


def test_long_features_join_hourly_exact_month_btc_context() -> None:
    cfg = LongNativeConfig(
        btc_month_regime_mode=BTC_MONTH_REGIME_MODE_HOURLY_EXACT_MONTH,
        min_listing_history_days=1,
        universe_volume_window_days=2,
    )

    features = build_long_features(_btc_month_klines(), funding=pl.DataFrame(), config=cfg)
    row = features.filter((pl.col("symbol") == "ALTUSDT") & (pl.col("ts_ms") == 32 * MS_PER_DAY)).row(
        0, named=True
    )

    assert row["btc_month_ret_exact"] == pytest.approx(2.0)
    assert row["btc_month_ret_30d"] == pytest.approx(0.5)
    assert row["btc_month_ret_smart"] == pytest.approx(0.51)
    assert row["btc_month_ret_exact_source_ts_ms"] == 36 * MS_PER_HOUR


def test_long_month_regime_gate_blocks_and_allows_by_selected_mode() -> None:
    row = {
        "in_universe": True,
        "regime_on": True,
        "mom_rank": 1,
        "mom_score": 0.10,
        "btc_month_ret_30d": -0.02,
        "btc_month_ret_exact": 0.03,
        "btc_month_ret_smart": 0.02,
    }
    base = dict(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_xsec_momentum=True,
        btc_month_regime_gate="uptrend",
    )

    blocked = LongNativeConfig(**base, btc_month_regime_mode=BTC_MONTH_REGIME_MODE_DAILY_30D)
    allowed = LongNativeConfig(**base, btc_month_regime_mode=BTC_MONTH_REGIME_MODE_HOURLY_EXACT_MONTH)

    assert _classify_entry(row, blocked)[0] is None
    assert _classify_entry(row, allowed)[0] == "xsec_momentum"


def test_long_smart_month_regime_gate_tolerates_small_disagreement() -> None:
    row = {
        "in_universe": True,
        "regime_on": True,
        "mom_rank": 1,
        "mom_score": 0.10,
        "btc_month_ret_30d": -0.005,
        "btc_month_ret_exact": 0.02,
        "btc_month_ret_smart": 0.005,
    }
    cfg = LongNativeConfig(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_xsec_momentum=True,
        btc_month_regime_gate="uptrend",
        btc_month_regime_mode=BTC_MONTH_REGIME_MODE_SMART_MONTH,
    )

    assert _classify_entry(row, cfg)[0] == "xsec_momentum"
