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
                "entry_exec_time_ms": _int(row.get("entry_exec_time_ms")),
                "entry_price": entry_price,
                "entry_fee_usdt": _float(row.get("entry_fee_usdt")),
                "qty": qty,
                "status": str(row.get("status") or ""),
                "exit_price": _float(row.get("exit_price")),
                "exit_ts_ms": _int(row.get("exit_ts_ms")),
                "exit_exec_time_ms": _int(row.get("exit_exec_time_ms")),
                "exit_reason": str(row.get("exit_reason") or ""),
                "exit_fee_usdt": _float(row.get("exit_fee_usdt")),
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
    per-pair breakdown. Pairing matches the globally smallest entry-time gaps
    first within each (symbol, side) group, bounded by ``entry_tolerance_ms``,
    so trades close in time cannot steal each other's better match."""
    paper = _clean_trades(paper_trades)
    demo = _clean_trades(demo_trades)
    tolerance = max(int(entry_tolerance_ms), 0)

    paper_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for trade in paper:
        paper_by_key.setdefault((trade["symbol"], trade["side"]), []).append(trade)
    for bucket in paper_by_key.values():
        bucket.sort(key=lambda item: item["entry_ts_ms"])

    # Index paper trades by trade_id within each bucket so trade-id pairing
    # and gap pairing both use the SAME (key, bucket_idx) addressing scheme.
    paper_tid_in_bucket: dict[tuple[str, str], dict[str, int]] = {}
    for key, bucket in paper_by_key.items():
        per_bucket: dict[str, int] = {}
        for paper_idx, paper_trade in enumerate(bucket):
            tid = str(paper_trade.get("trade_id") or "")
            if tid:
                per_bucket.setdefault(tid, paper_idx)
        paper_tid_in_bucket[key] = per_bucket

    # Pass 1: pair by exact trade_id. The id is deterministic from
    # (scenario, symbol, signal_ts) so identical trades on paper and demo
    # ledgers share the same id and pair cleanly regardless of fill-time
    # divergence (e.g. paper restarted later than demo). This is the
    # primary pairing path; gap-based pairing below is the fallback for
    # rows without a trade_id (legacy ledgers).
    candidates: list[tuple[int, int, int]] = []  # (gap, demo_idx, paper_bucket_idx)
    tid_matched_demo: set[int] = set()
    tid_matched_paper: dict[tuple[str, str], set[int]] = {}
    for demo_idx, demo_trade in enumerate(demo):
        tid = str(demo_trade.get("trade_id") or "")
        if not tid:
            continue
        key = (demo_trade["symbol"], demo_trade["side"])
        paper_idx = paper_tid_in_bucket.get(key, {}).get(tid)
        if paper_idx is None:
            continue
        paper_trade = paper_by_key[key][paper_idx]
        gap = abs(demo_trade["entry_ts_ms"] - paper_trade["entry_ts_ms"])
        # Trade-id matches always pair; tolerance is irrelevant when the id is identical.
        candidates.append((gap, demo_idx, paper_idx))
        tid_matched_demo.add(demo_idx)
        tid_matched_paper.setdefault(key, set()).add(paper_idx)

    # Pass 2: gap-based pairing for trades without a matching trade_id (e.g.
    # legacy ledger rows). Build every candidate within tolerance, then assign
    # smallest-gap-first so the best global pairs win — a greedy per-demo
    # nearest-time pass would let an earlier demo trade consume a paper trade
    # that is a tighter match for a later one, biasing slippage.
    for demo_idx, demo_trade in enumerate(demo):
        if demo_idx in tid_matched_demo:
            continue
        key = (demo_trade["symbol"], demo_trade["side"])
        bucket = paper_by_key.get(key, [])
        already_paired = tid_matched_paper.get(key, set())
        for paper_idx, paper_trade in enumerate(bucket):
            if paper_idx in already_paired:
                continue
            gap = abs(demo_trade["entry_ts_ms"] - paper_trade["entry_ts_ms"])
            if gap <= tolerance:
                candidates.append((gap, demo_idx, paper_idx))
    # Smallest gap first; ties broken by demo then paper index for determinism.
    candidates.sort()

    # Collected gap-first; re-sorted to demo entry-time order before returning
    # so the per-pair report stays chronological.
    matched_pairs: list[tuple[int, dict[str, Any]]] = []
    used_demo: set[int] = set()
    used_paper: dict[tuple[str, str], set[int]] = {}
    for _gap, demo_idx, paper_idx in candidates:
        if demo_idx in used_demo:
            continue
        demo_trade = demo[demo_idx]
        key = (demo_trade["symbol"], demo_trade["side"])
        paper_used = used_paper.setdefault(key, set())
        if paper_idx in paper_used:
            continue
        used_demo.add(demo_idx)
        paper_used.add(paper_idx)
        paper_trade = paper_by_key[key][paper_idx]
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
        exit_gap_ms: int | None = None
        exit_reason_match: bool | None = None
        fee_gap_usdt: float | None = None
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
            # Exit-time skew uses venue execTime if both sides recorded it,
            # falling back to exit_ts_ms (cycle wall-clock) otherwise. Paper
            # never has execTime so this typically falls back to exit_ts_ms
            # vs venue execTime — still useful, just a slight cross-clock skew.
            paper_exit_t = paper_trade["exit_exec_time_ms"] or paper_trade["exit_ts_ms"]
            demo_exit_t = demo_trade["exit_exec_time_ms"] or demo_trade["exit_ts_ms"]
            if paper_exit_t > 0 and demo_exit_t > 0:
                exit_gap_ms = abs(demo_exit_t - paper_exit_t)
            # exit_reason match: paper records "tp"/"stop"/"failed_fade" etc;
            # divergence here means demo and paper closed for *different reasons*
            # (e.g. paper TP-exited while demo failed_fade-exited), which is a
            # signal-vs-execution divergence worth surfacing.
            paper_reason = paper_trade["exit_reason"]
            demo_reason = demo_trade["exit_reason"]
            if paper_reason or demo_reason:
                exit_reason_match = paper_reason == demo_reason
            # Realized-fee residual — paper has 0 fees by construction; this is
            # the per-trade fee tax the demo path paid that paper did not.
            fee_gap_usdt = demo_trade["entry_fee_usdt"] + demo_trade["exit_fee_usdt"] - (
                paper_trade["entry_fee_usdt"] + paper_trade["exit_fee_usdt"]
            )
        matched_pairs.append(
            (
                demo_trade["entry_ts_ms"],
                {
                    "symbol": demo_trade["symbol"],
                    "side": side,
                    "paper_trade_id": paper_trade["trade_id"],
                    "demo_trade_id": demo_trade["trade_id"],
                    "entry_gap_ms": abs(demo_trade["entry_ts_ms"] - paper_trade["entry_ts_ms"]),
                    "exit_gap_ms": exit_gap_ms,
                    "paper_entry_price": paper_trade["entry_price"],
                    "demo_entry_price": demo_trade["entry_price"],
                    "entry_slippage_bps": _entry_slippage_bps(
                        side=side, paper_entry=paper_trade["entry_price"], demo_entry=demo_trade["entry_price"]
                    ),
                    "exit_slippage_bps": exit_bps,
                    "paper_return_pct": paper_return,
                    "demo_return_pct": demo_return,
                    "paper_exit_reason": paper_trade["exit_reason"],
                    "demo_exit_reason": demo_trade["exit_reason"],
                    "exit_reason_match": exit_reason_match,
                    "fee_gap_usdt": fee_gap_usdt,
                },
            )
        )

    pairs: list[dict[str, Any]] = [pair for _ts, pair in sorted(matched_pairs, key=lambda item: item[0])]
    entry_bps = [pair["entry_slippage_bps"] for pair in pairs]
    exit_bps = [pair["exit_slippage_bps"] for pair in pairs if pair["exit_slippage_bps"] is not None]
    exit_gaps = [pair["exit_gap_ms"] for pair in pairs if pair["exit_gap_ms"] is not None]
    fee_gaps = [pair["fee_gap_usdt"] for pair in pairs if pair["fee_gap_usdt"] is not None]
    exit_reason_known = [pair for pair in pairs if pair["exit_reason_match"] is not None]
    exit_reason_divergent = [pair for pair in exit_reason_known if not pair["exit_reason_match"]]
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
        "exit_gap_ms_mean": mean(exit_gaps) if exit_gaps else 0,
        "exit_gap_ms_median": median(exit_gaps) if exit_gaps else 0,
        "exit_gap_ms_worst": max(exit_gaps) if exit_gaps else 0,
        "exit_reason_divergent": len(exit_reason_divergent),
        "exit_reason_compared": len(exit_reason_known),
        "fee_gap_usdt_total": sum(fee_gaps) if fee_gaps else 0.0,
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
        "## Exit-time skew (demo exit ts vs paper exit ts, |ms|)",
        "",
        f"- mean: {summary['exit_gap_ms_mean']:.0f}",
        f"- median: {summary['exit_gap_ms_median']:.0f}",
        f"- worst: {summary['exit_gap_ms_worst']:.0f}",
        "",
        "## Exit-reason divergence",
        "",
        f"- pairs with both reasons known: {summary['exit_reason_compared']}",
        f"- diverged (paper closed for a different reason than demo): {summary['exit_reason_divergent']}",
        "",
        "## Fee residual (demo - paper, USDT; +ve = demo paid more in fees)",
        "",
        f"- total across closed pairs: {summary['fee_gap_usdt_total']:.3f}",
        "",
    ]
    if result["pairs"]:
        lines.append("## Per-pair")
        lines.append("")
        lines.append(
            "| symbol | side | entry slip bps | exit slip bps | exit gap (s) | paper reason | demo reason | paper ret % | demo ret % | fee Δ USDT |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for pair in result["pairs"]:
            exit_bps = pair["exit_slippage_bps"]
            paper_ret = pair["paper_return_pct"]
            demo_ret = pair["demo_return_pct"]
            exit_gap = pair["exit_gap_ms"]
            fee_gap = pair["fee_gap_usdt"]
            lines.append(
                f"| {pair['symbol']} | {pair['side']} | {pair['entry_slippage_bps']:.2f} | "
                f"{'-' if exit_bps is None else format(exit_bps, '.2f')} | "
                f"{'-' if exit_gap is None else format(exit_gap / 1000.0, '.1f')} | "
                f"{pair['paper_exit_reason'] or '-'} | {pair['demo_exit_reason'] or '-'} | "
                f"{'-' if paper_ret is None else format(paper_ret, '.3f')} | "
                f"{'-' if demo_ret is None else format(demo_ret, '.3f')} | "
                f"{'-' if fee_gap is None else format(fee_gap, '.3f')} |"
            )
    else:
        lines.append("No paired trades yet — both ledgers need overlapping trades to reconcile.")
    return "\n".join(lines) + "\n"


def reconcile_demo_bybit(
    demo_trades: pl.DataFrame,
    closed_pnl_records: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reconcile the demo ledger against Bybit's account-level truth.

    Returns a summary + per-trade diff. Pairs ledger trades to Bybit
    closed_pnl rows by symbol + side + nearest createdTime within an hour
    of the ledger's exit_ts_ms, then computes:

    - exit_price gap (ledger vs Bybit avgExitPrice)
    - PnL gap (ledger's (entry-exit)*qty vs Bybit's closedPnl, which is
      net of fees)
    - orphan_in_bybit: Bybit closures with no matching ledger trade
    - orphan_in_ledger: ledger trades closed but absent from Bybit
      (should never happen)
    - open_position_mismatch: open ledger trades that Bybit does not
      report as open (or vice-versa)
    """
    demo = _clean_trades(demo_trades)
    cleaned_bybit: list[dict[str, Any]] = []
    for record in closed_pnl_records or []:
        symbol = str(record.get("symbol") or "")
        bybit_close_side = str(record.get("side") or "")  # Buy = closed a short; Sell = closed a long
        original_side = "short" if bybit_close_side == "Buy" else ("long" if bybit_close_side == "Sell" else "")
        avg_exit = _float(record.get("avgExitPrice"))
        avg_entry = _float(record.get("avgEntryPrice"))
        closed_size = _float(record.get("closedSize"))
        if not symbol or not original_side or avg_exit <= 0.0 or avg_entry <= 0.0 or closed_size <= 0.0:
            continue
        cleaned_bybit.append(
            {
                "symbol": symbol,
                "side": original_side,
                "avg_entry_price": avg_entry,
                "avg_exit_price": avg_exit,
                "closed_size": closed_size,
                "closed_pnl_usdt": _float(record.get("closedPnl")),
                "exec_fee_usdt": _float(record.get("execFee")),
                "created_ts_ms": _int(record.get("createdTime") or record.get("updatedTime")),
            }
        )

    closed_demo_trades = [t for t in demo if t["status"] == "closed" and t["exit_price"] > 0.0]
    open_demo_trades = [t for t in demo if t["status"] == "open"]

    # Pair Bybit closed_pnl to ledger trades by (symbol, side); within group,
    # smallest createdTime↔exit_ts_ms gap first (same global-best policy as
    # paper↔demo, so a multi-leg series doesn't steal the wrong partner).
    candidates: list[tuple[int, int, int]] = []
    for bidx, bybit in enumerate(cleaned_bybit):
        for didx, dt in enumerate(closed_demo_trades):
            if dt["symbol"] != bybit["symbol"] or dt["side"] != bybit["side"]:
                continue
            dexit_t = dt["exit_exec_time_ms"] or dt["exit_ts_ms"]
            if dexit_t <= 0:
                continue
            gap = abs(dexit_t - bybit["created_ts_ms"])
            # 1h tolerance — Bybit closed_pnl can lag the ledger by a few minutes
            # on a slow reconcile cycle; an hour is generous enough to pair, tight
            # enough to spot a stale orphan.
            if gap <= 3_600_000:
                candidates.append((gap, didx, bidx))
    candidates.sort()
    used_demo: set[int] = set()
    used_bybit: set[int] = set()
    pairs: list[dict[str, Any]] = []
    for _gap, didx, bidx in candidates:
        if didx in used_demo or bidx in used_bybit:
            continue
        used_demo.add(didx)
        used_bybit.add(bidx)
        dt = closed_demo_trades[didx]
        bb = cleaned_bybit[bidx]
        # Ledger PnL: (entry - exit) * qty for shorts; opposite for longs. This is
        # GROSS of fees; Bybit's closedPnl is NET of fees, so a non-zero residual
        # of fee_gap_usdt is expected and is exactly the fee cost.
        if dt["side"] == "short":
            ledger_pnl = (dt["entry_price"] - dt["exit_price"]) * dt["qty"]
        else:
            ledger_pnl = (dt["exit_price"] - dt["entry_price"]) * dt["qty"]
        pairs.append(
            {
                "symbol": dt["symbol"],
                "side": dt["side"],
                "demo_trade_id": dt["trade_id"],
                "demo_qty": dt["qty"],
                "bybit_closed_size": bb["closed_size"],
                "qty_gap": dt["qty"] - bb["closed_size"],
                "demo_entry_price": dt["entry_price"],
                "bybit_avg_entry_price": bb["avg_entry_price"],
                "demo_exit_price": dt["exit_price"],
                "bybit_avg_exit_price": bb["avg_exit_price"],
                "exit_price_gap_bps": (
                    abs(dt["exit_price"] - bb["avg_exit_price"]) / bb["avg_exit_price"] * 10_000.0
                    if bb["avg_exit_price"] > 0.0
                    else 0.0
                ),
                "ledger_pnl_usdt": ledger_pnl,
                "bybit_closed_pnl_usdt": bb["closed_pnl_usdt"],
                # Bybit pnl is gross_pnl - fees; this gap = inferred fees + slippage error
                "pnl_gap_usdt": ledger_pnl - bb["closed_pnl_usdt"],
                "demo_exit_ts_ms": dt["exit_exec_time_ms"] or dt["exit_ts_ms"],
                "bybit_created_ts_ms": bb["created_ts_ms"],
                "exit_ts_gap_ms": abs((dt["exit_exec_time_ms"] or dt["exit_ts_ms"]) - bb["created_ts_ms"]),
            }
        )

    orphan_in_bybit = [
        {
            "symbol": bb["symbol"],
            "side": bb["side"],
            "closed_pnl_usdt": bb["closed_pnl_usdt"],
            "avg_entry_price": bb["avg_entry_price"],
            "avg_exit_price": bb["avg_exit_price"],
            "closed_size": bb["closed_size"],
            "created_ts_ms": bb["created_ts_ms"],
        }
        for bidx, bb in enumerate(cleaned_bybit) if bidx not in used_bybit
    ]
    orphan_in_ledger = [
        {
            "symbol": dt["symbol"],
            "side": dt["side"],
            "demo_trade_id": dt["trade_id"],
            "exit_price": dt["exit_price"],
            "exit_ts_ms": dt["exit_ts_ms"],
        }
        for didx, dt in enumerate(closed_demo_trades) if didx not in used_demo
    ]

    # Open-position cross-check: every ledger-open trade should have a non-zero
    # Bybit position; every non-zero Bybit position should map to a ledger trade.
    ledger_open_keys = {(t["symbol"], t["side"]) for t in open_demo_trades}
    bybit_open_keys: set[tuple[str, str]] = set()
    bybit_open_detail: dict[tuple[str, str], dict[str, Any]] = {}
    for pos in open_positions or []:
        size = _float(pos.get("size"))
        if size <= 0.0:
            continue
        symbol = str(pos.get("symbol") or "")
        bybit_side = str(pos.get("side") or "")
        original_side = "short" if bybit_side == "Sell" else "long" if bybit_side == "Buy" else ""
        if not symbol or not original_side:
            continue
        key = (symbol, original_side)
        bybit_open_keys.add(key)
        bybit_open_detail[key] = {
            "size": size,
            "avg_price": _float(pos.get("avgPrice")),
            "unrealised_pnl_usdt": _float(pos.get("unrealisedPnl")),
        }
    open_only_in_ledger = sorted(ledger_open_keys - bybit_open_keys)
    open_only_in_bybit = sorted(bybit_open_keys - ledger_open_keys)
    open_in_both = sorted(ledger_open_keys & bybit_open_keys)
    open_position_diffs: list[dict[str, Any]] = []
    for sym, side in open_in_both:
        ledger_trade = next(t for t in open_demo_trades if t["symbol"] == sym and t["side"] == side)
        bb = bybit_open_detail[(sym, side)]
        open_position_diffs.append(
            {
                "symbol": sym,
                "side": side,
                "demo_qty": ledger_trade["qty"],
                "bybit_size": bb["size"],
                "qty_gap": ledger_trade["qty"] - bb["size"],
                "demo_entry_price": ledger_trade["entry_price"],
                "bybit_avg_price": bb["avg_price"],
                "bybit_unrealised_pnl_usdt": bb["unrealised_pnl_usdt"],
            }
        )

    exit_price_bps_all = [p["exit_price_gap_bps"] for p in pairs]
    pnl_gaps_all = [p["pnl_gap_usdt"] for p in pairs]
    exit_ts_gaps_all = [p["exit_ts_gap_ms"] for p in pairs]
    summary = {
        "ledger_closed_trades": len(closed_demo_trades),
        "ledger_open_trades": len(open_demo_trades),
        "bybit_closed_records": len(cleaned_bybit),
        "bybit_open_positions": len(bybit_open_keys),
        "paired_closed": len(pairs),
        "orphan_in_bybit": len(orphan_in_bybit),
        "orphan_in_ledger": len(orphan_in_ledger),
        "open_only_in_ledger": len(open_only_in_ledger),
        "open_only_in_bybit": len(open_only_in_bybit),
        "open_in_both": len(open_in_both),
        "exit_price_gap_bps_mean": mean(exit_price_bps_all) if exit_price_bps_all else 0.0,
        "exit_price_gap_bps_worst": max(exit_price_bps_all) if exit_price_bps_all else 0.0,
        "pnl_gap_usdt_total": sum(pnl_gaps_all) if pnl_gaps_all else 0.0,
        "exit_ts_gap_ms_mean": mean(exit_ts_gaps_all) if exit_ts_gaps_all else 0,
        "exit_ts_gap_ms_worst": max(exit_ts_gaps_all) if exit_ts_gaps_all else 0,
    }
    return {
        "summary": summary,
        "pairs": pairs,
        "orphan_in_bybit": orphan_in_bybit,
        "orphan_in_ledger": orphan_in_ledger,
        "open_only_in_ledger": [{"symbol": s, "side": side} for s, side in open_only_in_ledger],
        "open_only_in_bybit": [{"symbol": s, "side": side} for s, side in open_only_in_bybit],
        "open_position_diffs": open_position_diffs,
    }


def format_demo_bybit_report(result: dict[str, Any]) -> str:
    """Render a demo↔Bybit reconciliation as markdown."""
    summary = result["summary"]
    lines = [
        "# Demo Ledger vs Bybit Account Reconciliation",
        "",
        f"- ledger closed trades: {summary['ledger_closed_trades']}",
        f"- ledger open trades: {summary['ledger_open_trades']}",
        f"- Bybit closed records: {summary['bybit_closed_records']}",
        f"- Bybit open positions: {summary['bybit_open_positions']}",
        f"- paired closed (ledger ↔ Bybit closed_pnl): {summary['paired_closed']}",
        "",
        "## Anomalies",
        "",
        f"- closures in Bybit with no ledger trade: **{summary['orphan_in_bybit']}**",
        f"- closures in ledger with no Bybit record: **{summary['orphan_in_ledger']}**",
        f"- open in ledger only (ghost position in ledger): **{summary['open_only_in_ledger']}**",
        f"- open on Bybit only (untracked position on exchange): **{summary['open_only_in_bybit']}**",
        f"- open on both sides: {summary['open_in_both']}",
        "",
        "## Exit-price gap (ledger vs Bybit avgExitPrice)",
        "",
        f"- mean: {summary['exit_price_gap_bps_mean']:.2f} bps",
        f"- worst: {summary['exit_price_gap_bps_worst']:.2f} bps",
        "",
        "## PnL gap (ledger gross_pnl − Bybit closedPnl, USDT)",
        "",
        "Bybit's closedPnl is net of fees; the ledger PnL is gross. This residual",
        "is the fee cost plus any fill-price reconciliation error.",
        "",
        f"- total across paired: {summary['pnl_gap_usdt_total']:.3f}",
        "",
        "## Exit timestamp gap (|ledger - Bybit createdTime|, seconds)",
        "",
        f"- mean: {summary['exit_ts_gap_ms_mean'] / 1000.0:.1f}",
        f"- worst: {summary['exit_ts_gap_ms_worst'] / 1000.0:.1f}",
        "",
    ]
    if result["pairs"]:
        lines.append("## Per-pair (closed)")
        lines.append("")
        lines.append("| symbol | side | qty Δ | exit price Δ bps | exit ts Δ s | ledger pnl | bybit pnl | pnl Δ |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for p in result["pairs"]:
            lines.append(
                f"| {p['symbol']} | {p['side']} | {p['qty_gap']:.6g} | "
                f"{p['exit_price_gap_bps']:.2f} | {p['exit_ts_gap_ms'] / 1000.0:.1f} | "
                f"{p['ledger_pnl_usdt']:.3f} | {p['bybit_closed_pnl_usdt']:.3f} | "
                f"{p['pnl_gap_usdt']:.3f} |"
            )
    if result["orphan_in_bybit"]:
        lines.append("")
        lines.append("## Orphans on Bybit (closed there, not in ledger)")
        lines.append("")
        lines.append("| symbol | side | closedPnl | avgEntry | avgExit | closedSize | createdTime |")
        lines.append("|---|---|---|---|---|---|---|")
        for o in result["orphan_in_bybit"]:
            lines.append(
                f"| {o['symbol']} | {o['side']} | {o['closed_pnl_usdt']:.3f} | "
                f"{o['avg_entry_price']} | {o['avg_exit_price']} | {o['closed_size']} | "
                f"{o['created_ts_ms']} |"
            )
    if result["orphan_in_ledger"]:
        lines.append("")
        lines.append("## Orphans in ledger (closed there, not on Bybit — INVESTIGATE)")
        lines.append("")
        for o in result["orphan_in_ledger"]:
            lines.append(f"- {o['symbol']} {o['side']} trade_id={o['demo_trade_id']} exit_price={o['exit_price']}")
    if result["open_only_in_ledger"] or result["open_only_in_bybit"]:
        lines.append("")
        lines.append("## Open-position mismatches")
        lines.append("")
        for o in result["open_only_in_ledger"]:
            lines.append(f"- ledger-only open: {o['symbol']} {o['side']}")
        for o in result["open_only_in_bybit"]:
            lines.append(f"- bybit-only open (untracked!): {o['symbol']} {o['side']}")
    if result["open_position_diffs"]:
        lines.append("")
        lines.append("## Open-position fill-fidelity (both sides hold position)")
        lines.append("")
        lines.append("| symbol | side | qty Δ | demo entry | bybit avg | bybit unrealised |")
        lines.append("|---|---|---|---|---|---|")
        for d in result["open_position_diffs"]:
            lines.append(
                f"| {d['symbol']} | {d['side']} | {d['qty_gap']:.6g} | "
                f"{d['demo_entry_price']} | {d['bybit_avg_price']} | "
                f"{d['bybit_unrealised_pnl_usdt']:.3f} |"
            )
    return "\n".join(lines) + "\n"


def run_demo_bybit_reconciliation(
    demo_root: str | Path,
    *,
    trading_client: Any,
    lookback_hours: int = 168,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Pull Bybit closed_pnl + open positions live, reconcile against the demo
    ledger, and write a markdown report. The trading_client must implement
    `get_closed_pnl(symbol=..., start_time_ms=..., end_time_ms=..., limit=...)`
    and `get_positions(settle_coin=...)` (see liquidity_migration.bybit).
    """
    import time

    demo_root_p = Path(demo_root).expanduser()
    trades = read_dataset(demo_root_p, "event_demo_trades")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(lookback_hours) * 3_600_000

    # Symbols to query: union of all symbols ever seen in the ledger PLUS
    # the symbols currently open on Bybit. Without union the latter set
    # would miss orphan closures on symbols never opened by this ledger.
    ledger_symbols = (
        {str(s) for s in trades["symbol"].to_list()} if not trades.is_empty() else set()
    )
    open_positions = trading_client.get_positions(settle_coin="USDT")
    bybit_symbols = {str(p.get("symbol") or "") for p in open_positions if float(p.get("size") or 0) > 0}
    closed_records: list[dict[str, Any]] = []
    for sym in sorted(ledger_symbols | bybit_symbols):
        if not sym:
            continue
        try:
            rows = trading_client.get_closed_pnl(symbol=sym, start_time_ms=start_ms, end_time_ms=end_ms, limit=200)
        except Exception:  # noqa: BLE001 - one-symbol failure should not kill the whole reconciliation
            continue
        for r in rows or []:
            r["_symbol_query"] = sym
            closed_records.append(r)

    result = reconcile_demo_bybit(trades, closed_records, open_positions)
    report = format_demo_bybit_report(result)
    report_dir = (
        Path(output_dir).expanduser() if output_dir else demo_root_p / "reports" / "demo_bybit_reconciliation"
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "demo_bybit_reconciliation.md"
    report_path.write_text(report, encoding="utf-8")
    return {"result": result, "report": report, "report_path": str(report_path)}


def run_paper_demo_reconciliation(
    paper_root: str | Path,
    demo_root: str | Path,
    *,
    entry_tolerance_ms: int = DEFAULT_ENTRY_TOLERANCE_MS,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Read the paper and demo trade ledgers, reconcile them, write a markdown
    report, and return the result plus the report path."""
    return _run_reconciliation(
        paper_root=paper_root,
        demo_root=demo_root,
        paper_dataset="event_demo_trades",
        demo_dataset="event_demo_trades",
        report_subdir="paper_demo_reconciliation",
        report_filename="paper_demo_reconciliation.md",
        entry_tolerance_ms=entry_tolerance_ms,
        output_dir=output_dir,
    )


def run_long_paper_demo_reconciliation(
    paper_root: str | Path,
    demo_root: str | Path,
    *,
    entry_tolerance_ms: int = DEFAULT_ENTRY_TOLERANCE_MS,
    output_dir: str | Path | None = None,
    min_pairs_warning: int = 30,
) -> dict[str, Any]:
    """B.4 — same pairing as the short reconciler but reads the long sleeve's
    own ledger datasets (``long_native_paper_trades`` vs
    ``long_native_demo_trades``). Emits an additional ``sample_warning`` flag
    in the summary when fewer than ``min_pairs_warning`` pairs were matched —
    surfacing the case where slippage statistics are not yet trustworthy.
    """
    payload = _run_reconciliation(
        paper_root=paper_root,
        demo_root=demo_root,
        paper_dataset="long_native_paper_trades",
        demo_dataset="long_native_demo_trades",
        report_subdir="long_paper_demo_reconciliation",
        report_filename="long_paper_demo_reconciliation.md",
        entry_tolerance_ms=entry_tolerance_ms,
        output_dir=output_dir,
    )
    summary = payload["result"]["summary"]
    summary["min_pairs_warning_threshold"] = int(min_pairs_warning)
    summary["sample_warning"] = bool(summary["paired"] < int(min_pairs_warning))
    return payload


def _run_reconciliation(
    *,
    paper_root: str | Path,
    demo_root: str | Path,
    paper_dataset: str,
    demo_dataset: str,
    report_subdir: str,
    report_filename: str,
    entry_tolerance_ms: int,
    output_dir: str | Path | None,
) -> dict[str, Any]:
    paper_root_p = Path(paper_root).expanduser()
    demo_root_p = Path(demo_root).expanduser()
    result = reconcile_paper_demo(
        read_dataset(paper_root_p, paper_dataset),
        read_dataset(demo_root_p, demo_dataset),
        entry_tolerance_ms=entry_tolerance_ms,
    )
    report = format_reconciliation_report(result)
    report_dir = (
        Path(output_dir).expanduser() if output_dir else demo_root_p / "reports" / report_subdir
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / report_filename
    report_path.write_text(report, encoding="utf-8")
    return {"result": result, "report": report, "report_path": str(report_path)}
