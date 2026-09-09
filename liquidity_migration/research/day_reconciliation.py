"""Reconcile one account's full UTC day between recorded equity boundaries and the venue's own cash rows."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from liquidity_migration.research.venue_wal_accounting import (
    FLAT_QTY,
    SEGMENT_KINDS,
    EngineFill,
    EngineTrade,
    EvidenceError,
    VenueCapture,
    WalRead,
    _decimal,
    _dedupe,
    _integer,
    _json_value,
    _manifest_covers,
    _table_name,
    _trade_values,
    _transaction_change,
    parse_wal_accounting,
    read_venue_capture,
    read_wal_family,
    unfetched_transfer_sources,
    write_report,
)

DAY_MS = 86_400_000
DEFAULT_TOLERANCE_USDT = Decimal("0.01")
DEFAULT_MAX_BOUNDARY_DISTANCE_S = 120

# The venue's own `type` field, mapped to the bucket this report settles it in.
TRANSACTION_BUCKETS = {
    "TRADE": "trade",
    "SETTLEMENT": "funding",
    "TRANSFER_IN": "transfers",
    "TRANSFER_OUT": "transfers",
    "DEPOSIT": "deposits",
    "WITHDRAW": "withdrawals",
}
BUCKETS = ("trade", "funding", "transfers", "deposits", "withdrawals", "unrecognised")

SAMPLE_FIELDS_READ = (
    "ts_ms",
    "kind",
    "realm",
    "state",
    "venue",
    "mode",
    "engine_commit",
    "account_user_id",
    "account_age_ms",
    "equity_usdt",
    "available_usdt",
    "position_count",
    "position_entry_notional_usdt",
    "sleeve_positions",
    "wallet_cash_usdt",
    "unrealised_pnl_usdt",
    "positions",
    "positions_truncated",
)
# What a boundary sample written before the recorder carried these fields
# cannot say. Each text is reported only while its field is absent.
SAMPLE_FIELDS_ABSENT = {
    "wallet_cash_usdt": "wallet cash: without wallet_cash_usdt the sample carries equity_usdt and "
    "available_usdt, not the venue's wallet balance",
    "unrealised_pnl_usdt": "unrealised P&L: without unrealised_pnl_usdt an equity change over open "
    "positions cannot be split into cash and mark",
    "positions": "per-symbol positions: without the positions list the sample carries "
    "position_count, position_entry_notional_usdt and sleeve_positions counts, not the symbols, "
    "sides, quantities or entry prices",
}
DECOMPOSITION_FIELDS = ("wallet_cash_usdt", "unrealised_pnl_usdt")


class Failures:
    """Gate reasons and named missing requirements, in the order they were found."""

    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.missing: list[str] = []

    def fail(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def require(self, requirement: str) -> None:
        if requirement not in self.missing:
            self.missing.append(requirement)
        self.fail(f"missing requirement: {requirement}")


@dataclass(frozen=True)
class SampleRow:
    ts_ms: int
    path: str
    line: int
    row: Mapping[str, Any]


@dataclass(frozen=True)
class SignedFill:
    sequence: int
    venue_ts_ms: int
    symbol: str
    sleeve: str
    signed_qty: Decimal
    px: Decimal


@dataclass(frozen=True)
class BaseExposure:
    """A rotation segment's restatement of exposure, which is where a copy that does not
    start at segment 1 has to start folding."""

    sequence: int
    segment: int
    wall_ts_ms: int | None
    claims: tuple[tuple[str, str, Decimal], ...]
    logged: Mapping[str, Decimal]
    issues: tuple[str, ...]


def day_window(day: str) -> tuple[int, int]:
    try:
        date = dt.date.fromisoformat(day)
    except ValueError:
        raise EvidenceError(f"--day {day!r} is not an ISO UTC date") from None
    start = int(dt.datetime.combine(date, dt.time(), tzinfo=dt.timezone.utc).timestamp() * 1000)
    return start, start + DAY_MS


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _month(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m")


def read_engine_samples(
    directory: Path, realm: str, months: Sequence[str], failures: Failures
) -> tuple[list[SampleRow], list[str]]:
    resolved = directory.expanduser().resolve()
    files: list[str] = []
    rows: list[SampleRow] = []
    for month in months:
        path = resolved / f"engine-{realm}-{month}.jsonl"
        if not path.exists():
            failures.require(f"equity recorder samples for {realm} {month}: {path} is absent")
            continue
        files.append(str(path))
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line, parse_float=Decimal)
                except json.JSONDecodeError as exc:
                    raise EvidenceError(f"{path}:{line_number}: malformed equity sample ({exc})") from exc
                if not isinstance(row, dict):
                    raise EvidenceError(f"{path}:{line_number}: equity sample is not a JSON object")
                if row.get("kind") != "engine" or row.get("realm") != realm:
                    continue
                stamp = _integer(row.get("ts_ms"))
                if stamp is None:
                    raise EvidenceError(f"{path}:{line_number}: equity sample has no integer ts_ms")
                rows.append(SampleRow(stamp, str(path), line_number, row))
    rows.sort(key=lambda sample: (sample.ts_ms, sample.path, sample.line))
    return rows, files


def sample_positions(
    name: str, sample: SampleRow, failures: Failures
) -> list[dict[str, Any]] | None:
    """The recorder's per-symbol position list, or None when the sample carries none."""

    rows = sample.row.get("positions")
    if rows is None:
        return None
    if not isinstance(rows, list):
        failures.fail(
            f"{name} boundary sample {sample.path}:{sample.line} has a positions field that is not a list"
        )
        return None
    out: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "") if isinstance(row, Mapping) else ""
        side = str(row.get("side") or "") if isinstance(row, Mapping) else ""
        qty = _decimal(row.get("qty")) if isinstance(row, Mapping) else None
        if not symbol or side not in {"long", "short"} or qty is None or qty <= 0:
            failures.fail(
                f"{name} boundary sample {sample.path}:{sample.line} has a position row without a "
                "readable symbol, side and quantity"
            )
            return None
        strategy = row.get("strategy")
        out.append(
            {
                "symbol": symbol,
                "side": side,
                "signed_qty": qty if side == "long" else -qty,
                "entry_px": _decimal(row.get("entry_px")),
                "mark_px": _decimal(row.get("mark_px")),
                "sleeve": None if strategy is None else str(strategy),
            }
        )
    return out


def select_boundary(
    samples: Sequence[SampleRow],
    name: str,
    boundary_ms: int,
    max_distance_s: int,
    failures: Failures,
) -> dict[str, Any]:
    at_or_after = [sample for sample in samples if sample.ts_ms >= boundary_ms]
    chosen: SampleRow | None = None
    skipped = 0
    for position, sample in enumerate(at_or_after):
        if sample.row.get("state") == "live" and _decimal(sample.row.get("equity_usdt")) is not None:
            chosen, skipped = sample, position
            break
    report: dict[str, Any] = {
        "boundary_utc": _iso(boundary_ms),
        "boundary_ms": boundary_ms,
        "samples_at_or_after_boundary": len(at_or_after),
        "samples_skipped_before_the_first_live_reading": skipped,
        "selected": None,
    }
    if chosen is None:
        failures.require(
            f"{name} boundary observation at {_iso(boundary_ms)}: no live engine equity sample at or "
            "after the boundary in the supplied recorder files"
        )
        return report
    age_ms = _integer(chosen.row.get("account_age_ms"))
    reading_ms = None if age_ms is None else chosen.ts_ms - age_ms
    selected: dict[str, Any] = {
        "sample_file": chosen.path,
        "sample_line": chosen.line,
        "sample_ts_ms": chosen.ts_ms,
        "sample_ts_utc": _iso(chosen.ts_ms),
        "sample_distance_s": Decimal(chosen.ts_ms - boundary_ms) / 1000,
        "account_age_ms": age_ms,
        "venue_reading_ts_ms": reading_ms,
        "venue_reading_ts_utc": _iso(reading_ms),
        "venue_reading_distance_s": None if reading_ms is None else Decimal(reading_ms - boundary_ms) / 1000,
        "state": chosen.row.get("state"),
        "equity_usdt": _decimal(chosen.row.get("equity_usdt")),
        "available_usdt": _decimal(chosen.row.get("available_usdt")),
        "position_count": _integer(chosen.row.get("position_count")),
        "position_entry_notional_usdt": _decimal(chosen.row.get("position_entry_notional_usdt")),
        "sleeve_positions": chosen.row.get("sleeve_positions"),
        "wallet_cash_usdt": _decimal(chosen.row.get("wallet_cash_usdt")),
        "unrealised_pnl_usdt": _decimal(chosen.row.get("unrealised_pnl_usdt")),
        "positions_truncated": chosen.row.get("positions_truncated") is True,
        "positions": sample_positions(name, chosen, failures),
        "account_user_id": chosen.row.get("account_user_id"),
        "venue": chosen.row.get("venue"),
        "mode": chosen.row.get("mode"),
        "engine_commit": chosen.row.get("engine_commit"),
    }
    report["selected"] = selected
    limit = Decimal(max_distance_s)
    if abs(selected["sample_distance_s"]) > limit:
        failures.require(
            f"{name} boundary observation within {max_distance_s} s of {_iso(boundary_ms)}: the first "
            f"live sample is {selected['sample_distance_s']} s away"
        )
    if reading_ms is None:
        failures.require(
            f"{name} boundary venue reading time: sample {chosen.path}:{chosen.line} carries no "
            "account_age_ms, so its equity cannot be placed in time"
        )
    elif abs(selected["venue_reading_distance_s"]) > limit:
        failures.require(
            f"{name} boundary venue reading within {max_distance_s} s of {_iso(boundary_ms)}: the "
            f"selected sample's venue reading is {selected['venue_reading_distance_s']} s away"
        )
    if selected["position_count"] is None:
        failures.require(f"{name} boundary position_count: sample {chosen.path}:{chosen.line} carries none")
    if not isinstance(selected["sleeve_positions"], Mapping):
        failures.require(f"{name} boundary sleeve_positions: sample {chosen.path}:{chosen.line} carries none")
    return report


def unique_transactions(capture: VenueCapture, failures: Failures) -> dict[str, Mapping[str, Any]]:
    issues: list[str] = []
    rows = _dedupe(capture.cash_rows(), "id", "venue transaction id", issues)
    for issue in issues:
        failures.fail(f"venue capture: {issue}")
    for identity, row in rows.items():
        if _integer(row.get("transactionTime")) is None:
            failures.fail(f"venue transaction {identity} has no integer transactionTime")
    return rows


def transactions_between(
    rows: Mapping[str, Mapping[str, Any]], start_ms: int, end_ms: int
) -> list[Mapping[str, Any]]:
    inside = [
        row
        for row in rows.values()
        if (_integer(row.get("transactionTime")) or -1) >= start_ms
        and (_integer(row.get("transactionTime")) or -1) < end_ms
    ]
    inside.sort(key=lambda row: (_integer(row.get("transactionTime")) or 0, str(row.get("id") or "")))
    return inside


def boundary_straddle(
    name: str,
    boundary_ms: int,
    reading_ms: int | None,
    rows: Mapping[str, Mapping[str, Any]],
    capture_window: tuple[int | None, int | None],
    failures: Failures,
) -> dict[str, Any]:
    """The cash rows between midnight and the boundary sample's venue reading, which the day's sum
    counts on one side of the boundary and the sample's equity on the other."""

    if reading_ms is None or reading_ms == boundary_ms:
        return {
            "interval_start_ms": boundary_ms,
            "interval_end_ms_exclusive": boundary_ms,
            "sign": 0,
            "rows": [],
            "signed_change_usdt": Decimal(0),
            "interval_inside_capture": True,
        }
    sign = 1 if reading_ms > boundary_ms else -1
    low, high = (boundary_ms, reading_ms) if sign > 0 else (reading_ms, boundary_ms)
    inside = transactions_between(rows, low, high)
    total = Decimal(0)
    receipts: list[dict[str, Any]] = []
    for row in inside:
        change = _decimal(row.get("change"))
        if change is None:
            failures.fail(
                f"venue transaction {row.get('id')} inside the {name} boundary straddle has no "
                "readable change"
            )
            continue
        total += change
        receipts.append(
            {
                "transaction_id": row.get("id"),
                "type": row.get("type"),
                "venue_ts_ms": _integer(row.get("transactionTime")),
                "venue_ts_utc": _iso(_integer(row.get("transactionTime"))),
                "change_usdt": change,
            }
        )
    capture_start, capture_end = capture_window
    covered = capture_start is not None and capture_end is not None and capture_start <= low and high <= capture_end
    if not covered:
        failures.require(
            f"{name} boundary straddle coverage: the capture does not cover "
            f"[{_iso(low)}, {_iso(high)}), so a cash row between midnight and the boundary sample's "
            "venue reading would be unobserved"
        )
    return {
        "interval_start_ms": low,
        "interval_end_ms_exclusive": high,
        "sign": sign,
        "rows": receipts,
        "signed_change_usdt": sign * total,
        "interval_inside_capture": covered,
    }


def summarise_transactions(rows: Sequence[Mapping[str, Any]], failures: Failures) -> dict[str, Any]:
    by_type: dict[str, dict[str, Any]] = {}
    buckets = {name: Decimal(0) for name in BUCKETS}
    change_total = Decimal(0)
    cash_flow_total = Decimal(0)
    funding_total = Decimal(0)
    fee_total = Decimal(0)
    trade_cash = Decimal(0)
    rows_without_change = 0
    issues: list[str] = []
    for row in rows:
        identity = str(row.get("id") or "<blank>")
        name = str(row.get("type") or "") or "<blank>"
        _transaction_change(row, issues, identity)
        change = _decimal(row.get("change"))
        cash_flow = _decimal(row.get("cashFlow"))
        funding = _decimal(row.get("funding")) or Decimal(0)
        fee = _decimal(row.get("fee")) or Decimal(0)
        bucket = TRANSACTION_BUCKETS.get(name, "unrecognised")
        entry = by_type.setdefault(
            name,
            {
                "bucket": bucket,
                "recognised": name in TRANSACTION_BUCKETS,
                "rows": 0,
                "change_usdt": Decimal(0),
                "cash_flow_usdt": Decimal(0),
                "funding_usdt": Decimal(0),
                "fee_usdt": Decimal(0),
                "rows_without_change": 0,
            },
        )
        entry["rows"] += 1
        entry["cash_flow_usdt"] += cash_flow or Decimal(0)
        entry["funding_usdt"] += funding
        entry["fee_usdt"] += fee
        cash_flow_total += cash_flow or Decimal(0)
        funding_total += funding
        fee_total += fee
        if name == "TRADE":
            trade_cash += cash_flow or Decimal(0)
        if change is None:
            absent = [
                field for field, value in (("cashFlow", cash_flow), ("change", change)) if value is None
            ]
            rows_without_change += 1
            entry["rows_without_change"] += 1
            failures.fail(
                f"venue transaction {identity} of type {name} has no readable "
                f"{' and '.join(absent)}; its cash effect stays unknown and is not summed as zero"
            )
            continue
        entry["change_usdt"] += change
        buckets[bucket] += change
        change_total += change
    for issue in issues:
        failures.fail(issue)
    unrecognised = [
        {"type": name, "rows": entry["rows"], "change_usdt": entry["change_usdt"]}
        for name, entry in sorted(by_type.items())
        if not entry["recognised"]
    ]
    for entry in unrecognised:
        failures.fail(
            f"venue transaction type {entry['type']} is not one this reconciliation buckets: "
            f"{entry['rows']} rows worth {entry['change_usdt']} USDT of account change"
        )
    identity_residual = change_total - (cash_flow_total + funding_total - fee_total)
    return {
        "rows": len(rows),
        "rows_without_change": rows_without_change,
        "by_type": by_type,
        "buckets": buckets,
        "components": {
            "trade_cash_usdt": trade_cash,
            "fees_usdt": fee_total,
            "funding_usdt": funding_total,
            "cash_flow_usdt": cash_flow_total,
        },
        "change_total_usdt": change_total,
        "row_arithmetic_residual_usdt": identity_residual,
        "unrecognised_types": unrecognised,
    }


def _chain_head(pending: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The row in one equal-timestamp group that no other row in it precedes.

    The venue's `id` is not an order, so within one `transactionTime` the chain
    itself is the order: the row whose `cashBalance - change` no sibling's
    `cashBalance` equals is the one the group starts at.
    """

    balances: dict[Decimal, int] = {}
    for row in pending:
        balance = _decimal(row.get("cashBalance"))
        if balance is not None:
            balances[balance] = balances.get(balance, 0) + 1
    for row in pending:
        cash_balance = _decimal(row.get("cashBalance"))
        change = _decimal(row.get("change"))
        if cash_balance is None or change is None:
            continue
        before = cash_balance - change
        siblings = balances.get(before, 0) - int(cash_balance == before)
        if siblings == 0:
            return row
    return pending[0]


def chain_order(rows: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], int]:
    """The rows in wallet-cash order, and how many equal-timestamp groups had to be walked."""

    ordered: list[Mapping[str, Any]] = []
    groups = 0
    balance: Decimal | None = None
    index = 0
    while index < len(rows):
        stamp = _integer(rows[index].get("transactionTime"))
        end = index
        while end < len(rows) and _integer(rows[end].get("transactionTime")) == stamp:
            end += 1
        pending = list(rows[index:end])
        groups += int(len(pending) > 1)
        while pending:
            following = None
            if balance is not None:
                for row in pending:
                    cash_balance = _decimal(row.get("cashBalance"))
                    change = _decimal(row.get("change"))
                    if cash_balance is not None and change is not None and cash_balance - change == balance:
                        following = row
                        break
            following = following if following is not None else _chain_head(pending)
            pending.remove(following)
            ordered.append(following)
            reached = _decimal(following.get("cashBalance"))
            balance = balance if reached is None else reached
        index = end
    return ordered, groups


def cash_chain(
    rows: Sequence[Mapping[str, Any]],
    change_total: Decimal,
    failures: Failures,
    *,
    unfetched_transfers: Sequence[str] = (),
    tolerance_usdt: Decimal = DEFAULT_TOLERANCE_USDT,
) -> dict[str, Any]:
    ordered, multi_row_groups = chain_order(rows)
    gaps: list[dict[str, Any]] = []
    opening: Decimal | None = None
    balance: Decimal | None = None
    for row in ordered:
        identity = str(row.get("id") or "<blank>")
        cash_balance = _decimal(row.get("cashBalance"))
        change = _decimal(row.get("change"))
        if cash_balance is None or change is None:
            failures.fail(f"venue transaction {identity} has no cashBalance and change pair")
            continue
        before = cash_balance - change
        if opening is None:
            opening = before
        elif balance is not None and before != balance:
            gaps.append(
                {
                    "transaction_id": identity,
                    "type": row.get("type"),
                    "venue_ts_ms": _integer(row.get("transactionTime")),
                    "venue_ts_utc": _iso(_integer(row.get("transactionTime"))),
                    "chain_balance_before_usdt": balance,
                    "row_balance_before_usdt": before,
                    "gap_usdt": before - balance,
                }
            )
        balance = cash_balance
    residual: Decimal | None = None
    if opening is not None and balance is not None:
        residual = (balance - opening) - change_total
    gap_total = sum((gap["gap_usdt"] for gap in gaps), Decimal(0))
    if unfetched_transfers:
        # One reason, because there is one cause: a transfer nobody fetched
        # leaves exactly this shaped hole in the chain and in the residual.
        jumps = "; ".join(
            f"{gap['gap_usdt']} USDT before {gap['transaction_id']} at {gap['venue_ts_utc']}"
            for gap in gaps
        )
        failures.require(
            f"venue transfer rows: this capture proves no complete {' and '.join(unfetched_transfers)} "
            f"pagination, so a UNIFIED-account transfer inside the day is unfetched, not zero. The "
            f"chain's unexplained jumps total {gap_total} USDT and its cash residual is "
            f"{residual} USDT, which is what such a transfer leaves and stays unattributed"
            + (f": {jumps}" if jumps else "; the chain itself is unbroken")
        )
    else:
        for gap in gaps:
            failures.fail(
                f"the venue's own wallet-cash chain jumps {gap['gap_usdt']} USDT before transaction "
                f"{gap['transaction_id']} of type {gap['type']} at {gap['venue_ts_utc']}: a cash row "
                "inside the day is not in the capture"
            )
        if residual is not None and abs(residual) > tolerance_usdt:
            failures.fail(
                f"wallet-cash residual {residual} USDT is over the declared {tolerance_usdt} USDT tolerance"
            )
    return {
        "established": opening is not None and balance is not None,
        "source": "the transaction log's own cashBalance column; inferred cash, not an independent snapshot",
        "order": "within one transactionTime the row whose cashBalance - change continues the running "
        "balance comes next; the venue's id is not an order",
        "rows_ordered": len(ordered),
        "equal_timestamp_groups_walked": multi_row_groups,
        "begin_cash_usdt": opening,
        "end_cash_usdt": balance,
        "cash_residual_usdt": residual,
        "gaps": gaps,
        "gap_total_usdt": gap_total,
        "unfetched_transfer_sources": list(unfetched_transfers),
    }


def signed_fills(fills: Sequence[EngineFill], failures: Failures) -> list[SignedFill]:
    out: list[SignedFill] = []
    for fill in fills:
        identity = fill.exec_id or f"<blank> at WAL sequence {fill.sequence}"
        if fill.venue_ts_ms is None or fill.venue_ts_ms <= 0:
            failures.fail(f"WAL fill {identity} has no venue timestamp, so boundary exposure is unestablished")
            continue
        if (
            fill.symbol is None
            or fill.qty is None
            or fill.qty <= 0
            or fill.px is None
            or fill.side not in {"Buy", "Sell"}
        ):
            failures.fail(
                f"WAL fill {identity} has no readable symbol, side, quantity and price, so boundary "
                "exposure is unestablished"
            )
            continue
        out.append(
            SignedFill(
                sequence=fill.sequence,
                venue_ts_ms=fill.venue_ts_ms,
                symbol=fill.symbol,
                sleeve=fill.strategy or "unresolved",
                signed_qty=fill.qty if fill.side == "Buy" else -fill.qty,
                px=fill.px,
            )
        )
    out.sort(key=lambda fill: fill.venue_ts_ms)
    return out


def read_base_exposure(wal: WalRead) -> BaseExposure | None:
    """The first copied rotation segment's restatement of exposure, or None when the copy
    begins at segment 1 and therefore has no restatement to begin from."""

    for row in wal.records:
        record = row.record
        if record.get("kind") not in SEGMENT_KINDS:
            continue
        strategies: list[Any] = record["strategies"] if isinstance(record.get("strategies"), list) else []
        symbols: list[Any] = record["symbols"] if isinstance(record.get("symbols"), list) else []
        issues: list[str] = []
        claims: list[tuple[str, str, Decimal]] = []
        attribution = record.get("attribution")
        if not isinstance(attribution, list):
            issues.append("the segment base restates no attribution rows")
            attribution = []
        for claim in attribution:
            sleeve = _table_name(strategies, claim.get("strategy")) if isinstance(claim, Mapping) else None
            symbol = _table_name(symbols, claim.get("symbol")) if isinstance(claim, Mapping) else None
            signed = _decimal(claim.get("signed_qty")) if isinstance(claim, Mapping) else None
            if sleeve is None or symbol is None or signed is None:
                issues.append("a segment-base attribution row has an unresolved sleeve, symbol or quantity")
                continue
            claims.append((sleeve, symbol, signed))
        logged: dict[str, Decimal] = {}
        exposure = record.get("logged_exposure")
        if not isinstance(exposure, list):
            issues.append("the segment base restates no logged exposure")
            exposure = []
        for position in exposure:
            symbol = _table_name(symbols, position.get("symbol")) if isinstance(position, Mapping) else None
            signed = _decimal(position.get("signed_qty")) if isinstance(position, Mapping) else None
            if symbol is None or signed is None:
                issues.append("a segment-base logged-exposure row has an unresolved symbol or quantity")
                continue
            logged[symbol] = logged.get(symbol, Decimal(0)) + signed
        return BaseExposure(
            sequence=row.sequence,
            segment=row.segment,
            wall_ts_ms=_integer(record.get("wall_ts_ms")),
            claims=tuple(claims),
            logged=logged,
            issues=tuple(issues),
        )
    return None


def exposure_at(
    fills: Sequence[SignedFill], boundary_ms: int, base: BaseExposure | None = None
) -> dict[str, Any]:
    held: dict[str, Decimal] = {}
    # Average-entry cost per symbol: a reduction keeps the average, a cross through flat restates
    # it, and None means the quantity came from a rotation restatement that carries no price.
    cost: dict[str, Decimal | None] = {}
    sleeves: dict[str, dict[str, Decimal]] = {}
    if base is not None:
        for sleeve, symbol, signed in base.claims:
            held[symbol] = held.get(symbol, Decimal(0)) + signed
            cost[symbol] = None
            claim = sleeves.setdefault(sleeve, {})
            claim[symbol] = claim.get(symbol, Decimal(0)) + signed
    superseded = 0
    for fill in fills:
        if fill.venue_ts_ms >= boundary_ms:
            break
        if base is not None and fill.sequence < base.sequence:
            # The restatement already carries this fill's effect.
            superseded += 1
            continue
        before = held.get(fill.symbol, Decimal(0))
        after = before + fill.signed_qty
        known = cost.get(fill.symbol, Decimal(0))
        if before == 0:
            cost[fill.symbol] = fill.signed_qty * fill.px
        elif (before > 0) == (fill.signed_qty > 0):
            cost[fill.symbol] = None if known is None else known + fill.signed_qty * fill.px
        elif abs(after) < FLAT_QTY or (before > 0) != (after > 0):
            cost[fill.symbol] = after * fill.px
        else:
            cost[fill.symbol] = None if known is None else known * after / before
        held[fill.symbol] = after
        claim = sleeves.setdefault(fill.sleeve, {})
        claim[fill.symbol] = claim.get(fill.symbol, Decimal(0)) + fill.signed_qty
    symbols = {symbol: qty for symbol, qty in held.items() if abs(qty) >= FLAT_QTY}
    sleeve_symbols = {
        sleeve: sorted(symbol for symbol, qty in claims.items() if abs(qty) >= FLAT_QTY)
        for sleeve, claims in sleeves.items()
    }
    sleeve_symbols = {sleeve: names for sleeve, names in sleeve_symbols.items() if names}
    owners: dict[str, list[str]] = {}
    for sleeve, names in sleeve_symbols.items():
        for symbol in names:
            owners.setdefault(symbol, []).append(sleeve)
    unpriced = sorted(symbol for symbol in symbols if cost.get(symbol) is None)
    return {
        "wal_position_count": len(symbols),
        "wal_entry_notional_usdt": sum(
            (abs(cost[symbol]) for symbol in symbols if cost.get(symbol) is not None), Decimal(0)  # type: ignore[arg-type]
        ),
        "wal_positions": [
            {
                "symbol": symbol,
                "signed_qty": symbols[symbol],
                "entry_notional_usdt": None if cost.get(symbol) is None else abs(cost[symbol]),  # type: ignore[arg-type]
                "sleeves": sorted(owners.get(symbol, [])),
            }
            for symbol in sorted(symbols)
        ],
        "wal_positions_without_an_entry_price": unpriced,
        "wal_fills_superseded_by_the_restatement": superseded,
        "wal_sleeve_position_counts": {sleeve: len(names) for sleeve, names in sorted(sleeve_symbols.items())},
        "symbols_claimed_by_more_than_one_sleeve": sorted(
            symbol for symbol, claim in owners.items() if len(claim) > 1 and symbol in symbols
        ),
    }


def compare_positions(
    name: str, boundary: Mapping[str, Any], exposure: Mapping[str, Any], failures: Failures
) -> list[dict[str, Any]]:
    selected = boundary.get("selected")
    if not isinstance(selected, Mapping):
        return []
    differences: list[dict[str, Any]] = []
    sample_counts = selected.get("sleeve_positions")
    sample_counts = sample_counts if isinstance(sample_counts, Mapping) else {}
    hand_held = _integer(sample_counts.get("unattributed")) or 0
    sample_count = selected.get("position_count")
    wal_count = int(exposure["wal_position_count"])
    if sample_count is not None and sample_count != wal_count + hand_held:
        differences.append(
            {
                "boundary": name,
                "kind": "physical_position_count",
                "sample_position_count": sample_count,
                "sample_unattributed_positions": hand_held,
                "wal_position_count": wal_count,
                "wal_symbols": [row["symbol"] for row in exposure["wal_positions"]],
                "gating": True,
            }
        )
    multi = exposure["symbols_claimed_by_more_than_one_sleeve"]
    wal_counts = exposure["wal_sleeve_position_counts"]
    for sleeve in sorted({*wal_counts, *(str(key) for key in sample_counts)} - {"unattributed"}):
        wal_sleeve = int(wal_counts.get(sleeve, 0))
        sample_sleeve = _integer(sample_counts.get(sleeve))
        if sample_sleeve == wal_sleeve:
            continue
        differences.append(
            {
                "boundary": name,
                "kind": "sleeve_position_count",
                "sleeve": sleeve,
                "sample_positions": sample_sleeve,
                "wal_positions": wal_sleeve,
                "wal_symbols": [
                    row["symbol"] for row in exposure["wal_positions"] if sleeve in row["sleeves"]
                ],
                "gating": not multi,
            }
        )
    differences.extend(_symbol_differences(name, selected, exposure))
    for difference in differences:
        if not difference["gating"]:
            continue
        failures.fail(
            f"{name} boundary positions differ from the WAL's attributed exposure: "
            f"{json.dumps(_json_value(difference), sort_keys=True)}"
        )
    return differences


def _symbol_differences(
    name: str, selected: Mapping[str, Any], exposure: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Per-symbol signed-quantity equality, which only a sample carrying its positions can gate."""

    sample_rows = selected.get("positions")
    if not isinstance(sample_rows, list):
        return []
    wal_held = {row["symbol"]: row["signed_qty"] for row in exposure["wal_positions"]}
    sample_held: dict[str, Decimal] = {}
    hand_held: set[str] = set()
    for row in sample_rows:
        symbol = str(row["symbol"])
        sample_held[symbol] = sample_held.get(symbol, Decimal(0)) + row["signed_qty"]
        if row["sleeve"] is None:
            hand_held.add(symbol)
    differences: list[dict[str, Any]] = []
    for symbol in sorted({*wal_held, *sample_held}):
        sample_qty = sample_held.get(symbol)
        wal_qty = wal_held.get(symbol)
        if sample_qty is not None and wal_qty is not None and abs(sample_qty - wal_qty) < FLAT_QTY:
            continue
        differences.append(
            {
                "boundary": name,
                "kind": "symbol_position",
                "symbol": symbol,
                "sample_signed_qty": sample_qty,
                "wal_signed_qty": wal_qty,
                "sample_calls_it_hand_held": symbol in hand_held,
                # The owner trades this account by hand; a position no sleeve
                # claims is his, and the WAL's attributed exposure omits it.
                "gating": not (wal_qty is None and symbol in hand_held),
            }
        )
    return differences


def reconcile_fills(
    fills: Sequence[EngineFill],
    capture: VenueCapture,
    start_ms: int,
    end_ms: int,
    failures: Failures,
) -> dict[str, Any]:
    wal_day = [fill for fill in fills if fill.venue_ts_ms is not None and start_ms <= fill.venue_ts_ms < end_ms]
    wal_ids = {fill.exec_id for fill in wal_day if fill.exec_id}
    if len(wal_ids) != len(wal_day):
        failures.fail("a WAL fill inside the day has no execution id")
    issues: list[str] = []
    executions = _dedupe(capture.executions, "execId", "venue execution", issues)
    for issue in issues:
        failures.fail(f"venue capture: {issue}")
    venue_ids: set[str] = set()
    venue_fee = Decimal(0)
    for identity, row in executions.items():
        stamp = _integer(row.get("execTime"))
        if row.get("execType") != "Trade" or stamp is None or not start_ms <= stamp < end_ms:
            continue
        venue_ids.add(identity)
        fee = _decimal(row.get("execFee"))
        if fee is None:
            failures.fail(f"venue execution {identity} has no readable fee")
            continue
        venue_fee += fee
    wal_fee = Decimal(0)
    for fill in wal_day:
        if fill.fee is None:
            failures.fail(f"WAL fill {fill.exec_id or '<blank>'} has no known fee")
            continue
        wal_fee += fill.fee
    transaction_fee = Decimal(0)
    for row in capture.transactions:
        stamp = _integer(row.get("transactionTime"))
        if row.get("type") != "TRADE" or stamp is None or not start_ms <= stamp < end_ms:
            continue
        transaction_fee += _decimal(row.get("fee")) or Decimal(0)
    for identity in sorted(wal_ids - venue_ids):
        failures.fail(f"WAL fill {identity} inside the day has no captured venue trade execution")
    for identity in sorted(venue_ids - wal_ids):
        failures.fail(f"venue trade execution {identity} inside the day has no WAL fill")
    if wal_fee != venue_fee:
        failures.fail(f"day fee sums differ: WAL {wal_fee}, venue executions {venue_fee}")
    if wal_fee != transaction_fee:
        failures.fail(f"day fee sums differ: WAL {wal_fee}, transaction-log TRADE rows {transaction_fee}")
    return {
        "wal_fills": len(wal_day),
        "venue_trade_executions": len(venue_ids),
        "wal_only_execution_ids": sorted(wal_ids - venue_ids),
        "venue_only_execution_ids": sorted(venue_ids - wal_ids),
        "wal_fee_usdt": wal_fee,
        "venue_execution_fee_usdt": venue_fee,
        "transaction_log_trade_fee_usdt": transaction_fee,
    }


def _trade_row(trade: EngineTrade) -> dict[str, Any]:
    entry: Decimal | None = None
    exit_value: Decimal | None = None
    fees: Decimal | None = None
    net: Decimal | None = None
    values = _trade_values(trade.fills)
    if values is not None:
        entry, exit_value, fees = values
        net = exit_value - entry - fees
    return {
        "symbol": trade.symbol,
        "opened_ms": trade.opened_ms,
        "opened_utc": _iso(trade.opened_ms),
        "closed_ms": trade.closed_ms,
        "closed_utc": _iso(trade.closed_ms),
        "fills": len(trade.fills),
        "entry_value_usdt": entry,
        "exit_value_usdt": exit_value,
        "fees_usdt": fees,
        "net_usdt": net,
        "issues": list(trade.issues),
    }


def sleeve_attribution(wal: WalRead, fills: Sequence[EngineFill], start_ms: int, end_ms: int) -> dict[str, Any]:
    sleeves = sorted({fill.strategy for fill in fills if fill.strategy})
    day_fills = [fill for fill in fills if fill.venue_ts_ms is not None and start_ms <= fill.venue_ts_ms < end_ms]
    out: dict[str, Any] = {}
    for sleeve in sleeves:
        accounting = parse_wal_accounting(wal, sleeve)
        closed = [
            trade
            for trade in accounting.closed_trades
            if trade.closed_ms is not None and start_ms <= trade.closed_ms < end_ms
        ]
        rows = [_trade_row(trade) for trade in closed]
        realised = sum(
            (row["net_usdt"] for row in rows if row["net_usdt"] is not None), Decimal(0)
        )
        sleeve_day_fills = [fill for fill in day_fills if fill.strategy == sleeve]
        out[sleeve] = {
            "day_fills": len(sleeve_day_fills),
            "day_fee_usdt": sum((fill.fee for fill in sleeve_day_fills if fill.fee is not None), Decimal(0)),
            "closed_trades_in_day": rows,
            "realised_net_usdt_closed_in_day": realised,
            "every_closed_trade_opened_inside_the_day": all(
                row["opened_ms"] is not None and row["opened_ms"] >= start_ms for row in rows
            ),
            "open_at_wal_tail": sorted(trade.symbol for trade in accounting.open_trades),
            "grouping_issues": list(accounting.issues),
        }
    return {
        "sleeves": out,
        "unresolved_sleeve_fills_in_day": sum(1 for fill in day_fills if not fill.strategy),
        "grouping_scope": "closed round trips are grouped long-first; a short-side sleeve keeps its "
        "fills and its signed exposure, not closed round trips",
    }


def absent_boundary_fields(boundaries: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """What the selected samples cannot say, named per field a boundary does not carry."""

    absent: list[str] = []
    for field, text in SAMPLE_FIELDS_ABSENT.items():
        for boundary in boundaries.values():
            selected = boundary.get("selected")
            if not isinstance(selected, Mapping) or selected.get(field) is None:
                absent.append(text)
                break
    return absent


def equity_decomposition(
    begin: Any, end: Any, change_total: Decimal, straddle_correction: Decimal
) -> dict[str, Any]:
    """Split the day's equity change into cash and mark, which only boundary samples carrying
    wallet cash and unrealised P&L can do."""

    report: dict[str, Any] = {
        "established": False,
        "wallet_cash_change_usdt": None,
        "unrealised_pnl_change_usdt": None,
        "wallet_cash_residual_usdt": None,
        "identity_residual_usdt": None,
        "fields": list(DECOMPOSITION_FIELDS),
    }
    if not isinstance(begin, Mapping) or not isinstance(end, Mapping):
        return report
    if any(side[field] is None for side in (begin, end) for field in DECOMPOSITION_FIELDS):
        return report
    identity = max(
        (side["equity_usdt"] - side["wallet_cash_usdt"] - side["unrealised_pnl_usdt"] for side in (begin, end)),
        key=abs,
    )
    cash_change = end["wallet_cash_usdt"] - begin["wallet_cash_usdt"]
    report.update(
        {
            "established": True,
            "wallet_cash_change_usdt": cash_change,
            "unrealised_pnl_change_usdt": end["unrealised_pnl_usdt"] - begin["unrealised_pnl_usdt"],
            "wallet_cash_residual_usdt": cash_change - change_total - straddle_correction,
            "identity_residual_usdt": identity,
        }
    )
    return report


def wal_coverage(
    wal: WalRead,
    base: BaseExposure | None,
    readings: Mapping[str, int | None],
    failures: Failures,
) -> dict[str, Any]:
    """A copied day never starts at segment 1, so what a WAL family must prove is that its
    records span both boundary readings, not that the family is whole."""

    notes = [issue for issue in wal.issues]
    if not wal.complete_family:
        notes.append(
            "the copy does not begin at segment 1, which a copied day never does; coverage of both "
            "boundary readings is required instead"
        )
    report: dict[str, Any] = {
        "segment_indices": [segment.index for segment in wal.segments],
        "complete_family": wal.complete_family,
        "first_segment": None,
        "last_segment": None,
        "covers_begin_boundary_reading": None,
        "covers_end_boundary_reading": None,
        "base_restatement": None,
        "notes": notes,
    }
    if not wal.segments:
        failures.require("at least one trusted WAL segment in the copied family")
        return report
    first, last = wal.segments[0], wal.segments[-1]
    report["first_segment"] = {
        "index": first.index,
        "first_record_ms": first.first_ts_ms,
        "first_record_utc": _iso(first.first_ts_ms),
    }
    report["last_segment"] = {
        "index": last.index,
        "last_record_ms": last.last_ts_ms,
        "last_record_utc": _iso(last.last_ts_ms),
    }
    if base is not None:
        report["base_restatement"] = {
            "segment": base.segment,
            "wall_ts_ms": base.wall_ts_ms,
            "wall_ts_utc": _iso(base.wall_ts_ms),
            "attributed_positions": len({symbol for _, symbol, _ in base.claims}),
            "logged_positions": len([1 for qty in base.logged.values() if abs(qty) >= FLAT_QTY]),
            "issues": list(base.issues),
        }
    for name, edge, stamp in (
        ("begin", "first", first.first_ts_ms),
        ("end", "last", last.last_ts_ms),
    ):
        reading = readings.get(name)
        if reading is None:
            continue
        covered = stamp is not None and (stamp <= reading if name == "begin" else stamp >= reading)
        report[f"covers_{name}_boundary_reading"] = covered
        if covered:
            continue
        segment = first if name == "begin" else last
        failures.require(
            f"WAL coverage of the {name} boundary reading at {_iso(reading)}: the {edge} copied "
            f"segment {segment.index}'s {edge} record is stamped "
            f"{_iso(stamp) if stamp is not None else 'nowhere'}"
        )
    return report


def reconcile_day(
    *,
    realm: str,
    day: str,
    wal_path: Path,
    capture_path: Path,
    samples_dir: Path,
    tolerance_usdt: Decimal = DEFAULT_TOLERANCE_USDT,
    max_boundary_distance_s: int = DEFAULT_MAX_BOUNDARY_DISTANCE_S,
) -> dict[str, Any]:
    start_ms, end_ms = day_window(day)
    failures = Failures()

    wal = read_wal_family(wal_path, accounting_only=True)
    accounting = parse_wal_accounting(wal)
    base = read_base_exposure(wal)
    if wal.damaged:
        failures.fail("a WAL segment has bytes after its last complete CRC-checked frame")

    capture = read_venue_capture(capture_path)
    for issue in capture.issues:
        failures.fail(f"venue capture: {issue}")
    for issue in _manifest_covers(capture, start_ms, end_ms - 1):
        failures.fail(f"venue capture: {issue}")
    manifest = capture.manifest or {}
    if str(manifest.get("realm") or "") != realm:
        failures.fail(f"venue capture realm {manifest.get('realm')!r} is not the requested {realm!r}")

    months = sorted({_month(start_ms), _month(end_ms)})
    samples, sample_files = read_engine_samples(samples_dir, realm, months, failures)
    boundaries = {
        name: select_boundary(samples, name, boundary_ms, max_boundary_distance_s, failures)
        for name, boundary_ms in (("begin", start_ms), ("end", end_ms))
    }
    capture_user_id = str(manifest.get("user_id") or "")
    sample_user_ids = sorted(
        {
            str(boundary["selected"]["account_user_id"] or "")
            for boundary in boundaries.values()
            if isinstance(boundary["selected"], Mapping)
        }
    )
    for user_id in sample_user_ids:
        if not user_id:
            failures.fail("a boundary sample names no venue account user id")
        elif capture_user_id and user_id != capture_user_id:
            failures.fail(
                f"boundary sample account {user_id} is not the captured account {capture_user_id}"
            )

    readings = {
        name: (
            boundaries[name]["selected"]["venue_reading_ts_ms"]
            if isinstance(boundaries[name]["selected"], Mapping)
            else None
        )
        for name in boundaries
    }
    coverage = wal_coverage(wal, base, readings, failures)

    ledger = unique_transactions(capture, failures)
    rows = transactions_between(ledger, start_ms, end_ms)
    transactions = summarise_transactions(rows, failures)
    unfetched = unfetched_transfer_sources(capture)
    chain = cash_chain(
        rows,
        transactions["change_total_usdt"],
        failures,
        unfetched_transfers=unfetched,
        tolerance_usdt=tolerance_usdt,
    )

    capture_window = (_integer(manifest.get("start_ms")), _integer(manifest.get("end_ms_exclusive")))
    straddle = {
        name: boundary_straddle(
            name,
            boundary_ms,
            readings[name],
            ledger,
            capture_window,
            failures,
        )
        for name, boundary_ms in (("begin", start_ms), ("end", end_ms))
    }
    straddle_correction = straddle["end"]["signed_change_usdt"] - straddle["begin"]["signed_change_usdt"]

    begin = boundaries["begin"]["selected"]
    end = boundaries["end"]["selected"]
    equity_change: Decimal | None = None
    equity_residual: Decimal | None = None
    corrected_residual: Decimal | None = None
    if isinstance(begin, Mapping) and isinstance(end, Mapping):
        equity_change = end["equity_usdt"] - begin["equity_usdt"]
        equity_residual = equity_change - transactions["change_total_usdt"]
        corrected_residual = equity_residual - straddle_correction
    decomposition = equity_decomposition(
        begin, end, transactions["change_total_usdt"], straddle_correction
    )
    not_flat = []
    for name, boundary in boundaries.items():
        selected = boundary["selected"]
        if not isinstance(selected, Mapping):
            continue
        count = selected["position_count"]
        if count is None or count > 0:
            not_flat.append(name)
    decomposable = equity_residual is not None and (
        decomposition["established"] or not not_flat
    )
    if equity_residual is None:
        failures.require("both boundary equity readings, to compute the day's equity residual")
    elif not decomposable:
        failures.require(
            "boundary unrealised P&L: the "
            + " and ".join(not_flat)
            + " boundary sample does not establish a flat account and carries neither "
            "unrealised_pnl_usdt nor wallet_cash_usdt, so the equity residual "
            f"{equity_residual} USDT is not split into cash and mark"
        )
    elif corrected_residual is not None and not decomposition["established"] and abs(corrected_residual) > tolerance_usdt:
        failures.fail(
            f"equity residual {equity_residual} USDT, {corrected_residual} USDT after the "
            f"{straddle_correction} USDT of cash rows straddling the boundary readings, is over the "
            f"declared {tolerance_usdt} USDT tolerance"
        )
    if decomposition["established"]:
        if abs(decomposition["wallet_cash_residual_usdt"]) > tolerance_usdt:
            failures.fail(
                f"boundary wallet-cash residual {decomposition['wallet_cash_residual_usdt']} USDT — "
                f"the recorded wallet cash moved {decomposition['wallet_cash_change_usdt']} USDT while "
                f"the venue's rows and the straddle account for "
                f"{transactions['change_total_usdt'] + straddle_correction} USDT — is over the declared "
                f"{tolerance_usdt} USDT tolerance"
            )
        if abs(decomposition["identity_residual_usdt"]) > tolerance_usdt:
            failures.fail(
                f"a boundary sample breaks equity = wallet cash + unrealised P&L by "
                f"{decomposition['identity_residual_usdt']} USDT"
            )

    fills = signed_fills(accounting.fills, failures)
    positions: dict[str, Any] = {}
    differences: list[dict[str, Any]] = []
    for name, boundary_ms in (("begin", start_ms), ("end", end_ms)):
        exposure = exposure_at(fills, boundary_ms, base)
        sample = boundaries[name]["selected"]
        notional = (
            None
            if not isinstance(sample, Mapping) or sample["position_entry_notional_usdt"] is None
            else sample["position_entry_notional_usdt"] - exposure["wal_entry_notional_usdt"]
        )
        positions[name] = {
            **exposure,
            "sample_position_count": sample["position_count"] if isinstance(sample, Mapping) else None,
            "sample_entry_notional_usdt": (
                sample["position_entry_notional_usdt"] if isinstance(sample, Mapping) else None
            ),
            "sample_sleeve_positions": sample["sleeve_positions"] if isinstance(sample, Mapping) else None,
            "entry_notional_difference_usdt": notional,
            "entry_notional_is_gating": False,
        }
        differences.extend(compare_positions(name, boundaries[name], exposure, failures))

    fill_report = reconcile_fills(accounting.fills, capture, start_ms, end_ms, failures)
    sleeves = sleeve_attribution(wal, accounting.fills, start_ms, end_ms)
    if sleeves["unresolved_sleeve_fills_in_day"]:
        failures.fail(
            f"{sleeves['unresolved_sleeve_fills_in_day']} WAL fills inside the day have no sleeve, so "
            "the day's exposure is not fully attributed"
        )

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "claim": "one UTC day of one account reconciles between two independently recorded equity "
        "boundaries and the venue's own transaction log, with the residual preserved",
        "gate": "fail" if failures.reasons else "pass",
        "gate_reasons": list(failures.reasons),
        "missing_requirements": list(failures.missing),
        "scope": {
            "realm": realm,
            "day": day,
            "window": {
                "start_ms": start_ms,
                "start_utc": _iso(start_ms),
                "end_ms_exclusive": end_ms,
                "end_utc": _iso(end_ms),
            },
            "tolerance_usdt": tolerance_usdt,
            "max_boundary_distance_s": max_boundary_distance_s,
            "boundary_fields_read": list(SAMPLE_FIELDS_READ),
            "boundary_fields_absent": absent_boundary_fields(boundaries),
        },
        "identities": {
            "wal_family": str(wal_path.expanduser().resolve()),
            "wal_complete_family": wal.complete_family,
            "wal_damaged": wal.damaged,
            "wal_segments": [segment.__dict__ for segment in wal.segments],
            "wal_coverage": coverage,
            "capture_path": capture.path,
            "capture_sha256": capture.sha256,
            "capture_manifest": capture.manifest,
            "capture_rows": {
                "execution": len(capture.executions),
                "closed_pnl": len(capture.closed_pnl),
                "transaction": len(capture.transactions),
                "transfer_in": len(capture.transfers_in),
                "transfer_out": len(capture.transfers_out),
            },
            "capture_unfetched_transfer_sources": list(unfetched),
            "equity_sample_dir": str(samples_dir.expanduser().resolve()),
            "equity_sample_files": sample_files,
            "engine_samples_read": len(samples),
            "capture_user_id": capture_user_id,
            "boundary_sample_user_ids": sample_user_ids,
        },
        "boundaries": boundaries,
        "transactions": transactions,
        "wallet_cash_chain": chain,
        "residuals": {
            "equity_change_usdt": equity_change,
            "transaction_change_total_usdt": transactions["change_total_usdt"],
            "equity_residual_usdt": equity_residual,
            "boundary_straddle": straddle,
            "boundary_straddle_correction_usdt": straddle_correction,
            "equity_residual_less_boundary_straddle_usdt": corrected_residual,
            "equity_residual_is_decomposable": decomposable,
            "cash_residual_usdt": chain["cash_residual_usdt"],
            "decomposition": decomposition,
            "boundary_fields_used": ["equity_usdt", "position_count", "account_age_ms"]
            + ([*DECOMPOSITION_FIELDS] if decomposition["established"] else []),
            "interpretation": "equity residual = (end equity - begin equity) - the venue's summed "
            "account change over the day. The sample's equity is the venue's reading at "
            "ts_ms - account_age_ms, so a cash row between midnight and that reading sits on one side "
            "of the day sum and the other side of the equity reading; those rows are listed and the "
            "gate reads the residual net of them. Both are reported and neither is forced to zero. "
            "While a boundary sample carries wallet_cash_usdt and unrealised_pnl_usdt the residual "
            "splits: the recorded wallet cash is gated against the venue's rows and the mark change "
            "is reported separately. Without those fields it is expected inside tolerance only while "
            "both boundaries read flat, because over an open position the residual also holds the mark "
            "change. The wallet-cash chain comes from the transaction log's own cashBalance column, "
            "which is inferred cash, not an independent boundary snapshot",
        },
        "positions": {"boundaries": positions, "differences": differences},
        "fills": fill_report,
        "sleeve_attribution": sleeves,
        "wal_issues": list(accounting.issues),
        "non_conclusions": [
            "A pass says the day's cash and boundary equity agree; it does not say the strategy was "
            "profitable, nor that the engine's decisions were correct.",
            "A missing row is missing evidence, never a zero fee, funding payment or transfer.",
            "Boundary exposure is folded from the first copied segment's exposure restatement plus "
            "the copied fills; the restatement carries quantities, not entry prices, so a position "
            "opened before the copy has no entry notional here.",
            "The entry-notional comparison is reported, never gated: the venue keeps its own average "
            "entry price and the sample's notional includes the owner's hand exposure.",
            "This report grants no trading or real-money authority.",
        ],
    }
    report["gate"] = "fail" if failures.reasons else "pass"
    report["gate_reasons"] = list(failures.reasons)
    report["missing_requirements"] = list(failures.missing)
    return _json_value(report)


def _cell(value: Any, width: int) -> str:
    text = "-" if value is None else str(value)
    return text.ljust(width)[:width]


def _straddle_lines(straddle: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for name, boundary in straddle.items():
        lines.append(
            f"  straddle {name} [{_iso(boundary['interval_start_ms'])}, "
            f"{_iso(boundary['interval_end_ms_exclusive'])}) sign={boundary['sign']} "
            f"inside_capture={boundary['interval_inside_capture']} rows={len(boundary['rows'])} "
            f"signed_change={boundary['signed_change_usdt']}"
        )
        for row in boundary["rows"]:
            lines.append(
                f"         {_cell(row['type'], 14)} {row['transaction_id']} at {row['venue_ts_utc']} "
                f"change={row['change_usdt']}"
            )
    return lines


def render_table(report: Mapping[str, Any]) -> str:
    scope = report["scope"]
    lines = [
        f"day reconciliation realm={scope['realm']} day={scope['day']} gate={report['gate'].upper()}",
        f"window {scope['window']['start_utc']} .. {scope['window']['end_utc']} "
        f"tolerance={scope['tolerance_usdt']} USDT max_boundary_distance={scope['max_boundary_distance_s']} s",
        "",
        "boundary observations",
        f"  {_cell('name', 6)} {_cell('sample ts UTC', 24)} {_cell('dist s', 9)} "
        f"{_cell('venue reading UTC', 24)} {_cell('dist s', 9)} {_cell('equity USDT', 16)} {_cell('pos', 4)}",
    ]
    for name, boundary in report["boundaries"].items():
        selected = boundary.get("selected")
        if not isinstance(selected, Mapping):
            lines.append(f"  {_cell(name, 6)} absent: no live sample at or after {boundary['boundary_utc']}")
            continue
        lines.append(
            f"  {_cell(name, 6)} {_cell(selected['sample_ts_utc'], 24)} "
            f"{_cell(selected['sample_distance_s'], 9)} {_cell(selected['venue_reading_ts_utc'], 24)} "
            f"{_cell(selected['venue_reading_distance_s'], 9)} {_cell(selected['equity_usdt'], 16)} "
            f"{_cell(selected['position_count'], 4)}"
        )
        lines.append(
            f"         wallet_cash={_cell(selected['wallet_cash_usdt'], 16)} "
            f"unrealised_pnl={_cell(selected['unrealised_pnl_usdt'], 16)} "
            f"positions_listed={'-' if selected['positions'] is None else len(selected['positions'])}"
            f"{' (truncated)' if selected['positions_truncated'] else ''}"
        )
        lines.append(f"         {selected['sample_file']}:{selected['sample_line']}")
    coverage = report["identities"]["wal_coverage"]
    lines += [
        "",
        f"WAL coverage segments={coverage['segment_indices']} complete_family={coverage['complete_family']}",
        f"  first segment {_cell(coverage['first_segment'] and coverage['first_segment']['index'], 6)}"
        f" first record {_cell(coverage['first_segment'] and coverage['first_segment']['first_record_utc'], 24)}"
        f" covers_begin={coverage['covers_begin_boundary_reading']}",
        f"  last  segment {_cell(coverage['last_segment'] and coverage['last_segment']['index'], 6)}"
        f" last  record {_cell(coverage['last_segment'] and coverage['last_segment']['last_record_utc'], 24)}"
        f" covers_end={coverage['covers_end_boundary_reading']}",
    ]
    if coverage["base_restatement"]:
        base = coverage["base_restatement"]
        lines.append(
            f"  base restatement segment {base['segment']} at {base['wall_ts_utc']} "
            f"attributed={base['attributed_positions']} logged={base['logged_positions']}"
        )
    for note in coverage["notes"]:
        lines.append(f"  note {note}")
    transactions = report["transactions"]
    lines += [
        "",
        f"transaction log by venue type ({transactions['rows']} rows)",
        f"  {_cell('type', 14)} {_cell('bucket', 13)} {_cell('rows', 6)} {_cell('change USDT', 18)} "
        f"{_cell('cashFlow', 18)} {_cell('funding', 16)} {_cell('fee', 16)}",
    ]
    for name, entry in sorted(transactions["by_type"].items()):
        lines.append(
            f"  {_cell(name, 14)} {_cell(entry['bucket'], 13)} {_cell(entry['rows'], 6)} "
            f"{_cell(entry['change_usdt'], 18)} {_cell(entry['cash_flow_usdt'], 18)} "
            f"{_cell(entry['funding_usdt'], 16)} {_cell(entry['fee_usdt'], 16)}"
        )
    buckets = transactions["buckets"]
    lines += [
        "",
        "buckets USDT",
        "  " + "  ".join(f"{name}={buckets[name]}" for name in BUCKETS),
        f"  trade_cash={transactions['components']['trade_cash_usdt']} "
        f"fees={transactions['components']['fees_usdt']} "
        f"funding={transactions['components']['funding_usdt']} "
        f"change_total={transactions['change_total_usdt']}",
    ]
    residuals = report["residuals"]
    decomposition = residuals["decomposition"]
    chain = report["wallet_cash_chain"]
    lines += [
        "",
        "residuals USDT",
        f"  equity_change={residuals['equity_change_usdt']} "
        f"transaction_change={residuals['transaction_change_total_usdt']}",
        f"  equity_residual={residuals['equity_residual_usdt']} "
        f"boundary_straddle={residuals['boundary_straddle_correction_usdt']} "
        f"residual_less_straddle={residuals['equity_residual_less_boundary_straddle_usdt']} "
        f"decomposable={residuals['equity_residual_is_decomposable']}",
        *_straddle_lines(residuals["boundary_straddle"]),
        f"  chain_cash begin={chain['begin_cash_usdt']} end={chain['end_cash_usdt']} "
        f"residual={chain['cash_residual_usdt']} gaps={len(chain['gaps'])} "
        f"unfetched_transfers={','.join(chain['unfetched_transfer_sources']) or '-'}",
        f"  decomposition established={decomposition['established']} "
        f"wallet_cash_change={decomposition['wallet_cash_change_usdt']} "
        f"unrealised_change={decomposition['unrealised_pnl_change_usdt']} "
        f"wallet_cash_residual={decomposition['wallet_cash_residual_usdt']} "
        f"identity_residual={decomposition['identity_residual_usdt']}",
        "",
        "positions",
    ]
    for name, boundary in report["positions"]["boundaries"].items():
        lines.append(
            f"  {_cell(name, 6)} wal_count={boundary['wal_position_count']} "
            f"sample_count={boundary['sample_position_count']} "
            f"wal_notional={boundary['wal_entry_notional_usdt']} "
            f"sample_notional={boundary['sample_entry_notional_usdt']} "
            f"notional_difference={boundary['entry_notional_difference_usdt']} (not gated)"
        )
        for row in boundary["wal_positions"]:
            lines.append(
                f"         {_cell(row['symbol'], 14)} qty={row['signed_qty']} "
                f"entry_notional={row['entry_notional_usdt'] or 'unpriced'} "
                f"sleeves={','.join(row['sleeves']) or '-'}"
            )
    for difference in report["positions"]["differences"]:
        lines.append(f"  difference {json.dumps(difference, sort_keys=True)}")
    lines += ["", "sleeve attribution"]
    for sleeve, entry in report["sleeve_attribution"]["sleeves"].items():
        lines.append(
            f"  {_cell(sleeve, 12)} day_fills={entry['day_fills']} day_fee={entry['day_fee_usdt']} "
            f"closed_in_day={len(entry['closed_trades_in_day'])} "
            f"realised_net={entry['realised_net_usdt_closed_in_day']} "
            f"open_at_wal_tail={','.join(entry['open_at_wal_tail']) or '-'}"
        )
    fills = report["fills"]
    lines += [
        "",
        f"fills wal={fills['wal_fills']} venue={fills['venue_trade_executions']} "
        f"wal_fee={fills['wal_fee_usdt']} venue_fee={fills['venue_execution_fee_usdt']} "
        f"transaction_fee={fills['transaction_log_trade_fee_usdt']}",
        "",
        f"gate {report['gate'].upper()}",
    ]
    for reason in report["gate_reasons"]:
        lines.append(f"  - {reason}")
    if not report["gate_reasons"]:
        lines.append("  - no unexplained residual, no missing requirement")
    return "\n".join(lines) + "\n"


def write_text(path: Path, text: str) -> None:
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        resolved.unlink(missing_ok=True)
        raise


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--realm", required=True, choices=("demo", "mainnet"))
    parser.add_argument("--day", required=True, help="the UTC day to reconcile, YYYY-MM-DD")
    parser.add_argument("--wal", required=True, type=Path, help="copied engine WAL family path")
    parser.add_argument("--capture", required=True, type=Path, help="captured Bybit account-history JSONL")
    parser.add_argument(
        "--equity-samples",
        required=True,
        type=Path,
        help="directory holding the recorder's engine-<realm>-<YYYY-MM>.jsonl samples",
    )
    parser.add_argument(
        "--tolerance-usdt",
        type=Decimal,
        default=DEFAULT_TOLERANCE_USDT,
        help=f"declared residual tolerance (default {DEFAULT_TOLERANCE_USDT})",
    )
    parser.add_argument(
        "--max-boundary-distance-s",
        type=int,
        default=DEFAULT_MAX_BOUNDARY_DISTANCE_S,
        help=f"declared maximum boundary distance from midnight (default {DEFAULT_MAX_BOUNDARY_DISTANCE_S})",
    )
    parser.add_argument("--out", required=True, type=Path, help="new mode-0600 JSON report; the table goes beside it")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = reconcile_day(
            realm=args.realm,
            day=args.day,
            wal_path=args.wal,
            capture_path=args.capture,
            samples_dir=args.equity_samples,
            tolerance_usdt=args.tolerance_usdt,
            max_boundary_distance_s=args.max_boundary_distance_s,
        )
        table = render_table(report)
        write_report(args.out, report)
        write_text(args.out.with_suffix(".txt"), table)
    except (EvidenceError, OSError) as exc:
        raise SystemExit(f"day reconciliation evidence is unreadable: {exc}") from None
    print(table, end="")
    print(f"report {args.out.expanduser().resolve()}")
    return 0 if report["gate"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
