from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.continuous_rebalance import (
    MS_PER_DAY,
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    ContinuousRebalanceScaleState,
    apply_rebalance_rule,
    compute_continuous_rebalance_scale,
    decompose_continuous_components,
    plan_continuous_rebalance_resizes,
    rebalance_rule_id,
    scaled_entry_cost,
)


def _components(raw_returns: list[float]) -> ContinuousRebalanceComponents:
    days = [idx * MS_PER_DAY for idx in range(len(raw_returns))]
    raw_by_day = dict(zip(days, raw_returns, strict=True))
    return ContinuousRebalanceComponents(
        days=days,
        raw_by_day=raw_by_day,
        gross_by_day=raw_by_day.copy(),
        cost_events={},
        funding_by_day={},
        active_gross_start={day: 0.0 for day in days},
        impact_exponent=0.5,
    )


def test_scaled_entry_cost_reprices_impact_with_scaled_notional() -> None:
    events = [(0.02, 12.0, 50.0)]
    base = scaled_entry_cost(events, scale=1.0, impact_exponent=0.5)
    scaled = scaled_entry_cost(events, scale=4.0, impact_exponent=0.5)

    assert scaled < base * 4.0


def test_apply_rebalance_rule_uses_prior_returns_for_scale() -> None:
    base = _components([0.01, -0.01, 0.015, -0.015, 0.012, 0.00])
    shocked_current_day = _components([0.01, -0.01, 0.015, -0.015, 0.012, 0.50])
    rule = ContinuousRebalanceRule(
        realized_vol_window_days=15,
        target_daily_vol=0.025,
        max_scale=4.0,
        drawdown_half_threshold=None,
        resize_cost_bps=0.0,
        strategy_momentum_window_days=0,
    )

    base_scaled = apply_rebalance_rule(base, rule)
    shocked_scaled = apply_rebalance_rule(shocked_current_day, rule)

    assert base_scaled["scale"].to_list()[-1] == pytest.approx(shocked_scaled["scale"].to_list()[-1])


def test_apply_rebalance_rule_hard_offs_below_strategy_momentum_hurdle() -> None:
    components = _components([0.0, 0.0, 0.0, 0.0, 0.0, 0.01])
    rule = ContinuousRebalanceRule(
        realized_vol_window_days=30,
        drawdown_half_threshold=None,
        resize_cost_bps=0.0,
        strategy_momentum_window_days=6,
        strategy_momentum_min_return=0.02,
        strategy_momentum_scale_when_below=0.0,
    )

    scaled = apply_rebalance_rule(components, rule)

    assert scaled["scale"].to_list()[:5] == [1.0] * 5
    assert scaled["scale"].to_list()[5] == 0.0


def test_compute_scale_matches_backtest_loop_day_by_day() -> None:
    returns = [0.01, -0.01, 0.02, -0.02, 0.015, 0.005, -0.01, 0.03, -0.005]
    components = _components(returns)
    rule = ContinuousRebalanceRule(
        realized_vol_window_days=6,
        target_daily_vol=0.025,
        max_scale=4.0,
        drawdown_half_threshold=-0.04,
        resize_cost_bps=0.0,
        strategy_momentum_window_days=6,
        strategy_momentum_min_return=0.0,
        strategy_momentum_scale_when_below=0.5,
    )

    scaled = apply_rebalance_rule(components, rule)
    equity = 1.0
    peak = 1.0
    expected_scales: list[float] = []
    for idx, row in enumerate(scaled.to_dicts()):
        scale = compute_continuous_rebalance_scale(
            ContinuousRebalanceScaleState(
                prior_raw_returns=tuple(returns[:idx]),
                prior_scaled_equity=equity,
                prior_scaled_peak=peak,
            ),
            rule,
        )
        expected_scales.append(scale)
        equity *= 1.0 + float(row["basket_return"])
        peak = max(peak, equity)

    assert scaled["scale"].to_list() == pytest.approx(expected_scales)


def test_compute_scale_drawdown_uses_prior_scaled_equity_state() -> None:
    rule = ContinuousRebalanceRule(
        realized_vol_window_days=30,
        drawdown_half_threshold=-0.04,
        strategy_momentum_window_days=0,
    )

    normal = compute_continuous_rebalance_scale(
        ContinuousRebalanceScaleState(prior_raw_returns=tuple([0.0] * 10), prior_scaled_equity=1.0, prior_scaled_peak=1.0),
        rule,
    )
    cut = compute_continuous_rebalance_scale(
        ContinuousRebalanceScaleState(prior_raw_returns=tuple([0.0] * 10), prior_scaled_equity=0.95, prior_scaled_peak=1.0),
        rule,
    )

    assert cut == pytest.approx(normal * 0.5)


def test_compute_scale_strategy_momentum_uses_only_prior_raw_returns() -> None:
    rule = ContinuousRebalanceRule(
        realized_vol_window_days=30,
        drawdown_half_threshold=None,
        strategy_momentum_window_days=6,
        strategy_momentum_min_return=0.02,
        strategy_momentum_scale_when_below=0.0,
    )

    off = compute_continuous_rebalance_scale(
        ContinuousRebalanceScaleState(prior_raw_returns=tuple([0.0] * 5)),
        rule,
    )
    on = compute_continuous_rebalance_scale(
        ContinuousRebalanceScaleState(prior_raw_returns=tuple([0.0] * 5 + [0.03])),
        rule,
    )

    assert off == 0.0
    assert on == 1.0


def test_decompose_components_splits_gross_cost_funding_and_open_gross() -> None:
    d = MS_PER_DAY
    trades = pl.DataFrame(
        {
            "entry_ts_ms": [0],
            "exit_ts_ms": [2 * d],
            "notional_weight": [0.02],
            "cost_return": [-0.001],
            "funding_return": [-0.0003],
        }
    )
    mtm = pl.DataFrame(
        {
            "ts_ms": [0, d, 2 * d],
            "basket_return": [-0.0015, 0.002, -0.0003],
        }
    )
    config = {"taker_fee_bps": 3.0, "spread_bps": 3.0, "impact_exponent": 0.5}

    components = decompose_continuous_components(trades, mtm, config)

    assert components.gross_by_day[0] == pytest.approx(-0.0005)
    assert components.gross_by_day[d] == pytest.approx(0.002)
    assert components.gross_by_day[2 * d] == pytest.approx(0.0)
    assert components.funding_by_day[2 * d] == pytest.approx(-0.0003)
    assert components.active_gross_start[d] == pytest.approx(0.02)
    assert components.active_gross_start[2 * d] == pytest.approx(0.0)


def test_rebalance_rule_id_pins_promoted_hurdle_rule() -> None:
    rule = ContinuousRebalanceRule()
    assert rebalance_rule_id(rule) == "w90_tv0.025_max4_ddh-0.04_ddzoff_tw180_tm0.02_ts0"


def test_plan_rebalance_resizes_reduces_oversized_short() -> None:
    plans = plan_continuous_rebalance_resizes(
        [{"trade_id": "t1", "symbol": "ABCUSDT", "qty": "3"}],
        price_by_symbol={"ABCUSDT": 100.0},
        equity_usdt=10_000.0,
        base_notional_pct_equity=2.0,
        target_scale=1.0,
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.side == "Buy"
    assert plan.reduce_only is True
    assert plan.qty == pytest.approx(1.0)
    assert plan.current_notional_usdt == pytest.approx(300.0)
    assert plan.target_notional_usdt == pytest.approx(200.0)
    assert plan.reason == "rebalance_reduce"


def test_plan_rebalance_resizes_increases_undersized_short() -> None:
    plans = plan_continuous_rebalance_resizes(
        [{"trade_id": "t1", "symbol": "ABCUSDT", "qty": "1"}],
        price_by_symbol={"ABCUSDT": 100.0},
        equity_usdt=10_000.0,
        base_notional_pct_equity=2.0,
        target_scale=2.0,
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.side == "Sell"
    assert plan.reduce_only is False
    assert plan.qty == pytest.approx(3.0)
    assert plan.current_notional_usdt == pytest.approx(100.0)
    assert plan.target_notional_usdt == pytest.approx(400.0)
    assert plan.reason == "rebalance_increase"


def test_plan_rebalance_resizes_zero_scale_covers_without_overbuying() -> None:
    plans = plan_continuous_rebalance_resizes(
        [{"trade_id": "t1", "symbol": "ABCUSDT", "qty": "3"}],
        price_by_symbol={"ABCUSDT": 100.0},
        equity_usdt=10_000.0,
        base_notional_pct_equity=2.0,
        target_scale=0.0,
    )

    assert len(plans) == 1
    assert plans[0].side == "Buy"
    assert plans[0].reduce_only is True
    assert plans[0].qty == pytest.approx(3.0)


def test_plan_rebalance_resizes_skips_tiny_and_unmarked_positions() -> None:
    plans = plan_continuous_rebalance_resizes(
        [
            {"trade_id": "tiny", "symbol": "ABCUSDT", "qty": "2.01"},
            {"trade_id": "missing", "symbol": "MISSUSDT", "qty": "5"},
        ],
        price_by_symbol={"ABCUSDT": 100.0},
        equity_usdt=10_000.0,
        base_notional_pct_equity=2.0,
        target_scale=1.0,
        min_resize_notional_usdt=5.0,
    )

    assert plans == []
