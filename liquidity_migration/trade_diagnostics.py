"""Read-only command-grain trade diagnostics from canonical account evidence.

The account journal and exact decision-book contexts remain authoritative. This
module creates one deterministic analytical row per venue command; it does not
mutate an account root, infer missing fees/marks, or turn paper observations
into calibrated execution evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence, cast

import polars as pl

from .artifact_snapshot import read_stable_file
from .account_contracts import (
    AccountEvent,
    AccountEventType,
)
from .account_kernel import reduce_account_events
from .deterministic_serialization import canonical_json
from .market_capture import (
    DEFAULT_POST_FILL_MARKOUT_HORIZONS_NS,
    capture_record_id,
)


TRADE_DIAGNOSTIC_SCHEMA_VERSION = 2
MARKOUT_HORIZON_LABELS = {
    1_000_000_000: "1s",
    15_000_000_000: "15s",
    60_000_000_000: "1m",
    300_000_000_000: "5m",
}


class TradeDiagnosticError(RuntimeError):
    """Canonical source evidence cannot support a diagnostic projection."""


EXECUTION_DIAGNOSTIC_SCHEMA = pl.Schema(
    {
        "schema_version": pl.UInt16,
        "account_id": pl.String,
        "batch_id": pl.String,
        "wave_key": pl.String,
        "command_id": pl.String,
        "symbol": pl.String,
        "side": pl.String,
        "action": pl.String,
        "command_created_ts_ns": pl.Int64,
        "reduce_only": pl.Boolean,
        "chunk_index": pl.Int64,
        "chunk_count": pl.Int64,
        "requested_qty": pl.Float64,
        "reference_price": pl.Float64,
        "target_signed_qty": pl.Float64,
        "leverage": pl.Float64,
        "decision_unit_keys_json": pl.String,
        "decision_unit_count": pl.UInt32,
        "strategy_ids_json": pl.String,
        "components_json": pl.String,
        "sleeves_json": pl.String,
        "contributing_target_count": pl.UInt32,
        "journal_command_sequence": pl.Int64,
        "command_event_hash": pl.String,
        "market_input_key": pl.String,
        "capture_record_id": pl.String,
        "context_status": pl.String,
        "book_update_id": pl.Int64,
        "book_sequence": pl.Int64,
        "book_exchange_engine_ts_ns": pl.Int64,
        "book_local_receive_ts_ns": pl.Int64,
        "decision_book_age_ns": pl.Int64,
        "best_bid": pl.Float64,
        "best_ask": pl.Float64,
        "decision_mid": pl.Float64,
        "quoted_spread_bps": pl.Float64,
        "top_imbalance": pl.Float64,
        "microprice": pl.Float64,
        "opposite_touch_qty": pl.Float64,
        "opposite_depth_qty_5bps": pl.Float64,
        "opposite_depth_qty_10bps": pl.Float64,
        "opposite_depth_qty_25bps": pl.Float64,
        "opposite_depth_notional_5bps": pl.Float64,
        "opposite_depth_notional_10bps": pl.Float64,
        "opposite_depth_notional_25bps": pl.Float64,
        "order_to_touch_depth": pl.Float64,
        "order_to_10bps_depth": pl.Float64,
        "order_to_25bps_depth": pl.Float64,
        "book_walk_qty": pl.Float64,
        "book_walk_fill_ratio": pl.Float64,
        "book_walk_vwap": pl.Float64,
        "book_walk_shortfall_bps": pl.Float64,
        "book_exhausted": pl.Boolean,
        "ack_accepted": pl.Boolean,
        "venue_order_id": pl.String,
        "rejection_key": pl.String,
        "terminal_status": pl.String,
        "local_socket_send_ts_ns": pl.Int64,
        "local_ack_ts_ns": pl.Int64,
        "exchange_ack_ts_ns": pl.Int64,
        "first_exchange_fill_ts_ns": pl.Int64,
        "last_exchange_fill_ts_ns": pl.Int64,
        "first_local_fill_receive_ts_ns": pl.Int64,
        "last_local_fill_receive_ts_ns": pl.Int64,
        "decision_to_send_ns": pl.Int64,
        "local_request_rtt_ns": pl.Int64,
        "send_to_first_local_fill_ns": pl.Int64,
        "exchange_ack_to_first_fill_ns": pl.Int64,
        "exchange_fill_span_ns": pl.Int64,
        "feed_delivery_plus_clock_offset_ns": pl.Int64,
        "fill_delivery_plus_clock_offset_ns": pl.Int64,
        "fill_count": pl.UInt32,
        "filled_qty": pl.Float64,
        "fill_ratio": pl.Float64,
        "unfilled_qty": pl.Float64,
        "fill_vwap": pl.Float64,
        "fill_price_min": pl.Float64,
        "fill_price_max": pl.Float64,
        "filled_notional_usdt": pl.Float64,
        "fee_usdt": pl.Float64,
        "fee_coverage": pl.String,
        "maker_fill_fraction": pl.Float64,
        "maker_coverage": pl.String,
        "execution_types_json": pl.String,
        "fee_currencies_json": pl.String,
        "arrival_shortfall_bps": pl.Float64,
        "effective_spread_bps": pl.Float64,
        "reference_shortfall_bps": pl.Float64,
        "book_walk_residual_bps": pl.Float64,
        "fee_bps": pl.Float64,
        "all_in_arrival_bps": pl.Float64,
        "markout_1s_status": pl.String,
        "markout_1s_bps": pl.Float64,
        "post_fill_adverse_1s_bps": pl.Float64,
        "markout_1s_coverage": pl.Float64,
        "markout_1s_actual_horizon_ns": pl.Int64,
        "markout_1s_source_records_json": pl.String,
        "markout_15s_status": pl.String,
        "markout_15s_bps": pl.Float64,
        "post_fill_adverse_15s_bps": pl.Float64,
        "markout_15s_coverage": pl.Float64,
        "markout_15s_actual_horizon_ns": pl.Int64,
        "markout_15s_source_records_json": pl.String,
        "markout_1m_status": pl.String,
        "markout_1m_bps": pl.Float64,
        "post_fill_adverse_1m_bps": pl.Float64,
        "markout_1m_coverage": pl.Float64,
        "markout_1m_actual_horizon_ns": pl.Int64,
        "markout_1m_source_records_json": pl.String,
        "markout_5m_status": pl.String,
        "markout_5m_bps": pl.Float64,
        "post_fill_adverse_5m_bps": pl.Float64,
        "markout_5m_coverage": pl.Float64,
        "markout_5m_actual_horizon_ns": pl.Int64,
        "markout_5m_source_records_json": pl.String,
        "markout_status": pl.String,
        "missing_reasons_json": pl.String,
        "source_event_ids_json": pl.String,
    }
)


_BOOK_NULLS: dict[str, Any] = {
    "book_update_id": None,
    "book_sequence": None,
    "book_exchange_engine_ts_ns": None,
    "book_local_receive_ts_ns": None,
    "best_bid": None,
    "best_ask": None,
    "decision_mid": None,
    "quoted_spread_bps": None,
    "top_imbalance": None,
    "microprice": None,
    "opposite_touch_qty": None,
    "opposite_depth_qty_5bps": None,
    "opposite_depth_qty_10bps": None,
    "opposite_depth_qty_25bps": None,
    "opposite_depth_notional_5bps": None,
    "opposite_depth_notional_10bps": None,
    "opposite_depth_notional_25bps": None,
    "order_to_touch_depth": None,
    "order_to_10bps_depth": None,
    "order_to_25bps_depth": None,
    "book_walk_qty": None,
    "book_walk_fill_ratio": None,
    "book_walk_vwap": None,
    "book_walk_shortfall_bps": None,
    "book_exhausted": None,
}


def _json_list(values: Iterable[object]) -> str:
    return json.dumps(
        sorted({str(value) for value in values if str(value)}),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _finite(value: object, *, label: str) -> float:
    try:
        output = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise TradeDiagnosticError(f"{label} must be numeric") from exc
    if not math.isfinite(output):
        raise TradeDiagnosticError(f"{label} must be finite")
    return output


def _positive(value: object, *, label: str) -> float:
    output = _finite(value, label=label)
    if output <= 0.0:
        raise TradeDiagnosticError(f"{label} must be positive")
    return output


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise TradeDiagnosticError(f"integer diagnostic field is invalid: {value!r}") from exc


def _nonnegative_delta(end: int | None, start: int | None) -> int | None:
    if end is None or start is None or end <= 0 or start <= 0 or end < start:
        return None
    return end - start


def _levels(record: Mapping[str, Any], field: str, *, descending: bool) -> list[tuple[float, float]]:
    raw = record.get(field)
    if not isinstance(raw, list) or not raw:
        raise TradeDiagnosticError(f"book context has no {field}")
    output: list[tuple[float, float]] = []
    for index, level in enumerate(raw):
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise TradeDiagnosticError(f"book context {field}[{index}] is malformed")
        price = _positive(level[0], label=f"{field}[{index}] price")
        qty = _positive(level[1], label=f"{field}[{index}] quantity")
        output.append((price, qty))
    comparisons = zip(output, output[1:], strict=False)
    if descending:
        if any(left[0] <= right[0] for left, right in comparisons):
            raise TradeDiagnosticError("book context bids are not strictly descending")
    elif any(left[0] >= right[0] for left, right in comparisons):
        raise TradeDiagnosticError("book context asks are not strictly ascending")
    return output


def _depth(
    levels: Sequence[tuple[float, float]],
    *,
    midpoint: float,
    direction: int,
    bps: float,
) -> tuple[float, float]:
    if direction > 0:
        selected = [level for level in levels if level[0] <= midpoint * (1.0 + bps / 10_000.0)]
    else:
        selected = [level for level in levels if level[0] >= midpoint * (1.0 - bps / 10_000.0)]
    return (
        math.fsum(qty for _price, qty in selected),
        math.fsum(price * qty for price, qty in selected),
    )


def _book_metrics(
    record: Mapping[str, Any] | None,
    *,
    expected_record_id: str,
    symbol: str,
    direction: int,
    requested_qty: float,
) -> dict[str, Any]:
    if record is None:
        return {"context_status": "missing", "capture_record_id": expected_record_id, **_BOOK_NULLS}
    recorded_id = str(record.get("record_id") or "")
    if recorded_id != expected_record_id:
        raise TradeDiagnosticError(
            f"book context identity mismatch: expected {expected_record_id!r}, got {recorded_id!r}"
        )
    if recorded_id != capture_record_id(record):
        raise TradeDiagnosticError(f"book context {recorded_id!r} has an invalid record identity")
    if str(record.get("kind") or "") != "book_context":
        raise TradeDiagnosticError(f"capture record {recorded_id!r} is not a book context")
    if str(record.get("symbol") or "").upper() != symbol:
        raise TradeDiagnosticError(f"book context {recorded_id!r} has the wrong symbol")

    base = {
        "capture_record_id": recorded_id,
        "book_update_id": _int_or_none(record.get("update_id")),
        "book_sequence": _int_or_none(record.get("cross_sequence")),
        "book_exchange_engine_ts_ns": _int_or_none(
            record.get("exchange_engine_ts_ns") or record.get("exchange_system_ts_ns")
        ),
        "book_local_receive_ts_ns": _int_or_none(record.get("book_local_receive_ts_ns")),
    }
    bids = _levels(record, "bids", descending=True)
    asks = _levels(record, "asks", descending=False)
    if bids[0][0] >= asks[0][0]:
        return {"context_status": "crossed_book", **_BOOK_NULLS, **base}
    if bool(record.get("sequence_gap")):
        return {"context_status": "sequence_gap", **_BOOK_NULLS, **base}

    bid, bid_qty = bids[0]
    ask, ask_qty = asks[0]
    midpoint = bid / 2.0 + ask / 2.0
    top_total = bid_qty + ask_qty
    top_imbalance = (bid_qty - ask_qty) / top_total
    microprice = (ask * bid_qty + bid * ask_qty) / top_total
    opposing = asks if direction > 0 else bids
    qty5, notional5 = _depth(opposing, midpoint=midpoint, direction=direction, bps=5.0)
    qty10, notional10 = _depth(opposing, midpoint=midpoint, direction=direction, bps=10.0)
    qty25, notional25 = _depth(opposing, midpoint=midpoint, direction=direction, bps=25.0)

    remaining = requested_qty
    walked_qty = 0.0
    walked_notional = 0.0
    for price, available in opposing:
        take = min(remaining, available)
        if take <= 0.0:
            break
        walked_qty += take
        walked_notional += take * price
        remaining -= take
        if remaining <= 1e-12:
            break
    walk_vwap = walked_notional / walked_qty if walked_qty > 0.0 else None
    walk_shortfall = (
        10_000.0 * direction * (walk_vwap - midpoint) / midpoint
        if walk_vwap is not None
        else None
    )
    touch_qty = opposing[0][1]

    return {
        "context_status": "healthy",
        **base,
        "best_bid": bid,
        "best_ask": ask,
        "decision_mid": midpoint,
        "quoted_spread_bps": 10_000.0 * (ask - bid) / midpoint,
        "top_imbalance": top_imbalance,
        "microprice": microprice,
        "opposite_touch_qty": touch_qty,
        "opposite_depth_qty_5bps": qty5,
        "opposite_depth_qty_10bps": qty10,
        "opposite_depth_qty_25bps": qty25,
        "opposite_depth_notional_5bps": notional5,
        "opposite_depth_notional_10bps": notional10,
        "opposite_depth_notional_25bps": notional25,
        "order_to_touch_depth": requested_qty / touch_qty,
        "order_to_10bps_depth": requested_qty / qty10 if qty10 > 0.0 else None,
        "order_to_25bps_depth": requested_qty / qty25 if qty25 > 0.0 else None,
        "book_walk_qty": walked_qty,
        "book_walk_fill_ratio": min(walked_qty / requested_qty, 1.0),
        "book_walk_vwap": walk_vwap,
        "book_walk_shortfall_bps": walk_shortfall,
        "book_exhausted": walked_qty < requested_qty - 1e-12,
    }


def _event_metadata(event: AccountEvent | None) -> Mapping[str, Any]:
    if event is None:
        return {}
    metadata = event.payload.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _fee_summary(fills: Sequence[AccountEvent]) -> tuple[float | None, str]:
    if not fills:
        return None, "no_fills"
    classes: list[str] = []
    fees: list[float] = []
    for fill in fills:
        metadata = _event_metadata(fill)
        fees.append(_finite(fill.payload.get("fee_usdt"), label="fill fee"))
        if metadata.get("fee_observed") is True or metadata.get("fee_status") == "observed_execution_fee":
            classes.append("observed")
        elif metadata.get("execution_model_scope") or str(metadata.get("fee_source") or "").startswith(
            "execution_twin"
        ):
            classes.append("modeled")
        else:
            classes.append("missing")
    kinds = set(classes)
    if kinds == {"observed"}:
        return math.fsum(fees), "observed_complete"
    if kinds == {"modeled"}:
        return math.fsum(fees), "modeled_complete"
    if "missing" in kinds:
        return None, "incomplete"
    return math.fsum(fees), "mixed_observed_modeled"


def _maker_summary(fills: Sequence[AccountEvent]) -> tuple[float | None, str]:
    if not fills:
        return None, "no_fills"
    observed: list[tuple[float, bool]] = []
    for fill in fills:
        value = _event_metadata(fill).get("is_maker")
        if type(value) is not bool:
            continue
        observed.append((abs(_finite(fill.payload.get("signed_qty"), label="fill quantity")), value))
    if not observed:
        return None, "missing"
    total = math.fsum(qty for qty, _value in observed)
    fraction = math.fsum(qty for qty, value in observed if value) / total
    return fraction, "complete" if len(observed) == len(fills) else "partial"


def _markout_horizon_metrics(
    fills: Sequence[AccountEvent],
    *,
    schedules: Mapping[str, Mapping[str, Any]],
    observations: Mapping[tuple[str, int], Mapping[str, Any]],
    horizon_ns: int,
) -> dict[str, Any]:
    label = MARKOUT_HORIZON_LABELS[horizon_ns]
    prefix = f"markout_{label}"
    if not fills:
        return {
            f"{prefix}_status": "no_fills",
            f"{prefix}_bps": None,
            f"post_fill_adverse_{label}_bps": None,
            f"{prefix}_coverage": None,
            f"{prefix}_actual_horizon_ns": None,
            f"{prefix}_source_records_json": "[]",
        }

    total_qty = math.fsum(
        abs(_finite(fill.payload.get("signed_qty"), label="markout fill quantity"))
        for fill in fills
    )
    observed: list[tuple[float, float, int, str]] = []
    unavailable: set[str] = set()
    for fill in fills:
        payload = fill.payload
        execution_id = str(payload.get("execution_id") or "")
        command_id = str(payload.get("command_id") or "")
        fill_qty = _finite(payload.get("signed_qty"), label="markout fill quantity")
        fill_price = _positive(payload.get("price"), label="markout fill price")
        fill_exchange_ts_ns = int(payload.get("exchange_ts_ns") or 0)
        fill_local_ts_ns = int(payload.get("local_receive_ts_ns") or 0)
        schedule = schedules.get(execution_id)
        schedule_accepted = False
        schedule_max_lateness_ns = 0
        if schedule is None:
            unavailable.add("not_registered")
        else:
            if (
                schedule.get("schema_version") != 1
                or str(schedule.get("kind") or "")
                != "post_fill_markout_schedule"
                or str(schedule.get("record_id") or "")
                != capture_record_id(schedule)
                or str(schedule.get("execution_id") or "") != execution_id
                or str(schedule.get("command_id") or "") != command_id
                or str(schedule.get("symbol") or "").upper() != fill.symbol
                or not math.isclose(
                    _finite(schedule.get("signed_qty"), label="schedule signed quantity"),
                    fill_qty,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                or not math.isclose(
                    _positive(schedule.get("fill_price"), label="schedule fill price"),
                    fill_price,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                or int(schedule.get("fill_exchange_ts_ns") or 0)
                != fill_exchange_ts_ns
                or int(schedule.get("fill_local_receive_ts_ns") or 0)
                != fill_local_ts_ns
            ):
                raise TradeDiagnosticError(
                    f"post-fill schedule contradicts execution {execution_id!r}"
                )
            raw_horizons = schedule.get("target_horizons_ns")
            if not isinstance(raw_horizons, list) or any(
                type(value) is not int or value <= 0 for value in raw_horizons
            ):
                raise TradeDiagnosticError(
                    f"post-fill schedule has invalid horizons for {execution_id!r}"
                )
            if raw_horizons != sorted(set(raw_horizons)):
                raise TradeDiagnosticError(
                    f"post-fill schedule has non-canonical horizons for {execution_id!r}"
                )
            schedule_status = str(schedule.get("schedule_status") or "")
            if schedule_status not in {"accepted", "rejected_capacity"}:
                raise TradeDiagnosticError(
                    f"post-fill schedule has invalid status for {execution_id!r}"
                )
            schedule_max_lateness_ns = int(
                schedule.get("max_lateness_ns") or 0
            )
            if (
                schedule_max_lateness_ns < 0
                or int(schedule.get("local_receive_ts_ns") or 0)
                < fill_local_ts_ns
                or int(schedule.get("pending_limit") or 0) <= 0
            ):
                raise TradeDiagnosticError(
                    f"post-fill schedule has invalid bounds for {execution_id!r}"
                )
            schedule_accepted = (
                schedule_status == "accepted"
                and horizon_ns in raw_horizons
            )
            if not schedule_accepted:
                unavailable.add("registration_rejected_or_horizon_missing")

        record = observations.get((execution_id, horizon_ns))
        if record is None:
            if schedule_accepted:
                unavailable.add("scheduled_no_observation")
            continue
        if not schedule_accepted:
            raise TradeDiagnosticError(
                f"post-fill observation lacks an accepted schedule for {execution_id!r}"
            )
        if (
            record.get("schema_version") != 1
            or str(record.get("kind") or "") != "post_fill_markout"
            or str(record.get("record_id") or "") != capture_record_id(record)
            or str(record.get("execution_id") or "") != execution_id
            or str(record.get("command_id") or "") != command_id
            or str(record.get("symbol") or "").upper() != fill.symbol
            or int(record.get("target_horizon_ns") or 0) != horizon_ns
            or not math.isclose(
                _finite(record.get("signed_qty"), label="markout signed quantity"),
                fill_qty,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or not math.isclose(
                _positive(record.get("fill_price"), label="markout fill price"),
                fill_price,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or int(record.get("fill_exchange_ts_ns") or 0) != fill_exchange_ts_ns
            or int(record.get("fill_local_receive_ts_ns") or 0) != fill_local_ts_ns
            or int(record.get("max_lateness_ns") or 0)
            != schedule_max_lateness_ns
            or int(record.get("target_local_receive_ts_ns") or 0)
            != fill_local_ts_ns + horizon_ns
        ):
            raise TradeDiagnosticError(
                f"post-fill observation contradicts execution {execution_id!r}"
            )
        actual_horizon_ns = int(record.get("actual_horizon_ns") or 0)
        record_local_ts_ns = int(record.get("local_receive_ts_ns") or 0)
        observation_lateness_ns = int(
            record.get("observation_lateness_ns") or 0
        )
        if (
            actual_horizon_ns != record_local_ts_ns - fill_local_ts_ns
            or actual_horizon_ns < horizon_ns
            or observation_lateness_ns != actual_horizon_ns - horizon_ns
            or int(record.get("book_local_receive_ts_ns") or 0)
            != record_local_ts_ns
        ):
            raise TradeDiagnosticError(
                f"post-fill horizon is invalid for {execution_id!r}"
            )
        status = str(record.get("markout_status") or "")
        if status == "missing":
            missing_reason = str(record.get("missing_reason") or "")
            if (
                record.get("markout_midpoint") is not None
                or not missing_reason
                or observation_lateness_ns <= schedule_max_lateness_ns
            ):
                raise TradeDiagnosticError(
                    f"missing post-fill observation is not explicit for {execution_id!r}"
                )
            unavailable.add(missing_reason)
            continue
        if (
            status != "observed_healthy"
            or bool(record.get("sequence_gap"))
            or str(record.get("missing_reason") or "")
            or observation_lateness_ns > schedule_max_lateness_ns
        ):
            raise TradeDiagnosticError(
                f"post-fill observation has invalid status for {execution_id!r}"
            )
        bids = _levels(record, "bids", descending=True)
        asks = _levels(record, "asks", descending=False)
        if bids[0][0] >= asks[0][0]:
            raise TradeDiagnosticError(
                f"post-fill observed book is crossed for {execution_id!r}"
            )
        midpoint = bids[0][0] / 2.0 + asks[0][0] / 2.0
        recorded_midpoint = _positive(
            record.get("markout_midpoint"),
            label="markout midpoint",
        )
        if not math.isclose(
            midpoint,
            recorded_midpoint,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise TradeDiagnosticError(
                f"post-fill midpoint contradicts its book for {execution_id!r}"
            )
        direction = 1.0 if fill_qty > 0.0 else -1.0
        markout_bps = (
            10_000.0 * direction * (midpoint - fill_price) / fill_price
        )
        observed.append(
            (
                abs(fill_qty),
                markout_bps,
                actual_horizon_ns,
                str(record.get("record_id") or ""),
            )
        )

    observed_qty = math.fsum(item[0] for item in observed)
    coverage = observed_qty / total_qty if total_qty > 0.0 else None
    markout = (
        math.fsum(qty * value for qty, value, _actual, _record_id in observed)
        / observed_qty
        if observed_qty > 0.0
        else None
    )
    actual = (
        int(
            round(
                math.fsum(qty * value for qty, _mark, value, _record_id in observed)
                / observed_qty
            )
        )
        if observed_qty > 0.0
        else None
    )
    if coverage is not None and math.isclose(
        coverage,
        1.0,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        result_status = "complete"
    elif observed:
        result_status = "partial"
    elif len(unavailable) == 1:
        result_status = next(iter(unavailable))
    else:
        result_status = "incomplete_mixed"
    return {
        f"{prefix}_status": result_status,
        f"{prefix}_bps": markout,
        f"post_fill_adverse_{label}_bps": -markout if markout is not None else None,
        f"{prefix}_coverage": coverage,
        f"{prefix}_actual_horizon_ns": actual,
        f"{prefix}_source_records_json": _json_list(
            item[3] for item in observed
        ),
    }


def _terminal_status(
    status_events: Sequence[AccountEvent],
    *,
    ack_accepted: bool | None,
    filled_qty: float,
    requested_qty: float,
) -> str:
    if status_events:
        return str(status_events[-1].payload.get("status") or "unknown")
    if filled_qty >= requested_qty - 1e-12:
        return "filled"
    if filled_qty > 0.0:
        return "partially_filled_unresolved"
    if ack_accepted is False:
        return "rejected"
    if ack_accepted is True:
        return "acknowledged_unresolved"
    return "commanded_unresolved"


def build_execution_diagnostics(
    events: Sequence[AccountEvent],
    book_contexts: Mapping[str, Mapping[str, Any]],
    *,
    markout_schedules: Mapping[str, Mapping[str, Any]] | None = None,
    markout_observations: Mapping[tuple[str, int], Mapping[str, Any]] | None = None,
) -> pl.DataFrame:
    """Build one deterministic row per canonical order command.

    ``events`` must already come from a verified journal snapshot. Replaying the
    reducer here adds a semantic transition check before any metric is emitted.
    Missing contexts remain explicit; malformed or identity-mismatched contexts
    fail the projection.
    """

    reduce_account_events(events)
    markout_schedules = markout_schedules or {}
    markout_observations = markout_observations or {}
    market_by_batch_symbol: dict[tuple[str, str], AccountEvent] = {}
    targets_by_batch_symbol: dict[tuple[str, str], list[AccountEvent]] = defaultdict(list)
    acks_by_command: dict[str, list[AccountEvent]] = defaultdict(list)
    ack_observations_by_command: dict[str, list[AccountEvent]] = defaultdict(list)
    fills_by_command: dict[str, list[AccountEvent]] = defaultdict(list)
    statuses_by_command: dict[str, list[AccountEvent]] = defaultdict(list)
    commands: list[AccountEvent] = []

    for event in events:
        if event.event_type == AccountEventType.MARKET_INPUT_REF.value:
            key = (event.correlation_id, event.symbol)
            if key in market_by_batch_symbol:
                raise TradeDiagnosticError(f"duplicate market input for batch/symbol {key!r}")
            market_by_batch_symbol[key] = event
        elif event.event_type == AccountEventType.TARGET.value:
            targets_by_batch_symbol[(event.correlation_id, event.symbol)].append(event)
        elif event.event_type == AccountEventType.ORDER_COMMAND.value:
            commands.append(event)
        elif event.event_type == AccountEventType.ACK.value:
            acks_by_command[str(event.payload.get("command_id") or "")].append(event)
        elif event.event_type == AccountEventType.ACK_OBSERVATION.value:
            ack_observations_by_command[str(event.payload.get("command_id") or "")].append(event)
        elif event.event_type == AccountEventType.FILL.value:
            fills_by_command[str(event.payload.get("command_id") or "")].append(event)
        elif event.event_type == AccountEventType.ORDER_STATUS.value:
            statuses_by_command[str(event.payload.get("command_id") or "")].append(event)

    rows: list[dict[str, Any]] = []
    for command in sorted(commands, key=lambda event: event.sequence):
        payload = command.payload
        command_id = str(payload.get("command_id") or "")
        if not command_id:
            raise TradeDiagnosticError("order command has no command_id")
        signed_qty = _finite(payload.get("signed_qty"), label=f"command {command_id} signed quantity")
        requested_qty = _positive(payload.get("qty"), label=f"command {command_id} quantity")
        direction = 1 if signed_qty > 0.0 else -1 if signed_qty < 0.0 else 0
        if direction == 0 or not math.isclose(abs(signed_qty), requested_qty, rel_tol=1e-12, abs_tol=1e-12):
            raise TradeDiagnosticError(f"command {command_id} has inconsistent signed quantity")
        expected_side = "buy" if direction > 0 else "sell"
        side = str(payload.get("side") or "").lower()
        if side != expected_side:
            raise TradeDiagnosticError(f"command {command_id} side disagrees with signed quantity")

        batch_id = command.correlation_id
        symbol = command.symbol
        target_events = sorted(
            targets_by_batch_symbol.get((batch_id, symbol), ()),
            key=lambda event: event.sequence,
        )
        decision_units: set[str] = set()
        strategy_ids: set[str] = set()
        components: set[str] = set()
        sleeves: set[str] = set()
        for target in target_events:
            target_payload = target.payload
            metadata = _event_metadata(target)
            sleeve = str(target_payload.get("sleeve") or target.sleeve or "")
            strategy_id = str(target_payload.get("strategy_id") or "")
            component = str(target_payload.get("component_id") or "")
            signal_ts = _int_or_none(metadata.get("signal_ts_ms"))
            decision_key = str(target_payload.get("decision_key") or "")
            decision_units.add(
                f"{sleeve}|{symbol}|{signal_ts}"
                if signal_ts is not None and signal_ts > 0
                else f"{sleeve}|{decision_key}"
            )
            strategy_ids.add(strategy_id)
            components.add(component)
            sleeves.add(sleeve)

        market_event = market_by_batch_symbol.get((batch_id, symbol))
        market_payload = market_event.payload if market_event is not None else {}
        market_input_key = str(market_payload.get("input_key") or "")
        book = _book_metrics(
            book_contexts.get(market_input_key),
            expected_record_id=market_input_key,
            symbol=symbol,
            direction=direction,
            requested_qty=requested_qty,
        )
        command_created_ts_ns = int(payload.get("created_ts_ns") or command.wall_ts_ns)
        book_local_receive_ts_ns = _int_or_none(book.get("book_local_receive_ts_ns"))
        decision_book_age_ns = _nonnegative_delta(command_created_ts_ns, book_local_receive_ts_ns)

        ack_events = sorted(acks_by_command.get(command_id, ()), key=lambda event: event.sequence)
        if len(ack_events) > 1:
            raise TradeDiagnosticError(f"command {command_id} has multiple semantic ACK events")
        ack = ack_events[0] if ack_events else None
        ack_observations = sorted(
            ack_observations_by_command.get(command_id, ()),
            key=lambda event: event.sequence,
        )
        timing_event = next(
            (
                event
                for event in ack_observations
                if _int_or_none(_event_metadata(event).get("local_socket_send_ts_ns"))
            ),
            ack,
        )
        ack_accepted = bool(ack.payload.get("accepted")) if ack is not None else None
        venue_order_id = str(ack.payload.get("venue_order_id") or "") if ack is not None else ""
        rejection_key = str(ack.payload.get("rejection_key") or "") if ack is not None else ""
        # A private fill can establish the semantic ACK before the HTTP create
        # response returns. Prefer the supplemental create-response observation
        # for request timing while keeping acceptance/order identity on the
        # semantic ACK above.
        local_ack_ts_ns = (
            _int_or_none(timing_event.payload.get("local_ack_ts_ns"))
            if timing_event is not None
            else None
        )
        exchange_ack_ts_ns = (
            _int_or_none(timing_event.payload.get("exchange_ts_ns"))
            if timing_event is not None
            else None
        )
        local_socket_send_ts_ns = _int_or_none(
            _event_metadata(timing_event).get("local_socket_send_ts_ns")
        )

        fills = sorted(
            fills_by_command.get(command_id, ()),
            key=lambda event: (
                int(event.payload.get("exchange_ts_ns") or 0),
                int(event.payload.get("local_receive_ts_ns") or 0),
                event.sequence,
            ),
        )
        fill_quantities: list[float] = []
        fill_prices: list[float] = []
        for fill in fills:
            fill_signed_qty = _finite(fill.payload.get("signed_qty"), label="fill signed quantity")
            if (fill_signed_qty > 0.0) != (direction > 0):
                raise TradeDiagnosticError(f"command {command_id} has a fill in the wrong direction")
            fill_quantities.append(abs(fill_signed_qty))
            fill_prices.append(_positive(fill.payload.get("price"), label="fill price"))
        filled_qty = math.fsum(fill_quantities)
        if filled_qty > requested_qty + 1e-9:
            raise TradeDiagnosticError(f"command {command_id} overfilled its requested quantity")
        filled_notional = math.fsum(qty * price for qty, price in zip(fill_quantities, fill_prices, strict=True))
        fill_vwap = filled_notional / filled_qty if filled_qty > 0.0 else None
        fee_usdt, fee_coverage = _fee_summary(fills)
        maker_fraction, maker_coverage = _maker_summary(fills)

        exchange_fill_times = [int(fill.payload.get("exchange_ts_ns") or 0) for fill in fills]
        local_fill_times = [int(fill.payload.get("local_receive_ts_ns") or 0) for fill in fills]
        first_exchange_fill_ts_ns = min((value for value in exchange_fill_times if value > 0), default=None)
        last_exchange_fill_ts_ns = max((value for value in exchange_fill_times if value > 0), default=None)
        first_local_fill_ts_ns = min((value for value in local_fill_times if value > 0), default=None)
        last_local_fill_ts_ns = max((value for value in local_fill_times if value > 0), default=None)

        midpoint = book.get("decision_mid")
        reference_price = _positive(payload.get("reference_price"), label="command reference price")
        arrival_shortfall = (
            10_000.0 * direction * (fill_vwap - float(midpoint)) / float(midpoint)
            if fill_vwap is not None and midpoint is not None
            else None
        )
        effective_spread = 2.0 * arrival_shortfall if arrival_shortfall is not None else None
        reference_shortfall = (
            10_000.0 * direction * (fill_vwap - reference_price) / reference_price
            if fill_vwap is not None
            else None
        )
        walk_vwap = book.get("book_walk_vwap")
        walk_residual = (
            10_000.0 * direction * (fill_vwap - float(walk_vwap)) / float(midpoint)
            if fill_vwap is not None and walk_vwap is not None and midpoint is not None
            else None
        )
        fee_bps = (
            10_000.0 * fee_usdt / filled_notional
            if fee_usdt is not None and filled_notional > 0.0
            else None
        )
        all_in = (
            arrival_shortfall + fee_bps
            if arrival_shortfall is not None and fee_bps is not None
            else None
        )
        markout_metrics: dict[str, Any] = {}
        markout_horizon_statuses: list[str] = []
        for horizon_ns in DEFAULT_POST_FILL_MARKOUT_HORIZONS_NS:
            horizon_metrics = _markout_horizon_metrics(
                fills,
                schedules=markout_schedules,
                observations=markout_observations,
                horizon_ns=horizon_ns,
            )
            markout_metrics.update(horizon_metrics)
            markout_horizon_statuses.append(
                str(
                    horizon_metrics[
                        f"markout_{MARKOUT_HORIZON_LABELS[horizon_ns]}_status"
                    ]
                )
            )
        if not fills:
            markout_status = "no_fills"
        elif all(value == "complete" for value in markout_horizon_statuses):
            markout_status = "complete"
        elif any(
            value in {"complete", "partial"}
            for value in markout_horizon_statuses
        ):
            markout_status = "partial"
        else:
            markout_status = "incomplete"

        market_exchange_ts_ns = _int_or_none(market_payload.get("exchange_ts_ns"))
        market_local_ts_ns = _int_or_none(market_payload.get("local_receive_ts_ns"))
        missing_reasons: set[str] = set()
        if book["context_status"] != "healthy":
            missing_reasons.add(f"decision_book:{book['context_status']}")
        if decision_book_age_ns is None:
            missing_reasons.add("decision_book_age:unavailable_or_future")
        if ack is None:
            missing_reasons.add("ack:missing")
        if local_socket_send_ts_ns is None:
            missing_reasons.add("local_socket_send:missing")
        if not fills:
            missing_reasons.add("fills:missing")
        if fee_usdt is None:
            missing_reasons.add(f"fee:{fee_coverage}")
        if maker_fraction is None:
            missing_reasons.add(f"maker:{maker_coverage}")
        for horizon_ns, status in zip(
            DEFAULT_POST_FILL_MARKOUT_HORIZONS_NS,
            markout_horizon_statuses,
            strict=True,
        ):
            if status != "complete":
                missing_reasons.add(
                    f"markout_{MARKOUT_HORIZON_LABELS[horizon_ns]}:{status}"
                )

        source_events = [
            *(event.event_id for event in target_events),
            *((market_event.event_id,) if market_event is not None else ()),
            command.event_id,
            *(event.event_id for event in ack_events),
            *(event.event_id for event in ack_observations),
            *(event.event_id for event in fills),
            *(event.event_id for event in statuses_by_command.get(command_id, ())),
        ]
        status_events = sorted(
            statuses_by_command.get(command_id, ()),
            key=lambda event: event.sequence,
        )

        rows.append(
            {
                "schema_version": TRADE_DIAGNOSTIC_SCHEMA_VERSION,
                "account_id": command.account_id,
                "batch_id": batch_id,
                "wave_key": batch_id,
                "command_id": command_id,
                "symbol": symbol,
                "side": side,
                "action": "risk_reducing" if bool(payload.get("reduce_only")) else "exposure_increasing",
                "command_created_ts_ns": command_created_ts_ns,
                "reduce_only": bool(payload.get("reduce_only")),
                "chunk_index": int(payload.get("chunk_index") or 0),
                "chunk_count": int(payload.get("chunk_count") or 0),
                "requested_qty": requested_qty,
                "reference_price": reference_price,
                "target_signed_qty": _finite(payload.get("target_signed_qty"), label="target signed quantity"),
                "leverage": _positive(payload.get("leverage") or 1.0, label="command leverage"),
                "decision_unit_keys_json": _json_list(decision_units),
                "decision_unit_count": len(decision_units),
                "strategy_ids_json": _json_list(strategy_ids),
                "components_json": _json_list(components),
                "sleeves_json": _json_list(sleeves),
                "contributing_target_count": len(target_events),
                "journal_command_sequence": command.sequence,
                "command_event_hash": command.event_hash,
                "market_input_key": market_input_key,
                "decision_book_age_ns": decision_book_age_ns,
                **book,
                "ack_accepted": ack_accepted,
                "venue_order_id": venue_order_id,
                "rejection_key": rejection_key,
                "terminal_status": _terminal_status(
                    status_events,
                    ack_accepted=ack_accepted,
                    filled_qty=filled_qty,
                    requested_qty=requested_qty,
                ),
                "local_socket_send_ts_ns": local_socket_send_ts_ns,
                "local_ack_ts_ns": local_ack_ts_ns,
                "exchange_ack_ts_ns": exchange_ack_ts_ns,
                "first_exchange_fill_ts_ns": first_exchange_fill_ts_ns,
                "last_exchange_fill_ts_ns": last_exchange_fill_ts_ns,
                "first_local_fill_receive_ts_ns": first_local_fill_ts_ns,
                "last_local_fill_receive_ts_ns": last_local_fill_ts_ns,
                "decision_to_send_ns": _nonnegative_delta(local_socket_send_ts_ns, command_created_ts_ns),
                "local_request_rtt_ns": _nonnegative_delta(local_ack_ts_ns, local_socket_send_ts_ns),
                "send_to_first_local_fill_ns": _nonnegative_delta(
                    first_local_fill_ts_ns, local_socket_send_ts_ns
                ),
                "exchange_ack_to_first_fill_ns": _nonnegative_delta(
                    first_exchange_fill_ts_ns, exchange_ack_ts_ns
                ),
                "exchange_fill_span_ns": _nonnegative_delta(
                    last_exchange_fill_ts_ns, first_exchange_fill_ts_ns
                ),
                "feed_delivery_plus_clock_offset_ns": (
                    market_local_ts_ns - market_exchange_ts_ns
                    if market_local_ts_ns is not None and market_exchange_ts_ns is not None
                    else None
                ),
                "fill_delivery_plus_clock_offset_ns": (
                    first_local_fill_ts_ns - first_exchange_fill_ts_ns
                    if first_local_fill_ts_ns is not None and first_exchange_fill_ts_ns is not None
                    else None
                ),
                "fill_count": len(fills),
                "filled_qty": filled_qty,
                "fill_ratio": min(filled_qty / requested_qty, 1.0),
                "unfilled_qty": max(requested_qty - filled_qty, 0.0),
                "fill_vwap": fill_vwap,
                "fill_price_min": min(fill_prices) if fill_prices else None,
                "fill_price_max": max(fill_prices) if fill_prices else None,
                "filled_notional_usdt": filled_notional if fills else None,
                "fee_usdt": fee_usdt,
                "fee_coverage": fee_coverage,
                "maker_fill_fraction": maker_fraction,
                "maker_coverage": maker_coverage,
                "execution_types_json": _json_list(
                    {str(_event_metadata(fill).get("execution_type") or "") for fill in fills}
                ),
                "fee_currencies_json": _json_list(
                    {str(_event_metadata(fill).get("fee_currency") or "") for fill in fills}
                ),
                "arrival_shortfall_bps": arrival_shortfall,
                "effective_spread_bps": effective_spread,
                "reference_shortfall_bps": reference_shortfall,
                "book_walk_residual_bps": walk_residual,
                "fee_bps": fee_bps,
                "all_in_arrival_bps": all_in,
                **markout_metrics,
                "markout_status": markout_status,
                "missing_reasons_json": _json_list(missing_reasons),
                "source_event_ids_json": _json_list(source_events),
            }
        )

    if not rows:
        return pl.DataFrame(schema=EXECUTION_DIAGNOSTIC_SCHEMA)
    return pl.DataFrame(rows, schema=EXECUTION_DIAGNOSTIC_SCHEMA, strict=True).sort(
        "command_created_ts_ns", "command_id"
    )


def required_book_context_ids(events: Sequence[AccountEvent]) -> set[str]:
    return {
        str(event.payload.get("input_key") or "")
        for event in events
        if event.event_type == AccountEventType.MARKET_INPUT_REF.value
        and str(event.payload.get("input_key") or "")
    }


def _load_context_line(raw: bytes, *, expected_id: str) -> dict[str, Any]:
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise TradeDiagnosticError(f"capture locator for {expected_id!r} is not one complete JSON line")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TradeDiagnosticError(f"capture locator for {expected_id!r} is invalid JSON") from exc
    if not isinstance(record, dict):
        raise TradeDiagnosticError(f"capture locator for {expected_id!r} is not an object")
    if str(record.get("record_id") or "") != expected_id:
        raise TradeDiagnosticError(f"capture locator returned the wrong record for {expected_id!r}")
    if expected_id != capture_record_id(record):
        raise TradeDiagnosticError(f"capture record {expected_id!r} has an invalid identity")
    return record


def _locator_record(
    root: Path,
    *,
    record_id: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any] | None:
    required = {
        "capture_segment_path",
        "capture_byte_offset",
        "capture_byte_length",
        "capture_record_sha256",
    }
    if not required.issubset(metadata):
        return None
    relative = str(metadata["capture_segment_path"])
    logical = PurePosixPath(relative)
    if (
        not relative
        or logical.is_absolute()
        or logical.as_posix() != relative
        or any(part in {"", ".", ".."} for part in logical.parts)
    ):
        raise TradeDiagnosticError(f"capture locator for {record_id!r} has an invalid path")
    path = (root / logical).resolve()
    if not path.is_relative_to(root):
        raise TradeDiagnosticError(f"capture locator for {record_id!r} escapes its root")
    offset = int(metadata["capture_byte_offset"])
    length = int(metadata["capture_byte_length"])
    expected_sha = str(metadata["capture_record_sha256"])
    if offset < 0 or length <= 0 or len(expected_sha) != 64:
        raise TradeDiagnosticError(f"capture locator for {record_id!r} has invalid bounds/hash")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(descriptor)
        raw = os.pread(descriptor, length, offset)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise TradeDiagnosticError(f"capture segment changed during read for {record_id!r}")
    if len(raw) != length or after.st_size < offset + length:
        raise TradeDiagnosticError(f"capture locator for {record_id!r} is truncated")
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise TradeDiagnosticError(f"capture locator hash mismatch for {record_id!r}")
    return _load_context_line(raw, expected_id=record_id)


def _read_stable_capture_segment(path: Path) -> bytes:
    try:
        return read_stable_file(
            path,
            label="trade-diagnostic capture segment",
            require_single_link=False,
        ).data
    except (OSError, RuntimeError, ValueError) as exc:
        raise TradeDiagnosticError(
            f"capture segment cannot be read as stable evidence: {path}"
        ) from exc


def load_required_book_contexts(
    capture_root: str | Path,
    events: Sequence[AccountEvent],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load required contexts by exact locator, with legacy scan fallback."""

    root = Path(capture_root).expanduser().resolve()
    required = required_book_context_ids(events)
    output: dict[str, dict[str, Any]] = {}
    direct = 0
    market_events = [
        event for event in events if event.event_type == AccountEventType.MARKET_INPUT_REF.value
    ]
    for event in market_events:
        record_id = str(event.payload.get("input_key") or "")
        if not record_id or record_id in output:
            continue
        metadata = event.payload.get("metadata")
        locator = _locator_record(
            root,
            record_id=record_id,
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )
        if locator is not None:
            output[record_id] = locator
            direct += 1

    missing = required - set(output)
    scanned_files = 0
    if missing and root.is_dir():
        for path in sorted(root.rglob("segment-*.jsonl")):
            scanned_files += 1
            data = _read_stable_capture_segment(path)
            if data and not data.endswith(b"\n"):
                raise TradeDiagnosticError(f"capture segment has a partial line: {path}")
            for raw in data.splitlines(keepends=True):
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise TradeDiagnosticError(f"capture segment has invalid JSON: {path}") from exc
                if not isinstance(record, dict):
                    raise TradeDiagnosticError(f"capture segment row is not an object: {path}")
                record_id = str(record.get("record_id") or "")
                if record_id not in missing:
                    continue
                loaded = _load_context_line(raw, expected_id=record_id)
                prior = output.get(record_id)
                if prior is not None and prior != loaded:
                    raise TradeDiagnosticError(f"capture record identity collision for {record_id!r}")
                output[record_id] = loaded
                missing.remove(record_id)
            if not missing:
                break

    return output, {
        "required_contexts": len(required),
        "direct_locator_contexts": direct,
        "legacy_scan_files": scanned_files,
        "missing_context_ids": sorted(missing),
    }


def load_post_fill_markouts(
    capture_root: str | Path,
    events: Sequence[AccountEvent],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, int], dict[str, Any]],
    dict[str, Any],
]:
    """Load bounded markout schedules/observations for canonical executions."""

    execution_ids = {
        str(event.payload.get("execution_id") or "")
        for event in events
        if event.event_type == AccountEventType.FILL.value
        and str(event.payload.get("execution_id") or "")
    }
    root = Path(capture_root).expanduser().resolve()
    schedules: dict[str, dict[str, Any]] = {}
    observations: dict[tuple[str, int], dict[str, Any]] = {}
    scanned_files = 0
    scanned_bytes = 0
    if root.is_dir() and execution_ids:
        for path in sorted(root.rglob("segment-*.jsonl")):
            scanned_files += 1
            data = _read_stable_capture_segment(path)
            scanned_bytes += len(data)
            if data and not data.endswith(b"\n"):
                raise TradeDiagnosticError(f"capture segment has a partial line: {path}")
            for raw in data.splitlines():
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise TradeDiagnosticError(
                        f"capture segment has invalid JSON: {path}"
                    ) from exc
                if not isinstance(record, dict):
                    raise TradeDiagnosticError(
                        f"capture segment row is not an object: {path}"
                    )
                kind = str(record.get("kind") or "")
                if kind not in {
                    "post_fill_markout_schedule",
                    "post_fill_markout",
                }:
                    continue
                execution_id = str(record.get("execution_id") or "")
                if execution_id not in execution_ids:
                    continue
                record_id = str(record.get("record_id") or "")
                if not record_id or record_id != capture_record_id(record):
                    raise TradeDiagnosticError(
                        f"post-fill capture record has invalid identity: {record_id!r}"
                    )
                if kind == "post_fill_markout_schedule":
                    prior = schedules.get(execution_id)
                    if prior is not None and prior != record:
                        raise TradeDiagnosticError(
                            f"execution {execution_id!r} has conflicting markout schedules"
                        )
                    schedules[execution_id] = record
                    continue
                horizon_ns = _int_or_none(record.get("target_horizon_ns"))
                if horizon_ns is None or horizon_ns <= 0:
                    raise TradeDiagnosticError(
                        f"execution {execution_id!r} has an invalid markout horizon"
                    )
                key = (execution_id, horizon_ns)
                prior = observations.get(key)
                if prior is not None and prior != record:
                    raise TradeDiagnosticError(
                        f"execution {execution_id!r} has conflicting {horizon_ns}ns markouts"
                    )
                observations[key] = record
    accepted_schedules = {
        execution_id
        for execution_id, record in schedules.items()
        if str(record.get("schedule_status") or "") == "accepted"
    }
    return schedules, observations, {
        "required_executions": len(execution_ids),
        "scheduled_executions": len(schedules),
        "accepted_schedules": len(accepted_schedules),
        "rejected_schedules": len(schedules) - len(accepted_schedules),
        "observation_records": len(observations),
        "missing_schedule_execution_ids": sorted(execution_ids - set(schedules)),
        "required_horizons_ns": list(DEFAULT_POST_FILL_MARKOUT_HORIZONS_NS),
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
    }


def build_trade_diagnostic_manifest(
    *,
    events: Sequence[AccountEvent],
    book_contexts: Mapping[str, Mapping[str, Any]],
    diagnostics: pl.DataFrame,
    code_commit: str,
    account_root: str,
    capture_root: str,
    context_lookup: Mapping[str, Any],
    markout_schedules: Mapping[str, Mapping[str, Any]] | None = None,
    markout_observations: Mapping[tuple[str, int], Mapping[str, Any]] | None = None,
    markout_lookup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic reconstruction manifest for a diagnostic table."""

    state = reduce_account_events(events)
    rows = diagnostics.to_dicts()
    markout_schedules = markout_schedules or {}
    markout_observations = markout_observations or {}
    journal_command_ids = {
        str(event.payload.get("command_id") or "")
        for event in events
        if event.event_type == AccountEventType.ORDER_COMMAND.value
    }
    row_command_ids = [str(row.get("command_id") or "") for row in rows]
    if (
        len(row_command_ids) != len(set(row_command_ids))
        or set(row_command_ids) != journal_command_ids
    ):
        raise TradeDiagnosticError(
            "diagnostic command identities do not exactly match the journal"
        )
    decision_units: set[str] = set()
    for row in rows:
        encoded = row.get("decision_unit_keys_json")
        try:
            values = json.loads(str(encoded or "[]"))
        except json.JSONDecodeError as exc:
            raise TradeDiagnosticError("diagnostic decision-unit keys are invalid JSON") from exc
        if not isinstance(values, list) or any(type(value) is not str for value in values):
            raise TradeDiagnosticError("diagnostic decision-unit keys are not a string list")
        decision_units.update(values)
    used_ids = sorted(
        {
            str(row.get("market_input_key") or "")
            for row in rows
            if str(row.get("market_input_key") or "")
        }
    )
    contexts = [book_contexts[record_id] for record_id in used_ids if record_id in book_contexts]
    markout_evidence = {
        "schedules": [
            markout_schedules[key] for key in sorted(markout_schedules)
        ],
        "observations": [
            markout_observations[key] for key in sorted(markout_observations)
        ],
    }
    null_counts = diagnostics.null_count().to_dicts()[0] if diagnostics.width else {}
    manifest: dict[str, Any] = {
        "kind": "trade_diagnostic_manifest",
        "schema_version": TRADE_DIAGNOSTIC_SCHEMA_VERSION,
        "code_commit": code_commit,
        "source": {
            "account_root": account_root,
            "capture_root": capture_root,
            "account_ids": sorted({event.account_id for event in events}),
            "journal_events": len(events),
            "journal_last_sequence": events[-1].sequence if events else 0,
            "journal_last_event_hash": events[-1].event_hash if events else "0" * 64,
            "journal_final_state_hash": state.state_hash(),
            "used_book_contexts": len(contexts),
            "book_contexts_sha256": hashlib.sha256(
                canonical_json({"contexts": contexts})
            ).hexdigest(),
            "context_lookup": dict(context_lookup),
            "markout_schedules": len(markout_schedules),
            "markout_observations": len(markout_observations),
            "markout_evidence_sha256": hashlib.sha256(
                canonical_json(markout_evidence)
            ).hexdigest(),
            "markout_lookup": dict(markout_lookup or {}),
        },
        "output": {
            "rows": diagnostics.height,
            "columns": {name: str(dtype) for name, dtype in diagnostics.schema.items()},
            "null_counts": null_counts,
            "rows_sha256": hashlib.sha256(canonical_json({"rows": rows})).hexdigest(),
            "grain_counts": {
                "commands": diagnostics.height,
                "executions": (
                    int(diagnostics["fill_count"].sum()) if diagnostics.height else 0
                ),
                "unique_decisions": len(decision_units),
                "waves": len({str(row.get("wave_key") or "") for row in rows}),
            },
        },
        "study_mode": "diagnostic_only",
        "explicit_non_conclusions": [
            "no alpha conclusion",
            "no paper calibration conclusion",
            "no strategy promotion or sizing conclusion",
            "no deployment or real-money authorization",
            "post-fill conclusions require the reported horizon-specific coverage",
        ],
    }
    manifest["manifest_payload_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest
