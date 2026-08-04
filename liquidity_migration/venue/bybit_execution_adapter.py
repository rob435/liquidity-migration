"""Account-owner-only adapter from kernel commands to Bybit mutations."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from liquidity_migration.account.account_contracts import (
    MarketInputRef,
    OrderCommand,
)
from liquidity_migration.marketdata.bybit_errors import BybitRequestRejected
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock
from liquidity_migration.core.venue_realm import client_venue_realm
from liquidity_migration.account.execution_adapters import ExecutionObservation, ExecutionObservationType
from liquidity_migration.venue.entry_quote_manager import EntryQuoteManager

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a protection import cycle
    from typing import Protocol

    class EntryStopVerifier(Protocol):
        def __call__(
            self,
            *,
            symbol: str,
            expected_stop_price: float,
            command_id: str,
        ) -> str: ...
else:  # pragma: no cover - runtime alias
    EntryStopVerifier = object


def bybit_private_execution_metadata(
    row: Mapping[str, Any],
    *,
    message_creation_ts_ns: int,
) -> dict[str, Any]:
    """Preserve the documented execution facts shared by every fill path."""

    fee_observed = row.get("execFee") not in (None, "") or row.get("exec_fee") not in (None, "")
    try:
        cross_sequence = int(float(row.get("seq") or 0))
    except (TypeError, ValueError):
        cross_sequence = 0
    return {
        "cross_sequence": cross_sequence,
        # Missing execFee is not a zero fee, but the reducer needs a numeric
        # placeholder, so the provenance stays explicit.
        "fee_observed": fee_observed,
        "fee_status": ("observed_execution_fee" if fee_observed else "pending_missing_execution_fee"),
        "fee_source": "bybit_private_execution.execFee",
        "fee_rate": str(row.get("feeRate") or row.get("fee_rate") or ""),
        "fee_currency": str(row.get("feeCurrency") or row.get("fee_currency") or ""),
        "is_maker": (
            row.get("isMaker")
            if type(row.get("isMaker")) is bool
            else row.get("is_maker")
            if type(row.get("is_maker")) is bool
            else None
        ),
        "execution_type": str(row.get("execType") or row.get("exec_type") or ""),
        "execution_value": str(row.get("execValue") or row.get("exec_value") or ""),
        "order_qty": str(row.get("orderQty") or row.get("order_qty") or ""),
        "leaves_qty": str(row.get("leavesQty") or row.get("leaves_qty") or ""),
        "closed_size": str(row.get("closedSize") or row.get("closed_size") or ""),
        "order_type": str(row.get("orderType") or row.get("order_type") or ""),
        "create_type": str(row.get("createType") or row.get("create_type") or ""),
        "message_creation_ts_ns": int(message_creation_ts_ns),
        "source": "bybit_private_execution_ws",
    }


class BybitDemoExecutionAdapter:
    """Thin Bybit command adapter for whichever realm its client addresses.

    Submission yields the create acknowledgement only; executions arrive through
    the private execution stream and go to the kernel driver. No fill is ever
    inferred from a successful create response.
    """

    # Frozen: journaled as ``adapter_name`` in every submission attempt, and
    # ``AccountExecutionService`` keys the position-truth and native-protection
    # requirements off this exact string. It is an identity, not a description,
    # so it keeps its demo-era spelling in both realms.
    name = "bybit_demo"
    submission_outcome_can_be_ambiguous = True

    def __init__(
        self,
        client: Any,
        *,
        clock: Clock | None = None,
        # Anchored to the batch journal instant shared by every sibling
        # command, so the budget has to absorb whole-batch venue latency
        # (leverage + create + stop verification per earlier sibling), not one
        # round trip. 5s wedged a nine-slice entry batch on 2026-08-01.
        max_unsubmitted_exposure_age_ns: int = 120_000_000_000,
        entry_stop_verifier: EntryStopVerifier | None = None,
        entry_quotes: EntryQuoteManager | None = None,
    ) -> None:
        # Realm-agnostic: the order path is identical in both realms, and the
        # arming decision belongs to credential resolution, which requires
        # REAL_MONEY for mainnet. What is refused here is a client whose
        # declared realm and transport disagree.
        self.realm = client_venue_realm(client, what="Bybit execution adapter")
        if (
            type(max_unsubmitted_exposure_age_ns) is not int
            or max_unsubmitted_exposure_age_ns <= 0
        ):
            raise ValueError("max_unsubmitted_exposure_age_ns must be a positive integer")
        self.client = client
        self.clock = clock or SystemClock()
        self.max_unsubmitted_exposure_age_ns = max_unsubmitted_exposure_age_ns
        # Atomic arming is a Bybit behaviour, not something this system owns.
        # Without a verifier the create is simply trusted.
        self.entry_stop_verifier = entry_stop_verifier
        # With a manager, exposure-increasing entries rest at the touch first;
        # every gate inside plan_entry_quote falls back to the market order.
        self.entry_quotes = entry_quotes

    @staticmethod
    def _entry_protection_metadata(command: OrderCommand) -> dict[str, Any]:
        entry_protection_metadata: dict[str, Any] = {}
        if command.reduce_only:
            if any(
                value not in (None, "")
                for value in (
                    command.entry_stop_price,
                    command.entry_stop_fraction,
                    command.entry_stop_source,
                    command.entry_stop_trigger_by,
                )
            ):
                raise RuntimeError("reduce-only Bybit command cannot carry entry protection")
        else:
            stop_price = command.entry_stop_price
            stop_fraction = command.entry_stop_fraction
            if (
                stop_price is None
                or not math.isfinite(stop_price)
                or stop_price <= 0.0
                or stop_fraction is None
                or not math.isfinite(stop_fraction)
                or not 0.0 < stop_fraction < 1.0
                or not command.entry_stop_source
                or command.entry_stop_trigger_by != "MarkPrice"
            ):
                # Internal invariant breach, not a venue rejection: raise before
                # any mutation so a corrupt command cannot replay as a naked
                # entry.
                raise RuntimeError("Bybit exposure-increasing command lacks durable entry-attached protection")
            if command.signed_qty > 0.0 and stop_price >= command.reference_price:
                raise RuntimeError("long entry stop is not below its durable reference price")
            if command.signed_qty < 0.0 and stop_price <= command.reference_price:
                raise RuntimeError("short entry stop is not above its durable reference price")
            entry_protection_metadata = {
                "entry_attached_protection_requested": True,
                "entry_stop_price": stop_price,
                "entry_stop_fraction": stop_fraction,
                "entry_stop_source": command.entry_stop_source,
                "entry_stop_trigger_by": command.entry_stop_trigger_by,
            }
        return entry_protection_metadata

    @staticmethod
    def _order_params(command: OrderCommand) -> dict[str, Any]:
        params = {
            "symbol": command.symbol,
            "side": command.side,
            "orderType": "Market",
            "qty": format(Decimal(str(command.qty)), "f"),
            "orderLinkId": command.command_id,
            "reduceOnly": command.reduce_only,
        }
        if not command.reduce_only:
            params.update(
                {
                    "positionIdx": 0,
                    "stopLoss": format(
                        Decimal(str(command.entry_stop_price)).normalize(),
                        "f",
                    ),
                    "slTriggerBy": command.entry_stop_trigger_by,
                    "tpslMode": "Full",
                    "slOrderType": "Market",
                }
            )
        return params

    def prepare_submission(
        self,
        command: OrderCommand,
        _market_input: MarketInputRef,
    ) -> Iterable[ExecutionObservation]:
        """Validate the command and negotiate idempotent leverage.

        This phase cannot create exposure, so the driver runs it before claiming
        the durable order-create attempt: a lost leverage response can retry
        without being confused with an ACK-lost market order.
        """

        entry_protection_metadata = self._entry_protection_metadata(command)
        # Build the deterministic request fields before the durable attempt.
        # The command is immutable, so ``submit_prepared``'s rebuild matches.
        self._order_params(command)
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
                            **entry_protection_metadata,
                        },
                    ),
                )
        return ()

    def _entry_quote_price(
        self,
        command: OrderCommand,
        market_input: MarketInputRef,
    ) -> str | None:
        """Near-touch limit price for an entry, or None for the market path."""

        if self.entry_quotes is None or command.reduce_only:
            return None
        try:
            return self.entry_quotes.plan_entry_quote(
                symbol=command.symbol,
                is_buy=command.signed_qty > 0.0,
                bid=market_input.bid_price,
                ask=market_input.ask_price,
            )
        except Exception:  # noqa: BLE001 - quoting must never block an entry
            return None

    def submit_prepared(
        self,
        command: OrderCommand,
        market_input: MarketInputRef,
    ) -> Iterable[ExecutionObservation]:
        """Perform only the exposure-capable order-create effect."""

        entry_protection_metadata = self._entry_protection_metadata(command)
        params = self._order_params(command)
        quote_price = self._entry_quote_price(command, market_input)
        execution_style = "market"
        clip_qty: str | None = None
        if quote_price is not None:
            if self.entry_quotes is not None:
                try:
                    clip_qty = self.entry_quotes.plan_entry_clip(
                        symbol=command.symbol,
                        is_buy=command.signed_qty > 0.0,
                        command_qty=command.qty,
                        price=float(quote_price),
                        bid_qty=market_input.bid_qty,
                        ask_qty=market_input.ask_qty,
                    )
                except Exception:  # noqa: BLE001 - clipping must never block an entry
                    clip_qty = None
            params.update(
                {
                    "orderType": "Limit",
                    "price": quote_price,
                    "timeInForce": "GTC",
                }
            )
            if clip_qty is not None:
                # Rest only what the displayed touch can absorb; the command
                # terminates at the window end with the shortfall un-ordered
                # and convergence plans the next window.
                params["qty"] = clip_qty
            execution_style = "resting_quote"
        # Measures the create-order request only; leverage negotiation sits
        # outside request/ack RTT but inside command-decision-to-socket delay.
        send_ts_ns = self.clock.wall_time_ns()
        try:
            try:
                result = self.client.place_order(**params)
            except BybitRequestRejected:
                if quote_price is None:
                    raise
                # A clean venue reject of the limit create leaves no order
                # under this link id, so the market order the fleet always
                # sent is still available and still protected.
                quote_price = None
                execution_style = "market_after_quote_reject"
                params = self._order_params(command)
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
                        "execution_style": execution_style,
                        **entry_protection_metadata,
                    },
                ),
            )
        # Transport failures and duplicate-link races are ambiguous: the venue
        # may already own this command, so the request is released with the
        # command still ``commanded`` and reconciliation probes the orderLinkId.
        local_ack_ts_ns = self.clock.wall_time_ns()
        idempotent_existing_order = bool(result.get("_idempotent_existing_order"))
        exchange_ack_ms = 0
        if not idempotent_existing_order:
            exchange_ack_ms = result.get("_response_time_ms") or result.get("time") or 0
        try:
            exchange_ack_ts_ns = int(float(exchange_ack_ms) * 1_000_000)
        except (TypeError, ValueError):
            exchange_ack_ts_ns = 0
        if quote_price is not None:
            # The stop attaches when the resting order fills; the manager runs
            # the same verifier at that moment, and reconciliation still owns
            # the fallback proof, exactly as it does for a repaired stop.
            verification = "deferred_resting_quote"
            if self.entry_quotes is not None and not idempotent_existing_order:
                self.entry_quotes.register(
                    command_id=command.command_id,
                    symbol=command.symbol,
                    is_buy=command.signed_qty > 0.0,
                    price=float(quote_price),
                )
        else:
            verification = self._verify_entry_attached_stop(command)
        metadata: dict[str, Any] = {
            "local_socket_send_ts_ns": send_ts_ns,
            "exchange_ack_ts_status": "observed" if exchange_ack_ts_ns else "unavailable",
            "exchange_ack_ts_source": (
                "bybit_v5_response_envelope_time" if exchange_ack_ts_ns else "unavailable"
            ),
            "idempotent_existing_order": idempotent_existing_order,
            "requested_leverage": command.leverage,
            "entry_attached_stop_verification": verification,
            "execution_style": execution_style,
            **entry_protection_metadata,
        }
        if quote_price is not None:
            metadata["entry_quote_price"] = quote_price
            metadata["entry_quote_window_seconds"] = (
                self.entry_quotes.config.window_seconds if self.entry_quotes is not None else 0.0
            )
            if clip_qty is not None:
                metadata["entry_clip_qty"] = clip_qty
                metadata["entry_commanded_qty"] = command.qty
        return (
            ExecutionObservation(
                observation_type=ExecutionObservationType.ACK,
                command_id=command.command_id,
                exchange_ts_ns=exchange_ack_ts_ns,
                local_receive_ts_ns=local_ack_ts_ns,
                accepted=True,
                venue_order_id=str(result.get("orderId") or ""),
                metadata=metadata,
            ),
        )

    def _verify_entry_attached_stop(self, command: OrderCommand) -> str:
        """Prove the venue applied the attached stop, right after the create.

        Never raises: the order is already at the venue, and losing this
        acknowledgement would orphan a live position. The verifier owns the
        fail-closed consequence -- repair where it can, latch a breach where it
        cannot, which blocks new exposure and flattens via the software-flat
        path.
        """

        if command.reduce_only or self.entry_stop_verifier is None:
            return "not_applicable" if command.reduce_only else "unverified_no_verifier"
        stop_price = command.entry_stop_price
        if stop_price is None:  # pragma: no cover - _entry_protection_metadata rejects this first
            return "unverified_no_stop_price"
        try:
            return str(
                self.entry_stop_verifier(
                    symbol=command.symbol,
                    expected_stop_price=float(stop_price),
                    command_id=command.command_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the ACK must survive a verifier fault
            return f"verifier_failed:{type(exc).__name__}"[:120]

    def submit(
        self,
        command: OrderCommand,
        market_input: MarketInputRef,
    ) -> Iterable[ExecutionObservation]:
        """Standalone adapter entry point used outside the kernel driver."""

        prepared = tuple(self.prepare_submission(command, market_input))
        if prepared:
            return prepared
        return self.submit_prepared(command, market_input)
