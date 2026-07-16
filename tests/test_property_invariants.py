"""Randomized invariants for core financial and PIT math."""
from __future__ import annotations

import math
import random

import polars as pl

from liquidity_migration._common import MS_PER_DAY
from liquidity_migration.continuous_rebalance import (
    ContinuousHedgeRule,
    ContinuousHedgeState,
    compute_continuous_hedge_ratio,
)
from liquidity_migration.config import TradeLifecycleConfig
from liquidity_migration.ingestion import normalize_funding_history
from liquidity_migration.trade_lifecycle import (
    _daily_sharpe,
    annualized_sharpe,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)

MS_PER_HOUR = MS_PER_DAY // 24


def _gen_trade(rng: random.Random, basket_id: str, exit_day: int, net_return: float) -> dict:
    """A minimal trade row summarize_baskets/build_equity_curve accept (mirrors the
    fixture in test_..._trade_lifecycle_sharpe). Exit hour is randomized to exercise the
    calendar-day bucketing."""
    exit_ms = exit_day * MS_PER_DAY + rng.randint(0, 23) * MS_PER_HOUR
    entry_ms = max(0, exit_ms - MS_PER_DAY)
    return {
        "trade_id": f"{basket_id}-t", "basket_id": basket_id,
        "entry_signal_ts_ms": entry_ms, "entry_ts_ms": entry_ms, "exit_ts_ms": exit_ms,
        "entry_date": "1970-01-01", "exit_date": "1970-01-02", "exit_month": "1970-01",
        "symbol": "BTCUSDT", "side": "long", "score": 0.0, "rank": 1,
        "entry_price": 100.0, "exit_price": 100.0 * (1.0 + net_return),
        "exit_reason": "take_profit", "planned_exit_ts_ms": exit_ms,
        "stop_price": 90.0, "take_profit_price": 130.0,
        "notional_weight": 1.0, "position_weight": 1.0,
        "gross_trade_return": net_return, "gross_return": net_return,
        "cost_return": 0.0, "funding_return": 0.0, "funding_mode": "ok", "funding_event_count": 0,
        "net_return": net_return, "mae": 0.0, "mfe": 0.0, "bars_held": 24, "hold_hours": 24.0,
        "actual_entry_delay_hours": 0.0, "pattern": "fomo_chase",
    }


def _summary(rng: random.Random, rets: list[float]) -> tuple[dict, pl.DataFrame]:
    days = sorted(rng.sample(range(1, 320), len(rets)))  # distinct exit calendar days
    trades = pl.DataFrame([_gen_trade(rng, f"B{i}", days[i], rets[i]) for i in range(len(rets))])
    cfg = TradeLifecycleConfig(rebalance_days=7, hold_days=7, score="test")
    baskets = summarize_baskets(trades, config=cfg)
    equity = build_equity_curve(baskets)
    return summarize_trade_backtest(trades, baskets, equity, config=cfg), equity


def test_equity_curve_accounting_identities() -> None:
    """max_drawdown = min(eq/cummax - 1) must always be <= 0; total_return must equal
    equity[-1]-1; sharpe_like must be finite. (These feed the three-tier decision rule.)"""
    rng = random.Random(31)
    for _ in range(120):
        rets = [rng.uniform(-0.3, 0.3) for _ in range(rng.randint(2, 25))]
        s, equity = _summary(rng, rets)
        assert s["max_drawdown"] <= 1e-12, s["max_drawdown"]           # drawdown never positive
        assert s["total_return"] > -1.0                                 # equity stays positive (rets > -1)
        assert math.isclose(s["total_return"], float(equity["equity"][-1] - 1.0), rel_tol=1e-9, abs_tol=1e-12)
        assert math.isfinite(s["sharpe_like"])


def test_all_positive_returns_have_zero_drawdown() -> None:
    """A strictly-increasing equity curve has exactly zero drawdown and positive return."""
    rng = random.Random(32)
    s, _ = _summary(rng, [rng.uniform(0.001, 0.05) for _ in range(12)])
    assert math.isclose(s["max_drawdown"], 0.0, abs_tol=1e-12)
    assert s["total_return"] > 0.0


def test_annualized_sharpe_scale_invariant_and_sign_flip() -> None:
    """Sharpe = mean/std is invariant to a positive scale and negates under a sign flip."""
    rng = random.Random(11)
    for _ in range(400):
        n = rng.randint(2, 40)
        xs = [rng.uniform(-0.1, 0.1) for _ in range(n)]
        base = annualized_sharpe(xs)
        if not math.isfinite(base) or base == 0.0:
            continue
        k = rng.uniform(0.01, 100.0)
        assert math.isclose(annualized_sharpe([k * x for x in xs]), base, rel_tol=1e-9), (k, xs)
        assert math.isclose(annualized_sharpe([-x for x in xs]), -base, rel_tol=1e-9), xs


def test_annualized_sharpe_degenerate_is_zero() -> None:
    assert annualized_sharpe([0.05]) == 0.0          # < 2 points
    assert annualized_sharpe([0.03, 0.03, 0.03]) == 0.0  # zero variance
    assert annualized_sharpe([]) == 0.0


def test_daily_sharpe_invariant_to_intraday_exit_hour() -> None:
    """_daily_sharpe must depend only on the calendar day + equity, NEVER the intra-day
    wall-clock hour of the exit. The iter-1 bug (intraday-stamped grid) violated this."""
    rng = random.Random(23)
    for _ in range(200):
        ndays = rng.randint(2, 30)
        days = sorted(rng.sample(range(0, 400), ndays))  # distinct calendar days, random gaps
        eqs, v = [], 1.0
        for _ in range(ndays):
            v *= 1.0 + rng.uniform(-0.05, 0.05)
            eqs.append(v)

        def _df(hours: list[int]) -> pl.DataFrame:
            return pl.DataFrame({
                "ts_ms": [d * MS_PER_DAY + h * MS_PER_HOUR for d, h in zip(days, hours)],
                "equity": eqs,
            })

        s_midnight = _daily_sharpe(_df([0] * ndays))
        s_random = _daily_sharpe(_df([rng.randint(0, 23) for _ in range(ndays)]))
        assert math.isclose(s_midnight, s_random, rel_tol=1e-9, abs_tol=1e-12), (days, eqs)


def test_normalize_funding_8h_equiv_finite_and_correct() -> None:
    """funding_rate_8h_equiv is always finite (0/negative interval clamps to the 8h
    default) and equals funding_rate * 480/clamped_interval (audit-iter6 fix)."""
    rng = random.Random(7)
    rows, expected = [], {}
    for i in range(500):
        sym = f"S{i}"
        rate = rng.uniform(-0.01, 0.01)
        c = rng.random()
        interval = 0 if c < 0.2 else (-rng.randint(1, 600) if c < 0.4 else rng.choice([60, 120, 240, 480, 720]))
        rows.append({"ts_ms": i, "symbol": sym, "funding_rate": rate, "funding_interval_min": interval})
        clamped = interval if interval > 0 else 480
        expected[sym] = rate * (480.0 / clamped)
    out = normalize_funding_history(pl.DataFrame(rows))
    for r in out.iter_rows(named=True):
        assert math.isfinite(r["funding_rate_8h_equiv"]), r
        assert math.isclose(r["funding_rate_8h_equiv"], expected[r["symbol"]], rel_tol=1e-9, abs_tol=1e-15), r


def test_hedge_ratio_bounded_by_cap_times_scale() -> None:
    """compute_continuous_hedge_ratio == clip(-beta,0,cap)*max(scale,0): always finite,
    in [0, cap*max(scale,0)], and exactly 0 for a non-positive target scale."""
    rng = random.Random(5)
    for _ in range(400):
        n = rng.randint(0, 120)
        raw = [rng.uniform(-0.1, 0.1) for _ in range(n)]
        hs = [rng.uniform(-0.1, 0.1) for _ in range(n)]
        cap = rng.uniform(0.5, 4.0)
        rule = ContinuousHedgeRule(
            beta_window_days=90, beta_min_obs=rng.randint(5, 60), hedge_cap=cap, cost_bps=5.0
        )
        scale = rng.uniform(-1.0, 4.0)
        state = ContinuousHedgeState(prior_raw_returns=tuple(raw), prior_hedge_returns=tuple(hs))
        r = compute_continuous_hedge_ratio(state, rule, scale)
        assert math.isfinite(r), (n, cap, scale)
        assert 0.0 <= r <= cap * max(scale, 0.0) + 1e-12, (r, cap, scale)
        if scale <= 0.0:
            assert r == 0.0
