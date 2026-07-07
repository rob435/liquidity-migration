"""Extracted from event_demo.py — see that module's docstring.

This sibling holds a cohesive slice of the event-demo machinery. It
imports shared helpers/configs from event_demo.py (the hub); the hub
re-imports this module's public names at the bottom so external callers
(`from liquidity_migration.event_demo import X`) keep working unchanged.
"""

from __future__ import annotations

import logging
from typing import Any




from .event_demo import (  # noqa: F401  (shared hub helpers)
    _float,
    _iso_dt,
)

_logger = logging.getLogger(__name__)


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

def format_telegram_status_message(payload: dict[str, Any]) -> str:
    cycle = payload["cycle"]
    bybit_summary = payload.get("bybit_position_summary", {})
    ledger_summary = payload.get("ledger_position_summary", {})
    reason = _telegram_notification_reason(payload)
    mode = str(cycle.get("mode", ""))
    # equity 0.0 means "no successful wallet read yet", not a $0 account — never
    # print $0.00 on an operator-facing safety message (audit 2026-06-12 round 3).
    equity = _float(cycle.get("equity_usdt"))
    equity_text = f"${equity:,.2f}" if equity > 0.0 else "NA"
    # A wallet-API failure makes _safe_wallet_equity_usdt substitute the fixed
    # fallback equity (default $10,000), which would otherwise print as a clean
    # read and silently mask the outage. When wallet_error is set, tag the equity
    # as a fallback so the operator cannot mistake it for a successful read.
    wallet_error = str(cycle.get("wallet_error") or "")
    if wallet_error and equity > 0.0:
        equity_text += " (FALLBACK — wallet read failed)"
    if mode.startswith("ws_risk"):
        # ws_risk counters are CUMULATIVE since engine start, and exit_candidates
        # is the tracked-order count — presenting them as per-cycle "exits=37/41"
        # misread a quiet cycle as a mass exit (audit 2026-06-12 round 3).
        header = "liquidity-migration | Bybit ws_risk engine"
        counters = (
            f"since-start: exits={cycle['exits_executed']} "
            f"stop_repairs={cycle.get('stop_repairs', 0)} "
            f"orders_tracked={cycle['exit_candidates']}"
        )
    else:
        header = "liquidity-migration | Bybit demo cycle"
        counters = (
            f"entries={cycle['entries_executed']}/{cycle['entry_candidates']} "
            f"exits={cycle['exits_executed']}/{cycle['exit_candidates']}"
        )
    lines = [
        header,
        f"time={_iso_dt(cycle['ts_ms'])}",
        f"reason={reason or 'manual_status'}",
        f"mode={cycle['mode']} equity={equity_text}",
        counters,
        f"pending_fills={cycle.get('pending_order_fills_reconciled', 0)}",
        f"bybit_positions={bybit_summary.get('positions', 0)} "
        f"value=${_float(bybit_summary.get('position_value_usdt')):,.2f} "
        f"uPnL=${_float(bybit_summary.get('unrealized_pnl_usdt')):,.2f} "
        f"({_float(bybit_summary.get('pnl_pct')):.2%})",
    ]
    if cycle.get("position_report_error"):
        lines.append(f"position_error={cycle['position_report_error']}")
    if wallet_error:
        lines.append(f"wallet_error={wallet_error}")
    bybit_rows = payload.get("bybit_positions", [])[:10]
    if bybit_rows:
        lines.append("Bybit positions:")
        for row in bybit_rows:
            lines.append(
                f"{row['symbol']} {row['side']} qty={_float(row['qty']):g} "
                f"value=${_float(row['position_value_usdt']):,.2f} "
                f"uPnL=${_float(row['unrealized_pnl_usdt']):,.2f} "
                f"({_float(row['pnl_pct']):.2%}) mark={_float(row['mark_price']):.8g} avg={_float(row['avg_price']):.8g}"
            )
    else:
        lines.append("Bybit positions: none")
    lines.append(
        f"ledger_open={ledger_summary.get('positions', 0)} (net) "
        f"value=${_float(ledger_summary.get('position_value_usdt')):,.2f} "
        f"uPnL=${_float(ledger_summary.get('unrealized_pnl_usdt')):,.2f} "
        f"({_float(ledger_summary.get('pnl_pct')):.2%})"
    )
    ledger_rows = payload.get("ledger_positions", [])[:6]
    if ledger_rows:
        lines.append("Ledger positions (net):")
        for row in ledger_rows:
            lines.append(
                f"{row['symbol']} {row['side']} qty={_float(row['qty']):g} "
                f"uPnL=${_float(row['unrealized_pnl_usdt']):,.2f} ({_float(row['pnl_pct']):.2%})"
            )
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
