"""Reconcile a flat demo account journal to immutable Bybit accounting rows."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from liquidity_migration.core.artifact_snapshot import StableFileSnapshot, read_stable_file
from liquidity_migration.account.account_contracts import (
    AccountEvent,
    AccountEventType,
)
from liquidity_migration.account.account_kernel import (
    account_transactions_path,
    read_account_journal_bytes,
    reduce_account_events,
)
from liquidity_migration.account.execution_environment import ExecutionEnvironment, execution_environment
from liquidity_migration.core.deterministic_serialization import canonical_json, json_safe


VENUE_ACCOUNTING_SCHEMA_VERSION = 1
VENUE_ACCOUNTING_EVIDENCE_SCOPE = "bybit_demo_account_pnl_funding_reconciliation"


@dataclass(frozen=True, slots=True)
class VenueAccountingRequirements:
    """Prospective sample floors and numeric comparison tolerances."""

    min_trade_rows: int = 2
    min_closed_pnl_rows: int = 1
    min_funding_rows: int = 1
    quantity_abs_tolerance: float = 1e-12
    price_abs_tolerance: float = 1e-8
    amount_abs_tolerance: float = 1e-8
    relative_tolerance: float = 1e-9

    def __post_init__(self) -> None:
        for label, minimum in (
            ("min_trade_rows", self.min_trade_rows),
            ("min_closed_pnl_rows", self.min_closed_pnl_rows),
            ("min_funding_rows", self.min_funding_rows),
        ):
            if minimum < 0:
                raise ValueError(f"{label} cannot be negative")
        for label, tolerance in (
            ("quantity_abs_tolerance", self.quantity_abs_tolerance),
            ("price_abs_tolerance", self.price_abs_tolerance),
            ("amount_abs_tolerance", self.amount_abs_tolerance),
            ("relative_tolerance", self.relative_tolerance),
        ):
            if not math.isfinite(tolerance) or tolerance < 0.0:
                raise ValueError(f"{label} must be finite and non-negative")


REGISTERED_VENUE_ACCOUNTING_REQUIREMENTS = VenueAccountingRequirements()


def require_registered_venue_accounting_requirements(
    requirements: VenueAccountingRequirements,
) -> VenueAccountingRequirements:
    """Reject any receipt or invocation that weakens the prospective gate."""

    registered = REGISTERED_VENUE_ACCOUNTING_REQUIREMENTS
    for label in (
        "min_trade_rows",
        "min_closed_pnl_rows",
        "min_funding_rows",
    ):
        observed = int(getattr(requirements, label))
        minimum = int(getattr(registered, label))
        if observed < minimum:
            raise ValueError(
                f"{label}={observed} weakens the registered minimum {minimum}"
            )
    for label in (
        "quantity_abs_tolerance",
        "price_abs_tolerance",
        "amount_abs_tolerance",
        "relative_tolerance",
    ):
        observed_tolerance = float(getattr(requirements, label))
        maximum_tolerance = float(getattr(registered, label))
        if observed_tolerance > maximum_tolerance:
            raise ValueError(
                f"{label}={observed_tolerance} weakens the registered maximum "
                f"{maximum_tolerance}"
            )
    return requirements


def _number(value: Any, *, label: str, empty_is_zero: bool = False) -> float:
    if empty_is_zero and value in (None, ""):
        return 0.0
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(output):
        raise ValueError(f"{label} must be finite")
    return output


def _integer(value: Any, *, label: str) -> int:
    try:
        output = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if output <= 0:
        raise ValueError(f"{label} must be positive")
    return output


def _close(left: float, right: float, *, absolute: float, relative: float) -> bool:
    return abs(left - right) <= max(absolute, relative * max(abs(left), abs(right)))


def _normalized_rows(rows: Sequence[Mapping[str, Any]], *, kind: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"{kind} row {index} must be an object")
        normalized = json_safe(dict(row))
        if not isinstance(normalized, dict):
            raise ValueError(f"{kind} row {index} could not be normalized")
        output.append(normalized)
    if kind == "closed_pnl":
        output.sort(
            key=lambda row: (
                str(row.get("updatedTime") or ""),
                str(row.get("orderId") or ""),
            )
        )
    elif kind in {"trade", "settlement"}:
        output.sort(
            key=lambda row: (
                str(row.get("transactionTime") or ""),
                str(row.get("id") or ""),
                str(row.get("tradeId") or ""),
            )
        )
    elif kind == "position":
        output.sort(
            key=lambda row: (
                str(row.get("symbol") or ""),
                str(row.get("positionIdx") or ""),
                str(row.get("side") or ""),
            )
        )
    else:
        output.sort(
            key=lambda row: (
                str(row.get("symbol") or ""),
                str(row.get("orderId") or ""),
                str(row.get("orderLinkId") or ""),
            )
        )
    return output


def _rows_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json({"rows": list(rows)})).hexdigest()


def _journal_sha256(events: Sequence[AccountEvent]) -> str:
    return hashlib.sha256(
        canonical_json({"events": [event.to_dict() for event in events]})
    ).hexdigest()


def _snapshot_fingerprint(
    snapshots: Mapping[str, StableFileSnapshot],
) -> dict[str, tuple[Any, ...]]:
    return {
        label: (
            str(snapshot.path),
            snapshot.device,
            snapshot.inode,
            snapshot.metadata.st_mode,
            snapshot.uid,
            snapshot.nlink,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.metadata.st_ctime_ns,
            snapshot.sha256,
        )
        for label, snapshot in sorted(snapshots.items())
    }


def _read_journal_snapshot(
    root: Path,
) -> tuple[list[AccountEvent], dict[str, StableFileSnapshot]]:
    transaction_root = account_transactions_path(root)
    transaction_paths = (
        sorted(transaction_root.glob("*.json")) if transaction_root.is_dir() else []
    )
    snapshots: dict[str, StableFileSnapshot] = {}
    if not transaction_paths:
        raise ValueError("account journal has no authoritative transaction segments")
    for path in transaction_paths:
        label = f"transactions/{path.name}"
        snapshots[label] = read_stable_file(
            path,
            label=f"venue-accounting journal {label}",
            require_single_link=False,
        )
    events = read_account_journal_bytes(
        transaction_files=[
            (label.removeprefix("transactions/"), snapshots[label].data)
            for label in sorted(snapshots)
        ],
        verify=True,
    )
    return events, snapshots


def _event_timestamp_ns(event: AccountEvent) -> int:
    payload = event.payload
    return int(
        payload.get("exchange_ts_ns")
        or payload.get("local_receive_ts_ns")
        or payload.get("local_ack_ts_ns")
        or event.wall_ts_ns
    )


def _transaction_identity(row: Mapping[str, Any], *, kind: str) -> str:
    if kind == "trade":
        return str(row.get("tradeId") or "")
    return str(row.get("id") or "")


def _active_positions(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if abs(_number(row.get("size"), label="position size", empty_is_zero=True)) > 0.0]


def _source_payload(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"row_count": len(rows), "rows_sha256": _rows_sha256(rows), "rows": list(rows)}


def build_venue_accounting_receipt(
    *,
    account_root: str | Path,
    expected_account_id: str,
    query_start_ms: int,
    query_end_ms: int,
    observed_ts_ns: int,
    closed_pnl_rows: Sequence[Mapping[str, Any]],
    trade_rows: Sequence[Mapping[str, Any]],
    settlement_rows: Sequence[Mapping[str, Any]],
    pre_position_rows: Sequence[Mapping[str, Any]],
    pre_open_order_rows: Sequence[Mapping[str, Any]],
    post_position_rows: Sequence[Mapping[str, Any]],
    post_open_order_rows: Sequence[Mapping[str, Any]],
    requirements: VenueAccountingRequirements | None = None,
    environment: str = ExecutionEnvironment.DEMO.value,
) -> dict[str, Any]:
    """Build a self-hashed, source-bound accounting reconciliation.

    ``three_way_reconciliation`` reads ``environment`` off the receipt to decide
    which fleet the evidence belongs to, so a wrong label routes it to the
    wrong account.
    """

    selected_environment = execution_environment(environment).value
    if not expected_account_id.strip() or observed_ts_ns <= 0:
        raise ValueError("venue accounting requires an account id and observation time")
    start_ms = int(query_start_ms)
    end_ms = int(query_end_ms)
    if start_ms <= 0 or end_ms <= start_ms:
        raise ValueError("venue accounting query window must be positive and increasing")
    if end_ms - start_ms > 7 * 24 * 60 * 60 * 1000:
        raise ValueError("venue accounting query window cannot exceed seven days")
    if observed_ts_ns < end_ms * 1_000_000:
        raise ValueError("venue accounting observation cannot precede the query window")
    requirements = requirements or VenueAccountingRequirements()
    account_path = Path(account_root).expanduser().resolve(strict=True)
    events, journal_snapshots = _read_journal_snapshot(account_path)
    if not events:
        raise ValueError("demo account journal is empty")
    account_ids = {event.account_id for event in events}
    if account_ids != {expected_account_id}:
        raise ValueError(
            f"journal account ids {sorted(account_ids)!r} do not equal {expected_account_id!r}"
        )
    state = reduce_account_events(events)

    closed_rows = _normalized_rows(closed_pnl_rows, kind="closed_pnl")
    trades = _normalized_rows(trade_rows, kind="trade")
    settlements = _normalized_rows(settlement_rows, kind="settlement")
    pre_positions = _normalized_rows(pre_position_rows, kind="position")
    pre_orders = _normalized_rows(pre_open_order_rows, kind="order")
    post_positions = _normalized_rows(post_position_rows, kind="position")
    post_orders = _normalized_rows(post_open_order_rows, kind="order")

    mismatches: list[str] = []
    gates: dict[str, bool] = {}

    local_flat = (
        not state.working_order_ids
        and all(abs(position.signed_qty) <= requirements.quantity_abs_tolerance for position in state.positions.values())
        and all(abs(quantity) <= requirements.quantity_abs_tolerance for quantity in state.aggregate_targets.values())
    )
    gates["final_local_flatness"] = local_flat
    if not local_flat:
        mismatches.append("canonical account journal is not flat and terminal")

    venue_flat = (
        not _active_positions(pre_positions)
        and not pre_orders
        and not _active_positions(post_positions)
        and not post_orders
    )
    gates["venue_flatness_before_and_after"] = venue_flat
    if not venue_flat:
        mismatches.append("Bybit demo was not flat with zero open orders before and after capture")

    fill_events = [event for event in events if event.event_type == AccountEventType.FILL.value]
    fill_by_id = {
        str(event.payload.get("execution_id") or ""): event
        for event in fill_events
        if event.payload.get("execution_id")
    }
    if len(fill_by_id) != len(fill_events):
        raise ValueError("canonical journal has missing or duplicate execution identities")
    target_batch_symbols = {
        (
            str(target.get("batch_id") or ""),
            str(target.get("symbol") or "").upper(),
        )
        for target in state.target_proposals.values()
    }
    filled_command_ids = {
        str(event.payload.get("command_id") or "") for event in fill_events
    }
    target_order_fill_lineage = bool(fill_events) and all(
        command_id in state.orders
        and bool(state.orders[command_id].venue_order_id)
        and (state.orders[command_id].batch_id, state.orders[command_id].symbol)
        in target_batch_symbols
        for command_id in filled_command_ids
    )
    gates["canonical_target_order_fill_lineage"] = target_order_fill_lineage
    if not target_order_fill_lineage:
        mismatches.append(
            "canonical fills lack complete target-batch, order-command, acknowledgement, or venue-order lineage"
        )
    trade_by_id: dict[str, dict[str, Any]] = {}
    for row in trades:
        identity = _transaction_identity(row, kind="trade")
        if not identity or identity in trade_by_id:
            raise ValueError("Bybit TRADE rows have missing or duplicate tradeId")
        if str(row.get("type") or "").upper() != "TRADE":
            raise ValueError(f"transaction {identity!r} is not type=TRADE")
        trade_by_id[identity] = row
    trade_floor = len(trades) >= requirements.min_trade_rows
    trade_identity = set(trade_by_id) == set(fill_by_id)
    gates["trade_sample_floor"] = trade_floor
    gates["trade_identity_coverage"] = trade_identity
    if not trade_floor:
        mismatches.append(
            f"TRADE row floor failed: {len(trades)} < {requirements.min_trade_rows}"
        )
    if not trade_identity:
        mismatches.append("Bybit TRADE tradeId set differs from canonical execution_id set")

    window_ok = True
    trade_fields_ok = trade_identity
    fee_provenance_ok = True
    trade_cash_flow = 0.0
    trade_fees = 0.0
    trade_changes = 0.0
    for execution_id, event in fill_by_id.items():
        metadata = event.payload.get("metadata") or {}
        if not isinstance(metadata, Mapping) or metadata.get("fee_observed") is not True:
            fee_provenance_ok = False
        timestamp_ns = _event_timestamp_ns(event)
        if timestamp_ns < start_ms * 1_000_000 or timestamp_ns > end_ms * 1_000_000:
            window_ok = False
        trade_row = trade_by_id.get(execution_id)
        if trade_row is None:
            continue
        transaction_ms = _integer(
            trade_row.get("transactionTime"), label=f"TRADE {execution_id} time"
        )
        if transaction_ms < start_ms or transaction_ms > end_ms:
            window_ok = False
        if str(trade_row.get("category") or "").lower() != "linear" or str(
            trade_row.get("currency") or ""
        ).upper() != "USDT":
            trade_fields_ok = False
        command_id = str(event.payload.get("command_id") or "")
        order = state.orders.get(command_id)
        if order is None:
            raise ValueError(f"execution {execution_id!r} references an unknown command")
        side = str(trade_row.get("side") or "").lower()
        signed_qty = _number(event.payload.get("signed_qty"), label=f"fill {execution_id} quantity")
        venue_signed_qty = (
            _number(trade_row.get("qty"), label=f"TRADE {execution_id} quantity")
            if side == "buy"
            else -_number(
                trade_row.get("qty"), label=f"TRADE {execution_id} quantity"
            )
            if side == "sell"
            else 0.0
        )
        venue_fee = _number(
            trade_row.get("fee"), label=f"TRADE {execution_id} fee"
        )
        cash_flow = _number(
            trade_row.get("cashFlow"), label=f"TRADE {execution_id} cashFlow"
        )
        funding = _number(
            trade_row.get("funding"),
            label=f"TRADE {execution_id} funding",
            empty_is_zero=True,
        )
        change = _number(
            trade_row.get("change"), label=f"TRADE {execution_id} change"
        )
        if not _close(
            change,
            cash_flow + funding - venue_fee,
            absolute=requirements.amount_abs_tolerance,
            relative=requirements.relative_tolerance,
        ):
            trade_fields_ok = False
        order_link = str(trade_row.get("orderLinkId") or "")
        if (
            str(trade_row.get("symbol") or "").upper() != event.symbol
            or str(trade_row.get("orderId") or "") != order.venue_order_id
            or (order_link and order_link != command_id)
            or not _close(
                venue_signed_qty,
                signed_qty,
                absolute=requirements.quantity_abs_tolerance,
                relative=requirements.relative_tolerance,
            )
            or not _close(
                _number(
                    trade_row.get("tradePrice"),
                    label=f"TRADE {execution_id} price",
                ),
                _number(event.payload.get("price"), label=f"fill {execution_id} price"),
                absolute=requirements.price_abs_tolerance,
                relative=requirements.relative_tolerance,
            )
            or not _close(
                venue_fee,
                _number(event.payload.get("fee_usdt"), label=f"fill {execution_id} fee"),
                absolute=requirements.amount_abs_tolerance,
                relative=requirements.relative_tolerance,
            )
        ):
            trade_fields_ok = False
        trade_cash_flow = math.fsum((trade_cash_flow, cash_flow))
        trade_fees = math.fsum((trade_fees, venue_fee))
        trade_changes = math.fsum((trade_changes, change))
    gates["query_window_covers_canonical_rows"] = window_ok
    gates["trade_fields_match"] = trade_fields_ok
    gates["fill_fee_provenance_observed"] = fee_provenance_ok
    if not window_ok:
        mismatches.append("query window does not cover every canonical/venue accounting row")
    if not trade_fields_ok:
        mismatches.append("Bybit TRADE rows disagree with canonical fill identity, quantity, price, fee, or cash equation")
    if not fee_provenance_ok:
        mismatches.append("one or more canonical fills lacks observed execution-fee provenance")

    fill_pnl_rows: list[Mapping[str, Any]] = []
    accounted_execution_ids: set[str] = set()
    for payload in state.pnl.values():
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        if bool(metadata.get("fill_accounting_checkpoint")) or str(payload.get("source") or "").startswith(
            "fill_reconstructed"
        ):
            fill_pnl_rows.append(payload)
            ids = metadata.get("accounted_execution_ids") or ()
            if isinstance(ids, Sequence) and not isinstance(ids, (str, bytes, bytearray)):
                accounted_execution_ids.update(str(value) for value in ids if str(value))
    local_fill_gross = math.fsum(
        _number(row.get("gross_pnl_usdt"), label="canonical fill gross PnL") for row in fill_pnl_rows
    )
    local_fill_fees = math.fsum(
        _number(row.get("fee_usdt"), label="canonical fill fee") for row in fill_pnl_rows
    )
    local_fill_net = math.fsum(
        _number(row.get("net_pnl_usdt"), label="canonical fill net PnL") for row in fill_pnl_rows
    )
    pnl_coverage = set(fill_by_id) == accounted_execution_ids
    pnl_totals_match = all(
        (
            _close(
                local_fill_gross,
                trade_cash_flow,
                absolute=requirements.amount_abs_tolerance,
                relative=requirements.relative_tolerance,
            ),
            _close(
                local_fill_fees,
                trade_fees,
                absolute=requirements.amount_abs_tolerance,
                relative=requirements.relative_tolerance,
            ),
            _close(
                local_fill_net,
                trade_changes,
                absolute=requirements.amount_abs_tolerance,
                relative=requirements.relative_tolerance,
            ),
        )
    )
    gates["canonical_fill_pnl_execution_coverage"] = pnl_coverage
    gates["canonical_fill_pnl_matches_transaction_log"] = pnl_totals_match
    if not pnl_coverage:
        mismatches.append("canonical fill PnL does not account for the exact execution-id set")
    if not pnl_totals_match:
        mismatches.append("canonical fill gross/fee/net totals disagree with Bybit TRADE cashFlow/fee/change")

    reduce_orders_by_venue_id = {
        order.venue_order_id: order
        for command_id, order in state.orders.items()
        if order.reduce_only
        and order.venue_order_id
        and any(
            str(event.payload.get("command_id") or "") == command_id
            for event in fill_events
        )
    }
    reduce_order_ids = set(reduce_orders_by_venue_id)
    closed_by_order: dict[str, dict[str, Any]] = {}
    closed_total = closed_fee_total = closed_gross_total = 0.0
    for row in closed_rows:
        order_id = str(row.get("orderId") or "")
        if not order_id or order_id in closed_by_order:
            raise ValueError("Bybit closed-PnL rows have missing or duplicate orderId")
        closed_by_order[order_id] = row
        closed_pnl = _number(row.get("closedPnl"), label=f"closed PnL {order_id}")
        open_fee = _number(row.get("openFee"), label=f"closed PnL {order_id} openFee")
        close_fee = _number(row.get("closeFee"), label=f"closed PnL {order_id} closeFee")
        updated_ms = _integer(
            row.get("updatedTime"), label=f"closed PnL {order_id} updatedTime"
        )
        if updated_ms < start_ms or updated_ms > end_ms:
            window_ok = False
        closed_total = math.fsum((closed_total, closed_pnl))
        closed_fee_total = math.fsum((closed_fee_total, open_fee, close_fee))
        closed_gross_total = math.fsum((closed_gross_total, closed_pnl, open_fee, close_fee))
    closed_floor = len(closed_rows) >= requirements.min_closed_pnl_rows
    closed_identity = set(closed_by_order) == reduce_order_ids and all(
        str(row.get("symbol") or "").upper()
        == reduce_orders_by_venue_id[order_id].symbol
        for order_id, row in closed_by_order.items()
        if order_id in reduce_orders_by_venue_id
    )
    closed_totals_match = all(
        (
            _close(
                closed_total,
                trade_changes,
                absolute=requirements.amount_abs_tolerance,
                relative=requirements.relative_tolerance,
            ),
            _close(
                closed_fee_total,
                trade_fees,
                absolute=requirements.amount_abs_tolerance,
                relative=requirements.relative_tolerance,
            ),
            _close(
                closed_gross_total,
                trade_cash_flow,
                absolute=requirements.amount_abs_tolerance,
                relative=requirements.relative_tolerance,
            ),
        )
    )
    gates["closed_pnl_sample_floor"] = closed_floor
    gates["closed_pnl_order_coverage"] = closed_identity
    gates["closed_pnl_matches_transaction_log"] = closed_totals_match
    if not closed_floor:
        mismatches.append(
            f"closed-PnL row floor failed: {len(closed_rows)} < {requirements.min_closed_pnl_rows}"
        )
    if not closed_identity:
        mismatches.append("Bybit closed-PnL orderId set differs from filled reduce-order set")
    if not closed_totals_match:
        mismatches.append("Bybit closed-PnL/openFee/closeFee totals disagree with transaction log")

    settlement_by_id: dict[str, dict[str, Any]] = {}
    for row in settlements:
        identity = _transaction_identity(row, kind="settlement")
        if not identity or identity in settlement_by_id:
            raise ValueError("Bybit SETTLEMENT rows have missing or duplicate id")
        if str(row.get("type") or "").upper() != "SETTLEMENT":
            raise ValueError(f"transaction {identity!r} is not type=SETTLEMENT")
        settlement_by_id[identity] = row
    local_funding_by_id: dict[str, AccountEvent] = {}
    for event in events:
        if event.event_type != AccountEventType.PNL.value or str(
            event.payload.get("source") or ""
        ) != "venue_funding_settlement":
            continue
        payload = event.payload
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ValueError("venue funding PnL event lacks metadata")
        identity = str(metadata.get("venue_transaction_id") or "")
        if not identity or identity in local_funding_by_id:
            raise ValueError("canonical venue-funding rows have missing or duplicate transaction ids")
        local_funding_by_id[identity] = event
    funding_floor = len(settlements) >= requirements.min_funding_rows
    funding_identity = set(settlement_by_id) == set(local_funding_by_id)
    funding_values = funding_identity
    venue_funding_total = venue_funding_net = local_funding_total = local_funding_net = 0.0
    for identity, event in local_funding_by_id.items():
        payload = event.payload
        local_funding_total = math.fsum(
            (
                local_funding_total,
                _number(payload.get("funding_usdt"), label=f"funding {identity} funding"),
            )
        )
        local_funding_net = math.fsum(
            (
                local_funding_net,
                _number(payload.get("net_pnl_usdt"), label=f"funding {identity} net"),
            )
        )
        event_ms = _event_timestamp_ns(event) // 1_000_000
        if event_ms < start_ms or event_ms > end_ms:
            window_ok = False
            funding_values = False
    for identity, row in settlement_by_id.items():
        transaction_ms = _integer(row.get("transactionTime"), label=f"SETTLEMENT {identity} time")
        if transaction_ms < start_ms or transaction_ms > end_ms:
            window_ok = False
            funding_values = False
        if str(row.get("category") or "").lower() != "linear" or str(
            row.get("currency") or ""
        ).upper() != "USDT":
            funding_values = False
        funding = _number(row.get("funding"), label=f"SETTLEMENT {identity} funding", empty_is_zero=True)
        cash_flow = _number(row.get("cashFlow"), label=f"SETTLEMENT {identity} cashFlow", empty_is_zero=True)
        fee = _number(row.get("fee"), label=f"SETTLEMENT {identity} fee", empty_is_zero=True)
        change = _number(row.get("change"), label=f"SETTLEMENT {identity} change")
        venue_funding_total = math.fsum((venue_funding_total, funding))
        venue_funding_net = math.fsum((venue_funding_net, change))
        if not _close(
            change,
            cash_flow + funding - fee,
            absolute=requirements.amount_abs_tolerance,
            relative=requirements.relative_tolerance,
        ):
            funding_values = False
        local_event = local_funding_by_id.get(identity)
        if local_event is None:
            continue
        local = local_event.payload
        if local_event.symbol != str(row.get("symbol") or "").upper():
            funding_values = False
        local_gross = _number(local.get("gross_pnl_usdt"), label=f"funding {identity} gross")
        local_fee = _number(local.get("fee_usdt"), label=f"funding {identity} fee")
        local_funding = _number(local.get("funding_usdt"), label=f"funding {identity} funding")
        local_net = _number(local.get("net_pnl_usdt"), label=f"funding {identity} net")
        if not all(
            (
                _close(local_gross, cash_flow, absolute=requirements.amount_abs_tolerance, relative=requirements.relative_tolerance),
                _close(local_fee, fee, absolute=requirements.amount_abs_tolerance, relative=requirements.relative_tolerance),
                _close(local_funding, funding, absolute=requirements.amount_abs_tolerance, relative=requirements.relative_tolerance),
                _close(local_net, change, absolute=requirements.amount_abs_tolerance, relative=requirements.relative_tolerance),
            )
        ):
            funding_values = False
        if int(local.get("exchange_ts_ns") or 0) != transaction_ms * 1_000_000:
            funding_values = False
    gates["query_window_covers_canonical_rows"] = window_ok
    gates["funding_sample_floor"] = funding_floor
    gates["funding_identity_coverage"] = funding_identity
    gates["funding_values_match"] = funding_values
    if not funding_floor:
        mismatches.append(
            f"funding row floor failed: {len(settlements)} < {requirements.min_funding_rows}"
        )
    if not funding_identity:
        mismatches.append("Bybit SETTLEMENT id set differs from canonical venue-funding id set")
    if not funding_values:
        mismatches.append("canonical venue-funding values disagree with Bybit SETTLEMENT rows")

    passed = all(gates.values()) and not mismatches
    receipt: dict[str, Any] = {
        "schema_version": VENUE_ACCOUNTING_SCHEMA_VERSION,
        "evidence_scope": VENUE_ACCOUNTING_EVIDENCE_SCOPE,
        "environment": selected_environment,
        "account_id": expected_account_id,
        "account_root": str(account_path),
        "observed_ts_ns": int(observed_ts_ns),
        "query_window_ms": {"start": start_ms, "end": end_ms},
        "requirements": asdict(requirements),
        "journal": {
            "event_count": len(events),
            "normalized_journal_sha256": _journal_sha256(events),
            "final_state_hash": events[-1].state_hash,
        },
        "venue_sources": {
            "closed_pnl": _source_payload(closed_rows),
            "trade_transactions": _source_payload(trades),
            "settlement_transactions": _source_payload(settlements),
            "pre_positions": _source_payload(pre_positions),
            "pre_open_orders": _source_payload(pre_orders),
            "post_positions": _source_payload(post_positions),
            "post_open_orders": _source_payload(post_orders),
        },
        "sample_counts": {
            "canonical_targets": len(state.target_proposals),
            "canonical_orders": len(state.orders),
            "canonical_fills": len(fill_events),
            "canonical_fill_pnl_rows": len(fill_pnl_rows),
            "canonical_funding_rows": len(local_funding_by_id),
            "trade_transactions": len(trades),
            "closed_pnl_rows": len(closed_rows),
            "settlement_transactions": len(settlements),
        },
        "totals_usdt": {
            "canonical_fill_gross": local_fill_gross,
            "canonical_fill_fees": local_fill_fees,
            "canonical_fill_net": local_fill_net,
            "venue_trade_cash_flow": trade_cash_flow,
            "venue_trade_fees": trade_fees,
            "venue_trade_change": trade_changes,
            "venue_closed_pnl": closed_total,
            "venue_closed_open_close_fees": closed_fee_total,
            "venue_funding": venue_funding_total,
            "venue_funding_net_change": venue_funding_net,
            "canonical_funding": local_funding_total,
            "canonical_funding_net": local_funding_net,
        },
        "gates": gates,
        "mismatches": mismatches,
        "venue_accounting_gate_passed": passed,
        "final_demo_flatness_gate_passed": gates["final_local_flatness"]
        and gates["venue_flatness_before_and_after"],
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    final_events, final_journal_snapshots = _read_journal_snapshot(account_path)
    if (
        _snapshot_fingerprint(final_journal_snapshots)
        != _snapshot_fingerprint(journal_snapshots)
        or canonical_json({"events": [event.to_dict() for event in final_events]})
        != canonical_json({"events": [event.to_dict() for event in events]})
    ):
        raise RuntimeError("venue-accounting journal mutated during reconciliation")
    return receipt


def verify_venue_accounting_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the receipt from its embedded venue rows and current journal."""

    payload = dict(receipt)
    if int(payload.get("schema_version") or 0) != VENUE_ACCOUNTING_SCHEMA_VERSION:
        raise ValueError("unsupported venue-accounting receipt schema")
    if payload.get("evidence_scope") != VENUE_ACCOUNTING_EVIDENCE_SCOPE:
        raise ValueError("venue-accounting receipt has the wrong evidence scope")
    observed_hash = str(payload.get("artifact_sha256") or "")
    expected_hash = hashlib.sha256(
        canonical_json({**payload, "artifact_sha256": ""})
    ).hexdigest()
    if observed_hash != expected_hash:
        raise ValueError("venue-accounting receipt hash mismatch")
    window = payload.get("query_window_ms")
    raw_requirements = payload.get("requirements")
    sources = payload.get("venue_sources")
    if not isinstance(window, Mapping) or not isinstance(raw_requirements, Mapping) or not isinstance(
        sources, Mapping
    ):
        raise ValueError("venue-accounting receipt lacks window, requirements, or sources")
    required_sources = {
        "closed_pnl",
        "trade_transactions",
        "settlement_transactions",
        "pre_positions",
        "pre_open_orders",
        "post_positions",
        "post_open_orders",
    }
    if set(sources) != required_sources:
        raise ValueError("venue-accounting receipt has the wrong source set")
    source_rows: dict[str, list[Mapping[str, Any]]] = {}
    for name in required_sources:
        source = sources[name]
        if not isinstance(source, Mapping) or not isinstance(source.get("rows"), list):
            raise ValueError(f"venue-accounting source {name!r} is invalid")
        rows = source["rows"]
        if int(source.get("row_count") or 0) != len(rows) or str(
            source.get("rows_sha256") or ""
        ) != _rows_sha256(rows):
            raise ValueError(f"venue-accounting source {name!r} hash/count mismatch")
        source_rows[name] = rows
    requirements = require_registered_venue_accounting_requirements(
        VenueAccountingRequirements(**dict(raw_requirements))
    )
    rebuilt = build_venue_accounting_receipt(
        account_root=str(payload.get("account_root") or ""),
        expected_account_id=str(payload.get("account_id") or ""),
        query_start_ms=int(window.get("start") or 0),
        query_end_ms=int(window.get("end") or 0),
        observed_ts_ns=int(payload.get("observed_ts_ns") or 0),
        closed_pnl_rows=source_rows["closed_pnl"],
        trade_rows=source_rows["trade_transactions"],
        settlement_rows=source_rows["settlement_transactions"],
        pre_position_rows=source_rows["pre_positions"],
        pre_open_order_rows=source_rows["pre_open_orders"],
        post_position_rows=source_rows["post_positions"],
        post_open_order_rows=source_rows["post_open_orders"],
        requirements=requirements,
    )
    if canonical_json(rebuilt) != canonical_json(payload):
        raise ValueError("venue-accounting receipt does not reproduce from bound sources")
    return payload


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        descriptor = os.open(
            str(path),
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("venue-accounting receipt write made no progress")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


def write_venue_accounting_receipt(
    path: str | Path, receipt: Mapping[str, Any]
) -> Path:
    output = Path(path).expanduser()
    if not output.is_absolute():
        raise ValueError("venue-accounting receipt output must be absolute")
    payload = verify_venue_accounting_receipt(receipt)
    _atomic_write(output, json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n")
    return output


def load_venue_accounting_receipt(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    if snapshot is None:
        snapshot = read_stable_file(
            path,
            label="venue-accounting receipt",
            require_single_link=False,
        )
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("venue-accounting receipt snapshot path differs")
    if snapshot.mode & 0o077:
        raise ValueError("venue-accounting receipt must have mode 0600")
    if snapshot.uid != os.geteuid():
        raise ValueError("venue-accounting receipt is not owned by the verifier")
    value = json.loads(snapshot.data)
    if not isinstance(value, Mapping):
        raise ValueError("venue-accounting receipt must contain an object")
    return verify_venue_accounting_receipt(value)
