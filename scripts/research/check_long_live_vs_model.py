#!/usr/bin/env python3
"""Weekly LONG parity check: what the live fleet did against what the model says it should have done.

Read-only. One command re-runs the registered LONG rule over a window, lines its
trades up against the live record pair by pair, and splits every gap into named
pieces: entry slippage, exit slippage, structural differences that are expected
(the model takes a take-profit, the live path has none; the LLM gate enters
names the model never sees), and a residual. Output: a pairs CSV, a
plain-English markdown report, and a printed summary block.

Inputs and where they come from:
  --data-root       the point-in-time kline/funding store the model replays
                    (default ~/SHARED_DATA/bybit_full_pit)
  --model-trades    optional: an existing model ledger CSV, skipping the re-run
  --transitions     producer-written enter/leave JSONL (exists since 2026-08-30;
                    missing or thin is fine)
  --trades          the engine's closed-trade journal JSONL, filtered to the
                    long sleeve; round_trip null means the entry predates a log
                    rotation and only the exit side is knowable
  --cycle-reports   directory of long_native_cycle_*.json producer payloads
                    (7-day retention); recovers entry intent and planned exits
  --venue-history   optional venue closed-pnl plus transaction-log JSONL, the
                    deepest price and settlement backstop; the venue does not
                    tag sleeves, so its rows are attributed by position, symbol,
                    and time only

The join key is (symbol, signal timestamp). The model's cold start is handled
by running it from --start minus --warmup-days and grading only trades whose
signal falls inside [--start, --end).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000
GIT_LOCAL_ENV_VARS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}
DEFAULT_DATA_ROOT = "~/SHARED_DATA/bybit_full_pit"
DEFAULT_HOLD_DAYS = 3
MONEY_TOLERANCE_USDT = 1e-8

#: Live entries the LLM gate produced. The gate does not exist in the kernel,
#: so these can never pair with a model trade and are reported apart.
GATE_PATTERNS = frozenset({"llm_gate", "llm_gate_wide"})

STRUCTURAL_NO_LIVE_TP = "model_take_profit_no_live_tp"
STRUCTURAL_GATE_ONLY = "gate_not_in_kernel"
STRUCTURAL_LIVE_ALREADY_HELD = "live_position_already_open"

CSV_COLUMNS = [
    "cohort",
    "pair_method",
    "structural",
    "symbol",
    "pattern",
    "model_trade_id",
    "live_trade_id",
    "model_signal_ts_ms",
    "live_signal_ts_ms",
    "live_request_ts_ms",
    "model_entry_ts_ms",
    "live_entry_ts_ms",
    "entry_gap_ms",
    "model_entry_px",
    "live_entry_px",
    "entry_slippage_bps",
    "model_exit_ts_ms",
    "live_exit_ts_ms",
    "exit_gap_ms",
    "model_exit_px",
    "live_exit_px",
    "exit_slippage_bps",
    "model_exit_reason",
    "live_exit_reason",
    "model_price_net_bps",
    "model_funding_bps",
    "model_net_bps_book",
    "live_net_bps",
    "gap_bps",
    "live_entry_reason",
    "live_planned_stop_pct",
    "live_planned_exit_qty",
    "live_notional_usdt",
    "live_request_ts_source",
    "live_entry_ts_source",
    "live_entry_px_source",
    "live_exit_ts_source",
    "live_exit_px_source",
    "live_exit_reason_source",
    "live_net_bps_source",
    "live_funding_bps",
    "live_net_including_funding_bps",
    "live_exit_request_ts_ms",
    "live_exit_request_ts_source",
    "venue_rows",
    "venue_terminal_gap_ms",
    "venue_terminal_qty",
    "venue_qty_gap",
    "venue_terminal_closed_pnl_usdt",
    "venue_terminal_price_fee_pnl_usdt",
    "venue_closed_pnl_residual_usdt",
    "venue_position_reconstruction",
    "venue_position_open_ts_ms",
    "venue_position_close_ts_ms",
    "venue_position_open_order_id",
    "venue_position_close_order_id",
    "venue_position_open_qty",
    "venue_position_close_qty",
    "venue_position_trade_rows",
    "venue_position_resizes",
    "venue_settlement_rows",
    "venue_settlement_usdt",
    "venue_closed_pnl_unexplained_usdt",
    "venue_engine_position_link",
    "venue_terminal_price_fee_net_bps",
    "engine_venue_net_gap_bps",
    "venue_terminal_entry_value_usdt",
    "notes",
]


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _i(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _date_ms(day: str) -> int:
    parsed = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), tzinfo=dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _read_jsonl(path: Path) -> list[dict]:
    """Read object rows; a missing optional file is empty, corrupt evidence fails."""
    if not path.exists():
        return []
    rows: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: malformed JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(row)
    return rows


def _signal_ts_from_trade_id(trade_id: str | None) -> int | None:
    """The live id embeds the signal timestamp: long-<SYMBOL>-<signal_ts_ms>."""
    if not trade_id:
        return None
    parts = str(trade_id).rsplit("-", 1)
    if len(parts) != 2:
        return None
    ts = _i(parts[1])
    return ts if ts and ts > 0 else None


@dataclass
class LiveTrade:
    """One live LONG trade, stitched from whichever records survive for it.

    Every *_source field names the record a value came from. Producer
    transitions and candidates are requests, not fills. Engine rows carry
    exact fill facts. A venue row without an order or trade link is only an
    ambiguous price backstop; it never supplies the live net return. Explicit
    settlement rows can explain a venue position's closed-PnL, but do not prove
    which sleeve owned that position.
    """

    symbol: str
    trade_id: str | None = None
    signal_ts_ms: int | None = None
    pattern: str | None = None
    entry_reason: str | None = None
    request_ts_ms: int | None = None
    request_ts_source: str | None = None
    entry_ts_ms: int | None = None
    entry_ts_source: str | None = None
    entry_px: float | None = None
    entry_px_source: str | None = None
    exit_ts_ms: int | None = None
    exit_ts_source: str | None = None
    exit_px: float | None = None
    exit_px_source: str | None = None
    exit_reason: str | None = None
    exit_reason_source: str | None = None
    exit_request_ts_ms: int | None = None
    exit_request_ts_source: str | None = None
    net_bps: float | None = None
    net_bps_source: str | None = None
    live_funding_bps: float | None = None
    live_net_including_funding_bps: float | None = None
    hold_deadline_ts_ms: int | None = None
    max_hold_days: int | None = None
    planned_stop_pct: float | None = None
    planned_exit_qty: float | None = None
    notional_usdt: float | None = None
    venue_rows: int = 0
    venue_terminal_gap_ms: int | None = None
    venue_terminal_qty: float | None = None
    venue_qty_gap: float | None = None
    venue_terminal_closed_pnl_usdt: float | None = None
    venue_terminal_price_fee_pnl_usdt: float | None = None
    venue_closed_pnl_residual_usdt: float | None = None
    venue_position_reconstruction: str | None = None
    venue_position_open_ts_ms: int | None = None
    venue_position_close_ts_ms: int | None = None
    venue_position_open_order_id: str | None = None
    venue_position_close_order_id: str | None = None
    venue_position_open_qty: float | None = None
    venue_position_close_qty: float | None = None
    venue_position_trade_rows: int = 0
    venue_position_resizes: int = 0
    venue_settlement_rows: int = 0
    venue_settlement_usdt: float | None = None
    venue_closed_pnl_unexplained_usdt: float | None = None
    venue_engine_position_link: str | None = None
    venue_terminal_price_fee_net_bps: float | None = None
    engine_venue_net_gap_bps: float | None = None
    venue_terminal_entry_value_usdt: float | None = None
    engine_attached: bool = False
    engine_side: str | None = None
    engine_qty: float | None = None
    engine_fills: int | None = None
    engine_entry_notional_usdt: float | None = None
    engine_price_fee_pnl_usdt: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class CycleEvidence:
    """What the producer's cycle payloads say: entry intent and planned exits.

    candidates and planned_exits are keyed by trade id; a candidate carries an
    ``entered`` flag when its cycle recorded a book addition. Candidates with
    no entry evidence anywhere are listed, not traded.
    """

    candidates: dict[str, dict]
    planned_exits: dict[str, dict]
    uncorroborated_candidate_ids: set[str]
    cycle_count: int = 0
    malformed_files: int = 0
    first_cycle_ts_ms: int | None = None
    last_cycle_ts_ms: int | None = None
    strategy_ids: tuple[str, ...] = ()
    strategy_profiles: tuple[str, ...] = ()
    config_changes: tuple[dict, ...] = ()


@dataclass
class Cohorts:
    """The four populations the report keeps apart, never pooled."""

    pairs: list[tuple[dict, LiveTrade, str]]
    model_while_live_held: list[tuple[dict, LiveTrade]]
    model_only: list[dict]
    live_only: list[LiveTrade]
    gate: list[LiveTrade]


@dataclass(frozen=True)
class VenuePositionEvidence:
    status: str
    reason: str | None = None
    open_ts_ms: int | None = None
    close_ts_ms: int | None = None
    open_order_id: str | None = None
    close_order_id: str | None = None
    open_qty: float | None = None
    close_qty: float | None = None
    trade_rows: int = 0
    resizes: int = 0
    settlement_rows: tuple[dict, ...] = ()
    settlement_usdt: float | None = None


def load_transitions(path: Path) -> list[dict]:
    return _read_jsonl(path)


def load_engine_long_trades(path: Path) -> list[dict]:
    return [row for row in _read_jsonl(path) if str(row.get("sleeve")) == "long"]


def load_venue_closed_pnl(path: Path) -> list[dict]:
    return [row for row in _read_jsonl(path) if row.get("_kind") == "closed_pnl"]


def load_venue_transactions(path: Path) -> list[dict]:
    rows, _ = _dedupe_venue_transactions([row for row in _read_jsonl(path) if row.get("_kind") == "txn"])
    return rows


def _dedupe_venue_transactions(rows: list[dict]) -> tuple[list[dict], int]:
    unique: list[dict] = []
    by_id: dict[str, dict] = {}
    duplicates = 0
    for row in rows:
        transaction_id = str(row.get("id") or "")
        if not transaction_id:
            unique.append(row)
            continue
        known = by_id.get(transaction_id)
        if known is None:
            by_id[transaction_id] = row
            unique.append(row)
            continue
        if known != row:
            raise ValueError(f"venue transaction id {transaction_id!r} has conflicting rows")
        duplicates += 1
    return unique, duplicates


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _decimal_close(left: Decimal, right: Decimal) -> bool:
    tolerance = max(Decimal("1e-12"), max(abs(left), abs(right)) * Decimal("1e-9"))
    return abs(left - right) <= tolerance


def _position_settlements(terminal_leg: dict, venue_transactions: list[dict]) -> VenuePositionEvidence:
    """Reconstruct one flat-to-flat venue position and its settlements."""

    def incomplete(reason: str, **facts: Any) -> VenuePositionEvidence:
        return VenuePositionEvidence(status="incomplete", reason=reason, **facts)

    symbol = str(terminal_leg.get("symbol") or "").upper()
    terminal_order_id = str(terminal_leg.get("orderId") or "")
    terminal_ts = _i(terminal_leg.get("updatedTime"))
    closing_side = str(terminal_leg.get("side") or "")
    if not symbol or not terminal_order_id or terminal_ts is None or closing_side not in {"Buy", "Sell"}:
        return incomplete("terminal closed-PnL row lacks symbol, order, time, or side")

    symbol_rows = [
        row
        for row in venue_transactions
        if str(row.get("symbol") or "").upper() == symbol and (_i(row.get("transactionTime")) or -1) <= terminal_ts
    ]
    terminal_trades = [
        row for row in symbol_rows if row.get("type") == "TRADE" and str(row.get("orderId") or "") == terminal_order_id
    ]
    if not terminal_trades:
        return incomplete("no transaction-log TRADE matches the terminal order", close_order_id=terminal_order_id)
    terminal_trades.sort(key=lambda row: _i(row.get("transactionTime")) or -1)
    terminal_times = [_i(row.get("transactionTime")) for row in terminal_trades]
    if any(ts is None for ts in terminal_times) or max(ts for ts in terminal_times if ts is not None) != terminal_ts:
        return incomplete("terminal order time does not match the closed-PnL row", close_order_id=terminal_order_id)
    if any(str(row.get("side") or "") != closing_side for row in terminal_trades):
        return incomplete("terminal order has inconsistent fill sides", close_order_id=terminal_order_id)
    close_qty = _decimal(terminal_leg.get("closedSize")) or _decimal(terminal_leg.get("qty"))
    terminal_qtys = [_decimal(row.get("qty")) for row in terminal_trades]
    if close_qty is None or close_qty <= 0 or any(qty is None or qty <= 0 for qty in terminal_qtys):
        return incomplete("terminal order has invalid quantity", close_order_id=terminal_order_id)
    if not _decimal_close(sum((qty for qty in terminal_qtys if qty is not None), Decimal(0)), close_qty):
        return incomplete("terminal transaction quantity does not match closedSize", close_order_id=terminal_order_id)
    terminal_size = _decimal(terminal_trades[-1].get("size"))
    if terminal_size is None or not _decimal_close(terminal_size, Decimal(0)):
        return incomplete("terminal order does not leave the venue position flat", close_order_id=terminal_order_id)

    open_side = "Buy" if closing_side == "Sell" else "Sell"
    open_txn = None
    first_terminal_ts = terminal_times[0]
    if first_terminal_ts is None:
        return incomplete("terminal order has no transaction time", close_order_id=terminal_order_id)
    for row in sorted(symbol_rows, key=lambda item: _i(item.get("transactionTime")) or -1, reverse=True):
        row_ts = _i(row.get("transactionTime"))
        if row_ts is None or row_ts >= first_terminal_ts or row.get("type") != "TRADE" or row.get("side") != open_side:
            continue
        size = _decimal(row.get("size"))
        qty = _decimal(row.get("qty"))
        if size is not None and qty is not None and size > 0 and _decimal_close(size, qty):
            open_txn = row
            break
    if open_txn is None:
        return incomplete(
            "history starts mid-position or has no provable flat-to-open trade",
            close_order_id=terminal_order_id,
        )

    open_ts = _i(open_txn.get("transactionTime"))
    open_qty = _decimal(open_txn.get("qty"))
    if open_ts is None or open_qty is None:
        return incomplete("flat-to-open trade lacks time or quantity", close_order_id=terminal_order_id)
    path_rows = [
        row
        for row in symbol_rows
        if row.get("type") in {"TRADE", "SETTLEMENT"}
        and open_ts <= (_i(row.get("transactionTime")) or -1) <= terminal_ts
    ]
    path_rows.sort(key=lambda row: _i(row.get("transactionTime")) or -1)
    trade_times = [_i(row.get("transactionTime")) for row in path_rows if row.get("type") == "TRADE"]
    if len(trade_times) != len(set(trade_times)):
        return incomplete(
            "same-millisecond trade ordering is ambiguous",
            open_ts_ms=open_ts,
            close_ts_ms=terminal_ts,
            open_order_id=str(open_txn.get("orderId") or "") or None,
            close_order_id=terminal_order_id,
        )

    current_size = Decimal(0)
    trade_count = 0
    resizes = 0
    settlements: list[dict] = []
    settlement_total = Decimal(0)
    for row in path_rows:
        row_ts = _i(row.get("transactionTime"))
        if row_ts is None:
            return incomplete("position path has a row without time", close_order_id=terminal_order_id)
        if row.get("type") == "TRADE":
            side = str(row.get("side") or "")
            qty = _decimal(row.get("qty"))
            post_size = _decimal(row.get("size"))
            if side not in {open_side, closing_side} or qty is None or qty <= 0 or post_size is None or post_size < 0:
                return incomplete("position path has an invalid trade row", close_order_id=terminal_order_id)
            expected = current_size + qty if side == open_side else current_size - qty
            if expected < 0 or not _decimal_close(expected, post_size):
                return incomplete("position-size arithmetic breaks inside the path", close_order_id=terminal_order_id)
            current_size = post_size
            trade_count += 1
            if trade_count > 1 and current_size > 0:
                resizes += 1
            if current_size == 0 and str(row.get("orderId") or "") != terminal_order_id:
                return incomplete("position goes flat before the terminal order", close_order_id=terminal_order_id)
            continue

        if row_ts in trade_times:
            return incomplete("a settlement shares a timestamp with a trade", close_order_id=terminal_order_id)
        settlement_side = str(row.get("side") or "")
        settlement_size = _decimal(row.get("size"))
        funding = _decimal(row.get("funding"))
        if (
            settlement_side != open_side
            or settlement_size is None
            or not _decimal_close(settlement_size, current_size)
            or str(row.get("currency") or "") != "USDT"
            or funding is None
        ):
            return incomplete(
                "a settlement does not match the evolving USDT position", close_order_id=terminal_order_id
            )
        settlements.append(row)
        settlement_total += funding

    if current_size != 0:
        return incomplete("reconstructed position is not flat at the end", close_order_id=terminal_order_id)
    return VenuePositionEvidence(
        status="exact_one_way_position",
        open_ts_ms=open_ts,
        close_ts_ms=terminal_ts,
        open_order_id=str(open_txn.get("orderId") or "") or None,
        close_order_id=terminal_order_id,
        open_qty=float(open_qty),
        close_qty=float(close_qty),
        trade_rows=trade_count,
        resizes=resizes,
        settlement_rows=tuple(settlements),
        settlement_usdt=float(settlement_total),
    )


def _engine_position_link(trade: LiveTrade, terminal_leg: dict) -> str:
    if trade.venue_position_reconstruction != "exact_one_way_position":
        return "unlinked: venue position is not exact"
    if not trade.engine_attached or trade.engine_side != "long":
        return "unlinked: no long-sleeve engine round trip"
    required = (
        trade.entry_ts_ms,
        trade.exit_ts_ms,
        trade.engine_qty,
        trade.engine_fills,
        trade.engine_entry_notional_usdt,
        trade.engine_price_fee_pnl_usdt,
        trade.entry_px,
        trade.exit_px,
        trade.venue_position_open_ts_ms,
        trade.venue_position_close_ts_ms,
        trade.venue_position_close_qty,
        trade.venue_terminal_entry_value_usdt,
        trade.venue_terminal_price_fee_pnl_usdt,
        trade.venue_closed_pnl_unexplained_usdt,
    )
    if any(value is None for value in required):
        return "unlinked: engine or venue position fields are incomplete"
    assert trade.entry_ts_ms is not None
    assert trade.exit_ts_ms is not None
    assert trade.engine_qty is not None
    assert trade.engine_fills is not None
    assert trade.engine_entry_notional_usdt is not None
    assert trade.engine_price_fee_pnl_usdt is not None
    assert trade.entry_px is not None
    assert trade.exit_px is not None
    assert trade.venue_position_open_ts_ms is not None
    assert trade.venue_position_close_ts_ms is not None
    assert trade.venue_position_close_qty is not None
    assert trade.venue_terminal_entry_value_usdt is not None
    assert trade.venue_terminal_price_fee_pnl_usdt is not None
    assert trade.venue_closed_pnl_unexplained_usdt is not None
    venue_entry_px = _f(terminal_leg.get("avgEntryPrice"))
    venue_exit_px = _f(terminal_leg.get("avgExitPrice"))
    if venue_entry_px is None or venue_exit_px is None:
        return "unlinked: venue average prices are incomplete"
    exact = (
        trade.entry_ts_ms == trade.venue_position_open_ts_ms
        and trade.exit_ts_ms == trade.venue_position_close_ts_ms
        and math.isclose(trade.engine_qty, trade.venue_position_close_qty, rel_tol=1e-9, abs_tol=1e-12)
        and trade.engine_fills == trade.venue_position_trade_rows
        and math.isclose(trade.entry_px, venue_entry_px, rel_tol=1e-9, abs_tol=1e-12)
        and math.isclose(trade.exit_px, venue_exit_px, rel_tol=1e-9, abs_tol=1e-12)
        and abs(trade.engine_entry_notional_usdt - trade.venue_terminal_entry_value_usdt) <= MONEY_TOLERANCE_USDT
        and abs(trade.engine_price_fee_pnl_usdt - trade.venue_terminal_price_fee_pnl_usdt) <= MONEY_TOLERANCE_USDT
        and abs(trade.venue_closed_pnl_unexplained_usdt) <= MONEY_TOLERANCE_USDT
    )
    return "exact_long_sleeve" if exact else "unlinked: engine and venue position or accounting facts differ"


def load_cycle_evidence(reports_dir: Path) -> CycleEvidence:
    candidates: dict[str, dict] = {}
    planned_exits: dict[str, dict] = {}
    cycle_rows: list[tuple[int, str, str, float | None, float | None, str]] = []
    malformed_files = 0
    for payload_path in sorted(reports_dir.glob("long_native_cycle_*.json")):
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            malformed_files += 1
            continue
        if not isinstance(payload, dict):
            malformed_files += 1
            continue
        cycle = payload.get("cycle") or {}
        cycle_ts = _i(cycle.get("ts_ms")) or 0
        runtime_config = payload.get("config") or {}
        cycle_rows.append(
            (
                cycle_ts,
                str(cycle.get("strategy_id") or ""),
                str(cycle.get("strategy_profile") or ""),
                _f(cycle.get("notional_multiplier") or runtime_config.get("notional_multiplier")),
                _f(cycle.get("entry_leverage") or runtime_config.get("entry_leverage")),
                str(cycle.get("operational_profile_sha256") or runtime_config.get("operational_profile_sha256") or ""),
            )
        )
        cycle_entry_count = max(
            _i(cycle.get("entry_book_additions")) or 0,
            _i(cycle.get("entry_targets_queued")) or 0,
        )
        payload_candidates = payload.get("candidates") or []
        entered_trade_ids: set[str] = {
            str(candidate.get("trade_id"))
            for candidate in payload_candidates
            if candidate.get("trade_id") and candidate.get("entered") is True
        }
        if cycle_entry_count == 1 and len(payload_candidates) == 1:
            entered_trade_ids.add(str(payload_candidates[0].get("trade_id") or ""))
        hold_days = _i((payload.get("strategy_config") or {}).get("fc_max_hold_days"))
        for cand in payload_candidates:
            trade_id = str(cand.get("trade_id") or "")
            if not trade_id or not cand.get("symbol"):
                continue
            known = candidates.get(trade_id)
            if known is None or cycle_ts >= (known.get("cycle_ts_ms") or 0):
                merged = dict(cand)
                merged["cycle_ts_ms"] = cycle_ts
                merged["request_ts_ms"] = cycle_ts
                merged["entered"] = trade_id in entered_trade_ids or bool(known and known.get("entered"))
                candidates[trade_id] = merged
            elif trade_id in entered_trade_ids:
                known["entered"] = True
        for planned in payload.get("planned_exits") or []:
            trade_id = str(planned.get("trade_id") or "")
            if not trade_id:
                continue
            known = planned_exits.get(trade_id)
            # Keep the last word: a planned exit's reason can change as the
            # position ages (stop first, time stop at the deadline).
            if known is None or cycle_ts >= (known.get("cycle_ts_ms") or 0):
                merged = dict(planned)
                merged["cycle_ts_ms"] = cycle_ts
                merged["exit_request_ts_ms"] = cycle_ts
                merged["max_hold_days"] = hold_days
                planned_exits[trade_id] = merged
    uncorroborated = {
        trade_id for trade_id, cand in candidates.items() if not cand.get("entered") and trade_id not in planned_exits
    }
    cycle_rows.sort(key=lambda row: row[0])
    config_changes: list[dict] = []
    previous: tuple[str, str, float | None, float | None, str] | None = None
    for cycle_ts, strategy_id, strategy_profile, multiplier, leverage, profile_sha in cycle_rows:
        identity = (strategy_id, strategy_profile, multiplier, leverage, profile_sha)
        if identity == previous:
            continue
        config_changes.append(
            {
                "ts_ms": cycle_ts,
                "strategy_id": strategy_id or None,
                "strategy_profile": strategy_profile or None,
                "notional_multiplier": multiplier,
                "entry_leverage": leverage,
                "operational_profile_sha256": profile_sha or None,
            }
        )
        previous = identity
    timestamps = [row[0] for row in cycle_rows if row[0] > 0]
    return CycleEvidence(
        candidates=candidates,
        planned_exits=planned_exits,
        uncorroborated_candidate_ids=uncorroborated,
        cycle_count=len(cycle_rows),
        malformed_files=malformed_files,
        first_cycle_ts_ms=min(timestamps) if timestamps else None,
        last_cycle_ts_ms=max(timestamps) if timestamps else None,
        strategy_ids=tuple(sorted({row[1] for row in cycle_rows if row[1]})),
        strategy_profiles=tuple(sorted({row[2] for row in cycle_rows if row[2]})),
        config_changes=tuple(config_changes),
    )


def build_live_trades(
    transitions: list[dict],
    engine_trades: list[dict],
    cycles: CycleEvidence | None,
    venue_rows: list[dict],
    venue_transactions: list[dict] | None = None,
    *,
    default_hold_days: int = DEFAULT_HOLD_DAYS,
) -> list[LiveTrade]:
    """Stitch the live record into one trade list, best source winning per field."""
    venue_transactions = venue_transactions or []
    by_id: dict[str, LiveTrade] = {}
    trades: list[LiveTrade] = []

    def get(trade_id: str | None, symbol: str) -> LiveTrade:
        if trade_id and trade_id in by_id:
            return by_id[trade_id]
        trade = LiveTrade(symbol=symbol, trade_id=trade_id, signal_ts_ms=_signal_ts_from_trade_id(trade_id))
        if trade_id:
            by_id[trade_id] = trade
        trades.append(trade)
        return trade

    for row in transitions:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        trade = get(str(row.get("trade_id") or "") or None, symbol)
        if row.get("event") == "enter":
            trade.signal_ts_ms = _i(row.get("signal_ts_ms")) or trade.signal_ts_ms
            trade.pattern = str(row.get("pattern") or "") or trade.pattern
            trade.entry_reason = str(row.get("entry_reason") or "") or trade.entry_reason
            trade.notional_usdt = _f(row.get("notional_usdt")) or trade.notional_usdt
            if trade.request_ts_ms is None and (ts := _i(row.get("ts_ms"))) is not None:
                trade.request_ts_ms = ts
                trade.request_ts_source = "transitions"
        elif row.get("event") == "leave":
            if trade.exit_request_ts_ms is None and (ts := _i(row.get("ts_ms"))) is not None:
                trade.exit_request_ts_ms = ts
                trade.exit_request_ts_source = "transitions"

    if cycles is not None:
        for trade_id, cand in cycles.candidates.items():
            entered = bool(cand.get("entered")) or trade_id in cycles.planned_exits or trade_id in by_id
            if not entered:
                continue
            trade = get(trade_id, str(cand.get("symbol") or "").upper())
            trade.signal_ts_ms = trade.signal_ts_ms or _i(cand.get("signal_ts_ms"))
            trade.pattern = trade.pattern or (str(cand.get("pattern") or "") or None)
            trade.entry_reason = trade.entry_reason or (str(cand.get("entry_reason") or "") or None)
            trade.planned_stop_pct = trade.planned_stop_pct or _f(cand.get("stop_loss_pct"))
            if trade.request_ts_ms is None:
                ts = _i(cand.get("request_ts_ms")) or _i(cand.get("entry_ready_ts_ms"))
                if ts is not None:
                    trade.request_ts_ms = ts
                    trade.request_ts_source = "cycle_candidate"
        for trade_id, planned in cycles.planned_exits.items():
            symbol = str(planned.get("symbol") or "").upper()
            trade = get(trade_id, symbol or trade_id)
            if not trade.symbol and symbol:
                trade.symbol = symbol
            reason = str(planned.get("exit_reason") or "")
            if reason:
                trade.exit_reason = reason
                trade.exit_reason_source = "planned_exit"
            if trade.exit_request_ts_ms is None:
                request_ts = _i(planned.get("exit_request_ts_ms")) or _i(planned.get("cycle_ts_ms"))
                if request_ts is not None:
                    trade.exit_request_ts_ms = request_ts
                    trade.exit_request_ts_source = "planned_exit_cycle"
            trade.hold_deadline_ts_ms = _i(planned.get("max_hold_deadline_ts_ms")) or trade.hold_deadline_ts_ms
            trade.max_hold_days = _i(planned.get("max_hold_days")) or trade.max_hold_days
            trade.planned_exit_qty = _f(planned.get("qty")) or trade.planned_exit_qty
            hold_days = trade.max_hold_days or default_hold_days
            if trade.entry_ts_ms is None and trade.hold_deadline_ts_ms is not None:
                trade.entry_ts_ms = trade.hold_deadline_ts_ms - hold_days * MS_PER_DAY
                trade.entry_ts_source = "producer_fill_observed_ts"

    for engine_row in sorted(engine_trades, key=lambda row: _i(row.get("closed_ms")) or 0):
        symbol = str(engine_row.get("symbol") or "").upper()
        closed_ms = _i(engine_row.get("closed_ms"))
        best: LiveTrade | None = None
        best_score: int | None = None
        if closed_ms is not None:
            for trade in trades:
                if trade.symbol != symbol or trade.engine_attached:
                    continue
                anchor = trade.entry_ts_ms or trade.request_ts_ms or trade.signal_ts_ms
                if anchor is None:
                    continue
                deadline = (
                    trade.exit_request_ts_ms
                    or trade.hold_deadline_ts_ms
                    or (anchor + ((trade.max_hold_days or default_hold_days) + 1) * MS_PER_DAY)
                )
                if closed_ms < anchor - MS_PER_HOUR or closed_ms > deadline + 12 * MS_PER_HOUR:
                    continue
                score = abs(
                    closed_ms - (trade.exit_ts_ms or trade.exit_request_ts_ms or trade.hold_deadline_ts_ms or closed_ms)
                )
                if best_score is None or score < best_score:
                    best, best_score = trade, score
        if best is None:
            best = LiveTrade(symbol=symbol)
            best.notes.append("engine close with no producer identity")
            trades.append(best)
        best.engine_attached = True
        best.engine_side = str(engine_row.get("side") or "") or None
        best.engine_qty = _f(engine_row.get("qty"))
        best.engine_fills = _i(engine_row.get("fills"))
        best.exit_ts_ms = closed_ms
        best.exit_ts_source = "engine_journal"
        best.exit_px = _f(engine_row.get("exit_px"))
        best.exit_px_source = "engine_journal"
        round_trip = engine_row.get("round_trip")
        if isinstance(round_trip, dict):
            if (opened := _i(round_trip.get("opened_ms"))) is not None:
                best.entry_ts_ms = opened
                best.entry_ts_source = "engine_journal"
            if (entry_px := _f(round_trip.get("entry_px"))) is not None:
                best.entry_px = entry_px
                best.entry_px_source = "engine_journal"
            if (net_bps := _f(round_trip.get("net_bps"))) is not None:
                best.net_bps = net_bps
                best.net_bps_source = "engine_journal"
            best.engine_entry_notional_usdt = _f(round_trip.get("entry_notional_usdt"))
            best.engine_price_fee_pnl_usdt = _f(round_trip.get("net_usdt"))
            best.notional_usdt = best.notional_usdt or best.engine_entry_notional_usdt
        else:
            best.notes.append("round_trip null: entry predates a journal rotation")

    for trade in trades:
        if not venue_rows:
            continue
        anchor = trade.entry_ts_ms or trade.request_ts_ms or trade.signal_ts_ms
        if anchor is None:
            continue
        end_anchor = (
            trade.exit_ts_ms
            or trade.exit_request_ts_ms
            or trade.hold_deadline_ts_ms
            or anchor + (trade.max_hold_days or default_hold_days) * MS_PER_DAY
        )
        low, high = anchor - MS_PER_HOUR, end_anchor + MS_PER_HOUR
        matched = [
            row
            for row in venue_rows
            if str(row.get("symbol") or "").upper() == trade.symbol
            and str(row.get("side")) == "Sell"
            and low <= (_i(row.get("updatedTime")) or -1) <= high
        ]
        if not matched:
            continue
        expected_exit = trade.exit_ts_ms or trade.exit_request_ts_ms or trade.hold_deadline_ts_ms
        terminal_leg = min(
            matched,
            key=lambda row: abs((_i(row.get("updatedTime")) or end_anchor) - (expected_exit or end_anchor)),
        )
        terminal_ts = _i(terminal_leg.get("updatedTime"))
        if expected_exit is not None and terminal_ts is not None:
            trade.venue_terminal_gap_ms = terminal_ts - expected_exit

        trade.venue_rows = len(matched)
        terminal_qty = _f(terminal_leg.get("closedSize")) or _f(terminal_leg.get("qty"))
        entry_value = _f(terminal_leg.get("cumEntryValue"))
        exit_value = _f(terminal_leg.get("cumExitValue"))
        open_fee = _f(terminal_leg.get("openFee"))
        close_fee = _f(terminal_leg.get("closeFee"))
        closed_pnl = _f(terminal_leg.get("closedPnl"))
        trade.venue_terminal_qty = terminal_qty
        trade.venue_terminal_closed_pnl_usdt = closed_pnl
        if trade.planned_exit_qty is not None and terminal_qty is not None:
            trade.venue_qty_gap = terminal_qty - trade.planned_exit_qty

        terminal_price_net_bps = None
        if (
            terminal_qty is not None
            and terminal_qty > 0.0
            and entry_value is not None
            and entry_value > 0.0
            and exit_value is not None
            and open_fee is not None
            and close_fee is not None
        ):
            price_fee_pnl = exit_value - entry_value - open_fee - close_fee
            terminal_price_net_bps = price_fee_pnl / entry_value * 1e4
            trade.venue_terminal_entry_value_usdt = entry_value
            trade.venue_terminal_price_fee_pnl_usdt = price_fee_pnl
            trade.venue_terminal_price_fee_net_bps = terminal_price_net_bps
            if closed_pnl is not None:
                trade.venue_closed_pnl_residual_usdt = closed_pnl - price_fee_pnl

        position = _position_settlements(terminal_leg, venue_transactions)
        trade.venue_position_reconstruction = (
            position.status if position.reason is None else f"{position.status}: {position.reason}"
        )
        trade.venue_position_open_ts_ms = position.open_ts_ms
        trade.venue_position_close_ts_ms = position.close_ts_ms
        trade.venue_position_open_order_id = position.open_order_id
        trade.venue_position_close_order_id = position.close_order_id
        trade.venue_position_open_qty = position.open_qty
        trade.venue_position_close_qty = position.close_qty
        trade.venue_position_trade_rows = position.trade_rows
        trade.venue_position_resizes = position.resizes
        trade.venue_settlement_rows = len(position.settlement_rows)
        trade.venue_settlement_usdt = position.settlement_usdt
        if trade.venue_closed_pnl_residual_usdt is not None and position.settlement_usdt is not None:
            trade.venue_closed_pnl_unexplained_usdt = trade.venue_closed_pnl_residual_usdt - position.settlement_usdt

        if trade.entry_px is None:
            entry_px = _f(terminal_leg.get("avgEntryPrice"))
            if entry_px is None and entry_value is not None and terminal_qty:
                entry_px = entry_value / terminal_qty
            if entry_px is not None:
                trade.entry_px = entry_px
                trade.entry_px_source = "venue_history_terminal_ambiguous"
        if trade.exit_px is None:
            exit_px = _f(terminal_leg.get("avgExitPrice"))
            if exit_px is None and exit_value is not None and terminal_qty:
                exit_px = exit_value / terminal_qty
            if exit_px is not None:
                trade.exit_px = exit_px
                trade.exit_px_source = "venue_history_terminal_ambiguous"
        if trade.net_bps is not None and terminal_price_net_bps is not None:
            trade.engine_venue_net_gap_bps = trade.net_bps - terminal_price_net_bps
        trade.venue_engine_position_link = _engine_position_link(trade, terminal_leg)
        if (
            trade.venue_engine_position_link == "exact_long_sleeve"
            and trade.venue_settlement_usdt is not None
            and trade.engine_entry_notional_usdt is not None
            and trade.engine_entry_notional_usdt > 0.0
        ):
            trade.live_funding_bps = trade.venue_settlement_usdt / trade.engine_entry_notional_usdt * 1e4
            if trade.net_bps is not None:
                trade.live_net_including_funding_bps = trade.net_bps + trade.live_funding_bps
        if trade.exit_ts_ms is None and terminal_ts is not None:
            trade.exit_ts_ms = terminal_ts
            trade.exit_ts_source = "venue_history_terminal_ambiguous"
        if trade.planned_exit_qty is not None and terminal_qty is not None:
            qty_note = (
                f"planned qty {trade.planned_exit_qty:g}, venue qty {terminal_qty:g}; no direct producer order link"
            )
        else:
            qty_note = "no producer-to-venue quantity link"
        trade.notes.append(
            f"{len(matched)} venue close candidate(s); nearest row is an ambiguous price backstop ({qty_note})"
        )
        if trade.venue_settlement_rows:
            if trade.venue_settlement_usdt is None:
                trade.notes.append(
                    f"{trade.venue_settlement_rows} venue-position settlement row(s) found, but their USDT value is incomplete"
                )
            else:
                trade.notes.append(
                    f"{trade.venue_settlement_rows} explicit venue-position settlement row(s) sum to "
                    f"{trade.venue_settlement_usdt:.8g} USDT; "
                    + (
                        "the engine round trip links this position to the long sleeve"
                        if trade.venue_engine_position_link == "exact_long_sleeve"
                        else "sleeve ownership remains unlinked"
                    )
                )
        elif trade.venue_position_reconstruction and trade.venue_position_reconstruction.startswith("incomplete"):
            trade.notes.append(f"venue-position reconstruction {trade.venue_position_reconstruction}")
        if trade.net_bps is None:
            trade.notes.append("venue-only net withheld because the close row is not linked to this trade")

    return trades


def window_filter_live(
    trades: list[LiveTrade], *, start_ms: int, end_ms: int
) -> tuple[list[LiveTrade], list[LiveTrade]]:
    """Keep trades whose signal (or, failing that, request, entry, then exit) is in the window."""
    kept: list[LiveTrade] = []
    dropped: list[LiveTrade] = []
    for trade in trades:
        basis = trade.signal_ts_ms or trade.request_ts_ms or trade.entry_ts_ms or trade.exit_ts_ms
        if basis is not None and start_ms <= basis < end_ms:
            kept.append(trade)
        else:
            dropped.append(trade)
    return kept, dropped


def run_model(data_root: str, profile: str, start: str, end: str, warmup_days: int, out_dir: Path) -> Path:
    """Replay the registered LONG rule and return the trade ledger CSV path.

    The kernel starts with no held positions and an empty cooldown map, so the
    run begins warmup_days before the window; grading later keeps only trades
    whose signal is inside the window.
    """
    from dataclasses import replace

    from liquidity_migration.research.backtest.long_native import run_long_native_research
    from liquidity_migration.rules.long_native import resolve_long_strategy_profile

    warmup_start = (dt.date.fromisoformat(start) - dt.timedelta(days=warmup_days)).isoformat()
    config = replace(resolve_long_strategy_profile(profile), start_date=warmup_start, end_date=end)
    result = run_long_native_research(data_root, config=config, report_dir=out_dir)
    ledger = Path(str(result["report_dir"])) / "long_native_trades.csv"
    if not ledger.exists():
        raise SystemExit(f"model run produced no trade ledger at {ledger}")
    return ledger


def load_model_trades(path: Path, *, start_ms: int, end_ms: int, margin_ms: int = 0) -> list[dict]:
    """Model ledger rows whose signal is inside [start_ms - margin_ms, end_ms),
    with returns restated per unit of the trade's own notional.

    Rows whose signal falls in the margin before the window carry
    in_window=False: they may pair with a live trade at the window boundary
    (daily signals sit exactly one day apart) but are never graded as
    model-only misses.

    The ledger's net_return is book-weighted (position weight times the deployed
    multiplier) and includes funding. The live engine's net_bps is per unit of
    the trade's own entry notional and excludes funding. The comparable model
    number is therefore model_price_net_bps = (gross_trade_return +
    cost_return / notional_weight) * 1e4, with funding restated the same way
    into model_funding_bps and shown separately.
    """
    rows: list[dict] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            signal_ts = _i(raw.get("entry_signal_ts_ms"))
            if signal_ts is None or not (start_ms - margin_ms <= signal_ts < end_ms):
                continue
            weight = _f(raw.get("notional_weight")) or 0.0
            gross = _f(raw.get("gross_trade_return"))
            cost = _f(raw.get("cost_return"))
            funding = _f(raw.get("funding_return"))
            net = _f(raw.get("net_return"))
            price_net_bps = None
            if gross is not None and cost is not None and weight > 0.0:
                per_unit_cost = cost / weight
                price_net_bps = (gross + per_unit_cost) * 1e4
            funding_bps = (funding / weight * 1e4) if (funding is not None and weight > 0.0) else None
            rows.append(
                {
                    "trade_id": raw.get("trade_id"),
                    "symbol": str(raw.get("symbol") or "").upper(),
                    "pattern": raw.get("pattern"),
                    "in_window": signal_ts >= start_ms,
                    "entry_signal_ts_ms": signal_ts,
                    "entry_ts_ms": _i(raw.get("entry_ts_ms")),
                    "exit_ts_ms": _i(raw.get("exit_ts_ms")),
                    "entry_price": _f(raw.get("entry_price")),
                    "exit_price": _f(raw.get("exit_price")),
                    "exit_reason": raw.get("exit_reason"),
                    "notional_weight": weight,
                    "model_price_net_bps": price_net_bps,
                    "model_funding_bps": funding_bps,
                    "model_net_bps_book": (net * 1e4) if net is not None else None,
                }
            )
    return rows


def pair_trades(
    model_rows: list[dict],
    live_trades: list[LiveTrade],
    *,
    window_ms: float,
    context_trades: list[LiveTrade] | None = None,
) -> Cohorts:
    """Pair model and live trades on (symbol, signal timestamp), then two
    fallbacks within window_ms: nearby signals (daily signals sit exactly one
    day apart, so a trade taken one bar late still pairs), then nearby entry
    times for live trades whose signal is unknown. Gate entries never enter the
    pairing pool, and a margin model row that stays unpaired is dropped rather
    than graded as a miss."""
    gate = [trade for trade in live_trades if (trade.pattern or "") in GATE_PATTERNS]
    pool = [trade for trade in live_trades if (trade.pattern or "") not in GATE_PATTERNS]

    live_by_key: dict[tuple[str, int], list[LiveTrade]] = {}
    for trade in pool:
        if trade.signal_ts_ms is not None:
            live_by_key.setdefault((trade.symbol, trade.signal_ts_ms), []).append(trade)

    pairs: list[tuple[dict, LiveTrade, str]] = []
    used_live: set[int] = set()
    used_model: set[int] = set()
    unmatched_model: list[dict] = []
    for model in model_rows:
        key = (model["symbol"], model["entry_signal_ts_ms"])
        matched = None
        for candidate in live_by_key.get(key, []):
            if id(candidate) not in used_live:
                matched = candidate
                break
        if matched is not None:
            used_live.add(id(matched))
            used_model.add(id(model))
            pairs.append((model, matched, "signal_key"))
        else:
            unmatched_model.append(model)

    def greedy(candidates: list[tuple[int, dict, LiveTrade]], method: str) -> None:
        for gap, model, trade in sorted(candidates, key=lambda item: item[0]):
            del gap
            if id(model) in used_model or id(trade) in used_live:
                continue
            used_model.add(id(model))
            used_live.add(id(trade))
            pairs.append((model, trade, method))

    signal_close: list[tuple[int, dict, LiveTrade]] = []
    for model in unmatched_model:
        for trade in pool:
            if id(trade) in used_live or trade.symbol != model["symbol"] or trade.signal_ts_ms is None:
                continue
            gap = abs(trade.signal_ts_ms - model["entry_signal_ts_ms"])
            if gap <= window_ms:
                signal_close.append((gap, model, trade))
    greedy(signal_close, "signal_proximity")

    entry_close: list[tuple[int, dict, LiveTrade]] = []
    for model in unmatched_model:
        if id(model) in used_model or model["entry_ts_ms"] is None:
            continue
        for trade in pool:
            if id(trade) in used_live or trade.symbol != model["symbol"]:
                continue
            if trade.signal_ts_ms is not None or trade.entry_ts_ms is None:
                continue
            gap = abs(trade.entry_ts_ms - model["entry_ts_ms"])
            if gap <= window_ms:
                entry_close.append((gap, model, trade))
    greedy(entry_close, "entry_proximity")

    model_only = [model for model in model_rows if id(model) not in used_model and model.get("in_window", True)]
    model_while_live_held: list[tuple[dict, LiveTrade]] = []
    for model in list(model_only):
        model_at = model.get("entry_ts_ms") or model.get("entry_signal_ts_ms")
        if model_at is None:
            continue
        candidates = []
        for trade in context_trades or []:
            if trade.symbol != model["symbol"]:
                continue
            opened = trade.entry_ts_ms or trade.request_ts_ms or trade.signal_ts_ms
            closed = trade.exit_ts_ms or trade.exit_request_ts_ms or trade.hold_deadline_ts_ms
            if opened is not None and closed is not None and opened <= model_at <= closed:
                candidates.append((opened, trade))
        if not candidates:
            continue
        _, held = max(candidates, key=lambda item: item[0])
        model_while_live_held.append((model, held))
        model_only.remove(model)
    live_only = [trade for trade in pool if id(trade) not in used_live]
    return Cohorts(
        pairs=pairs,
        model_while_live_held=model_while_live_held,
        model_only=model_only,
        live_only=live_only,
        gate=gate,
    )


def _blank_row() -> dict:
    return {column: None for column in CSV_COLUMNS}


def _fill_model(row: dict, model: dict) -> None:
    row["symbol"] = model["symbol"]
    row["model_trade_id"] = model.get("trade_id")
    row["model_signal_ts_ms"] = model.get("entry_signal_ts_ms")
    row["model_entry_ts_ms"] = model.get("entry_ts_ms")
    row["model_entry_px"] = model.get("entry_price")
    row["model_exit_ts_ms"] = model.get("exit_ts_ms")
    row["model_exit_px"] = model.get("exit_price")
    row["model_exit_reason"] = model.get("exit_reason")
    row["model_price_net_bps"] = model.get("model_price_net_bps")
    row["model_funding_bps"] = model.get("model_funding_bps")
    row["model_net_bps_book"] = model.get("model_net_bps_book")
    row["pattern"] = row["pattern"] or model.get("pattern")


def _fill_live(row: dict, trade: LiveTrade) -> None:
    row["symbol"] = trade.symbol
    row["pattern"] = trade.pattern or row["pattern"]
    row["live_trade_id"] = trade.trade_id
    row["live_signal_ts_ms"] = trade.signal_ts_ms
    row["live_request_ts_ms"] = trade.request_ts_ms
    row["live_entry_ts_ms"] = trade.entry_ts_ms
    row["live_entry_px"] = trade.entry_px
    row["live_exit_ts_ms"] = trade.exit_ts_ms
    row["live_exit_px"] = trade.exit_px
    row["live_exit_reason"] = trade.exit_reason
    row["live_net_bps"] = trade.net_bps
    row["live_entry_reason"] = trade.entry_reason
    row["live_planned_stop_pct"] = trade.planned_stop_pct
    row["live_planned_exit_qty"] = trade.planned_exit_qty
    row["live_notional_usdt"] = trade.notional_usdt
    row["live_request_ts_source"] = trade.request_ts_source
    row["live_entry_ts_source"] = trade.entry_ts_source
    row["live_entry_px_source"] = trade.entry_px_source
    row["live_exit_ts_source"] = trade.exit_ts_source
    row["live_exit_px_source"] = trade.exit_px_source
    row["live_exit_reason_source"] = trade.exit_reason_source
    row["live_net_bps_source"] = trade.net_bps_source
    row["live_funding_bps"] = trade.live_funding_bps
    row["live_net_including_funding_bps"] = trade.live_net_including_funding_bps
    row["live_exit_request_ts_ms"] = trade.exit_request_ts_ms
    row["live_exit_request_ts_source"] = trade.exit_request_ts_source
    row["venue_rows"] = trade.venue_rows or None
    row["venue_terminal_gap_ms"] = trade.venue_terminal_gap_ms
    row["venue_terminal_qty"] = trade.venue_terminal_qty
    row["venue_qty_gap"] = trade.venue_qty_gap
    row["venue_terminal_closed_pnl_usdt"] = trade.venue_terminal_closed_pnl_usdt
    row["venue_terminal_price_fee_pnl_usdt"] = trade.venue_terminal_price_fee_pnl_usdt
    row["venue_closed_pnl_residual_usdt"] = trade.venue_closed_pnl_residual_usdt
    row["venue_position_reconstruction"] = trade.venue_position_reconstruction
    row["venue_position_open_ts_ms"] = trade.venue_position_open_ts_ms
    row["venue_position_close_ts_ms"] = trade.venue_position_close_ts_ms
    row["venue_position_open_order_id"] = trade.venue_position_open_order_id
    row["venue_position_close_order_id"] = trade.venue_position_close_order_id
    row["venue_position_open_qty"] = trade.venue_position_open_qty
    row["venue_position_close_qty"] = trade.venue_position_close_qty
    row["venue_position_trade_rows"] = trade.venue_position_trade_rows or None
    row["venue_position_resizes"] = trade.venue_position_resizes
    row["venue_settlement_rows"] = trade.venue_settlement_rows or None
    row["venue_settlement_usdt"] = trade.venue_settlement_usdt
    row["venue_closed_pnl_unexplained_usdt"] = trade.venue_closed_pnl_unexplained_usdt
    row["venue_engine_position_link"] = trade.venue_engine_position_link
    row["venue_terminal_price_fee_net_bps"] = trade.venue_terminal_price_fee_net_bps
    row["engine_venue_net_gap_bps"] = trade.engine_venue_net_gap_bps
    row["venue_terminal_entry_value_usdt"] = trade.venue_terminal_entry_value_usdt
    row["notes"] = "; ".join(trade.notes) if trade.notes else row["notes"]


def build_rows(cohorts: Cohorts) -> list[dict]:
    """Flatten every cohort into one CSV row schema, gaps and slippage computed
    per pair. Sign convention: positive slippage means live paid more — bought
    higher on entry, sold lower on exit. Gaps are live minus model."""
    rows: list[dict] = []
    for model, trade, how in cohorts.pairs:
        row = _blank_row()
        row["cohort"] = "paired"
        row["pair_method"] = how
        _fill_model(row, model)
        _fill_live(row, trade)
        if row["model_entry_ts_ms"] is not None and trade.entry_ts_ms is not None:
            row["entry_gap_ms"] = trade.entry_ts_ms - row["model_entry_ts_ms"]
        if row["model_exit_ts_ms"] is not None and trade.exit_ts_ms is not None:
            row["exit_gap_ms"] = trade.exit_ts_ms - row["model_exit_ts_ms"]
        if (
            row["model_entry_px"]
            and trade.entry_px is not None
            and not str(trade.entry_px_source or "").endswith("_ambiguous")
        ):
            row["entry_slippage_bps"] = (trade.entry_px - row["model_entry_px"]) / row["model_entry_px"] * 1e4
        if (
            row["model_exit_px"]
            and trade.exit_px is not None
            and not str(trade.exit_px_source or "").endswith("_ambiguous")
        ):
            row["exit_slippage_bps"] = (row["model_exit_px"] - trade.exit_px) / row["model_exit_px"] * 1e4
        if row["model_price_net_bps"] is not None and trade.net_bps is not None:
            row["gap_bps"] = trade.net_bps - row["model_price_net_bps"]
        if model.get("exit_reason") == "take_profit" and (trade.exit_reason or "") != "take_profit":
            row["structural"] = STRUCTURAL_NO_LIVE_TP
        if not model.get("in_window", True):
            margin_note = "model signal falls just before the window start; paired on adjacent daily signals"
            row["notes"] = f"{row['notes']}; {margin_note}" if row["notes"] else margin_note
        rows.append(row)
    for model, trade in cohorts.model_while_live_held:
        row = _blank_row()
        row["cohort"] = "model_while_live_held"
        row["structural"] = STRUCTURAL_LIVE_ALREADY_HELD
        _fill_model(row, model)
        _fill_live(row, trade)
        row["notes"] = (
            f"live already held {trade.symbol} across the model entry; this is state/path divergence, "
            "not a missed execution"
        )
        rows.append(row)
    for model in cohorts.model_only:
        row = _blank_row()
        row["cohort"] = "model_only"
        _fill_model(row, model)
        row["notes"] = "the live book never took this trade"
        rows.append(row)
    for trade in cohorts.live_only:
        row = _blank_row()
        row["cohort"] = "live_only"
        _fill_live(row, trade)
        extra = "investigate: no model trade explains this live entry"
        row["notes"] = f"{row['notes']}; {extra}" if row["notes"] else extra
        rows.append(row)
    for trade in cohorts.gate:
        row = _blank_row()
        row["cohort"] = "live_only_gate"
        row["structural"] = STRUCTURAL_GATE_ONLY
        _fill_live(row, trade)
        rows.append(row)
    return rows


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(0.9 * len(ordered)) - 1))
    return ordered[index]


def _sum_or_none(values: list[float]) -> float | None:
    return sum(values) if values else None


def summarize(rows: list[dict]) -> dict:
    """The weekly numbers: gap and slippage stats over pairs, and the
    live-minus-model total split into entry, exit, structural, and residual.

    Slippage is an execution measure, so its stats and components only use
    pairs matched on the exact signal key with no structural flag. A pair
    matched on nearby signals or entries took a different trigger; its whole
    gap lands in the structural/selection bucket alongside the model
    take-profits the live path does not have.
    """
    paired = [row for row in rows if row["cohort"] == "paired"]
    execution = [row for row in paired if not row["structural"] and row["pair_method"] == "signal_key"]
    execution_ids = {id(row) for row in execution}
    structural_rows = [row for row in paired if id(row) not in execution_ids]
    model_only = [row for row in rows if row["cohort"] == "model_only"]
    model_while_live_held = [row for row in rows if row["cohort"] == "model_while_live_held"]
    live_only = [row for row in rows if row["cohort"] == "live_only"]
    gate = [row for row in rows if row["cohort"] == "live_only_gate"]

    entry_gaps = [abs(row["entry_gap_ms"]) for row in execution if row["entry_gap_ms"] is not None]
    entry_slips = [row["entry_slippage_bps"] for row in execution if row["entry_slippage_bps"] is not None]
    exit_gaps = [abs(row["exit_gap_ms"]) for row in execution if row["exit_gap_ms"] is not None]
    exit_slips = [row["exit_slippage_bps"] for row in execution if row["exit_slippage_bps"] is not None]

    clean_gaps = [row["gap_bps"] for row in execution if row["gap_bps"] is not None]
    all_gaps = [row["gap_bps"] for row in paired if row["gap_bps"] is not None]
    structural_gaps = [row["gap_bps"] for row in structural_rows if row["gap_bps"] is not None]
    gradeable_execution = [row for row in execution if row["gap_bps"] is not None]
    gradeable_entry_slips = [
        row["entry_slippage_bps"] for row in gradeable_execution if row["entry_slippage_bps"] is not None
    ]
    gradeable_exit_slips = [
        row["exit_slippage_bps"] for row in gradeable_execution if row["exit_slippage_bps"] is not None
    ]
    entry_component = -sum(gradeable_entry_slips) if gradeable_entry_slips else None
    exit_component = -sum(gradeable_exit_slips) if gradeable_exit_slips else None
    structural_bps = _sum_or_none(structural_gaps)
    residual = None
    if clean_gaps:
        residual = sum(clean_gaps) - (entry_component or 0.0) - (exit_component or 0.0)
    paired_total = _sum_or_none(all_gaps)
    model_only_bps = _sum_or_none(
        [row["model_price_net_bps"] for row in model_only if row["model_price_net_bps"] is not None]
    )
    live_only_bps = _sum_or_none([row["live_net_bps"] for row in live_only + gate if row["live_net_bps"] is not None])
    linked_live_only = [row for row in live_only + gate if row["live_net_including_funding_bps"] is not None]

    return {
        "n_pairs": len(paired),
        "n_paired_net": len(all_gaps),
        "n_structural_pairs": len(structural_rows),
        "n_gate": len(gate),
        "n_live_only": len(live_only),
        "n_model_only": len(model_only),
        "n_model_while_live_held": len(model_while_live_held),
        "entry_gap_ms_median": _median(entry_gaps),
        "entry_gap_ms_p90": _p90(entry_gaps),
        "entry_gap_n": len(entry_gaps),
        "entry_slip_bps_median": _median(entry_slips),
        "entry_slip_bps_p90": _p90(entry_slips),
        "entry_slip_n": len(entry_slips),
        "exit_gap_ms_median": _median(exit_gaps),
        "exit_gap_ms_p90": _p90(exit_gaps),
        "exit_gap_n": len(exit_gaps),
        "exit_slip_bps_median": _median(exit_slips),
        "exit_slip_bps_p90": _p90(exit_slips),
        "exit_slip_n": len(exit_slips),
        "paired_total_bps": paired_total,
        "entry_component_bps": entry_component,
        "exit_component_bps": exit_component,
        "structural_bps": structural_bps,
        "residual_bps": residual,
        "model_only_bps": model_only_bps,
        "model_while_live_held_bps": _sum_or_none(
            [row["model_price_net_bps"] for row in model_while_live_held if row["model_price_net_bps"] is not None]
        ),
        "live_only_bps": live_only_bps,
        "n_live_only_funding_linked": len(linked_live_only),
        "linked_live_only_funding_bps": _sum_or_none(
            [row["live_funding_bps"] for row in linked_live_only if row["live_funding_bps"] is not None]
        ),
        "linked_live_only_all_in_bps": _sum_or_none(
            [row["live_net_including_funding_bps"] for row in linked_live_only]
        ),
    }


def _verdict(summary: dict) -> str:
    if summary["n_pairs"] == 0:
        unpaired = (
            summary["n_model_only"] + summary["n_model_while_live_held"] + summary["n_live_only"] + summary["n_gate"]
        )
        if unpaired == 0:
            return "Nothing to grade: neither the model nor the live book traded in this window."
        return (
            f"No paired trades this window: the model fired {summary['n_model_only']} trade(s) "
            f"the live book never took, {summary['n_model_while_live_held']} met a position live already held, "
            f"and the live book holds {summary['n_live_only'] + summary['n_gate']} "
            "trade(s) the model does not explain."
        )
    if summary["n_paired_net"] == 0 or summary["paired_total_bps"] is None:
        return (
            f"{summary['n_pairs']} trade(s) paired, but none has comparable net return evidence; "
            "the price-and-fee gap is not gradeable."
        )
    total = float(summary["paired_total_bps"])
    pieces = {
        "entry slippage": summary["entry_component_bps"],
        "exit slippage": summary["exit_component_bps"],
        "structural and selection differences (no live take-profit; different trigger day)": summary["structural_bps"],
        "fees against the modeled cost, and the leftover": summary["residual_bps"],
    }
    pieces = {name: value for name, value in pieces.items() if value is not None}
    name, value = max(pieces.items(), key=lambda item: abs(item[1]))
    direction = "behind" if total < 0 else "ahead of"
    return (
        f"Live finished {abs(total):.0f} bp {direction} the model across {summary['n_pairs']} paired "
        f"trade(s); the biggest piece is {name} ({value:+.0f} bp)."
    )


def _fmt(value: float | None, unit: str = "", scale: float = 1.0, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value / scale:,.{digits}f}{unit}"


def _iso_ms(value: int | None) -> str:
    if value is None:
        return "n/a"
    return dt.datetime.fromtimestamp(value / 1000.0, tz=dt.timezone.utc).isoformat(timespec="milliseconds")


def render_summary_text(summary: dict, start: str, end: str) -> str:
    minutes = MS_PER_HOUR / 60.0
    lines = [
        f"LONG live vs model parity  {start} -> {end}",
        (
            f"pairs: {summary['n_pairs']} ({summary['n_paired_net']} net-gradeable; "
            f"{summary['n_structural_pairs']} structural)   "
            f"live-only gate: {summary['n_gate']}   live-only (investigate): {summary['n_live_only']}   "
            f"model/live-held: {summary['n_model_while_live_held']}   model-only (missed): {summary['n_model_only']}"
        ),
        (
            f"entry gap   median {_fmt(summary['entry_gap_ms_median'], ' min', minutes)}"
            f"  p90 {_fmt(summary['entry_gap_ms_p90'], ' min', minutes)}   (n={summary['entry_gap_n']})"
        ),
        (
            f"entry slip  median {_fmt(summary['entry_slip_bps_median'], ' bp')}"
            f"  p90 {_fmt(summary['entry_slip_bps_p90'], ' bp')}   (n={summary['entry_slip_n']}, "
            "exact-key clean pairs only)"
        ),
        (
            f"exit gap    median {_fmt(summary['exit_gap_ms_median'], ' h', MS_PER_HOUR)}"
            f"  p90 {_fmt(summary['exit_gap_ms_p90'], ' h', MS_PER_HOUR)}   (n={summary['exit_gap_n']})"
        ),
        (
            f"exit slip   median {_fmt(summary['exit_slip_bps_median'], ' bp')}"
            f"  p90 {_fmt(summary['exit_slip_bps_p90'], ' bp')}   (n={summary['exit_slip_n']}, "
            "exact-key clean pairs only)"
        ),
        f"paired live-minus-model total: {_fmt(summary['paired_total_bps'], ' bp')}",
        f"  from entry slippage:  {_fmt(summary['entry_component_bps'], ' bp')}",
        f"  from exit slippage:   {_fmt(summary['exit_component_bps'], ' bp')}",
        f"  structural/selection (no live take-profit; different trigger day): {_fmt(summary['structural_bps'], ' bp')}",
        f"  fees vs modeled cost, and rounding: {_fmt(summary['residual_bps'], ' bp')}",
        f"model-only trades the live book missed: {_fmt(summary['model_only_bps'], ' bp')} of model net",
        (
            "model entries blocked by a prior live position: "
            f"{_fmt(summary['model_while_live_held_bps'], ' bp')} on the model path"
        ),
        f"live-only trades (gate included): {_fmt(summary['live_only_bps'], ' bp')} of price-plus-fee net",
        (
            f"  linked crowd fee: {_fmt(summary['linked_live_only_funding_bps'], ' bp')}; "
            f"linked all-in: {_fmt(summary['linked_live_only_all_in_bps'], ' bp')} "
            f"(n={summary['n_live_only_funding_linked']})"
        ),
        f"verdict: {_verdict(summary)}",
    ]
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        # Six significant digits keep sub-cent prices readable next to bp values.
        return f"{value:,.6g}"
    return str(value)


def _md_table(rows: list[dict], columns: list[tuple[str, str]]) -> list[str]:
    if not rows:
        return ["(none)", ""]
    lines = ["| " + " | ".join(title for title, _ in columns) + " |"]
    lines.append("|" + "|".join(" --- " for _ in columns) + "|")
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(row.get(key)) for _, key in columns) + " |")
    lines.append("")
    return lines


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return {"path": str(resolved), "exists": False}
    return {
        "path": str(resolved),
        "exists": True,
        "bytes": resolved.stat().st_size,
        "sha256": _sha256_file(resolved),
    }


def _cycle_directory_identity(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    files = sorted(resolved.glob("long_native_cycle_*.json")) if resolved.is_dir() else []
    digest = hashlib.sha256()
    total_bytes = 0
    for payload_path in files:
        digest.update(payload_path.name.encode("utf-8"))
        digest.update(b"\0")
        with payload_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                total_bytes += len(chunk)
                digest.update(chunk)
        digest.update(b"\0")
    return {
        "path": str(resolved),
        "exists": resolved.is_dir(),
        "files": len(files),
        "bytes": total_bytes,
        "sha256": digest.hexdigest() if files else None,
    }


def _git_head() -> str | None:
    env = os.environ.copy()
    for name in GIT_LOCAL_ENV_VARS:
        env.pop(name, None)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _cycle_metadata(cycles: CycleEvidence | None) -> dict[str, Any]:
    if cycles is None:
        return {}
    return {
        "cycle_count": cycles.cycle_count,
        "malformed_files": cycles.malformed_files,
        "first_cycle_ts_ms": cycles.first_cycle_ts_ms,
        "last_cycle_ts_ms": cycles.last_cycle_ts_ms,
        "strategy_ids": list(cycles.strategy_ids),
        "strategy_profiles": list(cycles.strategy_profiles),
        "config_changes": list(cycles.config_changes),
    }


def render_markdown(
    *,
    start: str,
    end: str,
    warmup_days: int,
    profile: str,
    rows: list[dict],
    summary: dict,
    outside_window: list[LiveTrade],
    uncorroborated_candidates: int,
    inputs: dict[str, str],
    cycles: CycleEvidence | None = None,
    model_meta: dict[str, Any] | None = None,
    model_run_label: str | None = None,
    model_commit: str | None = None,
    model_commit_verified: bool = False,
    provenance_name: str = "long_live_vs_model_provenance.json",
) -> str:
    model_meta = model_meta or {}
    paired = [row for row in rows if row["cohort"] == "paired"]
    model_while_live_held = [row for row in rows if row["cohort"] == "model_while_live_held"]
    model_only = [row for row in rows if row["cohort"] == "model_only"]
    live_only = [row for row in rows if row["cohort"] == "live_only"]
    gate = [row for row in rows if row["cohort"] == "live_only_gate"]
    rotation_nulls = sum(1 for row in rows if "journal rotation" in (row["notes"] or ""))
    venue_backed = sum(1 for row in rows if row["venue_rows"])
    reconciled_venue_positions = sum(
        1
        for row in rows
        if row["venue_position_reconstruction"] == "exact_one_way_position"
        and row["venue_settlement_usdt"] is not None
        and row["venue_closed_pnl_unexplained_usdt"] is not None
        and abs(row["venue_closed_pnl_unexplained_usdt"]) <= MONEY_TOLERANCE_USDT
    )
    linked_venue_positions = sum(1 for row in rows if row["venue_engine_position_link"] == "exact_long_sleeve")
    tainted_model = bool(model_meta.get("tainted")) or model_meta.get("methodology_run_label") == "invalid"
    validity = (
        "diagnostic only: the model replay fails the full point-in-time membership gate"
        if tainted_model
        else "limited execution reconciliation"
    )
    observed_multipliers = sorted(
        {
            float(change["notional_multiplier"])
            for change in (cycles.config_changes if cycles else ())
            if change.get("notional_multiplier") is not None
        }
    )
    multiplier_text = ", ".join(f"{value:g}x" for value in observed_multipliers) or "not recoverable"
    strategy_ids = ", ".join(cycles.strategy_ids) if cycles and cycles.strategy_ids else "not recoverable"

    lines = [
        "# LONG: live against the model, pair by pair",
        "",
        f"Window {start} to {end} (end exclusive), model profile `{profile}`.",
        "",
        "## Evidence card",
        "",
        "- Claim and decision: identify why the demo LONG path differed from the registered model",
        "  during this window; this informs parity debugging, not an alpha or deployment verdict.",
        f"- Validity: **{validity}**.",
        "- Shaped versus graded: the v12 rule was shaped before this window, but this matching and",
        "  decomposition were built after seeing these live records. Treat the reconciliation as",
        "  exploratory even where the underlying days postdate the v12 config commit.",
        f"- Scope: Bybit demo, per-trade return on entry notional, strategy `{strategy_ids}`,",
        f"  {summary['n_pairs']} pair(s), {summary['n_model_while_live_held']} model/live-held,",
        f"  {summary['n_model_only']} model-only,",
        f"  {summary['n_live_only'] + summary['n_gate']} live-only.",
        f"- Effect: {_verdict(summary)}",
        (
            f"- Identities: model source commit `{model_commit or 'not recorded'}` "
            f"({'verified by this invocation' if model_commit_verified else 'operator-supplied, not independently bound to the ledger'});"
        ),
        f"  hashes and argv are in `{provenance_name}`.",
        "- Does not show: complete point-in-time population parity, paired ENA crowd-fee or net parity,",
        "  complete all-trade funding attribution, mainnet behavior, or account authority.",
        "",
        "## How to read this",
        "",
        f"- The model was re-run from {warmup_days} days before the window because the kernel is a",
        "  cold start: it begins with no held positions and an empty cooldown map. Only model trades",
        "  whose signal falls inside the window are graded here.",
        "- Trades pair on (symbol, signal timestamp). When the keys differ, same-symbol trades whose",
        "  signals sit within the fallback window pair by closeness (daily signals are exactly one day",
        "  apart, so a trade taken one bar late still pairs and the timing gap shows in the row); live",
        "  trades with no recoverable signal pair on entry-time closeness instead. Model trades from one",
        "  fallback window before the start take part in pairing but are never graded as model-only.",
        "- A model entry that occurs while the same symbol is already open in the live book is a",
        "  state/path divergence. It is shown separately from a missed live execution.",
        "- Slippage is signed so that positive means live did worse: bought higher on entry, sold",
        "  lower on exit. Time gaps are live minus model.",
        "- Returns compare in bp of each trade's own entry notional, never in dollars. The model",
        f"  ledger uses 1x while retained live cycles show {multiplier_text}; dollar P&L is not comparable.",
        "- The live number is the engine's net (price plus fees). The model's comparable number strips",
        "  its funding term out; the crowd fee (funding) the model expected is shown separately in",
        "  `model_funding_bps`. When the engine journal rotated, the nearest venue close is kept",
        "  only as an ambiguous price backstop. Its price-plus-fee reconstruction, `closedPnl`,",
        "  and their residual stay separate; none is attributed as this trade's net return without",
        "  a quantity or order link. When the transaction log proves a flat-to-flat one-way venue",
        "  position, explicit settlement rows explain that position's residual. Only an exact match",
        "  to the engine journal attributes those settlements to the long sleeve; linked funding and",
        "  all-in net are reported separately from the engine's price-plus-fee net.",
        "- Expected, structural differences are bucketed apart from slippage: the model takes a",
        "  4xATR take-profit while the live path has none, and the LLM gate opens trades the",
        "  kernel never sees.",
        "",
        "## Summary",
        "",
        "```",
        render_summary_text(summary, start, end),
        "```",
        "",
        "## Paired trades",
        "",
    ]
    lines += _md_table(
        paired,
        [
            ("symbol", "symbol"),
            ("pairing", "pair_method"),
            ("structural", "structural"),
            ("entry gap ms", "entry_gap_ms"),
            ("entry slip bp", "entry_slippage_bps"),
            ("exit gap ms", "exit_gap_ms"),
            ("exit slip bp", "exit_slippage_bps"),
            ("model exit", "model_exit_reason"),
            ("live exit", "live_exit_reason"),
            ("model net bp", "model_price_net_bps"),
            ("live net bp", "live_net_bps"),
            ("live funding bp", "live_funding_bps"),
            ("live all-in bp", "live_net_including_funding_bps"),
            ("gap bp", "gap_bps"),
            ("live net source", "live_net_bps_source"),
            ("venue candidates", "venue_rows"),
            ("planned qty", "live_planned_exit_qty"),
            ("venue qty", "venue_terminal_qty"),
            ("venue closed-PnL residual $", "venue_closed_pnl_residual_usdt"),
            ("venue settlement $", "venue_settlement_usdt"),
            ("unexplained $", "venue_closed_pnl_unexplained_usdt"),
        ],
    )
    lines += ["## Model entries blocked by a prior live position", ""]
    lines += _md_table(
        model_while_live_held,
        [
            ("symbol", "symbol"),
            ("model signal ts", "model_signal_ts_ms"),
            ("model entry ts", "model_entry_ts_ms"),
            ("live entry ts", "live_entry_ts_ms"),
            ("live exit ts", "live_exit_ts_ms"),
            ("model net bp", "model_price_net_bps"),
            ("structural", "structural"),
            ("notes", "notes"),
        ],
    )
    lines += ["## Model-only: trades the live book never took", ""]
    lines += _md_table(
        model_only,
        [
            ("symbol", "symbol"),
            ("signal ts", "model_signal_ts_ms"),
            ("entry px", "model_entry_px"),
            ("exit px", "model_exit_px"),
            ("exit reason", "model_exit_reason"),
            ("model net bp", "model_price_net_bps"),
        ],
    )
    lines += ["## Live-only, gate cohort (structural: the gate does not exist in the kernel)", ""]
    lines += _md_table(
        gate,
        [
            ("symbol", "symbol"),
            ("pattern", "pattern"),
            ("signal ts", "live_signal_ts_ms"),
            ("entry px", "live_entry_px"),
            ("exit px", "live_exit_px"),
            ("live net bp", "live_net_bps"),
            ("live funding bp", "live_funding_bps"),
            ("live all-in bp", "live_net_including_funding_bps"),
        ],
    )
    lines += ["## Live-only, unexplained (investigate)", ""]
    lines += _md_table(
        live_only,
        [
            ("symbol", "symbol"),
            ("signal ts", "live_signal_ts_ms"),
            ("entry px", "live_entry_px"),
            ("exit px", "live_exit_px"),
            ("live net bp", "live_net_bps"),
            ("notes", "notes"),
        ],
    )
    lines += ["## What the inputs could not support", ""]
    if model_run_label:
        lines.append(
            f"- The model replay's own data-integrity label is `{model_run_label}`. When the label says"
            " the point-in-time membership fell back to the current universe, newly listed or delisted"
            " names can be missing from the model side over this window."
        )
    if rotation_nulls:
        lines.append(
            f"- {rotation_nulls} live trade(s) have no entry facts in the engine journal"
            " (round_trip null: the entry predates a log rotation)."
        )
    if venue_backed:
        lines.append(
            f"- {venue_backed} live trade(s) lean on venue closed-pnl rows. The matched-close candidate"
            " count is shown, but only the row nearest the expected exit supplies an ambiguous"
            " price backstop. Its price-plus-fee P&L is diagnostic only and is not attributed as the"
            " live trade's net without quantity or order linkage. The venue does not tag sleeves, so"
            " neighbouring activity can still blur the row."
        )
        lines.append(
            f"- {reconciled_venue_positions} matched close(s) have an exact flat-to-flat transaction-log"
            f" path whose explicit settlement sum reconciles the venue-position closed-PnL residual"
            f" within {MONEY_TOLERANCE_USDT:g} USDT;"
            " this reconstruction assumes the one-way account mode enforced by the engine because the"
            " transaction rows omit `positionIdx`. It is account-position evidence, not sleeve ownership."
        )
        lines.append(
            f"- {linked_venue_positions} reconstructed position(s) also match a long-sleeve engine round"
            " trip on open/close time, quantity, fill count, prices, entry notional, and price-plus-fee"
            " P&L. Their settlements are sleeve-attributed; the remaining reconstructed positions are not."
        )
    if outside_window:
        names = ", ".join(f"{t.symbol} ({t.trade_id or 'no id'})" for t in outside_window)
        lines.append(f"- {len(outside_window)} live trade(s) fall outside the window and were set aside: {names}.")
    if uncorroborated_candidates:
        lines.append(
            f"- {uncorroborated_candidates} cycle candidate(s) show no entry evidence anywhere and were"
            " not counted as live trades."
        )
    if cycles:
        lines.append(
            f"- Cycle evidence covers {cycles.cycle_count} direct payload(s),"
            f" {_iso_ms(cycles.first_cycle_ts_ms)} through {_iso_ms(cycles.last_cycle_ts_ms)};"
            f" malformed payloads: {cycles.malformed_files}."
        )
    lines.append(
        "- The transitions log only exists since 2026-08-30, so earlier live entries are recovered"
        " from cycle payloads, the engine journal, and venue history instead."
    )
    if cycles and cycles.config_changes:
        lines += ["", "## Observed live config changes", ""]
        change_rows = [
            {
                **change,
                "when": _iso_ms(_i(change.get("ts_ms"))),
            }
            for change in cycles.config_changes
        ]
        lines += _md_table(
            change_rows,
            [
                ("UTC", "when"),
                ("strategy", "strategy_id"),
                ("notional multiplier", "notional_multiplier"),
                ("entry leverage", "entry_leverage"),
                ("operational profile SHA-256", "operational_profile_sha256"),
            ],
        )
    lines += ["", "## Inputs", ""]
    for name, value in inputs.items():
        lines.append(f"- {name}: {value}")
    lines.append("")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="window start date (UTC, inclusive, on signal timestamps)")
    parser.add_argument("--end", required=True, help="window end date (UTC, exclusive)")
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help="point-in-time store the model replays")
    parser.add_argument("--profile", default="v12", help="registered LONG profile to replay (v11a or v12)")
    parser.add_argument("--warmup-days", type=int, default=14, help="model cold-start run-in before the window")
    parser.add_argument("--model-trades", default=None, help="existing model ledger CSV; skips the model re-run")
    parser.add_argument(
        "--model-commit",
        default=None,
        help="operator-supplied source commit for --model-trades; recorded but not independently proven",
    )
    parser.add_argument("--transitions", default=None, help="producer enter/leave JSONL")
    parser.add_argument("--trades", default=None, help="engine closed-trade journal JSONL")
    parser.add_argument("--cycle-reports", default=None, help="directory of long_native_cycle_*.json payloads")
    parser.add_argument(
        "--venue-history",
        default=None,
        help="venue closed-pnl and transaction-log JSONL backstop",
    )
    parser.add_argument("--pair-window-hours", type=float, default=24.0, help="entry-time fallback pairing window")
    parser.add_argument("--out", required=True, help="output directory for the pairs CSV and report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    start_ms, end_ms = _date_ms(args.start), _date_ms(args.end)
    if end_ms <= start_ms:
        raise SystemExit("--end must be after --start")
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.model_trades:
        model_csv = Path(args.model_trades).expanduser()
        model_commit = str(args.model_commit or "") or None
        model_commit_verified = False
    else:
        model_csv = run_model(
            args.data_root, args.profile, args.start, args.end, args.warmup_days, out_dir / "model_run"
        )
        model_commit = _git_head()
        model_commit_verified = True
    window_ms = int(args.pair_window_hours * MS_PER_HOUR)
    model_rows = load_model_trades(model_csv, start_ms=start_ms, end_ms=end_ms, margin_ms=window_ms)
    model_run_label = None
    model_meta: dict[str, Any] = {}
    model_meta_path = model_csv.parent / "long_native_research_report.json"
    if model_meta_path.exists():
        try:
            model_meta = json.loads(model_meta_path.read_text(encoding="utf-8"))
            model_run_label = str(model_meta.get("run_label") or "") or None
        except (json.JSONDecodeError, OSError):
            model_meta = {}
            model_run_label = None

    transitions = load_transitions(Path(args.transitions).expanduser()) if args.transitions else []
    engine_trades = load_engine_long_trades(Path(args.trades).expanduser()) if args.trades else []
    cycles = load_cycle_evidence(Path(args.cycle_reports).expanduser()) if args.cycle_reports else None
    venue_history = _read_jsonl(Path(args.venue_history).expanduser()) if args.venue_history else []
    venue_rows = [row for row in venue_history if row.get("_kind") == "closed_pnl"]
    venue_transactions_raw = [row for row in venue_history if row.get("_kind") == "txn"]
    venue_transactions, venue_transaction_duplicates = _dedupe_venue_transactions(venue_transactions_raw)

    live_all = build_live_trades(transitions, engine_trades, cycles, venue_rows, venue_transactions)
    live_in_window, outside_window = window_filter_live(live_all, start_ms=start_ms, end_ms=end_ms)
    cohorts = pair_trades(
        model_rows,
        live_in_window,
        window_ms=window_ms,
        context_trades=outside_window,
    )
    context_position_ids = {id(trade) for _, trade in cohorts.model_while_live_held}
    outside_window = [trade for trade in outside_window if id(trade) not in context_position_ids]
    rows = build_rows(cohorts)
    summary = summarize(rows)

    pairs_path = out_dir / "long_live_vs_model_pairs.csv"
    provenance_path = out_dir / "long_live_vs_model_provenance.json"
    with pairs_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("" if value is None else value) for key, value in row.items()})

    report_path = out_dir / "long_live_vs_model_report.md"
    report_path.write_text(
        render_markdown(
            start=args.start,
            end=args.end,
            warmup_days=args.warmup_days,
            profile=args.profile,
            rows=rows,
            summary=summary,
            outside_window=outside_window,
            uncorroborated_candidates=len(cycles.uncorroborated_candidate_ids) if cycles else 0,
            cycles=cycles,
            model_meta=model_meta,
            model_run_label=model_run_label,
            model_commit=model_commit,
            model_commit_verified=model_commit_verified,
            provenance_name=provenance_path.name,
            inputs={
                "model ledger": str(model_csv),
                "transitions": args.transitions or "(not given)",
                "engine journal": args.trades or "(not given)",
                "cycle reports": args.cycle_reports or "(not given)",
                "venue history": args.venue_history or "(not given)",
            },
        ),
        encoding="utf-8",
    )

    invocation = list(sys.argv if argv is None else [str(Path(__file__).relative_to(REPO)), *argv])
    input_identities: dict[str, Any] = {
        "model_ledger": _file_identity(model_csv),
        "model_report": _file_identity(model_meta_path),
        "transitions": _file_identity(Path(args.transitions)) if args.transitions else None,
        "engine_journal": _file_identity(Path(args.trades)) if args.trades else None,
        "cycle_reports": _cycle_directory_identity(Path(args.cycle_reports)) if args.cycle_reports else None,
        "venue_history": _file_identity(Path(args.venue_history)) if args.venue_history else None,
    }
    data_root = Path(args.data_root).expanduser().resolve()
    input_identities["data_root"] = {
        "path": str(data_root),
        "read_by_checker": not bool(args.model_trades),
        "content_hash_complete": False,
        "note": "the model report and ledger are hashed; the underlying partition tree is identified by path only",
    }
    input_identities["archive_manifest_report"] = _file_identity(
        data_root / "reports" / "archive_manifest_bybit-public-trading.json"
    )
    provenance = {
        "schema_version": 1,
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "claim": "diagnose LONG demo execution parity against the registered model",
        "window": {"start": args.start, "end_exclusive": args.end},
        "pair_window_hours": args.pair_window_hours,
        "warmup_days": args.warmup_days,
        "profile": args.profile,
        "model_commit": model_commit,
        "model_commit_verified": model_commit_verified,
        "argv": invocation,
        "code": {
            "git_head": _git_head(),
            "script": _file_identity(Path(__file__)),
        },
        "model_metadata": model_meta,
        "cycle_metadata": _cycle_metadata(cycles),
        "input_identities": input_identities,
        "parsed_rows": {
            "model": len(model_rows),
            "transitions": len(transitions),
            "engine_long_trades": len(engine_trades),
            "venue_closed_pnl": len(venue_rows),
            "venue_transactions": len(venue_transactions),
            "venue_transaction_duplicates_removed": venue_transaction_duplicates,
            "live_stitched": len(live_all),
            "live_in_window": len(live_in_window),
            "live_context_positions": len(context_position_ids),
            "live_outside_window": len(outside_window),
        },
        "summary": summary,
        "outputs": {
            "pairs_csv": _file_identity(pairs_path),
            "report_markdown": _file_identity(report_path),
        },
    }
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(render_summary_text(summary, args.start, args.end))
    print(f"\npairs csv: {pairs_path}\nreport:    {report_path}\nprovenance: {provenance_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
