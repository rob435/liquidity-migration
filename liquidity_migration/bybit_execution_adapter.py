"""Account-owner-only adapter from kernel commands to Bybit demo mutations."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from .account_kernel import MarketInputRef, OrderCommand
from .bybit_errors import BybitRequestRejected
from .deterministic_runtime import Clock, SystemClock
from .execution_adapters import ExecutionObservation, ExecutionObservationType


class BybitDemoExecutionAdapter:
    """Thin, demo-only Bybit command adapter.

    Submission yields the create acknowledgement only. Actual executions must
    arrive through the private execution stream and be passed to the shared
    kernel driver; the adapter never invents fills from a successful create
    response.
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
                return (
                    ExecutionObservation(
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
                    ),
                )
        # Measure the create-order request itself. Entry leverage negotiation is
        # intentionally outside request/ack RTT but remains inside the broader
        # command-decision-to-socket delay.
        send_ts_ns = self.clock.wall_time_ns()
        try:
            result = self.client.place_order(**params)
        except BybitRequestRejected as exc:
            local_ack_ts_ns = self.clock.wall_time_ns()
            return (
                ExecutionObservation(
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
                ),
            )
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
        return (
            ExecutionObservation(
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
                        "bybit_v5_response_envelope_time" if exchange_ack_ts_ns else "unavailable"
                    ),
                    "idempotent_existing_order": idempotent_existing_order,
                    "requested_leverage": command.leverage,
                },
            ),
        )
