"""Tests for B.2 regime durability cohorting + Welch t-test."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from liquidity_migration._common import MS_PER_DAY
from liquidity_migration.regime_durability import (
    RegimeDurabilityConfig,
    _welch_t,
    cohort_for_trade,
    daily_close_series,
    regime_flips,
    run_regime_durability,
    summarize_cohort,
)


def _daily_synthetic(*, days: int, symbol: str, pattern: str = "ramp") -> pl.DataFrame:
    """Build a 1h-kline frame for `symbol` whose daily-close series exhibits
    a deterministic regime flip pattern.
    """
    rows = []
    if pattern == "ramp":
        # 30 days flat at 100, then 30 days at 150 → above SMA after the jump.
        prices = [100.0] * 30 + [150.0] * (days - 30)
    elif pattern == "oscillate":
        prices = []
        for i in range(days):
            # Decade-long cycle: 30 days up, 30 days down.
            phase = (i // 30) % 2
            prices.append(120.0 if phase == 0 else 80.0)
    else:
        raise ValueError(pattern)
    for i in range(days):
        date_ts = i * MS_PER_DAY
        for hour in range(24):
            rows.append(
                {
                    "ts_ms": date_ts + hour * (MS_PER_DAY // 24),
                    "symbol": symbol,
                    "date": f"1970-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}",
                    "close": prices[i],
                }
            )
    return pl.DataFrame(rows)


def test_daily_close_series_extracts_one_row_per_date() -> None:
    klines = _daily_synthetic(days=40, symbol="BTCUSDT")
    daily = daily_close_series(klines, symbol="BTCUSDT")
    assert daily.height == 40
    assert daily["close"][0] == 100.0
    assert daily["close"][-1] == 150.0


def test_regime_flips_detects_upward_crossing() -> None:
    klines = _daily_synthetic(days=60, symbol="BTCUSDT", pattern="ramp")
    daily = daily_close_series(klines, symbol="BTCUSDT")
    flips = regime_flips(daily, sma_days=30)
    # We expect a single regime-on flip shortly after the price jump.
    on_flips = [f for f in flips if f["direction"] == "on"]
    assert len(on_flips) >= 1


def test_regime_flips_handles_short_series() -> None:
    daily = pl.DataFrame(
        {
            "date": ["1970-01-01"],
            "ts_ms": [0],
            "close": [100.0],
        }
    )
    assert regime_flips(daily, sma_days=30) == []


def test_cohort_for_trade_held_through_flip_takes_precedence() -> None:
    flips = [
        {"date": "1970-01-15", "ts_ms": 15 * MS_PER_DAY, "direction": "on"},
        {"date": "1970-01-30", "ts_ms": 30 * MS_PER_DAY, "direction": "off"},
    ]
    cohort = cohort_for_trade(
        entry_ts_ms=20 * MS_PER_DAY,
        exit_ts_ms=35 * MS_PER_DAY,
        flips=flips,
        flip_window_days=7,
    )
    assert cohort == "held_through_flip"


def test_cohort_for_trade_fresh_regime_when_entry_within_window() -> None:
    flips = [{"date": "1970-01-15", "ts_ms": 15 * MS_PER_DAY, "direction": "on"}]
    cohort = cohort_for_trade(
        entry_ts_ms=20 * MS_PER_DAY,
        exit_ts_ms=25 * MS_PER_DAY,
        flips=flips,
        flip_window_days=7,
    )
    assert cohort == "fresh_regime"


def test_cohort_for_trade_standard_when_no_nearby_flip() -> None:
    flips = [{"date": "1970-01-01", "ts_ms": 0, "direction": "on"}]
    cohort = cohort_for_trade(
        entry_ts_ms=50 * MS_PER_DAY,
        exit_ts_ms=55 * MS_PER_DAY,
        flips=flips,
        flip_window_days=7,
    )
    assert cohort == "standard"


def test_welch_t_zero_for_identical_samples() -> None:
    a = np.array([0.05, 0.04, 0.06, 0.03])
    t, dof = _welch_t(a, a)
    assert t == 0.0
    assert dof > 0


def test_welch_t_sign_matches_mean_diff() -> None:
    a = np.array([0.10, 0.08, 0.12, 0.09])  # higher mean
    b = np.array([0.01, 0.02, 0.01, 0.03])
    t, _dof = _welch_t(a, b)
    assert t > 0
    t2, _ = _welch_t(b, a)
    assert t2 < 0


def test_welch_t_returns_zero_for_tiny_samples() -> None:
    t, dof = _welch_t(np.array([0.05]), np.array([0.05, 0.04]))
    assert t == 0.0
    assert dof == 0


def test_summarize_cohort_handles_empty() -> None:
    trades = pl.DataFrame(
        {
            "entry_ts_ms": pl.Series([], dtype=pl.Int64),
            "exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "net_return": pl.Series([], dtype=pl.Float64),
            "regime_cohort": pl.Series([], dtype=pl.String),
        }
    )
    out = summarize_cohort(trades, cohort="standard")
    assert out["trades"] == 0
    assert out["win_rate"] == 0.0


def test_run_regime_durability_full_path(tmp_path: Path) -> None:
    klines = _daily_synthetic(days=60, symbol="BTCUSDT", pattern="ramp")
    # Add ETH klines (just copies) so the runner has both symbols available.
    klines = pl.concat([klines, klines.with_columns(pl.lit("ETHUSDT").alias("symbol"))])
    # Build a small trade ledger with 3 trades in different cohorts.
    trades = pl.DataFrame(
        [
            # Entered after the (~day 30) regime flip → fresh_regime.
            {"entry_ts_ms": 33 * MS_PER_DAY, "exit_ts_ms": 36 * MS_PER_DAY,
             "net_return": 0.05, "symbol": "FOOUSDT"},
            # Entered well after the flip, no off-flip in lifetime → standard.
            {"entry_ts_ms": 55 * MS_PER_DAY, "exit_ts_ms": 58 * MS_PER_DAY,
             "net_return": -0.02, "symbol": "BARUSDT"},
            # Entered well before the flip, exit after the flip → in ramp pattern
            # there's no "off" flip, so this stays standard. Add another synthetic
            # row to keep the asserts numeric.
            {"entry_ts_ms": 40 * MS_PER_DAY, "exit_ts_ms": 45 * MS_PER_DAY,
             "net_return": 0.01, "symbol": "BAZUSDT"},
        ]
    )
    payload = run_regime_durability(
        trades=trades,
        klines_1h=klines,
        output_dir=tmp_path,
        config=RegimeDurabilityConfig(flip_window_days=7),
    )
    cohorts = {c["cohort"]: c for c in payload["cohorts"]}
    assert cohorts["fresh_regime"]["trades"] >= 1
    assert (tmp_path / "regime_durability_report.md").exists()
    assert (tmp_path / "regime_durability_report.json").exists()


def test_run_regime_durability_empty_trades(tmp_path: Path) -> None:
    klines = _daily_synthetic(days=30, symbol="BTCUSDT")
    trades = pl.DataFrame(
        {
            "entry_ts_ms": pl.Series([], dtype=pl.Int64),
            "exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "net_return": pl.Series([], dtype=pl.Float64),
        }
    )
    payload = run_regime_durability(
        trades=trades,
        klines_1h=klines,
        output_dir=tmp_path,
    )
    assert payload["trade_count"] == 0
    assert (tmp_path / "regime_durability_report.md").read_text().startswith("# Regime durability")
