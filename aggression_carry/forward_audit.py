from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import blake2b
from pathlib import Path
from typing import Any

import polars as pl

from .demo_cycle import DEMO_CYCLE_SLEEVES
from .storage import read_dataset
from .telegram import send_telegram_message


TELEGRAM_EVENT_LOOKBACK_HOURS = 36
TELEGRAM_EOD_READY_MINUTE = 2 * 60 + 35

TRADE_AUDIT_SCHEMA = {
    "sleeve": pl.String,
    "date": pl.String,
    "basket_id": pl.String,
    "paper_trade_id": pl.String,
    "symbol": pl.String,
    "side": pl.String,
    "paper_status": pl.String,
    "paper_entry_time": pl.String,
    "paper_entry_price": pl.Float64,
    "paper_exit_time": pl.String,
    "paper_exit_price": pl.Float64,
    "paper_exit_reason": pl.String,
    "paper_weight": pl.Float64,
    "paper_net_return": pl.Float64,
    "paper_weighted_net_return": pl.Float64,
    "entry_order_link_id": pl.String,
    "entry_order_status": pl.String,
    "entry_reconciled_status": pl.String,
    "entry_fill_status": pl.String,
    "entry_order_price": pl.Float64,
    "entry_avg_fill_price": pl.Float64,
    "entry_filled_qty": pl.Float64,
    "entry_filled_value": pl.Float64,
    "entry_slippage_bps": pl.Float64,
    "exit_order_link_id": pl.String,
    "exit_order_status": pl.String,
    "exit_reconciled_status": pl.String,
    "exit_fill_status": pl.String,
    "exit_avg_fill_price": pl.Float64,
    "exit_filled_qty": pl.Float64,
    "exit_filled_value": pl.Float64,
    "exit_slippage_bps": pl.Float64,
    "demo_realized_pnl_usdt": pl.Float64,
    "demo_realized_return": pl.Float64,
    "missed_reason": pl.String,
    "order_error": pl.String,
}

DAILY_AUDIT_SCHEMA = {
    "date": pl.String,
    "sleeve": pl.String,
    "paper_trades": pl.Int64,
    "paper_closed_trades": pl.Int64,
    "paper_weighted_net_return": pl.Float64,
    "demo_entry_orders": pl.Int64,
    "demo_entries_filled": pl.Int64,
    "demo_exits_filled": pl.Int64,
    "demo_missed_entries": pl.Int64,
    "demo_realized_pnl_usdt": pl.Float64,
    "demo_realized_return": pl.Float64,
    "avg_entry_slippage_bps": pl.Float64,
    "avg_exit_slippage_bps": pl.Float64,
}


def run_forward_demo_audit(
    data_root: str | Path,
    *,
    report_dir: str | Path | None = None,
    sleeves: tuple[str, ...] = DEMO_CYCLE_SLEEVES,
    send_telegram: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    base_root = Path(data_root).expanduser()
    output_dir = Path(report_dir or base_root / "reports")
    now_dt = _as_utc(now or datetime.now(tz=UTC))
    rows: list[dict[str, Any]] = []
    for sleeve in sleeves:
        sleeve_root = base_root / "forward_sleeves" / sleeve
        paper = read_dataset(sleeve_root, "forward_paper_trades")
        orders = read_dataset(sleeve_root, "demo_execution_orders")
        rows.extend(build_forward_demo_audit_rows(sleeve, paper, orders))

    trades = _frame(rows, TRADE_AUDIT_SCHEMA)
    daily = build_forward_demo_daily_summary(trades)
    summary = summarize_forward_demo_audit(trades, daily)
    payload = {
        "now": now_dt.isoformat(),
        "rows": {
            "sleeves": len(sleeves),
            "trade_audit_rows": trades.height,
            "daily_rows": daily.height,
        },
        "summary": summary,
        "sleeves": list(sleeves),
    }
    payload["telegram"] = _maybe_send_forward_demo_audit_telegram(
        output_dir,
        trades,
        daily,
        enabled=send_telegram,
        now=now_dt,
    )
    _write_forward_demo_audit_outputs(output_dir, payload, trades, daily)
    return payload


def build_forward_demo_audit_rows(sleeve: str, paper: pl.DataFrame, orders: pl.DataFrame) -> list[dict[str, Any]]:
    if paper.is_empty():
        return []
    order_rows = orders.to_dicts() if not orders.is_empty() else []
    output: list[dict[str, Any]] = []
    for trade in paper.sort(["entry_ts_ms", "symbol"]).to_dicts():
        trade_id = str(trade.get("trade_id") or "")
        entry = _latest_order(order_rows, trade_id, "entry")
        exit_order = _latest_order(order_rows, trade_id, "exit")
        side = str(trade.get("side") or "short").lower()
        paper_entry = _num(trade.get("entry_price"))
        paper_exit = _num(trade.get("exit_price"))
        entry_fill_price = _avg_fill_price(entry)
        exit_fill_price = _avg_fill_price(exit_order)
        demo_pnl, demo_return = _demo_realized_pnl(side, entry, exit_order)
        row = {
            "sleeve": sleeve,
            "date": str(trade.get("date") or ""),
            "basket_id": str(trade.get("basket_id") or ""),
            "paper_trade_id": trade_id,
            "symbol": str(trade.get("symbol") or ""),
            "side": side,
            "paper_status": str(trade.get("status") or ""),
            "paper_entry_time": str(trade.get("entry_time") or ""),
            "paper_entry_price": paper_entry,
            "paper_exit_time": str(trade.get("exit_time") or ""),
            "paper_exit_price": paper_exit,
            "paper_exit_reason": str(trade.get("exit_reason") or ""),
            "paper_weight": _num(trade.get("weight")),
            "paper_net_return": _num(trade.get("net_return")),
            "paper_weighted_net_return": _paper_weighted_net_return(trade),
            "entry_order_link_id": _str(entry, "order_link_id"),
            "entry_order_status": _str(entry, "status"),
            "entry_reconciled_status": _str(entry, "reconciled_status"),
            "entry_fill_status": _fill_status(entry),
            "entry_order_price": _num(_order_value(entry, "price")),
            "entry_avg_fill_price": entry_fill_price,
            "entry_filled_qty": _num(_order_value(entry, "filled_qty")),
            "entry_filled_value": _num(_order_value(entry, "filled_value")),
            "entry_slippage_bps": _entry_slippage_bps(side, paper_entry, entry_fill_price),
            "exit_order_link_id": _str(exit_order, "order_link_id"),
            "exit_order_status": _str(exit_order, "status"),
            "exit_reconciled_status": _str(exit_order, "reconciled_status"),
            "exit_fill_status": _fill_status(exit_order),
            "exit_avg_fill_price": exit_fill_price,
            "exit_filled_qty": _num(_order_value(exit_order, "filled_qty")),
            "exit_filled_value": _num(_order_value(exit_order, "filled_value")),
            "exit_slippage_bps": _exit_slippage_bps(side, paper_exit, exit_fill_price),
            "demo_realized_pnl_usdt": demo_pnl,
            "demo_realized_return": demo_return,
            "missed_reason": _missed_reason(trade, entry, exit_order),
            "order_error": _order_errors(entry, exit_order),
        }
        output.append(row)
    return output


def build_forward_demo_daily_summary(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return _frame([], DAILY_AUDIT_SCHEMA)
    rows: list[dict[str, Any]] = []
    for key, frame in trades.group_by(["date", "sleeve"], maintain_order=True):
        date, sleeve = key
        demo_entry_notional = _sum_non_null(frame, "entry_filled_value")
        demo_pnl = _sum_non_null(frame, "demo_realized_pnl_usdt")
        rows.append(
            {
                "date": str(date),
                "sleeve": str(sleeve),
                "paper_trades": frame.height,
                "paper_closed_trades": int((frame["paper_status"] == "closed").sum()),
                "paper_weighted_net_return": _sum_non_null(frame, "paper_weighted_net_return"),
                "demo_entry_orders": int((frame["entry_order_link_id"] != "").sum()),
                "demo_entries_filled": int((frame["entry_filled_qty"] > 0.0).sum()),
                "demo_exits_filled": int((frame["exit_filled_qty"] > 0.0).sum()),
                "demo_missed_entries": int((frame["missed_reason"] != "").sum()),
                "demo_realized_pnl_usdt": demo_pnl,
                "demo_realized_return": demo_pnl / demo_entry_notional if demo_entry_notional > 0.0 else None,
                "avg_entry_slippage_bps": _mean_non_null(frame, "entry_slippage_bps"),
                "avg_exit_slippage_bps": _mean_non_null(frame, "exit_slippage_bps"),
            }
        )
    return _frame(rows, DAILY_AUDIT_SCHEMA).sort(["date", "sleeve"])


def summarize_forward_demo_audit(trades: pl.DataFrame, daily: pl.DataFrame) -> dict[str, Any]:
    if trades.is_empty():
        return {
            "paper_trades": 0,
            "paper_closed_trades": 0,
            "demo_entry_orders": 0,
            "demo_entries_filled": 0,
            "demo_exits_filled": 0,
            "demo_missed_entries": 0,
            "paper_weighted_net_return": 0.0,
            "demo_realized_pnl_usdt": 0.0,
            "demo_realized_return": None,
        }
    demo_entry_notional = _sum_non_null(trades, "entry_filled_value")
    demo_pnl = _sum_non_null(trades, "demo_realized_pnl_usdt")
    return {
        "paper_trades": trades.height,
        "paper_closed_trades": int((trades["paper_status"] == "closed").sum()),
        "demo_entry_orders": int((trades["entry_order_link_id"] != "").sum()),
        "demo_entries_filled": int((trades["entry_filled_qty"] > 0.0).sum()),
        "demo_exits_filled": int((trades["exit_filled_qty"] > 0.0).sum()),
        "demo_missed_entries": int((trades["missed_reason"] != "").sum()),
        "paper_weighted_net_return": _sum_non_null(trades, "paper_weighted_net_return"),
        "demo_realized_pnl_usdt": demo_pnl,
        "demo_realized_return": demo_pnl / demo_entry_notional if demo_entry_notional > 0.0 else None,
        "daily_rows": daily.height,
    }


def format_forward_demo_audit_report(payload: dict[str, Any], trades: pl.DataFrame, daily: pl.DataFrame) -> str:
    summary = payload.get("summary", {})
    lines = [
        "# Forward Demo Audit",
        "",
        "This report joins the paper forward-test ledger to the Bybit demo execution ledger by sleeve and paper trade ID.",
        "Accepted demo order acknowledgements are not treated as fills; slippage and demo PnL require filled quantity/value from reconciliation.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Paper trades | {summary.get('paper_trades', 0)} |",
        f"| Paper closed trades | {summary.get('paper_closed_trades', 0)} |",
        f"| Demo entry orders | {summary.get('demo_entry_orders', 0)} |",
        f"| Demo entries filled | {summary.get('demo_entries_filled', 0)} |",
        f"| Demo exits filled | {summary.get('demo_exits_filled', 0)} |",
        f"| Demo missed/pending reasons | {summary.get('demo_missed_entries', 0)} |",
        f"| Paper weighted net | {_pct(summary.get('paper_weighted_net_return'))} |",
        f"| Demo realized PnL | {_money(summary.get('demo_realized_pnl_usdt'))} |",
        f"| Demo realized return | {_pct(summary.get('demo_realized_return'))} |",
        "",
        "## Daily PnL Comparison",
        "",
        "| Date | Sleeve | Paper Trades | Paper Closed | Paper Net | Demo Entry Fills | Demo Exit Fills | Missed/Pending | Demo PnL | Demo Ret | Entry Slip bps | Exit Slip bps |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in daily.sort(["date", "sleeve"]).to_dicts() if not daily.is_empty() else []:
        lines.append(
            f"| {row.get('date', '')} | {row.get('sleeve', '')} | {row.get('paper_trades', 0)} | "
            f"{row.get('paper_closed_trades', 0)} | {_pct(row.get('paper_weighted_net_return'))} | "
            f"{row.get('demo_entries_filled', 0)} | {row.get('demo_exits_filled', 0)} | "
            f"{row.get('demo_missed_entries', 0)} | {_money(row.get('demo_realized_pnl_usdt'))} | "
            f"{_pct(row.get('demo_realized_return'))} | {_num_text(row.get('avg_entry_slippage_bps'))} | "
            f"{_num_text(row.get('avg_exit_slippage_bps'))} |"
        )
    lines.extend(
        [
            "",
            "## Recent Trade Audit",
            "",
            "| Date | Sleeve | Symbol | Paper | Entry Fill | Exit Fill | Entry Slip bps | Exit Slip bps | Exit Reason | Demo PnL | Missed/Pending |",
            "|---|---|---|---|---|---|---:|---:|---|---:|---|",
        ]
    )
    if not trades.is_empty():
        for row in trades.sort(["date", "sleeve", "paper_entry_time"], descending=[True, False, True]).head(75).to_dicts():
            lines.append(
                f"| {row.get('date', '')} | {row.get('sleeve', '')} | {row.get('symbol', '')} | "
                f"{row.get('paper_status', '')} {_pct(row.get('paper_weighted_net_return'))} | "
                f"{row.get('entry_fill_status', '')} | {row.get('exit_fill_status', '')} | "
                f"{_num_text(row.get('entry_slippage_bps'))} | {_num_text(row.get('exit_slippage_bps'))} | "
                f"{row.get('paper_exit_reason', '')} | {_money(row.get('demo_realized_pnl_usdt'))} | "
                f"{str(row.get('missed_reason') or row.get('order_error') or '')[:120]} |"
            )
    lines.extend(
        [
            "",
            "Files:",
            "",
            "- `reports/forward_demo_audit_trades.csv` has one row per paper trade with joined entry/exit demo state.",
            "- `reports/forward_demo_audit_daily.csv` has sleeve/date paper-vs-demo PnL comparison.",
            "",
        ]
    )
    return "\n".join(lines)


def format_forward_demo_audit_message(events: list[dict[str, Any]]) -> str:
    lines = ["Forward audit events"]
    entries = [event for event in events if event.get("kind") == "entry"]
    exits = [event for event in events if event.get("kind") == "exit"]
    eod = [event for event in events if event.get("kind") == "eod"]
    if entries:
        lines.append("Positions entered:")
        for event in entries:
            lines.append(
                f"- {event.get('sleeve')} {event.get('symbol')} {event.get('side')} "
                f"qty={_qty(event.get('qty'))} fill={_price(event.get('fill_price'))} "
                f"paper={_price(event.get('paper_price'))} slip={_num_text(event.get('slippage_bps'))}bps"
            )
    if exits:
        lines.append("Positions exited:")
        for event in exits:
            lines.append(
                f"- {event.get('sleeve')} {event.get('symbol')} {event.get('exit_reason')} "
                f"qty={_qty(event.get('qty'))} fill={_price(event.get('fill_price'))} "
                f"pnl={_money(event.get('pnl_usdt'))} slip={_num_text(event.get('slippage_bps'))}bps"
            )
    if eod:
        lines.append("End-of-day PnL:")
        for event in eod:
            lines.append(
                f"- {event.get('date')} total demo={_money(event.get('demo_pnl_usdt'))} "
                f"paper={_pct(event.get('paper_return'))}"
            )
            for sleeve in event.get("sleeves", []):
                lines.append(
                    f"  {sleeve.get('sleeve')}: demo={_money(sleeve.get('demo_pnl_usdt'))} "
                    f"paper={_pct(sleeve.get('paper_return'))}"
                )
    return "\n".join(lines)


def _write_forward_demo_audit_outputs(
    output_dir: Path,
    payload: dict[str, Any],
    trades: pl.DataFrame,
    daily: pl.DataFrame,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "forward_demo_audit_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "forward_demo_audit_report.md").write_text(
        format_forward_demo_audit_report(payload, trades, daily),
        encoding="utf-8",
    )
    trades.write_csv(output_dir / "forward_demo_audit_trades.csv")
    daily.write_csv(output_dir / "forward_demo_audit_daily.csv")


def _maybe_send_forward_demo_audit_telegram(
    output_dir: Path,
    trades: pl.DataFrame,
    daily: pl.DataFrame,
    *,
    enabled: bool,
    now: datetime,
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False, "sent": False, "reason": "disabled"}
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "forward_demo_audit_telegram_state.json"
    previous = _read_json(state_path)
    sent_event_ids = set(previous.get("sent_event_ids") or [])
    events = _forward_demo_audit_telegram_events(trades, daily, now=now)
    new_events = [event for event in events if str(event.get("id") or "") not in sent_event_ids]
    if not new_events:
        return {"enabled": True, "sent": False, "reason": "no_trade_signal", "events": 0}
    text = format_forward_demo_audit_message(new_events)
    signature = blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
    if previous.get("signature") == signature:
        return {"enabled": True, "sent": False, "reason": "unchanged", "signature": signature}
    try:
        sent = send_telegram_message(text, enabled=True)
    except Exception as exc:  # noqa: BLE001 - Telegram must not break audit persistence
        return {"enabled": True, "sent": False, "reason": f"send_failed: {exc}", "signature": signature}
    if not sent:
        return {"enabled": True, "sent": False, "reason": "not_configured_or_rejected", "signature": signature}
    updated_ids = sorted((sent_event_ids | {str(event["id"]) for event in new_events}))[-1000:]
    state_path.write_text(
        json.dumps({"signature": signature, "sent_event_ids": updated_ids}, indent=2),
        encoding="utf-8",
    )
    return {"enabled": True, "sent": True, "reason": "sent", "signature": signature, "events": len(new_events)}


def _forward_demo_audit_telegram_events(
    trades: pl.DataFrame,
    daily: pl.DataFrame,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not trades.is_empty():
        for row in trades.sort(["date", "sleeve", "paper_entry_time"]).to_dicts():
            entry_time = _parse_dt(row.get("paper_entry_time"))
            if _recent_enough(entry_time, now):
                filled_qty = _num(row.get("entry_filled_qty"))
                order_link_id = str(row.get("entry_order_link_id") or "")
                if filled_qty > 0.0 and order_link_id:
                    events.append(
                        {
                            "id": f"entry:{row.get('sleeve')}:{row.get('paper_trade_id')}:{order_link_id}",
                            "kind": "entry",
                            "sleeve": row.get("sleeve"),
                            "symbol": row.get("symbol"),
                            "side": row.get("side"),
                            "qty": filled_qty,
                            "fill_price": row.get("entry_avg_fill_price"),
                            "paper_price": row.get("paper_entry_price"),
                            "slippage_bps": row.get("entry_slippage_bps"),
                        }
                    )
            exit_time = _parse_dt(row.get("paper_exit_time"))
            if _recent_enough(exit_time, now):
                filled_qty = _num(row.get("exit_filled_qty"))
                order_link_id = str(row.get("exit_order_link_id") or "")
                if filled_qty > 0.0 and order_link_id:
                    events.append(
                        {
                            "id": f"exit:{row.get('sleeve')}:{row.get('paper_trade_id')}:{order_link_id}",
                            "kind": "exit",
                            "sleeve": row.get("sleeve"),
                            "symbol": row.get("symbol"),
                            "exit_reason": row.get("paper_exit_reason"),
                            "qty": filled_qty,
                            "fill_price": row.get("exit_avg_fill_price"),
                            "pnl_usdt": row.get("demo_realized_pnl_usdt"),
                            "slippage_bps": row.get("exit_slippage_bps"),
                        }
                    )
    events.extend(_eod_telegram_events(daily, now=now))
    return events


def _eod_telegram_events(daily: pl.DataFrame, *, now: datetime) -> list[dict[str, Any]]:
    if daily.is_empty():
        return []
    rows = daily.to_dicts()
    ready_dates = sorted({str(row.get("date") or "") for row in rows if _eod_ready(str(row.get("date") or ""), now)})
    if not ready_dates:
        return []
    latest_date = ready_dates[-1]
    day_rows = [row for row in rows if str(row.get("date") or "") == latest_date]
    entry_fills = sum(int(_num(row.get("demo_entries_filled"))) for row in day_rows)
    exit_fills = sum(int(_num(row.get("demo_exits_filled"))) for row in day_rows)
    if entry_fills <= 0 and exit_fills <= 0:
        return []
    if entry_fills > exit_fills:
        return []
    sleeves = [
        {
            "sleeve": row.get("sleeve"),
            "demo_pnl_usdt": row.get("demo_realized_pnl_usdt"),
            "paper_return": row.get("paper_weighted_net_return"),
        }
        for row in sorted(day_rows, key=lambda item: str(item.get("sleeve") or ""))
    ]
    return [
        {
            "id": f"eod:{latest_date}",
            "kind": "eod",
            "date": latest_date,
            "demo_pnl_usdt": sum(_num(row.get("demo_realized_pnl_usdt")) for row in day_rows),
            "paper_return": sum(_num(row.get("paper_weighted_net_return")) for row in day_rows),
            "sleeves": sleeves,
        }
    ]


def _eod_ready(value: str, now: datetime) -> bool:
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return False
    today = now.date()
    if day >= today:
        return False
    if (today - day).days > 2:
        return False
    if (today - day).days > 1:
        return True
    return now.hour * 60 + now.minute >= TELEGRAM_EOD_READY_MINUTE


def _recent_enough(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return False
    return now - timedelta(hours=TELEGRAM_EVENT_LOOKBACK_HOURS) <= value <= now + timedelta(minutes=5)


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _latest_order(rows: list[dict[str, Any]], trade_id: str, action: str) -> dict[str, Any] | None:
    candidates = [
        row
        for row in rows
        if str(row.get("paper_trade_id") or "") == trade_id and str(row.get("action") or "") == action
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: int(_num(row.get("created_ts_ms")) or 0))


def _avg_fill_price(order: dict[str, Any] | None) -> float | None:
    qty = _num(_order_value(order, "filled_qty"))
    value = _num(_order_value(order, "filled_value"))
    if qty > 0.0 and value > 0.0:
        return value / qty
    return None


def _fill_status(order: dict[str, Any] | None) -> str:
    if order is None:
        return "missing"
    if _num(order.get("filled_qty")) > 0.0:
        status = str(order.get("reconciled_status") or order.get("status") or "filled")
        return status if status else "filled"
    return str(order.get("reconciled_status") or order.get("status") or "unknown")


def _missed_reason(trade: dict[str, Any], entry: dict[str, Any] | None, exit_order: dict[str, Any] | None) -> str:
    if entry is None:
        return "entry_order_missing"
    entry_status = str(entry.get("status") or "")
    entry_reconciled = str(entry.get("reconciled_status") or "")
    entry_error = str(entry.get("error") or entry.get("reconcile_error") or "")
    if _num(entry.get("filled_qty")) <= 0.0:
        if entry_status == "dry_run":
            return "dry_run_not_submitted"
        if entry_status in {"skipped", "place_failed", "cancel_failed"}:
            return entry_error or entry_status
        if entry_reconciled in {"cancelled", "missed_entry"}:
            return entry_reconciled
        if entry_status in {"accepted", "placed"} or entry_reconciled in {"accepted", "open_order_seen"}:
            return "entry_not_filled_yet"
    if str(trade.get("status") or "") == "closed":
        if exit_order is None:
            return "exit_order_missing"
        if _num(exit_order.get("filled_qty")) <= 0.0:
            exit_status = str(exit_order.get("status") or "")
            exit_reconciled = str(exit_order.get("reconciled_status") or "")
            exit_error = str(exit_order.get("error") or exit_order.get("reconcile_error") or "")
            if exit_status == "dry_run":
                return "exit_dry_run_not_submitted"
            if exit_status in {"skipped", "place_failed", "cancel_failed"}:
                return exit_error or exit_status
            if exit_reconciled:
                return f"exit_{exit_reconciled}"
            return "exit_not_filled_yet"
    return ""


def _order_errors(*orders: dict[str, Any] | None) -> str:
    errors = []
    for order in orders:
        if order is None:
            continue
        error = str(order.get("error") or order.get("reconcile_error") or "")
        if error:
            errors.append(error)
    return "; ".join(errors)


def _demo_realized_pnl(
    side: str,
    entry: dict[str, Any] | None,
    exit_order: dict[str, Any] | None,
) -> tuple[float | None, float | None]:
    entry_qty = _num(_order_value(entry, "filled_qty"))
    exit_qty = _num(_order_value(exit_order, "filled_qty"))
    entry_price = _avg_fill_price(entry)
    exit_price = _avg_fill_price(exit_order)
    if entry_qty <= 0.0 or exit_qty <= 0.0 or entry_price is None or exit_price is None:
        return None, None
    qty = min(entry_qty, exit_qty)
    if side == "short":
        pnl = (entry_price - exit_price) * qty
    else:
        pnl = (exit_price - entry_price) * qty
    entry_notional = entry_price * qty
    return pnl, pnl / entry_notional if entry_notional > 0.0 else None


def _entry_slippage_bps(side: str, paper_price: float, fill_price: float | None) -> float | None:
    if paper_price <= 0.0 or fill_price is None:
        return None
    if side == "short":
        return (paper_price - fill_price) / paper_price * 10_000.0
    return (fill_price - paper_price) / paper_price * 10_000.0


def _exit_slippage_bps(side: str, paper_price: float, fill_price: float | None) -> float | None:
    if paper_price <= 0.0 or fill_price is None:
        return None
    if side == "short":
        return (fill_price - paper_price) / paper_price * 10_000.0
    return (paper_price - fill_price) / paper_price * 10_000.0


def _paper_weighted_net_return(trade: dict[str, Any]) -> float:
    value = trade.get("weighted_net_return")
    if value is not None:
        return _num(value)
    return _num(trade.get("net_return")) * _num(trade.get("weight"))


def _order_value(order: dict[str, Any] | None, key: str) -> Any:
    if order is None:
        return None
    return order.get(key)


def _str(order: dict[str, Any] | None, key: str) -> str:
    return str(_order_value(order, key) or "")


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sum_non_null(frame: pl.DataFrame, column: str) -> float:
    if column not in frame.columns:
        return 0.0
    values = [float(value) for value in frame[column].drop_nulls().to_list()]
    return sum(values)


def _mean_non_null(frame: pl.DataFrame, column: str) -> float | None:
    if column not in frame.columns:
        return None
    values = [float(value) for value in frame[column].drop_nulls().to_list()]
    return sum(values) / len(values) if values else None


def _frame(rows: list[dict[str, Any]], schema: dict[str, pl.DataType]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema, infer_schema_length=None)


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{_num(value):.2%}"


def _money(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{_num(value):.4f}"


def _num_text(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{_num(value):.2f}"


def _price(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{_num(value):.8g}"


def _qty(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{_num(value):.8g}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
