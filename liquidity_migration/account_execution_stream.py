"""Normalize Bybit private execution/order streams into account-kernel facts."""

from __future__ import annotations

import logging
import math
import queue
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from .account_kernel import (
    AccountEventType,
    AccountExecutionKernel,
    AccountState,
    AccountTransitionError,
    read_account_journal,
)
from .deterministic_runtime import Clock, SystemClock
from .execution_adapters import ExecutionObservation, ExecutionObservationType, KernelExecutionDriver

_logger = logging.getLogger(__name__)


def _rows(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = message.get("data") or []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _float(value: Any) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return 0.0
    return output if math.isfinite(output) else 0.0


def _timestamp_ns(value: Any) -> int:
    numeric = _float(value)
    return int(numeric * 1_000_000) if numeric > 0.0 else 0


def _terminal_status(row: Mapping[str, Any]) -> str:
    raw = str(row.get("orderStatus") or row.get("order_status") or "").lower().replace("_", "")
    cumulative = _float(row.get("cumExecQty") or row.get("cum_exec_qty"))
    if raw in {"partiallyfilledcanceled", "partiallyfilledcancelled"}:
        return "partially_filled_cancelled"
    if raw in {"cancelled", "canceled", "deactivated"}:
        return "partially_filled_cancelled" if cumulative > 0.0 else "cancelled"
    if raw == "rejected":
        return "rejected"
    if raw == "filled":
        return "filled"
    return ""


def _command_id_for_row(row: Mapping[str, Any], state: AccountState) -> str:
    """Resolve Bybit rows by either client id or the venue's durable order id.

    Exchange-created position TP/SL orders have an empty ``orderLinkId``.  Once
    the first verified native fill is adopted, its synthetic kernel command
    retains ``orderId`` in the ACK, so every later execution/order row must be
    joined on that venue identity instead of being treated as another unknown
    external order.
    """

    command_id = str(row.get("orderLinkId") or row.get("order_link_id") or "")
    if command_id in state.orders:
        return command_id
    venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
    if not venue_order_id:
        return command_id
    matches = [
        order.command_id
        for order in state.orders.values()
        if order.venue_order_id == venue_order_id
    ]
    return matches[0] if len(matches) == 1 else command_id


@dataclass(frozen=True, slots=True)
class PendingTerminalStatus:
    command_id: str
    status: str
    cumulative_filled_qty: float
    exchange_ts_ns: int
    local_receive_ts_ns: int
    rejection_key: str
    metadata: Mapping[str, Any]


class BybitAccountExecutionConsumer:
    """One consumer thread owns all private-stream mutation of the kernel."""

    def __init__(
        self,
        *,
        kernel: AccountExecutionKernel,
        private_stream: Any | None = None,
        native_protection_manager: Any | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.kernel = kernel
        self.driver = KernelExecutionDriver(kernel)
        self.private_stream = private_stream
        self.native_protection_manager = native_protection_manager
        self.clock = clock or SystemClock()
        self.events: queue.Queue[tuple[str, Mapping[str, Any], int]] = queue.Queue()
        self.pending_terminal: dict[str, PendingTerminalStatus] = {}
        self.pending_native_terminal: dict[str, PendingTerminalStatus] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._terminal_recorded = {
            str(event.payload.get("command_id") or "") + ":" + str(event.payload.get("status") or "")
            for event in read_account_journal(kernel.journal.root)
            if event.event_type == AccountEventType.ORDER_STATUS.value
        }

    def _enqueue(self, kind: str, message: Mapping[str, Any]) -> None:
        self.events.put((kind, message, self.clock.wall_time_ns()))

    def start(self) -> None:
        if self.private_stream is None:
            raise RuntimeError("private stream is required")
        if self._thread is not None and self._thread.is_alive():
            return
        self.private_stream.subscribe_executions(lambda message: self._enqueue("execution", message))
        self.private_stream.subscribe_orders(lambda message: self._enqueue("order", message))
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="account-execution-consumer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                kind, message, local_ns = self.events.get(timeout=0.25)
            except queue.Empty:
                for command_id in list(self.pending_terminal):
                    self._flush_terminal(command_id)
                continue
            try:
                self.handle(kind, message, local_receive_ts_ns=local_ns)
            except Exception:  # noqa: BLE001 - persist daemon; error remains visible in logs
                _logger.exception("account execution stream event failed kind=%s", kind)

    def handle(self, kind: str, message: Mapping[str, Any], *, local_receive_ts_ns: int) -> None:
        if kind == "execution":
            self.on_execution(message, local_receive_ts_ns=local_receive_ts_ns)
        elif kind == "order":
            self.on_order(message, local_receive_ts_ns=local_receive_ts_ns)

    def on_execution(self, message: Mapping[str, Any], *, local_receive_ts_ns: int) -> None:
        observations: list[ExecutionObservation] = []
        for row in _rows(message):
            # External/native adoption mutates the kernel immediately. Refresh
            # per row so multiple fills for the same venue order in one Bybit
            # message join the synthetic command created by the first fill.
            state = self.kernel.state()
            command_id = _command_id_for_row(row, state)
            if command_id not in state.orders:
                if self.native_protection_manager is not None:
                    if not self.native_protection_manager.is_position_execution(row):
                        continue
                    try:
                        self.native_protection_manager.adopt_execution(
                            row,
                            local_receive_ts_ns=local_receive_ts_ns,
                        )
                    except AccountTransitionError as exc:
                        # Unknown/manual reductions must remain visible as a
                        # reconciliation failure; never relabel them as the
                        # account-owned stop just because one is also active.
                        _logger.error("unadopted external execution: %s", exc)
                        self.native_protection_manager.note_adoption_failure(row, exc)
                    venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
                    adopted_state = self.kernel.state()
                    adopted_command_id = _command_id_for_row(row, adopted_state)
                    pending = self.pending_native_terminal.get(venue_order_id)
                    if pending is not None and adopted_command_id in adopted_state.orders:
                        self.pending_native_terminal.pop(venue_order_id, None)
                        self.pending_terminal[adopted_command_id] = PendingTerminalStatus(
                            command_id=adopted_command_id,
                            status=pending.status,
                            cumulative_filled_qty=pending.cumulative_filled_qty,
                            exchange_ts_ns=pending.exchange_ts_ns,
                            local_receive_ts_ns=pending.local_receive_ts_ns,
                            rejection_key=pending.rejection_key,
                            metadata=pending.metadata,
                        )
                        self._flush_terminal(adopted_command_id)
                continue
            side = str(row.get("side") or "").lower()
            qty = _float(row.get("execQty") or row.get("exec_qty"))
            signed_qty = qty if side == "buy" else -qty if side == "sell" else 0.0
            execution_id = str(row.get("execId") or row.get("exec_id") or "")
            price = _float(row.get("execPrice") or row.get("exec_price"))
            if not execution_id or signed_qty == 0.0 or price <= 0.0:
                continue
            fee_observed = row.get("execFee") not in (None, "") or row.get(
                "exec_fee"
            ) not in (None, "")
            observations.append(ExecutionObservation(
                observation_type=ExecutionObservationType.FILL,
                command_id=command_id,
                exchange_ts_ns=_timestamp_ns(row.get("execTime") or row.get("exec_time")),
                local_receive_ts_ns=local_receive_ts_ns,
                venue_order_id=str(row.get("orderId") or row.get("order_id") or ""),
                execution_id=execution_id,
                signed_qty=signed_qty,
                price=price,
                fee_usdt=_float(row.get("execFee") or row.get("exec_fee")),
                metadata={
                    "cross_sequence": int(_float(row.get("seq"))),
                    # Missing execFee is not evidence of a zero fee.  The
                    # reducer still needs a numeric placeholder, so preserve
                    # explicit unresolved provenance beside it.
                    "fee_observed": fee_observed,
                    "fee_status": (
                        "observed_execution_fee"
                        if fee_observed
                        else "pending_missing_execution_fee"
                    ),
                    "fee_source": "bybit_private_execution.execFee",
                    "source": "bybit_private_execution_ws",
                },
            ))
        if observations:
            self.driver.ingest(observations)
            if self.native_protection_manager is not None:
                completion_observations: dict[str, ExecutionObservation] = {}
                for observation in observations:
                    prior = completion_observations.get(observation.command_id)
                    if prior is None or (
                        observation.exchange_ts_ns,
                        observation.local_receive_ts_ns,
                        observation.execution_id,
                    ) > (
                        prior.exchange_ts_ns,
                        prior.local_receive_ts_ns,
                        prior.execution_id,
                    ):
                        completion_observations[observation.command_id] = observation
                for observation in completion_observations.values():
                    observe_progress = getattr(
                        self.native_protection_manager,
                        "observe_adopted_fill_progress",
                        None,
                    )
                    if callable(observe_progress):
                        observe_progress(
                            command_id=observation.command_id,
                            exchange_ts_ns=observation.exchange_ts_ns,
                            local_receive_ts_ns=observation.local_receive_ts_ns,
                        )
                updated = self.kernel.state()
                self.native_protection_manager.sync_symbols([
                    updated.orders[observation.command_id].symbol
                    for observation in observations
                    if observation.command_id in updated.orders
                ])
            for command_id in {observation.command_id for observation in observations}:
                self._flush_terminal(command_id)

    def on_order(self, message: Mapping[str, Any], *, local_receive_ts_ns: int) -> None:
        for row in _rows(message):
            state = self.kernel.state()
            command_id = _command_id_for_row(row, state)
            order = state.orders.get(command_id)
            if order is None:
                if (
                    self.native_protection_manager is not None
                    and self.native_protection_manager.observe_order(row)
                ):
                    status = _terminal_status(row)
                    venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
                    cumulative = _float(row.get("cumExecQty") or row.get("cum_exec_qty"))
                    if status and venue_order_id and cumulative > 0.0:
                        self.pending_native_terminal[venue_order_id] = self._terminal(
                            row,
                            command_id="",
                            status=status,
                            local_receive_ts_ns=local_receive_ts_ns,
                            message=message,
                        )
                continue
            status = _terminal_status(row)
            if not status:
                continue
            terminal = self._terminal(
                row,
                command_id=command_id,
                status=status,
                local_receive_ts_ns=local_receive_ts_ns,
                message=message,
            )
            self.pending_terminal[command_id] = terminal
            self._flush_terminal(command_id)

    @staticmethod
    def _terminal(
        row: Mapping[str, Any],
        *,
        command_id: str,
        status: str,
        local_receive_ts_ns: int,
        message: Mapping[str, Any],
    ) -> PendingTerminalStatus:
        return PendingTerminalStatus(
            command_id=command_id,
            status=status,
            cumulative_filled_qty=_float(row.get("cumExecQty") or row.get("cum_exec_qty")),
            exchange_ts_ns=_timestamp_ns(
                row.get("updatedTime") or row.get("updated_time") or message.get("creationTime")
            ),
            local_receive_ts_ns=local_receive_ts_ns,
            rejection_key=(
                f"bybit-order:{command_id}:{row.get('rejectReason')}"
                if status == "rejected"
                else ""
            ),
            metadata={
                "order_status": str(row.get("orderStatus") or ""),
                "cancel_type": str(row.get("cancelType") or ""),
                "reject_reason": str(row.get("rejectReason") or ""),
                "venue_order_id": str(row.get("orderId") or row.get("order_id") or ""),
                "create_type": str(row.get("createType") or row.get("create_type") or ""),
                "stop_order_type": str(
                    row.get("stopOrderType") or row.get("stop_order_type") or ""
                ),
                "source": "bybit_private_order_ws",
            },
        )

    def _flush_terminal(self, command_id: str) -> None:
        terminal = self.pending_terminal.get(command_id)
        if terminal is None:
            return
        state = self.kernel.state()
        order = state.orders.get(command_id)
        if order is None:
            self.pending_terminal.pop(command_id, None)
            return
        reconstructed = abs(order.filled_signed_qty)
        tolerance = max(abs(order.signed_qty) * 1e-12, 1e-12)
        if order.status == "commanded":
            # A terminal venue row is stronger evidence than a missing create
            # response. Infer the acknowledgement so an ambiguous submission
            # cannot remain working forever. A clean Rejected row is a
            # definite negative acknowledgement; cancellation/fill states prove
            # prior acceptance. Rows claiming rejected-with-fills are treated as
            # accepted and wait for their executions before terminal handling.
            accepted = terminal.status != "rejected" or terminal.cumulative_filled_qty > tolerance
            self.kernel.record_ack(
                command_id=command_id,
                accepted=accepted,
                venue_order_id=str(terminal.metadata.get("venue_order_id") or ""),
                exchange_ts_ns=terminal.exchange_ts_ns,
                local_ack_ts_ns=terminal.local_receive_ts_ns,
                rejection_key=terminal.rejection_key if not accepted else "",
                metadata={
                    "inferred_from_terminal_status": terminal.status,
                    "source": terminal.metadata.get("source"),
                },
            )
            order = self.kernel.state().orders[command_id]
        if terminal.cumulative_filled_qty > reconstructed + tolerance:
            # Order WS raced ahead of execution WS. Wait for the missing fills;
            # a REST execution reconciliation can feed the same on_execution path.
            return
        key = f"{command_id}:{terminal.status}"
        if key not in self._terminal_recorded:
            self._terminal_recorded.update(
                str(event.payload.get("command_id") or "") + ":" + str(event.payload.get("status") or "")
                for event in read_account_journal(self.kernel.journal.root)
                if event.event_type == AccountEventType.ORDER_STATUS.value
            )
        if key in self._terminal_recorded:
            self.pending_terminal.pop(command_id, None)
            return
        if terminal.status == "filled" and reconstructed + tolerance < abs(order.signed_qty):
            return
        self.kernel.record_order_status(
            command_id=command_id,
            status=terminal.status,
            cumulative_filled_qty=terminal.cumulative_filled_qty,
            exchange_ts_ns=terminal.exchange_ts_ns,
            local_receive_ts_ns=terminal.local_receive_ts_ns,
            rejection_key=terminal.rejection_key,
            metadata=terminal.metadata,
        )
        # A partially-filled IOC is not final until its cancellation removes
        # the unfilled remainder. Full fills are finalized from the execution
        # path; the kernel call is idempotent for either delivery order.
        self.kernel.finalize_flat_position(
            symbol=order.symbol,
            command_id=command_id,
            exchange_ts_ns=terminal.exchange_ts_ns,
            local_receive_ts_ns=terminal.local_receive_ts_ns,
        )
        if self.native_protection_manager is not None:
            observe_terminal = getattr(
                self.native_protection_manager,
                "observe_terminal_status",
                None,
            )
            if callable(observe_terminal):
                observe_terminal(command_id=command_id, status=terminal.status)
        self._terminal_recorded.add(key)
        self.pending_terminal.pop(command_id, None)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self.private_stream is not None:
            close = getattr(self.private_stream, "close", None)
            if callable(close):
                close()
