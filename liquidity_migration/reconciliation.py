"""Reconcile the paper (dry-run) ledger against the demo ledger.

The paper runner records idealized fills at the signal price; the demo runner
records actual Bybit demo fills. Pairing the two ledgers' trades by symbol,
side and entry time, then diffing their fill prices, measures execution
slippage — the cost the demo execution path pays that the idealized paper path
does not. Unpaired trades on either side are fill-rate divergence.
"""

from __future__ import annotations

import datetime as dt
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

# --- v2-forward reconcile constants. The command-line reconcile front door passes
# these explicitly so the baseline gate is v2-forward only, while the library
# functions default to unfiltered behavior for tests and ad-hoc diagnostics. ---
CONTINUOUS_V2_FORWARD_START = "2026-06-18T19:54:00Z"
CONTINUOUS_V2_FORWARD_START_MS = int(
    dt.datetime(2026, 6, 18, 19, 54, tzinfo=dt.timezone.utc).timestamp() * 1000
)
CONTINUOUS_V2_DEMO_STRATEGY_ID = "continuous_fade_v2"
CONTINUOUS_V2_PAPER_STRATEGY_ID = "continuous_fade_v2_paper"
CONTINUOUS_V2_STRATEGY_IDS = (CONTINUOUS_V2_DEMO_STRATEGY_ID, CONTINUOUS_V2_PAPER_STRATEGY_ID)
CONTINUOUS_V2_PROFILE = "continuous_ensemble_v2"


def _rebalance_telemetry_required(strategy_profile: str | None) -> bool:
    """Whether forward-readiness should require rebalance telemetry columns.

    The deployed v2 continuous profile intentionally keeps daily vol-target
    rebalance disabled. Its cycle rows therefore do not carry
    ``rebalance_day_ts`` telemetry; requiring those columns makes the live
    execution reconcile fail for the intended runtime config.
    """
    return str(strategy_profile or "") != CONTINUOUS_V2_PROFILE


def _is_snipe_trade(row: dict[str, Any]) -> bool:
    return str(row.get("trade_id") or "").endswith(SNIPER_TRADE_SUFFIX)


def _pair_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Lifecycle pairing grain for ensemble books.

    Component is load-bearing: three continuous legs share symbol, side, and
    signal timestamp, so omitting it silently permutes p3/p4p3/p4p5 matches.
    """
    return (str(row.get("symbol") or ""), str(row.get("side") or ""), str(row.get("component") or ""))


def _canonical_exit_reason(value: Any) -> str:
    reason = str(value or "").strip().lower()
    return {
        "time_stop": "max_hold",
        "max_hold": "max_hold",
        "tp": "take_profit",
        "take_profit": "take_profit",
        "stop": "stop_loss",
        "stop_loss": "stop_loss",
    }.get(reason, reason)


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


def _utc_ms(value: int | None) -> str:
    if value is None:
        return ""
    return dt.datetime.fromtimestamp(int(value) / 1000, dt.timezone.utc).isoformat()


def _id_label(allowed: str | tuple[str, ...] | None) -> str:
    """Render a strategy-id filter (str / tuple / None) for a report banner."""
    if allowed is None:
        return ""
    return ",".join(str(v) for v in allowed) if isinstance(allowed, tuple) else str(allowed)


def _filter_min_ts(df: pl.DataFrame, start_ts_ms: int | None, columns: tuple[str, ...]) -> pl.DataFrame:
    if start_ts_ms is None or df.is_empty():
        return df
    exprs = [pl.col(col).cast(pl.Int64, strict=False) for col in columns if col in df.columns]
    if not exprs:
        return df
    return df.filter(pl.coalesce(exprs).fill_null(0) >= int(start_ts_ms))


def _filter_value(df: pl.DataFrame, column: str, allowed: str | tuple[str, ...] | None) -> pl.DataFrame:
    if allowed is None or df.is_empty() or column not in df.columns:
        return df
    values = (allowed,) if isinstance(allowed, str) else allowed
    values = tuple(str(v) for v in values if str(v))
    if not values:
        return df
    return df.filter(pl.col(column).cast(pl.Utf8).is_in(values))


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
                "component": str(row.get("component") or ""),
                "signal_ts_ms": _int(row.get("signal_ts_ms")),
                "entry_ts_ms": _int(row.get("entry_ts_ms")),
                "entry_exec_time_ms": _int(row.get("entry_exec_time_ms")),
                "entry_price": entry_price,
                "entry_fee_usdt": _float(row.get("entry_fee_usdt")),
                "entry_fee_recorded": row.get("entry_fee_usdt") is not None,
                "qty": qty,
                "status": str(row.get("status") or ""),
                "exit_price": _float(row.get("exit_price")),
                "exit_ts_ms": _int(row.get("exit_ts_ms")),
                "exit_exec_time_ms": _int(row.get("exit_exec_time_ms")),
                "exit_reason": str(row.get("exit_reason") or ""),
                "exit_fee_usdt": _float(row.get("exit_fee_usdt")),
                "exit_fee_recorded": row.get("exit_fee_usdt") is not None,
                "notional_usdt": _float(row.get("notional_usdt")),
                "equity_usdt": _float(row.get("equity_usdt")),
                "net_return": _float(row.get("net_return")),
                "venue_closed_pnl_allocated_usdt": _float(
                    row.get("venue_closed_pnl_allocated_usdt")
                ),
                "venue_closed_pnl_recorded": row.get("venue_closed_pnl_allocated_usdt") is not None,
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
    # Snipe rows had no paper twin. Keep base execution pairing separate, but
    # report the add-on exposure and PnL explicitly; count any still-open snipe
    # as a hard lifecycle fault now that the arm is retired.
    paper = [t for t in paper_all if not _is_snipe_trade(t)]
    demo = [t for t in demo_all if not _is_snipe_trade(t)]
    snipe_paper_only = sum(1 for t in paper_all if _is_snipe_trade(t))
    snipe_demo_only = sum(1 for t in demo_all if _is_snipe_trade(t))
    demo_snipes = [t for t in demo_all if _is_snipe_trade(t)]
    paper_snipes = [t for t in paper_all if _is_snipe_trade(t)]
    tolerance = max(int(entry_tolerance_ms), 0)
    signal_tolerance = max(int(signal_tolerance_ms), 0)

    paper_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for trade in paper:
        paper_by_key.setdefault(_pair_key(trade), []).append(trade)
    for bucket in paper_by_key.values():
        bucket.sort(key=lambda item: item["entry_ts_ms"])

    # Index paper trades by trade_id within each bucket so trade-id pairing
    # and gap pairing both use the SAME (key, bucket_idx) addressing scheme.
    paper_tid_in_bucket: dict[tuple[str, str, str], dict[str, int]] = {}
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
    tid_matched_paper: dict[tuple[str, str, str], set[int]] = {}
    for demo_idx, demo_trade in enumerate(demo):
        tid = str(demo_trade.get("trade_id") or "")
        if not tid:
            continue
        key = _pair_key(demo_trade)
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
    signal_matched_paper: dict[tuple[str, str, str], set[int]] = {
        k: set(v) for k, v in tid_matched_paper.items()
    }
    signal_candidates: list[tuple[int, int, int]] = []
    for demo_idx, demo_trade in enumerate(demo):
        if demo_idx in tid_matched_demo:
            continue
        demo_signal = demo_trade.get("signal_ts_ms", 0)
        if not demo_signal:
            continue
        key = _pair_key(demo_trade)
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
        key = _pair_key(demo[demo_idx])
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
        key = _pair_key(demo_trade)
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
    used_paper: dict[tuple[str, str, str], set[int]] = {}
    for _gap, demo_idx, paper_idx in candidates:
        if demo_idx in used_demo:
            continue
        demo_trade = demo[demo_idx]
        key = _pair_key(demo_trade)
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
                exit_reason_match = _canonical_exit_reason(paper_reason) == _canonical_exit_reason(demo_reason)
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
                    "component": demo_trade["component"],
                    "paper_trade_id": paper_trade["trade_id"],
                    "demo_trade_id": demo_trade["trade_id"],
                    "paper_status": paper_trade["status"],
                    "demo_status": demo_trade["status"],
                    "status_match": paper_trade["status"] == demo_trade["status"],
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
    status_divergent = [pair for pair in pairs if not pair["status_match"]]
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
        "snipe_demo_open": sum(1 for trade in demo_snipes if trade["status"] == "open"),
        "snipe_paper_open": sum(1 for trade in paper_snipes if trade["status"] == "open"),
        # Keep current exposure, historical sizing, local price-PnL estimates,
        # and venue-authoritative allocations distinct.  The legacy fields
        # called historical notional "exposure" and ledger price return
        # "realized PnL", which was materially false for closed snipes and for
        # rows whose shared-symbol exit attribution was incomplete.
        "snipe_demo_open_notional_usdt": sum(
            trade["notional_usdt"] for trade in demo_snipes if trade["status"] == "open"
        ),
        "snipe_demo_historical_entry_notional_usdt": sum(
            trade["notional_usdt"] for trade in demo_snipes
        ),
        "snipe_demo_gross_price_pnl_usdt": sum(
            trade["net_return"] * trade["equity_usdt"] for trade in demo_snipes
        ),
        "snipe_demo_net_return_total": sum(trade["net_return"] for trade in demo_snipes),
        "snipe_demo_trading_fees_usdt": sum(
            trade["entry_fee_usdt"] + trade["exit_fee_usdt"] for trade in demo_snipes
        ),
        "snipe_demo_fee_rows_recorded": sum(
            1
            for trade in demo_snipes
            if trade["entry_fee_recorded"] or trade["exit_fee_recorded"]
        ),
        "snipe_demo_venue_closed_pnl_allocated_usdt": sum(
            trade["venue_closed_pnl_allocated_usdt"]
            for trade in demo_snipes
            if trade["venue_closed_pnl_recorded"]
        ),
        "snipe_demo_venue_closed_pnl_rows_recorded": sum(
            1 for trade in demo_snipes if trade["venue_closed_pnl_recorded"]
        ),
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
        "status_divergent": len(status_divergent),
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
        f"- snipe demo-only (unshadowed add-on, excluded from base pairing): "
        f"{summary.get('snipe_demo_only', 0)}",
        f"- snipe open on demo: {summary.get('snipe_demo_open', 0)}",
        f"- snipe open exposure: ${summary.get('snipe_demo_open_notional_usdt', 0.0):,.2f}",
        f"- snipe historical entry notional: "
        f"${summary.get('snipe_demo_historical_entry_notional_usdt', 0.0):,.2f}",
        f"- snipe ledger-estimated gross price PnL/MTM: "
        f"${summary.get('snipe_demo_gross_price_pnl_usdt', 0.0):,.2f} "
        f"({summary.get('snipe_demo_net_return_total', 0.0):.4%} equity-return sum; "
        "not venue authority)",
        f"- snipe recorded trading fees: "
        f"${summary.get('snipe_demo_trading_fees_usdt', 0.0):,.2f} "
        f"across {summary.get('snipe_demo_fee_rows_recorded', 0)} row(s)",
        f"- snipe venue Closed-PnL allocation: "
        f"${summary.get('snipe_demo_venue_closed_pnl_allocated_usdt', 0.0):,.2f} "
        f"across {summary.get('snipe_demo_venue_closed_pnl_rows_recorded', 0)} row(s)",
        "- snipe funding: unavailable in the local trade ledger; use venue transaction records",
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
        f"- status-divergent pairs: {summary.get('status_divergent', 0)}",
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
            "| symbol | component | side | paper entry | demo entry | entry slip bps | paper exit | demo exit | "
            "exit slip bps | exit gap (s) | paper reason | demo reason | paper ret % | demo ret % | fee Δ USDT |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for pair in result["pairs"]:
            exit_bps = pair["exit_slippage_bps"]
            paper_ret = pair["paper_return_pct"]
            demo_ret = pair["demo_return_pct"]
            exit_gap = pair["exit_gap_ms"]
            fee_gap = pair["fee_gap_usdt"]
            lines.append(
                f"| {pair['symbol']} | {pair.get('component') or '-'} | {pair['side']} | "
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


def paper_demo_reconciliation_failures(summary: dict[str, Any]) -> list[str]:
    """Hard paper/demo drift gates.

    Sample-size warnings, slippage, and fee gaps are evidence to inspect, but
    they are not lifecycle-consistency failures. Unpaired trades or divergent
    lifecycle/exit semantics are.
    """
    failures: list[str] = []
    if int(summary.get("paper_only", 0) or 0) > 0:
        failures.append(f"paper_only={summary.get('paper_only')}")
    if int(summary.get("demo_only", 0) or 0) > 0:
        failures.append(f"demo_only={summary.get('demo_only')}")
    if int(summary.get("status_divergent", 0) or 0) > 0:
        failures.append(f"status_divergent={summary.get('status_divergent')}")
    if int(summary.get("exit_reason_divergent", 0) or 0) > 0:
        failures.append(f"exit_reason_divergent={summary.get('exit_reason_divergent')}")
    if int(summary.get("snipe_demo_open", 0) or 0) > 0:
        failures.append(f"snipe_demo_open={summary.get('snipe_demo_open')}")
    return failures


def paper_demo_reconciliation_ok(summary: dict[str, Any]) -> bool:
    return not paper_demo_reconciliation_failures(summary)


def run_long_paper_demo_reconciliation(
    paper_root: str | Path,
    demo_root: str | Path,
    *,
    entry_tolerance_ms: int = DEFAULT_ENTRY_TOLERANCE_MS,
    output_dir: str | Path | None = None,
    min_pairs_warning: int = 30,
) -> dict[str, Any]:
    """B.4 — reconcile the long sleeve's paper/demo ledger datasets
    (``long_native_paper_trades`` vs
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
    start_ts_ms: int | None = None,
    paper_strategy_id: str | tuple[str, ...] | None = None,
    demo_strategy_id: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Continuous-fade sleeve paper/demo execution-slippage
    reconciler. Same pairing as the long reconciler but reads the
    continuous sleeve's own ledger datasets (``continuous_fade_paper_trades``
    vs ``continuous_fade_demo_trades``). Like the long reconciler it emits a
    ``sample_warning`` when fewer than ``min_pairs_warning`` pairs were matched
    (continuous is sub-hourly so its pair count grows fast, but a fresh sleeve
    still warrants the caveat). The active profile enters from confirmed-bar
    +1h membership; small paper/demo entry-time skew can still come from order
    confirmation and fill timing.
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
        start_ts_ms=start_ts_ms,
        paper_strategy_id=paper_strategy_id,
        demo_strategy_id=demo_strategy_id,
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
    start_ts_ms: int | None = None,
    strategy_profile: str | None = None,
    cycle_strategy_id: str | tuple[str, ...] | None = None,
    order_strategy_id: str | tuple[str, ...] | None = None,
    require_rebalance_telemetry: bool = True,
) -> dict[str, Any]:
    """Audit daily-rebalance cycle telemetry for causal scale and resize discipline."""
    rule = rule or ContinuousRebalanceRule()
    issues: list[dict[str, Any]] = []
    raw_cycles = cycles.height
    raw_orders = orders.height
    cycles = _filter_min_ts(cycles, start_ts_ms, ("ts_ms",))
    cycles = _filter_value(cycles, "strategy_profile", strategy_profile)
    cycles = _filter_value(cycles, "strategy_id", cycle_strategy_id)
    orders = _filter_min_ts(orders, start_ts_ms, ("signal_ts_ms", "ts_ms", "updated_at_ms", "exec_time_ms"))
    orders = _filter_value(orders, "strategy_id", order_strategy_id)
    filters = {
        "start_ts_ms": start_ts_ms,
        "start_utc": _utc_ms(start_ts_ms),
        "strategy_profile": strategy_profile or "",
        "cycle_strategy_id": cycle_strategy_id or "",
        "order_strategy_id": order_strategy_id or "",
        "cycles_before_filter": raw_cycles,
        "orders_before_filter": raw_orders,
    }

    def _resize_order_count(frame: pl.DataFrame) -> int:
        if frame.is_empty() or "resize_reason" not in frame.columns:
            return 0
        return frame.filter(pl.col("resize_reason").is_not_null()).height

    def _disabled_rebalance_payload(kind: str | None = None) -> dict[str, Any]:
        order_resize_orders = _resize_order_count(orders)
        disabled_issues: list[dict[str, Any]] = []
        if kind is not None:
            disabled_issues.append({"kind": kind, "message": "daily rebalance telemetry absent"})
        if order_resize_orders > 0:
            disabled_issues.append(
                {
                    "kind": "resize_orders_with_rebalance_disabled",
                    "order_resize_orders": order_resize_orders,
                }
            )
        return {
            "ok": not disabled_issues,
            "summary": {
                "cycles": cycles.height,
                "rebalance_cycles": 0,
                "days": 0,
                "scale_mismatches": 0,
                "same_day_resize_violations": 0,
                "cycle_resize_orders": 0,
                "order_resize_orders": order_resize_orders,
                "resize_order_count_mismatch": False,
                "rebalance_telemetry_required": False,
                **filters,
            },
            "issues": disabled_issues,
        }

    if cycles.is_empty():
        return {
            "ok": False,
            "summary": {
                "cycles": 0,
                "rebalance_cycles": 0,
                "days": 0,
                "scale_mismatches": 0,
                "same_day_resize_violations": 0,
                "cycle_resize_orders": 0,
                "order_resize_orders": _resize_order_count(orders),
                "resize_order_count_mismatch": False,
                "rebalance_telemetry_required": bool(require_rebalance_telemetry),
                **filters,
            },
            "issues": [{"kind": "empty_cycles", "message": "no cycle rows"}],
        }

    if "rebalance_day_ts" not in cycles.columns:
        if not require_rebalance_telemetry:
            return _disabled_rebalance_payload()
        return {
            "ok": False,
            "summary": {
                "cycles": cycles.height,
                "rebalance_cycles": 0,
                "days": 0,
                "scale_mismatches": 0,
                "same_day_resize_violations": 0,
                "cycle_resize_orders": 0,
                "order_resize_orders": _resize_order_count(orders),
                "resize_order_count_mismatch": False,
                "rebalance_telemetry_required": True,
                **filters,
            },
            "issues": [{"kind": "missing_rebalance_columns", "message": "rebalance_day_ts column missing"}],
        }

    rebalance = cycles.filter(pl.col("rebalance_day_ts").is_not_null()).sort("ts_ms")
    if rebalance.is_empty() and not require_rebalance_telemetry:
        return _disabled_rebalance_payload()
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
        "rebalance_telemetry_required": bool(require_rebalance_telemetry),
        **filters,
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
        f"rebalance_telemetry_required: `{summary.get('rebalance_telemetry_required', True)}`",
        f"start_ts_ms: `{summary.get('start_ts_ms') or ''}`",
        f"start_utc: `{summary.get('start_utc') or ''}`",
        f"strategy_profile: `{summary.get('strategy_profile') or ''}`",
        f"cycle_strategy_id: `{summary.get('cycle_strategy_id') or ''}`",
        f"order_strategy_id: `{summary.get('order_strategy_id') or ''}`",
        f"cycles_before_filter: `{summary.get('cycles_before_filter', summary['cycles'])}`",
        f"orders_before_filter: `{summary.get('orders_before_filter', 0)}`",
        "",
        "This is a ledger-consistency audit, not promotion or OOS evidence.",
    ]
    issues = payload.get("issues") or []
    if issues:
        lines.extend(["", "## Issues"])
        for issue in issues[:50]:
            lines.append(f"- `{issue.get('kind')}`: {issue}")
    return "\n".join(lines) + "\n"


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _nonempty(value: Any) -> bool:
    return bool(str(value or "").strip())


def _filtered_jsonl_events(
    path: Path,
    *,
    start_ts_ms: int | None = None,
    strategy_id: str | tuple[str, ...] | None = None,
    invalid_lines: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    allowed = None
    if strategy_id is not None:
        allowed = {strategy_id} if isinstance(strategy_id, str) else set(strategy_id)
        allowed = {str(value) for value in allowed if str(value)}
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                if invalid_lines is not None:
                    invalid_lines.append(f"{path}:{line_no}")
                continue
            ts_ms = _int(row.get("ts_ms"))
            if start_ts_ms is not None and ts_ms > 0 and ts_ms < int(start_ts_ms):
                continue
            event_strategy = str(row.get("strategy_id") or "")
            if allowed is not None and event_strategy and event_strategy not in allowed:
                continue
            out.append(row)
    return out


def _status_counts(rows: list[dict[str, Any]], column: str = "status") -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get(column) or "").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _compact_counts(counts: dict[str, int]) -> str:
    return ",".join(f"{key}:{value}" for key, value in sorted(counts.items()) if value)


def _latency_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "max": None}
    return {
        "count": len(values),
        "mean": float(mean(values)),
        "median": float(median(values)),
        "max": float(max(values)),
    }


def _sum_rows(rows: list[dict[str, Any]], column: str) -> float:
    return sum(_float(row.get(column)) for row in rows)


def _count_rows_with_nonempty(rows: list[dict[str, Any]], column: str) -> int:
    return sum(1 for row in rows if _nonempty(row.get(column)))


def _is_reduce_only(row: dict[str, Any]) -> bool:
    return _boolish(row.get("reduce_only"))


def _is_post_only_order(row: dict[str, Any]) -> bool:
    tif = str(row.get("time_in_force") or row.get("timeInForce") or "").strip().lower()
    reason = str(row.get("reason") or "").strip().lower()
    if tif == "postonly":
        return True
    return reason == "sniper_wick_add"


def _latencies_from_rows(
    rows: list[dict[str, Any]],
    *,
    start_column: str,
    end_column: str,
) -> list[float]:
    values: list[float] = []
    for row in rows:
        start = _int(row.get(start_column))
        end = _int(row.get(end_column))
        if start > 0 and end >= start:
            values.append(float(end - start))
    return values


def _continuous_operational_issues(
    summary: dict[str, Any], *, role: str = "demo"
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if summary["cycles"] <= 0:
        issues.append({"kind": "empty_cycles", "message": "no cycle rows"})
    if summary["order_failure_count"] > 0:
        issues.append({"kind": "order_failures", "count": summary["order_failure_count"]})
    if summary["submitted_unconfirmed_orders"] > 0:
        issues.append({"kind": "submitted_unconfirmed_orders", "count": summary["submitted_unconfirmed_orders"]})
    if summary["risk_health_blocked_cycles"] > 0 or summary["risk_health_blocked_events"] > 0:
        issues.append(
            {
                "kind": "entry_risk_health_blocks",
                "cycles": summary["risk_health_blocked_cycles"],
                "events": summary["risk_health_blocked_events"],
            }
        )
    # A paper shadow deliberately owns no exchange position. Comparing its
    # hypothetical open ledger to the shared demo account is structurally
    # expected drift; venue parity is audited only for the submitting demo role.
    if role != "paper" and summary["ledger_mismatch_cycles"] > 0:
        issues.append({"kind": "ledger_mismatch_cycles", "count": summary["ledger_mismatch_cycles"]})
    if summary["unprotected_position_seconds_max"] and summary["unprotected_position_seconds_max"] > 0:
        issues.append(
            {
                "kind": "unprotected_position_time",
                "max_seconds": summary["unprotected_position_seconds_max"],
            }
        )
    if summary["account_drawdown_kill_switch_cycles"] > 0:
        issues.append(
            {
                "kind": "account_drawdown_kill_switch",
                "cycles": summary["account_drawdown_kill_switch_cycles"],
            }
        )
    if summary["lifecycle_transition_rejected_events"] > 0:
        issues.append(
            {
                "kind": "lifecycle_transition_rejected",
                "count": summary["lifecycle_transition_rejected_events"],
            }
        )
    if summary["stop_repair_error_count"] > 0:
        issues.append({"kind": "stop_repair_errors", "count": summary["stop_repair_error_count"]})
    if summary["invalid_jsonl_lines"] > 0:
        issues.append(
            {
                "kind": "invalid_jsonl_telemetry",
                "count": summary["invalid_jsonl_lines"],
                "sources": summary["invalid_jsonl_sources"],
            }
        )
    return issues


def audit_continuous_operational_metrics(
    cycles: pl.DataFrame,
    orders: pl.DataFrame,
    trades: pl.DataFrame,
    *,
    data_root: str | Path | None = None,
    start_ts_ms: int | None = None,
    strategy_profile: str | None = None,
    strategy_id: str | tuple[str, ...] | None = None,
    role: str = "demo",
) -> dict[str, Any]:
    """Audit Phase-13 live/paper telemetry that can be derived from ledgers.

    Missing orders/trades are reported as unavailable metrics, not as a failure;
    the forward-readiness sample gate is responsible for enforcing trade count.
    Concrete safety anomalies that are present in the ledgers do fail the audit.
    """
    raw_cycles = cycles.height
    raw_orders = orders.height
    raw_trades = trades.height
    cycles = _filter_min_ts(cycles, start_ts_ms, ("ts_ms",))
    cycles = _filter_value(cycles, "strategy_profile", strategy_profile)
    cycles = _filter_value(cycles, "strategy_id", strategy_id)
    orders = _filter_min_ts(orders, start_ts_ms, ("signal_ts_ms", "ts_ms", "updated_at_ms", "exec_time_ms"))
    orders = _filter_value(orders, "strategy_id", strategy_id)
    trades = _filter_min_ts(trades, start_ts_ms, ("signal_ts_ms", "entry_ts_ms", "ts_ms"))
    trades = _filter_value(trades, "strategy_id", strategy_id)

    cycle_rows = cycles.to_dicts() if not cycles.is_empty() else []
    order_rows = orders.to_dicts() if not orders.is_empty() else []
    trade_rows = trades.to_dicts() if not trades.is_empty() else []
    risk_events: list[dict[str, Any]] = []
    lifecycle_events: list[dict[str, Any]] = []
    stop_repair_events: list[dict[str, Any]] = []
    invalid_jsonl_lines: list[str] = []
    if data_root is not None:
        root = Path(data_root).expanduser()
        risk_events = _filtered_jsonl_events(
            root / "continuous_risk_events.jsonl",
            start_ts_ms=start_ts_ms,
            strategy_id=strategy_id,
            invalid_lines=invalid_jsonl_lines,
        )
        lifecycle_events = _filtered_jsonl_events(
            root / "continuous_lifecycle_events.jsonl",
            start_ts_ms=start_ts_ms,
            strategy_id=strategy_id,
            invalid_lines=invalid_jsonl_lines,
        )
        for stop_path in (
            root / "reports" / "event-risk-ws" / "stop_audit_events.jsonl",
            root / "stop_audit_events.jsonl",
        ):
            stop_repair_events.extend(
                _filtered_jsonl_events(
                    stop_path,
                    start_ts_ms=start_ts_ms,
                    invalid_lines=invalid_jsonl_lines,
                )
            )

    signal_latency = []
    for row in cycle_rows:
        ts_ms = _int(row.get("ts_ms"))
        signal_ts_ms = _int(row.get("entry_signal_ts_ms") or row.get("signal_ts_ms"))
        if ts_ms > 0 and signal_ts_ms > 0 and ts_ms >= signal_ts_ms:
            signal_latency.append(float(ts_ms - signal_ts_ms))

    entry_order_rows = [row for row in order_rows if not _is_reduce_only(row)]
    exit_order_rows = [row for row in order_rows if _is_reduce_only(row)]
    considered_orders = [row for row in order_rows if str(row.get("submit_mode") or "").lower() != "preflight"]
    filled_orders = [
        row
        for row in considered_orders
        if str(row.get("status") or "").strip().lower() in {"filled", "partial", "closed"}
    ]
    submitted_unconfirmed = [
        row for row in considered_orders if str(row.get("status") or "").strip().lower() == "submitted_unconfirmed"
    ]
    order_failures = [
        row
        for row in considered_orders
        if str(row.get("submit_mode") or "").strip().lower() == "error"
        or str(row.get("status") or "").strip().lower() in {"failed", "rejected"}
        or _nonempty(row.get("error"))
    ]
    post_only_orders = [row for row in considered_orders if _is_post_only_order(row)]
    post_only_cancelled = [
        row
        for row in post_only_orders
        if str(row.get("status") or "").strip().lower() in {"canceled", "cancelled", "expired"}
    ]
    order_latency = _latencies_from_rows(considered_orders, start_column="signal_ts_ms", end_column="ts_ms")
    fill_latency = _latencies_from_rows(considered_orders, start_column="ts_ms", end_column="exec_time_ms")
    stop_placement_latency = []
    for row in trade_rows:
        state = str(row.get("lifecycle_state") or "").strip().upper()
        protected_ts = _int(row.get("lifecycle_state_updated_at_ms"))
        entry_ts = _int(row.get("entry_exec_time_ms") or row.get("entry_ts_ms") or row.get("opened_at_ms"))
        if state == "PROTECTED" and entry_ts > 0 and protected_ts >= entry_ts:
            stop_placement_latency.append(float(protected_ts - entry_ts))

    maker_taker_counts: dict[str, int] = {}
    for row in trade_rows:
        for column in ("maker_taker", "entry_maker_taker", "exit_maker_taker", "liquidity"):
            value = str(row.get(column) or "").strip().lower()
            if value in {"maker", "taker"}:
                maker_taker_counts[value] = maker_taker_counts.get(value, 0) + 1

    risk_health_blocked_events = sum(
        1 for row in risk_events if str(row.get("event") or "") == "entry_risk_health_blocked"
    )
    lifecycle_transition_rejected_events = sum(
        1 for row in risk_events if str(row.get("event") or "") == "lifecycle_transition_rejected"
    )
    stop_repair_error_count = sum(1 for row in stop_repair_events if _nonempty(row.get("error")))
    unprotected_values = [
        _float(row.get("entry_risk_health_unprotected_max_age_seconds"))
        for row in cycle_rows
        if "entry_risk_health_unprotected_max_age_seconds" in row
    ]
    funding_total = 0.0
    for column in ("funding_pnl", "funding_pnl_usdt", "funding_usdt", "funding_fee_usdt"):
        funding_total += _sum_rows(trade_rows, column)
    trade_fee_columns_present = any(
        "entry_fee_usdt" in row or "exit_fee_usdt" in row for row in trade_rows
    )
    trade_fee_total = _sum_rows(trade_rows, "entry_fee_usdt") + _sum_rows(trade_rows, "exit_fee_usdt")
    order_fee_total = _sum_rows(order_rows, "fee_usdt")
    fees_usdt_total = trade_fee_total if trade_fee_columns_present else order_fee_total

    unavailable: list[str] = []
    if not order_rows:
        unavailable.extend(
            [
                "order_latency",
                "fill_latency",
                "fill_rate",
                "PostOnly_cancel_rate",
                "WS_fill_confirmation_time",
            ]
        )
    if not trade_rows:
        unavailable.extend(["fees", "funding", "maker_taker_split", "stop_placement_latency"])
    if not stop_repair_events:
        unavailable.append("stop_repair_count")
    if not signal_latency:
        unavailable.append("signal_latency")
    unavailable = sorted(set(unavailable))

    order_status_counts = _status_counts(considered_orders)
    lifecycle_status_counts = _status_counts(lifecycle_events, column="lifecycle_state")
    fill_rate = (len(filled_orders) / len(considered_orders)) if considered_orders else None
    post_only_cancel_rate = (len(post_only_cancelled) / len(post_only_orders)) if post_only_orders else None
    signal_latency_summary = _latency_summary(signal_latency)
    order_latency_summary = _latency_summary(order_latency)
    fill_latency_summary = _latency_summary(fill_latency)
    stop_placement_latency_summary = _latency_summary(stop_placement_latency)
    unprotected_max = max(unprotected_values) if unprotected_values else 0.0
    if role not in {"demo", "paper"}:
        raise ValueError("role must be 'demo' or 'paper'")
    summary = {
        "role": role,
        "cycles": len(cycle_rows),
        "orders": len(order_rows),
        "trades": len(trade_rows),
        "cycles_before_filter": raw_cycles,
        "orders_before_filter": raw_orders,
        "trades_before_filter": raw_trades,
        "start_ts_ms": start_ts_ms,
        "start_utc": _utc_ms(start_ts_ms),
        "strategy_profile": strategy_profile or "",
        "strategy_id": _id_label(strategy_id),
        "candidate_cycles": sum(1 for row in cycle_rows if _int(row.get("candidates")) > 0),
        "candidates_total": _sum_rows(cycle_rows, "candidates"),
        "entries_total": _sum_rows(cycle_rows, "entries"),
        "exits_total": _sum_rows(cycle_rows, "exits"),
        "entry_errors_total": _sum_rows(cycle_rows, "entry_errors"),
        "exit_errors_total": _sum_rows(cycle_rows, "exit_errors"),
        "sniper_errors_total": _sum_rows(cycle_rows, "sniper_errors"),
        "resize_errors_total": _sum_rows(cycle_rows, "resize_errors"),
        "wallet_error_cycles": _count_rows_with_nonempty(cycle_rows, "wallet_error"),
        "risk_health_blocked_cycles": sum(
            1
            for row in cycle_rows
            if row.get("entry_risk_health_ok") is False or _nonempty(row.get("entry_risk_health_reasons"))
        ),
        "risk_health_blocked_events": risk_health_blocked_events,
        "ledger_mismatch_cycles": sum(
            1
            for row in cycle_rows
            if _nonempty(row.get("entry_risk_health_ledger_missing_positions"))
            or _nonempty(row.get("entry_risk_health_exchange_only_positions"))
        ),
        "unprotected_position_cycles": _count_rows_with_nonempty(
            cycle_rows,
            "entry_risk_health_unprotected_positions",
        ),
        "unprotected_position_seconds_max": unprotected_max,
        "portfolio_heat_clamped_cycles": sum(1 for row in cycle_rows if _boolish(row.get("portfolio_heat_clamped"))),
        "account_drawdown_kill_switch_cycles": sum(
            1 for row in cycle_rows if _boolish(row.get("entry_account_drawdown_kill_switch_tripped"))
        ),
        "entry_orders": len(entry_order_rows),
        "exit_orders": len(exit_order_rows),
        "orders_considered": len(considered_orders),
        "filled_orders": len(filled_orders),
        "fill_rate": fill_rate,
        "submitted_unconfirmed_orders": len(submitted_unconfirmed),
        "order_failure_count": len(order_failures),
        "post_only_orders": len(post_only_orders),
        "post_only_cancelled": len(post_only_cancelled),
        "post_only_cancel_rate": post_only_cancel_rate,
        "order_status_counts": order_status_counts,
        "order_status_counts_text": _compact_counts(order_status_counts),
        "signal_latency_ms": signal_latency_summary,
        "order_latency_ms": order_latency_summary,
        "fill_latency_ms": fill_latency_summary,
        "stop_placement_latency_ms": stop_placement_latency_summary,
        "open_trades": sum(1 for row in trade_rows if str(row.get("status") or "").strip().lower() == "open"),
        "closed_trades": sum(1 for row in trade_rows if str(row.get("status") or "").strip().lower() == "closed"),
        "fees_usdt_total": fees_usdt_total,
        "trade_fees_usdt_total": trade_fee_total,
        "order_fees_usdt_total": order_fee_total,
        "funding_usdt_total": funding_total,
        "maker_taker_counts": maker_taker_counts,
        "maker_taker_counts_text": _compact_counts(maker_taker_counts),
        "risk_events": len(risk_events),
        "lifecycle_events": len(lifecycle_events),
        "lifecycle_status_counts": lifecycle_status_counts,
        "lifecycle_status_counts_text": _compact_counts(lifecycle_status_counts),
        "lifecycle_transition_rejected_events": lifecycle_transition_rejected_events,
        "stop_repair_events": len(stop_repair_events),
        "stop_repair_error_count": stop_repair_error_count,
        "invalid_jsonl_lines": len(invalid_jsonl_lines),
        "invalid_jsonl_sources": ",".join(invalid_jsonl_lines[:25]),
        "metrics_unavailable": unavailable,
    }
    issues = _continuous_operational_issues(summary, role=role)
    return {"ok": not issues, "summary": summary, "issues": issues}


def _fmt_optional_float(value: Any, *, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{_float(value):.{digits}f}"


def format_continuous_operational_metrics_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Continuous Operational Metrics Audit",
        "",
        f"ok: `{payload['ok']}`",
        "",
        "This is a Phase-13 telemetry audit, not alpha, promotion, or real-money evidence.",
        "",
        "## Filters",
        "",
        f"- start_ts_ms: `{summary.get('start_ts_ms') or ''}`",
        f"- start_utc: `{summary.get('start_utc') or ''}`",
        f"- strategy_profile: `{summary.get('strategy_profile') or ''}`",
        f"- strategy_id: `{summary.get('strategy_id') or ''}`",
        "",
        "## Cycle Telemetry",
        "",
        f"- cycles: `{summary['cycles']}`",
        f"- cycles_before_filter: `{summary['cycles_before_filter']}`",
        f"- candidate_cycles: `{summary['candidate_cycles']}`",
        f"- candidates_total: `{summary['candidates_total']:.0f}`",
        f"- entries_total: `{summary['entries_total']:.0f}`",
        f"- exits_total: `{summary['exits_total']:.0f}`",
        f"- signal_latency_ms_median: `{_fmt_optional_float(summary['signal_latency_ms']['median'])}`",
        f"- signal_latency_ms_max: `{_fmt_optional_float(summary['signal_latency_ms']['max'])}`",
        f"- entry_errors_total: `{summary['entry_errors_total']:.0f}`",
        f"- exit_errors_total: `{summary['exit_errors_total']:.0f}`",
        f"- sniper_errors_total: `{summary['sniper_errors_total']:.0f}`",
        f"- wallet_error_cycles: `{summary['wallet_error_cycles']}`",
        f"- risk_health_blocked_cycles: `{summary['risk_health_blocked_cycles']}`",
        f"- ledger_mismatch_cycles: `{summary['ledger_mismatch_cycles']}`",
        f"- unprotected_position_seconds_max: `{summary['unprotected_position_seconds_max']:.2f}`",
        f"- portfolio_heat_clamped_cycles: `{summary['portfolio_heat_clamped_cycles']}`",
        f"- account_drawdown_kill_switch_cycles: `{summary['account_drawdown_kill_switch_cycles']}`",
        "",
        "## Order And Fill Telemetry",
        "",
        f"- orders: `{summary['orders']}`",
        f"- orders_before_filter: `{summary['orders_before_filter']}`",
        f"- entry_orders: `{summary['entry_orders']}`",
        f"- exit_orders: `{summary['exit_orders']}`",
        f"- orders_considered: `{summary['orders_considered']}`",
        f"- filled_orders: `{summary['filled_orders']}`",
        f"- fill_rate: `{_fmt_optional_float(summary['fill_rate'], digits=4)}`",
        f"- order_latency_ms_median: `{_fmt_optional_float(summary['order_latency_ms']['median'])}`",
        f"- fill_latency_ms_median: `{_fmt_optional_float(summary['fill_latency_ms']['median'])}`",
        f"- submitted_unconfirmed_orders: `{summary['submitted_unconfirmed_orders']}`",
        f"- order_failure_count: `{summary['order_failure_count']}`",
        f"- post_only_orders: `{summary['post_only_orders']}`",
        f"- post_only_cancelled: `{summary['post_only_cancelled']}`",
        f"- post_only_cancel_rate: `{_fmt_optional_float(summary['post_only_cancel_rate'], digits=4)}`",
        f"- order_status_counts: `{summary['order_status_counts_text']}`",
        "",
        "## Trade And Protection Telemetry",
        "",
        f"- trades: `{summary['trades']}`",
        f"- trades_before_filter: `{summary['trades_before_filter']}`",
        f"- open_trades: `{summary['open_trades']}`",
        f"- closed_trades: `{summary['closed_trades']}`",
        f"- fees_usdt_total: `{summary['fees_usdt_total']:.6f}`",
        f"- trade_fees_usdt_total: `{summary['trade_fees_usdt_total']:.6f}`",
        f"- order_fees_usdt_total: `{summary['order_fees_usdt_total']:.6f}`",
        f"- funding_usdt_total: `{summary['funding_usdt_total']:.6f}`",
        f"- maker_taker_counts: `{summary['maker_taker_counts_text']}`",
        f"- stop_placement_latency_ms_median: `{_fmt_optional_float(summary['stop_placement_latency_ms']['median'])}`",
        f"- risk_events: `{summary['risk_events']}`",
        f"- risk_health_blocked_events: `{summary['risk_health_blocked_events']}`",
        f"- lifecycle_events: `{summary['lifecycle_events']}`",
        f"- lifecycle_status_counts: `{summary['lifecycle_status_counts_text']}`",
        f"- lifecycle_transition_rejected_events: `{summary['lifecycle_transition_rejected_events']}`",
        f"- stop_repair_events: `{summary['stop_repair_events']}`",
        f"- stop_repair_error_count: `{summary['stop_repair_error_count']}`",
        f"- invalid_jsonl_lines: `{summary['invalid_jsonl_lines']}`",
        f"- invalid_jsonl_sources: `{summary['invalid_jsonl_sources']}`",
        "",
        "## Metrics Not Yet Measurable",
        "",
        f"`{', '.join(summary['metrics_unavailable'])}`" if summary["metrics_unavailable"] else "`none`",
    ]
    issues = payload.get("issues") or []
    if issues:
        lines.extend(["", "## Issues"])
        for issue in issues[:50]:
            lines.append(f"- `{issue}`")
    return "\n".join(lines) + "\n"


def run_continuous_operational_metrics_audit(
    data_root: str | Path,
    *,
    cycles_dataset: str = "continuous_fade_paper_cycles",
    orders_dataset: str = "continuous_fade_paper_orders",
    trades_dataset: str = "continuous_fade_paper_trades",
    output_dir: str | Path | None = None,
    start_ts_ms: int | None = None,
    strategy_profile: str | None = None,
    strategy_id: str | tuple[str, ...] | None = None,
    role: str = "demo",
) -> dict[str, Any]:
    root = Path(data_root).expanduser()
    payload = audit_continuous_operational_metrics(
        read_dataset(root, cycles_dataset),
        read_dataset(root, orders_dataset),
        read_dataset(root, trades_dataset),
        data_root=root,
        start_ts_ms=start_ts_ms,
        strategy_profile=strategy_profile,
        strategy_id=strategy_id,
        role=role,
    )
    report = format_continuous_operational_metrics_report(payload)
    report_dir = Path(output_dir).expanduser() if output_dir else root / "reports" / "continuous_operational_metrics"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "continuous_operational_metrics.md"
    report_path.write_text(report, encoding="utf-8")
    return {"result": payload, "report": report, "report_path": str(report_path)}


def run_continuous_rebalance_cycle_audit(
    data_root: str | Path,
    *,
    cycles_dataset: str = "continuous_fade_paper_cycles",
    orders_dataset: str = "continuous_fade_paper_orders",
    output_dir: str | Path | None = None,
    start_ts_ms: int | None = None,
    strategy_profile: str | None = None,
    cycle_strategy_id: str | tuple[str, ...] | None = None,
    order_strategy_id: str | tuple[str, ...] | None = None,
    require_rebalance_telemetry: bool = True,
) -> dict[str, Any]:
    root = Path(data_root).expanduser()
    payload = audit_continuous_rebalance_cycles(
        read_dataset(root, cycles_dataset),
        read_dataset(root, orders_dataset),
        start_ts_ms=start_ts_ms,
        strategy_profile=strategy_profile,
        cycle_strategy_id=cycle_strategy_id,
        order_strategy_id=order_strategy_id,
        require_rebalance_telemetry=require_rebalance_telemetry,
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
    paper_operational = payload.get("paper_operational")
    demo_operational = payload.get("demo_operational")
    paper_demo = payload.get("paper_demo")
    demo_summary = demo_rebalance["result"]["summary"] if demo_rebalance is not None else None
    paper_operational_summary = (
        paper_operational["result"]["summary"] if paper_operational is not None else None
    )
    demo_operational_summary = demo_operational["result"]["summary"] if demo_operational is not None else None
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
        f"- paper operational ok: `{summary['paper_operational_ok']}`",
        f"- demo operational ok: `{summary['demo_operational_ok']}`",
        f"- paired trades: `{summary['paired']}`",
        f"- sample warning: `{summary['sample_warning']}`",
        f"- paper-only trades: `{summary['paper_only']}`",
        f"- demo-only trades: `{summary['demo_only']}`",
        f"- no unmatched required: `{summary['require_no_unmatched']}`",
        f"- start_ts_ms: `{summary.get('start_ts_ms') or ''}`",
        f"- start_utc: `{summary.get('start_utc') or ''}`",
        f"- strategy_profile: `{summary.get('strategy_profile') or ''}`",
        f"- paper_strategy_id: `{summary.get('paper_strategy_id') or ''}`",
        f"- demo_strategy_id: `{summary.get('demo_strategy_id') or ''}`",
        "",
        "## Paper Rebalance",
        "",
        f"- cycles: `{paper_summary['cycles']}`",
        f"- rebalance_cycles: `{paper_summary['rebalance_cycles']}`",
        f"- rebalance telemetry required: `{paper_summary.get('rebalance_telemetry_required', True)}`",
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
                f"- rebalance telemetry required: `{demo_summary.get('rebalance_telemetry_required', True)}`",
                f"- scale_mismatches: `{demo_summary['scale_mismatches']}`",
                f"- same_day_resize_violations: `{demo_summary['same_day_resize_violations']}`",
                f"- resize_order_count_mismatch: `{demo_summary['resize_order_count_mismatch']}`",
                f"- report: `{demo_rebalance['report_path']}`",
            ]
        )
    else:
        lines.extend(["", "## Demo Rebalance", "", "- skipped: `paper_only_mode`"])
    if paper_operational_summary is not None and paper_operational is not None:
        lines.extend(
            [
                "",
                "## Paper Operational Metrics",
                "",
                f"- cycles: `{paper_operational_summary['cycles']}`",
                f"- orders: `{paper_operational_summary['orders']}`",
                f"- trades: `{paper_operational_summary['trades']}`",
                f"- risk_health_blocked_cycles: `{paper_operational_summary['risk_health_blocked_cycles']}`",
                f"- order_failure_count: `{paper_operational_summary['order_failure_count']}`",
                f"- unprotected_position_seconds_max: `{paper_operational_summary['unprotected_position_seconds_max']:.2f}`",
                f"- portfolio_heat_clamped_cycles: `{paper_operational_summary['portfolio_heat_clamped_cycles']}`",
                f"- account_drawdown_kill_switch_cycles: `{paper_operational_summary['account_drawdown_kill_switch_cycles']}`",
                f"- metrics_unavailable: `{', '.join(paper_operational_summary['metrics_unavailable'])}`",
                f"- report: `{paper_operational['report_path']}`",
            ]
        )
    if demo_operational_summary is not None and demo_operational is not None:
        lines.extend(
            [
                "",
                "## Demo Operational Metrics",
                "",
                f"- cycles: `{demo_operational_summary['cycles']}`",
                f"- orders: `{demo_operational_summary['orders']}`",
                f"- trades: `{demo_operational_summary['trades']}`",
                f"- risk_health_blocked_cycles: `{demo_operational_summary['risk_health_blocked_cycles']}`",
                f"- order_failure_count: `{demo_operational_summary['order_failure_count']}`",
                f"- unprotected_position_seconds_max: `{demo_operational_summary['unprotected_position_seconds_max']:.2f}`",
                f"- portfolio_heat_clamped_cycles: `{demo_operational_summary['portfolio_heat_clamped_cycles']}`",
                f"- account_drawdown_kill_switch_cycles: `{demo_operational_summary['account_drawdown_kill_switch_cycles']}`",
                f"- metrics_unavailable: `{', '.join(demo_operational_summary['metrics_unavailable'])}`",
                f"- report: `{demo_operational['report_path']}`",
            ]
        )
    else:
        lines.extend(["", "## Demo Operational Metrics", "", "- skipped: `paper_only_mode`"])
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
    start_ts_ms: int | None = None,
    strategy_profile: str | None = None,
    paper_strategy_id: str | None = None,
    demo_strategy_id: str | None = None,
) -> dict[str, Any]:
    """Run the continuous candidate's forward-ledger readiness checks.

    This combines the daily-rebalance telemetry audit on both paper and demo
    roots with the paper↔demo trade reconciliation. It deliberately does not
    label alpha or promotion; it only says whether the forward ledgers are clean
    enough to be used as the next arbiter.

    The command-line reconcile front door passes the frozen v2-forward filters
    explicitly. The library default stays unfiltered so ad-hoc diagnostics and
    tests can still inspect full ledger history.
    """
    paper_root_p = Path(paper_root).expanduser()
    demo_root_p = Path(demo_root).expanduser()
    out_root = (
        Path(output_dir).expanduser()
        if output_dir
        else paper_root_p / "reports" / "continuous_forward_readiness"
    )
    out_root.mkdir(parents=True, exist_ok=True)
    require_rebalance_telemetry = _rebalance_telemetry_required(strategy_profile)

    paper_rebalance = run_continuous_rebalance_cycle_audit(
        paper_root_p,
        cycles_dataset="continuous_fade_paper_cycles",
        orders_dataset="continuous_fade_paper_orders",
        output_dir=out_root / "paper_rebalance",
        start_ts_ms=start_ts_ms,
        strategy_profile=strategy_profile,
        cycle_strategy_id=paper_strategy_id,
        order_strategy_id=paper_strategy_id,
        require_rebalance_telemetry=require_rebalance_telemetry,
    )
    paper_operational = run_continuous_operational_metrics_audit(
        paper_root_p,
        cycles_dataset="continuous_fade_paper_cycles",
        orders_dataset="continuous_fade_paper_orders",
        trades_dataset="continuous_fade_paper_trades",
        output_dir=out_root / "paper_operational",
        start_ts_ms=start_ts_ms,
        strategy_profile=strategy_profile,
        strategy_id=paper_strategy_id,
        role="paper",
    )
    demo_rebalance = None
    demo_operational = None
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
            start_ts_ms=start_ts_ms,
            strategy_profile=strategy_profile,
            cycle_strategy_id=demo_strategy_id,
            order_strategy_id=demo_strategy_id,
            require_rebalance_telemetry=require_rebalance_telemetry,
        )
        demo_operational = run_continuous_operational_metrics_audit(
            demo_root_p,
            cycles_dataset="continuous_fade_demo_cycles",
            orders_dataset="continuous_fade_demo_orders",
            trades_dataset="continuous_fade_demo_trades",
            output_dir=out_root / "demo_operational",
            start_ts_ms=start_ts_ms,
            strategy_profile=strategy_profile,
            strategy_id=demo_strategy_id,
            role="demo",
        )
        paper_demo = run_continuous_paper_demo_reconciliation(
            paper_root_p,
            demo_root_p,
            entry_tolerance_ms=entry_tolerance_ms,
            min_pairs_warning=min_pairs_warning,
            output_dir=out_root / "paper_demo",
            start_ts_ms=start_ts_ms,
            paper_strategy_id=paper_strategy_id,
            demo_strategy_id=demo_strategy_id,
        )
        rec_summary = paper_demo["result"]["summary"]

    issues: list[str] = []
    if not paper_rebalance["result"]["ok"]:
        issues.append("paper rebalance telemetry audit failed")
    if not paper_operational["result"]["ok"]:
        issues.append("paper operational metrics audit failed")
    if require_demo and demo_rebalance is not None and not demo_rebalance["result"]["ok"]:
        issues.append("demo rebalance telemetry audit failed")
    if require_demo and demo_operational is not None and not demo_operational["result"]["ok"]:
        issues.append("demo operational metrics audit failed")
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
    demo_operational_ok = None
    if require_demo:
        assert demo_operational is not None
        demo_operational_ok = bool(demo_operational["result"]["ok"])

    summary = {
        "paper_only_mode": not require_demo,
        "paper_rebalance_ok": bool(paper_rebalance["result"]["ok"]),
        "demo_rebalance_ok": demo_rebalance_ok,
        "paper_operational_ok": bool(paper_operational["result"]["ok"]),
        "demo_operational_ok": demo_operational_ok,
        "paired": int(rec_summary["paired"]),
        "paper_only": int(rec_summary["paper_only"]),
        "demo_only": int(rec_summary["demo_only"]),
        "sample_warning": bool(rec_summary.get("sample_warning")),
        "min_pairs_warning_threshold": int(rec_summary.get("min_pairs_warning_threshold", min_pairs_warning)),
        "require_no_unmatched": bool(require_no_unmatched),
        "start_ts_ms": start_ts_ms,
        "start_utc": _utc_ms(start_ts_ms),
        "strategy_profile": strategy_profile or "",
        "paper_strategy_id": paper_strategy_id or "",
        "demo_strategy_id": demo_strategy_id or "",
    }
    payload = {
        "ok": not issues,
        "summary": summary,
        "issues": issues,
        "paper_rebalance": paper_rebalance,
        "demo_rebalance": demo_rebalance,
        "paper_operational": paper_operational,
        "demo_operational": demo_operational,
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
    start_ts_ms: int | None = None,
    paper_strategy_id: str | tuple[str, ...] | None = None,
    demo_strategy_id: str | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    paper_root_p = Path(paper_root).expanduser()
    demo_root_p = Path(demo_root).expanduser()
    paper = read_dataset(paper_root_p, paper_dataset)
    demo = read_dataset(demo_root_p, demo_dataset)
    paper = _filter_min_ts(paper, start_ts_ms, ("signal_ts_ms", "entry_ts_ms", "ts_ms"))
    demo = _filter_min_ts(demo, start_ts_ms, ("signal_ts_ms", "entry_ts_ms", "ts_ms"))
    paper = _filter_value(paper, "strategy_id", paper_strategy_id)
    demo = _filter_value(demo, "strategy_id", demo_strategy_id)
    result = reconcile_paper_demo(
        paper,
        demo,
        entry_tolerance_ms=entry_tolerance_ms,
    )
    report = format_reconciliation_report(result)
    if start_ts_ms is not None or paper_strategy_id is not None or demo_strategy_id is not None:
        # Make the standalone reconcile report self-describing: print the active
        # forward-window boundary + strategy filters (v2-forward by default) so a
        # reader can see pre-freeze rows were excluded.
        report += (
            "\n## Forward-Window Filter\n\n"
            f"- start_ts_ms: `{start_ts_ms if start_ts_ms is not None else ''}`\n"
            f"- start_utc: `{_utc_ms(start_ts_ms)}`\n"
            f"- paper_strategy_id: `{_id_label(paper_strategy_id)}`\n"
            f"- demo_strategy_id: `{_id_label(demo_strategy_id)}`\n"
        )
    report_dir = (
        Path(output_dir).expanduser() if output_dir else demo_root_p / "reports" / report_subdir
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / report_filename
    report_path.write_text(report, encoding="utf-8")
    pairs_csv_path = _write_pairs_csv(report_path, result["pairs"])
    return {"result": result, "report": report, "report_path": str(report_path), "pairs_csv_path": pairs_csv_path}
