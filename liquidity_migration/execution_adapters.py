"""Execution ports for the account kernel and a deterministic market-order twin.

Historical and paper runs use :class:`MarketOrderExecutionTwin`.  Demo uses the
same :class:`KernelExecutionDriver` with :class:`BybitDemoExecutionAdapter`;
private WebSocket executions are normalized into the same observations.

The twin intentionally makes the replay-book limitation explicit: it walks the
observed book for this order but does not mutate future historical snapshots.
That is a replay assumption, not a claim that our order had no market impact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .account_kernel import (
    AccountEvent,
    AccountExecutionKernel,
    InstrumentRules,
    MarketInputRef,
    OrderCommand,
    TargetBatchResult,
)
from .deterministic_runtime import Clock, DeterministicIds, SystemClock
from .bybit import BybitRequestRejected


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

    def market_ref(self, *, input_key: str, source: str = "bybit_l2") -> MarketInputRef:
        if not self.bids or not self.asks:
            raise ValueError("a market reference requires non-empty bids and asks")
        return MarketInputRef(
            input_key=input_key,
            symbol=self.symbol,
            exchange_ts_ns=self.exchange_ts_ns,
            local_receive_ts_ns=self.local_receive_ts_ns,
            reference_price=(self.bids[0].price + self.asks[0].price) / 2.0,
            bid_price=self.bids[0].price,
            ask_price=self.asks[0].price,
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
    fill_spacing_ns: int = 1

    def __post_init__(self) -> None:
        if min(
            self.decision_to_socket_ns,
            self.order_entry_ns,
            self.order_response_ns,
            self.fill_spacing_ns,
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
    immutable_replay_book: bool = True
    residual_adverse_slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.fee_bps) or self.fee_bps < 0.0:
            raise ValueError("fee_bps must be finite and non-negative")
        if self.max_decision_age_ns < 0:
            raise ValueError("max_decision_age_ns cannot be negative")
        if self.rate_limit_orders < 0 or self.rate_limit_window_ns <= 0:
            raise ValueError("execution-twin rate limits are invalid")
        if not math.isfinite(self.residual_adverse_slippage_bps):
            raise ValueError("residual adverse slippage must be finite")
        if self.residual_adverse_slippage_bps <= -10_000.0:
            raise ValueError("residual adverse slippage would make a fill non-positive")


def _aligned(qty: float, step: float, *, tolerance: float = 1e-12) -> bool:
    if step <= 0.0:
        return False
    units = Decimal(str(abs(qty))) / Decimal(str(step))
    return abs(float(units) - round(float(units))) <= tolerance


class MarketOrderExecutionTwin:
    """Deterministic market-order book walker for historical and paper modes."""

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
    ) -> tuple[ExecutionObservation, ...]:
        return (ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=exchange_ts_ns,
            local_receive_ts_ns=local_receive_ts_ns,
            accepted=False,
            rejection_key=f"execution:{command.command_id}:{reason}",
            metadata={"reason": reason, "local_socket_send_ts_ns": send_ts_ns},
        ),)

    def submit(
        self,
        command: OrderCommand,
        market_input: MarketInputRef,
    ) -> Iterable[Mapping[str, Any] | ExecutionObservation]:
        symbol = command.symbol.upper()
        book = self.books.get(symbol)
        rules = self.instrument_rules.get(symbol)
        send_ts_ns = market_input.local_receive_ts_ns + self.config.latency.decision_to_socket_ns
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
        if book.sequence_gap:
            return self._rejection(
                command,
                reason="book_sequence_gap",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
            )
        decision_age_ns = send_ts_ns - market_input.local_receive_ts_ns
        if decision_age_ns > self.config.max_decision_age_ns:
            return self._rejection(
                command,
                reason="stale_decision",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
            )
        if command.qty < rules.min_qty or not _aligned(command.qty, rules.qty_step):
            return self._rejection(
                command,
                reason="quantity_rule",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
            )
        if not command.reduce_only and command.qty * command.reference_price < rules.min_notional:
            return self._rejection(
                command,
                reason="minimum_notional",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
            )
        if rules.max_order_qty > 0.0 and command.qty > rules.max_order_qty:
            return self._rejection(
                command,
                reason="maximum_order_quantity",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
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
                )
            self._send_times_ns.append(send_ts_ns)

        levels = book.asks if command.signed_qty > 0.0 else book.bids
        available = math.fsum(level.qty for level in levels if level.qty > 0.0 and level.price > 0.0)
        if available <= 0.0:
            return self._rejection(
                command,
                reason="no_liquidity",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
            )
        if available < command.qty and not self.config.allow_partial_fills:
            return self._rejection(
                command,
                reason="insufficient_depth",
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                send_ts_ns=send_ts_ns,
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
                "order_entry_latency_ns": self.config.latency.order_entry_ns,
                "order_response_latency_ns": self.config.latency.order_response_ns,
                "immutable_replay_book": self.config.immutable_replay_book,
            },
        )]
        remaining = command.qty
        executable = min(command.qty, available)
        fill_count = 0
        for level in levels:
            if remaining <= 1e-12 or level.qty <= 0.0 or level.price <= 0.0:
                continue
            fill_qty = min(remaining, level.qty)
            remaining -= fill_qty
            fill_count += 1
            signed_qty = math.copysign(fill_qty, command.signed_qty)
            exchange_fill_ts_ns = exchange_ack_ts_ns + fill_count * self.config.latency.fill_spacing_ns
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
                local_receive_ts_ns=exchange_fill_ts_ns + self.config.latency.order_response_ns,
                venue_order_id=venue_order_id,
                execution_id=self.ids.make("execution", command.command_id, fill_count),
                signed_qty=signed_qty,
                price=fill_price,
                fee_usdt=abs(fill_qty * fill_price) * self.config.fee_bps / 10_000.0,
                metadata={
                    "book_sequence": book.sequence,
                    "book_level": fill_count - 1,
                    "visible_book_price": level.price,
                    "residual_adverse_slippage_bps": self.config.residual_adverse_slippage_bps,
                    "terminal": terminal,
                    "unfilled_cancelled_qty": max(command.qty - executable, 0.0) if terminal else 0.0,
                    "immutable_replay_book": self.config.immutable_replay_book,
                    "fee_observed": True,
                    "fee_status": "modeled_execution_fee",
                    "fee_source": "execution_twin_config.fee_bps",
                    "source": "execution_twin_l2_fill",
                },
            ))
        observations.append(ExecutionObservation(
            observation_type=ExecutionObservationType.ORDER_STATUS,
            command_id=command.command_id,
            exchange_ts_ns=exchange_ack_ts_ns + (fill_count + 1) * self.config.latency.fill_spacing_ns,
            local_receive_ts_ns=(
                exchange_ack_ts_ns
                + (fill_count + 1) * self.config.latency.fill_spacing_ns
                + self.config.latency.order_response_ns
            ),
            venue_order_id=venue_order_id,
            status="filled" if executable >= command.qty - 1e-12 else "partially_filled_cancelled",
            cumulative_filled_qty=executable,
            metadata={"source": "execution_twin_order_status"},
        ))
        return tuple(observations)


class BybitDemoExecutionAdapter:
    """Thin, demo-only Bybit command adapter.

    Submission yields the create acknowledgement only.  Actual executions must
    arrive through the private execution stream and be passed to
    :meth:`KernelExecutionDriver.ingest`; the adapter never invents fills from a
    successful create response.
    """

    name = "bybit_demo"

    def __init__(self, client: Any, *, clock: Clock | None = None) -> None:
        if not bool(getattr(client, "demo", False)):
            raise ValueError("BybitDemoExecutionAdapter requires a demo client; mainnet is forbidden")
        self.client = client
        self.clock = clock or SystemClock()

    def submit(self, command: OrderCommand, market_input: MarketInputRef) -> Iterable[ExecutionObservation]:
        params = {
            "symbol": command.symbol,
            "side": command.side,
            "orderType": "Market",
            "qty": format(Decimal(str(command.qty)), "f"),
            "orderLinkId": command.command_id,
            "reduceOnly": command.reduce_only,
        }
        if not command.reduce_only:
            try:
                self.client.set_leverage(
                    symbol=command.symbol,
                    buy_leverage=command.leverage,
                    sell_leverage=command.leverage,
                )
            except BybitRequestRejected as exc:
                local_ack_ts_ns = self.clock.wall_time_ns()
                return (ExecutionObservation(
                    observation_type=ExecutionObservationType.ACK,
                    command_id=command.command_id,
                    exchange_ts_ns=0,
                    local_receive_ts_ns=local_ack_ts_ns,
                    accepted=False,
                    rejection_key=f"bybit-demo:{command.command_id}:set_leverage_failed",
                    metadata={
                        "local_socket_send_ts_ns": 0,
                        "exchange_ack_ts_status": "unavailable",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                        "requested_leverage": command.leverage,
                        "submission_phase": "set_leverage",
                    },
                ),)
        # Measure the create-order request itself. Entry leverage negotiation is
        # intentionally outside request/ack RTT but remains inside the broader
        # command-decision-to-socket delay.
        send_ts_ns = self.clock.wall_time_ns()
        try:
            result = self.client.place_order(**params)
        except BybitRequestRejected as exc:
            local_ack_ts_ns = self.clock.wall_time_ns()
            return (ExecutionObservation(
                observation_type=ExecutionObservationType.ACK,
                command_id=command.command_id,
                exchange_ts_ns=0,
                local_receive_ts_ns=local_ack_ts_ns,
                accepted=False,
                rejection_key=f"bybit-demo:{command.command_id}:place_order_failed",
                metadata={
                    "local_socket_send_ts_ns": send_ts_ns,
                    "exchange_ack_ts_status": "unavailable",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                    "requested_leverage": command.leverage,
                },
            ),)
        # Transport failures and duplicate-link visibility races are ambiguous:
        # the venue may already own this command. Let the service release the
        # request while the command remains ``commanded``. REST reconciliation
        # queries the same orderLinkId before any safe idempotent retry.
        local_ack_ts_ns = self.clock.wall_time_ns()
        idempotent_existing_order = bool(result.get("_idempotent_existing_order"))
        exchange_ack_ms = 0
        if not idempotent_existing_order:
            exchange_ack_ms = result.get("_response_time_ms") or result.get("time") or 0
        try:
            exchange_ack_ts_ns = int(float(exchange_ack_ms) * 1_000_000)
        except (TypeError, ValueError):
            exchange_ack_ts_ns = 0
        return (ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=exchange_ack_ts_ns,
            local_receive_ts_ns=local_ack_ts_ns,
            accepted=True,
            venue_order_id=str(result.get("orderId") or ""),
            metadata={
                "local_socket_send_ts_ns": send_ts_ns,
                "exchange_ack_ts_status": "observed" if exchange_ack_ts_ns else "unavailable",
                "exchange_ack_ts_source": (
                    "bybit_v5_response_envelope_time"
                    if exchange_ack_ts_ns
                    else "unavailable"
                ),
                "idempotent_existing_order": idempotent_existing_order,
                "requested_leverage": command.leverage,
            },
        ),)


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

        for command_index, command in enumerate(result.commands):
            order = self.kernel._state_ref().orders.get(command.command_id)
            if order is None:
                raise ValueError(f"unknown kernel command {command.command_id}")
            if order.status != "commanded":
                # Crash replay: an already acknowledged/filled/rejected command
                # must not be submitted again. A commanded order is safe to retry
                # because the venue adapter uses command_id as its idempotency key.
                continue
            market = market_inputs.get(command.symbol)
            if market is None:
                raise ValueError(f"missing market input for command symbol {command.symbol}")
            normalized = self._normalize_observations(adapter.submit(command, market))
            if self._is_complete_synchronous(command, normalized):
                synchronous.append((command_index, command, normalized))
                continue
            flush_synchronous()
            events.extend(self.ingest(normalized))
        flush_synchronous()
        return tuple(events)

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
        normalized.sort(key=lambda item: (
            priority[ExecutionObservationType(item.observation_type)],
            item.exchange_ts_ns,
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
                status_recorded = order.terminal_status_recorded
                if not status_recorded:
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
