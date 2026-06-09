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
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .continuous_rebalance import (
    ContinuousHedgeRule,
    ContinuousHedgeState,
    ContinuousRebalanceResizePlan,
    compute_continuous_hedge_ratio,
)

HEDGE_SYMBOL = "BTCUSDT"
HEDGE_LINK_PREFIX = "en-ca"  # ws_risk continuous-addon adoption namespace
# Frozen hedge rule — the banked engine-leg parameters (Stage-B, 8/8 pass).
FROZEN_HEDGE_RULE = ContinuousHedgeRule(beta_window_days=90, beta_min_obs=60, hedge_cap=2.0, cost_bps=5.0)
# The backtest book is 0.5-gross-short at scale 1; live H_equity_frac scales by the
# live book's actual gross-short fraction relative to that reference.
REFERENCE_GROSS_SHORT_FRAC = 0.5


@dataclass(frozen=True, slots=True)
class ContinuousHedgeConfig:
    data_root: str = "data/bybit-continuous-hedge-event"
    warmstart_csv: str = "deploy/hedge_warmstart/bybit_warmstart.csv"
    trades_dataset: str = "continuous_fade_demo_trades"
    orders_dataset: str = "continuous_fade_demo_orders"
    strategy_id: str = "continuous_btc_hedge_v1"
    min_resize_notional_usdt: float = 25.0
    max_hedge_equity_frac: float = 0.30  # hard sanity cap on live hedge size
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
    unit: list[float] = []
    btc: list[float | None] = []
    p = Path(path)
    if not p.exists():
        return unit, btc
    with p.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                u = float(row["unit_ret"])
            except (KeyError, TypeError, ValueError):
                continue
            unit.append(u)
            b = row.get("btc_ret")
            try:
                btc.append(float(b) if b not in (None, "") else None)
            except (TypeError, ValueError):
                btc.append(None)
    return unit, btc


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
    target_scale = max(live_gross_short_frac, 0.0) / REFERENCE_GROSS_SHORT_FRAC
    ratio = compute_continuous_hedge_ratio(state, FROZEN_HEDGE_RULE, target_scale)
    ratio = min(ratio, config.max_hedge_equity_frac)
    n_obs = sum(1 for b in btc_returns if b is not None)
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
) -> dict[str, Any]:
    """Conforming OPEN trade row for the hedge BTC long.

    SAFETY CONTRACT (verified against ws_risk.plan_risk_exits): stop_price,
    take_profit_price and planned_exit_ts_ms are ALL 0, so the risk service TRACKS
    this position (its symbol enters open_symbols -> adoption skips it) but NEVER
    force-exits it — the daily hedge manager is its sole manager. side='long',
    sleeve='continuous_addon' routes it to the addon ledger the risk service reads.
    """
    return {
        "trade_id": f"hedge-{order_link_id}",
        "strategy_id": config.strategy_id,
        "symbol": HEDGE_SYMBOL,
        "side": "long",
        "sleeve": "continuous_addon",
        "status": "open",
        "ts_ms": now_ms,
        "entry_ts_ms": now_ms,
        "opened_at_ms": now_ms,
        "updated_at_ms": now_ms,
        "signal_ts_ms": now_ms,
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
        "submit_mode": "submitted" if order_id else "dry_run",
    }


def hedge_order_link_id(now_ms: int) -> str:
    """Stable-namespaced link id for the hedge leg (ws_risk continuous-addon route)."""
    from .event_demo import _order_link_id

    return _order_link_id(HEDGE_LINK_PREFIX, symbol=HEDGE_SYMBOL, signal_ts_ms=int(now_ms))


def utc_today_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()
