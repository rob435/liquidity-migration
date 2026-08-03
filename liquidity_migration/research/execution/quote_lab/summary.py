"""Aggregate shadow-quote outcomes into per symbol-and-side summaries."""

from __future__ import annotations

from statistics import median
from typing import Any, Iterable

from liquidity_migration.research.execution.quote_lab.shadow import (
    MARKOUT_FIELDS,
    MARKOUT_HORIZONS_S,
    ShadowOutcome,
)


def _horizon_key(horizon_s: float) -> str:
    return f"{horizon_s:g}s"


def summarize_outcomes(
    outcomes: Iterable[ShadowOutcome],
    *,
    taker_fee_bp: float = 5.5,
) -> list[dict[str, Any]]:
    """One json-safe dict per symbol and side.

    Adverse markout means are conditioned on a fill (optimistic bound, which
    contains every conservative fill); positive = the price moved against our
    side after the fill. The cost comparison: a maker fill at the touch earns
    the half-spread minus the adverse drift, a taker pays the half-spread
    plus ``taker_fee_bp``.
    """

    groups: dict[tuple[str, str], list[ShadowOutcome]] = {}
    for outcome in outcomes:
        groups.setdefault((outcome.symbol, outcome.side), []).append(outcome)
    rows: list[dict[str, Any]] = []
    for symbol, side in sorted(groups):
        group = groups[(symbol, side)]
        attempts = len(group)
        cons_times = [
            outcome.time_to_fill_s_conservative
            for outcome in group
            if outcome.time_to_fill_s_conservative is not None
        ]
        opt_times = [
            outcome.time_to_fill_s_optimistic for outcome in group if outcome.time_to_fill_s_optimistic is not None
        ]
        mean_spread_bp = sum(outcome.decision_spread_bp for outcome in group) / attempts
        half_spread_bp = mean_spread_bp / 2.0
        filled = [outcome for outcome in group if outcome.filled_optimistic]
        adverse_by_horizon: dict[str, float | None] = {}
        maker_edge_by_horizon: dict[str, float | None] = {}
        for horizon_s in MARKOUT_HORIZONS_S:
            values = [
                value for outcome in filled if (value := getattr(outcome, MARKOUT_FIELDS[horizon_s])) is not None
            ]
            mean_adverse = sum(values) / len(values) if values else None
            adverse_by_horizon[_horizon_key(horizon_s)] = mean_adverse
            maker_edge_by_horizon[_horizon_key(horizon_s)] = (
                half_spread_bp - mean_adverse if mean_adverse is not None else None
            )
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "attempts": attempts,
                "fill_rate_conservative": sum(outcome.filled_conservative for outcome in group) / attempts,
                "fill_rate_optimistic": sum(outcome.filled_optimistic for outcome in group) / attempts,
                "traded_through_rate": sum(outcome.traded_through for outcome in group) / attempts,
                "median_time_to_fill_s_conservative": median(cons_times) if cons_times else None,
                "median_time_to_fill_s_optimistic": median(opt_times) if opt_times else None,
                "mean_decision_spread_bp": mean_spread_bp,
                "mean_adverse_markout_bp": adverse_by_horizon,
                "half_spread_bp": half_spread_bp,
                "taker_fee_bp": taker_fee_bp,
                "taker_cost_bp": half_spread_bp + taker_fee_bp,
                "maker_edge_bp": maker_edge_by_horizon,
            }
        )
    return rows
