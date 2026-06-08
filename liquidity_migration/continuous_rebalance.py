"""Daily-rebalance accounting for the continuous-fade research candidate.

This module is the package-level implementation of the decomposed accounting
that the scout script uses:

1. split the fixed-notional MTM ledger into gross MTM, entry cost, funding, and
   active-open gross;
2. apply a causal daily portfolio scale from prior strategy returns;
3. re-price entry impact at the scaled notional;
4. charge turnover for resizing already-open exposure.

It is still a research/backtest accounting engine, not a live order router.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import polars as pl

MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class ContinuousRebalanceRule:
    realized_vol_window_days: int = 90
    target_daily_vol: float = 0.025
    max_scale: float = 4.0
    drawdown_half_threshold: float | None = -0.04
    drawdown_zero_threshold: float | None = None
    resize_cost_bps: float = 10.0
    strategy_momentum_window_days: int = 180
    strategy_momentum_min_return: float = 0.02
    strategy_momentum_scale_when_below: float = 0.0


@dataclass(frozen=True)
class ContinuousRebalanceComponents:
    days: list[int]
    raw_by_day: dict[int, float]
    gross_by_day: dict[int, float]
    cost_events: dict[int, list[tuple[float, float, float]]]
    funding_by_day: dict[int, float]
    active_gross_start: dict[int, float]
    impact_exponent: float


@dataclass(frozen=True)
class ContinuousRebalanceResizePlan:
    """One desired live/paper resize action for an already-open continuous short."""

    trade_id: str
    symbol: str
    side: str
    reduce_only: bool
    qty: float
    current_notional_usdt: float
    target_notional_usdt: float
    delta_notional_usdt: float
    reason: str


@dataclass(frozen=True)
class ContinuousRebalanceScaleState:
    """Prior state required to compute the next live/paper target scale."""

    prior_raw_returns: tuple[float, ...]
    prior_scaled_equity: float = 1.0
    prior_scaled_peak: float = 1.0


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):
        return default
    return out


def fixed_cost_bps(config: dict[str, Any]) -> float:
    """Fixed round-trip bps before impact, honoring the cost stress multiplier."""
    mult = max(float(config.get("round_trip_cost_multiplier", 1.0)), 0.0)
    if config.get("flat_round_trip_bps") is not None:
        return float(config["flat_round_trip_bps"]) * mult
    return 2.0 * (float(config.get("taker_fee_bps", 3.0)) + float(config.get("spread_bps", 3.0))) * mult


def decompose_continuous_components(
    trades: pl.DataFrame,
    mtm_returns: pl.DataFrame,
    config: dict[str, Any],
) -> ContinuousRebalanceComponents:
    """Decompose a fixed-notional continuous MTM ledger into scalable components."""
    raw_by_day = {int(ts): float(ret) for ts, ret in mtm_returns.select("ts_ms", "basket_return").iter_rows()}
    base_cost_by_day: dict[int, float] = defaultdict(float)
    base_funding_by_day: dict[int, float] = defaultdict(float)
    cost_events: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    active_gross_start: dict[int, float] = {}
    fixed_bps = fixed_cost_bps(config)
    impact_exponent = float(config.get("impact_exponent", 0.5))
    days = sorted(raw_by_day)

    for row in trades.to_dicts():
        w = abs(float(row.get("notional_weight") or 0.0))
        entry_day = (int(row["entry_ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
        exit_day = (int(row["exit_ts_ms"]) // MS_PER_DAY) * MS_PER_DAY
        cost_r = float(row.get("cost_return") or 0.0)
        funding_r = float(row.get("funding_return") or 0.0)
        base_cost_by_day[entry_day] += cost_r
        base_funding_by_day[exit_day] += funding_r
        old_cost_bps = -cost_r / max(w, 1e-12) * 10_000.0
        impact_bps = 0.0 if config.get("flat_round_trip_bps") is not None else max(old_cost_bps - fixed_bps, 0.0)
        cost_events[entry_day].append((w, fixed_bps, impact_bps))

    for day in days:
        active = trades.filter((pl.col("entry_ts_ms") < day) & (pl.col("exit_ts_ms") > day))
        active_gross_start[day] = float(active["notional_weight"].abs().sum()) if not active.is_empty() else 0.0

    gross_by_day = {
        day: raw_by_day.get(day, 0.0) - base_cost_by_day.get(day, 0.0) - base_funding_by_day.get(day, 0.0)
        for day in days
    }
    return ContinuousRebalanceComponents(
        days=days,
        raw_by_day=raw_by_day,
        gross_by_day=gross_by_day,
        cost_events=dict(cost_events),
        funding_by_day=dict(base_funding_by_day),
        active_gross_start=active_gross_start,
        impact_exponent=impact_exponent,
    )


def scaled_entry_cost(events: list[tuple[float, float, float]], scale: float, impact_exponent: float) -> float:
    """Entry cost after scaling; fixed bps scale linearly, impact bps rise with participation."""
    total = 0.0
    for old_w, fixed_bps, impact_bps in events:
        new_w = old_w * scale
        new_bps = fixed_bps + impact_bps * (scale ** impact_exponent)
        total -= new_w * new_bps / 10_000.0
    return total


def compute_continuous_rebalance_scale(
    state: ContinuousRebalanceScaleState,
    rule: ContinuousRebalanceRule,
) -> float:
    """Compute today's target scale from prior-only state.

    This is the live/paper equivalent of the scale calculation inside the
    backtest loop. ``prior_raw_returns`` must exclude the day being sized.
    """
    prior = [float(x) for x in state.prior_raw_returns]
    scale = 1.0
    vol_window = int(rule.realized_vol_window_days)
    vol_hist = prior[-vol_window:] if vol_window > 0 else []
    if len(vol_hist) >= max(5, vol_window // 3):
        mean = sum(vol_hist) / len(vol_hist)
        variance = sum((x - mean) ** 2 for x in vol_hist) / max(1, len(vol_hist) - 1)
        scale = min(rule.max_scale, rule.target_daily_vol / max(variance**0.5, 1e-6))

    equity = max(float(state.prior_scaled_equity), 1e-12)
    peak = max(float(state.prior_scaled_peak), 1e-12)
    prior_dd = equity / peak - 1.0
    if rule.drawdown_zero_threshold is not None and prior_dd <= rule.drawdown_zero_threshold:
        scale = 0.0
    elif rule.drawdown_half_threshold is not None and prior_dd <= rule.drawdown_half_threshold:
        scale *= 0.5

    trend_window = int(rule.strategy_momentum_window_days)
    if trend_window > 0:
        trend_hist = prior[-trend_window:]
        min_trend_obs = max(5, trend_window // 3)
        if len(trend_hist) >= min_trend_obs and sum(trend_hist) < rule.strategy_momentum_min_return:
            scale *= max(float(rule.strategy_momentum_scale_when_below), 0.0)

    return max(float(scale), 0.0)


def plan_continuous_rebalance_resizes(
    open_trades: list[dict[str, Any]],
    *,
    price_by_symbol: dict[str, float],
    equity_usdt: float,
    base_notional_pct_equity: float,
    target_scale: float,
    min_resize_notional_usdt: float = 5.0,
) -> list[ContinuousRebalanceResizePlan]:
    """Plan resize orders needed to match a daily rebalance scale.

    The continuous demo ledger stores one row per open short. The promoted
    research rule scales the per-name base notional, so each open row targets:

    ``equity * base_notional_pct_equity / 100 * target_scale``.

    Positive delta means increase the short with a non-reduce-only Sell. Negative
    delta means reduce the short with a reduce-only Buy. This planner deliberately
    does not round to venue qty steps or submit orders; the live executor must
    apply contract filters immediately before placement.
    """
    base = max(_finite_float(equity_usdt), 0.0) * max(_finite_float(base_notional_pct_equity), 0.0) / 100.0
    scale = max(_finite_float(target_scale), 0.0)
    target = base * scale
    floor = max(_finite_float(min_resize_notional_usdt), 0.0)
    plans: list[ContinuousRebalanceResizePlan] = []

    for trade in open_trades:
        symbol = str(trade.get("symbol") or "")
        if not symbol:
            continue
        price = _finite_float(price_by_symbol.get(symbol))
        qty = abs(_finite_float(trade.get("qty")))
        if price <= 0.0 or qty <= 0.0:
            continue
        current = qty * price
        delta = target - current
        if abs(delta) < floor:
            continue
        if delta > 0.0:
            side = "Sell"
            reduce_only = False
            order_qty = delta / price
            reason = "rebalance_increase"
        else:
            side = "Buy"
            reduce_only = True
            order_qty = min(qty, abs(delta) / price)
            reason = "rebalance_reduce"
        if order_qty <= 0.0:
            continue
        plans.append(
            ContinuousRebalanceResizePlan(
                trade_id=str(trade.get("trade_id") or ""),
                symbol=symbol,
                side=side,
                reduce_only=reduce_only,
                qty=order_qty,
                current_notional_usdt=current,
                target_notional_usdt=target,
                delta_notional_usdt=delta,
                reason=reason,
            )
        )
    return plans


def apply_rebalance_rule(
    components: ContinuousRebalanceComponents,
    rule: ContinuousRebalanceRule,
) -> pl.DataFrame:
    """Apply a causal daily scale rule and rebuild decomposed equity."""
    out: list[tuple[int, float, float, float, float, float, float, float]] = []
    equity = 1.0
    peak = 1.0
    prev_scale = 1.0
    days = components.days
    raw_rets = [components.raw_by_day[d] for d in days]

    for idx, day in enumerate(days):
        scale = compute_continuous_rebalance_scale(
            ContinuousRebalanceScaleState(
                prior_raw_returns=tuple(raw_rets[:idx]),
                prior_scaled_equity=equity,
                prior_scaled_peak=peak,
            ),
            rule,
        )

        gross = scale * components.gross_by_day.get(day, 0.0)
        funding = scale * components.funding_by_day.get(day, 0.0)
        entry_cost = scaled_entry_cost(
            components.cost_events.get(day, []),
            scale,
            components.impact_exponent,
        )
        resize_cost = (
            -abs(scale - prev_scale)
            * components.active_gross_start.get(day, 0.0)
            * rule.resize_cost_bps
            / 10_000.0
        )
        basket_return = gross + funding + entry_cost + resize_cost
        equity *= 1.0 + basket_return
        peak = max(peak, equity)
        out.append((day, basket_return, scale, gross, entry_cost, funding, resize_cost, equity))
        prev_scale = scale

    return pl.DataFrame(
        out,
        schema=[
            "ts_ms",
            "basket_return",
            "scale",
            "gross_return",
            "entry_cost_return",
            "funding_return",
            "resize_cost_return",
            "equity",
        ],
        orient="row",
    ).with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))


def rebalance_rule_id(rule: ContinuousRebalanceRule) -> str:
    ddh = "off" if rule.drawdown_half_threshold is None else f"{rule.drawdown_half_threshold:g}"
    ddz = "off" if rule.drawdown_zero_threshold is None else f"{rule.drawdown_zero_threshold:g}"
    if rule.strategy_momentum_window_days <= 0:
        trend = "off"
    else:
        trend = (
            f"tw{rule.strategy_momentum_window_days}"
            f"_tm{rule.strategy_momentum_min_return:g}"
            f"_ts{rule.strategy_momentum_scale_when_below:g}"
        )
    return (
        f"w{rule.realized_vol_window_days}"
        f"_tv{rule.target_daily_vol:g}"
        f"_max{rule.max_scale:g}"
        f"_ddh{ddh}_ddz{ddz}_{trend}"
    )
