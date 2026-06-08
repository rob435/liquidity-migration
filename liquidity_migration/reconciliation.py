"""Reconcile the paper (dry-run) ledger against the demo ledger.

The paper runner records idealized fills at the signal price; the demo runner
records actual Bybit demo fills. Pairing the two ledgers' trades by symbol,
side and entry time, then diffing their fill prices, measures execution
slippage — the cost the demo execution path pays that the idealized paper path
does not. Unpaired trades on either side are fill-rate divergence.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from statistics import mean, median
from typing import Any

import polars as pl

from ._common import coerce_int, finite_float
from .continuous_rebalance import (
    ContinuousRebalanceRule,
    ContinuousRebalanceScaleState,
    compute_continuous_rebalance_scale,
)
from .storage import read_dataset

_logger = logging.getLogger(__name__)

DEFAULT_ENTRY_TOLERANCE_MS = 600_000
MS_PER_DAY = 86_400_000


def _normalized_side(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"sell", "short"}:
        return "short"
    if text in {"buy", "long"}:
        return "long"
    return text


def _float(value: Any) -> float:
    # Delegates to the finite-guarded _common.finite_float: a torn/partial WS
    # message or malformed venue field must not admit NaN/inf into reconcile
    # PnL/size math (quality-dup-1 — the local copy previously passed them through).
    return finite_float(value, default=0.0) or 0.0


def _int(value: Any) -> int:
    # Thin alias kept so the existing call sites stay untouched; the implementation
    # is the shared _common.coerce_int (quality-dup-9).
    return coerce_int(value)


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


def _fmt_price(price: Any) -> str:
    """Render a fill price for a markdown table. Prices span BTC (~1e5) to micro-cap
    alts (~1e-6), so use 6 significant figures; an absent/zero price (an open trade)
    shows as '-'."""
    value = _float(price)
    return "-" if value <= 0.0 else format(value, ".6g")


def _write_pairs_csv(report_path: Path, pairs: list[dict[str, Any]]) -> str | None:
    """Write the per-paired-trade detail to a sibling CSV next to the markdown
    report — a machine-readable companion (sort by slippage, filter by symbol, …)
    for reconciliation analysis. Returns the path, or None when nothing paired."""
    if not pairs:
        return None
    csv_path = report_path.with_name(report_path.stem + "_pairs.csv")
    pl.DataFrame(pairs, infer_schema_length=None).write_csv(csv_path)
    return str(csv_path)


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
        paper_idx_opt = paper_tid_in_bucket.get(key, {}).get(tid)
        if paper_idx_opt is None:
            continue
        paper_idx = paper_idx_opt  # narrowed to int after the None-guard above
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
                    "paper_exit_price": paper_trade["exit_price"],
                    "demo_exit_price": demo_trade["exit_price"],
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
    # distinct name from the loop-scoped `exit_bps: float | None` above (same fn scope)
    exit_bps_list = [pair["exit_slippage_bps"] for pair in pairs if pair["exit_slippage_bps"] is not None]
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
        "closed_pairs": len(exit_bps_list),
        "entry_tolerance_ms": tolerance,
        "entry_slippage_bps_mean": mean(entry_bps) if entry_bps else 0.0,
        "entry_slippage_bps_median": median(entry_bps) if entry_bps else 0.0,
        "entry_slippage_bps_worst": max(entry_bps) if entry_bps else 0.0,
        "exit_slippage_bps_mean": mean(exit_bps_list) if exit_bps_list else 0.0,
        "exit_slippage_bps_median": median(exit_bps_list) if exit_bps_list else 0.0,
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
            "| symbol | side | paper entry | demo entry | entry slip bps | paper exit | demo exit | "
            "exit slip bps | exit gap (s) | paper reason | demo reason | paper ret % | demo ret % | fee Δ USDT |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for pair in result["pairs"]:
            exit_bps = pair["exit_slippage_bps"]
            paper_ret = pair["paper_return_pct"]
            demo_ret = pair["demo_return_pct"]
            exit_gap = pair["exit_gap_ms"]
            fee_gap = pair["fee_gap_usdt"]
            lines.append(
                f"| {pair['symbol']} | {pair['side']} | "
                f"{_fmt_price(pair['paper_entry_price'])} | {_fmt_price(pair['demo_entry_price'])} | "
                f"{pair['entry_slippage_bps']:.2f} | "
                f"{_fmt_price(pair.get('paper_exit_price'))} | {_fmt_price(pair.get('demo_exit_price'))} | "
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
            "| symbol | side | signal Δ ms | bt entry | paper entry | entry Δ bps | bt exit | paper exit | "
            "exit Δ bps | bt reason | paper reason | bt ret % | paper ret % | ret Δ %% |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for p in result["pairs"]:
            exit_bps = p["exit_price_gap_bps"]
            bt_ret = p["backtest_return_pct"]
            paper_ret = p["paper_return_pct"]
            ret_gap = p["return_gap_pct"]
            lines.append(
                f"| {p['symbol']} | {p['side']} | {p['signal_gap_ms']} | "
                f"{_fmt_price(p['backtest_entry_price'])} | {_fmt_price(p['paper_entry_price'])} | "
                f"{p['entry_price_gap_bps']:.3f} | "
                f"{_fmt_price(p['backtest_exit_price'])} | {_fmt_price(p['paper_exit_price'])} | "
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
    pairs_csv_path = _write_pairs_csv(report_path, result["pairs"])
    return {"result": result, "report": report, "report_path": str(report_path), "pairs_csv_path": pairs_csv_path}


def _qty_weighted_price(legs: list[tuple[float, float]]) -> float:
    """Quantity-weighted average price over ``(size, price)`` legs, ignoring legs
    with a non-positive price or size. Returns 0.0 when nothing is priced."""
    priced = [(size, price) for size, price in legs if price > 0.0 and size > 0.0]
    total = sum(size for size, _ in priced)
    return sum(size * price for size, price in priced) / total if total > 0.0 else 0.0


def _aggregate_bybit_closures(
    records: list[dict[str, Any]], *, window_ms: int = 120_000
) -> list[dict[str, Any]]:
    """Fold a multi-order close into one logical closure.

    Bybit's closed_pnl returns one row per closing ORDER, so a single ledger
    trade closed via several reduce-only orders (a partial-exit sequence, or a
    max-order-qty split) maps to multiple rows. Without folding, the extra legs
    are mis-reported as orphan closures. Rows are merged when they share
    (symbol, side) and their createdTime cluster within ``window_ms`` of the
    previous leg: summed size/pnl/fee, qty-weighted avg entry/exit over priced
    legs, earliest createdTime. The strategy holds one position per symbol with
    a cooldown, so two genuinely distinct closes on the same symbol+side inside
    the window are not expected.

    Averaging/timestamp basis (audit pass2 #5/#15/#16 — deliberate, documented):
    avg_exit/avg_entry are VWAPs over the PRICED legs only; a cluster with NO
    priced leg yields 0.0, and the downstream exit_price_gap_bps already reports
    None (not a spurious gap) in that case. avg_entry weights by ``closed_size``
    (the exit quantity) — exact for the one-position-per-symbol close this folds.
    ``created_ts_ms`` is the EARLIEST leg (chronological sort key); it is not a
    close-completion timestamp."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        by_key.setdefault((record["symbol"], record["side"]), []).append(record)
    merged: list[dict[str, Any]] = []
    for group in by_key.values():
        group.sort(key=lambda r: r["created_ts_ms"])
        clusters: list[list[dict[str, Any]]] = []
        for record in group:
            # A missing Bybit createdTime/updatedTime yields created_ts_ms=0; such
            # rows must NOT cluster (0-0 <= window would merge unrelated closures on
            # the same symbol+side into one phantom leg). Require both timestamps
            # positive before merging on proximity (audit 2026-06-02 #29).
            if (
                clusters
                and record["created_ts_ms"] > 0
                and clusters[-1][-1]["created_ts_ms"] > 0
                and record["created_ts_ms"] - clusters[-1][-1]["created_ts_ms"] <= window_ms
            ):
                clusters[-1].append(record)
            else:
                clusters.append([record])
        for cluster in clusters:
            if len(cluster) == 1:
                merged.append(cluster[0])
                continue
            merged.append(
                {
                    "symbol": cluster[0]["symbol"],
                    "side": cluster[0]["side"],
                    "avg_entry_price": _qty_weighted_price(
                        [(r["closed_size"], r["avg_entry_price"]) for r in cluster]
                    ),
                    "avg_exit_price": _qty_weighted_price(
                        [(r["closed_size"], r["avg_exit_price"]) for r in cluster]
                    ),
                    "closed_size": sum(r["closed_size"] for r in cluster),
                    "closed_pnl_usdt": sum(r["closed_pnl_usdt"] for r in cluster),
                    "exec_fee_usdt": sum(r["exec_fee_usdt"] for r in cluster),
                    "created_ts_ms": min(r["created_ts_ms"] for r in cluster),
                    "merged_legs": len(cluster),
                }
            )
    return merged


def _summarize_funding(
    records: list[dict[str, Any]] | None,
    *,
    symbols: set[str] | None = None,
) -> tuple[float, list[dict[str, Any]]]:
    """Sum Bybit funding-settlement transaction-log rows into a signed total and
    a per-symbol breakdown. Defensive: a settlement amount may arrive under
    ``funding``, ``cashFlow``, or ``change`` depending on endpoint/version, and
    the sign is account cash-flow (positive = credit to the account, i.e. a short
    that RECEIVED funding). Rows missing all amount fields contribute 0.

    ``symbols``, when provided, scopes the aggregation to that set — the demo
    ledger's traded symbols. Bybit's transaction log is ACCOUNT-wide, so a box
    that runs more than one sleeve on the same account (the short + long sleeves
    share one demo account) would otherwise fold the other sleeves' funding into
    this sleeve's reported carry. Symbols a sleeve never traded are dropped.
    (A symbol traded by BOTH sleeves cannot be split by symbol alone — the
    funding row carries no order-link/sleeve tag — and is attributed in full.)"""
    by_symbol: dict[str, float] = {}
    for record in records or []:
        symbol = str(record.get("symbol") or "")
        if not symbol:
            continue
        if symbols is not None and symbol not in symbols:
            continue
        amount = record.get("funding")
        if amount is None:
            amount = record.get("cashFlow")
        if amount is None:
            amount = record.get("change")
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + _float(amount)
    total = sum(by_symbol.values())
    breakdown = [
        {"symbol": sym, "funding_usdt": amt}
        for sym, amt in sorted(by_symbol.items(), key=lambda kv: kv[0])
    ]
    return total, breakdown


def reconcile_demo_bybit(
    demo_trades: pl.DataFrame,
    closed_pnl_records: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    funding_records: list[dict[str, Any]] | None = None,
    *,
    scope_to_ledger_symbol_sides: bool = True,
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

    reconcile-ledger-7 COVERAGE NOTE: with ``scope_to_ledger_symbol_sides=True``
    (the default, for the shared/netted account) a Bybit closure/position on a
    (symbol, side) THIS ledger never traded is dropped before orphan detection, so
    a sibling sleeve's leg is not mis-flagged. The trade-off: a TRUE orphan whose
    ledger row was lost ENTIRELY (so its (symbol, side) is absent from this ledger)
    is also no longer detectable here — that residual is covered only by the
    orders-ledger orderId->sleeve map, not by this account-level reconcile.
    ``orphan_in_bybit`` is therefore NOT exhaustive in scoped mode; pass
    ``scope_to_ledger_symbol_sides=False`` for the raw account-wide detector.
    """
    demo = _clean_trades(demo_trades)
    # reconcile-ledger-7: on the SHARED demo account Bybit's closed_pnl + open
    # positions are ACCOUNT-wide, so a sibling sleeve's closure/position on a
    # symbol+side this ledger never traded would be mis-flagged as an orphan /
    # untracked here. Scope the venue truth to the (symbol, side) pairs THIS
    # ledger actually traded. NOTE: closed_pnl carries no orderLinkId, so a
    # symbol+side traded by BOTH sleeves cannot be split here (that residual
    # needs an orderId->sleeve map from the orders ledger) -- such a same-key
    # cross-sleeve closure is still attributed to this ledger, exactly as the
    # funding path documents.
    ledger_symbol_sides = {(t["symbol"], t["side"]) for t in demo}
    cleaned_bybit: list[dict[str, Any]] = []
    for record in closed_pnl_records or []:
        symbol = str(record.get("symbol") or "")
        bybit_close_side = str(record.get("side") or "")  # Buy = closed a short; Sell = closed a long
        original_side = "short" if bybit_close_side == "Buy" else ("long" if bybit_close_side == "Sell" else "")
        avg_exit = _float(record.get("avgExitPrice"))
        avg_entry = _float(record.get("avgEntryPrice"))
        closed_size = _float(record.get("closedSize"))
        # Only identity + a positive closedSize are essential. A closure missing
        # avgEntry/avgExit (Bybit omits them on some close types) must still be
        # surfaced — dropping it here would hide a real orphan from the
        # cross-check. The exit-price-gap calc below already guards avg_exit<=0.
        if not symbol or not original_side or closed_size <= 0.0:
            continue
        if scope_to_ledger_symbol_sides and (symbol, original_side) not in ledger_symbol_sides:
            # Sibling-sleeve closure on a symbol+side this ledger never traded:
            # not OUR orphan. Drop before aggregation + orphan detection.
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
    # Fold multi-order closes (one ledger trade closed via N reduce-only orders)
    # into one logical closure so the extra legs aren't mis-flagged as orphans.
    cleaned_bybit = _aggregate_bybit_closures(cleaned_bybit)

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
                # None (not 0.0) when Bybit reported no usable exit price for the
                # closure (e.g. a multi-leg close where avgExitPrice is omitted):
                # a 0.0 gap reads as a perfect match and falsely deflates the
                # slippage mean/worst. None == "unknown" and is excluded from the
                # aggregates (matches the backtest<->paper path).
                "exit_price_gap_bps": (
                    abs(dt["exit_price"] - bb["avg_exit_price"]) / bb["avg_exit_price"] * 10_000.0
                    if bb["avg_exit_price"] > 0.0
                    else None
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
    # Duplicate open trades on the same (symbol, side) collapse into a single key
    # for the cross-check below, so surface them separately: the strategy holds
    # one position per symbol, so >1 open ledger trade on a key is a stacking bug
    # the open-position diff would otherwise hide.
    open_key_counts: dict[tuple[str, str], int] = {}
    for trade in open_demo_trades:
        key = (trade["symbol"], trade["side"])
        open_key_counts[key] = open_key_counts.get(key, 0) + 1
    duplicate_open_ledger = [
        {"symbol": sym, "side": side, "count": count}
        for (sym, side), count in sorted(open_key_counts.items())
        if count > 1
    ]
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
        if scope_to_ledger_symbol_sides and key not in ledger_symbol_sides:
            # Sibling-sleeve live position on a symbol+side this ledger never
            # traded: not this sleeve's untracked position. Skip the cross-check.
            continue
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

    # Funding is settled separately from Bybit's closedPnl (which is trading
    # P&L net of trading fees only). For a short, positive funding is a CREDIT
    # the closedPnl-based pnl_gap never sees — surfacing it shows the live
    # short's true realized P&L (trading + funding). (E6)
    # Scope funding to THIS ledger's traded symbols — the transaction log is
    # account-wide and the short + long sleeves share one demo account, so an
    # unscoped sum would contaminate the short's reported funding with the
    # long sleeve's (M7).
    ledger_symbols = {str(t.get("symbol") or "") for t in demo} - {""}
    funding_total_usdt, funding_by_symbol = _summarize_funding(
        funding_records, symbols=ledger_symbols
    )

    exit_price_bps_all = [p["exit_price_gap_bps"] for p in pairs if p["exit_price_gap_bps"] is not None]
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
        "duplicate_open_ledger": len(duplicate_open_ledger),
        "exit_price_gap_bps_mean": mean(exit_price_bps_all) if exit_price_bps_all else 0.0,
        "exit_price_gap_bps_worst": max(exit_price_bps_all) if exit_price_bps_all else 0.0,
        "pnl_gap_usdt_total": sum(pnl_gaps_all) if pnl_gaps_all else 0.0,
        "exit_ts_gap_ms_mean": mean(exit_ts_gaps_all) if exit_ts_gaps_all else 0,
        "exit_ts_gap_ms_worst": max(exit_ts_gaps_all) if exit_ts_gaps_all else 0,
        "funding_settlement_usdt_total": funding_total_usdt,
    }
    return {
        "summary": summary,
        "pairs": pairs,
        "orphan_in_bybit": orphan_in_bybit,
        "orphan_in_ledger": orphan_in_ledger,
        "open_only_in_ledger": [{"symbol": s, "side": side} for s, side in open_only_in_ledger],
        "open_only_in_bybit": [{"symbol": s, "side": side} for s, side in open_only_in_bybit],
        "open_position_diffs": open_position_diffs,
        "duplicate_open_ledger": duplicate_open_ledger,
        "funding_by_symbol": funding_by_symbol,
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
        f"- duplicate open ledger trades on one (symbol, side) — stacking bug: "
        f"**{summary.get('duplicate_open_ledger', 0)}**",
        "",
        "## Exit-price gap (ledger vs Bybit avgExitPrice)",
        "",
        f"- mean: {summary['exit_price_gap_bps_mean']:.2f} bps",
        f"- worst: {summary['exit_price_gap_bps_worst']:.2f} bps",
        "",
        "## PnL gap (ledger gross_pnl − Bybit closedPnl, USDT)",
        "",
        "Bybit's closedPnl is net of fees; the ledger PnL is gross. This residual",
        "is therefore dominated by the fee cost, PLUS any fill-price reconciliation",
        "error AND any quantity mismatch (ledger qty vs Bybit closedSize — see the",
        "`qty Δ` column): the ledger leg is priced on its qty and the Bybit leg on",
        "closedSize, so a non-zero `qty Δ` adds a size-driven term that is NOT a fee.",
        "Read it as a gross-minus-net residual, not a pure fee total.",
        "",
        f"- total across paired: {summary['pnl_gap_usdt_total']:.3f}",
        "",
        "## Funding settlements (USDT; +ve = the account RECEIVED funding)",
        "",
        "Settled separately from closedPnl. For a short, positive funding is a",
        "credit — realized P&L the pnl-gap above never captures.",
        "",
        f"- total over window: {summary.get('funding_settlement_usdt_total', 0.0):.3f}",
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
            exit_bps_txt = (
                f"{p['exit_price_gap_bps']:.2f}" if p["exit_price_gap_bps"] is not None else "—"
            )
            lines.append(
                f"| {p['symbol']} | {p['side']} | {p['qty_gap']:.6g} | "
                f"{exit_bps_txt} | {p['exit_ts_gap_ms'] / 1000.0:.1f} | "
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
    closed_pnl_fetch_errors: list[str] = []
    for sym in sorted(ledger_symbols | bybit_symbols):
        if not sym:
            continue
        try:
            rows = trading_client.get_closed_pnl(symbol=sym, start_time_ms=start_ms, end_time_ms=end_ms, limit=200)
        except Exception as exc:  # noqa: BLE001 - one-symbol failure should not kill the whole reconciliation
            # ...but it must not vanish: a swallowed fetch makes this symbol look
            # like a genuine ledger<->venue mismatch in the report with no way to
            # tell it from a transient API error. Log it + surface the symbol so
            # the operator can distinguish a fetch error from a real mismatch.
            _logger.warning("reconcile: get_closed_pnl failed for %s: %s", sym, exc)
            closed_pnl_fetch_errors.append(sym)
            continue
        for r in rows or []:
            r["_symbol_query"] = sym
            closed_records.append(r)
    if closed_pnl_fetch_errors:
        _logger.warning(
            "reconcile: %d symbol(s) had closed-pnl fetch errors (treat their mismatches as suspect): %s",
            len(closed_pnl_fetch_errors), ", ".join(closed_pnl_fetch_errors),
        )

    # Funding settlements (optional): surfaces the short's funding tailwind/drag,
    # which closedPnl excludes. hasattr-guarded so older clients still work, and
    # a fetch failure degrades to "no funding data" rather than killing the run.
    funding_records: list[dict[str, Any]] = []
    fetch_funding = getattr(trading_client, "get_funding_settlements", None)
    if callable(fetch_funding):
        try:
            funding_records = fetch_funding(start_time_ms=start_ms, end_time_ms=end_ms) or []
        except Exception:  # noqa: BLE001 - funding visibility is best-effort, never fatal
            funding_records = []

    result = reconcile_demo_bybit(
        trades, closed_records, open_positions, funding_records,
        scope_to_ledger_symbol_sides=True,
    )
    report = format_demo_bybit_report(result)
    report_dir = (
        Path(output_dir).expanduser() if output_dir else demo_root_p / "reports" / "demo_bybit_reconciliation"
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "demo_bybit_reconciliation.md"
    report_path.write_text(report, encoding="utf-8")
    pairs_csv_path = _write_pairs_csv(report_path, result.get("pairs") or [])
    return {"result": result, "report": report, "report_path": str(report_path), "pairs_csv_path": pairs_csv_path,
            "closed_pnl_fetch_errors": closed_pnl_fetch_errors}


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


def run_continuous_paper_demo_reconciliation(
    paper_root: str | Path,
    demo_root: str | Path,
    *,
    entry_tolerance_ms: int = DEFAULT_ENTRY_TOLERANCE_MS,
    output_dir: str | Path | None = None,
    min_pairs_warning: int = 20,
) -> dict[str, Any]:
    """Continuous-fade sleeve (3rd sleeve) paper/demo execution-slippage
    reconciler. Same pairing as the short/long reconcilers but reads the
    continuous sleeve's own ledger datasets (``continuous_fade_paper_trades``
    vs ``continuous_fade_demo_trades``). Like the long reconciler it emits a
    ``sample_warning`` when fewer than ``min_pairs_warning`` pairs were matched
    (continuous is sub-hourly so its pair count grows fast, but a fresh sleeve
    still warrants the caveat). The continuous demo is intra-hour decile-cross
    driven, so a small entry-time skew vs the paper fill is expected.
    """
    payload = _run_reconciliation(
        paper_root=paper_root,
        demo_root=demo_root,
        paper_dataset="continuous_fade_paper_trades",
        demo_dataset="continuous_fade_demo_trades",
        report_subdir="continuous_paper_demo_reconciliation",
        report_filename="continuous_paper_demo_reconciliation.md",
        entry_tolerance_ms=entry_tolerance_ms,
        output_dir=output_dir,
    )
    summary = payload["result"]["summary"]
    summary["min_pairs_warning_threshold"] = int(min_pairs_warning)
    summary["sample_warning"] = bool(summary["paired"] < int(min_pairs_warning))
    return payload


def _rebalance_prior_state_from_cycles(cycles: pl.DataFrame, *, current_day_ts: int) -> ContinuousRebalanceScaleState:
    if cycles.is_empty():
        return ContinuousRebalanceScaleState(prior_raw_returns=())
    current_day = (int(current_day_ts) // MS_PER_DAY) * MS_PER_DAY
    latest_by_day: dict[int, tuple[tuple[int, int], float, dict[str, Any]]] = {}
    for idx, row in enumerate(cycles.to_dicts()):
        day = _int(row.get("rebalance_day_ts"))
        raw = _float(row.get("rebalance_raw_return"))
        if day <= 0 or day >= current_day:
            continue
        day = (day // MS_PER_DAY) * MS_PER_DAY
        key = (_int(row.get("ts_ms")), idx)
        prev = latest_by_day.get(day)
        if prev is None or key > prev[0]:
            latest_by_day[day] = (key, raw, row)
    if not latest_by_day:
        return ContinuousRebalanceScaleState(prior_raw_returns=())
    days = sorted(latest_by_day)
    latest = latest_by_day[days[-1]][2]
    equity = _float(latest.get("rebalance_scaled_equity")) or 1.0
    peak = _float(latest.get("rebalance_scaled_peak")) or max(equity, 1.0)
    return ContinuousRebalanceScaleState(
        prior_raw_returns=tuple(latest_by_day[day][1] for day in days),
        prior_scaled_equity=equity if equity > 0.0 else 1.0,
        prior_scaled_peak=max(peak, equity, 1.0),
    )


def audit_continuous_rebalance_cycles(
    cycles: pl.DataFrame,
    orders: pl.DataFrame,
    *,
    rule: ContinuousRebalanceRule | None = None,
    scale_tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Audit daily-rebalance cycle telemetry for causal scale and resize discipline."""
    rule = rule or ContinuousRebalanceRule()
    issues: list[dict[str, Any]] = []
    if cycles.is_empty():
        return {
            "ok": False,
            "summary": {
                "cycles": 0,
                "rebalance_cycles": 0,
                "days": 0,
                "scale_mismatches": 0,
                "same_day_resize_violations": 0,
                "resize_order_count_mismatch": False,
            },
            "issues": [{"kind": "empty_cycles", "message": "no cycle rows"}],
        }

    if "rebalance_day_ts" not in cycles.columns:
        return {
            "ok": False,
            "summary": {
                "cycles": cycles.height,
                "rebalance_cycles": 0,
                "days": 0,
                "scale_mismatches": 0,
                "same_day_resize_violations": 0,
                "resize_order_count_mismatch": False,
            },
            "issues": [{"kind": "missing_rebalance_columns", "message": "rebalance_day_ts column missing"}],
        }

    rebalance = cycles.filter(pl.col("rebalance_day_ts").is_not_null()).sort("ts_ms")
    scale_mismatches = 0
    for row in rebalance.to_dicts():
        day = _int(row.get("rebalance_day_ts"))
        if day <= 0:
            continue
        expected = compute_continuous_rebalance_scale(
            _rebalance_prior_state_from_cycles(rebalance, current_day_ts=day),
            rule,
        )
        observed = _float(row.get("rebalance_target_scale"))
        if abs(expected - observed) > scale_tolerance:
            scale_mismatches += 1
            issues.append(
                {
                    "kind": "scale_mismatch",
                    "cycle_id": str(row.get("cycle_id") or ""),
                    "day_ts": day,
                    "expected": expected,
                    "observed": observed,
                }
            )

    same_day_violations = 0
    for day in sorted({(_int(r.get("rebalance_day_ts")) // MS_PER_DAY) * MS_PER_DAY for r in rebalance.to_dicts()}):
        if day <= 0:
            continue
        rows = [
            r for r in rebalance.to_dicts()
            if (_int(r.get("rebalance_day_ts")) // MS_PER_DAY) * MS_PER_DAY == day
        ]
        rows.sort(key=lambda r: _int(r.get("ts_ms")))
        active_resize_rows = [r for r in rows if _int(r.get("rebalance_resize_orders")) > 0]
        if len(active_resize_rows) > 1:
            same_day_violations += 1
            issues.append({"kind": "same_day_multiple_resize", "day_ts": day, "count": len(active_resize_rows)})
        for idx, row in enumerate(rows[1:], start=1):
            skipped = str(row.get("rebalance_resize_skipped_same_day")).strip().lower() in {"1", "true", "yes"}
            if not skipped:
                same_day_violations += 1
                issues.append(
                    {
                        "kind": "same_day_skip_missing",
                        "day_ts": day,
                        "cycle_id": str(row.get("cycle_id") or ""),
                        "row_index": idx,
                    }
                )

    cycle_resize_orders = sum(_int(r.get("rebalance_resize_orders")) for r in rebalance.to_dicts())
    order_resize_orders = 0
    if not orders.is_empty() and "resize_reason" in orders.columns:
        order_resize_orders = orders.filter(pl.col("resize_reason").is_not_null()).height
    mismatch = order_resize_orders != cycle_resize_orders
    if mismatch:
        issues.append(
            {
                "kind": "resize_order_count_mismatch",
                "cycle_resize_orders": cycle_resize_orders,
                "order_resize_orders": order_resize_orders,
            }
        )

    summary = {
        "cycles": cycles.height,
        "rebalance_cycles": rebalance.height,
        "days": len({(_int(r.get("rebalance_day_ts")) // MS_PER_DAY) * MS_PER_DAY for r in rebalance.to_dicts()}),
        "scale_mismatches": scale_mismatches,
        "same_day_resize_violations": same_day_violations,
        "cycle_resize_orders": cycle_resize_orders,
        "order_resize_orders": order_resize_orders,
        "resize_order_count_mismatch": mismatch,
    }
    return {"ok": not issues, "summary": summary, "issues": issues}


def format_continuous_rebalance_audit_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Continuous Daily-Rebalance Cycle Audit",
        "",
        f"ok: `{payload['ok']}`",
        f"cycles: `{summary['cycles']}`",
        f"rebalance_cycles: `{summary['rebalance_cycles']}`",
        f"days: `{summary['days']}`",
        f"scale_mismatches: `{summary['scale_mismatches']}`",
        f"same_day_resize_violations: `{summary['same_day_resize_violations']}`",
        f"cycle_resize_orders: `{summary.get('cycle_resize_orders', 0)}`",
        f"order_resize_orders: `{summary.get('order_resize_orders', 0)}`",
        "",
        "This is a ledger-consistency audit, not promotion or OOS evidence.",
    ]
    issues = payload.get("issues") or []
    if issues:
        lines.extend(["", "## Issues"])
        for issue in issues[:50]:
            lines.append(f"- `{issue.get('kind')}`: {issue}")
    return "\n".join(lines) + "\n"


def run_continuous_rebalance_cycle_audit(
    data_root: str | Path,
    *,
    cycles_dataset: str = "continuous_fade_paper_cycles",
    orders_dataset: str = "continuous_fade_paper_orders",
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(data_root).expanduser()
    payload = audit_continuous_rebalance_cycles(
        read_dataset(root, cycles_dataset),
        read_dataset(root, orders_dataset),
    )
    report = format_continuous_rebalance_audit_report(payload)
    report_dir = Path(output_dir).expanduser() if output_dir else root / "reports" / "continuous_rebalance_cycle_audit"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "continuous_rebalance_cycle_audit.md"
    report_path.write_text(report, encoding="utf-8")
    return {"result": payload, "report": report, "report_path": str(report_path)}


def format_continuous_forward_readiness_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    paper_summary = payload["paper_rebalance"]["result"]["summary"]
    demo_rebalance = payload.get("demo_rebalance")
    paper_demo = payload.get("paper_demo")
    demo_summary = demo_rebalance["result"]["summary"] if demo_rebalance is not None else None
    reconcile_summary = paper_demo["result"]["summary"] if paper_demo is not None else None
    mode_text = (
        "paper-only telemetry gate for the no-order evidence collector"
        if summary.get("paper_only_mode")
        else "paper/demo ledger-readiness gate"
    )
    lines = [
        "# Continuous Forward-Readiness Gate",
        "",
        f"ok: `{payload['ok']}`",
        "",
        f"This is a {mode_text}, not promotion or real-money evidence.",
        "",
        "## Summary",
        "",
        f"- paper-only mode: `{summary['paper_only_mode']}`",
        f"- paper rebalance ok: `{summary['paper_rebalance_ok']}`",
        f"- demo rebalance ok: `{summary['demo_rebalance_ok']}`",
        f"- paired trades: `{summary['paired']}`",
        f"- sample warning: `{summary['sample_warning']}`",
        f"- paper-only trades: `{summary['paper_only']}`",
        f"- demo-only trades: `{summary['demo_only']}`",
        f"- no unmatched required: `{summary['require_no_unmatched']}`",
        "",
        "## Paper Rebalance",
        "",
        f"- cycles: `{paper_summary['cycles']}`",
        f"- rebalance_cycles: `{paper_summary['rebalance_cycles']}`",
        f"- scale_mismatches: `{paper_summary['scale_mismatches']}`",
        f"- same_day_resize_violations: `{paper_summary['same_day_resize_violations']}`",
        f"- resize_order_count_mismatch: `{paper_summary['resize_order_count_mismatch']}`",
        f"- report: `{payload['paper_rebalance']['report_path']}`",
    ]
    if demo_summary is not None and demo_rebalance is not None:
        lines.extend(
            [
                "",
                "## Demo Rebalance",
                "",
                f"- cycles: `{demo_summary['cycles']}`",
                f"- rebalance_cycles: `{demo_summary['rebalance_cycles']}`",
                f"- scale_mismatches: `{demo_summary['scale_mismatches']}`",
                f"- same_day_resize_violations: `{demo_summary['same_day_resize_violations']}`",
                f"- resize_order_count_mismatch: `{demo_summary['resize_order_count_mismatch']}`",
                f"- report: `{demo_rebalance['report_path']}`",
            ]
        )
    else:
        lines.extend(["", "## Demo Rebalance", "", "- skipped: `paper_only_mode`"])
    if reconcile_summary is not None and paper_demo is not None:
        lines.extend(
            [
                "",
                "## Paper vs Demo",
                "",
                f"- paired: `{reconcile_summary['paired']}`",
                f"- paper_only: `{reconcile_summary['paper_only']}`",
                f"- demo_only: `{reconcile_summary['demo_only']}`",
                f"- entry_slippage_bps_mean: `{reconcile_summary['entry_slippage_bps_mean']:.2f}`",
                f"- exit_slippage_bps_mean: `{reconcile_summary['exit_slippage_bps_mean']:.2f}`",
                f"- report: `{paper_demo['report_path']}`",
                f"- pairs_csv: `{paper_demo.get('pairs_csv_path') or ''}`",
            ]
        )
    else:
        lines.extend(["", "## Paper vs Demo", "", "- skipped: `paper_only_mode`"])
    if not payload["ok"]:
        lines.extend(["", "## Blocking Issues", ""])
        for issue in payload["issues"]:
            lines.append(f"- {issue}")
    return "\n".join(lines) + "\n"


def run_continuous_forward_readiness(
    paper_root: str | Path,
    demo_root: str | Path,
    *,
    entry_tolerance_ms: int = 600_000,
    min_pairs_warning: int = 20,
    require_no_unmatched: bool = True,
    require_demo: bool = True,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the continuous candidate's forward-ledger readiness checks.

    This combines the daily-rebalance telemetry audit on both paper and demo
    roots with the paper↔demo trade reconciliation. It deliberately does not
    label alpha or promotion; it only says whether the forward ledgers are clean
    enough to be used as the next arbiter.
    """
    paper_root_p = Path(paper_root).expanduser()
    demo_root_p = Path(demo_root).expanduser()
    out_root = (
        Path(output_dir).expanduser()
        if output_dir
        else paper_root_p / "reports" / "continuous_forward_readiness"
    )
    out_root.mkdir(parents=True, exist_ok=True)

    paper_rebalance = run_continuous_rebalance_cycle_audit(
        paper_root_p,
        cycles_dataset="continuous_fade_paper_cycles",
        orders_dataset="continuous_fade_paper_orders",
        output_dir=out_root / "paper_rebalance",
    )
    demo_rebalance = None
    paper_demo = None
    rec_summary: dict[str, Any] = {
        "paired": 0,
        "paper_only": 0,
        "demo_only": 0,
        "sample_warning": False,
        "min_pairs_warning_threshold": min_pairs_warning,
    }
    if require_demo:
        demo_rebalance = run_continuous_rebalance_cycle_audit(
            demo_root_p,
            cycles_dataset="continuous_fade_demo_cycles",
            orders_dataset="continuous_fade_demo_orders",
            output_dir=out_root / "demo_rebalance",
        )
        paper_demo = run_continuous_paper_demo_reconciliation(
            paper_root_p,
            demo_root_p,
            entry_tolerance_ms=entry_tolerance_ms,
            min_pairs_warning=min_pairs_warning,
            output_dir=out_root / "paper_demo",
        )
        rec_summary = paper_demo["result"]["summary"]

    issues: list[str] = []
    if not paper_rebalance["result"]["ok"]:
        issues.append("paper rebalance telemetry audit failed")
    if require_demo and demo_rebalance is not None and not demo_rebalance["result"]["ok"]:
        issues.append("demo rebalance telemetry audit failed")
    if require_demo and rec_summary.get("sample_warning"):
        issues.append(
            f"paired trades {rec_summary['paired']} below min_pairs_warning {rec_summary['min_pairs_warning_threshold']}"
        )
    if (
        require_demo
        and require_no_unmatched
        and (int(rec_summary["paper_only"]) > 0 or int(rec_summary["demo_only"]) > 0)
    ):
        issues.append(
            f"unmatched trades present: paper_only={rec_summary['paper_only']} demo_only={rec_summary['demo_only']}"
        )
    demo_rebalance_ok = None
    if require_demo:
        assert demo_rebalance is not None
        demo_rebalance_ok = bool(demo_rebalance["result"]["ok"])

    summary = {
        "paper_only_mode": not require_demo,
        "paper_rebalance_ok": bool(paper_rebalance["result"]["ok"]),
        "demo_rebalance_ok": demo_rebalance_ok,
        "paired": int(rec_summary["paired"]),
        "paper_only": int(rec_summary["paper_only"]),
        "demo_only": int(rec_summary["demo_only"]),
        "sample_warning": bool(rec_summary.get("sample_warning")),
        "min_pairs_warning_threshold": int(rec_summary.get("min_pairs_warning_threshold", min_pairs_warning)),
        "require_no_unmatched": bool(require_no_unmatched),
    }
    payload = {
        "ok": not issues,
        "summary": summary,
        "issues": issues,
        "paper_rebalance": paper_rebalance,
        "demo_rebalance": demo_rebalance,
        "paper_demo": paper_demo,
    }
    report = format_continuous_forward_readiness_report(payload)
    report_path = out_root / "continuous_forward_readiness.md"
    report_path.write_text(report, encoding="utf-8")
    payload["report"] = report
    payload["report_path"] = str(report_path)
    return payload


def _row_timestamp(row: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = _int(row.get(key))
        if value > 0:
            return value
    return 0


def _latest_trade_rows(trades: pl.DataFrame) -> list[dict[str, Any]]:
    if trades.is_empty():
        return []
    rows = trades.to_dicts()
    if "trade_id" not in trades.columns:
        return rows
    latest: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
    for idx, row in enumerate(rows):
        trade_id = str(row.get("trade_id") or "")
        if not trade_id:
            continue
        ts = _row_timestamp(row, ("updated_at_ms", "closed_at_ms", "exit_ts_ms", "entry_ts_ms", "ts_ms"))
        key = (ts, idx)
        if trade_id not in latest or key > latest[trade_id][0]:
            latest[trade_id] = (key, row)
    return [item[1] for item in latest.values()] if latest else rows


def _fee_adjusted_return(row: dict[str, Any]) -> float | None:
    has_return = False
    ret = 0.0
    for key in ("net_return", "gross_trade_return"):
        value = row.get(key)
        if value not in (None, ""):
            ret = _float(value)
            has_return = True
            break
    if not has_return:
        entry = _float(row.get("entry_price"))
        exit_price = _float(row.get("exit_price"))
        if entry <= 0.0 or exit_price <= 0.0:
            return None
        side = _normalized_side(row.get("side") or row.get("trade_side") or "short")
        ret = (entry - exit_price) / entry if side == "short" else (exit_price - entry) / entry
        notional = _float(row.get("notional_usdt"))
        equity = _float(row.get("equity_usdt"))
        if notional > 0.0 and equity > 0.0:
            ret *= notional / equity
    equity = _float(row.get("equity_usdt"))
    if equity > 0.0:
        fees = _float(row.get("entry_fee_usdt")) + _float(row.get("exit_fee_usdt"))
        ret -= fees / equity
    return ret


def _trade_ledger_daily_returns(trades: pl.DataFrame) -> dict[int, float]:
    daily: dict[int, float] = {}
    for row in _latest_trade_rows(trades):
        ts = _row_timestamp(row, ("exit_ts_ms", "closed_at_ms"))
        if ts <= 0:
            continue
        ret = _fee_adjusted_return(row)
        if ret is None:
            continue
        day = (ts // MS_PER_DAY) * MS_PER_DAY
        daily[day] = daily.get(day, 0.0) + float(ret)
    return daily


def _cycle_zero_daily_returns(cycles: pl.DataFrame) -> dict[int, float]:
    if cycles.is_empty() or "ts_ms" not in cycles.columns:
        return {}
    daily: dict[int, float] = {}
    for row in cycles.to_dicts():
        ts = _row_timestamp(row, ("ts_ms",))
        if ts <= 0:
            continue
        daily[(ts // MS_PER_DAY) * MS_PER_DAY] = 0.0
    return daily


def _daily_forward_returns(trades: pl.DataFrame, cycles: pl.DataFrame) -> tuple[dict[int, float], str]:
    cycle_returns = _cycle_zero_daily_returns(cycles)
    trade_returns = _trade_ledger_daily_returns(trades)
    if not cycle_returns:
        return trade_returns, "event_demo_trades"
    merged = dict(cycle_returns)
    merged.update(trade_returns)
    source = "event_demo_cycles+event_demo_trades" if trade_returns else "event_demo_cycles"
    return merged, source


def _continuous_cycle_daily_returns(cycles: pl.DataFrame) -> dict[int, float]:
    if cycles.is_empty() or "rebalance_day_ts" not in cycles.columns:
        return {}
    latest: dict[int, tuple[tuple[int, int], float]] = {}
    for idx, row in enumerate(cycles.to_dicts()):
        day = _int(row.get("rebalance_day_ts"))
        if day <= 0:
            continue
        day = (day // MS_PER_DAY) * MS_PER_DAY
        ret = _float(row.get("rebalance_scaled_return"))
        key = (_row_timestamp(row, ("ts_ms", "updated_at_ms")), idx)
        if day not in latest or key > latest[day][0]:
            latest[day] = (key, ret)
    return {day: item[1] for day, item in latest.items()}


def _calendar_metrics(returns_by_day: dict[int, float], *, start_day: int, end_day: int) -> dict[str, Any]:
    if start_day <= 0 or end_day < start_day:
        return {
            "days": 0,
            "total_return": 0.0,
            "annualized_return": 0.0,
            "max_drawdown": 0.0,
            "mar": None,
            "worst_day_return": 0.0,
            "equity": [],
        }
    days = list(range(start_day, end_day + MS_PER_DAY, MS_PER_DAY))
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    curve: list[dict[str, Any]] = []
    worst = 0.0
    for day in days:
        ret = float(returns_by_day.get(day, 0.0))
        equity += ret
        peak = max(peak, equity)
        dd = equity - peak
        max_drawdown = min(max_drawdown, dd)
        worst = min(worst, ret)
        curve.append({"ts_ms": day, "basket_return": ret, "equity": equity, "drawdown": dd})
    total = equity - 1.0
    years = max((len(days) - 1) / 365.25, 1e-9)
    annualized = total / years
    mar = annualized / abs(max_drawdown) if abs(max_drawdown) > 1e-12 else None
    return {
        "days": len(days),
        "total_return": total,
        "annualized_return": annualized,
        "max_drawdown": max_drawdown,
        "mar": mar,
        "worst_day_return": worst,
        "equity": curve,
    }


def _format_mar(value: float | None) -> str:
    return "NA" if value is None else f"{value:.6f}"


def _latest_day(returns_by_day: dict[int, float]) -> int:
    return max(returns_by_day) if returns_by_day else 0


def _maturity_day(start_day: int, min_common_days: int) -> int:
    if start_day <= 0 or min_common_days <= 0:
        return 0
    return start_day + (min_common_days - 1) * MS_PER_DAY


def format_continuous_vs_daily_forward_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    daily = payload["daily"]
    continuous = payload["continuous"]
    lines = [
        "# Continuous vs Daily Forward Comparator",
        "",
        f"ok: `{payload['ok']}`",
        "",
        "This compares realized forward ledger performance over the same calendar window.",
        "",
        "## Summary",
        "",
        f"- common_start_day_ts: `{summary['common_start_day_ts']}`",
        f"- common_end_day_ts: `{summary['common_end_day_ts']}`",
        f"- common_days: `{summary['common_days']}`",
        f"- min_common_days: `{summary['min_common_days']}`",
        f"- common_days_remaining: `{summary['common_days_remaining']}`",
        f"- maturity_day_ts: `{summary['maturity_day_ts']}`",
        f"- daily_observed_days: `{summary['daily_observed_days']}`",
        f"- continuous_observed_days: `{summary['continuous_observed_days']}`",
        f"- latest_daily_day_ts: `{summary['latest_daily_day_ts']}`",
        f"- latest_continuous_day_ts: `{summary['latest_continuous_day_ts']}`",
        f"- continuous beats return: `{summary['continuous_beats_return']}`",
        f"- continuous beats MAR: `{summary['continuous_beats_mar']}`",
        "",
        "## Daily Short",
        "",
        f"- source: `{summary['daily_source']}`",
        f"- total_return: `{daily['total_return']:.6f}`",
        f"- annualized_return: `{daily['annualized_return']:.6f}`",
        f"- max_drawdown: `{daily['max_drawdown']:.6f}`",
        f"- mar: `{_format_mar(daily['mar'])}`",
        f"- worst_day_return: `{daily['worst_day_return']:.6f}`",
        "",
        "## Continuous",
        "",
        f"- source: `{summary['continuous_source']}`",
        f"- total_return: `{continuous['total_return']:.6f}`",
        f"- annualized_return: `{continuous['annualized_return']:.6f}`",
        f"- max_drawdown: `{continuous['max_drawdown']:.6f}`",
        f"- mar: `{_format_mar(continuous['mar'])}`",
        f"- worst_day_return: `{continuous['worst_day_return']:.6f}`",
    ]
    if not payload["ok"]:
        lines.extend(["", "## Blocking Issues", ""])
        for issue in payload["issues"]:
            lines.append(f"- {issue}")
    return "\n".join(lines) + "\n"


def run_continuous_vs_daily_forward_comparison(
    daily_root: str | Path,
    continuous_root: str | Path,
    *,
    daily_trades_dataset: str = "event_demo_trades",
    daily_cycles_dataset: str = "event_demo_cycles",
    continuous_cycles_dataset: str = "continuous_fade_paper_cycles",
    continuous_trades_dataset: str = "continuous_fade_paper_trades",
    min_common_days: int = 30,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    daily_root_p = Path(daily_root).expanduser()
    continuous_root_p = Path(continuous_root).expanduser()
    daily_returns, daily_source = _daily_forward_returns(
        read_dataset(daily_root_p, daily_trades_dataset),
        read_dataset(daily_root_p, daily_cycles_dataset),
    )
    cycles = read_dataset(continuous_root_p, continuous_cycles_dataset)
    continuous_returns = _continuous_cycle_daily_returns(cycles)
    continuous_source = continuous_cycles_dataset
    if not continuous_returns:
        continuous_returns = _trade_ledger_daily_returns(read_dataset(continuous_root_p, continuous_trades_dataset))
        continuous_source = continuous_trades_dataset

    issues: list[str] = []
    if not daily_returns:
        issues.append(
            f"daily return series is empty from {daily_root_p}/{daily_cycles_dataset}+{daily_trades_dataset}"
        )
    if not continuous_returns:
        issues.append(f"continuous return series is empty from {continuous_root_p}/{continuous_source}")

    start = max(min(daily_returns) if daily_returns else 0, min(continuous_returns) if continuous_returns else 0)
    end = min(max(daily_returns) if daily_returns else 0, max(continuous_returns) if continuous_returns else 0)
    common_days = ((end - start) // MS_PER_DAY + 1) if start > 0 and end >= start else 0
    if common_days < int(min_common_days):
        issues.append(f"common forward window {common_days}d below min_common_days {int(min_common_days)}")

    daily = _calendar_metrics(daily_returns, start_day=start, end_day=end)
    continuous = _calendar_metrics(continuous_returns, start_day=start, end_day=end)
    continuous_beats_return = bool(continuous["total_return"] > daily["total_return"])
    daily_mar = daily["mar"]
    continuous_mar = continuous["mar"]
    continuous_beats_mar = bool(
        continuous_mar is not None and daily_mar is not None and continuous_mar > daily_mar
    )
    performance_window_ready = common_days >= int(min_common_days) and bool(daily_returns) and bool(continuous_returns)
    if performance_window_ready and not continuous_beats_return:
        issues.append(
            f"continuous return {continuous['total_return']:.6f} <= daily return {daily['total_return']:.6f}"
        )
    if performance_window_ready and not continuous_beats_mar:
        issues.append(f"continuous MAR {_format_mar(continuous_mar)} <= daily MAR {_format_mar(daily_mar)}")

    summary = {
        "common_start_day_ts": start,
        "common_end_day_ts": end,
        "common_days": common_days,
        "min_common_days": int(min_common_days),
        "common_days_remaining": max(int(min_common_days) - common_days, 0),
        "maturity_day_ts": _maturity_day(start, int(min_common_days)),
        "daily_observed_days": len(daily_returns),
        "continuous_observed_days": len(continuous_returns),
        "latest_daily_day_ts": _latest_day(daily_returns),
        "latest_continuous_day_ts": _latest_day(continuous_returns),
        "daily_source": daily_source,
        "continuous_source": continuous_source,
        "continuous_beats_return": continuous_beats_return,
        "continuous_beats_mar": continuous_beats_mar,
    }
    payload = {
        "ok": not issues,
        "inputs": {
            "daily_root": str(daily_root_p),
            "continuous_root": str(continuous_root_p),
            "daily_trades_dataset": daily_trades_dataset,
            "daily_cycles_dataset": daily_cycles_dataset,
            "continuous_cycles_dataset": continuous_cycles_dataset,
            "continuous_trades_dataset": continuous_trades_dataset,
            "min_common_days": int(min_common_days),
        },
        "summary": summary,
        "daily": {k: v for k, v in daily.items() if k != "equity"},
        "continuous": {k: v for k, v in continuous.items() if k != "equity"},
        "issues": issues,
    }
    report = format_continuous_vs_daily_forward_report(payload)
    report_dir = Path(output_dir).expanduser() if output_dir else continuous_root_p / "reports" / "continuous_vs_daily_forward"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "continuous_vs_daily_forward.md"
    report_path.write_text(report, encoding="utf-8")
    json_path = report_dir / "continuous_vs_daily_forward.json"
    daily_equity_csv = report_dir / "daily_forward_equity.csv"
    continuous_equity_csv = report_dir / "continuous_forward_equity.csv"
    pl.DataFrame(daily["equity"]).write_csv(daily_equity_csv)
    pl.DataFrame(continuous["equity"]).write_csv(continuous_equity_csv)
    payload["report"] = report
    payload["report_path"] = str(report_path)
    payload["json_path"] = str(json_path)
    payload["daily_equity_csv"] = str(daily_equity_csv)
    payload["continuous_equity_csv"] = str(continuous_equity_csv)
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
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
    pairs_csv_path = _write_pairs_csv(report_path, result["pairs"])
    return {"result": result, "report": report, "report_path": str(report_path), "pairs_csv_path": pairs_csv_path}


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
