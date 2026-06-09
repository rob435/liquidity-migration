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
class ContinuousHedgeRule:
    """Causal rolling-beta hedge leg (long hedge instrument against the short book).

    Beta is estimated on trailing LEDGER days strictly before the day being sized
    (per-unit book return vs hedge-instrument return), so the hedge ratio for day d
    uses data through d-1 only. ``beta_extra_lag_days`` shifts the estimation window
    one or more further days back (latency falsifier). The hedge is long-only:
    ``H(d) = clip(-beta, 0, hedge_cap) * scale(d)``.
    """

    beta_window_days: int = 90
    beta_min_obs: int = 60
    hedge_cap: float = 2.0
    cost_bps: float = 5.0
    beta_extra_lag_days: int = 0


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


def combine_continuous_components(
    pieces: dict[str, ContinuousRebalanceComponents],
    weights: dict[str, float],
) -> ContinuousRebalanceComponents:
    """Weighted combine of decomposed component ledgers (canonical implementation).

    Moved from the ensemble scout (`scripts/continuous_ensemble_rebalance_scout.py`),
    which now delegates here. Impact bps scale by ``weight ** impact_exponent``
    (participation shrinks with the slice of notional).
    """
    raw_by_day: dict[int, float] = defaultdict(float)
    gross_by_day: dict[int, float] = defaultdict(float)
    funding_by_day: dict[int, float] = defaultdict(float)
    active_gross_start: dict[int, float] = defaultdict(float)
    cost_events: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    impact_exponent = 0.5
    day_set: set[int] = set()

    for source, weight in weights.items():
        if weight <= 0.0:
            continue
        comp = pieces[source]
        impact_exponent = comp.impact_exponent
        day_set.update(comp.days)
        impact_weight = weight**comp.impact_exponent
        for day, value in comp.raw_by_day.items():
            raw_by_day[int(day)] += weight * float(value)
        for day, value in comp.gross_by_day.items():
            gross_by_day[int(day)] += weight * float(value)
        for day, value in comp.funding_by_day.items():
            funding_by_day[int(day)] += weight * float(value)
        for day, value in comp.active_gross_start.items():
            active_gross_start[int(day)] += weight * float(value)
        for day, events in comp.cost_events.items():
            for old_w, fixed_bps, impact_bps in events:
                cost_events[int(day)].append((old_w * weight, fixed_bps, impact_bps * impact_weight))

    days = sorted(day_set)
    return ContinuousRebalanceComponents(
        days=days,
        raw_by_day={day: raw_by_day.get(day, 0.0) for day in days},
        gross_by_day={day: gross_by_day.get(day, 0.0) for day in days},
        cost_events=dict(cost_events),
        funding_by_day=dict(funding_by_day),
        active_gross_start={day: active_gross_start.get(day, 0.0) for day in days},
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


def compute_hedge_beta(
    raw_rets: list[float],
    hedge_rets: list[float | None],
    idx: int,
    hedge_rule: ContinuousHedgeRule,
) -> float:
    """OLS beta of per-unit book return on hedge return over trailing ledger days.

    Uses pairs strictly before ``idx`` (minus ``beta_extra_lag_days``); only days
    where the hedge return is known enter the estimate.
    """
    end = idx - int(hedge_rule.beta_extra_lag_days)
    if end <= 0:
        return 0.0
    pairs = [
        (raw_rets[j], hedge_rets[j])
        for j in range(max(0, end - int(hedge_rule.beta_window_days)), end)
        if hedge_rets[j] is not None
    ]
    if len(pairs) < int(hedge_rule.beta_min_obs):
        return 0.0
    ys = [p[0] for p in pairs]
    hs = [float(p[1]) for p in pairs]  # type: ignore[arg-type]
    n = len(pairs)
    h_mean = sum(hs) / n
    y_mean = sum(ys) / n
    var = sum((h - h_mean) ** 2 for h in hs) / n
    if var <= 0.0:
        return 0.0
    cov = sum((h - h_mean) * (y - y_mean) for h, y in zip(hs, ys)) / n
    return cov / var


@dataclass(frozen=True)
class ContinuousHedgeState:
    """Prior-only state required to compute the live/paper hedge ratio.

    ``prior_raw_returns`` are per-unit (scale=1) book day returns and
    ``prior_hedge_returns`` the hedge-instrument day returns for the SAME ledger
    days, oldest to newest, both excluding the day being sized. ``None`` entries
    mark days with no hedge-instrument data (excluded from the estimate).
    """

    prior_raw_returns: tuple[float, ...]
    prior_hedge_returns: tuple[float | None, ...]


def compute_continuous_hedge_ratio(
    state: ContinuousHedgeState,
    hedge_rule: ContinuousHedgeRule,
    target_scale: float,
) -> float:
    """Live/paper twin of the hedge sizing inside ``apply_rebalance_rule``.

    Returns the long-hedge notional as a fraction of equity:
    ``clip(-beta, 0, hedge_cap) * target_scale``. Must match the backtest loop's
    ``hedge_ratio`` when fed the same prior series (parity-tested).
    """
    raw = [float(x) for x in state.prior_raw_returns]
    hs = list(state.prior_hedge_returns)
    if len(raw) != len(hs):
        raise ValueError("prior_raw_returns and prior_hedge_returns must be aligned")
    beta = compute_hedge_beta(raw, hs, len(raw), hedge_rule)
    return min(max(-beta, 0.0), float(hedge_rule.hedge_cap)) * max(float(target_scale), 0.0)


def plan_continuous_hedge_resize(
    *,
    hedge_symbol: str,
    current_qty: float,
    price: float,
    equity_usdt: float,
    hedge_ratio: float,
    min_resize_notional_usdt: float = 5.0,
) -> ContinuousRebalanceResizePlan | None:
    """Plan the order that brings the LONG hedge leg to ``equity * hedge_ratio``.

    The hedge leg is a long (opposite of the short book): positive delta means
    increase the long with a non-reduce-only Buy; negative delta means trim it
    with a reduce-only Sell capped at the current position. Venue qty/step
    filters are the executor's job, as in ``plan_continuous_rebalance_resizes``.
    """
    px = _finite_float(price)
    qty = max(_finite_float(current_qty), 0.0)
    if px <= 0.0:
        return None
    target = max(_finite_float(equity_usdt), 0.0) * max(_finite_float(hedge_ratio), 0.0)
    current = qty * px
    delta = target - current
    if abs(delta) < max(_finite_float(min_resize_notional_usdt), 0.0):
        return None
    if delta > 0.0:
        side, reduce_only, order_qty, reason = "Buy", False, delta / px, "hedge_increase"
    else:
        side, reduce_only, order_qty, reason = "Sell", True, min(qty, abs(delta) / px), "hedge_reduce"
    if order_qty <= 0.0:
        return None
    return ContinuousRebalanceResizePlan(
        trade_id="hedge",
        symbol=hedge_symbol,
        side=side,
        reduce_only=reduce_only,
        qty=order_qty,
        current_notional_usdt=current,
        target_notional_usdt=target,
        delta_notional_usdt=delta,
        reason=reason,
    )


def apply_rebalance_rule(
    components: ContinuousRebalanceComponents,
    rule: ContinuousRebalanceRule,
    hedge_rule: ContinuousHedgeRule | None = None,
    hedge_returns: dict[int, float] | None = None,
    hedge_funding: dict[int, float] | None = None,
) -> pl.DataFrame:
    """Apply a causal daily scale rule and rebuild decomposed equity.

    With ``hedge_rule`` set, a long hedge leg is added inside the daily loop:
    ``H(day) = clip(-beta, 0, cap) * scale(day)`` with beta causal at day open
    (trailing ledger days through day-1). The hedge PnL/funding/turnover-cost flow
    into ``basket_return`` BEFORE equity compounding, so the drawdown half-scale
    state reacts to hedged equity. The vol-target scale stays on the raw book
    returns (matching the live planner's ``prior_raw_returns`` semantics). On flat
    gaps the hedge is closed and reopened (turnover charged both ways); the final
    ledger day is charged the closing turnover.
    """
    hedged = hedge_rule is not None
    h_ret = hedge_returns or {}
    h_fund = hedge_funding or {}
    out: list[tuple] = []
    equity = 1.0
    peak = 1.0
    prev_scale = 1.0
    prev_hedge_ratio = 0.0
    days = components.days
    raw_rets = [components.raw_by_day[d] for d in days]
    hedge_rets: list[float | None] = [h_ret.get(d) for d in days]

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

        hedge_ratio = 0.0
        hedge_return = 0.0
        hedge_funding_return = 0.0
        hedge_cost_return = 0.0
        if hedged:
            assert hedge_rule is not None
            beta = compute_hedge_beta(raw_rets, hedge_rets, idx, hedge_rule)
            day_h = hedge_rets[idx]
            if day_h is not None:
                hedge_ratio = min(max(-beta, 0.0), float(hedge_rule.hedge_cap)) * scale
                hedge_return = hedge_ratio * float(day_h)
                hedge_funding_return = -hedge_ratio * float(h_fund.get(day, 0.0))
            if idx == 0 or days[idx] - days[idx - 1] == MS_PER_DAY:
                turnover = abs(hedge_ratio - prev_hedge_ratio)
            else:
                turnover = abs(prev_hedge_ratio) + abs(hedge_ratio)
            if idx == len(days) - 1:
                turnover += abs(hedge_ratio)
            hedge_cost_return = -turnover * float(hedge_rule.cost_bps) / 10_000.0
            basket_return += hedge_return + hedge_funding_return + hedge_cost_return
            prev_hedge_ratio = hedge_ratio

        equity *= 1.0 + basket_return
        peak = max(peak, equity)
        row = (day, basket_return, scale, gross, entry_cost, funding, resize_cost, equity)
        if hedged:
            row = row + (hedge_ratio, hedge_return, hedge_funding_return, hedge_cost_return)
        out.append(row)
        prev_scale = scale

    schema = [
        "ts_ms",
        "basket_return",
        "scale",
        "gross_return",
        "entry_cost_return",
        "funding_return",
        "resize_cost_return",
        "equity",
    ]
    if hedged:
        schema += ["hedge_ratio", "hedge_return", "hedge_funding_return", "hedge_cost_return"]
    return pl.DataFrame(
        out,
        schema=schema,
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
