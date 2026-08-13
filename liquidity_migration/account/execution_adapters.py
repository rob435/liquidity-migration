"""Execution ports for the account kernel and a deterministic market-order twin.

Historical replay uses :class:`MarketOrderExecutionTwin`. Demo uses the
same :class:`KernelExecutionDriver` with the adapter in
``bybit_execution_adapter``; private venue dependencies stay out of this shared
replay module. Private WebSocket executions normalize into the same observations.

The twin walks the observed book for an order but does not mutate future
historical snapshots — a replay assumption, not a claim of zero market impact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping

from liquidity_migration.account.account_contracts import (
    AccountEvent,
    AccountEventType,
    AmbiguousSubmissionAttemptError,
    InstrumentRules,
    MarketInputRef,
    OrderCommand,
    TargetBatchResult,
)
from liquidity_migration.account.account_kernel import AccountExecutionKernel
from liquidity_migration.core.deterministic_runtime import DeterministicIds


INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE = "integration_only_uncalibrated"


class AmbiguousExposureSubmission(RuntimeError):
    """A prior provider attempt may be live and cannot be resent as entry."""


class StaleUnsubmittedExposureCommand(RuntimeError):
    """A never-attempted entry command is too old to submit safely."""


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class ExecutionObservationType(StrEnum):
    ACK = "ack"
    FILL = "fill"
    ORDER_STATUS = "order_status"


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    observation_type: ExecutionObservationType | str
    command_id: str
    exchange_ts_ns: int
    local_receive_ts_ns: int
    accepted: bool = True
    venue_order_id: str = ""
    rejection_key: str = ""
    execution_id: str = ""
    signed_qty: float = 0.0
    price: float = 0.0
    fee_usdt: float = 0.0
    status: str = ""
    cumulative_filled_qty: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: float
    qty: float


@dataclass(frozen=True, slots=True)
class L2BookSnapshot:
    symbol: str
    sequence: int
    previous_sequence: int | None
    exchange_ts_ns: int
    local_receive_ts_ns: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    sequence_gap: bool = False
    clock_offset_estimate_ns: int | None = None

    def validation_error(self, *, expected_symbol: str | None = None) -> str:
        """Return a stable fail-closed reason for an unusable L2 snapshot."""

        if (
            type(self.symbol) is not str
            or not self.symbol
            or self.symbol != self.symbol.strip().upper()
        ):
            return "invalid_book_symbol"
        if expected_symbol is not None and self.symbol != expected_symbol:
            return "book_symbol_mismatch"
        if type(self.sequence) is not int or self.sequence <= 0:
            return "invalid_book_sequence"
        if self.previous_sequence is not None and (
            type(self.previous_sequence) is not int
            or self.previous_sequence < 0
            or self.previous_sequence >= self.sequence
        ):
            return "invalid_book_previous_sequence"
        if type(self.sequence_gap) is not bool:
            return "invalid_book_sequence_gap_flag"
        if self.sequence_gap:
            return "book_sequence_gap"
        if type(self.exchange_ts_ns) is not int or self.exchange_ts_ns <= 0:
            return "invalid_book_exchange_timestamp"
        if type(self.local_receive_ts_ns) is not int or self.local_receive_ts_ns <= 0:
            return "invalid_book_local_timestamp"
        if self.clock_offset_estimate_ns is not None and type(
            self.clock_offset_estimate_ns
        ) is not int:
            return "invalid_book_clock_offset"
        if not self.bids:
            return "empty_book_bids"
        if not self.asks:
            return "empty_book_asks"

        for side, levels in (("bid", self.bids), ("ask", self.asks)):
            for level in levels:
                if type(level) is not BookLevel:
                    return f"invalid_{side}_level"
                values = (level.price, level.qty)
                if any(
                    not _is_finite_number(value)
                    for value in values
                ):
                    return f"non_finite_{side}_level"
                if level.price <= 0.0 or level.qty <= 0.0:
                    return f"non_positive_{side}_level"
            try:
                total_qty = math.fsum(level.qty for level in levels)
            except OverflowError:
                return f"non_finite_{side}_depth"
            if not math.isfinite(total_qty):
                return f"non_finite_{side}_depth"

        if any(
            left.price <= right.price
            for left, right in zip(self.bids, self.bids[1:], strict=False)
        ):
            return "non_monotonic_book_bids"
        if any(
            left.price >= right.price
            for left, right in zip(self.asks, self.asks[1:], strict=False)
        ):
            return "non_monotonic_book_asks"
        if self.bids[0].price > self.asks[0].price:
            return "crossed_book"
        return ""

    def market_ref(self, *, input_key: str, source: str = "bybit_l2") -> MarketInputRef:
        error = self.validation_error()
        if error:
            raise ValueError(f"cannot build market reference from invalid L2: {error}")
        reference_price = self.bids[0].price / 2.0 + self.asks[0].price / 2.0
        if not math.isfinite(reference_price) or reference_price <= 0.0:
            raise ValueError(
                "cannot build market reference from invalid L2: invalid_reference_price"
            )
        return MarketInputRef(
            input_key=input_key,
            symbol=self.symbol,
            exchange_ts_ns=self.exchange_ts_ns,
            local_receive_ts_ns=self.local_receive_ts_ns,
            reference_price=reference_price,
            bid_price=self.bids[0].price,
            ask_price=self.asks[0].price,
            bid_qty=self.bids[0].qty,
            ask_qty=self.asks[0].qty,
            book_sequence=self.sequence,
            source=source,
            metadata={
                "previous_sequence": self.previous_sequence,
                "sequence_gap": self.sequence_gap,
                "clock_offset_estimate_ns": self.clock_offset_estimate_ns,
            },
        )


@dataclass(frozen=True, slots=True)
class LatencyProfile:
    decision_to_socket_ns: int
    order_entry_ns: int
    order_response_ns: int
    # Retain the fourth positional field for narrow test callers, but keep the
    # meanings distinct: spacing applies only after the first fill.
    fill_spacing_ns: int = 0
    submit_to_first_fill_ns: int = 0
    fill_response_ns: int = 0

    def __post_init__(self) -> None:
        if min(
            self.decision_to_socket_ns,
            self.order_entry_ns,
            self.order_response_ns,
            self.fill_spacing_ns,
            self.submit_to_first_fill_ns,
            self.fill_response_ns,
        ) < 0:
            raise ValueError("latencies cannot be negative")


@dataclass(frozen=True, slots=True)
class ExecutionTwinConfig:
    fee_bps: float
    latency: LatencyProfile
    max_decision_age_ns: int
    rate_limit_orders: int = 0
    rate_limit_window_ns: int = 1_000_000_000
    allow_partial_fills: bool = True
    fill_partition_policy: str = "book_level"
    immutable_replay_book: bool = True
    residual_adverse_slippage_bps: float = 0.0
    visible_book_depth: int = 50
    model_scope: str = INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE

    def __post_init__(self) -> None:
        if not math.isfinite(self.fee_bps) or self.fee_bps < 0.0:
            raise ValueError("fee_bps must be finite and non-negative")
        if self.max_decision_age_ns < 0:
            raise ValueError("max_decision_age_ns cannot be negative")
        if self.rate_limit_orders < 0 or self.rate_limit_window_ns <= 0:
            raise ValueError("execution-twin rate limits are invalid")
        if self.fill_partition_policy not in {
            "book_level",
            "single_level_full_fill_or_reject",
        }:
            raise ValueError("execution-twin fill partition policy is invalid")
        if (
            self.fill_partition_policy == "single_level_full_fill_or_reject"
            and self.allow_partial_fills
        ):
            raise ValueError("single-level full-fill policy cannot allow partial fills")
        if not math.isfinite(self.residual_adverse_slippage_bps):
            raise ValueError("residual adverse slippage must be finite")
        if self.residual_adverse_slippage_bps <= -10_000.0:
            raise ValueError("residual adverse slippage would make a fill non-positive")
        if self.visible_book_depth <= 0:
            raise ValueError("visible_book_depth must be positive")
        if not self.model_scope or self.model_scope.strip() != self.model_scope:
            raise ValueError("execution-twin model_scope must be a non-empty canonical value")


def _aligned(qty: float, step: float, *, tolerance: float = 1e-12) -> bool:
    if step <= 0.0:
        return False
    units = Decimal(str(abs(qty))) / Decimal(str(step))
    return abs(float(units) - round(float(units))) <= tolerance


def _market_input_validation_error(
    market_input: MarketInputRef,
    *,
    expected_symbol: str,
) -> str:
    if (
        type(market_input.symbol) is not str
        or not market_input.symbol
        or market_input.symbol != market_input.symbol.strip().upper()
    ):
        return "invalid_market_input_symbol"
    if market_input.symbol != expected_symbol:
        return "market_input_symbol_mismatch"
    if type(market_input.exchange_ts_ns) is not int or market_input.exchange_ts_ns <= 0:
        return "invalid_market_input_exchange_timestamp"
    if (
        type(market_input.local_receive_ts_ns) is not int
        or market_input.local_receive_ts_ns <= 0
    ):
        return "invalid_market_input_local_timestamp"
    if (
        not _is_finite_number(market_input.reference_price)
        or market_input.reference_price <= 0.0
    ):
        return "invalid_market_input_reference_price"
    if market_input.book_sequence is not None and (
        type(market_input.book_sequence) is not int or market_input.book_sequence <= 0
    ):
        return "invalid_market_input_sequence"
    for label, price in (
        ("bid", market_input.bid_price),
        ("ask", market_input.ask_price),
    ):
        if price is not None and (
            not _is_finite_number(price)
            or price <= 0.0
        ):
            return f"invalid_market_input_{label}"
    if (
        market_input.bid_price is not None
        and market_input.ask_price is not None
        and market_input.bid_price > market_input.ask_price
    ):
        return "crossed_market_input"
    return ""


class MarketOrderExecutionTwin:
    """Deterministic market-order book walker for historical replay."""

    def __init__(
        self,
        *,
        books: Mapping[str, L2BookSnapshot],
        instrument_rules: Mapping[str, InstrumentRules],
        config: ExecutionTwinConfig,
        name: str = "execution_twin",
        id_seed: str = "execution-twin-v1",
    ) -> None:
        self.name = name
        self.books = {symbol.upper(): book for symbol, book in books.items()}
        self.instrument_rules = {symbol.upper(): rules for symbol, rules in instrument_rules.items()}
        self.config = config
        self.ids = DeterministicIds(id_seed)
        self._send_times_ns: list[int] = []

    def _rejection(
        self,
        command: OrderCommand,
        *,
        reason: str,
        exchange_ts_ns: int,
        local_receive_ts_ns: int,
        send_ts_ns: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[ExecutionObservation, ...]:
        return (ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=exchange_ts_ns,
            local_receive_ts_ns=local_receive_ts_ns,
            accepted=False,
            rejection_key=f"execution:{command.command_id}:{reason}",
            metadata={
                "reason": reason,
                "local_socket_send_ts_ns": send_ts_ns,
                "fill_partition_policy": self.config.fill_partition_policy,
                "visible_book_depth": self.config.visible_book_depth,
                "execution_model_scope": self.config.model_scope,
                **dict(metadata or {}),
            },
        ),)

    def submit(
        self,
        command: OrderCommand,
        market_input: MarketInputRef,
        *,
        decision_age_limit_ns: int | None = None,
        decision_age_limit_source: str = "",
    ) -> Iterable[Mapping[str, Any] | ExecutionObservation]:
        symbol = command.symbol.upper()
        book = self.books.get(symbol)
        rules = self.instrument_rules.get(symbol)
        if command.created_ts_ns <= 0:
            raise ValueError("execution-twin command requires an explicit positive created_ts_ns")
        effective_decision_age_limit_ns = self.config.max_decision_age_ns
        effective_decision_age_limit_source = "execution_twin_config"
        decision_age_limit_overridden = False
        if decision_age_limit_ns is not None:
            if isinstance(decision_age_limit_ns, bool) or not isinstance(
                decision_age_limit_ns, int
            ):
                raise TypeError("decision-age limit must be an integer nanosecond value")
            if not command.reduce_only:
                raise ValueError("decision-age relaxation is permitted only for reduce-only commands")
            if decision_age_limit_ns < self.config.max_decision_age_ns:
                raise ValueError("reduce-only decision-age limit cannot tighten the twin limit")
            if not decision_age_limit_source or decision_age_limit_source.strip() != decision_age_limit_source:
                raise ValueError("decision-age limit source must be a non-empty canonical value")
            effective_decision_age_limit_ns = decision_age_limit_ns
            effective_decision_age_limit_source = decision_age_limit_source
            decision_age_limit_overridden = True
        send_ts_ns = command.created_ts_ns + self.config.latency.decision_to_socket_ns
        exchange_ack_ts_ns = send_ts_ns + self.config.latency.order_entry_ns
        local_ack_ts_ns = exchange_ack_ts_ns + self.config.latency.order_response_ns
        if book is None or rules is None:
            return self._rejection(
                command,
                reason="missing_book_or_rules",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
            )
        book_error = book.validation_error(expected_symbol=symbol)
        if book_error:
            return self._rejection(
                command,
                reason=book_error,
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
            )
        market_input_error = _market_input_validation_error(
            market_input,
            expected_symbol=symbol,
        )
        if market_input_error:
            return self._rejection(
                command,
                reason=market_input_error,
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
            )
        decision_age_ns = send_ts_ns - market_input.local_receive_ts_ns
        decision_age_metadata = {
            "decision_book_age_ns": decision_age_ns,
            "decision_book_age_limit_ns": effective_decision_age_limit_ns,
            "decision_book_age_limit_source": effective_decision_age_limit_source,
            "decision_book_age_limit_overridden": decision_age_limit_overridden,
        }
        if command.created_ts_ns < market_input.local_receive_ts_ns:
            return self._rejection(
                command,
                reason="future_decision_book",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
                metadata=decision_age_metadata,
            )
        if decision_age_ns > effective_decision_age_limit_ns:
            return self._rejection(
                command,
                reason="stale_decision",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
                metadata=decision_age_metadata,
            )
        if command.qty < rules.min_qty or not _aligned(command.qty, rules.qty_step):
            return self._rejection(
                command,
                reason="quantity_rule",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
                metadata=decision_age_metadata,
            )
        if not command.reduce_only and command.qty * command.reference_price < rules.min_notional:
            return self._rejection(
                command,
                reason="minimum_notional",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
                metadata=decision_age_metadata,
            )
        if rules.max_order_qty > 0.0 and command.qty > rules.max_order_qty:
            return self._rejection(
                command,
                reason="maximum_order_quantity",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
                metadata=decision_age_metadata,
            )
        if self.config.rate_limit_orders > 0:
            cutoff = send_ts_ns - self.config.rate_limit_window_ns
            self._send_times_ns = [value for value in self._send_times_ns if value > cutoff]
            if len(self._send_times_ns) >= self.config.rate_limit_orders:
                return self._rejection(
                    command,
                    reason="rate_limit",
                    exchange_ts_ns=exchange_ack_ts_ns,
                    local_receive_ts_ns=local_ack_ts_ns,
                    send_ts_ns=send_ts_ns,
                    metadata=decision_age_metadata,
                )
            self._send_times_ns.append(send_ts_ns)

        levels = (
            book.asks[: self.config.visible_book_depth]
            if command.signed_qty > 0.0
            else book.bids[: self.config.visible_book_depth]
        )
        available = math.fsum(level.qty for level in levels)
        if available <= 0.0:
            return self._rejection(
                command,
                reason="no_liquidity",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
                metadata=decision_age_metadata,
            )
        if available < command.qty and not self.config.allow_partial_fills:
            return self._rejection(
                command,
                reason="insufficient_depth",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
                metadata=decision_age_metadata,
            )
        if (
            self.config.fill_partition_policy == "single_level_full_fill_or_reject"
            and levels[0].qty < command.qty - 1e-12
        ):
            return self._rejection(
                command,
                reason="unidentified_split_fill_path",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
                metadata=decision_age_metadata,
            )

        venue_order_id = self.ids.make("venue-order", command.command_id)
        observations: list[ExecutionObservation] = [ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=exchange_ack_ts_ns,
            local_receive_ts_ns=local_ack_ts_ns,
            accepted=True,
            venue_order_id=venue_order_id,
            metadata={
                "local_socket_send_ts_ns": send_ts_ns,
                "book_sequence": book.sequence,
                "book_exchange_ts_ns": book.exchange_ts_ns,
                "book_local_receive_ts_ns": book.local_receive_ts_ns,
                "feed_latency_ns": book.local_receive_ts_ns - book.exchange_ts_ns,
                "command_created_ts_ns": command.created_ts_ns,
                "decision_book_age_ns": decision_age_ns,
                "decision_book_age_limit_ns": effective_decision_age_limit_ns,
                "decision_book_age_limit_source": effective_decision_age_limit_source,
                "decision_book_age_limit_overridden": decision_age_limit_overridden,
                "order_entry_latency_ns": self.config.latency.order_entry_ns,
                "order_response_latency_ns": self.config.latency.order_response_ns,
                "submit_to_first_fill_latency_ns": self.config.latency.submit_to_first_fill_ns,
                "fill_response_latency_ns": self.config.latency.fill_response_ns,
                "immutable_replay_book": self.config.immutable_replay_book,
                "fill_partition_policy": self.config.fill_partition_policy,
                "visible_book_depth": self.config.visible_book_depth,
                "execution_model_scope": self.config.model_scope,
            },
        )]
        remaining = command.qty
        executable = min(command.qty, available)
        fill_count = 0
        for level in levels:
            if remaining <= 1e-12:
                continue
            fill_qty = min(remaining, level.qty)
            remaining -= fill_qty
            fill_count += 1
            signed_qty = math.copysign(fill_qty, command.signed_qty)
            exchange_fill_ts_ns = (
                send_ts_ns
                + self.config.latency.submit_to_first_fill_ns
                + (fill_count - 1) * self.config.latency.fill_spacing_ns
            )
            terminal = abs(math.fsum(obs.signed_qty for obs in observations) + signed_qty) >= executable - 1e-12
            direction = 1.0 if command.signed_qty > 0.0 else -1.0
            fill_price = level.price * (
                1.0
                + direction
                * self.config.residual_adverse_slippage_bps
                / 10_000.0
            )
            if fill_price <= 0.0 or not math.isfinite(fill_price):
                raise ValueError("execution-twin residual slippage produced an invalid price")
            observations.append(ExecutionObservation(
                observation_type=ExecutionObservationType.FILL,
                command_id=command.command_id,
                exchange_ts_ns=exchange_fill_ts_ns,
                local_receive_ts_ns=exchange_fill_ts_ns + self.config.latency.fill_response_ns,
                venue_order_id=venue_order_id,
                execution_id=self.ids.make("execution", command.command_id, fill_count),
                signed_qty=signed_qty,
                price=fill_price,
                fee_usdt=abs(fill_qty * fill_price) * self.config.fee_bps / 10_000.0,
                metadata={
                    "book_sequence": book.sequence,
                    "book_level": fill_count - 1,
                    # Carried on every fill so equal-timestamp executions stay
                    # replayable when delivery order differs from book-level
                    # order; kernel state uses reconstructed cumulative quantity.
                    "modeled_terminal_cumulative_filled_qty": executable,
                    "submit_to_first_fill_latency_ns": self.config.latency.submit_to_first_fill_ns,
                    "inter_fill_spacing_ns": self.config.latency.fill_spacing_ns,
                    "fill_response_latency_ns": self.config.latency.fill_response_ns,
                    "visible_book_price": level.price,
                    "residual_adverse_slippage_bps": self.config.residual_adverse_slippage_bps,
                    "terminal": terminal,
                    "unfilled_cancelled_qty": max(command.qty - executable, 0.0) if terminal else 0.0,
                    "immutable_replay_book": self.config.immutable_replay_book,
                    "fill_partition_policy": self.config.fill_partition_policy,
                    "visible_book_depth": self.config.visible_book_depth,
                    "execution_model_scope": self.config.model_scope,
                    **decision_age_metadata,
                    "fee_observed": True,
                    "fee_status": "modeled_execution_fee",
                    "fee_source": "execution_twin_config.fee_bps",
                    "source": "execution_twin_l2_fill",
                },
            ))
        final_fill = next(
            observation
            for observation in reversed(observations)
            if observation.observation_type == ExecutionObservationType.FILL
        )
        observations.append(ExecutionObservation(
            observation_type=ExecutionObservationType.ORDER_STATUS,
            command_id=command.command_id,
            # No terminal-status latency is identifiable in V3.  Keep the
            # synchronous modeled status at the final fill boundary instead of
            # reusing inter-fill spacing for a different phenomenon.
            exchange_ts_ns=final_fill.exchange_ts_ns,
            local_receive_ts_ns=final_fill.local_receive_ts_ns,
            venue_order_id=venue_order_id,
            status="filled" if executable >= command.qty - 1e-12 else "partially_filled_cancelled",
            cumulative_filled_qty=executable,
            metadata={
                "source": "execution_twin_order_status",
                "fill_partition_policy": self.config.fill_partition_policy,
                "visible_book_depth": self.config.visible_book_depth,
                "execution_model_scope": self.config.model_scope,
                **decision_age_metadata,
            },
        ))
        return tuple(observations)


class KernelExecutionDriver:
    """Environment-neutral observation dispatcher for one account kernel."""

    def __init__(self, kernel: AccountExecutionKernel) -> None:
        self.kernel = kernel

    def execute_batch(
        self,
        result: TargetBatchResult,
        *,
        market_inputs: Mapping[str, MarketInputRef],
        adapter: Any,
    ) -> tuple[AccountEvent, ...]:
        events: list[AccountEvent] = []
        synchronous: list[tuple[int, OrderCommand, tuple[ExecutionObservation, ...]]] = []
        # Attempts claimed inside THIS pass's plan commit (gated fusion).
        # ``result.events`` carries only freshly appended events, so a crash
        # replay sees an empty set here and the ambiguous-resend refusal below
        # still owns every command whose durable attempt this process did not
        # just write.
        fused_command_ids = frozenset(
            str(event.payload.get("command_id") or "")
            for event in result.events
            if event.event_type == AccountEventType.SUBMISSION_ATTEMPT.value
        )

        def durable_barrier() -> int:
            """The one fail-closed pre-wire disk sync; returns its wait in ns."""

            started_ns = self.kernel.clock.monotonic_ns()
            self.kernel.journal.barrier()
            wait_ns = max(self.kernel.clock.monotonic_ns() - started_ns, 0)
            ledger = getattr(self.kernel, "span_ledger", None)
            if ledger is not None:
                ledger.barrier_wait_ns = max(ledger.barrier_wait_ns, 0) + wait_ns
                ledger.barrier_done_ts_ns = self.kernel.clock.wall_time_ns()
            return wait_ns

        def flush_synchronous() -> None:
            if not synchronous:
                return
            ack_fills: list[dict[str, Any]] = []
            statuses: list[dict[str, Any]] = []
            last_fill_by_symbol: dict[
                str, tuple[int, OrderCommand, ExecutionObservation]
            ] = {}
            for command_index, command, observations in synchronous:
                for observation in observations:
                    observation_type = ExecutionObservationType(
                        observation.observation_type
                    )
                    common = {
                        "command_id": observation.command_id,
                        "exchange_ts_ns": observation.exchange_ts_ns,
                        "local_receive_ts_ns": observation.local_receive_ts_ns,
                        "metadata": observation.metadata,
                    }
                    if observation_type is ExecutionObservationType.ACK:
                        ack_fills.append({
                            **common,
                            "kind": "ack",
                            "accepted": observation.accepted,
                            "venue_order_id": observation.venue_order_id,
                        })
                    elif observation_type is ExecutionObservationType.FILL:
                        ack_fills.append({
                            **common,
                            "kind": "fill",
                            "execution_id": observation.execution_id,
                            "signed_qty": observation.signed_qty,
                            "price": observation.price,
                            "fee_usdt": observation.fee_usdt,
                        })
                        last_fill_by_symbol[command.symbol] = (
                            command_index,
                            command,
                            observation,
                        )
                    else:
                        statuses.append({
                            **common,
                            "status": observation.status,
                            "cumulative_filled_qty": observation.cumulative_filled_qty,
                            "rejection_key": observation.rejection_key,
                        })

            events.extend(self.kernel.record_synchronous_ack_fill_batch(ack_fills))
            # All fills are reconstructed before flatness is tested. Finalize
            # only the last command per symbol so a chunked close cannot emit
            # duplicate Close/P&L rows.
            for _, command, fill in sorted(last_fill_by_symbol.values()):
                events.extend(self.kernel.finalize_flat_position(
                    symbol=command.symbol,
                    command_id=command.command_id,
                    exchange_ts_ns=fill.exchange_ts_ns,
                    local_receive_ts_ns=fill.local_receive_ts_ns,
                ))
            events.extend(self.kernel.record_order_status_batch(statuses))
            synchronous.clear()

        def dispatch(
            command_index: int,
            command: OrderCommand,
            normalized: tuple[ExecutionObservation, ...],
        ) -> None:
            if self._is_complete_synchronous(command, normalized):
                synchronous.append((command_index, command, normalized))
                return
            flush_synchronous()
            events.extend(self.ingest(normalized))

        batch_handled = self._submit_prepared_as_batch(
            result,
            market_inputs=market_inputs,
            adapter=adapter,
            dispatch=dispatch,
            fused_command_ids=fused_command_ids,
            durable_barrier=durable_barrier,
        )

        for command_index, command in enumerate(result.commands):
            order = self.kernel._state_ref().orders.get(command.command_id)
            if order is None:
                raise ValueError(f"unknown kernel command {command.command_id}")
            if order.status != "commanded":
                # Crash replay: an already acknowledged/filled/rejected command
                # must not be submitted again.
                continue
            if command.command_id in batch_handled:
                continue
            market = market_inputs.get(command.symbol)
            if market is None:
                raise ValueError(f"missing market input for command symbol {command.symbol}")
            ambiguous_provider = bool(
                getattr(
                    adapter,
                    "submission_outcome_can_be_ambiguous",
                    False,
                )
            )
            # This pass's plan commit already claimed the attempt (fusion), so
            # the claim below is skipped and the attempts>0 refusal must not
            # fire for it. Replay never reaches here with a non-empty set.
            fused_claim = command.command_id in fused_command_ids
            prepared_observations: tuple[ExecutionObservation, ...] = ()
            submit_effect = adapter.submit
            if ambiguous_provider:
                if (
                    not fused_claim
                    and order.submission_attempts > 0
                    and not order.reduce_only
                ):
                    raise AmbiguousExposureSubmission(
                        "refusing to resend an exposure-increasing command after "
                        "a durable ambiguous submission attempt: "
                        f"command={command.command_id} "
                        f"attempts={order.submission_attempts} "
                        f"last_started_ts_ns={order.last_submission_started_ts_ns}"
                    )
                max_unsubmitted_age_ns = getattr(
                    adapter,
                    "max_unsubmitted_exposure_age_ns",
                    None,
                )
                if not order.reduce_only and order.submission_attempts == 0:
                    if type(max_unsubmitted_age_ns) is not int or max_unsubmitted_age_ns <= 0:
                        raise ValueError(
                            "ambiguous provider adapter requires a positive integer "
                            "max_unsubmitted_exposure_age_ns"
                        )
                    now_ns = self.kernel.clock.wall_time_ns()
                    if (
                        command.created_ts_ns <= 0
                        or now_ns < command.created_ts_ns
                        or now_ns - command.created_ts_ns > max_unsubmitted_age_ns
                    ):
                        raise StaleUnsubmittedExposureCommand(
                            "refusing to submit a stale exposure-increasing command: "
                            f"command={command.command_id} "
                            f"created_ts_ns={command.created_ts_ns} "
                            f"now_ts_ns={now_ns} "
                            f"max_age_ns={max_unsubmitted_age_ns}"
                        )
                prepare_submission = getattr(
                    adapter,
                    "prepare_submission",
                    None,
                )
                submit_prepared = getattr(adapter, "submit_prepared", None)
                if callable(prepare_submission) != callable(submit_prepared):
                    raise ValueError(
                        "ambiguous provider adapter must define both "
                        "prepare_submission and submit_prepared"
                    )
                if callable(prepare_submission) and callable(submit_prepared):
                    submit_effect = submit_prepared
                    if not fused_claim:
                        # A fused command's leverage was proven satisfied at
                        # commit time, making this a guaranteed no-op there;
                        # skipping it keeps every venue call after the claim
                        # unambiguous.
                        prepare_started_ns = self.kernel.clock.monotonic_ns()
                        prepared_observations = self._normalize_observations(
                            prepare_submission(command, market)
                        )
                        self._record_leverage_wait(
                            max(
                                self.kernel.clock.monotonic_ns() - prepare_started_ns,
                                0,
                            )
                        )
                if prepared_observations:
                    normalized = prepared_observations
                else:
                    # Preparation is itself a provider round trip (leverage
                    # negotiation), so recheck freshness at the exposure boundary.
                    if not order.reduce_only and order.submission_attempts == 0:
                        now_ns = self.kernel.clock.wall_time_ns()
                        assert type(max_unsubmitted_age_ns) is int
                        if (
                            now_ns < command.created_ts_ns
                            or now_ns - command.created_ts_ns > max_unsubmitted_age_ns
                        ):
                            raise StaleUnsubmittedExposureCommand(
                                "refusing to submit a stale exposure-increasing command "
                                "after provider preparation: "
                                f"command={command.command_id} "
                                f"created_ts_ns={command.created_ts_ns} "
                                f"now_ts_ns={now_ns} "
                                f"max_age_ns={max_unsubmitted_age_ns}"
                            )
                    if not fused_claim:
                        ledger = getattr(self.kernel, "span_ledger", None)
                        try:
                            self.kernel.record_submission_attempt(
                                command_id=command.command_id,
                                adapter_name=str(getattr(adapter, "name", "provider")),
                                allow_repeat=order.reduce_only,
                                plan_committed_ts_ns=(
                                    ledger.plan_committed_ts_ns if ledger is not None else 0
                                ),
                            )
                        except AmbiguousSubmissionAttemptError as exc:
                            # The journal transaction, not the stale preflight
                            # read, owns the single-winner guarantee under
                            # concurrency.
                            raise AmbiguousExposureSubmission(str(exc)) from exc
                    # The plan and the attempt claim must be power-loss durable
                    # before bytes can leave for the venue. On a write-behind
                    # journal this is the single place the order path waits for
                    # a disk sync; on a synchronous journal it is free.
                    barrier_wait_ns = durable_barrier()
                    normalized = self._after_wire_observations(
                        self._normalize_observations(
                            submit_effect(command, market)
                        ),
                        barrier_wait_ns=barrier_wait_ns,
                    )
            else:
                normalized = self._normalize_observations(
                    submit_effect(command, market)
                )
            if self._is_complete_synchronous(command, normalized):
                synchronous.append((command_index, command, normalized))
                continue
            flush_synchronous()
            events.extend(self.ingest(normalized))
        flush_synchronous()
        return tuple(events)

    def _record_leverage_wait(self, wait_ns: int) -> None:
        """Accumulate driver-side leverage negotiation time into the ledger."""

        ledger = getattr(self.kernel, "span_ledger", None)
        if ledger is None:
            return
        ledger.leverage_wait_ns = max(ledger.leverage_wait_ns, 0) + max(wait_ns, 0)
        ledger.leverage_done_ts_ns = self.kernel.clock.wall_time_ns()

    def _after_wire_observations(
        self,
        observations: tuple[ExecutionObservation, ...],
        *,
        barrier_wait_ns: int,
    ) -> tuple[ExecutionObservation, ...]:
        """Stamp post-barrier ACKs and record the batch's wire/ack extremes.

        The measured pre-wire barrier wait rides into each ACK's metadata
        beside the adapter's own ``local_socket_send_ts_ns``; the span ledger
        keeps the earliest send and the latest acknowledgement so the receipt
        brackets the whole batch's venue exchange.
        """

        ledger = getattr(self.kernel, "span_ledger", None)
        if ledger is None and barrier_wait_ns < 0:
            return observations
        stamped: list[ExecutionObservation] = []
        for observation in observations:
            if (
                ExecutionObservationType(observation.observation_type)
                is ExecutionObservationType.ACK
            ):
                metadata = (
                    observation.metadata
                    if isinstance(observation.metadata, Mapping)
                    else {}
                )
                if barrier_wait_ns >= 0:
                    observation = replace(
                        observation,
                        metadata={
                            **dict(metadata),
                            "barrier_wait_ns": int(barrier_wait_ns),
                        },
                    )
                if ledger is not None:
                    try:
                        send_ts_ns = int(metadata.get("local_socket_send_ts_ns") or 0)
                    except (TypeError, ValueError):
                        send_ts_ns = 0
                    if send_ts_ns > 0 and (
                        ledger.wire_sent_ts_ns == 0
                        or send_ts_ns < ledger.wire_sent_ts_ns
                    ):
                        ledger.wire_sent_ts_ns = send_ts_ns
                    if observation.local_receive_ts_ns > ledger.ack_received_ts_ns:
                        ledger.ack_received_ts_ns = int(observation.local_receive_ts_ns)
            stamped.append(observation)
        return tuple(stamped)

    def _submit_prepared_as_batch(
        self,
        result: TargetBatchResult,
        *,
        market_inputs: Mapping[str, MarketInputRef],
        adapter: Any,
        dispatch: Callable[[int, OrderCommand, tuple[ExecutionObservation, ...]], None],
        fused_command_ids: frozenset[str] = frozenset(),
        durable_barrier: Callable[[], int] | None = None,
    ) -> frozenset[str]:
        """Claim and send eligible commands as one venue batch.

        Returns the command ids this path fully handled. Engages only for an
        ambiguous provider implementing ``submit_prepared_batch`` with at
        least two claimable commands; everything else stays on the proven
        single-order path. One journal transaction claims every attempt and
        one barrier makes them durable, so an N-slice batch pays one commit,
        one disk sync, and one venue request instead of N of each — and a
        batch whose attempts were already fused into the plan commit
        (``fused_command_ids``) pays no claim transaction here at all.
        Chunks are partitioned by reduce-only because Bybit does not
        document mixing them in one batch, and exits go first.
        """

        submit_batch = getattr(adapter, "submit_prepared_batch", None)
        if not callable(submit_batch):
            return frozenset()
        if not bool(getattr(adapter, "submission_outcome_can_be_ambiguous", False)):
            return frozenset()
        max_unsubmitted_age_ns = getattr(adapter, "max_unsubmitted_exposure_age_ns", None)
        state_orders = self.kernel._state_ref().orders
        eligible: list[tuple[int, OrderCommand, MarketInputRef, bool]] = []
        for command_index, command in enumerate(result.commands):
            order = state_orders.get(command.command_id)
            if order is None or order.status != "commanded":
                continue
            fused_claim = command.command_id in fused_command_ids
            if order.submission_attempts > 0 and not fused_claim:
                # Re-attempts carry ambiguity semantics the single path owns.
                continue
            market = market_inputs.get(command.symbol)
            if market is None:
                continue
            eligible.append((command_index, command, market, fused_claim))
        if len(eligible) < 2:
            return frozenset()

        def fresh_only(
            rows: list[tuple[int, OrderCommand, MarketInputRef, bool]],
        ) -> list[tuple[int, OrderCommand, MarketInputRef, bool]]:
            """Drop stale entries instead of aborting the whole batch.

            The single path raises for a stale entry, which here would hold
            every sibling — fresh exits included — hostage to one dead
            command. Dropped commands fall to the single path, which still
            raises for them after the batch's commands are served. Fused
            commands are exempt exactly as the single path exempts attempted
            commands: their claim is already durable, and dropping one here
            would strand it claimed-but-unsent.
            """

            now_ns = self.kernel.clock.wall_time_ns()
            fresh: list[tuple[int, OrderCommand, MarketInputRef, bool]] = []
            for row in rows:
                command = row[1]
                if command.reduce_only or row[3]:
                    fresh.append(row)
                    continue
                if type(max_unsubmitted_age_ns) is not int or max_unsubmitted_age_ns <= 0:
                    raise ValueError(
                        "ambiguous provider adapter requires a positive integer "
                        "max_unsubmitted_exposure_age_ns"
                    )
                if (
                    command.created_ts_ns <= 0
                    or now_ns < command.created_ts_ns
                    or now_ns - command.created_ts_ns > max_unsubmitted_age_ns
                ):
                    continue
                fresh.append(row)
            return fresh

        eligible = fresh_only(eligible)
        if len(eligible) < 2:
            return frozenset()
        handled: set[str] = set()
        remaining: list[tuple[int, OrderCommand, MarketInputRef, bool]] = []
        prepare_submission = getattr(adapter, "prepare_submission", None)
        prepare_started_ns = self.kernel.clock.monotonic_ns()
        prepare_ran = False
        for command_index, command, market, fused_claim in eligible:
            if callable(prepare_submission) and not fused_claim:
                # Fused commands skip preparation: their leverage was proven
                # satisfied at commit time, so nothing is left to negotiate
                # and no venue call may sit between the claim and the wire.
                prepare_ran = True
                prepared_observations = self._normalize_observations(
                    prepare_submission(command, market)
                )
                if prepared_observations:
                    # A definite leverage reject: its acks flow, no create runs.
                    dispatch(command_index, command, prepared_observations)
                    handled.add(command.command_id)
                    continue
            remaining.append((command_index, command, market, fused_claim))
        leverage_wait_ns = -1
        if prepare_ran:
            leverage_wait_ns = max(
                self.kernel.clock.monotonic_ns() - prepare_started_ns, 0
            )
            self._record_leverage_wait(leverage_wait_ns)
        # Preparation was provider round trips (leverage negotiation), so
        # recheck freshness at the exposure boundary, exactly as the single
        # path does.
        remaining = fresh_only(remaining)
        if len(remaining) < 2:
            # Too few left to be worth a batch request; the single path,
            # which already owns one-command semantics, takes them.
            return frozenset(handled)
        unfused_claims = tuple(
            (command.command_id, command.reduce_only)
            for _, command, _, fused_claim in remaining
            if not fused_claim
        )
        if unfused_claims:
            ledger = getattr(self.kernel, "span_ledger", None)
            try:
                self.kernel.record_submission_attempt_batch(
                    claims=unfused_claims,
                    adapter_name=str(getattr(adapter, "name", "provider")),
                    plan_committed_ts_ns=(
                        ledger.plan_committed_ts_ns if ledger is not None else 0
                    ),
                    leverage_wait_ns=leverage_wait_ns,
                )
            except AmbiguousSubmissionAttemptError as exc:
                # The journal transaction, not the stale preflight read, owns
                # the single-winner guarantee under concurrency.
                raise AmbiguousExposureSubmission(str(exc)) from exc
        # One durable boundary for the whole batch, immediately before send.
        if durable_barrier is not None:
            barrier_wait_ns = durable_barrier()
        else:
            self.kernel.journal.barrier()
            barrier_wait_ns = -1
        reduces = [row for row in remaining if row[1].reduce_only]
        entries = [row for row in remaining if not row[1].reduce_only]
        for group in (reduces, entries):
            for chunk_start in range(0, len(group), 20):
                chunk = group[chunk_start : chunk_start + 20]
                if not chunk:
                    continue
                observations = self._after_wire_observations(
                    self._normalize_observations(
                        submit_batch([(command, market) for _, command, market, _ in chunk])
                    ),
                    barrier_wait_ns=barrier_wait_ns,
                )
                by_command: dict[str, list[ExecutionObservation]] = {}
                for observation in observations:
                    by_command.setdefault(observation.command_id, []).append(observation)
                # Dispatch this chunk's outcomes before the next chunk's
                # venue round trip: a later failure must not discard acks
                # whose side effects (quote registration, stop verification)
                # already ran inside the adapter.
                for command_index, command, _, _ in chunk:
                    handled.add(command.command_id)
                    normalized = tuple(by_command.get(command.command_id, ()))
                    if normalized:
                        dispatch(command_index, command, normalized)
                    # A command with no observations had an unknown
                    # individual outcome: it stays claimed and the
                    # reconciler's orderLinkId probes resolve it, exactly
                    # like a lost single-order answer.
        return frozenset(handled)

    @staticmethod
    def _normalize_observations(
        observations: Iterable[ExecutionObservation | Mapping[str, Any]],
    ) -> tuple[ExecutionObservation, ...]:
        normalized = [
            raw if isinstance(raw, ExecutionObservation) else ExecutionObservation(**raw)
            for raw in observations
        ]
        priority = {
            ExecutionObservationType.ACK: 0,
            ExecutionObservationType.FILL: 1,
            ExecutionObservationType.ORDER_STATUS: 2,
        }

        def modeled_fill_order(item: ExecutionObservation) -> tuple[int, int]:
            if ExecutionObservationType(item.observation_type) is not ExecutionObservationType.FILL:
                return (0, 0)
            metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
            raw_level = metadata.get("book_level")
            if type(raw_level) is int and raw_level >= 0:
                return (0, raw_level)
            # For non-twin observations with an exchange-timestamp tie, never
            # apply an explicitly terminal fill before another same-time fill.
            return (1 if bool(metadata.get("terminal")) else 0, 0)

        normalized.sort(key=lambda item: (
            priority[ExecutionObservationType(item.observation_type)],
            item.exchange_ts_ns,
            modeled_fill_order(item),
            item.execution_id,
        ))
        return tuple(normalized)

    @staticmethod
    def _is_complete_synchronous(
        command: OrderCommand,
        observations: tuple[ExecutionObservation, ...],
    ) -> bool:
        if not observations or any(
            observation.command_id != command.command_id
            for observation in observations
        ):
            return False
        acknowledgements = [
            observation
            for observation in observations
            if ExecutionObservationType(observation.observation_type)
            is ExecutionObservationType.ACK
        ]
        fills = [
            observation
            for observation in observations
            if ExecutionObservationType(observation.observation_type)
            is ExecutionObservationType.FILL
        ]
        statuses = [
            observation
            for observation in observations
            if ExecutionObservationType(observation.observation_type)
            is ExecutionObservationType.ORDER_STATUS
        ]
        if (
            len(acknowledgements) != 1
            or not acknowledgements[0].accepted
            or not fills
            or len(statuses) != 1
            or not all(fill.execution_id for fill in fills)
            or len({fill.execution_id for fill in fills}) != len(fills)
        ):
            return False
        status = statuses[0]
        if status.status.lower() not in {"filled", "partially_filled_cancelled"}:
            return False
        cumulative_fill = math.fsum(abs(fill.signed_qty) for fill in fills)
        tolerance = max(abs(command.signed_qty) * 1e-12, 1e-12)
        return abs(cumulative_fill - status.cumulative_filled_qty) <= tolerance

    def ingest(
        self,
        observations: Iterable[ExecutionObservation | Mapping[str, Any]],
    ) -> tuple[AccountEvent, ...]:
        events: list[AccountEvent] = []
        normalized = self._normalize_observations(observations)
        # One socket callback may contain ack/execution rows in arbitrary order.
        # Apply the causal ack first, then executions in exchange order.
        for observation in normalized:
            event_type = ExecutionObservationType(observation.observation_type)
            if event_type is ExecutionObservationType.ACK:
                order = self.kernel._state_ref().orders.get(observation.command_id)
                if order is None:
                    raise ValueError(f"ack references unknown command {observation.command_id}")
                if order.status == "commanded":
                    events.extend(self.kernel.record_ack(
                        command_id=observation.command_id,
                        accepted=observation.accepted,
                        venue_order_id=observation.venue_order_id,
                        exchange_ts_ns=observation.exchange_ts_ns,
                        local_ack_ts_ns=observation.local_receive_ts_ns,
                        rejection_key=observation.rejection_key,
                        metadata=observation.metadata,
                    ))
                elif order.status == "rejected" and observation.accepted:
                    raise ValueError(f"accepted ack contradicts rejected command {observation.command_id}")
            elif event_type is ExecutionObservationType.FILL:
                state = self.kernel._state_ref()
                if observation.execution_id in state.executions:
                    continue
                order = state.orders.get(observation.command_id)
                if order is None:
                    raise ValueError(f"fill references unknown command {observation.command_id}")
                if order.status == "commanded":
                    # A venue execution is stronger acceptance evidence than a
                    # missing/reordered create ack. Preserve canonical ordering by
                    # recording an explicit inferred Ack before the Fill.
                    events.extend(self.kernel.record_ack(
                        command_id=observation.command_id,
                        accepted=True,
                        venue_order_id=observation.venue_order_id,
                        exchange_ts_ns=observation.exchange_ts_ns,
                        local_ack_ts_ns=observation.local_receive_ts_ns,
                        metadata={"inferred_from_execution_id": observation.execution_id},
                    ))
                elif order.status == "rejected":
                    raise ValueError(f"fill contradicts rejected command {observation.command_id}")
                events.extend(self.kernel.record_fill(
                    command_id=observation.command_id,
                    execution_id=observation.execution_id,
                    signed_qty=observation.signed_qty,
                    price=observation.price,
                    fee_usdt=observation.fee_usdt,
                    exchange_ts_ns=observation.exchange_ts_ns,
                    local_receive_ts_ns=observation.local_receive_ts_ns,
                    metadata=observation.metadata,
                ))
                events.extend(self.kernel.finalize_flat_position(
                    symbol=order.symbol,
                    command_id=observation.command_id,
                    exchange_ts_ns=observation.exchange_ts_ns,
                    local_receive_ts_ns=observation.local_receive_ts_ns,
                ))
            elif event_type is ExecutionObservationType.ORDER_STATUS:
                order = self.kernel._state_ref().orders.get(observation.command_id)
                if order is None:
                    raise ValueError(f"order status references unknown command {observation.command_id}")
                tolerance = max(abs(order.signed_qty) * 1e-12, 1e-12)
                if observation.cumulative_filled_qty > abs(order.filled_signed_qty) + tolerance:
                    # Delivery fault reordered terminal status ahead of one or
                    # more executions. Execution WS/REST recovery will establish
                    # the fills first; do not manufacture them from cumExecQty.
                    continue
                if not order.terminal_status_recorded:
                    events.extend(self.kernel.record_order_status(
                        command_id=observation.command_id,
                        status=observation.status,
                        cumulative_filled_qty=observation.cumulative_filled_qty,
                        exchange_ts_ns=observation.exchange_ts_ns,
                        local_receive_ts_ns=observation.local_receive_ts_ns,
                        metadata=observation.metadata,
                    ))
                    events.extend(self.kernel.finalize_flat_position(
                        symbol=order.symbol,
                        command_id=observation.command_id,
                        exchange_ts_ns=observation.exchange_ts_ns,
                        local_receive_ts_ns=observation.local_receive_ts_ns,
                    ))
        return tuple(events)
