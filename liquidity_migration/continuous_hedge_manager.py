"""Live BTC-beta hedge manager for the continuous demo book (WP3, banked 2026-06-09).

A once-daily manager that holds a small LONG BTC position sized to the continuous
short book's causal rolling beta, market-neutralizing its uncompensated alt-season
exposure (receipts: continuous-hedge-{overlay,engine}-2026-06-09.md). It is a
SEPARATE sleeve from the entry daemon: its own ledger root, its own orderLinkId
namespace (`lm-en-ca-*`), registered with the risk service via
`continuous_addon_data_root` so the BTC long is ADOPTED (tracked, never flattened as
an orphan), and it submits through the same REST private client (demo has no WS-trade).

Sizing is the parity-tested live twin of the backtest hedge leg
(`compute_continuous_hedge_ratio` + `plan_continuous_hedge_resize`, frozen
`ContinuousHedgeRule(90,60,2.0)`). The 90-day beta window is warm-started from the
shipped research unit-return + BTC series (`deploy/hedge_warmstart/`) and extended
with realized live book days, so the hedge is correctly sized from day one rather
than after a 90-day live warm-up.

Demo only. REAL_MONEY must be false. This is execution evidence, not alpha proof.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .continuous_rebalance import (
    ContinuousHedge2FState,
    ContinuousHedgeRule,
    ContinuousHedgeState,
    ContinuousRebalanceResizePlan,
    compute_continuous_hedge_ratio,
    compute_continuous_hedge_ratios_2f,
)
from .continuous_forward_replay import frozen_hedge_regime, frozen_hedge_rule
from .continuous_regime import latest_btcvol_intensity

HEDGE_SYMBOL = "BTCUSDT"
HEDGE_SYMBOL_2 = "ETHUSDT"  # second leg of the banked 2f hedge (2026-06-10 Stage-B)
HEDGE_LINK_PREFIX = "en-ca"  # ws_risk continuous-addon adoption namespace
# Frozen hedge rule — the banked engine-leg parameters (Stage-B, 8/8 pass; the same
# rule object parameterizes both the single-leg WP3 form and the 2f form). Derived from
# the single source of truth (FROZEN_FORWARD_CONFIG['hedge']) so the live manager can't
# drift from the forward ledger it mirrors (audit-iter6); byte-identical to the prior
# literal ContinuousHedgeRule(90, 60, 2.0, 5.0).
FROZEN_HEDGE_RULE = frozen_hedge_rule()
# The backtest book is 0.5-gross-short at scale 1; live H_equity_frac scales by the
# live book's actual gross-short fraction relative to that reference.
REFERENCE_GROSS_SHORT_FRAC = 0.5


def _regime_hedge_intensity(btc_returns: list[float | None]) -> float:
    """Today's BTC-vol regime-hedge intensity for the live book (1.0 when no regime
    is frozen). Reads the authoritative, hash-pinned regime from
    ``frozen_hedge_regime()`` so the live demo hedge applies the IDENTICAL signal the
    forward ledger accrues (errors-we-never-repeat #16, same hedge object). Causal:
    uses the prior BTC return series only (continuous_regime.latest_btcvol_intensity).
    """
    regime = frozen_hedge_regime()
    if not regime:
        return 1.0
    return latest_btcvol_intensity(
        btc_returns, regime["lam"], regime["vol_window"], regime["pct_window"]
    )


@dataclass(frozen=True, slots=True)
class ContinuousHedgeConfig:
    data_root: str = "data/bybit-continuous-hedge-event"
    warmstart_csv: str = "deploy/hedge_warmstart/bybit_warmstart.csv"
    trades_dataset: str = "continuous_fade_demo_trades"
    orders_dataset: str = "continuous_fade_demo_orders"
    strategy_id: str = "continuous_btc_hedge_v1"
    # "2f" = the banked BTC+ETH two-factor hedge (Stage-B s0-s8 PASS, 2026-06-10);
    # "btc" = the prior single-leg WP3 form (fallback; also used when the warm-start
    # has no eth_ret column or too few joint observations).
    hedge_mode: str = "2f"
    min_resize_notional_usdt: float = 25.0
    max_hedge_equity_frac: float = 0.30  # hard sanity cap on TOTAL live hedge size
    fallback_equity_usdt: float = 10_000.0
    submit_orders: bool = False
    confirm_demo_orders: bool = False


@dataclass(slots=True)
class HedgeDecision:
    """The computed hedge action for one daily run (pure; no I/O)."""

    beta_window_days: int
    hedge_ratio_equity_frac: float
    target_notional_usdt: float
    current_notional_usdt: float
    n_obs: int
    plan: ContinuousRebalanceResizePlan | None
    diagnostics: dict[str, Any] = field(default_factory=dict)


def load_warmstart(path: str | Path) -> tuple[list[float], list[float | None]]:
    """Return (unit_returns, btc_returns) oldest->newest from the shipped warm-start CSV."""
    unit, btc, _eth = load_warmstart_2f(path)
    return unit, btc


def load_warmstart_2f(path: str | Path) -> tuple[list[float], list[float | None], list[float | None]]:
    """Return (unit_returns, btc_returns, eth_returns) oldest->newest.

    ``eth_ret`` is an optional column (older warm-start files lack it); missing or
    blank entries load as None so the 2f path can fall back to single-leg when the
    joint-observation count is insufficient.
    """
    unit: list[float] = []
    btc: list[float | None] = []
    eth: list[float | None] = []
    p = Path(path)
    if not p.exists():
        return unit, btc, eth
    with p.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                u = float(row["unit_ret"])
            except (KeyError, TypeError, ValueError):
                continue
            unit.append(u)
            for col, out in (("btc_ret", btc), ("eth_ret", eth)):
                v = row.get(col)
                try:
                    out.append(float(v) if v not in (None, "") else None)
                except (TypeError, ValueError):
                    out.append(None)
    return unit, btc, eth


def extend_with_live_days(
    warm_unit: list[float],
    warm_btc: list[float | None],
    live_unit_by_day: dict[str, float],
    live_btc_by_day: dict[str, float],
) -> tuple[list[float], list[float | None]]:
    """Append realized live book days (oldest->newest) onto the warm-start series.

    ``live_*_by_day`` are keyed by ISO date strings AFTER the warm-start window; both
    book and BTC returns must be realized (a day enters only when the book return is
    known). Missing BTC for a present book-day enters as None (excluded from beta).
    """
    unit = list(warm_unit)
    btc = list(warm_btc)
    for day in sorted(live_unit_by_day):
        unit.append(float(live_unit_by_day[day]))
        b = live_btc_by_day.get(day)
        btc.append(float(b) if b is not None else None)
    return unit, btc


def compute_hedge_decision(
    config: ContinuousHedgeConfig,
    *,
    unit_returns: list[float],
    btc_returns: list[float | None],
    live_gross_short_frac: float,
    btc_price: float,
    current_hedge_qty: float,
    equity_usdt: float,
) -> HedgeDecision:
    """Pure hedge sizing for one daily run (no orders, no I/O).

    The beta is computed over the full supplied history (warm-start + live) via the
    parity-tested live twin; ``target_scale`` carries the live book's gross-short
    fraction relative to the 0.5 backtest reference, so a half-deployed live book
    gets a half-sized hedge. The result is hard-capped at ``max_hedge_equity_frac``.
    """
    state = ContinuousHedgeState(
        prior_raw_returns=tuple(unit_returns),
        prior_hedge_returns=tuple(btc_returns),
    )
    base_scale = max(live_gross_short_frac, 0.0) / REFERENCE_GROSS_SHORT_FRAC
    # BTC-vol regime-hedge overlay: scale the hedge by today's causal intensity
    # (1.0 when no regime is frozen). Identical to the backtest's hedge_scale.
    hedge_intensity = _regime_hedge_intensity(btc_returns)
    target_scale = base_scale * hedge_intensity
    ratio = compute_continuous_hedge_ratio(state, FROZEN_HEDGE_RULE, target_scale)
    ratio = min(ratio, config.max_hedge_equity_frac)
    n_obs = _beta_window_observation_count(btc_returns, FROZEN_HEDGE_RULE)
    plan: ContinuousRebalanceResizePlan | None = None
    if btc_price > 0.0:
        from .continuous_rebalance import plan_continuous_hedge_resize

        plan = plan_continuous_hedge_resize(
            hedge_symbol=HEDGE_SYMBOL,
            current_qty=current_hedge_qty,
            price=btc_price,
            equity_usdt=equity_usdt,
            hedge_ratio=ratio,
            min_resize_notional_usdt=config.min_resize_notional_usdt,
        )
    target_notional = max(equity_usdt, 0.0) * ratio
    return HedgeDecision(
        beta_window_days=FROZEN_HEDGE_RULE.beta_window_days,
        hedge_ratio_equity_frac=ratio,
        target_notional_usdt=target_notional,
        current_notional_usdt=max(current_hedge_qty, 0.0) * max(btc_price, 0.0),
        n_obs=n_obs,
        plan=plan,
        diagnostics={
            "target_scale": target_scale,
            "base_scale": base_scale,
            "hedge_intensity": hedge_intensity,
            "live_gross_short_frac": live_gross_short_frac,
            "history_days": len(unit_returns),
            "beta_window_observations": n_obs,
        },
    )


def _beta_window_observation_count(
    btc_returns: list[float | None],
    hedge_rule: ContinuousHedgeRule,
) -> int:
    end = len(btc_returns) - int(hedge_rule.beta_extra_lag_days)
    if end <= 0:
        return 0
    start = max(0, end - int(hedge_rule.beta_window_days))
    return sum(1 for b in btc_returns[start:end] if b is not None)


def _beta_window_joint_observation_count(
    btc_returns: list[float | None],
    eth_returns: list[float | None],
    hedge_rule: ContinuousHedgeRule,
) -> int:
    """Joint BTC+ETH observation count over the SAME trailing window the 2f beta
    actually uses (``compute_hedge_betas_2f``: rows in ``[lo, end)`` where both legs
    are known, with ``end = len - beta_extra_lag_days`` and
    ``lo = max(0, end - beta_window_days)``).

    The 2f fallback gate MUST use this windowed count, not a full-series count: the
    betas are estimated on the trailing window only, so a series whose full history
    has >= beta_min_obs joint obs but whose trailing window has < beta_min_obs would
    yield (0, 0) betas WITHOUT tripping a full-series fallback gate — silently
    leaving the book completely unhedged (audit hedge-1)."""
    end = len(btc_returns) - int(hedge_rule.beta_extra_lag_days)
    if end <= 0:
        return 0
    start = max(0, end - int(hedge_rule.beta_window_days))
    return sum(
        1
        for b, e in zip(btc_returns[start:end], eth_returns[start:end])
        if b is not None and e is not None
    )


@dataclass(slots=True)
class HedgeDecision2F:
    """The computed two-leg (BTC+ETH) hedge action for one daily run (pure; no I/O)."""

    beta_window_days: int
    ratio_btc: float
    ratio_eth: float
    target_btc_usdt: float
    target_eth_usdt: float
    n_obs_joint: int
    plan_btc: ContinuousRebalanceResizePlan | None
    plan_eth: ContinuousRebalanceResizePlan | None
    fell_back_to_btc: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)


def compute_hedge_decision_2f(
    config: ContinuousHedgeConfig,
    *,
    unit_returns: list[float],
    btc_returns: list[float | None],
    eth_returns: list[float | None],
    live_gross_short_frac: float,
    btc_price: float,
    eth_price: float,
    current_btc_qty: float,
    current_eth_qty: float,
    equity_usdt: float,
) -> HedgeDecision2F:
    """Pure two-leg hedge sizing for one daily run (no orders, no I/O).

    Per-leg ratios come from the parity-tested live twin
    (``compute_continuous_hedge_ratios_2f``, frozen Stage-B rule). If the joint
    BTC+ETH observation count WITHIN THE TRAILING BETA WINDOW is below
    ``beta_min_obs`` the twin returns (0, 0); in that case this falls back to the
    single-leg BTC decision so a thin ETH window can never leave the book unhedged.
    The fallback gate is measured over the same trailing window the beta uses (not
    the full series) so an ETH-thin window with a deep full history still falls
    back instead of silently producing a zero hedge (audit hedge-1). The TOTAL is
    hard-capped at ``max_hedge_equity_frac`` (legs scaled proportionally).
    """
    from .continuous_rebalance import plan_continuous_hedge_resize

    base_scale = max(live_gross_short_frac, 0.0) / REFERENCE_GROSS_SHORT_FRAC
    # BTC-vol regime-hedge overlay: one causal intensity scales BOTH legs (and the
    # single-leg fallback below, since it reuses target_scale) — identical to the
    # backtest's hedge_scale. 1.0 when no regime is frozen.
    hedge_intensity = _regime_hedge_intensity(btc_returns)
    target_scale = base_scale * hedge_intensity
    # Count joint obs over the SAME trailing window the beta uses (not the full
    # series): a full-series count would miss the case where the trailing window is
    # ETH-thin (betas -> 0) yet the full history is not, leaving the book silently
    # unhedged with no fallback (audit hedge-1).
    n_joint = _beta_window_joint_observation_count(btc_returns, eth_returns, FROZEN_HEDGE_RULE)
    state = ContinuousHedge2FState(
        prior_raw_returns=tuple(unit_returns),
        prior_hedge_returns_1=tuple(btc_returns),
        prior_hedge_returns_2=tuple(eth_returns),
    )
    r_btc, r_eth = compute_continuous_hedge_ratios_2f(state, FROZEN_HEDGE_RULE, target_scale)
    fell_back = False
    # audit-iter6: fall back to the single-leg BTC hedge on ANY degenerate (0,0) 2f
    # result, not only the thin-window case — the degenerate-variance / uncovered-zero-beta
    # paths also yield (0,0) and would otherwise leave the book silently unhedged. Genuine
    # no-hedge cases (target_scale==0, beta clipped to 0) also yield a 0.0 single-leg, so
    # this never introduces a spurious hedge.
    if r_btc == 0.0 and r_eth == 0.0:
        single = compute_continuous_hedge_ratio(
            ContinuousHedgeState(prior_raw_returns=tuple(unit_returns), prior_hedge_returns=tuple(btc_returns)),
            FROZEN_HEDGE_RULE,
            target_scale,
        )
        r_btc, r_eth, fell_back = single, 0.0, True
    total = r_btc + r_eth
    if total > config.max_hedge_equity_frac and total > 0.0:
        shrink = config.max_hedge_equity_frac / total
        r_btc *= shrink
        r_eth *= shrink
    plan_btc = plan_eth = None
    if btc_price > 0.0:
        plan_btc = plan_continuous_hedge_resize(
            hedge_symbol=HEDGE_SYMBOL, current_qty=current_btc_qty, price=btc_price,
            equity_usdt=equity_usdt, hedge_ratio=r_btc,
            min_resize_notional_usdt=config.min_resize_notional_usdt,
        )
    if eth_price > 0.0:
        plan_eth = plan_continuous_hedge_resize(
            hedge_symbol=HEDGE_SYMBOL_2, current_qty=current_eth_qty, price=eth_price,
            equity_usdt=equity_usdt, hedge_ratio=r_eth,
            min_resize_notional_usdt=config.min_resize_notional_usdt,
        )
    eq = max(equity_usdt, 0.0)
    return HedgeDecision2F(
        beta_window_days=FROZEN_HEDGE_RULE.beta_window_days,
        ratio_btc=r_btc,
        ratio_eth=r_eth,
        target_btc_usdt=eq * r_btc,
        target_eth_usdt=eq * r_eth,
        n_obs_joint=n_joint,
        plan_btc=plan_btc,
        plan_eth=plan_eth,
        fell_back_to_btc=fell_back,
        diagnostics={
            "target_scale": target_scale,
            "base_scale": base_scale,
            "hedge_intensity": hedge_intensity,
            "live_gross_short_frac": live_gross_short_frac,
            "history_days": len(unit_returns),
        },
    )


def build_hedge_trade_row(
    config: ContinuousHedgeConfig,
    *,
    qty: float,
    entry_price: float,
    now_ms: int,
    order_link_id: str,
    order_id: str = "",
    symbol: str = HEDGE_SYMBOL,
) -> dict[str, Any]:
    """Conforming OPEN trade row for a submitted hedge BTC long."""
    return build_hedge_tracking_row(
        config,
        qty=qty,
        entry_price=entry_price,
        opened_ms=now_ms,
        updated_ms=now_ms,
        order_link_id=order_link_id,
        order_id=order_id,
        signal_ts_ms=now_ms,
        submit_mode="submitted" if order_id else "dry_run",
        symbol=symbol,
    )


def build_hedge_tracking_row(
    config: ContinuousHedgeConfig,
    *,
    qty: float,
    entry_price: float,
    opened_ms: int,
    updated_ms: int,
    order_link_id: str,
    order_id: str = "",
    signal_ts_ms: int | None = None,
    submit_mode: str | None = None,
    symbol: str = HEDGE_SYMBOL,
) -> dict[str, Any]:
    """Conforming OPEN tracking row for the externally managed hedge BTC long.

    SAFETY CONTRACT (verified against ws_risk.plan_risk_exits): stop_price,
    take_profit_price and planned_exit_ts_ms are ALL 0, so the risk service TRACKS
    this position (its symbol enters open_symbols -> adoption skips it) but NEVER
    force-exits it — the daily hedge manager is its sole manager. side='long',
    sleeve='continuous_addon' routes it to the addon ledger the risk service reads.
    """
    return {
        "trade_id": f"hedge-{order_link_id}",
        "strategy_id": config.strategy_id,
        "symbol": symbol,
        "side": "long",
        "sleeve": "continuous_addon",
        "status": "open",
        "ts_ms": opened_ms,
        "entry_ts_ms": opened_ms,
        "opened_at_ms": opened_ms,
        "updated_at_ms": updated_ms,
        "signal_ts_ms": opened_ms if signal_ts_ms is None else signal_ts_ms,
        "entry_price": float(entry_price),
        "qty": float(qty),
        "notional_usdt": abs(float(entry_price) * float(qty)),
        # The three force-exit triggers, all disabled — externally managed.
        "stop_price": 0.0,
        "take_profit_price": 0.0,
        "planned_exit_ts_ms": 0,
        "stop_loss_pct": 0.0,
        "entry_order_link_id": order_link_id,
        "entry_order_id": order_id,
        "submit_mode": submit_mode or ("submitted" if order_id else "dry_run"),
    }


def hedge_order_link_id(now_ms: int, symbol: str = HEDGE_SYMBOL) -> str:
    """Stable-namespaced link id for a hedge leg (ws_risk continuous-addon route)."""
    from .event_demo import _order_link_id

    return _order_link_id(HEDGE_LINK_PREFIX, symbol=symbol, signal_ts_ms=int(now_ms))




