"""Calibrate the market-order execution twin from captured Bybit demo tapes.

The calibrator consumes two independently verified inputs: the canonical demo
account journal and raw public-market capture segments.  It never treats a
local/exchange timestamp difference as one-way latency unless an external clock
offset receipt is supplied.  Market-by-price capture cannot identify passive
queue position, so the receipt records that limitation instead of fabricating a
queue calibration.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .account_kernel import (
    AccountEvent,
    AccountEventType,
    account_journal_path,
    account_transactions_path,
    read_account_journal_bytes,
)
from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json
from .execution_adapters import ExecutionTwinConfig, LatencyProfile
from .market_capture import capture_record_id


CALIBRATION_SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class CalibrationRequirements:
    """Predeclared sample floors for a decision-grade demo calibration."""

    min_feed_samples: int = 5_000
    min_target_events: int = 30
    min_order_commands: int = 30
    min_request_ack_samples: int = 30
    min_filled_orders: int = 30
    min_pnl_events: int = 10
    min_symbols: int = 3
    min_context_link_ratio: float = 0.95
    min_reference_match_ratio: float = 0.99
    max_reference_error_bps: float = 0.01
    min_observed_multi_fill_orders: int = 3
    min_partial_fill_spacing_samples: int = 3

    def __post_init__(self) -> None:
        counts = (
            self.min_feed_samples,
            self.min_target_events,
            self.min_order_commands,
            self.min_request_ack_samples,
            self.min_filled_orders,
            self.min_pnl_events,
            self.min_symbols,
            self.min_observed_multi_fill_orders,
            self.min_partial_fill_spacing_samples,
        )
        if min(counts) < 0:
            raise ValueError("calibration sample floors cannot be negative")
        if self.min_observed_multi_fill_orders < 1:
            raise ValueError("min_observed_multi_fill_orders must be positive")
        if self.min_partial_fill_spacing_samples < 1:
            raise ValueError("min_partial_fill_spacing_samples must be positive")
        if not 0.0 <= self.min_context_link_ratio <= 1.0:
            raise ValueError("min_context_link_ratio must be between zero and one")
        if not 0.0 <= self.min_reference_match_ratio <= 1.0:
            raise ValueError("min_reference_match_ratio must be between zero and one")
        if not math.isfinite(self.max_reference_error_bps) or self.max_reference_error_bps < 0.0:
            raise ValueError("max_reference_error_bps must be finite and non-negative")


DECISION_GRADE_CALIBRATION_REQUIREMENTS = CalibrationRequirements()
_COUNT_REQUIREMENT_FIELDS = (
    "min_feed_samples",
    "min_target_events",
    "min_order_commands",
    "min_request_ack_samples",
    "min_filled_orders",
    "min_pnl_events",
    "min_symbols",
    "min_observed_multi_fill_orders",
    "min_partial_fill_spacing_samples",
)


def require_decision_grade_calibration_requirements(
    requirements: CalibrationRequirements,
) -> None:
    """Require a contract at least as strong as the registered cutover floors."""

    baseline = DECISION_GRADE_CALIBRATION_REQUIREMENTS
    weakened = [
        field_name
        for field_name in _COUNT_REQUIREMENT_FIELDS
        if getattr(requirements, field_name) < getattr(baseline, field_name)
    ]
    if requirements.min_context_link_ratio < baseline.min_context_link_ratio:
        weakened.append("min_context_link_ratio")
    if requirements.min_reference_match_ratio < baseline.min_reference_match_ratio:
        weakened.append("min_reference_match_ratio")
    if requirements.max_reference_error_bps > baseline.max_reference_error_bps:
        weakened.append("max_reference_error_bps")
    if weakened:
        raise ValueError("execution-twin requirements weaken registered floors: " + ",".join(weakened))


def _requirements_from_receipt(payload: Mapping[str, Any]) -> CalibrationRequirements:
    raw = payload.get("requirements")
    expected_fields = set(asdict(DECISION_GRADE_CALIBRATION_REQUIREMENTS))
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise ValueError("execution-twin calibration requirements are malformed")
    if any(type(raw.get(field_name)) is not int for field_name in _COUNT_REQUIREMENT_FIELDS):
        raise ValueError("execution-twin calibration count requirements must be integers")
    numeric_fields = expected_fields - set(_COUNT_REQUIREMENT_FIELDS)
    if any(
        isinstance(raw.get(field_name), bool) or not isinstance(raw.get(field_name), (int, float))
        for field_name in numeric_fields
    ):
        raise ValueError("execution-twin calibration ratio/error requirements must be numeric")
    return CalibrationRequirements(**dict(raw))


def _recomputed_sample_gate(payload: Mapping[str, Any], requirements: CalibrationRequirements) -> dict[str, bool]:
    counts = payload.get("sample_counts")
    latency = payload.get("latency_ns")
    inputs = payload.get("inputs")
    if not isinstance(counts, Mapping) or not isinstance(latency, Mapping) or not isinstance(inputs, Mapping):
        raise ValueError("execution-twin calibration gate inputs are malformed")

    def count(mapping: Mapping[str, Any], key: str) -> int:
        value = mapping.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(f"execution-twin calibration count {key!r} is invalid")
        return value

    def distribution_count(key: str) -> int:
        distribution = latency.get(key)
        if not isinstance(distribution, Mapping):
            raise ValueError(f"execution-twin calibration distribution {key!r} is invalid")
        return count(distribution, "count")

    def ratio(key: str) -> float:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"execution-twin calibration ratio {key!r} is invalid")
        normalized = float(value)
        if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
            raise ValueError(f"execution-twin calibration ratio {key!r} is invalid")
        return normalized

    clock_correction = inputs.get("local_minus_exchange_ns")
    clock_hash = inputs.get("clock_offset_receipt_sha256")
    has_clock_receipt = (
        type(clock_correction) is int
        and isinstance(clock_hash, str)
        and len(clock_hash) == 64
        and all(character in "0123456789abcdef" for character in clock_hash)
    )
    filled_orders = count(counts, "filled_orders")
    submit_to_first_fill_orders = count(counts, "submit_to_first_fill_orders")
    fill_response_orders = count(counts, "fill_response_orders")
    return {
        "feed_samples": count(counts, "feed_latency") >= requirements.min_feed_samples,
        "target_events": count(counts, "target_events") >= requirements.min_target_events,
        "order_commands": count(counts, "order_commands") >= requirements.min_order_commands,
        "request_ack_samples": (count(counts, "request_ack_rtt") >= requirements.min_request_ack_samples),
        "order_entry_samples": (
            distribution_count("order_entry_clock_adjusted") >= requirements.min_request_ack_samples
        ),
        "order_response_samples": (
            distribution_count("order_response_clock_adjusted") >= requirements.min_request_ack_samples
        ),
        "submit_to_first_fill_samples": (
            submit_to_first_fill_orders == filled_orders
            and submit_to_first_fill_orders >= requirements.min_filled_orders
            and distribution_count("submit_to_first_fill_clock_adjusted")
            >= submit_to_first_fill_orders
        ),
        "fill_response_samples": (
            fill_response_orders == filled_orders
            and fill_response_orders >= requirements.min_filled_orders
            and distribution_count("fill_response_clock_adjusted") >= fill_response_orders
        ),
        "filled_orders": filled_orders >= requirements.min_filled_orders,
        "pnl_events": count(counts, "pnl_events") >= requirements.min_pnl_events,
        "symbols": count(counts, "symbols") >= requirements.min_symbols,
        "context_link_ratio": ratio("context_link_ratio") >= requirements.min_context_link_ratio,
        "reference_match_ratio": (ratio("reference_match_ratio") >= requirements.min_reference_match_ratio),
        "slippage_samples": (count(counts, "slippage_orders") >= requirements.min_filled_orders),
        "clock_offset_receipt": has_clock_receipt,
        "nonnegative_adjusted_feed_latency": (ratio("negative_adjusted_feed_latency_ratio") <= 0.01),
        "nonnegative_adjusted_order_entry_latency": (ratio("negative_adjusted_order_entry_latency_ratio") <= 0.01),
        "nonnegative_adjusted_order_response_latency": (
            ratio("negative_adjusted_order_response_latency_ratio") <= 0.01
        ),
        "nonnegative_adjusted_submit_to_first_fill_latency": (
            ratio("negative_adjusted_submit_to_first_fill_latency_ratio") <= 0.01
        ),
        "nonnegative_adjusted_fill_response_latency": (
            ratio("negative_adjusted_fill_response_latency_ratio") <= 0.01
        ),
    }


def _recomputed_partial_fill_gate(
    payload: Mapping[str, Any],
    requirements: CalibrationRequirements,
) -> dict[str, bool]:
    fills = payload.get("fills")
    latency = payload.get("latency_ns")
    if not isinstance(fills, Mapping) or not isinstance(latency, Mapping):
        raise ValueError("execution-twin partial-fill gate inputs are malformed")

    observed_orders = fills.get("multi_fill_orders")
    if type(observed_orders) is not int or observed_orders < 0:
        raise ValueError("execution-twin observed multifill order count is invalid")
    spacing = latency.get("partial_fill_spacing")
    if not isinstance(spacing, Mapping):
        raise ValueError("execution-twin partial-fill spacing distribution is invalid")
    spacing_samples = spacing.get("count")
    if type(spacing_samples) is not int or spacing_samples < 0:
        raise ValueError("execution-twin partial-fill spacing count is invalid")
    return {
        "observed_multi_fill_orders": (observed_orders >= requirements.min_observed_multi_fill_orders),
        "partial_fill_spacing_samples": (spacing_samples >= requirements.min_partial_fill_spacing_samples),
    }


def _number(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires at least one value")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p75": None,
            "p95": None,
            "p99": None,
            "max": None,
            "mean": None,
        }
    return {
        "count": len(finite),
        "min": min(finite),
        "p50": _quantile(finite, 0.50),
        "p75": _quantile(finite, 0.75),
        "p95": _quantile(finite, 0.95),
        "p99": _quantile(finite, 0.99),
        "max": max(finite),
        "mean": statistics.fmean(finite),
    }


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
    if transaction_paths:
        for path in transaction_paths:
            label = f"transactions/{path.name}"
            snapshots[label] = read_stable_file(
                path,
                label=f"calibration account journal {label}",
                require_single_link=False,
            )
        events = read_account_journal_bytes(
            transaction_files=[
                (label.removeprefix("transactions/"), snapshots[label].data)
                for label in sorted(snapshots)
            ],
            verify=True,
        )
    else:
        projection = account_journal_path(root)
        snapshots["events.jsonl"] = read_stable_file(
            projection,
            label="calibration account journal projection",
            require_single_link=False,
        )
        events = read_account_journal_bytes(
            projection_data=snapshots["events.jsonl"].data,
            verify=True,
        )
    return events, snapshots


def _read_capture(
    root: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, str]],
    str,
    dict[str, StableFileSnapshot],
]:
    paths = sorted(root.rglob("segment-*.jsonl")) if root.is_dir() else []
    if not paths:
        raise ValueError(f"no market capture segments found under {root}")
    rows: list[dict[str, Any]] = []
    manifest: list[dict[str, str]] = []
    snapshots: dict[str, StableFileSnapshot] = {}
    seen_record_ids: set[str] = set()
    for path in paths:
        relative = str(path.relative_to(root))
        snapshot = read_stable_file(
            path,
            label=f"calibration market capture {relative}",
            require_single_link=False,
        )
        snapshots[relative] = snapshot
        data = snapshot.data
        if data and not data.endswith(b"\n"):
            raise ValueError(f"market capture segment has a partial final line: {path}")
        manifest.append(
            {
                "path": relative,
                "sha256": snapshot.sha256,
            }
        )
        for line_number, raw in enumerate(data.splitlines(), start=1):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid capture JSON: {path}:{line_number}") from exc
            if not isinstance(value, dict) or int(value.get("schema_version", 0)) != 1:
                raise ValueError(f"unknown capture schema: {path}:{line_number}")
            if _integer(value.get("local_receive_ts_ns")) <= 0:
                raise ValueError(f"capture row lacks receive timestamp: {path}:{line_number}")
            record_id = str(value.get("record_id") or "")
            if record_id != capture_record_id(value):
                raise ValueError(f"capture record id mismatch: {path}:{line_number}")
            if record_id in seen_record_ids:
                raise ValueError(f"duplicate capture record id: {path}:{line_number}")
            seen_record_ids.add(record_id)
            rows.append(value)
    manifest_sha256 = hashlib.sha256(canonical_json({"files": manifest})).hexdigest()
    return rows, manifest, manifest_sha256, snapshots


def _journal_sha256(events: Sequence[AccountEvent]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event.to_dict()))
        digest.update(b"\n")
    return digest.hexdigest()


def _events_of_type(events: Sequence[AccountEvent], event_type: AccountEventType) -> list[AccountEvent]:
    return [event for event in events if event.event_type == event_type.value]


def _latency_ns(
    local_ns: int,
    exchange_ns: int,
    *,
    local_minus_exchange_ns: int | None,
) -> int | None:
    if local_ns <= 0 or exchange_ns <= 0 or local_minus_exchange_ns is None:
        return None
    return local_ns - exchange_ns - local_minus_exchange_ns


def _visible_book_vwap(row: Mapping[str, Any], *, signed_qty: float, fill_qty: float) -> float | None:
    """Walk the captured decision book for the quantity actually filled."""

    if signed_qty == 0.0 or fill_qty <= 0.0:
        return None
    raw_levels = row.get("asks" if signed_qty > 0.0 else "bids")
    if not isinstance(raw_levels, Sequence) or isinstance(raw_levels, (str, bytes)):
        return None
    remaining = fill_qty
    notional = 0.0
    executed = 0.0
    for raw_level in raw_levels:
        if not isinstance(raw_level, Sequence) or isinstance(raw_level, (str, bytes)) or len(raw_level) < 2:
            continue
        price = _number(raw_level[0]) or 0.0
        available = _number(raw_level[1]) or 0.0
        if price <= 0.0 or available <= 0.0:
            continue
        quantity = min(remaining, available)
        notional += quantity * price
        executed += quantity
        remaining -= quantity
        if remaining <= 1e-12:
            break
    if remaining > 1e-9 or executed <= 0.0:
        return None
    return notional / executed


def _top_of_book_mid(row: Mapping[str, Any]) -> float | None:
    """Return a sane captured midpoint without trusting level ordering."""

    def prices(raw_levels: Any) -> list[float]:
        if not isinstance(raw_levels, Sequence) or isinstance(raw_levels, (str, bytes)):
            return []
        output: list[float] = []
        for raw_level in raw_levels:
            if not isinstance(raw_level, Sequence) or isinstance(raw_level, (str, bytes)) or not raw_level:
                continue
            price = _number(raw_level[0]) or 0.0
            if price > 0.0:
                output.append(price)
        return output

    bids = prices(row.get("bids"))
    asks = prices(row.get("asks"))
    if not bids or not asks:
        return None
    bid = max(bids)
    ask = min(asks)
    if bid > ask:
        return None
    return (bid + ask) / 2.0


def calibrate_execution_twin(
    *,
    account_root: str | Path,
    market_capture_root: str | Path,
    expected_account_id: str,
    observed_ts_ns: int,
    local_minus_exchange_ns: int | None = None,
    clock_offset_receipt_sha256: str = "",
    requirements: CalibrationRequirements | None = None,
) -> dict[str, Any]:
    """Build a self-hashed calibration receipt without mutating either input."""

    if not expected_account_id.strip() or observed_ts_ns <= 0:
        raise ValueError("calibration requires an explicit account id and observation time")
    if (local_minus_exchange_ns is None) != (not clock_offset_receipt_sha256):
        raise ValueError("clock correction and its source receipt hash must be supplied together")
    if clock_offset_receipt_sha256 and (
        len(clock_offset_receipt_sha256) != 64
        or any(character not in "0123456789abcdef" for character in clock_offset_receipt_sha256)
    ):
        raise ValueError("clock-offset receipt SHA-256 must be 64 lowercase hex characters")
    requirements = requirements or CalibrationRequirements()
    account_path = Path(account_root).expanduser().resolve(strict=True)
    capture_path = Path(market_capture_root).expanduser().resolve(strict=True)
    if not account_path.is_dir() or not capture_path.is_dir():
        raise ValueError("calibration account and market-capture roots must be directories")
    events, journal_snapshots = _read_journal_snapshot(account_path)
    if not events:
        raise ValueError("demo account journal is empty")
    account_ids = {event.account_id for event in events}
    if account_ids != {expected_account_id}:
        raise ValueError(f"journal account ids {sorted(account_ids)!r} do not equal {expected_account_id!r}")
    (
        capture_rows,
        capture_manifest,
        capture_manifest_sha256,
        capture_snapshots,
    ) = _read_capture(capture_path)

    targets = _events_of_type(events, AccountEventType.TARGET)
    commands = _events_of_type(events, AccountEventType.ORDER_COMMAND)
    acks = _events_of_type(events, AccountEventType.ACK)
    ack_observations = _events_of_type(events, AccountEventType.ACK_OBSERVATION)
    fills = _events_of_type(events, AccountEventType.FILL)
    statuses = _events_of_type(events, AccountEventType.ORDER_STATUS)
    pnl_events = _events_of_type(events, AccountEventType.PNL)
    market_refs = _events_of_type(events, AccountEventType.MARKET_INPUT_REF)

    commands_by_id = {
        str(event.payload.get("command_id") or ""): event for event in commands if event.payload.get("command_id")
    }
    market_by_batch_symbol = {
        (str(event.payload.get("batch_id") or event.correlation_id), event.symbol): event for event in market_refs
    }
    capture_by_id = {str(row.get("record_id") or ""): row for row in capture_rows if row.get("record_id")}
    capture_ids = set(capture_by_id)
    linked_contexts = 0
    reference_matches = 0
    reference_error_bps: list[float] = []
    validated_context_by_command: dict[str, Mapping[str, Any]] = {}
    for command in commands:
        market = market_by_batch_symbol.get((command.correlation_id, command.symbol))
        input_key = str(market.payload.get("input_key") or "") if market is not None else ""
        context = capture_by_id.get(input_key) if input_key in capture_ids else None
        if (
            context is None
            or str(context.get("kind") or "") != "book_context"
            or str(context.get("context_kind") or "") != "account_service_decision"
            or str(context.get("reference_key") or "") != command.correlation_id
            or str(context.get("symbol") or "").upper() != command.symbol.upper()
            or context.get("sequence_gap") is not False
        ):
            continue
        midpoint = _top_of_book_mid(context)
        command_reference = _number(command.payload.get("reference_price")) or 0.0
        if midpoint is None or command_reference <= 0.0:
            continue
        command_id = str(command.payload.get("command_id") or "")
        linked_contexts += 1
        validated_context_by_command[command_id] = context
        error_bps = abs(command_reference - midpoint) / midpoint * 10_000.0
        reference_error_bps.append(error_bps)
        if error_bps <= requirements.max_reference_error_bps:
            reference_matches += 1
    context_link_ratio = linked_contexts / len(commands) if commands else 0.0
    reference_match_ratio = reference_matches / len(commands) if commands else 0.0

    feed_apparent_ns: list[float] = []
    feed_adjusted_ns: list[float] = []
    for row in capture_rows:
        kind = str(row.get("kind") or "")
        if kind.startswith("orderbook_"):
            exchange_ns = _integer(row.get("exchange_engine_ts_ns") or row.get("exchange_system_ts_ns"))
        elif kind == "public_trade":
            exchange_ns = _integer(row.get("exchange_trade_ts_ns") or row.get("exchange_system_ts_ns"))
        else:
            continue
        local_ns = _integer(row.get("local_receive_ts_ns"))
        if exchange_ns > 0:
            feed_apparent_ns.append(float(local_ns - exchange_ns))
            adjusted = _latency_ns(
                local_ns,
                exchange_ns,
                local_minus_exchange_ns=local_minus_exchange_ns,
            )
            if adjusted is not None:
                feed_adjusted_ns.append(float(adjusted))

    decision_to_socket_ns: list[float] = []
    request_ack_rtt_ns: list[float] = []
    order_entry_ns: list[float] = []
    order_response_ns: list[float] = []
    accepted_ack_count = sum(ack.payload.get("accepted") is True for ack in acks)
    timing_ack_by_command: dict[str, AccountEvent] = {}
    for ack in (*acks, *ack_observations):
        if ack.payload.get("accepted") is not True:
            continue
        command_id = str(ack.payload.get("command_id") or "")
        metadata = ack.payload.get("metadata") or {}
        if isinstance(metadata, Mapping) and metadata.get("idempotent_existing_order") is True:
            # A duplicate-link lookup is ownership evidence, not create timing.
            continue
        send_ns = _integer(metadata.get("local_socket_send_ts_ns")) if isinstance(metadata, Mapping) else 0
        if not command_id or send_ns <= 0:
            continue
        current = timing_ack_by_command.get(command_id)
        if current is None or (_integer(ack.payload.get("local_ack_ts_ns")), ack.sequence) < (
            _integer(current.payload.get("local_ack_ts_ns")),
            current.sequence,
        ):
            timing_ack_by_command[command_id] = ack
    for command_id, ack in sorted(timing_ack_by_command.items()):
        ack_command = commands_by_id.get(command_id)
        metadata = ack.payload.get("metadata") or {}
        send_ns = _integer(metadata.get("local_socket_send_ts_ns")) if isinstance(metadata, Mapping) else 0
        local_ack_ns = _integer(ack.payload.get("local_ack_ts_ns"))
        exchange_ack_ns = _integer(ack.payload.get("exchange_ts_ns"))
        if send_ns > 0 and ack_command is not None and send_ns >= ack_command.wall_ts_ns:
            decision_to_socket_ns.append(float(send_ns - ack_command.wall_ts_ns))
        if send_ns > 0 and local_ack_ns >= send_ns:
            request_ack_rtt_ns.append(float(local_ack_ns - send_ns))
        if send_ns > 0 and exchange_ack_ns > 0 and local_minus_exchange_ns is not None:
            # local = exchange + offset
            order_entry_ns.append(float(exchange_ack_ns - send_ns + local_minus_exchange_ns))
            order_response_ns.append(float(local_ack_ns - exchange_ack_ns - local_minus_exchange_ns))

    fills_by_command: dict[str, list[AccountEvent]] = {}
    for fill in fills:
        fills_by_command.setdefault(str(fill.payload.get("command_id") or ""), []).append(fill)
    terminal_command_ids = {
        str(status.payload.get("command_id") or "")
        for status in statuses
        if str(status.payload.get("status") or "").lower()
        in {"filled", "partially_filled_cancelled", "cancelled", "rejected"}
    }
    fill_order_ids = {
        command_id
        for command_id, command_fills in fills_by_command.items()
        if any(abs(_number(fill.payload.get("signed_qty")) or 0.0) > 0.0 for fill in command_fills)
    }
    fully_filled_without_status: set[str] = set()
    for command_id in fill_order_ids:
        filled_command = commands_by_id.get(command_id)
        if filled_command is None:
            continue
        requested_qty = abs(_number(filled_command.payload.get("qty")) or 0.0)
        filled_qty = math.fsum(
            abs(_number(fill.payload.get("signed_qty")) or 0.0) for fill in fills_by_command.get(command_id, ())
        )
        if requested_qty > 0.0 and filled_qty >= requested_qty - 1e-12:
            fully_filled_without_status.add(command_id)
    completed_order_ids = terminal_command_ids | fully_filled_without_status
    multi_fill_order_ids: set[str] = set()
    incomplete_order_ids: set[str] = set()
    fill_ratios: list[float] = []
    submit_to_first_fill_ns: list[float] = []
    submit_to_first_fill_command_ids: set[str] = set()
    fill_response_ns: list[float] = []
    fill_response_command_ids: set[str] = set()
    fill_spacing_ns: list[float] = []
    slippage_bps: list[float] = []
    visible_book_slippage_bps: list[float] = []
    residual_slippage_bps: list[float] = []
    weighted_slippage_numerator = 0.0
    weighted_slippage_denominator = 0.0
    total_fee_usdt = 0.0
    total_fill_notional = 0.0
    for command_id in sorted(completed_order_ids):
        completed_command = commands_by_id.get(command_id)
        if completed_command is None:
            continue
        command_fills = sorted(
            (
                fill
                for fill in fills_by_command.get(command_id, ())
                if abs(_number(fill.payload.get("signed_qty")) or 0.0) > 0.0
            ),
            key=lambda event: (_integer(event.payload.get("exchange_ts_ns")), event.sequence),
        )
        if len(command_fills) > 1:
            multi_fill_order_ids.add(command_id)
        requested_qty = abs(_number(completed_command.payload.get("qty")) or 0.0)
        filled_qty = math.fsum(abs(_number(fill.payload.get("signed_qty")) or 0.0) for fill in command_fills)
        if requested_qty > 0.0 and command_id in fill_order_ids:
            ratio = min(filled_qty / requested_qty, 1.0)
            fill_ratios.append(ratio)
            if ratio < 1.0 - 1e-9:
                incomplete_order_ids.add(command_id)
        timing_ack = timing_ack_by_command.get(command_id)
        if command_fills and timing_ack is not None and local_minus_exchange_ns is not None:
            timing_metadata = timing_ack.payload.get("metadata") or {}
            send_ns = (
                _integer(timing_metadata.get("local_socket_send_ts_ns"))
                if isinstance(timing_metadata, Mapping)
                else 0
            )
            first_fill_exchange_ns = _integer(command_fills[0].payload.get("exchange_ts_ns"))
            if send_ns > 0 and first_fill_exchange_ns > 0:
                # local = exchange + offset, so this is the identifiable
                # socket-send -> first exchange fill interval.  It deliberately
                # does not use the API response-envelope timestamp as a
                # matching-engine boundary.
                submit_to_first_fill_ns.append(
                    float(first_fill_exchange_ns - send_ns + local_minus_exchange_ns)
                )
                submit_to_first_fill_command_ids.add(command_id)
        previous_exchange_ns = 0
        reference_price = _number(completed_command.payload.get("reference_price")) or 0.0
        signed_command_qty = _number(completed_command.payload.get("signed_qty")) or 0.0
        direction = 1.0 if signed_command_qty > 0 else -1.0
        actual_fill_notional = 0.0
        actual_fill_qty = 0.0
        for fill in command_fills:
            exchange_ns = _integer(fill.payload.get("exchange_ts_ns"))
            local_ns = _integer(fill.payload.get("local_receive_ts_ns"))
            adjusted = _latency_ns(
                local_ns,
                exchange_ns,
                local_minus_exchange_ns=local_minus_exchange_ns,
            )
            if adjusted is not None:
                fill_response_ns.append(float(adjusted))
                fill_response_command_ids.add(command_id)
            if previous_exchange_ns > 0 and exchange_ns > previous_exchange_ns:
                fill_spacing_ns.append(float(exchange_ns - previous_exchange_ns))
            previous_exchange_ns = exchange_ns or previous_exchange_ns
            price = _number(fill.payload.get("price")) or 0.0
            qty = abs(_number(fill.payload.get("signed_qty")) or 0.0)
            fee = abs(_number(fill.payload.get("fee_usdt")) or 0.0)
            notional = price * qty
            total_fee_usdt += fee
            total_fill_notional += notional
            actual_fill_notional += notional
            actual_fill_qty += qty
            if reference_price > 0.0 and price > 0.0:
                adverse_bps = direction * (price - reference_price) / reference_price * 10_000.0
                slippage_bps.append(adverse_bps)
                weighted_slippage_numerator += adverse_bps * notional
                weighted_slippage_denominator += notional

        validated_context = validated_context_by_command.get(command_id)
        visible_vwap = (
            _visible_book_vwap(
                validated_context,
                signed_qty=signed_command_qty,
                fill_qty=actual_fill_qty,
            )
            if validated_context is not None
            else None
        )
        if reference_price > 0.0 and actual_fill_qty > 0.0 and visible_vwap is not None:
            actual_vwap = actual_fill_notional / actual_fill_qty
            visible_book_slippage_bps.append(direction * (visible_vwap - reference_price) / reference_price * 10_000.0)
            residual_slippage_bps.append(direction * (actual_vwap - visible_vwap) / reference_price * 10_000.0)

    command_ids = set(commands_by_id)
    completed_with_commands = len(completed_order_ids & command_ids)
    filled_with_commands = len(fill_order_ids & command_ids)
    multi_fill_orders = len(multi_fill_order_ids)
    incomplete_orders = len(incomplete_order_ids)
    observed_partial_fill_orders = len(multi_fill_order_ids | incomplete_order_ids)
    zero_fill_terminal_orders = len((completed_order_ids & command_ids) - fill_order_ids)
    fee_bps = total_fee_usdt / total_fill_notional * 10_000.0 if total_fill_notional > 0.0 else None
    adjusted_negative_ratio = (
        sum(value < 0.0 for value in feed_adjusted_ns) / len(feed_adjusted_ns) if feed_adjusted_ns else 1.0
    )
    entry_negative_ratio = sum(value < 0.0 for value in order_entry_ns) / len(order_entry_ns) if order_entry_ns else 1.0
    response_negative_ratio = (
        sum(value < 0.0 for value in order_response_ns) / len(order_response_ns) if order_response_ns else 1.0
    )
    first_fill_negative_ratio = (
        sum(value < 0.0 for value in submit_to_first_fill_ns) / len(submit_to_first_fill_ns)
        if submit_to_first_fill_ns
        else 1.0
    )
    fill_response_negative_ratio = (
        sum(value < 0.0 for value in fill_response_ns) / len(fill_response_ns)
        if fill_response_ns
        else 1.0
    )
    symbols = sorted({event.symbol for event in commands if event.symbol})

    sample_gate = {
        "feed_samples": len(feed_adjusted_ns) >= requirements.min_feed_samples,
        "target_events": len(targets) >= requirements.min_target_events,
        "order_commands": len(commands) >= requirements.min_order_commands,
        "request_ack_samples": len(request_ack_rtt_ns) >= requirements.min_request_ack_samples,
        "order_entry_samples": len(order_entry_ns) >= requirements.min_request_ack_samples,
        "order_response_samples": len(order_response_ns) >= requirements.min_request_ack_samples,
        "submit_to_first_fill_samples": (
            len(submit_to_first_fill_command_ids) == filled_with_commands
            and len(submit_to_first_fill_command_ids) >= requirements.min_filled_orders
        ),
        "fill_response_samples": (
            len(fill_response_command_ids) == filled_with_commands
            and len(fill_response_command_ids) >= requirements.min_filled_orders
        ),
        "filled_orders": filled_with_commands >= requirements.min_filled_orders,
        "pnl_events": len(pnl_events) >= requirements.min_pnl_events,
        "symbols": len(symbols) >= requirements.min_symbols,
        "context_link_ratio": context_link_ratio >= requirements.min_context_link_ratio,
        "reference_match_ratio": (reference_match_ratio >= requirements.min_reference_match_ratio),
        "slippage_samples": len(residual_slippage_bps) >= requirements.min_filled_orders,
        "clock_offset_receipt": local_minus_exchange_ns is not None,
        "nonnegative_adjusted_feed_latency": adjusted_negative_ratio <= 0.01,
        "nonnegative_adjusted_order_entry_latency": entry_negative_ratio <= 0.01,
        "nonnegative_adjusted_order_response_latency": response_negative_ratio <= 0.01,
        "nonnegative_adjusted_submit_to_first_fill_latency": first_fill_negative_ratio <= 0.01,
        "nonnegative_adjusted_fill_response_latency": fill_response_negative_ratio <= 0.01,
    }
    market_order_smoke_gate_passed = all(sample_gate.values())
    partial_upper_bound = (
        min(1.0, 3.0 / filled_with_commands) if filled_with_commands and multi_fill_orders == 0 else None
    )

    partial_fill_gate = {
        "observed_multi_fill_orders": (multi_fill_orders >= requirements.min_observed_multi_fill_orders),
        "partial_fill_spacing_samples": (len(fill_spacing_ns) >= requirements.min_partial_fill_spacing_samples),
    }
    partial_fill_calibration_gate_passed = all(partial_fill_gate.values())
    execution_twin_gate_passed = market_order_smoke_gate_passed and partial_fill_calibration_gate_passed

    receipt: dict[str, Any] = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "kind": "bybit_demo_market_order_execution_twin_calibration",
        "observed_ts_ns": observed_ts_ns,
        "expected_account_id": expected_account_id,
        "scope": {
            "order_type": "market",
            "claim": (
                "estimates market-order latency, visible-book walk, fees, and residual slippage; "
                "partial-fill timing/behavior is calibrated only when its separate gate passes"
            ),
            "does_not_establish": [
                "alpha validity",
                "live-runtime parity",
                "deployment authorization",
                "passive limit-order queue position",
                "market impact beyond captured visible depth",
                "partial-fill probability outside the bounded observed sample",
                "one-to-one correspondence between MBP levels and venue execution partitions",
            ],
        },
        "inputs": {
            "account_root": str(account_path.resolve()),
            "account_journal_sha256": _journal_sha256(events),
            "account_last_event_hash": events[-1].event_hash,
            "market_capture_root": str(capture_path.resolve()),
            "market_capture_manifest": capture_manifest,
            "market_capture_manifest_sha256": capture_manifest_sha256,
            "local_minus_exchange_ns": local_minus_exchange_ns,
            "clock_offset_receipt_sha256": clock_offset_receipt_sha256,
        },
        "requirements": asdict(requirements),
        "sample_counts": {
            "journal_events": len(events),
            "capture_records": len(capture_rows),
            "feed_latency": len(feed_adjusted_ns),
            "target_events": len(targets),
            "order_commands": len(commands),
            "accepted_acks": accepted_ack_count,
            "request_ack_rtt": len(request_ack_rtt_ns),
            "submit_to_first_fill": len(submit_to_first_fill_ns),
            "submit_to_first_fill_orders": len(submit_to_first_fill_command_ids),
            "fill_response": len(fill_response_ns),
            "fill_response_orders": len(fill_response_command_ids),
            "fill_events": len(fills),
            "filled_orders": filled_with_commands,
            "terminal_or_fully_filled_orders": completed_with_commands,
            "pnl_events": len(pnl_events),
            "symbols": len(symbols),
            "linked_order_contexts": linked_contexts,
            "reference_matches": reference_matches,
            "slippage_orders": len(residual_slippage_bps),
        },
        "latency_ns": {
            "feed_apparent_includes_clock_offset": _distribution(feed_apparent_ns),
            "feed_clock_adjusted": _distribution(feed_adjusted_ns),
            "decision_to_socket": _distribution(decision_to_socket_ns),
            "request_ack_round_trip": _distribution(request_ack_rtt_ns),
            "order_entry_clock_adjusted": _distribution(order_entry_ns),
            "order_response_clock_adjusted": _distribution(order_response_ns),
            "submit_to_first_fill_clock_adjusted": _distribution(submit_to_first_fill_ns),
            "fill_response_clock_adjusted": _distribution(fill_response_ns),
            "partial_fill_spacing": _distribution(fill_spacing_ns),
        },
        "fills": {
            "fill_ratio": _distribution(fill_ratios),
            "multi_fill_orders": multi_fill_orders,
            "multi_fill_order_rate": (multi_fill_orders / filled_with_commands if filled_with_commands else None),
            "incomplete_orders": incomplete_orders,
            "incomplete_order_rate": (incomplete_orders / filled_with_commands if filled_with_commands else None),
            "zero_fill_terminal_orders": zero_fill_terminal_orders,
            "observed_partial_fill_orders": observed_partial_fill_orders,
            "zero_multi_fill_rule_of_three_upper_bound": partial_upper_bound,
            "partial_fill_calibrated": partial_fill_calibration_gate_passed,
            "allow_partial_fills": partial_fill_calibration_gate_passed,
            "calibration_scope": (
                "observed multifill/incomplete occurrence, fill ratio, and positive "
                "within-order venue-timestamp spacing only"
            ),
            "book_level_partition_calibrated": False,
            "uncalibrated_behavior": "single_level_full_fill_or_reject",
            "uncertainty": (
                "Observed rates and spacing describe only this bounded market-order sample; "
                "zero events do not identify timing or prove partial fills impossible, and a "
                "passing minimum does not identify passive queue position or market impact."
            ),
        },
        "slippage": {
            "reference": "account decision-boundary captured mid",
            "adverse_bps": _distribution(slippage_bps),
            "visible_book_walk_adverse_bps": _distribution(visible_book_slippage_bps),
            "residual_adverse_bps_after_visible_book": _distribution(residual_slippage_bps),
            "notional_weighted_adverse_bps": (
                weighted_slippage_numerator / weighted_slippage_denominator
                if weighted_slippage_denominator > 0.0
                else None
            ),
            "fee_bps": fee_bps,
        },
        "queue_assumption": {
            "market_orders": "walk visible depth frozen at the decision boundary",
            "future_book_mutation": "disabled; immutable replay-book assumption",
            "passive_limit_orders": "unidentified",
            "capture_granularity": "market_by_price_not_market_by_order",
            "passive_queue_calibrated": False,
            "reason": "MBP capture cannot identify our position in a passive order queue",
        },
        "context_link_ratio": context_link_ratio,
        "reference_match_ratio": reference_match_ratio,
        "reference_error_bps": _distribution(reference_error_bps),
        "negative_adjusted_feed_latency_ratio": adjusted_negative_ratio,
        "negative_adjusted_order_entry_latency_ratio": entry_negative_ratio,
        "negative_adjusted_order_response_latency_ratio": response_negative_ratio,
        "negative_adjusted_submit_to_first_fill_latency_ratio": first_fill_negative_ratio,
        "negative_adjusted_fill_response_latency_ratio": fill_response_negative_ratio,
        "sample_gate": sample_gate,
        "market_order_smoke_gate_passed": market_order_smoke_gate_passed,
        "partial_fill_gate": partial_fill_gate,
        "partial_fill_calibration_gate_passed": partial_fill_calibration_gate_passed,
        "execution_twin_gate_passed": execution_twin_gate_passed,
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = hashlib.sha256(canonical_json({**receipt, "artifact_sha256": ""})).hexdigest()
    final_events, final_journal_snapshots = _read_journal_snapshot(account_path)
    (
        _final_capture_rows,
        final_capture_manifest,
        final_capture_manifest_sha256,
        final_capture_snapshots,
    ) = _read_capture(capture_path)
    if (
        _snapshot_fingerprint(final_journal_snapshots)
        != _snapshot_fingerprint(journal_snapshots)
        or _snapshot_fingerprint(final_capture_snapshots)
        != _snapshot_fingerprint(capture_snapshots)
        or canonical_json({"events": [event.to_dict() for event in final_events]})
        != canonical_json({"events": [event.to_dict() for event in events]})
        or final_capture_manifest != capture_manifest
        or final_capture_manifest_sha256 != capture_manifest_sha256
    ):
        raise RuntimeError("execution-twin calibration sources mutated during computation")
    return receipt


def verify_calibration_receipt(
    receipt: Mapping[str, Any],
    *,
    require_registered_requirements: bool = False,
) -> dict[str, Any]:
    payload = dict(receipt)
    if int(payload.get("schema_version", 0)) != CALIBRATION_SCHEMA_VERSION:
        raise ValueError("unknown execution-twin calibration schema")
    if payload.get("kind") != "bybit_demo_market_order_execution_twin_calibration":
        raise ValueError("unexpected execution-twin calibration kind")
    observed = str(payload.get("artifact_sha256") or "")
    expected = hashlib.sha256(canonical_json({**payload, "artifact_sha256": ""})).hexdigest()
    if observed != expected:
        raise ValueError("execution-twin calibration receipt hash mismatch")
    requirements = _requirements_from_receipt(payload)
    if require_registered_requirements:
        require_decision_grade_calibration_requirements(requirements)
    sample_gate = payload.get("sample_gate")
    if not isinstance(sample_gate, Mapping) or not sample_gate:
        raise ValueError("execution-twin calibration sample gate is missing")
    if any(value is not True and value is not False for value in sample_gate.values()):
        raise ValueError("execution-twin calibration sample gate values must be booleans")
    expected_gate = _recomputed_sample_gate(payload, requirements)
    if dict(sample_gate) != expected_gate:
        raise ValueError("execution-twin calibration sample gate does not reproduce")
    smoke_gate_passed = all(value is True for value in expected_gate.values())
    if payload.get("market_order_smoke_gate_passed") is not smoke_gate_passed:
        raise ValueError("execution-twin market-order smoke gate is inconsistent")

    partial_fill_gate = payload.get("partial_fill_gate")
    if not isinstance(partial_fill_gate, Mapping) or not partial_fill_gate:
        raise ValueError("execution-twin partial-fill gate is missing")
    if any(value is not True and value is not False for value in partial_fill_gate.values()):
        raise ValueError("execution-twin partial-fill gate values must be booleans")
    expected_partial_fill_gate = _recomputed_partial_fill_gate(payload, requirements)
    if dict(partial_fill_gate) != expected_partial_fill_gate:
        raise ValueError("execution-twin partial-fill gate does not reproduce")
    partial_fill_gate_passed = all(value is True for value in expected_partial_fill_gate.values())
    if payload.get("partial_fill_calibration_gate_passed") is not partial_fill_gate_passed:
        raise ValueError("execution-twin partial-fill aggregate gate is inconsistent")
    fills = payload.get("fills")
    if not isinstance(fills, Mapping):
        raise ValueError("execution-twin fill calibration is malformed")
    if fills.get("partial_fill_calibrated") is not partial_fill_gate_passed:
        raise ValueError("execution-twin partial-fill calibration label is inconsistent")
    if fills.get("allow_partial_fills") is not partial_fill_gate_passed:
        raise ValueError("execution-twin partial-fill behavior is inconsistent")
    if fills.get("book_level_partition_calibrated") is not False:
        raise ValueError("execution-twin MBP fill partition must remain an assumption")
    if fills.get("calibration_scope") != (
        "observed multifill/incomplete occurrence, fill ratio, and positive within-order venue-timestamp spacing only"
    ):
        raise ValueError("execution-twin partial-fill calibration scope is unknown")
    if fills.get("uncalibrated_behavior") != "single_level_full_fill_or_reject":
        raise ValueError("execution-twin uncalibrated partial-fill behavior is unknown")

    gate_passed = smoke_gate_passed and partial_fill_gate_passed
    if payload.get("execution_twin_gate_passed") is not gate_passed:
        raise ValueError("execution-twin calibration aggregate gate is inconsistent")
    return payload


def write_calibration_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    payload = verify_calibration_receipt(receipt)
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        fd = os.open(
            str(resolved),
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        try:
            data = canonical_json(payload) + b"\n"
            view = memoryview(data)
            written = 0
            while written < len(data):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise OSError("calibration receipt write made no progress")
                written += count
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(str(resolved.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if created:
            resolved.unlink(missing_ok=True)
        raise
    return resolved


def load_calibration_receipt(
    path: str | Path,
    *,
    require_registered_requirements: bool = True,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    if snapshot is None:
        snapshot = read_stable_file(
            path,
            label="execution-twin calibration receipt",
            require_single_link=False,
        )
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("execution-twin calibration receipt snapshot path differs")
    value = json.loads(snapshot.data)
    if not isinstance(value, dict):
        raise ValueError("execution-twin calibration receipt must be an object")
    return verify_calibration_receipt(
        value,
        require_registered_requirements=require_registered_requirements,
    )


def execution_twin_config_from_calibration(
    receipt: Mapping[str, Any],
    *,
    max_decision_age_ns: int,
    latency_quantile: str = "p50",
    slippage_quantile: str = "p50",
    require_gate: bool = True,
    require_registered_requirements: bool = True,
) -> ExecutionTwinConfig:
    """Construct the twin config while keeping stress quantiles explicit.

    A receipt that passes only the market-order smoke gate can be inspected with
    ``require_gate=False``. Its config fails closed on any multi-level or
    incomplete-fill path; it never invents a spacing value.
    """

    payload = verify_calibration_receipt(
        receipt,
        require_registered_requirements=require_registered_requirements,
    )
    if require_gate and payload.get("execution_twin_gate_passed") is not True:
        raise ValueError("execution-twin calibration gate has not passed")
    if latency_quantile not in {"p50", "p75", "p95", "p99"}:
        raise ValueError("latency_quantile must be p50, p75, p95, or p99")
    if slippage_quantile not in {"p50", "p75", "p95", "p99"}:
        raise ValueError("slippage_quantile must be p50, p75, p95, or p99")
    if max_decision_age_ns <= 0:
        raise ValueError("max_decision_age_ns must be positive")
    latency = payload.get("latency_ns") or {}

    def value(metric: str, *, fallback: int = 0) -> int:
        distribution = latency.get(metric) or {}
        raw = distribution.get(latency_quantile)
        if raw is None:
            return fallback
        return max(int(round(float(raw))), 0)

    def required_value(metric: str) -> int:
        distribution = latency.get(metric) or {}
        raw = distribution.get(latency_quantile)
        if raw is None:
            raise ValueError(f"calibration has no observed {metric} basis")
        return max(int(round(float(raw))), 0)

    fee_bps = (payload.get("slippage") or {}).get("fee_bps")
    if fee_bps is None:
        raise ValueError("calibration has no observed fee basis")
    residual_distribution = (payload.get("slippage") or {}).get("residual_adverse_bps_after_visible_book") or {}
    residual_slippage = residual_distribution.get(slippage_quantile)
    if residual_slippage is None:
        raise ValueError("calibration has no visible-book residual slippage basis")
    partial_fill_calibrated = payload.get("partial_fill_calibration_gate_passed") is True
    if partial_fill_calibrated:
        raw_fill_spacing = (latency.get("partial_fill_spacing") or {}).get(latency_quantile)
        if raw_fill_spacing is None:
            raise ValueError("partial-fill gate passed without an observed spacing basis")
        fill_spacing_ns = value("partial_fill_spacing")
    else:
        fill_spacing_ns = 0
    return ExecutionTwinConfig(
        fee_bps=float(fee_bps),
        latency=LatencyProfile(
            decision_to_socket_ns=value("decision_to_socket"),
            order_entry_ns=value("order_entry_clock_adjusted"),
            order_response_ns=value("order_response_clock_adjusted"),
            fill_spacing_ns=fill_spacing_ns,
            submit_to_first_fill_ns=required_value("submit_to_first_fill_clock_adjusted"),
            fill_response_ns=required_value("fill_response_clock_adjusted"),
        ),
        max_decision_age_ns=max_decision_age_ns,
        allow_partial_fills=partial_fill_calibrated,
        fill_partition_policy=("book_level" if partial_fill_calibrated else "single_level_full_fill_or_reject"),
        immutable_replay_book=True,
        residual_adverse_slippage_bps=float(residual_slippage),
    )
