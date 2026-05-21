"""Reconcile the paper (dry-run) ledger against the demo ledger.

The paper runner records idealized fills at the signal price; the demo runner
records actual Bybit demo fills. Pairing the two ledgers' trades by symbol,
side and entry time, then diffing their fill prices, measures execution
slippage — the cost the demo execution path pays that the idealized paper path
does not. Unpaired trades on either side are fill-rate divergence.
"""

from __future__ import annotations

from pathlib import Path
from statistics import mean, median
from typing import Any

import polars as pl

from .storage import read_dataset

DEFAULT_ENTRY_TOLERANCE_MS = 600_000


def _normalized_side(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"sell", "short"}:
        return "short"
    if text in {"buy", "long"}:
        return "long"
    return text


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clean_trades(trades: pl.DataFrame) -> list[dict[str, Any]]:
    if trades.is_empty():
        return []
    cleaned: list[dict[str, Any]] = []
    for row in trades.to_dicts():
        symbol = str(row.get("symbol") or "")
        side = _normalized_side(row.get("side"))
        entry_price = _float(row.get("entry_price"))
        qty = _float(row.get("qty"))
        if not symbol or side not in {"long", "short"} or entry_price <= 0.0 or qty <= 0.0:
            continue
        cleaned.append(
            {
                "trade_id": str(row.get("trade_id") or ""),
                "symbol": symbol,
                "side": side,
                "entry_ts_ms": _int(row.get("entry_ts_ms")),
                "entry_price": entry_price,
                "qty": qty,
                "status": str(row.get("status") or ""),
                "exit_price": _float(row.get("exit_price")),
            }
        )
    return cleaned


def _entry_slippage_bps(*, side: str, paper_entry: float, demo_entry: float) -> float:
    """Adverse entry slippage in basis points. Positive means the demo fill was
    worse than the idealized paper fill (sold lower / paid up); negative means
    the demo path got price improvement over the signal price."""
    if paper_entry <= 0.0:
        return 0.0
    if side == "short":  # selling to open — a lower fill price is adverse
        return (paper_entry - demo_entry) / paper_entry * 10_000.0
    return (demo_entry - paper_entry) / paper_entry * 10_000.0


def _exit_slippage_bps(*, side: str, paper_exit: float, demo_exit: float) -> float:
    """Adverse exit slippage in basis points. Closing a short is a buy (a higher
    fill price is adverse); closing a long is a sell (a lower price is adverse)."""
    if paper_exit <= 0.0:
        return 0.0
    if side == "short":
        return (demo_exit - paper_exit) / paper_exit * 10_000.0
    return (paper_exit - demo_exit) / paper_exit * 10_000.0


def _realized_return_pct(*, side: str, entry_price: float, exit_price: float) -> float:
    if entry_price <= 0.0 or exit_price <= 0.0:
        return 0.0
    if side == "short":
        return (entry_price - exit_price) / entry_price * 100.0
    return (exit_price - entry_price) / entry_price * 100.0


def reconcile_paper_demo(
    paper_trades: pl.DataFrame,
    demo_trades: pl.DataFrame,
    *,
    entry_tolerance_ms: int = DEFAULT_ENTRY_TOLERANCE_MS,
) -> dict[str, Any]:
    """Pair paper and demo trades by symbol/side/entry time and measure the
    fill-price slippage between them. Returns a JSON-serializable summary plus a
    per-pair breakdown. Pairing is greedy nearest-entry-time within each
    (symbol, side) group, bounded by ``entry_tolerance_ms``."""
    paper = _clean_trades(paper_trades)
    demo = _clean_trades(demo_trades)
    tolerance = max(int(entry_tolerance_ms), 0)

    paper_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for trade in paper:
        paper_by_key.setdefault((trade["symbol"], trade["side"]), []).append(trade)
    for bucket in paper_by_key.values():
        bucket.sort(key=lambda item: item["entry_ts_ms"])

    pairs: list[dict[str, Any]] = []
    matched: set[tuple[str, str, int]] = set()
    for demo_trade in sorted(demo, key=lambda item: item["entry_ts_ms"]):
        key = (demo_trade["symbol"], demo_trade["side"])
        bucket = paper_by_key.get(key, [])
        best_idx = -1
        best_gap = tolerance + 1
        for idx, paper_trade in enumerate(bucket):
            if (key[0], key[1], idx) in matched:
                continue
            gap = abs(demo_trade["entry_ts_ms"] - paper_trade["entry_ts_ms"])
            if gap <= tolerance and gap < best_gap:
                best_gap = gap
                best_idx = idx
        if best_idx < 0:
            continue
        matched.add((key[0], key[1], best_idx))
        paper_trade = bucket[best_idx]
        side = demo_trade["side"]
        both_closed = (
            demo_trade["status"] == "closed"
            and paper_trade["status"] == "closed"
            and demo_trade["exit_price"] > 0.0
            and paper_trade["exit_price"] > 0.0
        )
        exit_bps: float | None = None
        paper_return: float | None = None
        demo_return: float | None = None
        if both_closed:
            exit_bps = _exit_slippage_bps(
                side=side, paper_exit=paper_trade["exit_price"], demo_exit=demo_trade["exit_price"]
            )
            paper_return = _realized_return_pct(
                side=side, entry_price=paper_trade["entry_price"], exit_price=paper_trade["exit_price"]
            )
            demo_return = _realized_return_pct(
                side=side, entry_price=demo_trade["entry_price"], exit_price=demo_trade["exit_price"]
            )
        pairs.append(
            {
                "symbol": demo_trade["symbol"],
                "side": side,
                "paper_trade_id": paper_trade["trade_id"],
                "demo_trade_id": demo_trade["trade_id"],
                "entry_gap_ms": abs(demo_trade["entry_ts_ms"] - paper_trade["entry_ts_ms"]),
                "paper_entry_price": paper_trade["entry_price"],
                "demo_entry_price": demo_trade["entry_price"],
                "entry_slippage_bps": _entry_slippage_bps(
                    side=side, paper_entry=paper_trade["entry_price"], demo_entry=demo_trade["entry_price"]
                ),
                "exit_slippage_bps": exit_bps,
                "paper_return_pct": paper_return,
                "demo_return_pct": demo_return,
            }
        )

    entry_bps = [pair["entry_slippage_bps"] for pair in pairs]
    exit_bps = [pair["exit_slippage_bps"] for pair in pairs if pair["exit_slippage_bps"] is not None]
    summary = {
        "paper_trades": len(paper),
        "demo_trades": len(demo),
        "paired": len(pairs),
        "paper_only": len(paper) - len(pairs),
        "demo_only": len(demo) - len(pairs),
        "closed_pairs": len(exit_bps),
        "entry_tolerance_ms": tolerance,
        "entry_slippage_bps_mean": mean(entry_bps) if entry_bps else 0.0,
        "entry_slippage_bps_median": median(entry_bps) if entry_bps else 0.0,
        "entry_slippage_bps_worst": max(entry_bps) if entry_bps else 0.0,
        "exit_slippage_bps_mean": mean(exit_bps) if exit_bps else 0.0,
        "exit_slippage_bps_median": median(exit_bps) if exit_bps else 0.0,
    }
    return {"summary": summary, "pairs": pairs}


def format_reconciliation_report(result: dict[str, Any]) -> str:
    """Render a reconciliation result (from reconcile_paper_demo) as markdown."""
    summary = result["summary"]
    lines = [
        "# Paper vs Demo Reconciliation",
        "",
        f"- paper trades: {summary['paper_trades']}",
        f"- demo trades: {summary['demo_trades']}",
        f"- paired: {summary['paired']}",
        f"- paper-only (demo did not take): {summary['paper_only']}",
        f"- demo-only (paper did not take): {summary['demo_only']}",
        "",
        "## Entry slippage — demo fill vs idealized paper fill (bps, +adverse)",
        "",
        f"- mean: {summary['entry_slippage_bps_mean']:.2f}",
        f"- median: {summary['entry_slippage_bps_median']:.2f}",
        f"- worst: {summary['entry_slippage_bps_worst']:.2f}",
        "",
        f"## Exit slippage — closed pairs only ({summary['closed_pairs']})",
        "",
        f"- mean: {summary['exit_slippage_bps_mean']:.2f} bps",
        f"- median: {summary['exit_slippage_bps_median']:.2f} bps",
        "",
    ]
    if result["pairs"]:
        lines.append("## Per-pair")
        lines.append("")
        lines.append("| symbol | side | entry slip bps | exit slip bps | paper ret % | demo ret % |")
        lines.append("|---|---|---|---|---|---|")
        for pair in result["pairs"]:
            exit_bps = pair["exit_slippage_bps"]
            paper_ret = pair["paper_return_pct"]
            demo_ret = pair["demo_return_pct"]
            lines.append(
                f"| {pair['symbol']} | {pair['side']} | {pair['entry_slippage_bps']:.2f} | "
                f"{'-' if exit_bps is None else format(exit_bps, '.2f')} | "
                f"{'-' if paper_ret is None else format(paper_ret, '.3f')} | "
                f"{'-' if demo_ret is None else format(demo_ret, '.3f')} |"
            )
    else:
        lines.append("No paired trades yet — both ledgers need overlapping trades to reconcile.")
    return "\n".join(lines) + "\n"


def run_paper_demo_reconciliation(
    paper_root: str | Path,
    demo_root: str | Path,
    *,
    entry_tolerance_ms: int = DEFAULT_ENTRY_TOLERANCE_MS,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Read the paper and demo trade ledgers, reconcile them, write a markdown
    report, and return the result plus the report path."""
    paper_root = Path(paper_root).expanduser()
    demo_root = Path(demo_root).expanduser()
    result = reconcile_paper_demo(
        read_dataset(paper_root, "event_demo_trades"),
        read_dataset(demo_root, "event_demo_trades"),
        entry_tolerance_ms=entry_tolerance_ms,
    )
    report = format_reconciliation_report(result)
    report_dir = (
        Path(output_dir).expanduser() if output_dir else demo_root / "reports" / "paper_demo_reconciliation"
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "paper_demo_reconciliation.md"
    report_path.write_text(report, encoding="utf-8")
    return {"result": result, "report": report, "report_path": str(report_path)}
