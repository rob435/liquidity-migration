"""Account-level deterministic execution kernel.

This is the migration target for every strategy sleeve and risk process.  A
strategy proposes component-level :class:`DesiredTarget` values; one serialized
transaction evaluates the complete projected account and emits the net venue
commands.  No execution adapter is allowed to mutate account state directly.

The canonical control-plane order is::

    MarketInputRef -> Decision -> Target -> RiskDecision -> OrderCommand
      -> Ack -> Fill -> Protection -> Close -> P&L

The environment (historical, paper, demo) is intentionally absent from domain
state.  It belongs to an execution adapter, which makes pre-execution hashes
directly comparable across environments.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .deterministic_serialization import canonical_json, json_safe
from .deterministic_runtime import Clock, DeterministicIds, SystemClock
from .storage import exclusive_file_lock


ACCOUNT_SCHEMA_VERSION = 2
ACCOUNT_JOURNAL_DIRECTORY = "account_journal"
ACCOUNT_JOURNAL_FILENAME = "events.jsonl"
ACCOUNT_JOURNAL_LOCK_FILENAME = "journal.lock"
ACCOUNT_TRANSACTIONS_DIRECTORY = "transactions"
GENESIS_HASH = "0" * 64
_EVENT_NAMESPACE = uuid.UUID("16cac165-fc18-4d5b-b0f7-44bdf47bbac9")


class AccountKernelError(RuntimeError):
    """Base class for account-kernel failures."""


class AccountJournalIntegrityError(AccountKernelError):
    """The account journal cannot be verified or reconstructed exactly."""


class AccountTransitionError(AccountKernelError):
    """A control-plane event is illegal for the reconstructed state."""


class AccountEventType(StrEnum):
    MARKET_INPUT_REF = "market_input_ref"
    DECISION = "decision"
    TARGET = "target"
    RISK_DECISION = "risk_decision"
    ORDER_COMMAND = "order_command"
    ACK = "ack"
    # Supplemental transport observation. A private fill can establish the
    # semantic ACK before the HTTP create response returns; retain that later
    # request/response timing without rewriting or duplicating the transition.
    ACK_OBSERVATION = "ack_observation"
    FILL = "fill"
    PROTECTION = "protection"
    CLOSE = "close"
    PNL = "pnl"
    # Supplemental venue fact. Market IOC orders can partially fill then cancel;
    # without a terminal status the unfilled remainder would remain phantom
    # working exposure forever.
    ORDER_STATUS = "order_status"
    VENUE_SNAPSHOT = "venue_snapshot"


@dataclass(frozen=True, slots=True)
class MarketInputRef:
    input_key: str
    symbol: str
    exchange_ts_ns: int
    local_receive_ts_ns: int
    reference_price: float
    bid_price: float | None = None
    ask_price: float | None = None
    book_sequence: int | None = None
    source: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DesiredTarget:
    """A sleeve's desired signed position component.

    ``signed_qty`` is positive for long and negative for short.  ``target_key``
    is the stable ownership key replaced by later decisions, normally
    ``sleeve/strategy/component/symbol``.
    """

    decision_key: str
    target_key: str
    sleeve: str
    strategy_id: str
    component_id: str
    symbol: str
    signed_qty: float
    reference_price: float
    leverage: float
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    symbol: str
    qty_step: float
    min_qty: float
    min_notional: float
    tick_size: float = 0.0
    max_order_qty: float = 0.0
    max_leverage: float = 0.0
    source: str = ""
    environment: str = "shared"
    observed_ts_ns: int = 0


@dataclass(frozen=True, slots=True)
class AccountRiskSnapshot:
    equity_usdt: float
    available_margin_usdt: float
    snapshot_key: str
    snapshot_ts_ns: int


@dataclass(frozen=True, slots=True)
class AccountRiskPolicy:
    """Explicit absolute limits; no hidden resize floor or implicit leverage."""

    max_component_gross_notional_usdt: float
    max_account_gross_notional_usdt: float
    max_symbol_notional_usdt: float
    max_initial_margin_usdt: float
    max_leverage: float
    quantity_tolerance: float = 1e-12


@dataclass(frozen=True, slots=True)
class AccountEventSpec:
    event_type: AccountEventType | str
    idempotency_key: str
    correlation_id: str
    causation_id: str
    account_id: str
    sleeve: str
    symbol: str
    wall_ts_ns: int
    monotonic_ns: int
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AccountEvent:
    schema_version: int
    event_id: str
    sequence: int
    event_type: str
    correlation_id: str
    causation_id: str
    account_id: str
    sleeve: str
    symbol: str
    wall_ts_ns: int
    monotonic_ns: int
    payload: dict[str, Any]
    prev_event_hash: str
    state_hash: str
    event_hash: str

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "AccountEvent":
        fields = set(cls.__dataclass_fields__)
        missing = sorted(fields - set(row))
        if missing:
            raise AccountJournalIntegrityError(f"account event missing fields: {', '.join(missing)}")
        try:
            return cls(**{name: row[name] for name in fields})
        except (TypeError, ValueError) as exc:
            raise AccountJournalIntegrityError(f"invalid account event: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PositionState:
    signed_qty: float = 0.0
    average_price: float = 0.0
    realized_from_fills_usdt: float = 0.0
    fees_from_fills_usdt: float = 0.0
    reported_realized_usdt: float = 0.0
    reported_fees_usdt: float = 0.0


@dataclass(slots=True)
class OrderState:
    command_id: str
    batch_id: str
    symbol: str
    signed_qty: float
    reduce_only: bool
    status: str = "commanded"
    ack_accepted: bool | None = None
    ack_request_timing_observed: bool = False
    filled_signed_qty: float = 0.0
    venue_order_id: str = ""
    rejection_key: str = ""
    terminal_status_recorded: bool = False

    @property
    def remaining_signed_qty(self) -> float:
        if self.status in {"rejected", "cancelled", "filled", "partially_filled_cancelled"}:
            return 0.0
        return self.signed_qty - self.filled_signed_qty


@dataclass(slots=True)
class AccountState:
    latest_market_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    target_proposals: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Latest accepted replacement for every component key, including explicit
    # zero targets. ``component_targets`` deliberately omits zeroes so normal
    # risk evaluation does not require prices/rules for every historical
    # component, but the execution owner still needs the zero replacement to
    # retry a terminally rejected/cancelled close after a restart.
    component_target_desires: dict[str, dict[str, Any]] = field(default_factory=dict)
    component_target_desire_sequences: dict[str, int] = field(default_factory=dict)
    component_targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    aggregate_targets: dict[str, float] = field(default_factory=dict)
    risk_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, OrderState] = field(default_factory=dict)
    positions: dict[str, PositionState] = field(default_factory=dict)
    executions: dict[str, dict[str, Any]] = field(default_factory=dict)
    protections: dict[str, dict[str, Any]] = field(default_factory=dict)
    closes: dict[str, dict[str, Any]] = field(default_factory=dict)
    pnl: dict[str, dict[str, Any]] = field(default_factory=dict)
    venue_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    processed_batches: set[str] = field(default_factory=set)
    events_applied: int = 0
    rolling_state_hash: str = GENESIS_HASH
    # Reducer-maintained acceleration index. It is derived entirely from
    # ``orders`` and deliberately excluded from materialized domain equality.
    working_order_ids: set[str] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )

    def working_signed_qty(self, symbol: str) -> float:
        return math.fsum(
            self.orders[command_id].remaining_signed_qty
            for command_id in self.working_order_ids
            if self.orders[command_id].symbol == symbol
        )

    def working_symbols(self, *, tolerance: float = 0.0) -> set[str]:
        return {
            self.orders[command_id].symbol
            for command_id in self.working_order_ids
            if abs(self.orders[command_id].remaining_signed_qty) > tolerance
        }

    def working_order_count(self, symbol: str, *, tolerance: float = 0.0) -> int:
        return sum(
            1
            for command_id in self.working_order_ids
            if self.orders[command_id].symbol == symbol
            and abs(self.orders[command_id].remaining_signed_qty) > tolerance
        )

    def state_hash(self) -> str:
        # A transition hash is O(1) in journal length and is reproduced by the
        # reducer; hashing every historical map after every event made replay
        # quadratic.
        return self.rolling_state_hash


@dataclass(frozen=True, slots=True)
class OrderCommand:
    command_id: str
    batch_id: str
    symbol: str
    side: str
    qty: float
    signed_qty: float
    reduce_only: bool
    reference_price: float
    target_signed_qty: float
    chunk_index: int
    chunk_count: int
    leverage: float = 1.0
    created_ts_ns: int = 0


@dataclass(frozen=True, slots=True)
class TargetBatchResult:
    batch_id: str
    accepted: bool
    rejection_keys: tuple[str, ...]
    commands: tuple[OrderCommand, ...]
    events: tuple[AccountEvent, ...]
    state_hash: str


def account_journal_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ACCOUNT_JOURNAL_DIRECTORY / ACCOUNT_JOURNAL_FILENAME


def account_journal_lock_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ACCOUNT_JOURNAL_DIRECTORY / ACCOUNT_JOURNAL_LOCK_FILENAME


def account_transactions_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ACCOUNT_JOURNAL_DIRECTORY / ACCOUNT_TRANSACTIONS_DIRECTORY


def _event_hash(payload: Mapping[str, Any]) -> str:
    material = dict(payload)
    material.pop("event_hash", None)
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _finite(value: Any, *, label: str) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise AccountKernelError(f"{label} must be numeric") from exc
    if not math.isfinite(output):
        raise AccountKernelError(f"{label} must be finite")
    return output


def _normalized_spec(spec: AccountEventSpec) -> dict[str, Any]:
    try:
        event_type = AccountEventType(spec.event_type)
    except ValueError as exc:
        raise AccountKernelError(f"unknown account event type {spec.event_type!r}") from exc
    if not spec.idempotency_key:
        raise AccountKernelError("account event idempotency_key is required")
    if not spec.account_id:
        raise AccountKernelError("account_id is required")
    if spec.wall_ts_ns <= 0 or spec.monotonic_ns < 0:
        raise AccountKernelError("wall_ts_ns must be positive and monotonic_ns non-negative")
    event_id = str(uuid.uuid5(_EVENT_NAMESPACE, f"{spec.account_id}\x1f{spec.idempotency_key}"))
    return {
        "event_id": event_id,
        "event_type": event_type.value,
        "correlation_id": str(spec.correlation_id),
        "causation_id": str(spec.causation_id),
        "account_id": str(spec.account_id),
        "sleeve": str(spec.sleeve),
        "symbol": str(spec.symbol).upper(),
        "wall_ts_ns": int(spec.wall_ts_ns),
        "monotonic_ns": int(spec.monotonic_ns),
        "payload": json_safe(dict(spec.payload)),
    }


def _validate_event_shape(event: AccountEvent) -> None:
    if event.schema_version != ACCOUNT_SCHEMA_VERSION:
        raise AccountJournalIntegrityError(
            f"unsupported account schema {event.schema_version}; expected {ACCOUNT_SCHEMA_VERSION}"
        )
    try:
        uuid.UUID(event.event_id)
        AccountEventType(event.event_type)
    except ValueError as exc:
        raise AccountJournalIntegrityError(f"invalid account event identity/type at {event.sequence}") from exc
    if event.sequence <= 0 or event.wall_ts_ns <= 0 or event.monotonic_ns < 0:
        raise AccountJournalIntegrityError(f"invalid account event time/sequence at {event.sequence}")
    if not event.account_id:
        raise AccountJournalIntegrityError("account_id is required")


def _apply_fill(state: AccountState, event: AccountEvent) -> None:
    payload = event.payload
    command_id = str(payload.get("command_id") or "")
    execution_id = str(payload.get("execution_id") or "")
    if not command_id or command_id not in state.orders:
        raise AccountTransitionError(f"fill references unknown command {command_id!r}")
    if not execution_id:
        raise AccountTransitionError("fill requires execution_id")
    if execution_id in state.executions:
        raise AccountTransitionError(f"duplicate execution_id {execution_id}")
    order = state.orders[command_id]
    if order.status not in {"acknowledged", "partially_filled"}:
        raise AccountTransitionError(f"fill for command {command_id} before accepted acknowledgement")
    signed_qty = _finite(payload.get("signed_qty"), label="fill signed_qty")
    price = _finite(payload.get("price"), label="fill price")
    if signed_qty == 0.0 or price <= 0.0 or signed_qty * order.signed_qty <= 0.0:
        raise AccountTransitionError(f"invalid fill direction/price for command {command_id}")
    next_filled = order.filled_signed_qty + signed_qty
    tolerance = max(abs(order.signed_qty) * 1e-12, 1e-12)
    if abs(next_filled) > abs(order.signed_qty) + tolerance:
        raise AccountTransitionError(f"fill overstates command quantity for {command_id}")
    order.filled_signed_qty = next_filled
    fill_metadata = payload.get("metadata") or {}
    modeled_terminal_qty = 0.0
    if isinstance(fill_metadata, Mapping) and "modeled_terminal_cumulative_filled_qty" in fill_metadata:
        modeled_terminal_qty = abs(
            _finite(
                fill_metadata.get("modeled_terminal_cumulative_filled_qty"),
                label="modeled terminal cumulative filled qty",
            )
        )
        if modeled_terminal_qty <= tolerance or modeled_terminal_qty > abs(order.signed_qty) + tolerance:
            raise AccountTransitionError(
                f"invalid modeled terminal cumulative quantity for command {command_id}"
            )
    if abs(next_filled - order.signed_qty) <= tolerance:
        order.status = "filled"
    elif modeled_terminal_qty and abs(next_filled) >= modeled_terminal_qty - tolerance:
        order.status = "partially_filled_cancelled"
    elif not modeled_terminal_qty and bool(fill_metadata.get("terminal")):
        order.status = "partially_filled_cancelled"
    else:
        order.status = "partially_filled"

    position = state.positions.setdefault(order.symbol, PositionState())
    position.fees_from_fills_usdt += _finite(payload.get("fee_usdt"), label="fill fee_usdt")
    prior_qty = position.signed_qty
    prior_price = position.average_price
    if prior_qty == 0.0 or prior_qty * signed_qty > 0.0:
        new_qty = prior_qty + signed_qty
        position.average_price = (
            (abs(prior_qty) * prior_price + abs(signed_qty) * price) / abs(new_qty) if new_qty != 0.0 else 0.0
        )
        position.signed_qty = new_qty
    else:
        closed_qty = min(abs(prior_qty), abs(signed_qty))
        position.realized_from_fills_usdt += closed_qty * (price - prior_price) * (1.0 if prior_qty > 0 else -1.0)
        new_qty = prior_qty + signed_qty
        position.signed_qty = 0.0 if abs(new_qty) <= tolerance else new_qty
        if position.signed_qty == 0.0:
            position.average_price = 0.0
        elif prior_qty * position.signed_qty < 0.0:
            position.average_price = price
    state.executions[execution_id] = dict(payload)


def apply_account_event(state: AccountState, event: AccountEvent) -> None:
    event_type = AccountEventType(event.event_type)
    payload = event.payload
    if event_type is AccountEventType.MARKET_INPUT_REF:
        input_key = str(payload.get("input_key") or "")
        if not input_key or not event.symbol:
            raise AccountTransitionError("market input requires input_key and symbol")
        state.latest_market_inputs[event.symbol] = dict(payload)
    elif event_type is AccountEventType.DECISION:
        decision_key = str(payload.get("decision_key") or "")
        if not decision_key:
            raise AccountTransitionError("decision requires decision_key")
        prior = state.decisions.get(decision_key)
        if prior is not None and prior != payload:
            raise AccountTransitionError(f"decision key {decision_key!r} changed content")
        state.decisions[decision_key] = dict(payload)
    elif event_type is AccountEventType.TARGET:
        target_key = str(payload.get("target_key") or "")
        if not target_key:
            raise AccountTransitionError("target requires target_key")
        state.target_proposals[f"{event.correlation_id}:{target_key}"] = dict(payload)
    elif event_type is AccountEventType.RISK_DECISION:
        batch_id = str(payload.get("batch_id") or event.correlation_id)
        if not batch_id:
            raise AccountTransitionError("risk decision requires batch_id")
        state.risk_decisions[batch_id] = dict(payload)
        state.processed_batches.add(batch_id)
        if bool(payload.get("accepted")):
            updates = payload.get("target_updates") or {}
            aggregates = payload.get("aggregate_targets") or {}
            if not isinstance(updates, Mapping) or not isinstance(aggregates, Mapping):
                raise AccountTransitionError("accepted risk decision has invalid target maps")
            accepted_proposals = [
                target for target in state.target_proposals.values() if str(target.get("batch_id") or "") == batch_id
            ]
            # Convergence retries reassert already-accepted desires solely to
            # create a fresh net order command. They must not become a new
            # strategy lifecycle revision or replace the original stop/TP/
            # reason metadata. Require both the reserved namespace and the
            # explicit marker so a coincidental/spoofed batch id cannot bypass
            # desire tracking. All ordinary accepted batches advance the full
            # registry, including zero replacements.
            convergence_retry = (
                batch_id.startswith("account-convergence/")
                and bool(accepted_proposals)
                and all(
                    bool((target.get("metadata") or {}).get("account_convergence_retry"))
                    for target in accepted_proposals
                )
            )
            if not convergence_retry:
                for target in accepted_proposals:
                    target_key = str(target.get("target_key") or "")
                    if not target_key:
                        raise AccountTransitionError("accepted target proposal lacks target_key")
                    state.component_target_desires[target_key] = dict(target)
                    state.component_target_desire_sequences[target_key] = event.sequence
            projected_targets: dict[str, dict[str, Any]] = {}
            for key, target in updates.items():
                if not isinstance(target, Mapping):
                    raise AccountTransitionError("target update must be an object")
                if abs(_finite(target.get("signed_qty"), label="target update signed_qty")) > 0.0:
                    projected_targets[str(key)] = dict(target)
            # The journal retains every zero replacement target. Current state
            # retains only desired nonzero components, otherwise every future
            # risk batch would require prices/rules for every symbol ever closed.
            state.component_targets = projected_targets
            state.aggregate_targets = {str(key): float(value) for key, value in aggregates.items()}
    elif event_type is AccountEventType.ORDER_COMMAND:
        command_id = str(payload.get("command_id") or "")
        if not command_id:
            raise AccountTransitionError("order command requires command_id")
        if command_id in state.orders:
            raise AccountTransitionError(f"duplicate order command {command_id}")
        state.orders[command_id] = OrderState(
            command_id=command_id,
            batch_id=str(payload.get("batch_id") or event.correlation_id),
            symbol=event.symbol,
            signed_qty=_finite(payload.get("signed_qty"), label="command signed_qty"),
            reduce_only=bool(payload.get("reduce_only")),
        )
        state.working_order_ids.add(command_id)
    elif event_type is AccountEventType.ACK:
        command_id = str(payload.get("command_id") or "")
        if command_id not in state.orders:
            raise AccountTransitionError(f"ack references unknown command {command_id!r}")
        order = state.orders[command_id]
        if order.status != "commanded":
            raise AccountTransitionError(f"second/stale acknowledgement for command {command_id}")
        accepted = bool(payload.get("accepted"))
        order.ack_accepted = accepted
        order.status = "acknowledged" if accepted else "rejected"
        if not accepted:
            state.working_order_ids.discard(command_id)
        order.venue_order_id = str(payload.get("venue_order_id") or "")
        order.rejection_key = str(payload.get("rejection_key") or "")
        ack_metadata = payload.get("metadata") or {}
        try:
            local_socket_send_ts_ns = int(
                (ack_metadata.get("local_socket_send_ts_ns") if isinstance(ack_metadata, Mapping) else 0) or 0
            )
        except (TypeError, ValueError):
            local_socket_send_ts_ns = 0
        order.ack_request_timing_observed = local_socket_send_ts_ns > 0
    elif event_type is AccountEventType.ACK_OBSERVATION:
        command_id = str(payload.get("command_id") or "")
        if command_id not in state.orders:
            raise AccountTransitionError(f"ack observation references unknown command {command_id!r}")
        order = state.orders[command_id]
        accepted = bool(payload.get("accepted"))
        if order.ack_accepted is None or order.ack_accepted is not accepted:
            raise AccountTransitionError(f"ack observation contradicts command {command_id}")
        observed_venue_order_id = str(payload.get("venue_order_id") or "")
        if observed_venue_order_id and order.venue_order_id and observed_venue_order_id != order.venue_order_id:
            raise AccountTransitionError(f"ack observation changed venue order id for command {command_id}")
        observation_metadata = payload.get("metadata") or {}
        try:
            local_socket_send_ts_ns = int(
                (
                    observation_metadata.get("local_socket_send_ts_ns")
                    if isinstance(observation_metadata, Mapping)
                    else 0
                )
                or 0
            )
        except (TypeError, ValueError):
            local_socket_send_ts_ns = 0
        if payload.get("observation_kind") != "http_create_response_timing" or local_socket_send_ts_ns <= 0:
            raise AccountTransitionError(f"ack observation lacks request timing for command {command_id}")
        order.ack_request_timing_observed = True
    elif event_type is AccountEventType.FILL:
        _apply_fill(state, event)
        command_id = str(payload.get("command_id") or "")
        if command_id and state.orders[command_id].remaining_signed_qty == 0.0:
            state.working_order_ids.discard(command_id)
    elif event_type is AccountEventType.ORDER_STATUS:
        command_id = str(payload.get("command_id") or "")
        if command_id not in state.orders:
            raise AccountTransitionError(f"order status references unknown command {command_id!r}")
        order = state.orders[command_id]
        status = str(payload.get("status") or "").lower()
        allowed = {"cancelled", "rejected", "filled", "partially_filled_cancelled"}
        if status not in allowed:
            raise AccountTransitionError(f"unsupported terminal order status {status!r}")
        if status == "rejected" and abs(order.filled_signed_qty) > 1e-12:
            raise AccountTransitionError("an order with fills cannot become rejected")
        if status == "filled" and abs(order.filled_signed_qty - order.signed_qty) > 1e-12:
            raise AccountTransitionError("filled status precedes reconstructed executions")
        order.status = status
        order.rejection_key = str(payload.get("rejection_key") or order.rejection_key)
        order.terminal_status_recorded = True
        state.working_order_ids.discard(command_id)
    elif event_type is AccountEventType.PROTECTION:
        protection_key = str(payload.get("protection_key") or "")
        if not protection_key:
            raise AccountTransitionError("protection requires protection_key")
        if str(payload.get("status") or "").lower() == "active":
            position = state.positions.get(event.symbol, PositionState())
            if position.signed_qty == 0.0:
                raise AccountTransitionError("active protection requires an open reconstructed position")
        state.protections[protection_key] = dict(payload)
    elif event_type is AccountEventType.CLOSE:
        close_key = str(payload.get("close_key") or "")
        if not close_key:
            raise AccountTransitionError("close requires close_key")
        if bool(payload.get("venue_flat")):
            position = state.positions.get(event.symbol, PositionState())
            if abs(position.signed_qty) > 1e-12:
                raise AccountTransitionError("venue-flat close cannot precede reconstructed close fills")
        state.closes[close_key] = dict(payload)
    elif event_type is AccountEventType.PNL:
        pnl_key = str(payload.get("pnl_key") or "")
        if not pnl_key:
            raise AccountTransitionError("pnl requires pnl_key")
        close_key = str(payload.get("close_key") or "")
        if close_key and close_key not in state.closes:
            raise AccountTransitionError(f"pnl references unknown close {close_key!r}")
        state.pnl[pnl_key] = dict(payload)
        pnl_position = state.positions.get(event.symbol)
        metadata = payload.get("metadata") or {}
        source = str(payload.get("source") or "")
        # Funding and liquidation cash-flow rows do not account for fill P&L.
        # Advancing these checkpoints for every generic PNL event could make a
        # later close silently omit realized fills or execution fees.
        fill_checkpoint = (
            (bool(metadata.get("fill_accounting_checkpoint")) if isinstance(metadata, Mapping) else False)
            or source.startswith("fill_reconstructed")
            or source == "venue_closed_pnl"
        )
        if pnl_position is not None and fill_checkpoint:
            pnl_position.reported_realized_usdt = pnl_position.realized_from_fills_usdt
            pnl_position.reported_fees_usdt = pnl_position.fees_from_fills_usdt
    elif event_type is AccountEventType.VENUE_SNAPSHOT:
        snapshot_key = str(payload.get("snapshot_key") or "")
        if not snapshot_key:
            raise AccountTransitionError("venue snapshot requires snapshot_key")
        # The immutable journal is the complete reconciliation history. The
        # materialized state only needs current venue truth; retaining every
        # checkpoint here made each later transaction copy an ever-growing map.
        state.venue_snapshots.clear()
        state.venue_snapshots[snapshot_key] = dict(payload)
    state.events_applied += 1
    transition = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "correlation_id": event.correlation_id,
        "causation_id": event.causation_id,
        "account_id": event.account_id,
        "sleeve": event.sleeve,
        "symbol": event.symbol,
        "payload": event.payload,
    }
    state.rolling_state_hash = hashlib.sha256(
        state.rolling_state_hash.encode("ascii") + canonical_json(transition)
    ).hexdigest()


def reduce_account_events(events: Sequence[AccountEvent]) -> AccountState:
    state = AccountState()
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence != expected_sequence:
            raise AccountJournalIntegrityError(
                f"account sequence gap: got {event.sequence}, expected {expected_sequence}"
            )
        apply_account_event(state, event)
    return state


def _transaction_hash(payload: Mapping[str, Any]) -> str:
    material = dict(payload)
    material.pop("transaction_hash", None)
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _read_transaction_event_bytes(
    files: Sequence[tuple[str, bytes]],
) -> list[AccountEvent] | None:
    if not files:
        return None
    events: list[AccountEvent] = []
    for label, data in files:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise AccountJournalIntegrityError(f"invalid account transaction {label}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise AccountJournalIntegrityError(f"account transaction is not an object: {label}")
        if int(payload.get("schema_version") or 0) != ACCOUNT_SCHEMA_VERSION:
            raise AccountJournalIntegrityError(f"unsupported account transaction schema: {label}")
        if str(payload.get("transaction_hash") or "") != _transaction_hash(payload):
            raise AccountJournalIntegrityError(f"account transaction hash mismatch: {label}")
        rows = payload.get("events")
        if not isinstance(rows, list) or not rows:
            raise AccountJournalIntegrityError(f"account transaction has no events: {label}")
        transaction_events = [AccountEvent.from_dict(row) for row in rows]
        if int(payload.get("first_sequence") or 0) != transaction_events[0].sequence:
            raise AccountJournalIntegrityError(f"account transaction first_sequence mismatch: {label}")
        if int(payload.get("last_sequence") or 0) != transaction_events[-1].sequence:
            raise AccountJournalIntegrityError(f"account transaction last_sequence mismatch: {label}")
        events.extend(transaction_events)
    return events


def _read_transaction_events(root: str | Path) -> list[AccountEvent] | None:
    directory = account_transactions_path(root)
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    return _read_transaction_event_bytes(
        [(str(path), path.read_bytes()) for path in paths]
    )


def _verify_account_events(events: Sequence[AccountEvent], *, verify: bool) -> list[AccountEvent]:
    state = AccountState()
    expected_hash = GENESIS_HASH
    seen_ids: set[str] = set()
    account_id = ""
    output: list[AccountEvent] = []
    for line_number, event in enumerate(events, start=1):
        _validate_event_shape(event)
        if event.sequence != line_number:
            raise AccountJournalIntegrityError(f"account sequence gap at line {line_number}: got {event.sequence}")
        if event.event_id in seen_ids:
            raise AccountJournalIntegrityError(f"duplicate account event_id {event.event_id}")
        if account_id and event.account_id != account_id:
            raise AccountJournalIntegrityError("one account journal contains multiple account ids")
        account_id = event.account_id
        if verify:
            if event.prev_event_hash != expected_hash:
                raise AccountJournalIntegrityError(f"account hash-chain break at sequence {event.sequence}")
            if event.event_hash != _event_hash(event.to_dict()):
                raise AccountJournalIntegrityError(f"account event hash mismatch at sequence {event.sequence}")
        apply_account_event(state, event)
        if verify and event.state_hash != state.state_hash():
            raise AccountJournalIntegrityError(f"account state hash mismatch at sequence {event.sequence}")
        output.append(event)
        seen_ids.add(event.event_id)
        expected_hash = event.event_hash
    return output


def read_account_journal(root: str | Path, *, verify: bool = True) -> list[AccountEvent]:
    """Read the authoritative atomic transaction segments.

    ``events.jsonl`` is a human/tooling projection. Journals persist each
    complete kernel transaction via fsync + atomic rename first, so a crash can
    expose either the prior state or the whole transaction, never half a target
    batch. A projection without transaction segments is not authoritative and
    requires an explicit account-root reset.
    """

    transaction_events = _read_transaction_events(root)
    if transaction_events is not None:
        return _verify_account_events(transaction_events, verify=verify)
    projection = account_journal_path(root)
    if projection.exists() and projection.stat().st_size > 0:
        raise AccountJournalIntegrityError(
            "account journal has events.jsonl but no authoritative transaction segments; "
            "reset the account root explicitly"
        )
    return []


def read_account_journal_bytes(
    *,
    transaction_files: Sequence[tuple[str, bytes]] = (),
    verify: bool = True,
) -> list[AccountEvent]:
    """Verify one already-captured authoritative account-journal snapshot."""

    transaction_events = _read_transaction_event_bytes(transaction_files)
    if transaction_events is None:
        raise AccountJournalIntegrityError("authoritative account transaction snapshot is empty")
    return _verify_account_events(transaction_events, verify=verify)


def verify_account_journal(root: str | Path) -> dict[str, Any]:
    """Verify and summarize the authoritative account journal."""

    events = read_account_journal(root, verify=True)
    state = reduce_account_events(events)
    transaction_dir = account_transactions_path(root)
    return {
        "journal_path": str(account_journal_path(root)),
        "events": len(events),
        "trades": len(state.closes),
        "fills": len(state.executions),
        "transactions": (len(list(transaction_dir.glob("*.json"))) if transaction_dir.is_dir() else 0),
        "last_sequence": events[-1].sequence if events else 0,
        "last_event_hash": events[-1].event_hash if events else GENESIS_HASH,
        "final_state_hash": state.state_hash(),
    }


def _atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        written = 0
        while written < len(data):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("atomic account write made no progress")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _write_transaction(root: str | Path, events: Sequence[AccountEvent]) -> Path:
    if not events:
        raise ValueError("cannot write an empty account transaction")
    payload: dict[str, Any] = {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "first_sequence": events[0].sequence,
        "last_sequence": events[-1].sequence,
        "events": [event.to_dict() for event in events],
        "transaction_hash": "",
    }
    payload["transaction_hash"] = _transaction_hash(payload)
    filename = f"{events[0].sequence:020d}-{events[-1].sequence:020d}-{payload['transaction_hash'][:16]}.json"
    path = account_transactions_path(root) / filename
    if path.exists():
        existing = json.loads(path.read_bytes())
        if existing != payload:
            raise AccountJournalIntegrityError(f"immutable transaction path changed: {path}")
        return path
    _atomic_replace(path, canonical_json(payload) + b"\n")
    return path


def _write_jsonl_projection(root: str | Path, events: Sequence[AccountEvent]) -> None:
    data = b"".join(canonical_json(event.to_dict()) + b"\n" for event in events)
    _atomic_replace(account_journal_path(root), data)


def _projection_last_event_hash(path: Path) -> str:
    """Read only the last JSONL record; transaction segments remain authority."""

    try:
        with path.open("rb") as handle:
            end = handle.seek(0, os.SEEK_END)
            if end <= 0:
                return ""
            block = 4096
            position = end
            data = b""
            while position > 0:
                take = min(block, position)
                position -= take
                handle.seek(position)
                data = handle.read(take) + data
                lines = data.splitlines()
                if len(lines) >= 2 or position == 0:
                    for line in reversed(lines):
                        if line.strip():
                            payload = json.loads(line)
                            return str(payload.get("event_hash") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""
    return ""


def _append_jsonl_projection(
    root: str | Path,
    *,
    existing: Sequence[AccountEvent],
    appended: Sequence[AccountEvent],
) -> None:
    """Append the rebuildable projection, repairing it after an interrupted write.

    Rewriting every prior event after each ACK/fill made long historical tapes
    quadratic. Atomic transaction segments are authoritative, so a stale/torn
    projection can be detected from its last hash and rebuilt before append.
    """

    if not appended:
        return
    path = account_journal_path(root)
    expected_previous = existing[-1].event_hash if existing else ""
    actual_previous = _projection_last_event_hash(path) if path.exists() else ""
    if actual_previous != expected_previous:
        _write_jsonl_projection(root, [*existing, *appended])
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    descriptor = os.open(str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        for event in appended:
            data = canonical_json(event.to_dict()) + b"\n"
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("account journal projection append made no progress")
                offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if created:
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class AccountJournal:
    """Serialized append-only account store with transactional event building."""

    def __init__(self, root: str | Path, *, account_id: str) -> None:
        self.root = Path(root).expanduser()
        self.account_id = account_id
        # Transaction segments serialize writers across processes.  This lock
        # protects the in-process cache as one immutable committed snapshot;
        # prospective transaction state is never installed until its segment
        # has been durably replaced on disk.
        self._cache_lock = threading.RLock()
        self._cached_events: list[AccountEvent] | None = None
        self._cached_events_by_id: dict[str, AccountEvent] | None = None
        self._cached_signature: tuple[object, ...] | None = None
        self._cached_state: AccountState | None = None
        if not account_id:
            raise ValueError("account_id is required")

    def _storage_signature(self) -> tuple[object, ...]:
        with self._cache_lock:
            transaction_dir = account_transactions_path(self.root)
            if transaction_dir.is_dir():
                try:
                    mtime = transaction_dir.stat().st_mtime_ns
                except OSError:
                    mtime = -1
                if (
                    self._cached_signature is not None
                    and self._cached_signature[0] == "transactions"
                    and self._cached_signature[1] == mtime
                ):
                    return self._cached_signature
                paths = sorted(transaction_dir.glob("*.json"))
                if paths:
                    return ("transactions", mtime, len(paths), paths[-1].name)
            projection = account_journal_path(self.root)
            try:
                stat = projection.stat()
            except OSError:
                return ("empty",)
            return ("jsonl", stat.st_mtime_ns, stat.st_size)

    def _events_ref(self) -> list[AccountEvent]:
        with self._cache_lock:
            signature = self._storage_signature()
            if self._cached_events is not None and signature == self._cached_signature:
                return self._cached_events
            events = read_account_journal(self.root, verify=True)
            self._cached_events = list(events)
            self._cached_events_by_id = {event.event_id: event for event in events}
            self._cached_signature = signature
            self._cached_state = None
            return self._cached_events

    def events(self) -> list[AccountEvent]:
        with self._cache_lock:
            return list(self._events_ref())

    def _state_ref(self) -> AccountState:
        with self._cache_lock:
            events = self._events_ref()
            if self._cached_state is None:
                self._cached_state = reduce_account_events(events)
            return self._cached_state

    def replay(self) -> AccountState:
        with self._cache_lock:
            return copy.deepcopy(self._state_ref())

    def transact(
        self,
        builder: Callable[[AccountState], Iterable[AccountEventSpec]],
        *,
        trusted_readonly_builder: bool = False,
    ) -> list[AccountEvent]:
        """Atomically append builder events.

        Every builder operates on state isolated from the committed cache.
        Kernel-owned builders are reviewed as read-only and may share the
        isolated reducer copy; general callers receive a separate sandbox so
        builder-side mutation cannot influence persisted event hashes.
        """

        path = account_journal_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(account_journal_lock_path(self.root), stale_seconds=600, poll_seconds=0.01):
            # Take a stable committed snapshot while holding the cache lock,
            # then release it during the potentially slow durable write.  This
            # lets concurrent readers continue to see the prior committed
            # snapshot; they can never observe the prospective reducer state.
            with self._cache_lock:
                existing = list(self._events_ref())
                committed_state = self._state_ref()
                existing_by_id = dict(self._cached_events_by_id or {event.event_id: event for event in existing})
                storage_signature = self._cached_signature
            if existing and existing[0].account_id != self.account_id:
                raise AccountJournalIntegrityError(
                    f"journal belongs to {existing[0].account_id!r}, not {self.account_id!r}"
                )
            transaction_count = 0
            if storage_signature is not None and storage_signature[0] == "transactions":
                if (
                    len(storage_signature) != 4
                    or type(storage_signature[2]) is not int
                    or storage_signature[2] <= 0
                ):
                    raise AccountJournalIntegrityError("invalid cached account transaction signature")
                transaction_count = storage_signature[2]
            elif existing:
                raise AccountJournalIntegrityError(
                    "account events exist without authoritative transaction segments; "
                    "reset the account root explicitly"
                )
            prospective_state = copy.deepcopy(committed_state)
            builder_state = prospective_state if trusted_readonly_builder else copy.deepcopy(committed_state)
            specs = list(builder(builder_state))
            normalized = [_normalized_spec(spec) for spec in specs]
            pending_by_id: dict[str, AccountEvent] = {}
            appended: list[AccountEvent] = []
            next_sequence = len(existing) + 1
            prev_hash = existing[-1].event_hash if existing else GENESIS_HASH
            for row in normalized:
                duplicate = existing_by_id.get(str(row["event_id"])) or pending_by_id.get(str(row["event_id"]))
                if duplicate is not None:
                    comparison = duplicate.to_dict()
                    for transient in (
                        "schema_version",
                        "sequence",
                        "prev_event_hash",
                        "state_hash",
                        "event_hash",
                    ):
                        comparison.pop(transient, None)
                    if comparison != row:
                        raise AccountJournalIntegrityError(
                            f"immutable account event {row['event_id']} changed content "
                            f"(existing={comparison.get('event_type')}:{comparison.get('correlation_id')}, "
                            f"proposed={row.get('event_type')}:{row.get('correlation_id')})"
                        )
                    continue
                payload = {
                    "schema_version": ACCOUNT_SCHEMA_VERSION,
                    "sequence": next_sequence,
                    **row,
                    "prev_event_hash": prev_hash,
                    "state_hash": "",
                    "event_hash": "",
                }
                provisional = AccountEvent.from_dict(payload)
                apply_account_event(prospective_state, provisional)
                payload["state_hash"] = prospective_state.state_hash()
                payload["event_hash"] = _event_hash(payload)
                event = AccountEvent.from_dict(payload)
                appended.append(event)
                pending_by_id[event.event_id] = event
                next_sequence += 1
                prev_hash = event.event_hash
            if appended:
                transaction_path = _write_transaction(self.root, appended)
                prospective_events = [*existing, *appended]
                prospective_events_by_id = {
                    **existing_by_id,
                    **{event.event_id: event for event in appended},
                }
                try:
                    transaction_mtime = transaction_path.parent.stat().st_mtime_ns
                except OSError:
                    transaction_mtime = -1
                committed_signature: tuple[object, ...] = (
                    "transactions",
                    transaction_mtime,
                    transaction_count + 1,
                    transaction_path.name,
                )
                # The atomic segment is the commit point. Publish all cache
                # fields together only after it succeeds; failed writes leave
                # the prior cache untouched.
                with self._cache_lock:
                    self._cached_events = prospective_events
                    self._cached_events_by_id = prospective_events_by_id
                    self._cached_signature = committed_signature
                    self._cached_state = prospective_state
                # Rebuildable operator/tooling projection. If this fails, the
                # transaction and published cache still agree on committed truth.
                _append_jsonl_projection(
                    self.root,
                    existing=existing,
                    appended=appended,
                )
            return appended


def _target_payload(target: DesiredTarget, *, batch_id: str) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "decision_key": target.decision_key,
        "target_key": target.target_key,
        "sleeve": target.sleeve,
        "strategy_id": target.strategy_id,
        "component_id": target.component_id,
        "symbol": target.symbol.upper(),
        "signed_qty": _finite(target.signed_qty, label="target signed_qty"),
        "reference_price": _finite(target.reference_price, label="target reference_price"),
        "leverage": _finite(target.leverage, label="target leverage"),
        "reason": target.reason,
        "metadata": json_safe(dict(target.metadata)),
    }


def _target_batch_request_hash(
    *,
    batch_id: str,
    target_payloads: Sequence[Mapping[str, Any]],
    command_symbols: set[str] | None,
    require_strict_risk_reduction: bool,
    request_content_hash: str | None,
) -> str:
    """Bind an idempotency key to immutable strategy-request content.

    Market, wallet, policy, and rule snapshots are deliberately excluded. They
    are evaluation evidence recorded by the first transaction, but a crash
    retry may observe fresher versions and must still recover that committed
    result instead of turning the same immutable target request into a conflict.
    """

    identity: Mapping[str, Any]
    if request_content_hash is None:
        identity = {
            "derived_targets": sorted(
                (dict(payload) for payload in target_payloads),
                key=lambda row: (
                    str(row["target_key"]),
                    str(row["decision_key"]),
                ),
            )
        }
    else:
        if len(request_content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in request_content_hash
        ):
            raise ValueError("request_content_hash must be lowercase SHA-256")
        identity = {"upstream_request_content_hash": request_content_hash}
    material = {
        "batch_id": batch_id,
        "identity": identity,
        "command_symbols": (None if command_symbols is None else sorted(command_symbols)),
        "require_strict_risk_reduction": bool(require_strict_risk_reduction),
    }
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _quantized_down(qty: float, step: float) -> float:
    if step <= 0.0:
        raise AccountKernelError("qty_step must be positive")
    qty_dec = Decimal(str(abs(qty)))
    step_dec = Decimal(str(step))
    units = (qty_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
    output = float(units * step_dec)
    return math.copysign(output, qty)


def _risk_rejection_key(batch_id: str, reason: str, symbol: str = "") -> str:
    suffix = f":{symbol}" if symbol else ""
    return f"account-risk:{batch_id}:{reason}{suffix}"


def _projected_nonzero_targets(
    state: AccountState,
    target_payloads: Sequence[Mapping[str, Any]],
    *,
    tolerance: float,
) -> dict[str, dict[str, Any]]:
    projected = {key: dict(value) for key, value in state.component_targets.items()}
    projected.update({str(payload["target_key"]): dict(payload) for payload in target_payloads})
    return {
        key: value
        for key, value in projected.items()
        if abs(_finite(value.get("signed_qty"), label=f"{key} signed_qty")) > tolerance
    }


def _aggregate_target_quantities(
    updates: Mapping[str, Mapping[str, Any]],
) -> dict[str, float]:
    by_symbol: dict[str, list[float]] = {}
    for target_key, payload in updates.items():
        symbol = str(payload.get("symbol") or "").upper()
        by_symbol.setdefault(symbol, []).append(_finite(payload.get("signed_qty"), label=f"{target_key} signed_qty"))
    return {symbol: math.fsum(quantities) for symbol, quantities in by_symbol.items()}


def _strictly_risk_reducing_target_batch(
    state: AccountState,
    target_payloads: Sequence[Mapping[str, Any]],
    *,
    tolerance: float,
) -> bool:
    """Prove that a target replacement cannot add venue or component risk.

    The exemption is intentionally narrower than ``reduceOnly`` inference.  A
    component may only stay the same or move toward zero, and the resulting net
    target may only stay on the reconstructed/projected position's side while
    moving toward zero.  This rejects new nonzero components, sign flips, and
    targets that look smaller than an old desire but would add to the actual
    position.  Reasserting a durable zero desire against a still-open position
    remains eligible, which is what a convergence close needs after a reject.
    """

    if not target_payloads:
        return False
    requested_symbols: set[str] = set()
    component_reduced = False
    for payload in target_payloads:
        target_key = str(payload.get("target_key") or "")
        symbol = str(payload.get("symbol") or "").upper()
        requested_symbols.add(symbol)
        new_qty = _finite(
            payload.get("signed_qty"),
            label=f"{target_key or symbol} signed_qty",
        )
        prior = state.component_target_desires.get(target_key)
        if prior is None:
            prior = state.component_targets.get(target_key)
        if prior is None:
            # A newly introduced nonzero component is an entry even when some
            # unrelated/manual venue exposure happens to make the net order
            # point toward zero. Orphan cleanup is allowed only to flat.
            if abs(new_qty) > tolerance:
                return False
            continue
        prior_symbol = str(prior.get("symbol") or "").upper()
        if not symbol or prior_symbol != symbol:
            return False
        prior_qty = _finite(
            prior.get("signed_qty"),
            label=f"prior {target_key} signed_qty",
        )
        if abs(prior_qty) > tolerance and abs(new_qty) > tolerance and prior_qty * new_qty < 0.0:
            return False
        if abs(new_qty) > abs(prior_qty) + tolerance:
            return False
        if abs(new_qty) + tolerance < abs(prior_qty):
            component_reduced = True

    if not requested_symbols or "" in requested_symbols:
        return False
    updates = _projected_nonzero_targets(
        state,
        target_payloads,
        tolerance=tolerance,
    )
    aggregates = _aggregate_target_quantities(updates)
    position_reduced = False
    all_requested_flat = True
    for symbol in requested_symbols:
        target_qty = float(aggregates.get(symbol, 0.0))
        projected_qty = math.fsum(
            (
                state.positions.get(symbol, PositionState()).signed_qty,
                state.working_signed_qty(symbol),
            )
        )
        if abs(target_qty) > tolerance and abs(projected_qty) > tolerance and target_qty * projected_qty < 0.0:
            return False
        if abs(target_qty) > abs(projected_qty) + tolerance:
            return False
        if abs(target_qty) + tolerance < abs(projected_qty):
            position_reduced = True
        if abs(target_qty) > tolerance:
            all_requested_flat = False
    return component_reduced or position_reduced or all_requested_flat


def _order_commands_from_events(events: Iterable[AccountEvent]) -> tuple[OrderCommand, ...]:
    commands: list[OrderCommand] = []
    for event in events:
        if event.event_type != AccountEventType.ORDER_COMMAND.value:
            continue
        payload = event.payload
        commands.append(
            OrderCommand(
                command_id=str(payload["command_id"]),
                batch_id=str(payload["batch_id"]),
                symbol=event.symbol,
                side=str(payload["side"]),
                qty=float(payload["qty"]),
                signed_qty=float(payload["signed_qty"]),
                reduce_only=bool(payload["reduce_only"]),
                reference_price=float(payload["reference_price"]),
                target_signed_qty=float(payload["target_signed_qty"]),
                chunk_index=int(payload["chunk_index"]),
                chunk_count=int(payload["chunk_count"]),
                leverage=float(payload.get("leverage") or 1.0),
                # Pre-cutover journals did not persist command time in the
                # payload.  Their immutable event envelope remains the direct
                # source for that timestamp; new journals persist both and the
                # drift verifier requires them to agree.
                created_ts_ns=int(payload.get("created_ts_ns") or event.wall_ts_ns),
            )
        )
    return tuple(commands)


class AccountExecutionKernel:
    """Pure account portfolio/risk sequencer plus immutable journal boundary."""

    def __init__(
        self,
        root: str | Path,
        *,
        account_id: str,
        clock: Clock | None = None,
        id_seed: str = "account-kernel-v1",
    ) -> None:
        self.journal = AccountJournal(root, account_id=account_id)
        self.account_id = account_id
        self.clock = clock or SystemClock()
        self.ids = DeterministicIds(f"{id_seed}:{account_id}")

    def state(self) -> AccountState:
        return self.journal.replay()

    def _state_ref(self) -> AccountState:
        """Trusted immutable in-process owner snapshot; never expose outside the process."""

        return self.journal._state_ref()

    def targets_are_strictly_risk_reducing(
        self,
        targets: Sequence[DesiredTarget],
        *,
        quantity_tolerance: float,
    ) -> bool:
        """Preview the kernel-owned reduction proof without mutating state.

        The service uses this only to avoid requesting unrelated account books
        and health for a close. ``_evaluate_batch`` repeats the same proof
        inside the serialized transaction, so a stale preview can never turn an
        entry into an exempt order.
        """

        if quantity_tolerance < 0.0:
            raise ValueError("quantity_tolerance cannot be negative")
        payloads = [_target_payload(target, batch_id="risk-reduction-preview") for target in targets]
        return _strictly_risk_reducing_target_batch(
            self._state_ref(),
            payloads,
            tolerance=quantity_tolerance,
        )

    def submit_targets(
        self,
        *,
        batch_id: str,
        market_inputs: Sequence[MarketInputRef],
        targets: Sequence[DesiredTarget],
        risk_snapshot: AccountRiskSnapshot,
        risk_policy: AccountRiskPolicy,
        instrument_rules: Mapping[str, InstrumentRules],
        command_symbols: set[str] | frozenset[str] | None = None,
        require_strict_risk_reduction: bool = False,
        request_content_hash: str | None = None,
    ) -> TargetBatchResult:
        if not batch_id:
            raise ValueError("batch_id is required")
        if not targets:
            raise ValueError("at least one target is required")
        market_by_symbol = {item.symbol.upper(): item for item in market_inputs}
        rules_by_symbol = {symbol.upper(): rules for symbol, rules in instrument_rules.items()}
        normalized_command_symbols = (
            None if command_symbols is None else {str(symbol).upper() for symbol in command_symbols}
        )
        if normalized_command_symbols is not None and not normalized_command_symbols:
            raise ValueError("command_symbols cannot be empty when supplied")
        target_payloads = [_target_payload(target, batch_id=batch_id) for target in targets]
        if len({row["target_key"] for row in target_payloads}) != len(target_payloads):
            raise ValueError("target_key values must be unique within a batch")
        if len({row["decision_key"] for row in target_payloads}) != len(target_payloads):
            raise ValueError("decision_key values must be unique within a batch")
        request_hash = _target_batch_request_hash(
            batch_id=batch_id,
            target_payloads=target_payloads,
            command_symbols=normalized_command_symbols,
            require_strict_risk_reduction=require_strict_risk_reduction,
            request_content_hash=request_content_hash,
        )

        def build(state: AccountState) -> list[AccountEventSpec]:
            if batch_id in state.processed_batches:
                prior = state.risk_decisions.get(batch_id) or {}
                prior_request_hash = str(prior.get("request_hash") or "")
                if len(prior_request_hash) != 64 or any(
                    character not in "0123456789abcdef"
                    for character in prior_request_hash
                ):
                    raise AccountJournalIntegrityError(
                        f"batch id {batch_id!r} has no canonical request hash; "
                        "reset the account root explicitly"
                    )
                if prior_request_hash != request_hash:
                    raise AccountJournalIntegrityError(
                        f"batch id {batch_id!r} was reused but request content changed"
                    )
                return []
            now_wall = self.clock.wall_time_ns()
            now_mono = self.clock.monotonic_ns()
            specs: list[AccountEventSpec] = []
            for market in sorted(market_inputs, key=lambda item: (item.symbol.upper(), item.input_key)):
                symbol = market.symbol.upper()
                payload = {
                    "batch_id": batch_id,
                    "input_key": market.input_key,
                    "exchange_ts_ns": int(market.exchange_ts_ns),
                    "local_receive_ts_ns": int(market.local_receive_ts_ns),
                    "reference_price": _finite(market.reference_price, label="market reference_price"),
                    "bid_price": None if market.bid_price is None else _finite(market.bid_price, label="bid_price"),
                    "ask_price": None if market.ask_price is None else _finite(market.ask_price, label="ask_price"),
                    "book_sequence": market.book_sequence,
                    "source": market.source,
                    "metadata": json_safe(dict(market.metadata)),
                }
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.MARKET_INPUT_REF,
                        idempotency_key=f"batch:{batch_id}:market:{market.input_key}",
                        correlation_id=batch_id,
                        causation_id=market.input_key,
                        account_id=self.account_id,
                        sleeve="market_data",
                        symbol=symbol,
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload=payload,
                    )
                )
            for payload in sorted(target_payloads, key=lambda row: str(row["decision_key"])):
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.DECISION,
                        idempotency_key=f"batch:{batch_id}:decision:{payload['decision_key']}",
                        correlation_id=batch_id,
                        causation_id=str(payload["decision_key"]),
                        account_id=self.account_id,
                        sleeve=str(payload["sleeve"]),
                        symbol=str(payload["symbol"]),
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload={
                            "batch_id": batch_id,
                            "decision_key": payload["decision_key"],
                            "strategy_id": payload["strategy_id"],
                            "component_id": payload["component_id"],
                            "reason": payload["reason"],
                            "market_input_key": market_by_symbol.get(
                                str(payload["symbol"]),
                                MarketInputRef(
                                    input_key="",
                                    symbol="",
                                    exchange_ts_ns=0,
                                    local_receive_ts_ns=0,
                                    reference_price=0.0,
                                ),
                            ).input_key,
                            "metadata": payload["metadata"],
                        },
                    )
                )
            for payload in sorted(target_payloads, key=lambda row: str(row["target_key"])):
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.TARGET,
                        idempotency_key=f"batch:{batch_id}:target:{payload['target_key']}",
                        correlation_id=batch_id,
                        causation_id=str(payload["decision_key"]),
                        account_id=self.account_id,
                        sleeve=str(payload["sleeve"]),
                        symbol=str(payload["symbol"]),
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload=payload,
                    )
                )

            accepted, rejection_keys, risk_payload, commands = self._evaluate_batch(
                state=state,
                batch_id=batch_id,
                target_payloads=target_payloads,
                market_by_symbol=market_by_symbol,
                risk_snapshot=risk_snapshot,
                risk_policy=risk_policy,
                instrument_rules=rules_by_symbol,
                command_created_ts_ns=now_wall,
                command_symbols=normalized_command_symbols,
                require_strict_risk_reduction=require_strict_risk_reduction,
            )
            specs.append(
                AccountEventSpec(
                    event_type=AccountEventType.RISK_DECISION,
                    idempotency_key=f"batch:{batch_id}:risk",
                    correlation_id=batch_id,
                    causation_id=batch_id,
                    account_id=self.account_id,
                    sleeve="account_risk",
                    symbol="",
                    wall_ts_ns=now_wall,
                    monotonic_ns=now_mono,
                    payload={
                        "accepted": accepted,
                        "rejection_keys": rejection_keys,
                        "request_hash": request_hash,
                        **risk_payload,
                    },
                )
            )
            if accepted:
                for command in commands:
                    specs.append(
                        AccountEventSpec(
                            event_type=AccountEventType.ORDER_COMMAND,
                            idempotency_key=f"command:{command.command_id}",
                            correlation_id=batch_id,
                            causation_id=f"batch:{batch_id}:risk",
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=command.symbol,
                            wall_ts_ns=now_wall,
                            monotonic_ns=now_mono,
                            payload=asdict(command),
                        )
                    )
            return specs

        appended = self.journal.transact(build, trusted_readonly_builder=True)
        all_events = self.journal.events()
        batch_events = tuple(event for event in all_events if event.correlation_id == batch_id)
        risk_events = [event for event in batch_events if event.event_type == AccountEventType.RISK_DECISION.value]
        if not risk_events:
            raise AccountJournalIntegrityError(f"batch {batch_id!r} has no risk decision")
        risk_payload = risk_events[-1].payload
        if str(risk_payload.get("request_hash") or "") != request_hash:
            raise AccountJournalIntegrityError(f"batch id {batch_id!r} does not match its recorded request content")
        commands = _order_commands_from_events(batch_events)
        state_hash = all_events[-1].state_hash if all_events else AccountState().state_hash()
        return TargetBatchResult(
            batch_id=batch_id,
            accepted=bool(risk_payload.get("accepted")),
            rejection_keys=tuple(str(key) for key in risk_payload.get("rejection_keys") or ()),
            commands=commands,
            events=tuple(appended),
            state_hash=state_hash,
        )

    def _evaluate_batch(
        self,
        *,
        state: AccountState,
        batch_id: str,
        target_payloads: Sequence[Mapping[str, Any]],
        market_by_symbol: Mapping[str, MarketInputRef],
        risk_snapshot: AccountRiskSnapshot,
        risk_policy: AccountRiskPolicy,
        instrument_rules: Mapping[str, InstrumentRules],
        command_created_ts_ns: int,
        command_symbols: set[str] | None = None,
        require_strict_risk_reduction: bool = False,
    ) -> tuple[bool, list[str], dict[str, Any], list[OrderCommand]]:
        rejections: list[str] = []
        for payload in target_payloads:
            target_key = str(payload.get("target_key") or "")
            prior = state.component_target_desires.get(target_key)
            if prior is None:
                continue
            metadata = payload.get("metadata") or {}
            prior_metadata = prior.get("metadata") or {}
            if not isinstance(metadata, Mapping) or not isinstance(prior_metadata, Mapping):
                continue
            try:
                revision_ns = int(metadata.get("account_request_created_ts_ns") or 0)
                prior_revision_ns = int(prior_metadata.get("account_request_created_ts_ns") or 0)
            except (TypeError, ValueError):
                rejections.append(_risk_rejection_key(batch_id, "invalid_component_revision", target_key))
                continue
            if revision_ns <= 0 or prior_revision_ns <= 0:
                continue
            request_id = str(metadata.get("account_request_id") or "")
            prior_request_id = str(prior_metadata.get("account_request_id") or "")
            new_qty = _finite(
                payload.get("signed_qty"),
                label=f"{target_key} revised signed_qty",
            )
            prior_qty = _finite(
                prior.get("signed_qty"),
                label=f"{target_key} prior revised signed_qty",
            )
            if revision_ns < prior_revision_ns or (
                revision_ns == prior_revision_ns
                and request_id != prior_request_id
                and abs(prior_qty) <= 1e-12
                and abs(new_qty) > 1e-12
            ):
                rejections.append(_risk_rejection_key(batch_id, "stale_component_revision", target_key))
        updates = _projected_nonzero_targets(
            state,
            target_payloads,
            tolerance=risk_policy.quantity_tolerance,
        )
        requested_symbols = {str(payload.get("symbol") or "").upper() for payload in target_payloads}
        risk_reducing_only = _strictly_risk_reducing_target_batch(
            state,
            target_payloads,
            tolerance=risk_policy.quantity_tolerance,
        )
        if require_strict_risk_reduction and not risk_reducing_only:
            rejections.append(_risk_rejection_key(batch_id, "strict_risk_reduction_proof_failed"))
        requested_leverages: dict[str, list[float]] = {}
        for payload in target_payloads:
            requested_leverages.setdefault(str(payload.get("symbol") or "").upper(), []).append(
                _finite(payload.get("leverage"), label="requested target leverage")
            )
        policy_values = asdict(risk_policy)
        for key, value in policy_values.items():
            number = _finite(value, label=f"risk policy {key}")
            if number < 0.0:
                rejections.append(_risk_rejection_key(batch_id, f"negative_{key}"))
        equity = _finite(risk_snapshot.equity_usdt, label="equity_usdt")
        available_margin = _finite(risk_snapshot.available_margin_usdt, label="available_margin_usdt")
        snapshot_unavailable = risk_snapshot.snapshot_key.startswith("exit-only-capital-unavailable:")
        if snapshot_unavailable and not risk_reducing_only:
            rejections.append(_risk_rejection_key(batch_id, "capital_snapshot_unavailable"))
        if equity <= 0.0 and not risk_reducing_only:
            rejections.append(_risk_rejection_key(batch_id, "nonpositive_equity"))
        if available_margin < 0.0 and not risk_reducing_only:
            rejections.append(_risk_rejection_key(batch_id, "negative_available_margin"))

        aggregates = _aggregate_target_quantities(updates)
        prices: dict[str, float] = {}
        component_gross = 0.0
        component_margin = 0.0
        symbol_leverages: dict[str, list[float]] = {}
        for target_key, payload in sorted(updates.items()):
            symbol = str(payload.get("symbol") or "").upper()
            signed_qty = _finite(payload.get("signed_qty"), label=f"{target_key} signed_qty")
            proposed_price = _finite(payload.get("reference_price"), label=f"{target_key} reference_price")
            leverage = _finite(payload.get("leverage"), label=f"{target_key} leverage")
            market = market_by_symbol.get(symbol)
            if market is None and not (risk_reducing_only and symbol not in requested_symbols):
                rejections.append(_risk_rejection_key(batch_id, "missing_market_input", symbol))
                continue
            if market is not None:
                price = _finite(
                    market.reference_price,
                    label=f"{symbol} market reference_price",
                )
            else:
                prior_market = state.latest_market_inputs.get(symbol) or {}
                price = _finite(
                    prior_market.get("reference_price", proposed_price),
                    label=f"{symbol} retained reference_price",
                )
            if not symbol or price <= 0.0 or proposed_price <= 0.0:
                rejections.append(_risk_rejection_key(batch_id, "missing_or_invalid_price", symbol))
                continue
            rules = instrument_rules.get(symbol)
            if rules is None and not (risk_reducing_only and symbol not in requested_symbols):
                rejections.append(_risk_rejection_key(batch_id, "missing_instrument_rules", symbol))
                continue
            if leverage <= 0.0:
                rejections.append(_risk_rejection_key(batch_id, "leverage_limit", symbol))
            elif not risk_reducing_only and leverage > risk_policy.max_leverage:
                rejections.append(_risk_rejection_key(batch_id, "leverage_limit", symbol))
            if (
                not risk_reducing_only
                and rules is not None
                and rules.max_leverage > 0.0
                and leverage > rules.max_leverage
            ):
                rejections.append(_risk_rejection_key(batch_id, "venue_leverage_limit", symbol))
            prices[symbol] = price
            symbol_leverages.setdefault(symbol, []).append(leverage)
            component_gross += abs(signed_qty) * price
            if leverage > 0.0:
                component_margin += abs(signed_qty) * price / leverage

        # A final zero replacement is absent from ``updates`` by design, but
        # its symbol must remain in this batch's aggregate so the kernel emits
        # the reducing command from the reconstructed position to flat.
        for symbol in sorted(requested_symbols):
            if not symbol:
                rejections.append(_risk_rejection_key(batch_id, "missing_symbol"))
                continue
            market = market_by_symbol.get(symbol)
            if market is None:
                rejections.append(_risk_rejection_key(batch_id, "missing_market_input", symbol))
                continue
            if not risk_reducing_only and market.metadata.get("exit_only_preview_freshness"):
                rejections.append(_risk_rejection_key(batch_id, "market_input_not_fresh", symbol))
            rules = instrument_rules.get(symbol)
            if rules is None:
                rejections.append(_risk_rejection_key(batch_id, "missing_instrument_rules", symbol))
                continue
            prices.setdefault(
                symbol,
                _finite(market.reference_price, label=f"{symbol} market reference_price"),
            )
            aggregates.setdefault(symbol, 0.0)
            symbol_leverages.setdefault(symbol, requested_leverages.get(symbol, [1.0]))

        account_gross = math.fsum(abs(qty) * prices[symbol] for symbol, qty in aggregates.items() if symbol in prices)
        account_margin = math.fsum(
            abs(qty) * prices[symbol] / min(symbol_leverages[symbol])
            for symbol, qty in aggregates.items()
            if symbol in prices and symbol_leverages.get(symbol) and min(symbol_leverages[symbol]) > 0.0
        )
        if not risk_reducing_only and component_gross > risk_policy.max_component_gross_notional_usdt:
            rejections.append(_risk_rejection_key(batch_id, "component_gross_limit"))
        if not risk_reducing_only and account_gross > risk_policy.max_account_gross_notional_usdt:
            rejections.append(_risk_rejection_key(batch_id, "account_gross_limit"))
        if not risk_reducing_only and component_margin > risk_policy.max_initial_margin_usdt:
            rejections.append(_risk_rejection_key(batch_id, "initial_margin_limit"))
        if not risk_reducing_only and component_margin > available_margin:
            rejections.append(_risk_rejection_key(batch_id, "available_margin_limit"))
        for symbol, signed_qty in sorted(aggregates.items()):
            if risk_reducing_only and symbol not in requested_symbols:
                continue
            if symbol not in prices or symbol not in instrument_rules:
                continue
            notional = abs(signed_qty) * prices[symbol]
            if not risk_reducing_only and notional > risk_policy.max_symbol_notional_usdt:
                rejections.append(_risk_rejection_key(batch_id, "symbol_notional_limit", symbol))
            rules = instrument_rules[symbol]
            quantized = _quantized_down(signed_qty, rules.qty_step)
            if abs(quantized - signed_qty) > risk_policy.quantity_tolerance:
                rejections.append(_risk_rejection_key(batch_id, "qty_step_mismatch", symbol))

        commands: list[OrderCommand] = []
        if not rejections:
            working_symbols = state.working_symbols(tolerance=risk_policy.quantity_tolerance)
            for symbol, target_qty in sorted(aggregates.items()):
                if risk_reducing_only and symbol not in requested_symbols:
                    continue
                if command_symbols is not None and symbol not in command_symbols:
                    continue
                position_qty = state.positions.get(symbol, PositionState()).signed_qty
                projected_qty = position_qty + state.working_signed_qty(symbol)
                tolerance = risk_policy.quantity_tolerance
                if symbol in working_symbols:
                    # A newer target supersedes the desired state immediately,
                    # but must not create an offsetting market order while an
                    # older submission is still ambiguous/live. Reconciliation
                    # first terminalizes that command; owner convergence then
                    # executes only the residual against reconstructed fills.
                    # This is especially important for target-flat: emitting a
                    # reduce-only offset while venue position is still zero is
                    # both unsafe and the source of Bybit 110017 reject loops.
                    continue
                if projected_qty * target_qty < -tolerance:
                    rejections.append(_risk_rejection_key(batch_id, "sign_flip_requires_flat", symbol))
                    continue
                delta = target_qty - projected_qty
                if abs(delta) <= tolerance:
                    continue
                rules = instrument_rules[symbol]
                qty = abs(_quantized_down(delta, rules.qty_step))
                reduce_only = abs(target_qty) + tolerance < abs(projected_qty)
                if qty + tolerance < rules.min_qty:
                    rejections.append(_risk_rejection_key(batch_id, "below_min_qty", symbol))
                    continue
                if not reduce_only and qty * prices[symbol] + tolerance < rules.min_notional:
                    rejections.append(_risk_rejection_key(batch_id, "below_min_notional", symbol))
                    continue
                max_qty = rules.max_order_qty if rules.max_order_qty > 0.0 else qty
                chunk_count = max(1, math.ceil(qty / max_qty - tolerance))
                remaining = qty
                for chunk_index in range(chunk_count):
                    chunk_qty = min(remaining, max_qty)
                    remaining -= chunk_qty
                    signed_chunk = math.copysign(chunk_qty, delta)
                    command_id = self.ids.make("order-command", batch_id, symbol, chunk_index)
                    commands.append(
                        OrderCommand(
                            command_id=command_id,
                            batch_id=batch_id,
                            symbol=symbol,
                            side="Buy" if signed_chunk > 0.0 else "Sell",
                            qty=chunk_qty,
                            signed_qty=signed_chunk,
                            reduce_only=reduce_only,
                            reference_price=prices[symbol],
                            target_signed_qty=target_qty,
                            chunk_index=chunk_index,
                            chunk_count=chunk_count,
                            leverage=min(symbol_leverages[symbol]),
                            created_ts_ns=command_created_ts_ns,
                        )
                    )

        accepted = not rejections
        if not accepted:
            commands = []
        risk_payload = {
            "batch_id": batch_id,
            "risk_snapshot": asdict(risk_snapshot),
            "risk_snapshot_status": ("unavailable_exit_only" if snapshot_unavailable else "observed"),
            "risk_policy": asdict(risk_policy),
            "instrument_rules": {
                symbol: asdict(instrument_rules[symbol]) for symbol in sorted(aggregates) if symbol in instrument_rules
            },
            "target_updates": updates if accepted else {},
            "aggregate_targets": aggregates if accepted else dict(state.aggregate_targets),
            "component_gross_notional_usdt": component_gross,
            "account_gross_notional_usdt": account_gross,
            "component_initial_margin_usdt": component_margin,
            "account_initial_margin_usdt": account_margin,
            "projected_order_count": len(commands),
            "strictly_risk_reducing": risk_reducing_only,
            "strict_risk_reduction_required": require_strict_risk_reduction,
            "risk_evaluation_symbols": sorted(requested_symbols if risk_reducing_only else aggregates),
        }
        return accepted, sorted(set(rejections)), risk_payload, commands

    def record_ack(
        self,
        *,
        command_id: str,
        accepted: bool,
        venue_order_id: str,
        exchange_ts_ns: int,
        local_ack_ts_ns: int,
        rejection_key: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        accepted = bool(accepted)
        venue_order_id = str(venue_order_id)
        rejection_key = str(rejection_key)
        exchange_ts_ns = int(exchange_ts_ns)
        local_ack_ts_ns = int(local_ack_ts_ns)
        normalized_metadata = json_safe(dict(metadata or {}))
        event_key = f"ack:{command_id}:{venue_order_id or rejection_key or int(accepted)}"

        def build(state: AccountState) -> list[AccountEventSpec]:
            order = state.orders.get(command_id)
            if order is None:
                raise AccountTransitionError(f"unknown command {command_id!r}")
            if order.ack_accepted is not None:
                if order.ack_accepted is not accepted:
                    raise AccountTransitionError(f"ack acceptance changed for command {command_id}")
                if venue_order_id and order.venue_order_id and venue_order_id != order.venue_order_id:
                    raise AccountTransitionError(f"ack venue order id changed for command {command_id}")
                if rejection_key and order.rejection_key and rejection_key != order.rejection_key:
                    raise AccountTransitionError(f"ack rejection key changed for command {command_id}")
                # REST create responses, private executions, and private order
                # rows can race while carrying the same durable acknowledgement.
                # The first observation owns the semantic transition. Preserve
                # a later HTTP request/response measurement as a supplemental
                # fact so execution timing evidence does not lose valid data.
                try:
                    local_socket_send_ts_ns = int(normalized_metadata.get("local_socket_send_ts_ns") or 0)
                except (TypeError, ValueError):
                    local_socket_send_ts_ns = 0
                if accepted and local_socket_send_ts_ns > 0 and not order.ack_request_timing_observed:
                    return [
                        AccountEventSpec(
                            event_type=AccountEventType.ACK_OBSERVATION,
                            idempotency_key=(
                                f"ack-observation:{command_id}:"
                                f"{venue_order_id or 1}:{local_socket_send_ts_ns}:"
                                f"{local_ack_ts_ns}"
                            ),
                            correlation_id=order.batch_id,
                            causation_id=command_id,
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=order.symbol,
                            wall_ts_ns=max(local_ack_ts_ns, 1),
                            # The HTTP response's durable wall timestamps are in
                            # the payload. Zero keeps exact redelivery
                            # idempotent; a later process cannot reconstruct the
                            # first observer's process-local monotonic clock.
                            monotonic_ns=0,
                            payload={
                                "command_id": command_id,
                                "accepted": accepted,
                                "venue_order_id": venue_order_id,
                                "exchange_ts_ns": exchange_ts_ns,
                                "local_ack_ts_ns": local_ack_ts_ns,
                                "rejection_key": rejection_key,
                                "metadata": normalized_metadata,
                                "observation_kind": "http_create_response_timing",
                            },
                        )
                    ]
                return []
            if order.status != "commanded":
                raise AccountTransitionError(f"acknowledgement for non-commanded order {command_id}")
            return [
                AccountEventSpec(
                    event_type=AccountEventType.ACK,
                    idempotency_key=event_key,
                    correlation_id=order.batch_id,
                    causation_id=command_id,
                    account_id=self.account_id,
                    sleeve="account_execution",
                    symbol=order.symbol,
                    wall_ts_ns=max(local_ack_ts_ns, 1),
                    monotonic_ns=self.clock.monotonic_ns(),
                    payload={
                        "command_id": command_id,
                        "accepted": accepted,
                        "venue_order_id": venue_order_id,
                        "exchange_ts_ns": exchange_ts_ns,
                        "local_ack_ts_ns": local_ack_ts_ns,
                        "rejection_key": rejection_key,
                        "metadata": normalized_metadata,
                    },
                )
            ]

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def record_fill(
        self,
        *,
        command_id: str,
        execution_id: str,
        signed_qty: float,
        price: float,
        fee_usdt: float,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        execution_id = str(execution_id)
        normalized_signed_qty = _finite(signed_qty, label="fill signed_qty")
        normalized_price = _finite(price, label="fill price")
        normalized_fee_usdt = _finite(fee_usdt, label="fill fee_usdt")
        exchange_ts_ns = int(exchange_ts_ns)
        local_receive_ts_ns = int(local_receive_ts_ns)

        def build(state: AccountState) -> list[AccountEventSpec]:
            order = state.orders.get(command_id)
            if order is None:
                raise AccountTransitionError(f"unknown command {command_id!r}")
            prior = state.executions.get(execution_id)
            if prior is not None:
                if str(prior.get("command_id") or "") != command_id:
                    raise AccountTransitionError(f"execution {execution_id} changed command identity")
                numeric_facts = (
                    ("signed quantity", prior.get("signed_qty"), normalized_signed_qty),
                    ("price", prior.get("price"), normalized_price),
                    ("fee", prior.get("fee_usdt"), normalized_fee_usdt),
                )
                for label, existing, proposed in numeric_facts:
                    if not math.isclose(
                        _finite(existing, label=f"existing fill {label}"),
                        proposed,
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    ):
                        raise AccountTransitionError(f"execution {execution_id} changed {label}")
                if int(prior.get("exchange_ts_ns") or 0) != exchange_ts_ns:
                    raise AccountTransitionError(f"execution {execution_id} changed exchange timestamp")
                # Delivery-local timestamps and source metadata may legitimately
                # differ across WS redelivery and REST recovery. The first
                # durable observation remains authoritative.
                return []
            return [
                AccountEventSpec(
                    event_type=AccountEventType.FILL,
                    idempotency_key=f"fill:{execution_id}",
                    correlation_id=order.batch_id,
                    causation_id=command_id,
                    account_id=self.account_id,
                    sleeve="account_execution",
                    symbol=order.symbol,
                    wall_ts_ns=max(local_receive_ts_ns, 1),
                    monotonic_ns=self.clock.monotonic_ns(),
                    payload={
                        "command_id": command_id,
                        "execution_id": execution_id,
                        "signed_qty": normalized_signed_qty,
                        "price": normalized_price,
                        "fee_usdt": normalized_fee_usdt,
                        "exchange_ts_ns": exchange_ts_ns,
                        "local_receive_ts_ns": local_receive_ts_ns,
                        "metadata": json_safe(dict(metadata or {})),
                    },
                )
            ]

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def record_synchronous_ack_fill_batch(
        self,
        observations: Sequence[Mapping[str, Any]],
    ) -> tuple[AccountEvent, ...]:
        """Atomically record complete synchronous adapter ACK/fill observations."""

        def build(state: AccountState) -> list[AccountEventSpec]:
            specs: list[AccountEventSpec] = []
            accepted_in_batch: set[str] = set()
            for observation in observations:
                kind = str(observation.get("kind") or "")
                command_id = str(observation.get("command_id") or "")
                order = state.orders.get(command_id)
                if order is None:
                    raise AccountTransitionError(f"synchronous execution references unknown command {command_id!r}")
                if kind == "ack":
                    if not bool(observation.get("accepted")):
                        raise AccountTransitionError("rejected acknowledgement cannot use accepted synchronous batch")
                    if order.status != "commanded":
                        raise AccountTransitionError(
                            f"synchronous acknowledgement for non-commanded order {command_id}"
                        )
                    venue_order_id = str(observation.get("venue_order_id") or "")
                    specs.append(
                        AccountEventSpec(
                            event_type=AccountEventType.ACK,
                            idempotency_key=f"ack:{command_id}:{venue_order_id or 1}",
                            correlation_id=order.batch_id,
                            causation_id=command_id,
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=order.symbol,
                            wall_ts_ns=max(int(observation.get("local_receive_ts_ns") or 0), 1),
                            monotonic_ns=self.clock.monotonic_ns(),
                            payload={
                                "command_id": command_id,
                                "accepted": True,
                                "venue_order_id": venue_order_id,
                                "exchange_ts_ns": int(observation.get("exchange_ts_ns") or 0),
                                "local_ack_ts_ns": int(observation.get("local_receive_ts_ns") or 0),
                                "rejection_key": "",
                                "metadata": json_safe(dict(observation.get("metadata") or {})),
                            },
                        )
                    )
                    accepted_in_batch.add(command_id)
                    continue
                if kind != "fill":
                    raise AccountTransitionError(f"unsupported synchronous execution observation {kind!r}")
                execution_id = str(observation.get("execution_id") or "")
                if not execution_id:
                    raise AccountTransitionError("synchronous fill requires execution_id")
                if execution_id in state.executions:
                    continue
                if order.status != "acknowledged" and command_id not in accepted_in_batch:
                    raise AccountTransitionError(f"synchronous fill for {command_id} lacks accepted acknowledgement")
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.FILL,
                        idempotency_key=f"fill:{execution_id}",
                        correlation_id=order.batch_id,
                        causation_id=command_id,
                        account_id=self.account_id,
                        sleeve="account_execution",
                        symbol=order.symbol,
                        wall_ts_ns=max(int(observation.get("local_receive_ts_ns") or 0), 1),
                        monotonic_ns=self.clock.monotonic_ns(),
                        payload={
                            "command_id": command_id,
                            "execution_id": execution_id,
                            "signed_qty": _finite(observation.get("signed_qty"), label="fill signed_qty"),
                            "price": _finite(observation.get("price"), label="fill price"),
                            "fee_usdt": _finite(observation.get("fee_usdt"), label="fill fee_usdt"),
                            "exchange_ts_ns": int(observation.get("exchange_ts_ns") or 0),
                            "local_receive_ts_ns": int(observation.get("local_receive_ts_ns") or 0),
                            "metadata": json_safe(dict(observation.get("metadata") or {})),
                        },
                    )
                )
            return specs

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def record_order_status_batch(
        self,
        observations: Sequence[Mapping[str, Any]],
    ) -> tuple[AccountEvent, ...]:
        """Record terminal statuses from one synchronous execution batch."""

        def build(state: AccountState) -> list[AccountEventSpec]:
            specs: list[AccountEventSpec] = []
            for observation in observations:
                command_id = str(observation.get("command_id") or "")
                order = state.orders.get(command_id)
                if order is None:
                    raise AccountTransitionError(f"order status references unknown command {command_id!r}")
                if order.terminal_status_recorded:
                    continue
                normalized = str(observation.get("status") or "").lower()
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.ORDER_STATUS,
                        idempotency_key=f"order-status:{command_id}:{normalized}",
                        correlation_id=order.batch_id,
                        causation_id=command_id,
                        account_id=self.account_id,
                        sleeve="account_execution",
                        symbol=order.symbol,
                        wall_ts_ns=max(int(observation.get("local_receive_ts_ns") or 0), 1),
                        monotonic_ns=self.clock.monotonic_ns(),
                        payload={
                            "command_id": command_id,
                            "status": normalized,
                            "cumulative_filled_qty": _finite(
                                observation.get("cumulative_filled_qty"),
                                label="cumulative_filled_qty",
                            ),
                            "exchange_ts_ns": int(observation.get("exchange_ts_ns") or 0),
                            "local_receive_ts_ns": int(observation.get("local_receive_ts_ns") or 0),
                            "rejection_key": str(observation.get("rejection_key") or ""),
                            "metadata": json_safe(dict(observation.get("metadata") or {})),
                        },
                    )
                )
            return specs

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def finalize_flat_position(
        self,
        *,
        symbol: str,
        command_id: str,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        """Checkpoint a completed reduce batch into account-level Close/P&L.

        The historical method name is retained for callers, but waiting for the
        *whole symbol* to become flat is incorrect when two components own the
        same net venue position.  A first component can exit while the second
        remains open; deferring its realized fills would later credit them to
        the second component's close reason.  This method therefore checkpoints
        every terminal reduce-only batch.

        The venue exposes one net position and netted fills, so the resulting
        P&L is truthful only at symbol/account-batch scope.  Component target
        identities are retained as causal context while exact component P&L is
        explicitly marked pending rather than allocated by an invented rule.
        """

        symbol = symbol.upper()
        caller_metadata = dict(metadata or {})

        def build(state: AccountState) -> list[AccountEventSpec]:
            # Re-evaluate every precondition inside the serialized journal
            # transaction. Private-stream and REST recovery can observe the same
            # terminal fill concurrently; a stale pre-transaction snapshot must
            # not propose a different immutable Close for an already-finalized
            # batch.
            order = state.orders.get(command_id)
            if order is None or order.symbol != symbol:
                raise AccountTransitionError(f"unknown close command {command_id!r} for {symbol}")
            if not order.reduce_only:
                return []
            position = state.positions.get(symbol, PositionState())
            tolerance = max(abs(order.signed_qty) * 1e-12, 1e-12)
            batch_orders = {
                candidate.command_id: candidate
                for candidate in state.orders.values()
                if candidate.batch_id == order.batch_id and candidate.symbol == symbol
            }
            if any(command in state.working_order_ids for command in batch_orders):
                return []
            batch_execution_ids = {
                execution_id
                for execution_id, execution in state.executions.items()
                if str(execution.get("command_id") or "") in batch_orders
            }
            if not batch_execution_ids:
                return []
            close_key = f"reduction:{order.batch_id}:{symbol}"
            pnl_key = f"fills:{order.batch_id}:{symbol}"
            if pnl_key in state.pnl:
                return []

            reconstructed_flat = (
                abs(position.signed_qty) <= tolerance
                and abs(float(state.aggregate_targets.get(symbol, 0.0))) <= tolerance
                and abs(state.working_signed_qty(symbol)) <= tolerance
            )
            target_rows = sorted(
                (
                    dict(target)
                    for target in state.target_proposals.values()
                    if str(target.get("batch_id") or "") == order.batch_id
                    and str(target.get("symbol") or "").upper() == symbol
                ),
                key=lambda target: str(target.get("target_key") or ""),
            )
            target_reasons = sorted(
                {
                    str(target.get("reason") or "")
                    for target in target_rows
                    if str(target.get("reason") or "")
                }
            )
            component_ids = sorted(
                {
                    str(target.get("component_id") or "")
                    for target in target_rows
                    if str(target.get("component_id") or "")
                }
            )
            component_target_keys = [
                str(target.get("target_key") or "")
                for target in target_rows
                if str(target.get("target_key") or "")
            ]
            close_reason = reason or ",".join(target_reasons) or "target_reduction"
            attribution_status = (
                "pending_account_netting"
                if component_target_keys
                else "pending_unidentified_component"
            )
            venue_flat_confirmed = (
                bool(caller_metadata.get("venue_position_confirmed_flat"))
                and reconstructed_flat
            )
            component_metadata = {
                "accounting_scope": "symbol_reduce_batch",
                "component_attribution_status": attribution_status,
                "component_ids": component_ids,
                "component_target_keys": component_target_keys,
                "component_reasons": target_reasons,
            }
            specs: list[AccountEventSpec] = []
            if close_key not in state.closes:
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.CLOSE,
                        idempotency_key=f"close:{close_key}",
                        correlation_id=order.batch_id,
                        causation_id=command_id,
                        account_id=self.account_id,
                        sleeve="account_execution",
                        symbol=symbol,
                        wall_ts_ns=max(int(local_receive_ts_ns), 1),
                        monotonic_ns=self.clock.monotonic_ns(),
                        payload={
                            "close_key": close_key,
                            "command_id": command_id,
                            "reason": close_reason,
                            # A reconstructed zero is not a fresh REST venue
                            # fact. Only an explicit caller can confirm it.
                            "venue_flat": venue_flat_confirmed,
                            "exchange_ts_ns": int(exchange_ts_ns),
                            "local_receive_ts_ns": int(local_receive_ts_ns),
                            "metadata": json_safe({
                                **caller_metadata,
                                "source": "fill_reconstruction",
                                "batch_id": order.batch_id,
                                "reconstructed_flat": reconstructed_flat,
                                "venue_position_status": (
                                    "confirmed_flat"
                                    if venue_flat_confirmed
                                    else "pending_reconciliation"
                                ),
                                **component_metadata,
                            }),
                        },
                    )
                )

            gross = position.realized_from_fills_usdt - position.reported_realized_usdt
            fees = position.fees_from_fills_usdt - position.reported_fees_usdt
            accounted_execution_ids: set[str] = set()
            for prior in state.pnl.values():
                prior_metadata = prior.get("metadata") or {}
                if not isinstance(prior_metadata, Mapping):
                    continue
                prior_ids = prior_metadata.get("accounted_execution_ids") or ()
                if isinstance(prior_ids, Sequence) and not isinstance(prior_ids, (str, bytes)):
                    accounted_execution_ids.update(
                        str(value) for value in prior_ids if str(value)
                    )
            unaccounted: dict[str, dict[str, Any]] = {}
            for execution_id, execution in state.executions.items():
                execution_order = state.orders.get(str(execution.get("command_id") or ""))
                if (
                    execution_id not in accounted_execution_ids
                    and execution_order is not None
                    and execution_order.symbol == symbol
                ):
                    unaccounted[execution_id] = execution
            fee_provenance: dict[str, str] = {}
            for execution_id, execution in sorted(unaccounted.items()):
                fill_metadata = execution.get("metadata") or {}
                if not isinstance(fill_metadata, Mapping):
                    fill_metadata = {}
                fee_status = str(fill_metadata.get("fee_status") or "")
                if not fee_status:
                    if fill_metadata.get("fee_observed") is True:
                        fee_status = "observed_execution_fee"
                    elif fill_metadata.get("fee_observed") is False:
                        fee_status = "pending_missing_execution_fee"
                    else:
                        fee_status = "pending_unknown_fee_provenance"
                fee_provenance[execution_id] = fee_status
            fee_statuses = set(fee_provenance.values())
            if any(value.startswith("pending") for value in fee_statuses) or not fee_statuses:
                fee_status = "pending_missing_or_unknown_execution_fee"
            elif fee_statuses == {"modeled_execution_fee"}:
                fee_status = "modeled_execution_fee"
            elif fee_statuses == {"observed_execution_fee"}:
                fee_status = "observed_execution_fee"
            else:
                fee_status = "observed_or_modeled_execution_fee"
            execution_twin_model = (
                bool(fee_statuses) and fee_statuses == {"modeled_execution_fee"}
            )
            funding_status = (
                "modeled_separately"
                if execution_twin_model
                else "pending_venue_reconciliation"
            )
            venue_pnl_status = (
                "not_applicable_execution_twin"
                if execution_twin_model
                else "pending_venue_reconciliation"
            )
            specs.append(
                AccountEventSpec(
                    event_type=AccountEventType.PNL,
                    idempotency_key=f"pnl:{pnl_key}",
                    correlation_id=close_key,
                    causation_id=close_key,
                    account_id=self.account_id,
                    sleeve="account_accounting",
                    symbol=symbol,
                    wall_ts_ns=max(int(local_receive_ts_ns), 1),
                    monotonic_ns=self.clock.monotonic_ns(),
                    payload={
                        "pnl_key": pnl_key,
                        "close_key": close_key,
                        "gross_pnl_usdt": _finite(gross, label="gross_pnl_usdt"),
                        "fee_usdt": _finite(fees, label="pnl fee_usdt"),
                        "funding_usdt": 0.0,
                        "net_pnl_usdt": _finite(gross - fees, label="net_pnl_usdt"),
                        "exchange_ts_ns": int(exchange_ts_ns),
                        "local_receive_ts_ns": int(local_receive_ts_ns),
                        "source": "fill_reconstructed_provisional_funding",
                        "metadata": json_safe({
                            **caller_metadata,
                            **component_metadata,
                            "batch_id": order.batch_id,
                            "fill_accounting_checkpoint": True,
                            "accounted_execution_ids": sorted(unaccounted),
                            "fee_status": fee_status,
                            "fee_provenance_by_execution": fee_provenance,
                            "funding_status": funding_status,
                            "venue_closed_pnl_status": venue_pnl_status,
                            "pnl_finalization_status": (
                                "modeled_execution_twin"
                                if execution_twin_model
                                else "provisional_venue_reconciliation"
                            ),
                        }),
                    },
                )
            )
            return specs

        return tuple(self.journal.transact(build, trusted_readonly_builder=True))

    def adopt_external_protection_fill(
        self,
        *,
        protection_key: str,
        venue_order_id: str,
        execution_id: str,
        symbol: str,
        signed_qty: float,
        price: float,
        fee_usdt: float,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        reason: str = "native_protection_triggered",
        execution_origin: str = "verified_native_stop",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        """Atomically adopt an out-of-band reduce fill and zero its owners.

        Bybit-created TP/SL executions do not carry a kernel command id.  This
        also applies to manual, liquidation, and otherwise unattributed venue
        reductions. This method synthesizes the missing canonical command/ack
        while replacing all same-symbol component targets with zero in the
        *same journal transaction* as the fill. A restart or later target cycle
        therefore cannot reopen a position the venue already reduced.

        ``execution_origin`` is deliberately closed: only a fill independently
        identified from Bybit stop provenance may claim ``verified_native_stop``.
        Other reductions are adopted conservatively without being mislabeled as
        the account-owned protection.
        """

        symbol = symbol.upper()
        if not protection_key or not execution_id:
            raise ValueError("protection_key and execution_id are required")
        allowed_origins = {
            "verified_native_stop",
            "bybit_stop_loss_unbound",
            "unattributed_external_reduction",
            "venue_liquidation",
            "venue_adl",
        }
        if execution_origin not in allowed_origins:
            raise ValueError(f"unsupported external execution_origin {execution_origin!r}")
        native_protection = execution_origin == "verified_native_stop"
        event_prefix = "external-protection" if native_protection else "external-reduction"
        fill_qty = _finite(signed_qty, label="external protection signed_qty")
        fill_price = _finite(price, label="external protection price")
        fill_fee = _finite(fee_usdt, label="external protection fee_usdt")
        if fill_qty == 0.0 or fill_price <= 0.0:
            raise ValueError("external protection fill requires nonzero qty and positive price")
        now_wall = max(int(local_receive_ts_ns), 1)
        now_mono = self.clock.monotonic_ns()
        external_order_key = venue_order_id or protection_key
        command_id = self.ids.make(f"{event_prefix}-order", protection_key, external_order_key)

        def build(state: AccountState) -> list[AccountEventSpec]:
            if execution_id in state.executions:
                return []
            protection = state.protections.get(protection_key)
            if native_protection and (
                protection is None or str(protection.get("status") or "") not in {"active", "triggering"}
            ):
                raise AccountTransitionError(f"external fill has no active native protection {protection_key!r}")
            if protection is None:
                protection = {
                    "protection_key": protection_key,
                    "command_id": "",
                    "status": "untracked_external_reduction",
                    "stop_price": None,
                    "take_profit_price": None,
                    "exchange_ts_ns": 0,
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "metadata": {"symbol": symbol, "native_exchange": False},
                }
            position = state.positions.get(symbol, PositionState())
            tolerance = max(abs(position.signed_qty) * 1e-12, 1e-12)
            if position.signed_qty == 0.0 or position.signed_qty * fill_qty >= 0.0:
                raise AccountTransitionError("external protection fill is not position-reducing")
            if abs(fill_qty) > abs(position.signed_qty) + tolerance:
                raise AccountTransitionError("external protection fill exceeds reconstructed position")
            projected_qty = position.signed_qty + fill_qty
            projected_flat = abs(projected_qty) <= tolerance
            order = state.orders.get(command_id)
            batch_id = order.batch_id if order is not None else f"{event_prefix}/{protection_key}/{external_order_key}"
            specs: list[AccountEventSpec] = []
            if order is None:
                owned_targets = {
                    key: dict(target)
                    for key, target in state.component_targets.items()
                    if str(target.get("symbol") or "").upper() == symbol
                    and abs(float(target.get("signed_qty") or 0.0)) > tolerance
                }
                if not owned_targets and abs(float(state.aggregate_targets.get(symbol, 0.0))) <= tolerance:
                    zero_desires = [
                        (key, dict(target), int(state.component_target_desire_sequences.get(key) or 0))
                        for key, target in state.component_target_desires.items()
                        if str(target.get("symbol") or "").upper() == symbol
                        and abs(float(target.get("signed_qty") or 0.0)) <= tolerance
                    ]
                    latest_revision = max(
                        (revision for _key, _target, revision in zero_desires),
                        default=0,
                    )
                    owned_targets = {
                        key: target for key, target, revision in zero_desires if revision == latest_revision
                    }
                if not owned_targets and abs(float(state.aggregate_targets.get(symbol, 0.0))) <= tolerance:
                    orphan_key = f"account/convergence/orphan/{symbol}"
                    owned_targets = {
                        orphan_key: {
                            "target_key": orphan_key,
                            "decision_key": f"external-reduction-orphan:{symbol}",
                            "batch_id": batch_id,
                            "sleeve": "account_risk",
                            "strategy_id": "account-convergence",
                            "component_id": "orphan",
                            "symbol": symbol,
                            "signed_qty": 0.0,
                            "reference_price": fill_price,
                            "leverage": 1.0,
                            "reason": "reconstructed_orphan_to_flat",
                            "metadata": {"account_convergence_orphan": True},
                        },
                    }
                if not owned_targets:
                    raise AccountTransitionError(f"external reduction fill for {symbol} has no component target owners")
                input_key = f"{event_prefix}-fill:{execution_id}"
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.MARKET_INPUT_REF,
                        idempotency_key=f"{event_prefix}-market:{execution_id}",
                        correlation_id=batch_id,
                        causation_id=execution_id,
                        account_id=self.account_id,
                        sleeve="market_data",
                        symbol=symbol,
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload={
                            "batch_id": batch_id,
                            "input_key": input_key,
                            "exchange_ts_ns": int(exchange_ts_ns),
                            "local_receive_ts_ns": int(local_receive_ts_ns),
                            "reference_price": fill_price,
                            "bid_price": None,
                            "ask_price": None,
                            "book_sequence": None,
                            "source": (
                                "bybit_native_protection_execution"
                                if native_protection
                                else "bybit_external_position_execution"
                            ),
                            "metadata": json_safe(dict(metadata or {})),
                        },
                    )
                )
                updates = {key: dict(value) for key, value in state.component_targets.items()}
                for target_key, prior in sorted(owned_targets.items()):
                    decision_key = f"{event_prefix}:{execution_id}:{target_key}"
                    execution_metadata = {
                        "external_execution_id": execution_id,
                        "external_execution_origin": execution_origin,
                    }
                    if native_protection:
                        execution_metadata["native_protection_key"] = protection_key
                    updated = {
                        **prior,
                        "batch_id": batch_id,
                        "decision_key": decision_key,
                        "signed_qty": 0.0,
                        "reference_price": fill_price,
                        "reason": reason,
                        "metadata": {
                            **dict(prior.get("metadata") or {}),
                            **execution_metadata,
                        },
                    }
                    updates[target_key] = updated
                    specs.extend(
                        (
                            AccountEventSpec(
                                event_type=AccountEventType.DECISION,
                                idempotency_key=f"{event_prefix}-decision:{execution_id}:{target_key}",
                                correlation_id=batch_id,
                                causation_id=execution_id,
                                account_id=self.account_id,
                                sleeve=str(prior.get("sleeve") or "account_risk"),
                                symbol=symbol,
                                wall_ts_ns=now_wall,
                                monotonic_ns=now_mono,
                                payload={
                                    "batch_id": batch_id,
                                    "decision_key": decision_key,
                                    "strategy_id": str(prior.get("strategy_id") or ""),
                                    "component_id": str(prior.get("component_id") or ""),
                                    "reason": reason,
                                    "market_input_key": input_key,
                                    "metadata": execution_metadata,
                                },
                            ),
                            AccountEventSpec(
                                event_type=AccountEventType.TARGET,
                                idempotency_key=f"{event_prefix}-target:{execution_id}:{target_key}",
                                correlation_id=batch_id,
                                causation_id=decision_key,
                                account_id=self.account_id,
                                sleeve=str(prior.get("sleeve") or "account_risk"),
                                symbol=symbol,
                                wall_ts_ns=now_wall,
                                monotonic_ns=now_mono,
                                payload=updated,
                            ),
                        )
                    )
                aggregates: dict[str, float] = {}
                for target in updates.values():
                    target_symbol = str(target.get("symbol") or "").upper()
                    aggregates[target_symbol] = math.fsum(
                        (
                            aggregates.get(target_symbol, 0.0),
                            float(target.get("signed_qty") or 0.0),
                        )
                    )
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.RISK_DECISION,
                        idempotency_key=f"{event_prefix}-risk:{execution_id}",
                        correlation_id=batch_id,
                        causation_id=execution_id,
                        account_id=self.account_id,
                        sleeve="account_risk",
                        symbol=symbol,
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload={
                            "batch_id": batch_id,
                            "accepted": True,
                            "rejection_keys": [],
                            "target_updates": updates,
                            "aggregate_targets": aggregates,
                            "external_position_reduction": True,
                            "external_native_protection": native_protection,
                            "external_execution_origin": execution_origin,
                        },
                    )
                )
                command_qty = abs(position.signed_qty)
                command_signed_qty = math.copysign(command_qty, fill_qty)
                specs.extend(
                    (
                        AccountEventSpec(
                            event_type=AccountEventType.ORDER_COMMAND,
                            idempotency_key=f"{event_prefix}-command:{command_id}",
                            correlation_id=batch_id,
                            causation_id=f"{event_prefix}-risk:{execution_id}",
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=symbol,
                            wall_ts_ns=now_wall,
                            monotonic_ns=now_mono,
                            payload={
                                "command_id": command_id,
                                "batch_id": batch_id,
                                "symbol": symbol,
                                "side": "Buy" if command_signed_qty > 0.0 else "Sell",
                                "qty": command_qty,
                                "signed_qty": command_signed_qty,
                                "reduce_only": True,
                                "reference_price": fill_price,
                                "target_signed_qty": 0.0,
                                "chunk_index": 0,
                                "chunk_count": 1,
                                "leverage": min(
                                    float(target.get("leverage") or 1.0) for target in owned_targets.values()
                                ),
                                "external_position_reduction": True,
                                "external_native_protection": native_protection,
                                "external_execution_origin": execution_origin,
                            },
                        ),
                        AccountEventSpec(
                            event_type=AccountEventType.ACK,
                            idempotency_key=f"{event_prefix}-ack:{command_id}",
                            correlation_id=batch_id,
                            causation_id=command_id,
                            account_id=self.account_id,
                            sleeve="account_execution",
                            symbol=symbol,
                            wall_ts_ns=now_wall,
                            monotonic_ns=now_mono,
                            payload={
                                "command_id": command_id,
                                "accepted": True,
                                "venue_order_id": venue_order_id,
                                "exchange_ts_ns": int(exchange_ts_ns),
                                "local_ack_ts_ns": int(local_receive_ts_ns),
                                "rejection_key": "",
                                "metadata": {
                                    "inferred_from_external_execution": execution_id,
                                    "external_execution_origin": execution_origin,
                                    "external_native_protection": native_protection,
                                },
                            },
                        ),
                    )
                )
            elif order.symbol != symbol or order.signed_qty * fill_qty <= 0.0:
                raise AccountTransitionError("external reduction fill contradicts adopted command")
            specs.append(
                AccountEventSpec(
                    event_type=AccountEventType.FILL,
                    idempotency_key=f"fill:{execution_id}",
                    correlation_id=batch_id,
                    causation_id=command_id,
                    account_id=self.account_id,
                    sleeve="account_execution",
                    symbol=symbol,
                    wall_ts_ns=now_wall,
                    monotonic_ns=now_mono,
                    payload={
                        "command_id": command_id,
                        "execution_id": execution_id,
                        "signed_qty": fill_qty,
                        "price": fill_price,
                        "fee_usdt": fill_fee,
                        "exchange_ts_ns": int(exchange_ts_ns),
                        "local_receive_ts_ns": int(local_receive_ts_ns),
                        "metadata": {
                            **dict(metadata or {}),
                            **({"native_protection_key": protection_key} if native_protection else {}),
                            "external_position_reduction": True,
                            "external_native_protection": native_protection,
                            "external_execution_origin": execution_origin,
                        },
                    },
                )
            )
            protection_status = (
                ("triggered" if projected_flat else "triggering")
                if native_protection
                else ("external_reduction_flat" if projected_flat else "external_reduction_partial")
            )
            prior_protection_status = str(protection.get("status") or "")
            if (
                projected_flat
                or (native_protection and prior_protection_status == "active")
                or (
                    not native_protection
                    and prior_protection_status not in {"external_reduction_partial", "external_reduction_flat"}
                )
            ):
                specs.append(
                    AccountEventSpec(
                        event_type=AccountEventType.PROTECTION,
                        idempotency_key=f"protection:{protection_key}:{protection_status}",
                        correlation_id=batch_id,
                        causation_id=command_id,
                        account_id=self.account_id,
                        sleeve="account_risk",
                        symbol=symbol,
                        wall_ts_ns=now_wall,
                        monotonic_ns=now_mono,
                        payload={
                            **dict(protection),
                            "protection_key": protection_key,
                            "command_id": command_id,
                            "status": protection_status,
                            "exchange_ts_ns": int(exchange_ts_ns),
                            "local_receive_ts_ns": int(local_receive_ts_ns),
                            "metadata": {
                                **dict(protection.get("metadata") or {}),
                                "external_execution_id": execution_id,
                                "external_execution_origin": execution_origin,
                                "external_native_protection": native_protection,
                            },
                        },
                    )
                )
            return specs

        events = list(self.journal.transact(build, trusted_readonly_builder=True))
        state = self._state_ref()
        position = state.positions.get(symbol, PositionState())
        if abs(position.signed_qty) <= 1e-12:
            events.extend(
                self.finalize_flat_position(
                    symbol=symbol,
                    command_id=command_id,
                    exchange_ts_ns=exchange_ts_ns,
                    local_receive_ts_ns=local_receive_ts_ns,
                    reason=reason,
                    metadata={
                        **({"native_protection_key": protection_key} if native_protection else {}),
                        "external_execution_id": execution_id,
                        "external_execution_origin": execution_origin,
                        "external_native_protection": native_protection,
                    },
                )
            )
        return tuple(events)

    def record_protection(
        self,
        *,
        protection_key: str,
        symbol: str,
        status: str,
        stop_price: float | None,
        take_profit_price: float | None,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        command_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        symbol = symbol.upper()
        if not protection_key:
            raise ValueError("protection_key is required")
        state = self._state_ref()
        correlation_id = state.orders[command_id].batch_id if command_id in state.orders else protection_key
        specs = [
            AccountEventSpec(
                event_type=AccountEventType.PROTECTION,
                idempotency_key=f"protection:{protection_key}:{status}",
                correlation_id=correlation_id,
                causation_id=command_id or protection_key,
                account_id=self.account_id,
                sleeve="account_risk",
                symbol=symbol,
                wall_ts_ns=max(int(local_receive_ts_ns), 1),
                monotonic_ns=self.clock.monotonic_ns(),
                payload={
                    "protection_key": protection_key,
                    "command_id": command_id,
                    "status": status,
                    "stop_price": None if stop_price is None else _finite(stop_price, label="stop_price"),
                    "take_profit_price": (
                        None if take_profit_price is None else _finite(take_profit_price, label="take_profit_price")
                    ),
                    "exchange_ts_ns": int(exchange_ts_ns),
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "metadata": json_safe(dict(metadata or {})),
                },
            )
        ]
        return tuple(self.journal.transact(lambda _: specs, trusted_readonly_builder=True))

    def record_venue_snapshot(
        self,
        *,
        snapshot_key: str,
        venue_positions: Mapping[str, float],
        reconstructed_positions: Mapping[str, float],
        mismatches: Sequence[str],
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        if not snapshot_key:
            raise ValueError("snapshot_key is required")
        specs = [
            AccountEventSpec(
                event_type=AccountEventType.VENUE_SNAPSHOT,
                idempotency_key=f"venue-snapshot:{snapshot_key}",
                correlation_id=snapshot_key,
                causation_id="venue_reconcile",
                account_id=self.account_id,
                sleeve="account_reconcile",
                symbol="",
                wall_ts_ns=max(int(local_receive_ts_ns), 1),
                monotonic_ns=self.clock.monotonic_ns(),
                payload={
                    "snapshot_key": snapshot_key,
                    "venue_positions": {key: float(value) for key, value in sorted(venue_positions.items())},
                    "reconstructed_positions": {
                        key: float(value) for key, value in sorted(reconstructed_positions.items())
                    },
                    "mismatches": list(mismatches),
                    "healthy": not mismatches,
                    "exchange_ts_ns": int(exchange_ts_ns),
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "metadata": json_safe(dict(metadata or {})),
                },
            )
        ]
        return tuple(self.journal.transact(lambda _: specs, trusted_readonly_builder=True))

    def record_order_status(
        self,
        *,
        command_id: str,
        status: str,
        cumulative_filled_qty: float,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        rejection_key: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        state = self._state_ref()
        order = state.orders.get(command_id)
        if order is None:
            raise AccountTransitionError(f"unknown command {command_id!r}")
        normalized = status.lower()
        specs = [
            AccountEventSpec(
                event_type=AccountEventType.ORDER_STATUS,
                idempotency_key=f"order-status:{command_id}:{normalized}",
                correlation_id=order.batch_id,
                causation_id=command_id,
                account_id=self.account_id,
                sleeve="account_execution",
                symbol=order.symbol,
                wall_ts_ns=max(int(local_receive_ts_ns), 1),
                monotonic_ns=self.clock.monotonic_ns(),
                payload={
                    "command_id": command_id,
                    "status": normalized,
                    "cumulative_filled_qty": _finite(cumulative_filled_qty, label="cumulative_filled_qty"),
                    "exchange_ts_ns": int(exchange_ts_ns),
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "rejection_key": rejection_key,
                    "metadata": json_safe(dict(metadata or {})),
                },
            )
        ]
        return tuple(self.journal.transact(lambda _: specs, trusted_readonly_builder=True))

    def record_close(
        self,
        *,
        close_key: str,
        symbol: str,
        reason: str,
        venue_flat: bool,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        command_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        symbol = symbol.upper()
        if not close_key:
            raise ValueError("close_key is required")
        state = self._state_ref()
        correlation_id = state.orders[command_id].batch_id if command_id in state.orders else close_key
        specs = [
            AccountEventSpec(
                event_type=AccountEventType.CLOSE,
                idempotency_key=f"close:{close_key}",
                correlation_id=correlation_id,
                causation_id=command_id or close_key,
                account_id=self.account_id,
                sleeve="account_execution",
                symbol=symbol,
                wall_ts_ns=max(int(local_receive_ts_ns), 1),
                monotonic_ns=self.clock.monotonic_ns(),
                payload={
                    "close_key": close_key,
                    "command_id": command_id,
                    "reason": reason,
                    "venue_flat": bool(venue_flat),
                    "exchange_ts_ns": int(exchange_ts_ns),
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "metadata": json_safe(dict(metadata or {})),
                },
            )
        ]
        return tuple(self.journal.transact(lambda _: specs, trusted_readonly_builder=True))

    def record_pnl(
        self,
        *,
        pnl_key: str,
        close_key: str,
        symbol: str,
        gross_pnl_usdt: float,
        fee_usdt: float,
        funding_usdt: float,
        net_pnl_usdt: float,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        source: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AccountEvent, ...]:
        if not pnl_key:
            raise ValueError("pnl_key is required")
        specs = [
            AccountEventSpec(
                event_type=AccountEventType.PNL,
                idempotency_key=f"pnl:{pnl_key}",
                correlation_id=close_key or pnl_key,
                causation_id=close_key,
                account_id=self.account_id,
                sleeve="account_accounting",
                symbol=symbol.upper(),
                wall_ts_ns=max(int(local_receive_ts_ns), 1),
                monotonic_ns=self.clock.monotonic_ns(),
                payload={
                    "pnl_key": pnl_key,
                    "close_key": close_key,
                    "gross_pnl_usdt": _finite(gross_pnl_usdt, label="gross_pnl_usdt"),
                    "fee_usdt": _finite(fee_usdt, label="pnl fee_usdt"),
                    "funding_usdt": _finite(funding_usdt, label="funding_usdt"),
                    "net_pnl_usdt": _finite(net_pnl_usdt, label="net_pnl_usdt"),
                    "exchange_ts_ns": int(exchange_ts_ns),
                    "local_receive_ts_ns": int(local_receive_ts_ns),
                    "source": source,
                    "metadata": json_safe(dict(metadata or {})),
                },
            )
        ]
        return tuple(self.journal.transact(lambda _: specs, trusted_readonly_builder=True))
