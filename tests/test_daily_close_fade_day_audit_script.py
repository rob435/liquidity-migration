from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_daily_close_fade_day_audit as day_audit
from aggression_carry.config import DailyCloseFadeConfig


def test_day_audit_joins_pre_signal_context_without_using_post_trade_path() -> None:
    config = DailyCloseFadeConfig(
        signal_minute=22 * 60,
        top_n=2,
        pump_filter="pump",
        liquidity_rank_min=31,
        liquidity_rank_max=150,
        exclude_symbols=(),
    )
    features = pl.DataFrame(
        [
            _feature("2026-01-01", 1000, "BTCUSDT", 0.01, False, 1),
            _feature("2026-01-01", 1000, "AUSDT", 0.12, True, 31),
            _feature("2026-01-01", 1000, "BUSDT", 0.08, True, 40),
            _feature("2026-01-01", 1000, "CUSDT", -0.01, False, 50),
            _feature("2026-01-02", 2000, "BTCUSDT", -0.02, False, 1),
            _feature("2026-01-02", 2000, "AUSDT", 0.04, True, 31),
            _feature("2026-01-02", 2000, "BUSDT", 0.03, True, 40),
            _feature("2026-01-02", 2000, "CUSDT", -0.03, False, 50),
        ],
        infer_schema_length=None,
    )
    trades = pl.DataFrame(
        [
            _trade("1000-1320", "2026-01-01", 1000, "AUSDT", 0.12, 0.01),
            _trade("1000-1320", "2026-01-01", 1000, "BUSDT", 0.08, 0.02),
            _trade("2000-1320", "2026-01-02", 2000, "AUSDT", 0.04, -0.01),
            _trade("2000-1320", "2026-01-02", 2000, "BUSDT", 0.03, -0.02),
        ],
        infer_schema_length=None,
    )
    baskets = pl.DataFrame(
        [
            {"basket_id": "1000-1320", "date": "2026-01-01", "signal_ts_ms": 1000, "signal_minute": 1320, "trade_count": 2, "basket_return": 0.03},
            {"basket_id": "2000-1320", "date": "2026-01-02", "signal_ts_ms": 2000, "signal_minute": 1320, "trade_count": 2, "basket_return": -0.03},
        ],
        infer_schema_length=None,
    )
    btc = pl.DataFrame(
        [
            {"date": "2026-01-01", "btc_day_return": 0.01, "btc_last_60m_return": 0.002, "btc_last_240m_return": 0.005},
            {"date": "2026-01-02", "btc_day_return": -0.02, "btc_last_60m_return": -0.001, "btc_last_240m_return": -0.004},
        ],
        infer_schema_length=None,
    )

    rows = day_audit.build_day_audit_rows(features, trades, baskets, btc, config=config)

    first = rows.filter(pl.col("date") == "2026-01-01").row(0, named=True)
    assert first["winning_day"] is True
    assert first["symbols"] == "AUSDT,BUSDT"
    assert round(first["selected_avg_day_return"], 6) == 0.10
    assert round(first["selected_excess_vs_btc"], 6) == 0.09
    assert first["candidate_count"] == 2
    assert first["exit_mix"] == "max_hold:2"


def test_win_loss_contrast_finds_metrics_higher_on_losing_days() -> None:
    rows = pl.DataFrame(
        [
            {"winning_day": True, "btc_day_return": 0.01, "selected_excess_vs_market": 0.03},
            {"winning_day": True, "btc_day_return": 0.02, "selected_excess_vs_market": 0.02},
            {"winning_day": False, "btc_day_return": 0.08, "selected_excess_vs_market": 0.10},
            {"winning_day": False, "btc_day_return": 0.07, "selected_excess_vs_market": 0.11},
        ],
        infer_schema_length=None,
    )

    contrast = day_audit.build_win_loss_contrast(rows)
    btc = contrast.filter(pl.col("metric") == "btc_day_return").row(0, named=True)

    assert btc["loss_mean"] > btc["win_mean"]
    assert btc["standardized_diff"] > 0


def test_context_buckets_count_trading_days_and_returns() -> None:
    rows = pl.DataFrame(
        [
            {"btc_day_return": value, "basket_return": ret, "trade_count": 1}
            for value, ret in [(0.01, 0.01), (0.02, 0.02), (0.03, -0.01), (0.04, 0.03), (0.05, -0.02)]
        ],
        infer_schema_length=None,
    )

    buckets = day_audit.build_context_bucket_summary(rows, buckets=5)

    assert buckets.height == 5
    assert buckets["days"].sum() == 5
    assert "compounded_return" in buckets.columns


def _feature(date: str, ts_ms: int, symbol: str, day_return: float, pump_like: bool, rank: int) -> dict:
    return {
        "date": date,
        "signal_ts_ms": ts_ms,
        "signal_minute": 1320,
        "symbol": symbol,
        "bar_coverage": 1.0,
        "eligible": symbol != "BTCUSDT",
        "day_return": day_return,
        "vol_adjusted_day_return": day_return / 0.02,
        "day_turnover": 1_000_000.0,
        "last_60m_turnover": 100_000.0,
        "late_volume_ratio": 1.5,
        "vwap_extension": day_return / 2,
        "pump_like": pump_like,
        "baseline_liquidity_rank": rank,
        "baseline_liquidity_turnover": 1_000_000.0,
    }


def _trade(basket_id: str, date: str, signal_ts_ms: int, symbol: str, day_return: float, weighted_return: float) -> dict:
    return {
        "basket_id": basket_id,
        "date": date,
        "signal_ts_ms": signal_ts_ms,
        "symbol": symbol,
        "entry_rank": 1,
        "day_return": day_return,
        "vol_adjusted_day_return": day_return / 0.02,
        "late_volume_ratio": 1.5,
        "vwap_extension": day_return / 2,
        "pump_score": 4,
        "baseline_liquidity_rank": 35,
        "mae": -0.01,
        "mfe": 0.04,
        "exit_reason": "max_hold",
        "weighted_net_return": weighted_return,
        "net_return": weighted_return * 2,
    }
