"""Extracted from event_demo.py — see that module's docstring.

This sibling holds a cohesive slice of the event-demo machinery. It
imports shared helpers/configs from event_demo.py (the hub); the hub
re-imports this module's public names at the bottom so external callers
(`from liquidity_migration.event_demo import X`) keep working unchanged.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any




from .event_demo import (  # noqa: F401  (shared hub helpers)
    _float,
    _iso_dt,
)
from .telegram import format_pct, format_usd

_logger = logging.getLogger(__name__)


# These are notification thresholds, not trading or liquidation rules.  They
# measure unrealised P&L as a fraction of current position value (the same
# denominator used in ``build_position_pnl_snapshot``).  The environment value
# accepts comma-separated positive fractions, for example ``0.05,0.10,0.20``.
DEFAULT_POSITION_LOSS_ALERT_LEVELS = (0.05, 0.10, 0.20, 0.40)


@dataclass(frozen=True, slots=True)
class PositionLossAlert:
    symbol: str
    side: str
    qty: float
    position_value_usdt: float
    unrealized_pnl_usdt: float
    pnl_pct: float
    avg_price: float
    mark_price: float
    liquidation_price: float
    leverage: float
    stop_price: float
    take_profit_price: float
    threshold: float
    next_threshold: float | None


def position_loss_alert_levels(raw: str | None = None) -> tuple[float, ...]:
    """Return validated, ordered loss-band fractions.

    Invalid configuration fails back to the conservative defaults instead of
    disabling warnings silently.  Values are positive magnitudes; a level of
    ``0.10`` represents a position P&L at or below ``-10%``.
    """
    text = os.environ.get("TELEGRAM_POSITION_LOSS_LEVELS", "") if raw is None else raw
    if not str(text or "").strip():
        return DEFAULT_POSITION_LOSS_ALERT_LEVELS
    try:
        values = sorted({float(part.strip()) for part in str(text).split(",") if part.strip()})
    except (TypeError, ValueError):
        return DEFAULT_POSITION_LOSS_ALERT_LEVELS
    if not values or any(not math.isfinite(value) or value <= 0.0 for value in values):
        return DEFAULT_POSITION_LOSS_ALERT_LEVELS
    return tuple(values)


def position_loss_alerts(
    payload: dict[str, Any],
    *,
    levels: tuple[float, ...] | None = None,
) -> list[PositionLossAlert]:
    """Return the deepest crossed loss band for each verified Bybit position."""
    cycle = payload.get("cycle", {})
    if cycle.get("position_report_error"):
        return []
    configured = levels or position_loss_alert_levels()
    alerts: list[PositionLossAlert] = []
    for row in payload.get("bybit_positions", []) or []:
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "").lower()
        pnl_pct = _float(row.get("pnl_pct"))
        if not symbol or not math.isfinite(pnl_pct) or pnl_pct >= 0.0:
            continue
        crossed = [level for level in configured if pnl_pct <= -level]
        if not crossed:
            continue
        threshold = max(crossed)
        next_threshold = next((level for level in configured if level > threshold), None)
        alerts.append(
            PositionLossAlert(
                symbol=symbol,
                side=side or "position",
                qty=_float(row.get("qty")),
                position_value_usdt=_float(row.get("position_value_usdt")),
                unrealized_pnl_usdt=_float(row.get("unrealized_pnl_usdt")),
                pnl_pct=pnl_pct,
                avg_price=_float(row.get("avg_price")),
                mark_price=_float(row.get("mark_price")),
                liquidation_price=_float(row.get("liquidation_price")),
                leverage=_float(row.get("leverage")),
                stop_price=_float(row.get("stop_price")),
                take_profit_price=_float(row.get("take_profit_price")),
                threshold=threshold,
                next_threshold=next_threshold,
            )
        )
    return sorted(alerts, key=lambda alert: alert.pnl_pct)


def format_position_loss_alert(
    alert: PositionLossAlert,
    *,
    now_ms: int | None = None,
    reminder: bool = False,
) -> str:
    """One human-readable, one-position loss-band notification."""
    lines = [
        "\N{LARGE RED CIRCLE} Loss warning \N{MIDDLE DOT} Bybit demo",
        f"{alert.symbol} {alert.side.upper()}",
        f"P&L {format_usd(alert.unrealized_pnl_usdt, signed=True)} "
        f"({format_pct(alert.pnl_pct, signed=True)} of position value)",
        f"Exposure {format_usd(alert.position_value_usdt)} \N{MIDDLE DOT} qty {alert.qty:g}"
        + (f" \N{MIDDLE DOT} {alert.leverage:g}x leverage" if alert.leverage > 0.0 else ""),
    ]
    if alert.avg_price > 0.0 and alert.mark_price > 0.0:
        lines.append(f"Entry ${alert.avg_price:.8g} \N{RIGHTWARDS ARROW} mark ${alert.mark_price:.8g}")
    if alert.liquidation_price > 0.0:
        liquidation_distance = (
            (alert.liquidation_price - alert.mark_price) / alert.mark_price
            if alert.side == "short"
            else (alert.mark_price - alert.liquidation_price) / alert.mark_price
        ) if alert.mark_price > 0.0 else 0.0
        lines.append(
            f"Liquidation ${alert.liquidation_price:.8g} "
            f"({format_pct(max(liquidation_distance, 0.0))} away)"
        )
    protection = [
        f"TP ${alert.take_profit_price:.8g}"
        if alert.take_profit_price > 0.0
        else "no venue TP",
        f"SL ${alert.stop_price:.8g}"
        if alert.stop_price > 0.0
        else "no venue stop",
    ]
    lines.append("Protection: " + " · ".join(protection))
    threshold_text = format_pct(-alert.threshold)
    if reminder:
        cadence = (
            f"24-hour reminder: this position is still past {threshold_text}. "
            "Another alert requires a deeper band or another 24 hours."
        )
    elif alert.next_threshold is None:
        cadence = "No higher configured loss band. An unchanged loss can remind again after 24h."
    else:
        cadence = (
            f"First alert past {threshold_text}; next only past "
            f"{format_pct(-alert.next_threshold)} (or after 24h if still here)."
        )
    lines.append(cadence)
    if now_ms:
        lines.append(_iso_dt(now_ms))
    return "\n".join(lines)[:3900]


def format_event_demo_cycle_report(payload: dict[str, Any]) -> str:
    cycle = payload["cycle"]
    lines = [
        "# Event Demo Cycle",
        "",
        f"- Time: {_iso_dt(cycle['ts_ms'])}",
        f"- Mode: `{cycle['mode']}`",
        f"- Strategy: `{cycle.get('strategy_id', '')}`",
        f"- Strategy profile: `{cycle.get('strategy_profile', '')}`",
        f"- Universe symbols: {cycle['symbols']}",
        f"- Feature rows: {cycle['feature_rows']}",
        f"- Latest feature: {_iso_dt(cycle.get('latest_feature_ts_ms'))}",
        f"- Equity used: ${cycle['equity_usdt']:,.2f}",
        f"- Entries executed: {cycle['entries_executed']} / candidates {cycle['entry_candidates']}",
        f"- Exits executed: {cycle['exits_executed']} / candidates {cycle['exit_candidates']}",
        f"- Pending fills reconciled: {cycle.get('pending_order_fills_reconciled', 0)} "
        f"(entries {cycle.get('pending_entry_fills_reconciled', 0)} / exits {cycle.get('pending_exit_fills_reconciled', 0)})",
        f"- Stale pending entries terminalized: {cycle.get('stale_pending_entry_orders_terminalized', 0)}",
        f"- Open trades after: {cycle['open_trades_after']}",
        f"- Per-entry notional: {_float(cycle.get('order_notional_pct_equity')):.2%} of equity",
        f"- Per-entry initial margin: {_float(cycle.get('order_initial_margin_pct_equity')):.2%} of equity at {_float(cycle.get('entry_leverage')):.2g}x",
        f"- Target gross / initial margin: {_float(cycle.get('target_gross_exposure')):.2%} / {_float(cycle.get('target_initial_margin_pct_equity')):.2%} of equity",
        f"- Bybit positions: {cycle.get('bybit_positions', 0)} / uPnL ${_float(cycle.get('bybit_unrealized_pnl_usdt')):,.2f}",
        f"- Ledger positions: {cycle.get('ledger_positions', 0)} / uPnL ${_float(cycle.get('ledger_unrealized_pnl_usdt')):,.2f}",
        f"- Telegram: {('enqueued (async send; delivery outcome in journald)' if str(cycle.get('telegram_error') or '') == 'enqueued' else cycle.get('telegram_error') or ('sent' if cycle.get('telegram_sent') else 'not sent'))}",
        "",
        "## Entries",
        "",
        "| Symbol | Side | Qty | Notional | Init Margin | Lev | Signal | Ready | Stop | TP | Mode |",
        "|---|---|---:|---:|---:|---:|---|---|---:|---:|---|",
    ]
    for row in payload.get("entries", []):
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('side', '')} | {row.get('qty', '')} | "
            f"${_float(row.get('notional_usdt')):,.2f} | ${_float(row.get('initial_margin_usdt')):,.2f} | "
            f"{_float(row.get('entry_leverage')):.2g}x | {_iso_dt(row.get('signal_ts_ms'))} | "
            f"{_iso_dt(row.get('entry_ready_ts_ms'))} | {_float(row.get('stop_price')):.8g} | "
            f"{_float(row.get('take_profit_price')):.8g} | {row.get('submit_mode', '')} |"
        )
    if not payload.get("entries"):
        lines.append("|  |  |  |  |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Exits",
            "",
            "| Symbol | Reason | Qty | Trigger | Mode |",
            "|---|---|---:|---|---|",
        ]
    )
    for row in payload.get("exits", []):
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('exit_reason', '')} | {row.get('qty', '')} | "
            f"{_iso_dt(row.get('exit_trigger_ts_ms'))} | {row.get('submit_mode', '')} |"
        )
    if not payload.get("exits"):
        lines.append("|  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Bybit Positions",
            "",
            "| Symbol | Side | Qty | Value | uPnL | PnL % | Mark | Avg |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload.get("bybit_positions", [])[:20]:
        lines.append(_position_markdown_row(row))
    if not payload.get("bybit_positions"):
        lines.append("|  |  |  |  |  |  |  |  |")
    if payload["cycle"].get("position_report_error"):
        lines.extend(["", f"Position report error: {payload['cycle']['position_report_error']}"])
    lines.extend([""])
    return "\n".join(lines)

def format_event_risk_cycle_report(payload: dict[str, Any]) -> str:
    cycle = payload["cycle"]
    lines = [
        "# Event Risk Cycle",
        "",
        f"- Time: {_iso_dt(cycle['ts_ms'])}",
        f"- Mode: `{cycle['mode']}`",
        f"- Exit candidates: {cycle['exit_candidates']}",
        f"- Exits executed: {cycle['exits_executed']}",
        f"- Stop repairs: {cycle.get('stop_repairs', 0)}",
        f"- Pending fills reconciled: {cycle.get('pending_order_fills_reconciled', cycle.get('pending_fills_reconciled', 0))} "
        f"(entries {cycle.get('pending_entry_fills_reconciled', 0)} / exits {cycle.get('pending_exit_fills_reconciled', 0)})",
        f"- Pending entry Bybit positions: {cycle.get('pending_entry_positions', 0)}",
        f"- Open trades after: {cycle['open_trades_after']}",
        f"- Bybit positions: {cycle.get('bybit_positions', 0)} / uPnL ${_float(cycle.get('bybit_unrealized_pnl_usdt')):,.2f}",
        f"- Ledger positions: {cycle.get('ledger_positions', 0)} / uPnL ${_float(cycle.get('ledger_unrealized_pnl_usdt')):,.2f}",
        f"- Untracked Bybit positions: {cycle.get('untracked_positions', 0)}",
        f"- Telegram: {('enqueued (async send; delivery outcome in journald)' if str(cycle.get('telegram_error') or '') == 'enqueued' else cycle.get('telegram_error') or ('sent' if cycle.get('telegram_sent') else 'not sent'))}",
        "",
        "## Exits",
        "",
        "| Symbol | Reason | Qty | Trigger | Price | Mode |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in payload.get("exits", []):
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('exit_reason', '')} | {row.get('qty', '')} | "
            f"{_iso_dt(row.get('exit_trigger_ts_ms'))} | {_float(row.get('exit_price')):.8g} | "
            f"{row.get('submit_mode', '')} |"
        )
    if not payload.get("exits"):
        lines.append("|  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Stop Repairs",
            "",
            "| Symbol | Stop | TP | Status | Mode | Error |",
            "|---|---:|---:|---|---|---|",
        ]
    )
    for row in payload.get("stop_repairs", []):
        lines.append(
            f"| {row.get('symbol', '')} | {_float(row.get('stop_price')):.8g} | "
            f"{_float(row.get('take_profit_price')):.8g} | {row.get('status', '')} | "
            f"{row.get('submit_mode', '')} | {row.get('error', '')} |"
        )
    if not payload.get("stop_repairs"):
        lines.append("|  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Bybit Positions",
            "",
            "| Symbol | Side | Qty | Value | uPnL | PnL % | Mark | Avg |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload.get("bybit_positions", [])[:20]:
        lines.append(_position_markdown_row(row))
    if not payload.get("bybit_positions"):
        lines.append("|  |  |  |  |  |  |  |  |")
    if payload.get("untracked_positions"):
        lines.extend(["", "## Untracked Positions", ""])
        for row in payload.get("untracked_positions", [])[:20]:
            lines.append(f"- {row.get('symbol', '')} {row.get('side', '')} qty={_float(row.get('qty')):g}")
    if payload.get("pending_entry_positions"):
        lines.extend(["", "## Pending Entry Positions", ""])
        for row in payload.get("pending_entry_positions", [])[:20]:
            lines.append(f"- {row.get('symbol', '')} {row.get('side', '')} qty={_float(row.get('qty')):g}")
    if payload["cycle"].get("position_report_error"):
        lines.extend(["", f"Position report error: {payload['cycle']['position_report_error']}"])
    lines.extend([""])
    return "\n".join(lines)

def _position_markdown_row(row: dict[str, Any]) -> str:
    return (
        f"| {row.get('symbol', '')} | {row.get('side', '')} | {_float(row.get('qty')):g} | "
        f"${_float(row.get('position_value_usdt')):,.2f} | ${_float(row.get('unrealized_pnl_usdt')):,.2f} | "
        f"{_float(row.get('pnl_pct')):.2%} | {_float(row.get('mark_price')):.8g} | {_float(row.get('avg_price')):.8g} |"
    )

_TELEGRAM_REASON_TITLES = {
    "entry_executed": "\N{LARGE GREEN CIRCLE} Position opened",
    "entry_fill_reconciled": "\N{LARGE GREEN CIRCLE} Position opened",
    "exit_executed": "\N{WHITE HEAVY CHECK MARK} Position closed",
    "exit_fill_reconciled": "\N{WHITE HEAVY CHECK MARK} Position closed",
    "position_reconciled": "\N{CLOCKWISE OPEN CIRCLE ARROW} Position records reconciled",
    "position_close_pending_pnl": "\N{WARNING SIGN} Position close awaiting P&L",
    "entry_order_error": "\N{WARNING SIGN} Entry order failed",
    "entry_stop_update_failed": "\N{WARNING SIGN} Position protection failed",
    "risk_order_error": "\N{WARNING SIGN} Exit order failed",
    "stop_repair_failed": "\N{WARNING SIGN} Stop repair failed",
    "stop_repaired": "\N{WHITE HEAVY CHECK MARK} Stop repaired",
    "stop_repair_planned": "\N{INFORMATION SOURCE} Stop repair planned",
    "untracked_position": "\N{WARNING SIGN} Untracked Bybit position",
    "untracked_position_exit": "\N{WARNING SIGN} Untracked position exit submitted",
    "entry_order_unconfirmed": "\N{WARNING SIGN} Entry fill unconfirmed",
    "exit_order_unconfirmed": "\N{WARNING SIGN} Exit fill unconfirmed",
    "position_report_error": "\N{WARNING SIGN} Bybit position check unavailable",
    "wallet_error": "\N{WARNING SIGN} Bybit wallet check unavailable",
}


def human_exit_reason(value: Any) -> str:
    raw = str(value or "closed").strip().lower()
    if raw == "take_profit_level_reached":
        return "TAKE-PROFIT LEVEL REACHED (ORDER TYPE UNCONFIRMED)"
    if raw == "stop_loss_level_reached":
        return "STOP-LOSS LEVEL REACHED (ORDER TYPE UNCONFIRMED)"
    if raw == "trailing_take_profit":
        return "TRAILING TAKE PROFIT"
    if "take_profit" in raw or raw in {"tp", "take-profit"}:
        return "TAKE PROFIT"
    if "stop_loss" in raw or raw in {"sl", "stop"}:
        return "STOP LOSS"
    if raw == "trailing_stop":
        return "TRAILING STOP"
    if raw == "liquidation":
        return "LIQUIDATION"
    if raw == "auto_deleveraging":
        return "AUTO-DELEVERAGING"
    if raw == "manual_close":
        return "MANUAL VENUE CLOSE"
    if raw == "mixed_venue_close":
        return "MIXED VENUE CLOSE"
    if raw == "bybit_position_missing":
        return "VENUE CLOSED (CAUSE UNCONFIRMED)"
    if raw in {"time_stop", "max_hold", "max_hold_24h"} or "time" in raw:
        return "MAX HOLD / TIME EXIT"
    if raw == "untracked_position":
        return "UNTRACKED POSITION SAFETY EXIT"
    return raw.replace("_", " ").upper()


def _realized_pnl(row: dict[str, Any]) -> tuple[float, float | None]:
    entry = _float(row.get("entry_price"))
    exit_price = _float(row.get("exit_price") or row.get("avg_price"))
    qty = abs(_float(row.get("qty") or row.get("filled_qty")))
    if entry <= 0.0 or exit_price <= 0.0 or qty <= 0.0:
        return 0.0, None
    side = str(row.get("side") or row.get("trade_side") or "").lower()
    gross = (entry - exit_price) * qty if side in {"short", "sell"} else (exit_price - entry) * qty
    fees = _float(row.get("entry_fee_usdt")) + _float(
        row.get("exit_fee_usdt") or row.get("fee_usdt")
    )
    # Prefer an explicitly allocated venue Closed-PnL value when orphan/TP
    # reconciliation supplied one.  It is stronger evidence than recomputing
    # from a cost-influenced average exit price.  Normal direct fills use the
    # entry/exit/fee reconstruction below.
    venue_pnl_raw = row.get("venue_closed_pnl_allocated_usdt")
    if venue_pnl_raw not in (None, ""):
        venue_pnl = _float(venue_pnl_raw)
        return venue_pnl, venue_pnl / (entry * qty)
    realized_pnl_raw = row.get("realized_pnl_usdt")
    if realized_pnl_raw not in (None, ""):
        realized_pnl = _float(realized_pnl_raw)
        realized_pnl_pct_raw = row.get("realized_pnl_pct")
        realized_pnl_pct = (
            _float(realized_pnl_pct_raw)
            if realized_pnl_pct_raw not in (None, "")
            else realized_pnl / (entry * qty)
        )
        return realized_pnl, realized_pnl_pct
    return gross - fees, (gross / (entry * qty))


def _event_rows(payload: dict[str, Any], *, closing: bool) -> list[dict[str, Any]]:
    keys = (
        ("exits", "reconciliations", "pending_fill_reconciliations")
        if closing
        else ("entries", "pending_fill_trades", "pending_fill_reconciliations")
    )
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for key in keys:
        for row in payload.get(key, []) or []:
            status = str(row.get("status") or "").lower()
            is_close = status == "closed" or bool(row.get("exit_reason"))
            if closing != is_close:
                continue
            identity = (
                str(row.get("trade_id") or ""),
                str(row.get("symbol") or ""),
                str(row.get("exit_order_link_id") or row.get("entry_order_link_id") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(row)
    return rows


def aggregate_position_event_rows(
    rows: list[dict[str, Any]],
    *,
    closing: bool,
    default_side: str = "position",
) -> list[dict[str, Any]]:
    """Net component/trade rows into one operator update per venue position."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "UNKNOWN")
        raw_side = str(row.get("trade_side") or row.get("side") or default_side).lower()
        side = (
            "short" if raw_side in {"short", "sell"}
            else "long" if raw_side in {"long", "buy"}
            else raw_side
        )
        grouped.setdefault((symbol, side), []).append(row)

    output: list[dict[str, Any]] = []
    for (symbol, side), group in grouped.items():
        qty = sum(abs(_float(row.get("qty") or row.get("filled_qty"))) for row in group)
        entry_value = sum(
            abs(_float(row.get("qty") or row.get("filled_qty")))
            * _float(row.get("entry_price"))
            for row in group
        )
        exit_value = sum(
            abs(_float(row.get("qty") or row.get("filled_qty")))
            * _float(row.get("exit_price") or row.get("avg_price"))
            for row in group
        )
        aggregate: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": entry_value / qty if qty > 0.0 else 0.0,
            "exit_price": exit_value / qty if qty > 0.0 else 0.0,
            "notional_usdt": sum(_float(row.get("notional_usdt")) for row in group),
            "entry_fee_usdt": sum(_float(row.get("entry_fee_usdt")) for row in group),
            "exit_fee_usdt": sum(_float(row.get("exit_fee_usdt")) for row in group),
            "leg_count": len(group),
            "event_ids": tuple(
                sorted(
                    {
                        str(value)
                        for row in group
                        for value in (
                            row.get("trade_id"),
                            row.get("entry_order_link_id"),
                            row.get("exit_order_link_id"),
                            row.get("order_link_id"),
                            row.get("order_id"),
                        )
                        if value not in (None, "")
                    }
                )
            ),
            "take_profit_prices": tuple(
                sorted({_float(row.get("take_profit_price")) for row in group if _float(row.get("take_profit_price")) > 0.0})
            ),
            "stop_prices": tuple(
                sorted({_float(row.get("stop_price")) for row in group if _float(row.get("stop_price")) > 0.0})
            ),
        }
        if closing:
            reasons = sorted({human_exit_reason(row.get("exit_reason")) for row in group})
            if reasons:
                aggregate["exit_reason"] = " / ".join(reasons)
            reason_sources = sorted(
                {
                    str(row.get("exit_reason_source") or "")
                    for row in group
                    if str(row.get("exit_reason_source") or "")
                }
            )
            if reason_sources:
                aggregate["exit_reason_source"] = ",".join(reason_sources)
            realized_parts = [_realized_pnl(row) for row in group]
            if realized_parts and all(pct is not None for _pnl, pct in realized_parts):
                realized_pnl = sum(pnl for pnl, _pct in realized_parts)
                aggregate["realized_pnl_usdt"] = realized_pnl
                aggregate["realized_pnl_pct"] = (
                    realized_pnl / entry_value if entry_value > 0.0 else 0.0
                )
        output.append(aggregate)
    return output


def format_position_event_lines(row: dict[str, Any], *, closing: bool) -> list[str]:
    symbol = str(row.get("symbol") or "UNKNOWN")
    side = str(row.get("side") or row.get("trade_side") or "position").upper()
    qty = abs(_float(row.get("qty") or row.get("filled_qty")))
    if closing:
        reason = str(row.get("exit_reason") or "CLOSED")
        lines = [f"{symbol} {side} \N{MIDDLE DOT} {reason}"]
        exit_price = _float(row.get("exit_price") or row.get("avg_price"))
        if exit_price > 0.0:
            lines.append(f"Closed {qty:g} @ ${exit_price:.8g}")
        pnl, pnl_pct = _realized_pnl(row)
        if pnl_pct is not None:
            lines.append(
                f"Realised P&L {format_usd(pnl, signed=True)} "
                f"({format_pct(pnl_pct, signed=True)} of entry exposure)"
            )
        if str(row.get("exit_reason_source") or "").startswith("exit_price_vs_ledger_"):
            lines.append("Close cause inferred from exit price; venue order type was unavailable.")
        return lines
    entry_price = _float(row.get("entry_price") or row.get("avg_price"))
    notional = _float(row.get("notional_usdt")) or entry_price * qty
    lines = [f"{symbol} {side}"]
    if entry_price > 0.0:
        lines.append(
            f"Opened {qty:g} @ ${entry_price:.8g} \N{MIDDLE DOT} exposure {format_usd(notional)}"
        )
    take_profits = tuple(row.get("take_profit_prices") or ())
    stops = tuple(row.get("stop_prices") or ())
    if not take_profits and _float(row.get("take_profit_price")) > 0.0:
        take_profits = (_float(row.get("take_profit_price")),)
    if not stops and _float(row.get("stop_price")) > 0.0:
        stops = (_float(row.get("stop_price")),)
    protection: list[str] = []
    protection.append(
        "TP " + ", ".join(f"${price:.8g}" for price in take_profits)
        if take_profits else "no venue TP"
    )
    protection.append(
        "SL " + ", ".join(f"${price:.8g}" for price in stops)
        if stops else "no venue stop"
    )
    lines.append(" \N{MIDDLE DOT} ".join(protection))
    leg_count = int(row.get("leg_count") or 0)
    if leg_count > 1:
        lines.append(f"{leg_count} strategy legs netted into this one Bybit position")
    return lines


def _account_now_line(payload: dict[str, Any]) -> str:
    if str(payload.get("cycle", {}).get("position_report_error") or ""):
        return ""
    summary = payload.get("bybit_position_summary", {})
    return (
        f"Account now: {int(summary.get('positions') or 0)} open \N{MIDDLE DOT} "
        f"exposure {format_usd(summary.get('position_value_usdt'))} \N{MIDDLE DOT} "
        f"uPnL {format_usd(summary.get('unrealized_pnl_usdt'), signed=True)}"
    )


def _zero_position_reduce_only_error(value: Any) -> bool:
    return "current position is zero" in str(value or "").lower()


def format_position_event_messages(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Return one venue-position update per opened/closed net position.

    Several strategy component rows can own one Bybit position, so rows are
    netted first. Conversely, unrelated symbols never share a Telegram event:
    every actual position gets its own complete message and dedupe identity.
    """
    cycle = payload.get("cycle", {})
    messages: list[tuple[str, str]] = []
    for raw_rows, closing in (
        (_event_rows(payload, closing=False), False),
        (_event_rows(payload, closing=True), True),
    ):
        for row in aggregate_position_event_rows(raw_rows, closing=closing):
            title = (
                "\N{WHITE HEAVY CHECK MARK} Position closed"
                if closing
                else "\N{LARGE GREEN CIRCLE} Position opened"
            )
            lines = [f"{title} \N{MIDDLE DOT} Bybit demo", _iso_dt(cycle.get("ts_ms"))]
            lines.extend(format_position_event_lines(row, closing=closing))
            account_line = _account_now_line(payload)
            if account_line:
                lines.append(account_line)
            event_ids = tuple(row.get("event_ids") or ())
            if not event_ids:
                event_ids = (
                    f"{row.get('symbol')}:{row.get('side')}:{_float(row.get('qty')):.12g}:"
                    f"{_float(row.get('entry_price')):.12g}:{_float(row.get('exit_price')):.12g}:"
                    f"{int(_float(cycle.get('ts_ms')))}",
                )
            key = "|".join(
                [
                    "close" if closing else "open",
                    str(row.get("symbol") or "UNKNOWN"),
                    str(row.get("side") or "position"),
                    ",".join(event_ids),
                ]
            )
            messages.append((key, "\n".join(lines)[:3900]))
    return messages


def format_telegram_status_message(payload: dict[str, Any]) -> str:
    """Compact event alert; the hourly digest owns whole-portfolio reporting."""
    cycle = payload["cycle"]
    reason = _telegram_notification_reason(payload)
    opening_rows = aggregate_position_event_rows(
        _event_rows(payload, closing=False),
        closing=False,
    )
    closing_rows = aggregate_position_event_rows(
        _event_rows(payload, closing=True),
        closing=True,
    )
    title = _TELEGRAM_REASON_TITLES.get(reason, "\N{INFORMATION SOURCE} Trading update")
    if opening_rows and closing_rows:
        title = "\N{CLOCKWISE OPEN CIRCLE ARROW} Position updates"
    elif closing_rows and reason == "position_reconciled":
        title = "\N{WHITE HEAVY CHECK MARK} Position closed"
    lines = [
        f"{title} \N{MIDDLE DOT} Bybit demo",
        _iso_dt(cycle.get("ts_ms")),
    ]

    displayed = 0
    for event_rows, closing in ((opening_rows, False), (closing_rows, True)):
        for row in event_rows:
            if displayed >= 6:
                break
            if displayed:
                lines.append("")
            lines.extend(format_position_event_lines(row, closing=closing))
            displayed += 1

    position_error = str(cycle.get("position_report_error") or "")
    wallet_error = str(cycle.get("wallet_error") or "")
    if position_error:
        lines.append(f"Bybit positions could not be verified: {position_error[:240]}")
    if wallet_error:
        lines.append(f"Wallet could not be verified: {wallet_error[:240]}")

    flat_rejection_symbols: set[str] = set()
    for row in (payload.get("entry_orders", []) or []) + (payload.get("exit_orders", []) or []):
        if (
            str(row.get("submit_mode") or "") != "error"
            and str(row.get("status") or "") not in {"failed", "rejected", "submitted_unconfirmed", "partial"}
        ):
            continue
        symbol = str(row.get("symbol") or "UNKNOWN")
        error = str(row.get("error") or "")
        if _zero_position_reduce_only_error(error):
            if symbol not in flat_rejection_symbols:
                lines.append(
                    f"{symbol}: Bybit confirmed this position was already flat; "
                    "further close retries are suppressed."
                )
                flat_rejection_symbols.add(symbol)
            continue
        lines.append(
            f"{symbol}: "
            f"{str(error or row.get('status') or 'order not confirmed')[:240]}"
        )

    for row in (payload.get("stop_repairs", []) or [])[:4]:
        lines.append(
            f"{row.get('symbol', 'UNKNOWN')}: stop ${_float(row.get('stop_price')):.8g}, "
            f"TP ${_float(row.get('take_profit_price')):.8g} "
            f"({row.get('submit_mode') or row.get('status') or 'unknown'})"
        )
    for row in (payload.get("untracked_positions", []) or [])[:4]:
        lines.append(
            f"{row.get('symbol', 'UNKNOWN')} {str(row.get('side') or '').upper()} "
            f"qty {_float(row.get('qty')):g} exists on Bybit but has no owning ledger row."
        )
    pending_positions = aggregate_position_event_rows(
        payload.get("pending_orphan_positions", []) or [],
        closing=False,
    )
    for row in pending_positions[:4]:
        leg_count = int(row.get("leg_count") or 1)
        row_label = "row" if leg_count == 1 else "component rows"
        lines.append(
            f"{row.get('symbol', 'UNKNOWN')} {str(row.get('side') or 'position').upper()}: "
            f"Bybit is flat; {leg_count} local {row_label} await confirmed close P&L."
        )

    if not position_error:
        lines.append(_account_now_line(payload))
    return "\n".join(lines)[:3900]

def _telegram_notification_reason(payload: dict[str, Any]) -> str:
    cycle = payload.get("cycle", {})
    if cycle.get("position_report_error"):
        return "position_report_error"
    # A wallet-read outage must page the operator: it silently degrades equity to
    # the fixed fallback (masking real equity drift and sizing off a phantom
    # balance), so surface it as a notification trigger rather than letting a pure
    # wallet outage pass as a quiet cycle.
    if cycle.get("wallet_error"):
        return "wallet_error"
    if payload.get("reconciliations"):
        return "position_reconciled"
    if payload.get("pending_orphan_positions"):
        return "position_close_pending_pnl"
    if any(
        str(row.get("submit_mode", "")) == "error" or str(row.get("status", "")) == "failed"
        for row in payload.get("entry_orders", [])
    ):
        return "entry_order_error"
    if any(
        str(row.get("entry_stop_update_status", "")) == "failed"
        for row in (payload.get("entries") or [])
        + (payload.get("entry_orders") or [])
        + (payload.get("pending_fill_trades") or [])
        + (payload.get("pending_fill_orders") or [])
    ):
        return "entry_stop_update_failed"
    if any(str(row.get("submit_mode", "")) == "error" for row in payload.get("exit_orders", [])):
        return "risk_order_error"
    if payload.get("stop_repairs"):
        if any(str(row.get("submit_mode", "")) == "error" for row in payload.get("stop_repairs", [])):
            return "stop_repair_failed"
        if any(str(row.get("submit_mode", "")) == "submitted" for row in payload.get("stop_repairs", [])):
            return "stop_repaired"
        return "stop_repair_planned"
    if payload.get("untracked_positions"):
        return "untracked_position"
    if cycle.get("reason") == "untracked_exit_submitted":
        return "untracked_position_exit"
    if int(cycle.get("entries_executed") or 0) > 0:
        return "entry_executed"
    if int(cycle.get("exits_executed") or 0) > 0:
        return "exit_executed"
    if int(cycle.get("pending_entry_fills_reconciled") or 0) > 0:
        return "entry_fill_reconciled"
    if int(cycle.get("pending_exit_fills_reconciled") or 0) > 0:
        return "exit_fill_reconciled"
    if any(str(row.get("status", "")) in {"partial", "submitted_unconfirmed"} for row in payload.get("entry_orders", [])):
        return "entry_order_unconfirmed"
    if any(str(row.get("status", "")) in {"partial", "submitted_unconfirmed"} for row in payload.get("exit_orders", [])):
        return "exit_order_unconfirmed"
    return ""

# NOTE: _maybe_notify deliberately lives in event_demo.py (the hub), NOT here.
# It is the only telegram function with a test-patchability contract: several
# tests monkeypatch `liquidity_migration.event_demo.send_telegram_message` and
# expect that to intercept the notify call. Keeping _maybe_notify in the hub
# (where send_telegram_message is imported) preserves that contract. The pure
# formatters above (format_telegram_status_message, _telegram_notification_reason)
# have no such contract and live here.
