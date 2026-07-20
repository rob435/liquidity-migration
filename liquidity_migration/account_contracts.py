"""Pure-data contracts for the account execution kernel.

This module is the serialization-free contract slice of the account kernel:
event types, value objects, mutable account state, and the execution-intent
results that dependents exchange with :mod:`liquidity_migration.account_kernel`.
It deliberately has no internal imports so that consumers of the contract
vocabulary (diagnostics, adapters, replay, profiles) do not depend on journal
persistence or the stateful kernel engine.

The canonical control-plane order is::

    MarketInputRef -> Decision -> Target -> RiskDecision -> OrderCommand
      -> Ack -> Fill -> Protection -> Close -> P&L

The environment (historical, paper, demo) is intentionally absent from domain
state.  It belongs to an execution adapter, which makes pre-execution hashes
directly comparable across environments.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


ACCOUNT_SCHEMA_VERSION = 2
GENESIS_HASH = "0" * 64


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
        # dataclasses.asdict deep-copies every leaf — and before Python
        # 3.12's atomic fast path it routes even plain scalars through
        # copy.deepcopy — putting an O(payload) copy on the trusted append
        # path for consumers that only hash or JSON-serialize the result.
        # Events are immutable after publication; serialize by reference
        # with a top-level payload copy against accidental caller aliasing.
        output = {name: getattr(self, name) for name in self.__dataclass_fields__}
        output["payload"] = dict(self.payload)
        return output


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


def transaction_state_copy(state: AccountState) -> AccountState:
    """Copy mutable reducer structure without cloning immutable history payloads.

    Journal event payloads are normalized before reduction and existing mapping
    values are never mutated by the account-event reducer; a transition only
    replaces top-level mapping entries.  Orders and positions are the two
    reducer-owned mutable value types, so they receive independent shallow
    copies along with every top-level container.  This keeps prospective state
    isolated from concurrent readers without making transaction latency grow
    with all historical decision metadata.

    Untrusted transaction builders still receive a full deep copy in the
    kernel.  This optimized copy is only the reducer's private prospective
    state.
    """

    return AccountState(
        latest_market_inputs=dict(state.latest_market_inputs),
        decisions=dict(state.decisions),
        target_proposals=dict(state.target_proposals),
        component_target_desires=dict(state.component_target_desires),
        component_target_desire_sequences=dict(
            state.component_target_desire_sequences
        ),
        component_targets=dict(state.component_targets),
        aggregate_targets=dict(state.aggregate_targets),
        risk_decisions=dict(state.risk_decisions),
        orders={key: copy.copy(value) for key, value in state.orders.items()},
        positions={key: copy.copy(value) for key, value in state.positions.items()},
        executions=dict(state.executions),
        protections=dict(state.protections),
        closes=dict(state.closes),
        pnl=dict(state.pnl),
        venue_snapshots=dict(state.venue_snapshots),
        processed_batches=set(state.processed_batches),
        events_applied=state.events_applied,
        rolling_state_hash=state.rolling_state_hash,
        working_order_ids=set(state.working_order_ids),
    )


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
