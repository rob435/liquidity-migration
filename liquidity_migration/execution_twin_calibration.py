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

from .account_kernel import AccountEvent, AccountEventType, read_account_journal
from .deterministic_serialization import canonical_json
from .execution_adapters import ExecutionTwinConfig, LatencyProfile
from .market_capture import capture_record_id


CALIBRATION_SCHEMA_VERSION = 1


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

    def __post_init__(self) -> None:
        counts = (
            self.min_feed_samples,
            self.min_target_events,
            self.min_order_commands,
            self.min_request_ack_samples,
            self.min_filled_orders,
            self.min_pnl_events,
            self.min_symbols,
        )
        if min(counts) < 0:
            raise ValueError("calibration sample floors cannot be negative")
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
        "filled_orders": count(counts, "filled_orders") >= requirements.min_filled_orders,
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


def _read_capture(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    paths = sorted(root.rglob("segment-*.jsonl")) if root.is_dir() else []
    if not paths:
        raise ValueError(f"no market capture segments found under {root}")
    rows: list[dict[str, Any]] = []
    manifest: list[dict[str, str]] = []
    seen_record_ids: set[str] = set()
    for path in paths:
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            raise ValueError(f"market capture segment has a partial final line: {path}")
        manifest.append(
            {
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(data).hexdigest(),
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
    return rows, manifest, manifest_sha256


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
    account_path = Path(account_root).expanduser()
    capture_path = Path(market_capture_root).expanduser()
    events = read_account_journal(account_path, verify=True)
    if not events:
        raise ValueError("demo account journal is empty")
    account_ids = {event.account_id for event in events}
    if account_ids != {expected_account_id}:
        raise ValueError(f"journal account ids {sorted(account_ids)!r} do not equal {expected_account_id!r}")
    capture_rows, capture_manifest, capture_manifest_sha256 = _read_capture(capture_path)

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
    multi_fill_orders = 0
    incomplete_orders = 0
    fill_ratios: list[float] = []
    fill_response_ns: list[float] = []
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
            fills_by_command.get(command_id, ()),
            key=lambda event: (_integer(event.payload.get("exchange_ts_ns")), event.sequence),
        )
        if len(command_fills) > 1:
            multi_fill_orders += 1
        requested_qty = abs(_number(completed_command.payload.get("qty")) or 0.0)
        filled_qty = math.fsum(abs(_number(fill.payload.get("signed_qty")) or 0.0) for fill in command_fills)
        if requested_qty > 0.0 and command_id in fill_order_ids:
            ratio = min(filled_qty / requested_qty, 1.0)
            fill_ratios.append(ratio)
            if ratio < 1.0 - 1e-9:
                incomplete_orders += 1
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
            if previous_exchange_ns and exchange_ns >= previous_exchange_ns:
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
    zero_fill_terminal_orders = len((completed_order_ids & command_ids) - fill_order_ids)
    fee_bps = total_fee_usdt / total_fill_notional * 10_000.0 if total_fill_notional > 0.0 else None
    adjusted_negative_ratio = (
        sum(value < 0.0 for value in feed_adjusted_ns) / len(feed_adjusted_ns) if feed_adjusted_ns else 1.0
    )
    entry_negative_ratio = sum(value < 0.0 for value in order_entry_ns) / len(order_entry_ns) if order_entry_ns else 1.0
    response_negative_ratio = (
        sum(value < 0.0 for value in order_response_ns) / len(order_response_ns) if order_response_ns else 1.0
    )
    symbols = sorted({event.symbol for event in commands if event.symbol})

    sample_gate = {
        "feed_samples": len(feed_adjusted_ns) >= requirements.min_feed_samples,
        "target_events": len(targets) >= requirements.min_target_events,
        "order_commands": len(commands) >= requirements.min_order_commands,
        "request_ack_samples": len(request_ack_rtt_ns) >= requirements.min_request_ack_samples,
        "order_entry_samples": len(order_entry_ns) >= requirements.min_request_ack_samples,
        "order_response_samples": len(order_response_ns) >= requirements.min_request_ack_samples,
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
    }
    execution_twin_gate_passed = all(sample_gate.values())
    partial_upper_bound = (
        min(1.0, 3.0 / filled_with_commands) if filled_with_commands and multi_fill_orders == 0 else None
    )

    receipt: dict[str, Any] = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "kind": "bybit_demo_market_order_execution_twin_calibration",
        "observed_ts_ns": observed_ts_ns,
        "expected_account_id": expected_account_id,
        "scope": {
            "order_type": "market",
            "claim": "calibrates the market-order execution twin only",
            "does_not_establish": [
                "alpha validity",
                "live-runtime parity",
                "deployment authorization",
                "passive limit-order queue position",
                "market impact beyond captured visible depth",
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
            "zero_multi_fill_rule_of_three_upper_bound": partial_upper_bound,
            "allow_partial_fills": True,
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
        "sample_gate": sample_gate,
        "execution_twin_gate_passed": execution_twin_gate_passed,
        "artifact_sha256": "",
    }
    receipt["artifact_sha256"] = hashlib.sha256(canonical_json({**receipt, "artifact_sha256": ""})).hexdigest()
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
    gate_passed = all(value is True for value in expected_gate.values())
    if payload.get("execution_twin_gate_passed") is not gate_passed:
        raise ValueError("execution-twin calibration aggregate gate is inconsistent")
    return payload


def write_calibration_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    payload = verify_calibration_receipt(receipt)
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    fd = os.open(str(temporary), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        data = canonical_json(payload) + b"\n"
        view = memoryview(data)
        written = 0
        while written < len(data):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("calibration receipt write made no progress")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, resolved)
    directory_fd = os.open(str(resolved.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return resolved


def load_calibration_receipt(
    path: str | Path,
    *,
    require_registered_requirements: bool = True,
) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().read_bytes())
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
    """Construct the twin config while keeping stress quantiles explicit."""

    payload = verify_calibration_receipt(
        receipt,
        require_registered_requirements=require_registered_requirements,
    )
    if require_gate and payload.get("execution_twin_gate_passed") is not True:
        raise ValueError("execution-twin calibration sample gate has not passed")
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

    fee_bps = (payload.get("slippage") or {}).get("fee_bps")
    if fee_bps is None:
        raise ValueError("calibration has no observed fee basis")
    residual_distribution = (payload.get("slippage") or {}).get("residual_adverse_bps_after_visible_book") or {}
    residual_slippage = residual_distribution.get(slippage_quantile)
    if residual_slippage is None:
        raise ValueError("calibration has no visible-book residual slippage basis")
    return ExecutionTwinConfig(
        fee_bps=float(fee_bps),
        latency=LatencyProfile(
            decision_to_socket_ns=value("decision_to_socket"),
            order_entry_ns=value("order_entry_clock_adjusted"),
            order_response_ns=value("order_response_clock_adjusted"),
            fill_spacing_ns=max(value("partial_fill_spacing", fallback=1), 1),
        ),
        max_decision_age_ns=max_decision_age_ns,
        allow_partial_fills=True,
        immutable_replay_book=True,
        residual_adverse_slippage_bps=float(residual_slippage),
    )
