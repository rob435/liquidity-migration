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

# Canonical value is continuous_demo.SNIPER_TRADE_SUFFIX ("-snipe"); mirrored here
# to avoid importing the heavy live-trading module (bybit client, etc.) into the
# reconcile path. A filled demo snipe books a "{base}-snipe" SHORT row, but the
# paper (dry-run) runner never books a snipe fill (there is no venue to fill
# against), so a snipe row has no paper twin BY DESIGN — counting it as demo_only
# pollutes the slippage/divergence diagnostic (sniper-2). Snipe rows are excluded
# from pairing and reported under a separate snipe_demo_only count.
SNIPER_TRADE_SUFFIX = "-snipe"


def _is_snipe_trade(row: dict[str, Any]) -> bool:
    return str(row.get("trade_id") or "").endswith(SNIPER_TRADE_SUFFIX)


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
    paper_all = _clean_trades(paper_trades)
    demo_all = _clean_trades(demo_trades)
    # Snipe rows are demo-only by design (sniper-2): pull them OUT of the pairing
    # population so they cannot inflate demo_only, and report them separately so an
    # operator (or the decision rule) does not read by-design behavior as ledger drift.
    paper = [t for t in paper_all if not _is_snipe_trade(t)]
    demo = [t for t in demo_all if not _is_snipe_trade(t)]
    snipe_paper_only = sum(1 for t in paper_all if _is_snipe_trade(t))
    snipe_demo_only = sum(1 for t in demo_all if _is_snipe_trade(t))
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
        # By-design snipe rows excluded from the pairing population (sniper-2);
        # surfaced separately so they are not mistaken for reconciliation drift.
        "snipe_demo_only": snipe_demo_only,
        "snipe_paper_only": snipe_paper_only,
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
        f"- snipe demo-only (no paper twin by design, excluded from pairing): "
        f"{summary.get('snipe_demo_only', 0)}",
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


def _latest_rebalance_row_by_day(
    rows: list[dict[str, Any]],
) -> dict[int, tuple[tuple[int, int], float, dict[str, Any]]]:
    """Reduce rebalance rows to the LATEST row per (floored) rebalance day.

    Materialized ONCE per audit (reconciliation-4): the latest row per day, keyed by
    ``(ts_ms, original_index)`` so the most-recent same-day cycle wins deterministically.
    """
    latest_by_day: dict[int, tuple[tuple[int, int], float, dict[str, Any]]] = {}
    for idx, row in enumerate(rows):
        day = _int(row.get("rebalance_day_ts"))
        if day <= 0:
            continue
        day = (day // MS_PER_DAY) * MS_PER_DAY
        key = (_int(row.get("ts_ms")), idx)
        prev = latest_by_day.get(day)
        if prev is None or key > prev[0]:
            latest_by_day[day] = (key, _float(row.get("rebalance_raw_return")), row)
    return latest_by_day


def _prior_state_before_day(
    latest_by_day: dict[int, tuple[tuple[int, int], float, dict[str, Any]]],
    sorted_days: list[int],
    *,
    current_day_ts: int,
) -> ContinuousRebalanceScaleState:
    """Prior-state for sizing the cycle at ``current_day_ts`` from a PRECOMPUTED
    per-day reduction — equivalent to the old per-call full-frame rescan but O(prior
    days) instead of O(rows) (reconciliation-4). ``prior_raw_returns`` excludes the
    day being sized; equity/peak come from the most-recent prior day's latest row.
    """
    current_day = (int(current_day_ts) // MS_PER_DAY) * MS_PER_DAY
    prior_days = [d for d in sorted_days if d < current_day]
    if not prior_days:
        return ContinuousRebalanceScaleState(prior_raw_returns=())
    latest = latest_by_day[prior_days[-1]][2]
    equity = _float(latest.get("rebalance_scaled_equity")) or 1.0
    peak = _float(latest.get("rebalance_scaled_peak")) or max(equity, 1.0)
    return ContinuousRebalanceScaleState(
        prior_raw_returns=tuple(latest_by_day[day][1] for day in prior_days),
        prior_scaled_equity=equity if equity > 0.0 else 1.0,
        prior_scaled_peak=max(peak, equity, 1.0),
    )


def _rebalance_prior_state_from_cycles(cycles: pl.DataFrame, *, current_day_ts: int) -> ContinuousRebalanceScaleState:
    if cycles.is_empty():
        return ContinuousRebalanceScaleState(prior_raw_returns=())
    latest_by_day = _latest_rebalance_row_by_day(cycles.to_dicts())
    if not latest_by_day:
        return ContinuousRebalanceScaleState(prior_raw_returns=())
    return _prior_state_before_day(
        latest_by_day, sorted(latest_by_day), current_day_ts=current_day_ts
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
    # Materialize the rebalance rows ONCE and reuse the list everywhere below
    # (reconciliation-4): every downstream pass previously re-ran rebalance.to_dicts(),
    # and the scale loop re-scanned the whole frame per row => O(n^2) over operating
    # days. The per-day reduction is computed once and the prior-state for each row is
    # derived from it in O(prior days).
    rebalance_rows = rebalance.to_dicts()
    latest_by_day = _latest_rebalance_row_by_day(rebalance_rows)
    sorted_days = sorted(latest_by_day)
    scale_mismatches = 0
    for row in rebalance_rows:
        day = _int(row.get("rebalance_day_ts"))
        if day <= 0:
            continue
        expected = compute_continuous_rebalance_scale(
            _prior_state_before_day(latest_by_day, sorted_days, current_day_ts=day),
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

    # Group rebalance rows by floored day in a single pass instead of re-filtering
    # the frame per distinct day.
    rows_by_floored_day: dict[int, list[dict[str, Any]]] = {}
    for r in rebalance_rows:
        floored = (_int(r.get("rebalance_day_ts")) // MS_PER_DAY) * MS_PER_DAY
        if floored <= 0:
            continue
        rows_by_floored_day.setdefault(floored, []).append(r)

    same_day_violations = 0
    for day in sorted(rows_by_floored_day):
        rows = sorted(rows_by_floored_day[day], key=lambda r: _int(r.get("ts_ms")))
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

    cycle_resize_orders = sum(_int(r.get("rebalance_resize_orders")) for r in rebalance_rows)
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
        "days": len(rows_by_floored_day),
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


def _fee_adjusted_return(row: dict[str, Any], *, net_of_cost: bool = False) -> float | None:
    """Per-trade fractional return on the trade's equity, cost-consistent.

    The ``net_return`` field is OVERLOADED by source and must be disambiguated by
    the caller (code-quality-4 / cost-funding-2):

    * LIVE ledger (``net_of_cost=False``, default): ``net_return`` is GROSS-of-cost
      (``gross_trade_return * notional_weight``; see continuous_demo.py DEPLOY NOTE
      ~L1193), so this function subtracts the realised round-trip fees here. Funding
      is NOT tracked per-trade on the live close path, so it is folded in IFF a
      per-trade funding term is present in the row; otherwise the return is
      funding-BLIND (a known conservative residual on a receive-funding short book).
    * BACKTEST ledger (``net_of_cost=True``): ``net_return`` is already NET-of-cost
      (gross + cost_return + funding_return), so fees/funding must NOT be re-applied
      — doing so would double-count and understate returns.

    Passing a backtest (net-of-cost) frame without ``net_of_cost=True`` is the bug
    this guard prevents; the flag makes the source convention explicit at the call.
    """
    has_return = False
    ret = 0.0
    for key in ("net_return", "gross_trade_return"):
        value = row.get(key)
        if value not in (None, ""):
            ret = _float(value)
            # audit2c: gross_trade_return is the RAW per-trade return; weight it by
            # notional/equity so it lands on the same notional-weighted basis as
            # net_return (= gtr * notional_weight) before fees are subtracted below.
            if key == "gross_trade_return":
                notional = _float(row.get("notional_usdt"))
                equity = _float(row.get("equity_usdt"))
                if notional > 0.0 and equity > 0.0:
                    ret *= notional / equity
            has_return = True
            break
    if not has_return:
        entry = _float(row.get("entry_price"))
        exit_price = _float(row.get("exit_price"))
        if entry <= 0.0 or exit_price <= 0.0:
            return None
        # Do NOT default a missing side to "short": that sign-flips a long trade's
        # return. A row with no return field AND no side is undirected — skip it
        # rather than guess (audit-iter1 archive-recon-2). Callers already skip None.
        raw_side = row.get("side") or row.get("trade_side")
        if not raw_side:
            return None
        side = _normalized_side(raw_side)
        ret = (entry - exit_price) / entry if side == "short" else (exit_price - entry) / entry
        notional = _float(row.get("notional_usdt"))
        equity = _float(row.get("equity_usdt"))
        if notional > 0.0 and equity > 0.0:
            ret *= notional / equity
    if net_of_cost:
        # Already net of fees+funding by construction (backtest convention) —
        # re-subtracting fees here would double-count (code-quality-4).
        return ret
    equity = _float(row.get("equity_usdt"))
    if equity > 0.0:
        fees = _float(row.get("entry_fee_usdt")) + _float(row.get("exit_fee_usdt"))
        ret -= fees / equity
        # Fold in realised funding when the ledger carries it (cost-funding-2).
        # The live close path does NOT write a per-trade funding term today, so this
        # is usually a no-op and the return stays funding-blind; once the close path
        # records funding (USDT or fractional) this folds it in without a code change.
        funding_usdt = row.get("funding_usdt")
        if funding_usdt not in (None, ""):
            # +funding_usdt = credit received (improves a short book's return).
            ret += _float(funding_usdt) / equity
        else:
            funding_return = row.get("funding_return")
            if funding_return not in (None, ""):
                ret += _float(funding_return)
    return ret


def _trade_ledger_daily_returns(trades: pl.DataFrame, *, net_of_cost: bool = False) -> dict[int, float]:
    """Sum per-trade returns into a calendar-day series.

    ``net_of_cost`` is forwarded to :func:`_fee_adjusted_return` and MUST match the
    ledger's cost convention (False = live gross-of-fees, True = backtest net-of-cost);
    the callers below pass live ``read_dataset`` ledgers, hence the False default.
    """
    daily: dict[int, float] = {}
    for row in _latest_trade_rows(trades):
        ts = _row_timestamp(row, ("exit_ts_ms", "closed_at_ms"))
        if ts <= 0:
            continue
        ret = _fee_adjusted_return(row, net_of_cost=net_of_cost)
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
    # COMPOUND, peak-relative drawdown (metrics-2 / reconciliation-2): the daily
    # series here is a FRACTIONAL return whose true equity COMPOUNDS — the continuous
    # engine persists rebalance_scaled_equity = prior * (1 + scaled_return) and
    # drawdown = equity/peak - 1 (continuous_rebalance.apply_rebalance_rule /
    # continuous_demo). Summing returns additively (equity += ret) and reporting an
    # ABSOLUTE drawdown (equity - peak) diverges materially from that engine curve and
    # the forward-readiness summary it sits beside, so the standalone continuous MAR /
    # total-return shown to the operator disagreed with the engine for the SAME book.
    # NOTE: this legitimately shifts the reported comparator numbers (additive ->
    # compounded); MAR is STATE.md's named primary forward arbiter, so a fresh read /
    # pre-registration is owed before any decision binds on the new value.
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    curve: list[dict[str, Any]] = []
    worst = 0.0
    for day in days:
        ret = float(returns_by_day.get(day, 0.0))
        equity *= 1.0 + ret
        peak = max(peak, equity)
        dd = (equity - peak) / peak if peak > 0.0 else 0.0
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


def _continuous_beats_daily_mar(continuous: dict[str, Any], daily: dict[str, Any]) -> bool:
    """Does the continuous leg beat the daily leg on MAR?

    audit2: _calendar_metrics returns mar=None EXACTLY for a zero-drawdown curve
    (abs(max_dd) <= 1e-12) — the best-possible drawdown outcome (effectively infinite
    MAR), reachable whenever the continuous book never dips below its running peak. The
    old inline gate folded that None into `continuous_mar > daily_mar` -> False and
    raised a spurious "continuous MAR <= daily MAR" issue on the BEST drawdown case.
    Treat a zero-drawdown continuous book as beating any finite daily MAR whenever its
    return is at least the daily leg's; otherwise the strict finite comparison stands.
    """
    continuous_mar = continuous["mar"]
    daily_mar = daily["mar"]
    if continuous_mar is None and abs(continuous["max_drawdown"]) <= 1e-12:
        return bool(continuous["total_return"] >= daily["total_return"])
    return bool(continuous_mar is not None and daily_mar is not None and continuous_mar > daily_mar)


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
        f"mode: `{summary.get('mode', 'comparison')}`",
        "",
        "This compares realized forward ledger performance over the same calendar window.",
        "The daily leg is OPTIONAL (2026-06-11 operator decision): in `continuous_only`",
        "mode the binding read is the continuous ledger over its OWN window (below).",
        "",
        "## Continuous (own full window — the standalone read)",
        "",
        f"- days: `{summary.get('continuous_full_days', 0)}`",
        f"- total_return: `{summary.get('continuous_full_total_return', 0.0):.6f}`",
        f"- mar: `{_format_mar(summary.get('continuous_full_mar'))}`",
        f"- max_drawdown: `{summary.get('continuous_full_max_drawdown', 0.0):.6f}`",
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
    if payload.get("notes"):
        lines.extend(["", "## Notes (non-blocking)", ""])
        for note in payload["notes"]:
            lines.append(f"- {note}")
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

    # 2026-06-11 operator decision: the continuous forward report must NOT depend on the
    # short sleeve. The daily leg is OPTIONAL — daily-side gaps are informational notes,
    # never blocking issues; the comparison block only binds once both legs matured.
    issues: list[str] = []
    notes: list[str] = []
    if not daily_returns:
        notes.append(
            f"daily return series is empty from {daily_root_p}/{daily_cycles_dataset}+{daily_trades_dataset} "
            "(short sleeve ERASED 2026-06-11 — continuous-only mode)"
        )
    if not continuous_returns:
        issues.append(f"continuous return series is empty from {continuous_root_p}/{continuous_source}")

    start = max(min(daily_returns) if daily_returns else 0, min(continuous_returns) if continuous_returns else 0)
    end = min(max(daily_returns) if daily_returns else 0, max(continuous_returns) if continuous_returns else 0)
    common_days = ((end - start) // MS_PER_DAY + 1) if start > 0 and end >= start else 0
    if common_days < int(min_common_days):
        notes.append(f"common forward window {common_days}d below min_common_days {int(min_common_days)}")

    daily = _calendar_metrics(daily_returns, start_day=start, end_day=end)
    continuous = _calendar_metrics(continuous_returns, start_day=start, end_day=end)
    # standalone continuous view over its OWN window — the primary read when the
    # daily leg is off/immature
    continuous_full = _calendar_metrics(
        continuous_returns,
        start_day=min(continuous_returns) if continuous_returns else 0,
        end_day=max(continuous_returns) if continuous_returns else 0,
    )
    continuous_beats_return = bool(continuous["total_return"] > daily["total_return"])
    daily_mar = daily["mar"]
    continuous_mar = continuous["mar"]
    continuous_beats_mar = _continuous_beats_daily_mar(continuous, daily)
    performance_window_ready = common_days >= int(min_common_days) and bool(daily_returns) and bool(continuous_returns)
    if performance_window_ready and not continuous_beats_return:
        issues.append(
            f"continuous return {continuous['total_return']:.6f} <= daily return {daily['total_return']:.6f}"
        )
    if performance_window_ready and not continuous_beats_mar:
        issues.append(f"continuous MAR {_format_mar(continuous_mar)} <= daily MAR {_format_mar(daily_mar)}")

    summary = {
        "mode": "comparison" if performance_window_ready else "continuous_only",
        "continuous_full_days": len(continuous_returns),
        "continuous_full_total_return": continuous_full["total_return"],
        "continuous_full_mar": continuous_full["mar"],
        "continuous_full_max_drawdown": continuous_full["max_drawdown"],
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
        "continuous_full": {k: v for k, v in continuous_full.items() if k != "equity"},
        "issues": issues,
        "notes": notes,
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


