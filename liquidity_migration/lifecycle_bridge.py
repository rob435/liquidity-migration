"""Migration bridge from existing trade/order rows to the canonical journal.

The legacy row schemas remain useful compatibility APIs, but writes pass through
this module.  It emits the missing canonical lifecycle facts, appends an
immutable projection patch, then regenerates the Parquet ledger from journal
replay.  Historical, paper and demo rows all use the same event builder and
reducer; only the explicit ``mode`` differs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping, TypedDict

import polars as pl

from .canonical_journal import (
    EventSpec,
    EventType,
    LIFECYCLE_INDEX,
    LIFECYCLE_SEQUENCE,
    append_events,
    read_journal,
    rebuild_ledger_projections,
    replay_journal,
    record_unavailable_markouts,
    write_tca_projection,
)
from .storage import read_dataset


class _Identity(TypedDict):
    mode: str
    sleeve: str
    strategy_id: str
    trade_id: str
    symbol: str
    side: str


def _rows(value: pl.DataFrame | Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, pl.DataFrame):
        return value.to_dicts() if not value.is_empty() else []
    return [dict(row) for row in value]


def _text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _int(row: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return int(default)


def _float(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            output = float(value)
        except (TypeError, ValueError):
            continue
        if output == output and abs(output) != float("inf"):
            return output
    return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _row_hash(row: Mapping[str, Any]) -> str:
    from .canonical_journal import _canonical_json, _json_safe

    return hashlib.sha256(_canonical_json(_json_safe(dict(row)))).hexdigest()


def infer_mode(*, dataset: str = "", submit_orders: bool | None = None, explicit: str = "") -> str:
    if explicit:
        return explicit
    lowered = dataset.lower()
    if "paper" in lowered:
        return "paper"
    if "shadow" in lowered:
        return "shadow"
    if submit_orders is False:
        return "paper"
    return "demo"


def infer_sleeve(*, dataset: str = "", explicit: str = "") -> str:
    if explicit:
        return explicit
    lowered = dataset.lower()
    if "continuous" in lowered:
        return "continuous"
    if "long" in lowered:
        return "long"
    if "event_demo" in lowered:
        return "event"
    return "unknown"


def _identity(
    row: Mapping[str, Any],
    *,
    mode: str,
    sleeve: str,
    fallback_trade_id: str = "",
) -> _Identity:
    trade_id = _text(row, "trade_id") or fallback_trade_id
    if not trade_id:
        order_link = _text(row, "order_link_id", "orderLinkId")
        trade_id = f"order-{order_link}" if order_link else ""
    if not trade_id:
        raise ValueError("canonical lifecycle row requires trade_id or order_link_id")
    explicit_trade_side = _text(row, "trade_side", "position_side").lower()
    side = explicit_trade_side or _text(row, "side").lower()
    if side == "buy":
        # A reduce-only Buy closes a short. Entry Buy opens a long.
        side = "short" if _bool(row.get("reduce_only")) and not explicit_trade_side else "long"
    elif side == "sell":
        # A reduce-only Sell closes a long. Entry Sell opens a short.
        side = "long" if _bool(row.get("reduce_only")) and not explicit_trade_side else "short"
    return {
        "mode": mode,
        "sleeve": sleeve,
        "strategy_id": _text(row, "strategy_id") or f"{sleeve}_{mode}",
        "trade_id": trade_id,
        "symbol": _text(row, "symbol"),
        "side": side,
    }


def _trade_target(row: Mapping[str, Any]) -> EventType | None:
    status = _text(row, "status").lower()
    if status in {"failed", "rejected", "cancelled", "canceled", "expired"}:
        return EventType.SUBMITTED if _text(row, "order_link_id", "entry_order_link_id") else EventType.DECISION
    if status in {"submitted", "submitted_unconfirmed", "pending", "planned"}:
        return EventType.SUBMITTED
    if status == "open":
        return EventType.PROTECTION_ACTIVE
    if status == "closed":
        return EventType.PNL_CONFIRMED if _pnl_known(row) else EventType.CLOSE_FILL
    return EventType.DECISION if status or _text(row, "trade_id") else None


def _order_target(row: Mapping[str, Any]) -> EventType | None:
    status = _text(row, "status", "orderStatus", "order_status").lower()
    submit_mode = _text(row, "submit_mode").lower()
    reduce_only = _bool(row.get("reduce_only"))
    rejected = submit_mode == "error" or status in {"failed", "rejected", "cancelled", "canceled", "expired"}
    if reduce_only:
        if status in {"filled", "closed", "partial", "partiallyfilled", "partially_filled"}:
            return EventType.CLOSE_FILL
        return EventType.EXIT_REQUESTED
    if status in {"filled", "closed", "partial", "partiallyfilled", "partially_filled"}:
        return EventType.FILL
    if submit_mode == "preflight" or status in {"planned", "preflight"}:
        return EventType.RISK_ACCEPTED
    if rejected:
        return EventType.SUBMITTED if _text(row, "order_link_id", "orderLinkId") else EventType.DECISION
    if submit_mode == "submitted" or status in {"new", "submitted", "submitted_unconfirmed"}:
        # A successful place-order response is the venue acknowledgement.
        return EventType.ACKNOWLEDGED if _text(row, "order_id", "orderId", "venue_order_id") else EventType.SUBMITTED
    return EventType.DECISION if _text(row, "order_link_id", "orderLinkId") else None


def _pnl_known(row: Mapping[str, Any]) -> bool:
    return any(
        row.get(key) is not None and row.get(key) != ""
        for key in ("realized_pnl_usdt", "closed_pnl", "net_pnl_usdt", "net_return", "gross_return")
    )


def _stage_timestamp(row: Mapping[str, Any], event_type: EventType, fallback: int) -> tuple[int, int]:
    if event_type is EventType.DECISION:
        local = _int(row, "decision_ts_ms", "signal_ts_ms", "entry_signal_ts_ms", "ts_ms", default=fallback)
    elif event_type is EventType.RISK_ACCEPTED:
        local = _int(row, "risk_accepted_ts_ms", "decision_ts_ms", "signal_ts_ms", "ts_ms", default=fallback)
    elif event_type is EventType.SUBMITTED:
        local = _int(row, "submitted_at_ms", "order_submit_ts_ms", "ts_ms", default=fallback)
    elif event_type is EventType.ACKNOWLEDGED:
        local = _int(row, "acknowledged_at_ms", "updated_at_ms", "ts_ms", default=fallback)
    elif event_type is EventType.FILL:
        local = _int(row, "entry_ts_ms", "opened_at_ms", "exec_time_ms", "updated_at_ms", "ts_ms", default=fallback)
    elif event_type is EventType.PROTECTION_ACTIVE:
        local = _int(row, "protection_active_ts_ms", "entry_stop_update_ts_ms", "opened_at_ms", "entry_ts_ms", default=fallback)
    elif event_type is EventType.EXIT_REQUESTED:
        local = _int(row, "exit_signal_ts_ms", "exit_requested_ts_ms", "exit_ts_ms", "updated_at_ms", default=fallback)
    elif event_type is EventType.CLOSE_FILL:
        local = _int(row, "exit_ts_ms", "closed_at_ms", "exec_time_ms", "updated_at_ms", default=fallback)
    else:
        local = _int(row, "pnl_confirmed_ts_ms", "updated_at_ms", "exit_ts_ms", "closed_at_ms", default=fallback)
    venue = _int(
        row,
        "venue_ts_ms",
        "exec_time_ms",
        "execTime",
        "venue_fill_ts_ms",
        default=local,
    )
    return max(local, 1), max(venue, 0)


def _prices(row: Mapping[str, Any], event_type: EventType) -> tuple[float | None, float | None, float | None]:
    decision = _float(
        row, "decision_price", "signal_price", "entry_reference_price", "entry_price", "price", "avg_price"
    )
    submission = _float(
        row, "submission_price", "submitted_price", "order_price", "price", "entry_price", "avg_price"
    )
    if event_type is EventType.CLOSE_FILL:
        fill = _float(row, "exit_price", "avg_fill_price", "avg_price", "price")
    else:
        fill = _float(row, "entry_price", "avg_fill_price", "avg_price", "price")
    return decision, submission, fill


def _price_source(row: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if _float(row, key) is not None:
            return key
    return ""


def _qty(row: Mapping[str, Any], event_type: EventType) -> float | None:
    if event_type is EventType.CLOSE_FILL:
        return _float(row, "exit_filled_qty", "filled_qty", "exec_qty", "qty")
    return _float(row, "entry_filled_qty", "filled_qty", "exec_qty", "qty")


def _realized_pnl(row: Mapping[str, Any]) -> float | None:
    return _float(row, "realized_pnl_usdt", "closed_pnl", "net_pnl_usdt")


def _projection_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Remove journal-only transport fields from compatibility projections."""
    return {
        key: value
        for key, value in row.items()
        if key not in {"canonical_fill_details"}
    }


def _fill_details(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("canonical_fill_details")
    if not isinstance(raw, (list, tuple)):
        return []
    details: list[dict[str, Any]] = []
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            continue
        qty = _float(value, "qty", "exec_qty", "execQty")
        price = _float(value, "price", "exec_price", "execPrice")
        if qty is None or qty <= 0.0 or price is None or price <= 0.0:
            continue
        venue_ts_ms = _int(value, "venue_ts_ms", "exec_time_ms", "execTime", default=0)
        exec_id = _text(value, "exec_id", "execId")
        if not exec_id:
            # Some paper/test clients omit Bybit's execId. The fallback is
            # deterministic for identical venue facts and never uses list order
            # alone as identity.
            exec_id = hashlib.sha256(
                f"{qty}:{price}:{venue_ts_ms}:{_float(value, 'fee')}:{index}".encode("utf-8")
            ).hexdigest()[:24]
        details.append({
            "exec_id": exec_id,
            "qty": qty,
            "price": price,
            "value": _float(value, "value", "exec_value", "execValue") or abs(qty * price),
            "fee": _float(value, "fee", "exec_fee", "execFee") or 0.0,
            "venue_ts_ms": venue_ts_ms,
        })
    return details


def _individual_fill_spec(
    *,
    row: Mapping[str, Any],
    identity: _Identity,
    event_type: EventType,
    detail: Mapping[str, Any],
    order_version: int,
    position_version: int,
    fallback_ts_ms: int,
    order_link_id: str,
    venue_order_id: str,
    trade_dataset: str,
    order_dataset: str,
) -> EventSpec:
    decision_price, submission_price, _ = _prices(row, event_type)
    fill_price = _float(detail, "price")
    qty = _float(detail, "qty")
    venue_ts_ms = _int(detail, "venue_ts_ms", default=0)
    local_ts_ms = max(_int(row, "updated_at_ms", "ts_ms", default=fallback_ts_ms), 1)
    submit_ts_ms = _int(row, "submitted_at_ms", "order_submit_ts_ms", "ts_ms", default=local_ts_ms)
    exec_id = _text(detail, "exec_id")
    return EventSpec(
        event_type=event_type,
        **identity,
        local_ts_ms=local_ts_ms,
        venue_ts_ms=max(venue_ts_ms, 0),
        order_version=order_version,
        position_version=position_version,
        order_link_id=order_link_id,
        venue_order_id=venue_order_id,
        qty=qty,
        price=fill_price,
        decision_price=decision_price,
        submission_price=submission_price,
        fill_price=fill_price,
        depth_consumed_quote=_float(detail, "value") or abs((qty or 0.0) * (fill_price or 0.0)),
        latency_ms=float(max(venue_ts_ms - submit_ts_ms, 0)) if venue_ts_ms else None,
        idempotency_key=(
            f"venue-fill:{identity['mode']}:{identity['sleeve']}:"
            f"{identity['trade_id']}:{event_type.value}:{exec_id}"
        ),
        metadata={
            "source": "venue_execution",
            "exec_id": exec_id,
            "fill_fee_usdt": _float(detail, "fee") or 0.0,
            "decision_price_source": _price_source(
                row,
                ("decision_price", "signal_price", "entry_reference_price", "entry_price", "price", "avg_price"),
            ),
            "submission_price_source": _price_source(
                row,
                ("submission_price", "submitted_price", "order_price", "price", "entry_price", "avg_price"),
            ),
            "depth_source": "executed_quote_notional",
            "latency_source": "order_submit_to_venue_execution",
        },
        trade_dataset=trade_dataset,
        order_dataset=order_dataset,
    )


def _missing_lifecycle_specs(
    *,
    row: Mapping[str, Any],
    identity: _Identity,
    target: EventType,
    current_index: int,
    order_version: int,
    position_version: int,
    fallback_ts_ms: int,
    order_link_id: str,
    venue_order_id: str,
    trade_dataset: str,
    order_dataset: str,
) -> tuple[list[EventSpec], int, int, int]:
    specs: list[EventSpec] = []
    submit_ts = _int(row, "submitted_at_ms", "order_submit_ts_ms", "ts_ms", default=fallback_ts_ms)
    target_index = LIFECYCLE_INDEX[target]
    for index in range(current_index + 1, target_index + 1):
        event_type = LIFECYCLE_SEQUENCE[index]
        if event_type in {EventType.SUBMITTED, EventType.EXIT_REQUESTED}:
            order_version += 1
        if event_type in {EventType.FILL, EventType.PROTECTION_ACTIVE, EventType.CLOSE_FILL}:
            position_version += 1
        local_ts, venue_ts = _stage_timestamp(row, event_type, fallback_ts_ms)
        decision_price, submission_price, fill_price = _prices(row, event_type)
        qty = _qty(row, event_type)
        latency = None
        if event_type in {EventType.FILL, EventType.CLOSE_FILL}:
            latency = float(max(venue_ts - submit_ts, 0))
        depth = abs(qty * fill_price) if qty is not None and fill_price is not None else None
        specs.append(
            EventSpec(
                event_type=event_type,
                **identity,
                local_ts_ms=local_ts,
                venue_ts_ms=venue_ts,
                order_version=order_version,
                position_version=position_version,
                order_link_id=order_link_id,
                venue_order_id=venue_order_id,
                qty=qty if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
                price=fill_price if event_type in {EventType.FILL, EventType.CLOSE_FILL} else submission_price,
                decision_price=decision_price,
                submission_price=submission_price,
                fill_price=fill_price if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
                depth_consumed_quote=depth if event_type in {EventType.FILL, EventType.CLOSE_FILL} else None,
                latency_ms=latency,
                realized_pnl=_realized_pnl(row) if event_type is EventType.PNL_CONFIRMED else None,
                idempotency_key=(
                    f"lifecycle:{identity['mode']}:{identity['sleeve']}:"
                    f"{identity['trade_id']}:{event_type.value}:{order_version}:{position_version}"
                ),
                metadata={
                    "source": "legacy_projection_bridge",
                    "decision_price_source": _price_source(
                        row,
                        ("decision_price", "signal_price", "entry_reference_price", "entry_price", "price", "avg_price"),
                    ),
                    "submission_price_source": _price_source(
                        row,
                        ("submission_price", "submitted_price", "order_price", "price", "entry_price", "avg_price"),
                    ),
                    "depth_source": (
                        "executed_quote_notional" if event_type in {EventType.FILL, EventType.CLOSE_FILL} else ""
                    ),
                    "latency_source": (
                        "order_submit_to_venue_execution" if event_type in {EventType.FILL, EventType.CLOSE_FILL} else ""
                    ),
                    "protection_kind": (
                        "stop_or_take_profit_or_time_exit" if event_type is EventType.PROTECTION_ACTIVE else ""
                    ),
                },
                trade_dataset=trade_dataset,
                order_dataset=order_dataset,
            )
        )
        current_index = index
    return specs, current_index, order_version, position_version


def record_ledger_rows(
    root: str | Path,
    *,
    trade_rows: pl.DataFrame | Iterable[Mapping[str, Any]] | None = None,
    order_rows: pl.DataFrame | Iterable[Mapping[str, Any]] | None = None,
    trade_dataset: str = "",
    order_dataset: str = "",
    mode: str = "",
    sleeve: str = "",
    now_ms: int,
    rebuild: bool = True,
    bootstrap_existing: bool = True,
) -> dict[str, Any]:
    """Append row-derived facts and optionally rebuild compatibility ledgers."""
    resolved_mode = infer_mode(dataset=trade_dataset or order_dataset, explicit=mode)
    resolved_sleeve = infer_sleeve(dataset=trade_dataset or order_dataset, explicit=sleeve)
    if bootstrap_existing:
        bootstrap_legacy_ledgers(
            root,
            trade_dataset=trade_dataset,
            order_dataset=order_dataset,
            mode=resolved_mode,
            sleeve=resolved_sleeve,
            now_ms=now_ms,
        )
    projection = replay_journal(root)
    specs: list[EventSpec] = []

    trade_input_rows = _rows(trade_rows)
    order_input_rows = _rows(order_rows)
    items: list[tuple[str, dict[str, Any]]] = []
    items.extend(("order", row) for row in order_input_rows)
    items.extend(("trade", row) for row in trade_input_rows)
    items.sort(
        key=lambda item: (
            _int(item[1], "ts_ms", "signal_ts_ms", "entry_ts_ms", "exit_ts_ms", default=now_ms),
            # Venue order/fill facts must precede the final trade projection so
            # multi-fill executions remain individual lifecycle events.
            0 if item[0] == "order" else 1,
            _text(item[1], "trade_id", "order_link_id", "orderLinkId"),
        )
    )

    # Track the predicted state through the batch. append_events performs the
    # authoritative reducer validation before it writes a byte.
    predicted: dict[str, tuple[int, int, int]] = {
        trade_id: (state.lifecycle_index, state.order_version, state.position_version)
        for trade_id, state in projection.trades.items()
    }
    identity_by_trade: dict[str, _Identity] = {
        trade_id: {
            "mode": state.mode,
            "sleeve": state.sleeve,
            "strategy_id": state.strategy_id,
            "trade_id": trade_id,
            "symbol": state.symbol,
            "side": state.side,
        }
        for trade_id, state in projection.trades.items()
    }
    # Seed order-row identity from the accompanying trade rows without changing
    # processing order. This preserves position side/strategy even when the
    # venue action is the opposing reduce-only side.
    for row in trade_input_rows:
        identity = _identity(
            row,
            mode=resolved_mode,
            sleeve=resolved_sleeve,
            fallback_trade_id=_text(row, "trade_id"),
        )
        identity_by_trade.setdefault(identity["trade_id"], identity)

    journal_events = read_journal(root, verify=True)
    known_execution_ids: set[tuple[str, str, str]] = {
        (
            event.trade_id,
            event.event_type,
            str(event.metadata.get("exec_id") or ""),
        )
        for event in journal_events
        if event.event_type in {EventType.FILL.value, EventType.CLOSE_FILL.value}
        and str(event.metadata.get("exec_id") or "")
    }
    cumulative_filled: dict[tuple[str, str], float] = {
        (trade_id, order_link): _float(row, "filled_qty", "cum_exec_qty", "cumExecQty") or 0.0
        for trade_id, state in projection.trades.items()
        for order_link, row in state.order_rows.items()
    }
    for kind, row in items:
        fallback_trade_id = _text(row, "trade_id")
        identity = _identity(
            row,
            mode=resolved_mode,
            sleeve=resolved_sleeve,
            fallback_trade_id=fallback_trade_id,
        )
        trade_id = identity["trade_id"]
        prior_identity = identity_by_trade.get(trade_id)
        if prior_identity is not None:
            # Order rows frequently omit strategy_id/trade_side. Once a trade
            # identity exists, the journal identity is authoritative; venue
            # action side (e.g. Buy to close a short) must not redefine it.
            identity = prior_identity.copy()
        else:
            identity_by_trade[trade_id] = identity.copy()
        target = _order_target(row) if kind == "order" else _trade_target(row)
        current_index, order_version, position_version = predicted.get(trade_id, (-1, 0, 0))
        order_link = _text(row, "order_link_id", "orderLinkId", "entry_order_link_id", "exit_order_link_id")
        venue_order = _text(row, "venue_order_id", "order_id", "orderId")
        details = _fill_details(row) if kind == "order" else []
        fill_target = target if target in {EventType.FILL, EventType.CLOSE_FILL} else None
        new_details = [
            detail
            for detail in details
            if (trade_id, str(fill_target.value if fill_target else ""), str(detail["exec_id"]))
            not in known_execution_ids
        ]
        if fill_target is not None and details:
            prerequisite = (
                EventType.ACKNOWLEDGED if fill_target is EventType.FILL else EventType.EXIT_REQUESTED
            )
            if LIFECYCLE_INDEX[prerequisite] > current_index:
                new_specs, current_index, order_version, position_version = _missing_lifecycle_specs(
                    row=row,
                    identity=identity,
                    target=prerequisite,
                    current_index=current_index,
                    order_version=order_version,
                    position_version=position_version,
                    fallback_ts_ms=max(int(now_ms), 1),
                    order_link_id=order_link,
                    venue_order_id=venue_order,
                    trade_dataset=trade_dataset,
                    order_dataset=order_dataset,
                )
                specs.extend(new_specs)
            for detail in new_details:
                if current_index not in {LIFECYCLE_INDEX[fill_target] - 1, LIFECYCLE_INDEX[fill_target]}:
                    raise ValueError(
                        f"venue execution for {trade_id} arrived after lifecycle "
                        f"{LIFECYCLE_SEQUENCE[current_index].value if current_index >= 0 else 'NONE'}"
                    )
                position_version += 1
                specs.append(
                    _individual_fill_spec(
                        row=row,
                        identity=identity,
                        event_type=fill_target,
                        detail=detail,
                        order_version=order_version,
                        position_version=position_version,
                        fallback_ts_ms=max(int(now_ms), 1),
                        order_link_id=order_link,
                        venue_order_id=venue_order,
                        trade_dataset=trade_dataset,
                        order_dataset=order_dataset,
                    )
                )
                current_index = LIFECYCLE_INDEX[fill_target]
                known_execution_ids.add((trade_id, fill_target.value, str(detail["exec_id"])))
        elif target is not None and LIFECYCLE_INDEX[target] > current_index:
            new_specs, current_index, order_version, position_version = _missing_lifecycle_specs(
                row=row,
                identity=identity,
                target=target,
                current_index=current_index,
                order_version=order_version,
                position_version=position_version,
                fallback_ts_ms=max(int(now_ms), 1),
                order_link_id=order_link,
                venue_order_id=venue_order,
                trade_dataset=trade_dataset,
                order_dataset=order_dataset,
            )
            specs.extend(new_specs)
        elif (
            kind == "order"
            and fill_target is not None
            and LIFECYCLE_INDEX[fill_target] == current_index
            and order_link
        ):
            # WS/REST reconciliation rows often carry cumulative fill quantity.
            # Journal only the positive delta, so repeated snapshots are
            # idempotent and partial fills advance the position version.
            cumulative = _float(row, "filled_qty", "cum_exec_qty", "cumExecQty") or 0.0
            prior = cumulative_filled.get((trade_id, order_link), 0.0)
            delta = max(cumulative - prior, 0.0)
            if delta > 0.0:
                position_version += 1
                fill_price = _prices(row, fill_target)[2] or _float(row, "avg_price", "price") or 0.0
                detail = {
                    "exec_id": f"cumulative:{order_link}:{cumulative}",
                    "qty": delta,
                    "price": fill_price,
                    "value": abs(delta * fill_price),
                    "fee": 0.0,
                    "venue_ts_ms": _int(row, "exec_time_ms", "venue_ts_ms", default=0),
                }
                if fill_price > 0.0:
                    specs.append(
                        _individual_fill_spec(
                            row=row,
                            identity=identity,
                            event_type=fill_target,
                            detail=detail,
                            order_version=order_version,
                            position_version=position_version,
                            fallback_ts_ms=max(int(now_ms), 1),
                            order_link_id=order_link,
                            venue_order_id=venue_order,
                            trade_dataset=trade_dataset,
                            order_dataset=order_dataset,
                        )
                    )
                else:
                    position_version -= 1

        # A rejected order is an immutable fact but does not pretend the order
        # progressed to a fill. It leaves the lifecycle at decision/submitted or
        # exit_requested for an explicit later retry/reconciliation event.
        status = _text(row, "status", "orderStatus", "order_status").lower()
        if kind == "order" and (
            _text(row, "submit_mode").lower() == "error"
            or status in {"failed", "rejected", "cancelled", "canceled", "expired"}
        ):
            specs.append(
                EventSpec(
                    event_type=EventType.ORDER_REJECTED,
                    **identity,
                    local_ts_ms=max(_int(row, "updated_at_ms", "ts_ms", default=now_ms), 1),
                    venue_ts_ms=max(_int(row, "venue_ts_ms", "updated_at_ms", "ts_ms", default=now_ms), 0),
                    order_version=order_version,
                    position_version=position_version,
                    order_link_id=order_link,
                    venue_order_id=venue_order,
                    idempotency_key=f"order_rejected:{trade_id}:{order_link}:{_row_hash(row)}",
                    metadata={"error": _text(row, "error", "order_error"), "status": status},
                    trade_dataset=trade_dataset,
                    order_dataset=order_dataset,
                )
            )

        projected_row = _projection_row(row)
        row_hash = _row_hash(projected_row)
        specs.append(
            EventSpec(
                event_type=EventType.PROJECTION_PATCH,
                **identity,
                local_ts_ms=max(_int(row, "updated_at_ms", "ts_ms", default=now_ms), 1),
                venue_ts_ms=max(_int(row, "venue_ts_ms", "exec_time_ms", "ts_ms", default=now_ms), 0),
                order_version=order_version,
                position_version=position_version,
                order_link_id=order_link,
                venue_order_id=venue_order,
                idempotency_key=f"projection:{kind}:{trade_id}:{order_link}:{row_hash}",
                metadata={"row_hash": row_hash, "row_kind": kind},
                trade_patch=projected_row if kind == "trade" else {},
                order_patch=projected_row if kind == "order" else {},
                trade_dataset=trade_dataset,
                order_dataset=order_dataset,
            )
        )
        if kind == "order" and order_link:
            cumulative_filled[(trade_id, order_link)] = max(
                cumulative_filled.get((trade_id, order_link), 0.0),
                _float(row, "filled_qty", "cum_exec_qty", "cumExecQty") or 0.0,
            )
        predicted[trade_id] = (current_index, order_version, position_version)

    appended = append_events(root, specs)
    counts: dict[str, int] = {}
    if rebuild:
        counts = rebuild_ledger_projections(
            root,
            trade_datasets=(trade_dataset,) if trade_dataset else (),
            order_datasets=(order_dataset,) if order_dataset else (),
        )
        write_tca_projection(root)
    return {
        "events_appended": len(appended),
        "last_sequence": appended[-1].sequence if appended else replay_journal(root).events_applied,
        "projection_counts": counts,
    }


def bootstrap_legacy_ledgers(
    root: str | Path,
    *,
    trade_dataset: str = "",
    order_dataset: str = "",
    mode: str = "",
    sleeve: str = "",
    now_ms: int,
) -> dict[str, Any]:
    """Import pre-journal ledgers exactly once before their first projection write.

    This closes the migration hazard where deploying the new projector over an
    existing root would otherwise rebuild from only the first *new* row and
    erase older operational state. Dataset registration in immutable events is
    the bootstrap marker; no mutable sidecar flag is trusted.
    """
    projection = replay_journal(root)
    registered_trades = {
        str(state.trade_row.get("_projection_trade_dataset") or "")
        for state in projection.trades.values()
        if state.trade_row
    }
    registered_orders = {
        str(row.get("_projection_order_dataset") or "")
        for state in projection.trades.values()
        for row in state.order_rows.values()
    }
    need_trade = bool(trade_dataset) and trade_dataset not in registered_trades
    need_order = bool(order_dataset) and order_dataset not in registered_orders
    if not need_trade and not need_order:
        return {"bootstrapped": False, "trade_rows": 0, "order_rows": 0}
    existing_trades = read_dataset(root, trade_dataset) if need_trade else pl.DataFrame()
    existing_orders = read_dataset(root, order_dataset) if need_order else pl.DataFrame()
    if existing_trades.is_empty() and existing_orders.is_empty():
        return {"bootstrapped": False, "trade_rows": 0, "order_rows": 0}
    record_ledger_rows(
        root,
        trade_rows=existing_trades,
        order_rows=existing_orders,
        trade_dataset=trade_dataset,
        order_dataset=order_dataset,
        mode=mode,
        sleeve=sleeve,
        now_ms=now_ms,
        rebuild=False,
        bootstrap_existing=False,
    )
    return {
        "bootstrapped": True,
        "trade_rows": existing_trades.height,
        "order_rows": existing_orders.height,
    }


def record_historical_trades(
    root: str | Path,
    trades: pl.DataFrame,
    *,
    sleeve: str,
    strategy_id: str,
    now_ms: int,
    deploy_capital_usd: float | None = None,
    markout_unavailable_reason: str = "historical_source_resolution_exceeds_markout_horizon",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(_rows(trades)):
        enriched = dict(row)
        source_trade_id = _text(row, "trade_id") or f"row-{index}"
        enriched["source_trade_id"] = source_trade_id
        enriched["trade_id"] = f"historical:{strategy_id}:{source_trade_id}"
        enriched.setdefault("strategy_id", strategy_id)
        enriched.setdefault("status", "closed" if _int(row, "exit_ts_ms", default=0) > 0 else "open")
        if enriched.get("qty") in {None, ""} and deploy_capital_usd is not None:
            weight = _float(enriched, "notional_weight", "position_weight")
            entry_price = _float(enriched, "entry_price")
            if weight is not None and entry_price is not None and entry_price > 0.0:
                enriched["qty"] = abs(weight * float(deploy_capital_usd) / entry_price)
        rows.append(enriched)
    result = record_ledger_rows(
        root,
        trade_rows=rows,
        mode="historical",
        sleeve=sleeve,
        now_ms=now_ms,
        rebuild=False,
    )
    unavailable = record_unavailable_markouts(
        root,
        mode="historical",
        reason=markout_unavailable_reason,
        now_ms=now_ms,
    )
    result["events_appended"] = int(result["events_appended"]) + len(unavailable)
    result["markouts_unavailable_recorded"] = len(unavailable)
    write_tca_projection(root)
    return result


__all__ = [
    "bootstrap_legacy_ledgers",
    "infer_mode",
    "infer_sleeve",
    "record_historical_trades",
    "record_ledger_rows",
]
