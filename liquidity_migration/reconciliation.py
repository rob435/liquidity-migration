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
                "signal_ts_ms": _int(row.get("signal_ts_ms")),
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


def _clean_backtest_trades(trades: pl.DataFrame) -> list[dict[str, Any]]:
    """Adapt the volume-events backtest trade ledger (`volume_event_best_trades.csv`)
    to the same dict shape that `_clean_trades` produces for paper/demo. The
    backtest carries `entry_signal_ts_ms` (the signal-bar time) rather than the
    paper ledger's `signal_ts_ms`; entry/exit times in the backtest are
    deterministic from the strategy clock, not real wall-clock, so they are the
    primary pairing keys."""
    if trades.is_empty():
        return []
    cleaned: list[dict[str, Any]] = []
    for row in trades.to_dicts():
        symbol = str(row.get("symbol") or "")
        side = _normalized_side(row.get("side"))
        entry_price = _float(row.get("entry_price"))
        if not symbol or side not in {"long", "short"} or entry_price <= 0.0:
            continue
        exit_price = _float(row.get("exit_price"))
        exit_ts_ms = _int(row.get("exit_ts_ms"))
        cleaned.append(
            {
                "trade_id": str(row.get("trade_id") or ""),
                "symbol": symbol,
                "side": side,
                "signal_ts_ms": _int(row.get("entry_signal_ts_ms") or row.get("signal_ts_ms")),
                "entry_ts_ms": _int(row.get("entry_ts_ms")),
                # No exec_time / fee on the backtest path — it's a strategy-clock
                # model with idealized fills + a per-side cost penalty baked into
                # cost_return. Fee residual will be backtest gross vs paper gross,
                # which by construction should match exactly.
                "entry_exec_time_ms": 0,
                "entry_price": entry_price,
                "entry_fee_usdt": 0.0,
                "qty": 0.0,  # backtest doesn't track qty; uses notional_weight
                "notional_weight": _float(row.get("notional_weight")),
                "status": "closed" if exit_price > 0.0 and exit_ts_ms > 0 else "open",
                "exit_price": exit_price,
                "exit_ts_ms": exit_ts_ms,
                "exit_exec_time_ms": 0,
                "exit_reason": str(row.get("exit_reason") or ""),
                "exit_fee_usdt": 0.0,
                "gross_trade_return": _float(row.get("gross_trade_return")),
                "net_return": _float(row.get("net_return")),
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
    signal_tolerance_ms: int = 60_000,
) -> dict[str, Any]:
    """Pair paper and demo trades by trade_id, then signal_ts, then entry_ts.
    Measure fill-price + exit-time + exit-reason slippage between them.

    Pairing precedence:

    1. **Exact `trade_id`** — the strongest match. Trade-ids are deterministic
       from (strategy, symbol, signal_ts), so identical IDs pair cleanly even
       when fill times differ by hours.
    2. **`signal_ts_ms` gap** (within `signal_tolerance_ms`, default 60s) —
       the second strongest. Signal_ts is the strategy decision time and is
       set from the same bar boundary on both sides; tight tolerance.
    3. **`entry_ts_ms` gap** (within `entry_tolerance_ms`, default 10 min) —
       the legacy fallback for rows missing both trade_id and signal_ts.

    Within each pass, the globally smallest gap is paired first so trades
    close in time cannot steal each other's better match.
    """
    paper = _clean_trades(paper_trades)
    demo = _clean_trades(demo_trades)
    tolerance = max(int(entry_tolerance_ms), 0)
    signal_tolerance = max(int(signal_tolerance_ms), 0)

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

    # Pass 1.5: pair by signal_ts gap within `signal_tolerance_ms`. Recovery-
    # backfilled trades can have entry_ts_ms that differs from paper's
    # entry_ts_ms by HOURS (the recovered demo trade was backfilled with the
    # original signal-bar time as its entry_ts, while paper recorded its own
    # later first-cycle entry_ts) — so signal_ts is the only safe non-id key
    # in that case. We do this BEFORE the legacy entry_ts pass so a true
    # signal_ts match wins over a coincidental entry_ts proximity.
    signal_matched_demo: set[int] = set(tid_matched_demo)
    signal_matched_paper: dict[tuple[str, str], set[int]] = {
        k: set(v) for k, v in tid_matched_paper.items()
    }
    signal_candidates: list[tuple[int, int, int]] = []
    for demo_idx, demo_trade in enumerate(demo):
        if demo_idx in tid_matched_demo:
            continue
        demo_signal = demo_trade.get("signal_ts_ms", 0)
        if not demo_signal:
            continue
        key = (demo_trade["symbol"], demo_trade["side"])
        bucket = paper_by_key.get(key, [])
        already_paired = tid_matched_paper.get(key, set())
        for paper_idx, paper_trade in enumerate(bucket):
            if paper_idx in already_paired:
                continue
            paper_signal = paper_trade.get("signal_ts_ms", 0)
            if not paper_signal:
                continue
            sig_gap = abs(demo_signal - paper_signal)
            if sig_gap <= signal_tolerance:
                # Sort key reuses the standard `gap` slot (smaller = better)
                # so this list folds into the main `candidates` ordering.
                signal_candidates.append((sig_gap, demo_idx, paper_idx))
    # Assign signal-ts matches smallest-first so the best signal-aligned pair
    # wins inside each (symbol, side) bucket.
    signal_candidates.sort()
    for sig_gap, demo_idx, paper_idx in signal_candidates:
        if demo_idx in signal_matched_demo:
            continue
        key = (demo[demo_idx]["symbol"], demo[demo_idx]["side"])
        paper_used = signal_matched_paper.setdefault(key, set())
        if paper_idx in paper_used:
            continue
        # Use entry_ts gap as the secondary scoring (kept consistent with the
        # rest of `candidates`) so the chronological re-sort below still works.
        demo_trade = demo[demo_idx]
        paper_trade = paper_by_key[key][paper_idx]
        entry_gap = abs(demo_trade["entry_ts_ms"] - paper_trade["entry_ts_ms"])
        candidates.append((entry_gap, demo_idx, paper_idx))
        signal_matched_demo.add(demo_idx)
        paper_used.add(paper_idx)

    # Pass 2: gap-based pairing for trades without a matching trade_id or
    # signal_ts (e.g. legacy ledger rows). Build every candidate within
    # entry_tolerance_ms, then assign smallest-gap-first so the best global
    # pairs win — a greedy per-demo nearest-time pass would let an earlier
    # demo trade consume a paper trade that is a tighter match for a later
    # one, biasing slippage.
    for demo_idx, demo_trade in enumerate(demo):
        if demo_idx in signal_matched_demo:
            continue
        key = (demo_trade["symbol"], demo_trade["side"])
        bucket = paper_by_key.get(key, [])
        already_paired = signal_matched_paper.get(key, set())
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


def reconcile_backtest_paper(
    backtest_trades: pl.DataFrame,
    paper_trades: pl.DataFrame,
    *,
    signal_tolerance_ms: int = 60_000,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> dict[str, Any]:
    """Reconcile the offline volume-events backtest against the live paper
    (dry-run) ledger.

    Paper IS the live execution of the same strategy code that the backtest
    runs offline; they MUST agree on which signals fire and at what price.
    A mismatch surfaces one of:

    - **Code drift**: the offline backtest and the live runner have diverged
      (different filter, different threshold, different event detection).
    - **Data drift**: the offline backtest used historical klines that
      differ from what the live runner saw at the same timestamp (e.g.
      revised funding, late OI bar).
    - **Universe drift**: the offline backtest's PIT manifest disagrees
      with the live universe at that ts.

    Pairs by (symbol, side, signal_ts) within ``signal_tolerance_ms``. If
    `window_start_ms`/`window_end_ms` are supplied, only trades whose
    signal_ts falls in that window are considered — needed when the
    backtest covers a longer period than the forward paper run.
    """
    backtest = _clean_backtest_trades(backtest_trades)
    paper = _clean_trades(paper_trades)
    if window_start_ms is not None:
        backtest = [t for t in backtest if t["signal_ts_ms"] >= window_start_ms]
        paper = [t for t in paper if t["signal_ts_ms"] >= window_start_ms]
    if window_end_ms is not None:
        backtest = [t for t in backtest if t["signal_ts_ms"] <= window_end_ms]
        paper = [t for t in paper if t["signal_ts_ms"] <= window_end_ms]

    tolerance = max(int(signal_tolerance_ms), 0)
    paper_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for trade in paper:
        paper_by_key.setdefault((trade["symbol"], trade["side"]), []).append(trade)
    for bucket in paper_by_key.values():
        bucket.sort(key=lambda t: t["signal_ts_ms"])

    candidates: list[tuple[int, int, int]] = []
    for bidx, bt in enumerate(backtest):
        key = (bt["symbol"], bt["side"])
        bucket = paper_by_key.get(key, [])
        for pidx, paper_trade in enumerate(bucket):
            gap = abs(bt["signal_ts_ms"] - paper_trade["signal_ts_ms"])
            if gap <= tolerance:
                candidates.append((gap, bidx, pidx))
    candidates.sort()
    used_backtest: set[int] = set()
    used_paper: dict[tuple[str, str], set[int]] = {}
    pairs: list[dict[str, Any]] = []
    for _gap, bidx, pidx in candidates:
        if bidx in used_backtest:
            continue
        bt = backtest[bidx]
        key = (bt["symbol"], bt["side"])
        paper_used = used_paper.setdefault(key, set())
        if pidx in paper_used:
            continue
        used_backtest.add(bidx)
        paper_used.add(pidx)
        paper_trade = paper_by_key[key][pidx]
        side = bt["side"]
        # Both prices should match to 1 bp in a perfectly-synced setup; larger
        # gaps mean the live runner picked a different bar (clock-skew) or
        # the offline backtest used revised klines.
        entry_price_gap_bps = (
            abs(bt["entry_price"] - paper_trade["entry_price"]) / paper_trade["entry_price"] * 10_000.0
            if paper_trade["entry_price"] > 0.0
            else 0.0
        )
        both_closed = bt["status"] == "closed" and paper_trade["status"] == "closed"
        exit_price_gap_bps: float | None = None
        bt_return: float | None = None
        paper_return: float | None = None
        return_gap_pct: float | None = None
        exit_reason_match: bool | None = None
        if both_closed:
            exit_price_gap_bps = (
                abs(bt["exit_price"] - paper_trade["exit_price"]) / paper_trade["exit_price"] * 10_000.0
                if paper_trade["exit_price"] > 0.0
                else 0.0
            )
            bt_return = bt["gross_trade_return"] * 100.0
            paper_return = _realized_return_pct(
                side=side,
                entry_price=paper_trade["entry_price"],
                exit_price=paper_trade["exit_price"],
            )
            return_gap_pct = bt_return - paper_return
            bt_reason = bt["exit_reason"]
            paper_reason = paper_trade["exit_reason"]
            if bt_reason or paper_reason:
                exit_reason_match = bt_reason == paper_reason
        pairs.append(
            {
                "symbol": bt["symbol"],
                "side": side,
                "backtest_trade_id": bt["trade_id"],
                "paper_trade_id": paper_trade["trade_id"],
                "signal_gap_ms": abs(bt["signal_ts_ms"] - paper_trade["signal_ts_ms"]),
                "backtest_entry_price": bt["entry_price"],
                "paper_entry_price": paper_trade["entry_price"],
                "entry_price_gap_bps": entry_price_gap_bps,
                "backtest_exit_price": bt["exit_price"],
                "paper_exit_price": paper_trade["exit_price"],
                "exit_price_gap_bps": exit_price_gap_bps,
                "backtest_exit_reason": bt["exit_reason"],
                "paper_exit_reason": paper_trade["exit_reason"],
                "exit_reason_match": exit_reason_match,
                "backtest_return_pct": bt_return,
                "paper_return_pct": paper_return,
                "return_gap_pct": return_gap_pct,
            }
        )

    pairs.sort(key=lambda p: p["paper_trade_id"] or p["backtest_trade_id"])
    entry_bps = [p["entry_price_gap_bps"] for p in pairs]
    exit_bps = [p["exit_price_gap_bps"] for p in pairs if p["exit_price_gap_bps"] is not None]
    return_gaps = [p["return_gap_pct"] for p in pairs if p["return_gap_pct"] is not None]
    exit_reason_known = [p for p in pairs if p["exit_reason_match"] is not None]
    exit_reason_divergent = [p for p in exit_reason_known if not p["exit_reason_match"]]
    summary = {
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
        "signal_tolerance_ms": tolerance,
        "backtest_trades": len(backtest),
        "paper_trades": len(paper),
        "paired": len(pairs),
        "backtest_only": len(backtest) - len(pairs),
        "paper_only": len(paper) - len(pairs),
        "entry_price_gap_bps_mean": mean(entry_bps) if entry_bps else 0.0,
        "entry_price_gap_bps_worst": max(entry_bps) if entry_bps else 0.0,
        "exit_price_gap_bps_mean": mean(exit_bps) if exit_bps else 0.0,
        "exit_price_gap_bps_worst": max(exit_bps) if exit_bps else 0.0,
        "return_gap_pct_mean": mean(return_gaps) if return_gaps else 0.0,
        "return_gap_pct_worst": max((abs(r) for r in return_gaps), default=0.0),
        "exit_reason_compared": len(exit_reason_known),
        "exit_reason_divergent": len(exit_reason_divergent),
    }
    backtest_only_list = [
        {"trade_id": bt["trade_id"], "symbol": bt["symbol"], "side": bt["side"], "signal_ts_ms": bt["signal_ts_ms"]}
        for bidx, bt in enumerate(backtest) if bidx not in used_backtest
    ]
    paper_only_list: list[dict[str, Any]] = []
    for key, bucket in paper_by_key.items():
        used = used_paper.get(key, set())
        for pidx, pt in enumerate(bucket):
            if pidx in used:
                continue
            paper_only_list.append(
                {"trade_id": pt["trade_id"], "symbol": pt["symbol"], "side": pt["side"], "signal_ts_ms": pt["signal_ts_ms"]}
            )
    return {
        "summary": summary,
        "pairs": pairs,
        "backtest_only": backtest_only_list,
        "paper_only": paper_only_list,
    }


def format_backtest_paper_report(result: dict[str, Any]) -> str:
    """Render a backtest↔paper reconciliation as markdown."""
    summary = result["summary"]
    lines = [
        "# Backtest vs Paper Reconciliation",
        "",
        "Pairs the offline volume-events backtest against the live paper",
        "(dry-run) ledger. A clean match here proves the live strategy code",
        "matches the offline backtest model on the same data.",
        "",
        f"- window: {summary['window_start_ms']} → {summary['window_end_ms']}",
        f"- signal-pair tolerance: {summary['signal_tolerance_ms']} ms",
        f"- backtest trades: {summary['backtest_trades']}",
        f"- paper trades: {summary['paper_trades']}",
        f"- paired: {summary['paired']}",
        f"- backtest-only (paper missed signal — LIVE CODE DRIFT): {summary['backtest_only']}",
        f"- paper-only (backtest missed signal — OFFLINE CODE/DATA DRIFT): {summary['paper_only']}",
        "",
        "## Entry-price agreement (bps; perfect sync = 0)",
        "",
        f"- mean: {summary['entry_price_gap_bps_mean']:.3f}",
        f"- worst: {summary['entry_price_gap_bps_worst']:.3f}",
        "",
        "## Exit-price agreement (bps; perfect sync = 0)",
        "",
        f"- mean: {summary['exit_price_gap_bps_mean']:.3f}",
        f"- worst: {summary['exit_price_gap_bps_worst']:.3f}",
        "",
        "## Realized-return agreement (percentage points)",
        "",
        f"- mean Δ: {summary['return_gap_pct_mean']:.4f}",
        f"- worst |Δ|: {summary['return_gap_pct_worst']:.4f}",
        "",
        "## Exit-reason divergence",
        "",
        f"- pairs compared: {summary['exit_reason_compared']}",
        f"- diverged: {summary['exit_reason_divergent']}",
        "",
    ]
    if result["pairs"]:
        lines.append("## Per-pair")
        lines.append("")
        lines.append(
            "| symbol | side | signal Δ ms | entry Δ bps | exit Δ bps | bt reason | paper reason | bt ret % | paper ret % | ret Δ %% |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for p in result["pairs"]:
            exit_bps = p["exit_price_gap_bps"]
            bt_ret = p["backtest_return_pct"]
            paper_ret = p["paper_return_pct"]
            ret_gap = p["return_gap_pct"]
            lines.append(
                f"| {p['symbol']} | {p['side']} | {p['signal_gap_ms']} | "
                f"{p['entry_price_gap_bps']:.3f} | "
                f"{'-' if exit_bps is None else format(exit_bps, '.3f')} | "
                f"{p['backtest_exit_reason'] or '-'} | {p['paper_exit_reason'] or '-'} | "
                f"{'-' if bt_ret is None else format(bt_ret, '.3f')} | "
                f"{'-' if paper_ret is None else format(paper_ret, '.3f')} | "
                f"{'-' if ret_gap is None else format(ret_gap, '.4f')} |"
            )
    if result["backtest_only"]:
        lines.append("")
        lines.append("## Backtest-only signals (paper missed — investigate live-code drift)")
        lines.append("")
        for t in result["backtest_only"]:
            lines.append(f"- {t['symbol']} {t['side']} signal_ts_ms={t['signal_ts_ms']} trade_id={t['trade_id']}")
    if result["paper_only"]:
        lines.append("")
        lines.append("## Paper-only signals (backtest missed — investigate offline code/data drift)")
        lines.append("")
        for t in result["paper_only"]:
            lines.append(f"- {t['symbol']} {t['side']} signal_ts_ms={t['signal_ts_ms']} trade_id={t['trade_id']}")
    return "\n".join(lines) + "\n"


def run_backtest_paper_reconciliation(
    backtest_trades_csv: str | Path,
    paper_root: str | Path,
    *,
    signal_tolerance_ms: int = 60_000,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
    paper_dataset: str = "event_demo_trades",
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Read the volume-events backtest trade CSV and the paper ledger, then
    reconcile. If `window_start_ms` is None, defaults to the paper ledger's
    earliest signal_ts (so the comparison is scoped to the forward window).
    """
    backtest_path = Path(backtest_trades_csv).expanduser()
    paper_root_p = Path(paper_root).expanduser()
    backtest_df = pl.read_csv(backtest_path) if backtest_path.exists() else pl.DataFrame()
    paper_df = read_dataset(paper_root_p, paper_dataset)
    if window_start_ms is None and not paper_df.is_empty() and "signal_ts_ms" in paper_df.columns:
        non_null = [v for v in paper_df["signal_ts_ms"].to_list() if v is not None]
        if non_null:
            window_start_ms = int(min(non_null))
    result = reconcile_backtest_paper(
        backtest_df,
        paper_df,
        signal_tolerance_ms=signal_tolerance_ms,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    report = format_backtest_paper_report(result)
    report_dir = (
        Path(output_dir).expanduser() if output_dir else paper_root_p / "reports" / "backtest_paper_reconciliation"
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "backtest_paper_reconciliation.md"
    report_path.write_text(report, encoding="utf-8")
    return {"result": result, "report": report, "report_path": str(report_path)}


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


def run_full_reconciliation(
    *,
    paper_root: str | Path,
    demo_root: str | Path,
    trading_client: Any | None = None,
    backtest_trades_csv: str | Path | None = None,
    entry_tolerance_ms: int = DEFAULT_ENTRY_TOLERANCE_MS,
    signal_tolerance_ms: int = 60_000,
    lookback_hours: int = 168,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the full reconciliation triangle (or quadrilateral when a backtest
    CSV is provided): backtest ↔ paper ↔ demo ↔ Bybit. Writes a combined
    markdown report + individual sub-reports. Returns the structured payload
    so callers can post the headline numbers to telegram or alert on regressions.

    Sub-runs that lack inputs are skipped, not errored:
    - `backtest_trades_csv=None` skips backtest↔paper
    - `trading_client=None` skips demo↔Bybit (e.g. CI without credentials)

    The remaining paper↔demo always runs since both ledgers are local.
    """
    paper_root_p = Path(paper_root).expanduser()
    demo_root_p = Path(demo_root).expanduser()
    out_root = Path(output_dir).expanduser() if output_dir else demo_root_p / "reports" / "full_reconciliation"
    out_root.mkdir(parents=True, exist_ok=True)

    sub_reports: dict[str, dict[str, Any]] = {}
    paper_demo_payload = run_paper_demo_reconciliation(
        paper_root_p,
        demo_root_p,
        entry_tolerance_ms=entry_tolerance_ms,
        output_dir=out_root / "paper_demo",
    )
    sub_reports["paper_demo"] = paper_demo_payload

    if backtest_trades_csv is not None:
        backtest_path = Path(backtest_trades_csv).expanduser()
        if backtest_path.exists():
            backtest_paper_payload = run_backtest_paper_reconciliation(
                backtest_path,
                paper_root_p,
                signal_tolerance_ms=signal_tolerance_ms,
                output_dir=out_root / "backtest_paper",
            )
            sub_reports["backtest_paper"] = backtest_paper_payload

    if trading_client is not None:
        demo_bybit_payload = run_demo_bybit_reconciliation(
            demo_root_p,
            trading_client=trading_client,
            lookback_hours=lookback_hours,
            output_dir=out_root / "demo_bybit",
        )
        sub_reports["demo_bybit"] = demo_bybit_payload

    # Combined headline
    headline = ["# Full Reconciliation — backtest ↔ paper ↔ demo ↔ Bybit", ""]
    if "backtest_paper" in sub_reports:
        s = sub_reports["backtest_paper"]["result"]["summary"]
        headline.append(
            f"- **backtest↔paper**: paired={s['paired']} "
            f"backtest-only={s['backtest_only']} paper-only={s['paper_only']} "
            f"entry_gap_bps_worst={s['entry_price_gap_bps_worst']:.2f} "
            f"exit_gap_bps_worst={s['exit_price_gap_bps_worst']:.2f} "
            f"return_gap_pct_worst={s['return_gap_pct_worst']:.3f}"
        )
    else:
        headline.append("- **backtest↔paper**: skipped (no backtest CSV provided)")
    s = sub_reports["paper_demo"]["result"]["summary"]
    headline.append(
        f"- **paper↔demo**: paired={s['paired']} paper-only={s['paper_only']} "
        f"demo-only={s['demo_only']} entry_slip_bps_mean={s['entry_slippage_bps_mean']:.2f} "
        f"exit_slip_bps_mean={s['exit_slippage_bps_mean']:.2f} "
        f"exit_gap_ms_worst={s['exit_gap_ms_worst']} "
        f"exit_reason_divergent={s['exit_reason_divergent']} "
        f"fee_gap_usdt_total={s['fee_gap_usdt_total']:.3f}"
    )
    if "demo_bybit" in sub_reports:
        s = sub_reports["demo_bybit"]["result"]["summary"]
        headline.append(
            f"- **demo↔Bybit**: paired={s['paired_closed']} "
            f"orphan_in_bybit={s['orphan_in_bybit']} orphan_in_ledger={s['orphan_in_ledger']} "
            f"open_only_in_ledger={s['open_only_in_ledger']} open_only_in_bybit={s['open_only_in_bybit']} "
            f"pnl_gap_usdt_total={s['pnl_gap_usdt_total']:.3f}"
        )
    else:
        headline.append("- **demo↔Bybit**: skipped (no Bybit credentials provided)")
    headline.append("")
    headline.append("## Sub-reports")
    headline.append("")
    for key, payload in sub_reports.items():
        headline.append(f"- {key}: `{payload['report_path']}`")
    combined_path = out_root / "full_reconciliation.md"
    combined_path.write_text("\n".join(headline) + "\n", encoding="utf-8")
    return {
        "sub_reports": sub_reports,
        "combined_report_path": str(combined_path),
    }
