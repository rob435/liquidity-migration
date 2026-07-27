"""Normalize Bybit private execution/order streams into account-kernel facts."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .account_contracts import (
    AccountEventType,
    AccountState,
    AccountTransitionError,
    PositionState,
    quantity_tolerance,
)
from .account_kernel import AccountExecutionKernel
from .bybit_execution_adapter import bybit_private_execution_metadata
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

    venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
    if venue_order_id:
        matches = [
            order.command_id
            for order in state.orders.values()
            if order.venue_order_id == venue_order_id
        ]
        if len(matches) == 1:
            return matches[0]
    command_id = str(row.get("orderLinkId") or row.get("order_link_id") or "")
    return command_id if command_id in state.orders else ""


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
        fill_observer: Callable[[str], Any] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.kernel = kernel
        self.driver = KernelExecutionDriver(kernel)
        self.private_stream = private_stream
        self.native_protection_manager = native_protection_manager
        self.fill_observer = fill_observer
        self.clock = clock or SystemClock()
        self.events: queue.Queue[tuple[str, Mapping[str, Any], int]] = queue.Queue()
        self.pending_terminal: dict[str, PendingTerminalStatus] = {}
        self.pending_native_terminal: dict[str, PendingTerminalStatus] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream_lock = threading.RLock()
        self._closed = False
        self._terminal_recorded: set[str] = set()
        # Highest journal sequence already folded into `_terminal_recorded`.
        # Rescanning the whole journal on every new terminal AND on every 0.25 s
        # blocked retry was O(journal) per pass on the owner hot path
        # (2026-07-27 audit L14); only the delta since this mark can contain a
        # row we have not seen.
        self._terminal_scanned_sequence = 0
        self._absorb_recorded_terminals()

    def _absorb_recorded_terminals(self) -> None:
        """Fold ORDER_STATUS rows committed since the last scan into the index.

        Journal verification guarantees contiguous 1-based sequences, so the
        already-scanned prefix can be skipped by slicing the owner-internal
        snapshot rather than re-walking a full copy of the journal.
        """

        events, _state = self.kernel._snapshot_ref()
        if len(events) <= self._terminal_scanned_sequence:
            return
        for event in events[self._terminal_scanned_sequence :]:
            if event.event_type == AccountEventType.ORDER_STATUS.value:
                self._terminal_recorded.add(
                    str(event.payload.get("command_id") or "")
                    + ":"
                    + str(event.payload.get("status") or "")
                )
        self._terminal_scanned_sequence = len(events)

    def _notify_committed_fill(self, execution_id: str) -> None:
        if self.fill_observer is None or not execution_id:
            return
        try:
            self.fill_observer(execution_id)
        except Exception:  # noqa: BLE001 - diagnostics cannot block fill ownership
            _logger.exception(
                "post-fill observer failed after canonical execution=%s",
                execution_id,
            )

    def _enqueue(self, kind: str, message: Mapping[str, Any]) -> None:
        self.events.put((kind, message, self.clock.wall_time_ns()))

    def _subscribe(self, private_stream: Any) -> None:
        private_stream.subscribe_executions(lambda message: self._enqueue("execution", message))
        private_stream.subscribe_orders(lambda message: self._enqueue("order", message))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._stream_lock:
            if self.private_stream is None:
                raise RuntimeError("private stream is required")
            self._closed = False
            self._subscribe(self.private_stream)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="account-execution-consumer", daemon=True)
        self._thread.start()

    def prepare_private_stream(self, private_stream: Any) -> None:
        """Subscribe a candidate without changing the currently published stream."""

        if private_stream is None:
            raise RuntimeError("replacement private stream is required")
        with self._stream_lock:
            if self._closed:
                raise RuntimeError("execution consumer is closed")
            if private_stream is self.private_stream:
                raise RuntimeError("replacement private stream must be a new instance")
        # Provider construction/subscription may perform network I/O.  Keep it
        # outside the publication lock so owner-loop health checks and shutdown
        # remain responsive while a replacement handshake is in flight.
        self._subscribe(private_stream)

    def publish_private_stream(
        self,
        private_stream: Any,
        *,
        expected_current: Any,
    ) -> None:
        """Atomically publish a ready candidate and retire its exact predecessor."""

        with self._stream_lock:
            if self._closed:
                raise RuntimeError("execution consumer is closed")
            current = self.private_stream
            if current is not expected_current:
                raise RuntimeError("private stream changed during replacement handshake")
            self.private_stream = private_stream
        if current is not None:
            close = getattr(current, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - replacement is already subscribed and published
                    _logger.warning("retired private execution stream close failed", exc_info=True)

    def current_private_stream(self) -> Any | None:
        with self._stream_lock:
            return self.private_stream

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
        external_rows: list[Mapping[str, Any]] = []
        message_creation_ts_ns = _timestamp_ns(message.get("creationTime") or message.get("creation_time"))
        for row in _rows(message):
            state = self.kernel._state_ref()
            command_id = _command_id_for_row(row, state)
            venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
            venue_identity_bound = bool(
                command_id in state.orders
                and venue_order_id
                and state.orders[command_id].venue_order_id == venue_order_id
            )
            if (
                self.native_protection_manager is not None
                and self.native_protection_manager.is_position_execution(row)
                and not venue_identity_bound
                and (
                    self.native_protection_manager.has_native_stop_provenance(row)
                    or self.native_protection_manager.native_execution_identity_evidence(row)
                )
            ):
                # Exchange-created stop children normally have an empty client
                # id, but a provider payload may echo its parent orderLinkId.
                # Native provenance must win before ordinary command lookup or
                # the reduction could be misapplied as another entry fill.
                external_rows.append(row)
                continue
            if command_id not in state.orders:
                if self.native_protection_manager is not None and self.native_protection_manager.is_position_execution(
                    row
                ):
                    external_rows.append(row)
                continue
            side = str(row.get("side") or "").lower()
            qty = _float(row.get("execQty") or row.get("exec_qty"))
            signed_qty = qty if side == "buy" else -qty if side == "sell" else 0.0
            execution_id = str(row.get("execId") or row.get("exec_id") or "")
            price = _float(row.get("execPrice") or row.get("exec_price"))
            if not execution_id or signed_qty == 0.0 or price <= 0.0:
                # A malformed execution row for a KNOWN command is missing
                # accounting, not noise: silently dropping it lets the kernel
                # position diverge from the venue with no causal record. Latch
                # it as an adoption failure so owner health blocks until the
                # row parses or reconciliation resolves the divergence.
                if execution_id and self.native_protection_manager is not None:
                    malformed = AccountTransitionError(
                        f"known-command execution row is malformed: "
                        f"command={command_id} side={side!r} qty={qty!r} price={price!r}"
                    )
                    _logger.error("dropped malformed execution row: %s", malformed)
                    self.native_protection_manager.note_adoption_failure(row, malformed)
                continue
            observations.append(
                ExecutionObservation(
                    observation_type=ExecutionObservationType.FILL,
                    command_id=command_id,
                    exchange_ts_ns=_timestamp_ns(row.get("execTime") or row.get("exec_time")),
                    local_receive_ts_ns=local_receive_ts_ns,
                    venue_order_id=str(row.get("orderId") or row.get("order_id") or ""),
                    execution_id=execution_id,
                    signed_qty=signed_qty,
                    price=price,
                    fee_usdt=_float(row.get("execFee") or row.get("exec_fee")),
                    metadata=bybit_private_execution_metadata(
                        row,
                        message_creation_ts_ns=message_creation_ts_ns,
                    ),
                )
            )
        if observations:
            committed_events = self.driver.ingest(observations)
            for event in committed_events:
                if event.event_type == AccountEventType.FILL.value:
                    self._notify_committed_fill(str(event.payload.get("execution_id") or ""))
        # Bybit may coalesce an entry fill and its immediately-triggered
        # attached stop into one message, in either row order. Commit every
        # known entry fill first so the stop is evaluated against the real
        # reconstructed position instead of being rejected as a reduction of
        # local flat. External rows remain sequential so partial fills join the
        # synthetic command created by the first row.
        if self.native_protection_manager is not None:
            for row in external_rows:
                adopted_events: tuple[Any, ...] = ()
                try:
                    adopted_events = self.native_protection_manager.adopt_execution(
                        row,
                        local_receive_ts_ns=local_receive_ts_ns,
                        message_creation_ts_ns=message_creation_ts_ns,
                    )
                except AccountTransitionError as exc:
                    # Unknown/manual reductions must remain visible as a
                    # reconciliation failure; never relabel them as the
                    # account-owned stop just because one is also active.
                    _logger.error("unadopted external execution: %s", exc)
                    self.native_protection_manager.note_adoption_failure(row, exc)
                for event in adopted_events:
                    if event.event_type == AccountEventType.FILL.value:
                        self._notify_committed_fill(str(event.payload.get("execution_id") or ""))
                venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
                adopted_state = self.kernel._state_ref()
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

            if observations:
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
                updated = self.kernel._state_ref()
                self.native_protection_manager.sync_symbols(
                    [
                        updated.orders[observation.command_id].symbol
                        for observation in observations
                        if observation.command_id in updated.orders
                        and abs(
                            updated.positions.get(
                                updated.orders[observation.command_id].symbol,
                                PositionState(),
                            ).signed_qty
                        )
                        > 1e-12
                    ]
                )
        if observations:
            for command_id in {observation.command_id for observation in observations}:
                self._flush_terminal(command_id)

    def on_order(self, message: Mapping[str, Any], *, local_receive_ts_ns: int) -> None:
        for row in _rows(message):
            state = self.kernel._state_ref()
            command_id = _command_id_for_row(row, state)
            venue_order_id = str(row.get("orderId") or row.get("order_id") or "")
            venue_identity_bound = bool(
                command_id in state.orders
                and venue_order_id
                and state.orders[command_id].venue_order_id == venue_order_id
            )
            native_observed = bool(
                not venue_identity_bound
                and self.native_protection_manager is not None
                and self.native_protection_manager.observe_order(row)
            )
            if native_observed:
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
            order = state.orders.get(command_id)
            if order is None:
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
            rejection_key=(f"bybit-order:{command_id}:{row.get('rejectReason')}" if status == "rejected" else ""),
            metadata={
                "order_status": str(row.get("orderStatus") or ""),
                "cancel_type": str(row.get("cancelType") or ""),
                "reject_reason": str(row.get("rejectReason") or ""),
                "venue_order_id": str(row.get("orderId") or row.get("order_id") or ""),
                "create_type": str(row.get("createType") or row.get("create_type") or ""),
                "stop_order_type": str(row.get("stopOrderType") or row.get("stop_order_type") or ""),
                "source": "bybit_private_order_ws",
            },
        )

    def _flush_terminal(self, command_id: str) -> None:
        terminal = self.pending_terminal.get(command_id)
        if terminal is None:
            return
        state = self.kernel._state_ref()
        order = state.orders.get(command_id)
        if order is None:
            self.pending_terminal.pop(command_id, None)
            return
        reconstructed = abs(order.filled_signed_qty)
        tolerance = quantity_tolerance(order.signed_qty)
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
            order = self.kernel._state_ref().orders[command_id]
        if terminal.cumulative_filled_qty > reconstructed + tolerance:
            # Order WS raced ahead of execution WS. Wait for the missing fills;
            # a REST execution reconciliation can feed the same on_execution path.
            return
        key = f"{command_id}:{terminal.status}"
        if key not in self._terminal_recorded:
            self._absorb_recorded_terminals()
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
        with self._stream_lock:
            self._closed = True
            private_stream = self.private_stream
            self.private_stream = None
        if private_stream is not None:
            close = getattr(private_stream, "close", None)
            if callable(close):
                close()


class PrivateExecutionStreamSupervisor:
    """Block health on a dead private stream and rebuild it without auth storms."""

    def __init__(
        self,
        *,
        consumer: BybitAccountExecutionConsumer,
        stream_factory: Callable[[], Any],
        reconnect_after_seconds: float = 180.0,
        reconnect_cooldown_seconds: float = 180.0,
        candidate_ready_timeout_seconds: float = 10.0,
        candidate_ready_poll_seconds: float = 0.05,
    ) -> None:
        reconnect_after = float(reconnect_after_seconds)
        reconnect_cooldown = float(reconnect_cooldown_seconds)
        candidate_ready_timeout = float(candidate_ready_timeout_seconds)
        candidate_ready_poll = float(candidate_ready_poll_seconds)
        if not math.isfinite(reconnect_after) or reconnect_after <= 0.0:
            raise ValueError("private stream reconnect bound must be positive and finite")
        if not math.isfinite(reconnect_cooldown) or reconnect_cooldown <= 0.0:
            raise ValueError("private stream reconnect cooldown must be positive and finite")
        if not math.isfinite(candidate_ready_timeout) or candidate_ready_timeout <= 0.0:
            raise ValueError("private stream candidate-ready timeout must be positive and finite")
        if not math.isfinite(candidate_ready_poll) or candidate_ready_poll <= 0.0:
            raise ValueError("private stream candidate-ready poll must be positive and finite")
        self.consumer = consumer
        self.stream_factory = stream_factory
        self.reconnect_after_seconds = reconnect_after
        self.reconnect_cooldown_seconds = reconnect_cooldown
        self.candidate_ready_timeout_seconds = candidate_ready_timeout
        self.candidate_ready_poll_seconds = candidate_ready_poll
        self.not_ready_since: float | None = None
        self.last_reconnect_attempt = float("-inf")
        self.connection_status: bool | None = None
        self.reconnect_attempts = 0
        self.reconnect_successes = 0
        self.last_error = ""
        self._rebuild_results: queue.Queue[tuple[str, str]] = queue.Queue()
        self._rebuild_thread: threading.Thread | None = None

    @staticmethod
    def _connected(stream: Any | None) -> bool | None:
        if stream is None:
            return False
        probe = getattr(stream, "is_ready", None)
        if not callable(probe):
            probe = getattr(stream, "is_connected", None)
        if not callable(probe):
            return None
        try:
            value = probe()
        except Exception:  # noqa: BLE001 - probe failures become blocked health, never loop failures
            return None
        return value if type(value) is bool else None

    @property
    def health_detail(self) -> str:
        if self.connection_status is False:
            stream = self.consumer.current_private_stream()
            try:
                provider_detail = getattr(stream, "readiness_detail", "")
                if callable(provider_detail):
                    provider_detail = provider_detail()
            except Exception:  # noqa: BLE001 - detail must not break health publication
                provider_detail = ""
            detail = str(provider_detail or "private execution websocket is disconnected")
        elif self.connection_status is None:
            detail = "private execution websocket liveness is unavailable"
        else:
            return ""
        if self.last_error:
            detail += f": {self.last_error}"
        if self.rebuild_in_progress:
            detail += "; rebuild in progress"
        return detail[:1000]

    @property
    def rebuild_in_progress(self) -> bool:
        thread = self._rebuild_thread
        return thread is not None and thread.is_alive()

    def require_recent_healthy(self, *, max_age_ns: int) -> None:
        del max_age_ns  # an active local socket probe is stronger than cached age
        # Admission can follow reconciliation/protection work after the loop's
        # supervisory check. Probe again here so a disconnect in that interval
        # cannot inherit the earlier healthy observation.
        self.connection_status = self._connected(self.consumer.current_private_stream())
        if self.connection_status is not True:
            raise RuntimeError(self.health_detail)

    def _rebuild(self) -> None:
        replacement: Any | None = None
        current = self.consumer.current_private_stream()
        try:
            replacement = self.stream_factory()
            self.consumer.prepare_private_stream(replacement)
            deadline = time.monotonic() + self.candidate_ready_timeout_seconds
            while True:
                # Do not discard a stream that recovered while its candidate
                # was authenticating/subscribing in the background.
                if self._connected(current) is True:
                    close = getattr(replacement, "close", None)
                    if callable(close):
                        close()
                    self._rebuild_results.put(("recovered", ""))
                    return
                if self._connected(replacement) is True:
                    break
                if time.monotonic() >= deadline:
                    detail = getattr(replacement, "readiness_detail", "")
                    raise RuntimeError(
                        "replacement private stream did not become ready" + (f": {detail}" if detail else "")
                    )
                time.sleep(self.candidate_ready_poll_seconds)
            self.consumer.publish_private_stream(
                replacement,
                expected_current=current,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced into blocked health by the owner loop
            if replacement is not None and replacement is not self.consumer.current_private_stream():
                close = getattr(replacement, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - retain the original rebuild error
                        pass
            error = f"private stream rebuild failed: {type(exc).__name__}: {exc}"[:500]
            self._rebuild_results.put(("failed", error))
            return
        self._rebuild_results.put(("replaced", ""))

    def _consume_rebuild_results(self) -> None:
        while True:
            try:
                outcome, error = self._rebuild_results.get_nowait()
            except queue.Empty:
                return
            self._rebuild_thread = None
            if outcome in {"replaced", "recovered"}:
                if outcome == "replaced":
                    self.reconnect_successes += 1
                # The replacement gets its own continuous-down grace period if
                # it is already false on the next owner tick. Recovery also
                # starts a fresh continuous-down interval if it drops again.
                self.not_ready_since = None
                self.last_error = ""
            else:
                self.last_error = error
                _logger.error(error)

    def check(self, *, now_monotonic: float) -> bool | None:
        now = float(now_monotonic)
        if not math.isfinite(now):
            raise ValueError("private stream health clock must be finite")
        self._consume_rebuild_results()
        stream = self.consumer.current_private_stream()
        connected = self._connected(stream)
        self.connection_status = connected
        if connected is True:
            self.not_ready_since = None
            self.last_error = ""
            return True
        if connected is None:
            # Older/opaque clients cannot be rebuilt safely from an ambiguous
            # signal, but unknown mutation-stream health still fails closed.
            return None
        if self.not_ready_since is None:
            self.not_ready_since = now
        not_ready_for = now - self.not_ready_since
        if not_ready_for <= self.reconnect_after_seconds:
            return False
        if now - self.last_reconnect_attempt <= self.reconnect_cooldown_seconds:
            return False
        if self.rebuild_in_progress:
            return False

        self.last_reconnect_attempt = now
        self.reconnect_attempts += 1
        _logger.warning(
            "private execution websocket not ready %.1fs; rebuilding attempt=%d",
            not_ready_for,
            self.reconnect_attempts,
        )
        self._rebuild_thread = threading.Thread(
            target=self._rebuild,
            name="account-private-stream-rebuild",
            daemon=True,
        )
        self._rebuild_thread.start()
        return False
