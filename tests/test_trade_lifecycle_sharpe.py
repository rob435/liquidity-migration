"""Tests for the daily-aligned Sharpe in trade_lifecycle: ``summarize_trade_backtest``
emits a single ``sharpe_like`` annualised off the daily equity curve, forward-filled
onto the calendar-day grid.
"""
from __future__ import annotations

import math

import polars as pl

from liquidity_migration.core._common import MS_PER_DAY
from liquidity_migration.core.config import TradeLifecycleConfig
from liquidity_migration.data.trade_lifecycle import (
    _daily_sharpe,
    annualized_sharpe,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)

MS_PER_HOUR = MS_PER_DAY // 24


def _trade(*, basket_id: str, exit_day: int, net_return: float) -> dict:
    """Synthesise a minimal trade row that summarize_trade_backtest accepts."""
    exit_ms = int(exit_day) * MS_PER_DAY
    entry_ms = max(0, exit_ms - MS_PER_DAY)
    return {
        "trade_id": f"{basket_id}-t",
        "basket_id": basket_id,
        "entry_signal_ts_ms": entry_ms,
        "entry_ts_ms": entry_ms,
        "exit_ts_ms": exit_ms,
        "entry_date": "1970-01-01",
        "exit_date": "1970-01-02",
        "exit_month": "1970-01",
        "symbol": "BTCUSDT",
        "side": "long",
        "score": 0.0,
        "rank": 1,
        "entry_price": 100.0,
        "exit_price": 100.0 * (1.0 + net_return),
        "exit_reason": "take_profit",
        "planned_exit_ts_ms": exit_ms,
        "stop_price": 90.0,
        "take_profit_price": 130.0,
        "notional_weight": 1.0,
        "position_weight": 1.0,
        "gross_trade_return": net_return,
        "gross_return": net_return,
        "cost_return": 0.0,
        "funding_return": 0.0,
        "funding_mode": "ok",
        "funding_event_count": 0,
        "net_return": net_return,
        "mae": 0.0,
        "mfe": 0.0,
        "bars_held": 24,
        "hold_hours": 24.0,
        "actual_entry_delay_hours": 0.0,
        "pattern": "fomo_chase",
    }


def test_daily_sharpe_zero_when_vol_zero() -> None:
    # 10 days of flat equity → zero vol → zero Sharpe.
    eq = pl.DataFrame(
        {
            "ts_ms": [i * MS_PER_DAY for i in range(10)],
            "equity": [1.0] * 10,
            "drawdown": [0.0] * 10,
            "basket_return": [0.0] * 10,
            "date": [f"1970-01-{i+1:02d}" for i in range(10)],
        }
    )
    assert _daily_sharpe(eq) == 0.0


def test_daily_sharpe_zero_for_constant_growth() -> None:
    # Constant +0.1% daily growth has zero realised vol, so the Sharpe
    # formula short-circuits to 0 via the σ ≤ 1e-12 guard.
    days = 60
    eq_values = [(1.001 ** i) for i in range(days)]
    eq = pl.DataFrame(
        {
            "ts_ms": [i * MS_PER_DAY for i in range(days)],
            "equity": eq_values,
            "drawdown": [0.0] * days,
            "basket_return": [0.001] * days,
            "date": [f"1970-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}" for i in range(days)],
        }
    )
    assert _daily_sharpe(eq) == 0.0


def test_summarize_trade_backtest_emits_daily_sharpe_only() -> None:
    config = TradeLifecycleConfig(rebalance_days=7, hold_days=7, score="test")
    trades = pl.DataFrame(
        [
            _trade(basket_id="B1", exit_day=1, net_return=0.05),
            _trade(basket_id="B2", exit_day=8, net_return=-0.02),
            _trade(basket_id="B3", exit_day=15, net_return=0.04),
            _trade(basket_id="B4", exit_day=22, net_return=-0.01),
            _trade(basket_id="B5", exit_day=29, net_return=0.03),
        ]
    )
    baskets = summarize_baskets(trades, config=config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=config)

    assert "sharpe_like" in summary
    assert math.isfinite(summary["sharpe_like"])


def test_daily_sharpe_handles_short_series() -> None:
    eq = pl.DataFrame(
        {
            "ts_ms": [0],
            "equity": [1.0],
            "drawdown": [0.0],
            "basket_return": [0.0],
            "date": ["1970-01-01"],
        }
    )
    assert _daily_sharpe(eq) == 0.0


def test_daily_sharpe_handles_empty() -> None:
    eq = pl.DataFrame(
        {
            "ts_ms": pl.Series([], dtype=pl.Int64),
            "equity": pl.Series([], dtype=pl.Float64),
            "drawdown": pl.Series([], dtype=pl.Float64),
            "basket_return": pl.Series([], dtype=pl.Float64),
        }
    )
    assert _daily_sharpe(eq) == 0.0


def test_daily_sharpe_ignores_non_finite() -> None:
    eq = pl.DataFrame(
        {
            "ts_ms": [0, MS_PER_DAY, 2 * MS_PER_DAY, 3 * MS_PER_DAY],
            "equity": [1.0, 1.01, 1.02, 1.03],
            "drawdown": [0.0, 0.0, 0.0, 0.0],
            "basket_return": [0.01, 0.01, 0.01, 0.01],
            "date": ["1970-01-01", "1970-01-02", "1970-01-03", "1970-01-04"],
        }
    )
    s = _daily_sharpe(eq)
    assert math.isfinite(s)


def test_daily_sharpe_single_diff_returns_zero() -> None:
    # Two equity points produce a single diff. ddof=1 → division by zero, so
    # the helper must short-circuit to 0 rather than emitting NaN/inf.
    eq = pl.DataFrame(
        {
            "ts_ms": [0, MS_PER_DAY],
            "equity": [1.0, 1.05],
            "drawdown": [0.0, 0.0],
            "basket_return": [0.05, 0.05],
            "date": ["1970-01-01", "1970-01-02"],
        }
    )
    assert _daily_sharpe(eq) == 0.0


def test_daily_sharpe_buckets_by_calendar_day_not_intraday_exit_time() -> None:
    """Production exits carry intra-day wall-clock timestamps, not midnight, so the
    Sharpe must forward-fill on TRUE calendar days. A hand-rolled grid stepping
    MS_PER_DAY off the first exit's clock time mis-buckets and injects spurious 0%
    days: with non-midnight exits on three consecutive days the correct series is
    [+50%, -20%], the stepped grid gives [0%, +50%].
    """
    eq = pl.DataFrame(
        {
            # 1970-01-01 01:00, 1970-01-02 23:00, 1970-01-03 06:00 (non-midnight)
            "ts_ms": [
                1 * MS_PER_HOUR,
                MS_PER_DAY + 23 * MS_PER_HOUR,
                2 * MS_PER_DAY + 6 * MS_PER_HOUR,
            ],
            "equity": [1.0, 1.5, 1.2],
            "drawdown": [0.0, 0.0, 0.0],
            "basket_return": [0.0, 0.5, -0.2],
            "date": ["1970-01-01", "1970-01-02", "1970-01-03"],
        }
    )
    expected = annualized_sharpe([0.5, -0.2])  # true calendar-day forward-fill
    buggy = annualized_sharpe([0.0, 0.5])      # what the old intraday grid produced
    got = _daily_sharpe(eq)
    assert math.isclose(got, expected, rel_tol=1e-9)
    assert not math.isclose(got, buggy, rel_tol=1e-9)


def test_sparse_strategy_daily_sharpe_is_finite_and_unbiased_by_firing_rate() -> None:
    """Two strategies with identical PnL on identical calendar days but different
    ``rebalance_days`` must produce the same ``sharpe_like``: the metric annualises
    off the actual daily series, not the assumed firing rate.
    """
    rows = []
    for i in range(24):
        rows.append(_trade(basket_id=f"V{i}", exit_day=15 * (i + 1),
                           net_return=0.05 if i % 2 == 0 else -0.02))
    trades = pl.DataFrame(rows)

    config_dense = TradeLifecycleConfig(rebalance_days=3, hold_days=3, score="test")
    config_sparse = TradeLifecycleConfig(rebalance_days=14, hold_days=14, score="test")
    baskets_d = summarize_baskets(trades, config=config_dense)
    baskets_s = summarize_baskets(trades, config=config_sparse)
    eq_d = build_equity_curve(baskets_d)
    eq_s = build_equity_curve(baskets_s)
    sd = summarize_trade_backtest(trades, baskets_d, eq_d, config=config_dense)
    ss = summarize_trade_backtest(trades, baskets_s, eq_s, config=config_sparse)
    assert math.isfinite(sd["sharpe_like"])
    assert math.isfinite(ss["sharpe_like"])
    # Same daily PnL grid + same equity curve regardless of TradeLifecycleConfig
    # rebalance_days, so the honest Sharpe collapses to one value.
    assert abs(sd["sharpe_like"] - ss["sharpe_like"]) < 1e-9
