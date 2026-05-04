from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_daily_close_fade_coin_filter_sweep as coin_sweep
from aggression_carry.config import DailyCloseFadeConfig


def test_coin_market_context_adds_per_coin_excess_vs_market() -> None:
    features = pl.DataFrame(
        [
            _feature("AAAUSDT", "2026-01-01", 1_000, 0.10),
            _feature("BBBUSDT", "2026-01-01", 1_000, 0.02),
            _feature("CCCUSDT", "2026-01-01", 1_000, -0.02),
        ],
        infer_schema_length=None,
    )

    output = coin_sweep.attach_coin_market_context(features, DailyCloseFadeConfig(signal_minute=1335))

    row = output.filter(pl.col("symbol") == "AAAUSDT").row(0, named=True)
    assert abs(row["market_median_day_return"] - 0.02) < 1e-12
    assert abs(row["coin_excess_vs_market"] - 0.08) < 1e-12


def test_apply_coin_filter_gates_each_candidate_before_selection() -> None:
    features = pl.DataFrame(
        [
            {**_feature("AAAUSDT", "2026-01-01", 1_000, 0.10), "coin_excess_vs_market": 0.08},
            {**_feature("BBBUSDT", "2026-01-01", 1_000, 0.07), "coin_excess_vs_market": 0.04},
            {**_feature("CCCUSDT", "2026-01-01", 1_000, 0.12), "coin_excess_vs_market": 0.09, "vwap_extension": 0.01},
        ],
        infer_schema_length=None,
    )
    spec = coin_sweep.CoinFilterSpec(
        coin_excess_vs_market_min=0.05,
        coin_vwap_extension_min=0.025,
        coin_late_volume_ratio_min=0.75,
    )

    output = coin_sweep.apply_coin_filter(features, spec)

    eligible = dict(output.select(["symbol", "eligible"]).iter_rows())
    assert eligible == {"AAAUSDT": True, "BBBUSDT": False, "CCCUSDT": False}


def test_summarize_baskets_counts_skipped_days_as_zero_return() -> None:
    calendar = pl.DataFrame(
        [
            {"basket_id": "a", "date": "2026-01-01", "basket_return": 0.05, "trade_count": 5},
            {"basket_id": "b", "date": "2026-01-02", "basket_return": -0.10, "trade_count": 5},
        ],
        infer_schema_length=None,
    )
    selected = pl.DataFrame(
        [{"basket_id": "a", "date": "2026-01-01", "basket_return": 0.05, "trade_count": 2}],
        infer_schema_length=None,
    )

    row = coin_sweep.summarize_baskets_against_calendar(
        calendar,
        selected,
        split="test",
        allocation_mode="reallocate",
        label="filtered",
        spec=coin_sweep.CoinFilterSpec(),
        baseline_total_return=0.0,
        baseline_max_drawdown=0.0,
    )

    assert row["selected_days"] == 1
    assert row["skipped_days"] == 1
    assert row["trades"] == 2
    assert abs(row["total_return"] - 0.05) < 1e-12


def test_fixed_slot_baskets_leave_missing_top_n_weight_in_cash() -> None:
    trades = pl.DataFrame(
        [
            {
                "basket_id": "a",
                "signal_ts_ms": 1_000,
                "date": "2026-01-01",
                "signal_minute": 1335,
                "gross_return": 0.10,
                "cost_return": 0.01,
                "net_return": 0.09,
                "mae": -0.02,
                "mfe": 0.12,
            },
            {
                "basket_id": "a",
                "signal_ts_ms": 1_000,
                "date": "2026-01-01",
                "signal_minute": 1335,
                "gross_return": 0.05,
                "cost_return": 0.01,
                "net_return": 0.04,
                "mae": -0.01,
                "mfe": 0.07,
            },
        ],
        infer_schema_length=None,
    )

    baskets = coin_sweep.summarize_fixed_slot_baskets(trades, DailyCloseFadeConfig(top_n=5, gross_exposure=1.0))

    row = baskets.row(0, named=True)
    assert row["trade_count"] == 2
    assert abs(row["basket_return"] - ((0.09 + 0.04) / 5)) < 1e-12
    assert abs(row["basket_gross_exposure"] - 0.4) < 1e-12


def _feature(symbol: str, date: str, ts_ms: int, day_return: float) -> dict:
    return {
        "symbol": symbol,
        "date": date,
        "signal_ts_ms": ts_ms,
        "signal_minute": 1335,
        "bar_coverage": 1.0,
        "day_return": day_return,
        "vwap_extension": 0.03,
        "late_volume_ratio": 1.0,
        "eligible": True,
    }
