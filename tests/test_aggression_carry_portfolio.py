from __future__ import annotations

import math
from pathlib import Path

import polars as pl

from aggression_carry.config import PortfolioConfig, SignalConfig
from aggression_carry.features import compute_features_from_store
from aggression_carry.ingestion import generate_fixture_data
from aggression_carry.portfolio import (
    _compound_returns,
    _period_returns_by_month,
    _target_weights,
    compute_robustness,
    run_cost_scenario,
    run_detailed_cost_scenario,
    run_portfolio_backtest,
)


def test_target_weights_use_short_quantile_and_preserve_signs_after_net_cap() -> None:
    rows = [
        {"symbol": f"S{i}", "composite_score": score}
        for i, score in enumerate([-3.0, -2.0, -1.0, 0.5, 1.0, 2.0, 3.0, 4.0])
    ]
    weights = _target_weights(
        rows,
        portfolio=PortfolioConfig(max_net_exposure_abs=0.0, max_single_position_weight=0.20),
        signals=SignalConfig(min_abs_score_entry=0.0, long_quantile=0.50, short_quantile=0.25),
    )

    assert sum(1 for weight in weights.values() if weight > 0) == 4
    assert sum(1 for weight in weights.values() if weight < 0) == 2
    assert all(abs(weight) <= 0.20 for weight in weights.values())
    assert all(weight > 0 for symbol, weight in weights.items() if symbol in {"S4", "S5", "S6", "S7"})
    assert all(weight < 0 for symbol, weight in weights.items() if symbol in {"S0", "S1"})


def test_portfolio_selection_does_not_filter_on_future_returns() -> None:
    df = pl.DataFrame(
        [
            {"ts_ms": 0, "symbol": "A", "composite_score": -3.0, "forward_return_4h": float("nan"), "funding_rate_8h_equiv": 0.0},
            {"ts_ms": 0, "symbol": "B", "composite_score": -2.0, "forward_return_4h": math.log(0.99), "funding_rate_8h_equiv": 0.0},
            {"ts_ms": 0, "symbol": "C", "composite_score": 0.0, "forward_return_4h": math.log(1.00), "funding_rate_8h_equiv": 0.0},
            {"ts_ms": 0, "symbol": "D", "composite_score": 2.0, "forward_return_4h": math.log(1.01), "funding_rate_8h_equiv": 0.0},
            {"ts_ms": 0, "symbol": "E", "composite_score": 3.0, "forward_return_4h": math.log(1.02), "funding_rate_8h_equiv": 0.0},
        ]
    )

    result = run_cost_scenario(
        df,
        scenario="test",
        cost_multiplier=1.0,
        portfolio_config=PortfolioConfig(max_single_position_weight=0.50),
        signal_config=SignalConfig(min_abs_score_entry=0.0, long_quantile=0.40, short_quantile=0.40),
    )

    assert result.selected_missing_forward_returns == 1


def test_portfolio_backtest_reports_funding_pnl(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)
    compute_features_from_store(tmp_path)

    payload = run_portfolio_backtest(tmp_path)

    base = next(item for item in payload["scenarios"] if item["scenario"] == "base")
    assert base["funding_pnl"] != 0.0
    assert "selected_missing_forward_returns" in base


def test_detailed_scenario_ledger_is_additive_and_price_pnl_is_separate() -> None:
    df = pl.DataFrame(
        [
            {"ts_ms": 0, "symbol": "A", "composite_score": -3.0, "forward_return_4h": math.log(0.98), "funding_rate_8h_equiv": 0.001},
            {"ts_ms": 0, "symbol": "B", "composite_score": -2.0, "forward_return_4h": math.log(0.99), "funding_rate_8h_equiv": 0.001},
            {"ts_ms": 0, "symbol": "C", "composite_score": 2.0, "forward_return_4h": math.log(1.02), "funding_rate_8h_equiv": -0.001},
            {"ts_ms": 0, "symbol": "D", "composite_score": 3.0, "forward_return_4h": math.log(1.03), "funding_rate_8h_equiv": -0.001},
        ]
    )

    run = run_detailed_cost_scenario(
        df,
        scenario="base",
        cost_multiplier=1.0,
        portfolio_config=PortfolioConfig(max_single_position_weight=0.50),
        signal_config=SignalConfig(min_abs_score_entry=0.0, long_quantile=0.50, short_quantile=0.50),
    )

    period = run.periods[0]
    position_total = sum(row["total_pnl"] for row in run.positions)
    price_total = sum(row["price_pnl"] for row in run.positions)
    assert period["period_return"] == position_total
    assert run.summary.long_pnl + run.summary.short_pnl == price_total
    assert run.summary.gross_alpha_pnl == run.summary.long_pnl + run.summary.short_pnl + run.summary.funding_pnl
    assert run.summary.total_cost_pnl == run.summary.fee_pnl + run.summary.slippage_pnl
    assert run.symbol_attribution
    assert "abs_pnl_share" in run.symbol_attribution[0]


def test_portfolio_payload_contains_period_symbol_month_and_robustness(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)
    compute_features_from_store(tmp_path)

    payload = run_portfolio_backtest(tmp_path)

    assert payload["periods"]
    assert payload["positions"]
    assert payload["symbol_attribution"]
    assert payload["monthly_attribution"]
    assert "return_excluding_btc_eth_sol" in payload["robustness"]
    assert "survives_excluding_best_month" in {gate["gate"] for gate in payload["acceptance_gates"]}
    assert "fees_share_gross_alpha" in payload["scenarios"][0]


def test_robustness_excluding_best_month_uses_remaining_months() -> None:
    periods = [
        {"scenario": "base", "ts_ms": 0, "month": "2025-01", "period_return": 0.10},
        {"scenario": "base", "ts_ms": 1, "month": "2025-02", "period_return": 0.02},
        {"scenario": "base", "ts_ms": 2, "month": "2025-03", "period_return": 0.03},
    ]
    positions = [
        {"scenario": "base", "ts_ms": row["ts_ms"], "month": row["month"], "symbol": "XRPUSDT", "total_pnl": row["period_return"]}
        for row in periods
    ]
    summaries = [
        {"scenario": "base", "total_return": _compound_returns([0.10, 0.02, 0.03]), "long_pnl": 0.1, "short_pnl": 0.1, "fees_share_gross_alpha": 0.1},
        {"scenario": "2x_costs", "total_return": 0.01},
    ]
    symbols = [{"scenario": "base", "symbol": "XRPUSDT", "net_pnl": 0.15, "abs_pnl_share": 1.0, "total_pnl_share": 1.0}]

    robustness = compute_robustness(summaries, periods, positions, symbols)

    assert robustness["best_month"] == "2025-01"
    assert robustness["return_excluding_best_month"] == _compound_returns([0.02, 0.03])
    assert robustness["return_excluding_btc_eth_sol"] == _compound_returns([0.10, 0.02, 0.03])


def test_period_returns_by_month_compounds_each_month() -> None:
    periods = [
        {"month": "2025-01", "period_return": 0.01},
        {"month": "2025-01", "period_return": 0.02},
        {"month": "2025-02", "period_return": -0.01},
    ]

    monthly = _period_returns_by_month(periods)

    assert monthly["2025-01"] == _compound_returns([0.01, 0.02])
    assert math.isclose(monthly["2025-02"], -0.01)
