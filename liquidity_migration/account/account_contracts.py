"""Pure-data contracts for the account execution kernel.

Event types, value objects, mutable account state, and execution-intent results.
No internal imports, so consumers of the contract vocabulary (diagnostics,
adapters, replay, profiles) do not pull in journal persistence or the kernel.

The canonical control-plane order is::

    MarketInputRef -> Decision -> Target -> RiskDecision -> OrderCommand
      -> Ack -> Fill -> Protection -> Close -> P&L

The environment (historical, demo, mainnet) is absent from domain state; it
belongs to an execution adapter, which keeps pre-execution hashes comparable
across environments.
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


class AmbiguousSubmissionAttemptError(AccountTransitionError):
    """An exposure command already crossed the durable provider boundary."""


class AccountEventType(StrEnum):
    MARKET_INPUT_REF = "market_input_ref"
    DECISION = "decision"
    TARGET = "target"
    RISK_DECISION = "risk_decision"
    ORDER_COMMAND = "order_command"
    # Durable pre-effect boundary, recorded immediately before an
    # exposure-capable submission so ``commanded`` never conflates unsent work
    # with an ACK-lost, possibly-live venue order.
    SUBMISSION_ATTEMPT = "submission_attempt"
    ACK = "ack"
    # A private fill can establish the semantic ACK before the HTTP create
    # response returns; this records the later timing without duplicating the
    # transition.
    ACK_OBSERVATION = "ack_observation"
    FILL = "fill"
    PROTECTION = "protection"
    CLOSE = "close"
    PNL = "pnl"
    # Market IOC orders can partially fill then cancel; without a terminal
    # status the unfilled remainder stays phantom working exposure forever.
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
    # Base quantity displayed at each touch; sizes a resting entry's clip.
    bid_qty: float | None = None
    ask_qty: float | None = None
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


# Float error in a reconstructed cumulative quantity scales with the quantity.
# A routine order is 1e5-1e7 base units and several partials accumulate a few
# ulps -- inside a relative tolerance, outside a fixed absolute one. Every
# quantity comparison (kernel, execution stream, venue protection, adapters)
# must use this one rule; a site that disagrees can raise inside the journal
# transaction on every retry forever.
QUANTITY_RELATIVE_TOLERANCE = 1e-12
QUANTITY_ABSOLUTE_TOLERANCE = 1e-12


def quantity_tolerance(signed_qty: float) -> float:
    """Comparison tolerance for a base-unit quantity, scaled by its magnitude."""

    return max(abs(signed_qty) * QUANTITY_RELATIVE_TOLERANCE, QUANTITY_ABSOLUTE_TOLERANCE)


@dataclass(frozen=True, slots=True)
class AccountRiskSnapshot:
    equity_usdt: float
    available_margin_usdt: float
    snapshot_key: str
    snapshot_ts_ns: int


@dataclass(frozen=True, slots=True)
class SleeveCapitalLimit:
    """One sleeve's private share of the account envelope.

    ``max_component_gross_notional_usdt`` bounds every sleeve's exposure
    together, so without a partition one sleeve can consume the whole envelope
    and leave the others unable to enter.

    Declared as a tuple on the policy rather than a mapping so the policy stays
    a frozen, hashable, ``asdict``-serializable value recorded verbatim in the
    journal's risk decision.
    """

    sleeve: str
    max_gross_notional_usdt: float
    max_initial_margin_usdt: float


@dataclass(frozen=True, slots=True)
class AccountRiskPolicy:
    """Explicit absolute limits; no hidden resize floor or implicit leverage."""

    max_component_gross_notional_usdt: float
    max_account_gross_notional_usdt: float
    max_symbol_notional_usdt: float
    max_initial_margin_usdt: float
    max_leverage: float
    quantity_tolerance: float = 1e-12
    #: Absolute daily loss ceiling in USDT against the UTC day's opening equity;
    #: 0.0 disables it. Enforced by ``AccountLossGuard`` in the owner loop, not
    #: here: it needs the day's opening equity and a trip must survive a restart.
    max_daily_loss_usdt: float = 0.0
    #: Per-sleeve partition of the account envelope. Empty means unpartitioned.
    #: Non-empty partitions the envelope *and* refuses any sleeve it does not
    #: name.
    sleeve_limits: tuple[SleeveCapitalLimit, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeDisasterProtectionPolicy:
    """Account-level contract for exchange-attached entry protection.

    Separate from strategy stop metadata: this is the process-death seatbelt,
    a required execution input, while the exact post-fill stop stays
    component/fill anchored.
    """

    fallback_stop_fraction: float
    trigger_by: str = "MarkPrice"

    def __post_init__(self) -> None:
        fraction = self.fallback_stop_fraction
        if (
            isinstance(fraction, bool)
            or not isinstance(fraction, (int, float))
            or not math.isfinite(float(fraction))
            or not 0.0 < float(fraction) < 1.0
        ):
            raise ValueError("native disaster fallback_stop_fraction must be a finite fraction in (0, 1)")
        if self.trigger_by != "MarkPrice":
            raise ValueError("native disaster protection requires MarkPrice triggering")


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
        # Not dataclasses.asdict: it deep-copies every leaf, an O(payload) cost
        # on the append path. Events are immutable after publication, so
        # serialize by reference with one top-level payload copy.
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
    created_ts_ns: int = 0
    command_sequence: int = 0
    entry_stop_price: float | None = None
    entry_stop_fraction: float | None = None
    entry_stop_source: str = ""
    entry_stop_trigger_by: str = ""
    submission_attempts: int = 0
    last_submission_started_ts_ns: int = 0
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
    # Latest accepted replacement per component key, zero targets included.
    # ``component_targets`` omits zeroes so risk evaluation does not need
    # prices/rules for every historical component, but the execution owner
    # needs them to retry a terminally rejected/cancelled close after restart.
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
    # Same contract: derived entirely from ``orders``. A venue order id maps to
    # a set because two commands can carry the same id, and a caller joining on
    # venue identity must be able to see that the join is ambiguous.
    command_ids_by_venue_order_id: dict[str, set[str]] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    # Same contract: derived entirely from ``target_proposals``, keyed by the
    # proposal's batch id.
    target_proposal_keys_by_batch: dict[str, set[str]] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    # Command ids whose ``OrderState`` object belongs to this state alone.
    # ``transaction_state_copy`` shares order objects, so every write must go
    # through ``order_for_write`` or it reaches into committed state that a
    # rolled-back transaction must leave untouched.
    private_order_ids: set[str] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )

    def order_for_write(self, command_id: str) -> OrderState:
        """The order under ``command_id``, private to this state and mutable."""

        order = self.orders[command_id]
        if command_id not in self.private_order_ids:
            order = copy.copy(order)
            self.orders[command_id] = order
            self.private_order_ids.add(command_id)
        return order

    def put_order(self, command_id: str, order: OrderState) -> OrderState:
        """Install a freshly built order; it is private by construction."""

        self.orders[command_id] = order
        self.private_order_ids.add(command_id)
        return order

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
        # Rolling: O(1) in journal length. Hashing every historical map per
        # event makes replay quadratic.
        return self.rolling_state_hash


def transaction_state_copy(state: AccountState) -> AccountState:
    """Copy mutable reducer structure without cloning immutable history payloads.

    A transition only replaces top-level mapping entries, so every top-level
    container plus the two reducer-owned mutable value types (orders,
    positions) get independent shallow copies. This is the reducer's private
    prospective state only; untrusted transaction builders still get a full
    deep copy in the kernel.
    """

    return AccountState(
        latest_market_inputs=dict(state.latest_market_inputs),
        decisions=dict(state.decisions),
        target_proposals=dict(state.target_proposals),
        component_target_desires=dict(state.component_target_desires),
        component_target_desire_sequences=dict(state.component_target_desire_sequences),
        component_targets=dict(state.component_targets),
        aggregate_targets=dict(state.aggregate_targets),
        risk_decisions=dict(state.risk_decisions),
        # Shared values, private dict: a write must privatize through
        # ``order_for_write``. Copying every order ever held cost a slice of the
        # whole history per transaction.
        orders=dict(state.orders),
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
        # Shared values: the reducer must REBIND these sets
        # (``d[k] = {*d.get(k, ()), v}``), never ``.add()`` in place, or a
        # rolled-back transaction leaves committed entries for events that never
        # landed. Pinned by test_the_reducer_never_mutates_an_index_set_in_place.
        command_ids_by_venue_order_id=dict(state.command_ids_by_venue_order_id),
        target_proposal_keys_by_batch=dict(state.target_proposal_keys_by_batch),
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
    entry_stop_price: float | None = None
    entry_stop_fraction: float | None = None
    entry_stop_source: str = ""
    entry_stop_trigger_by: str = ""


@dataclass(frozen=True, slots=True)
class TargetBatchResult:
    batch_id: str
    accepted: bool
    rejection_keys: tuple[str, ...]
    commands: tuple[OrderCommand, ...]
    events: tuple[AccountEvent, ...]
    state_hash: str
