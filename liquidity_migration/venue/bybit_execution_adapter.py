"""Account-owner-only adapter from kernel commands to Bybit mutations."""

from __future__ import annotations

import math
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal
from typing import AbstractSet, Any, Callable, Iterable, Mapping, Sequence

from liquidity_migration.account.account_contracts import (
    MarketInputRef,
    OrderCommand,
)
from liquidity_migration.marketdata.bybit_errors import (
    BybitRequestRejected,
    BybitSubmissionUncertain,
    is_transient_venue_fault,
)
from liquidity_migration.core.deterministic_runtime import Clock, SystemClock
from liquidity_migration.core.venue_realm import client_venue_realm
from liquidity_migration.account.execution_adapters import ExecutionObservation, ExecutionObservationType
from liquidity_migration.venue.entry_quote_manager import EntryQuoteManager, EntryStopVerifier


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
        # round trip. A nine-slice entry batch needs more than 5s.
        max_unsubmitted_exposure_age_ns: int = 120_000_000_000,
        entry_stop_verifier: EntryStopVerifier | None = None,
        entry_quotes: EntryQuoteManager | None = None,
        # True when this process is the only party that sets leverage on the
        # account. See ``retain_confirmed_leverage``: it decides whether a
        # symbol going flat forgets its leverage and pays a round trip on the
        # next entry. False is the hand-traded-account behaviour.
        sole_leverage_authority: bool = True,
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
        self.sole_leverage_authority = bool(sole_leverage_authority)
        # Leverage this process has already set at the venue, per symbol.
        # Bybit keeps a symbol's leverage until someone changes it, so resending
        # the value it already holds bought nothing and cost a full round trip
        # ahead of every entry — about 175 ms on the Frankfurt route, which was
        # most of the delay between deciding to trade and the order leaving.
        # Starts empty each process, so the first entry per symbol still sets
        # it, and any rejected create drops the symbol back to unknown.
        # ``retain_confirmed_leverage`` keeps only what authenticated position
        # truth still agrees with, because this is not the only party that
        # changes leverage on the account.
        self._venue_leverage: dict[str, float] = {}
        # Outcomes of the last speculative pre-commit leverage round, per
        # symbol: ("rejected"|"uncertain", leverage, error, deadline_ns).
        # Written only at the join (owner thread), consumed once by
        # ``prepare_submission`` so the stored answer replaces the venue call
        # it already made — never a second source of truth beyond its short
        # deadline.
        self._speculative_leverage_outcomes: dict[
            str, tuple[str, float, BaseException, int]
        ] = {}

    #: How long a speculative leverage outcome may substitute for the live
    #: call. The consuming prepare follows the join within the same pass —
    #: milliseconds — so this only has to outlive one pass, and an outcome
    #: nobody consumed (the batch was risk-rejected) must not answer for a
    #: later, unrelated batch.
    _SPECULATIVE_OUTCOME_TTL_NS = 5_000_000_000

    def confirmed_venue_leverage(self) -> dict[str, float]:
        """Leverage facts an ENTRY claim may fuse on: sole authority only.

        Under shared leverage authority the cache still speeds the post-commit
        prepare step, but it must not license commit fusion for entries:
        fusing changes the crash window's retry semantics, and the funded
        hand-traded account keeps today's order flow byte-identical except
        for reduce-only claims, which are repeatable and fuse everywhere.
        """

        if not self.sole_leverage_authority:
            return {}
        return dict(self._venue_leverage)

    def speculative_leverage_pairs(
        self, pairs: Sequence[tuple[str, float]]
    ) -> tuple[tuple[str, float], ...]:
        """The (symbol, leverage) pairs a speculative round would need to set.

        Empty under shared leverage authority: the owner hand-sets leverage on
        this account, and a speculative call for a batch the RISK_DECISION
        then rejects would overwrite a hand-set value on a flat symbol. The
        post-commit sequence only sets leverage for risk-accepted commands,
        so shared authority keeps exactly that sequence.
        """

        if not self.sole_leverage_authority:
            return ()
        wanted: dict[str, float] = {}
        for symbol, leverage in pairs:
            wanted[str(symbol).upper()] = float(leverage)
        return tuple(
            (symbol, leverage)
            for symbol, leverage in sorted(wanted.items())
            if self._venue_leverage.get(symbol) != leverage
        )

    def begin_speculative_leverage(
        self, pairs: Sequence[tuple[str, float]]
    ) -> Callable[[], None] | None:
        """Fire cache-miss ``set_leverage`` calls concurrently, pre-commit.

        Returns a join, or None when there is nothing to do. Workers only
        RETURN outcomes — every cache and outcome-store mutation happens in
        the join, on the calling owner thread, so ``_venue_leverage`` never
        gains a second writer. The join never raises: a definite reject or an
        uncertain answer is stored per symbol and ``prepare_submission``
        replays it with exactly the semantics the inline call has today —
        reject becomes the unaccepted pre-create ACK, uncertain raises before
        any claim exists.
        """

        # Round boundary first: answers from a prior round must not outlive
        # it even when THIS round has nothing to do — an unconsumed outcome
        # (its target was risk-rejected) could otherwise answer a later,
        # unrelated batch's prepare within the TTL.
        self._speculative_leverage_outcomes.clear()
        plan = self.speculative_leverage_pairs(pairs)
        if not plan:
            return None

        def negotiate(symbol: str, leverage: float) -> None:
            self.client.set_leverage(
                symbol=symbol,
                buy_leverage=leverage,
                sell_leverage=leverage,
            )

        pool = ThreadPoolExecutor(
            max_workers=len(plan),
            thread_name_prefix="account-leverage-speculate",
        )
        futures: list[tuple[str, float, Future[None]]] = [
            (symbol, leverage, pool.submit(negotiate, symbol, leverage))
            for symbol, leverage in plan
        ]
        pool.shutdown(wait=False)

        def join() -> None:
            deadline_ns = self.clock.wall_time_ns() + self._SPECULATIVE_OUTCOME_TTL_NS
            for symbol, leverage, future in futures:
                try:
                    future.result()
                except BybitRequestRejected as exc:
                    self._speculative_leverage_outcomes[symbol] = (
                        "rejected",
                        leverage,
                        exc,
                        deadline_ns,
                    )
                except BaseException as exc:  # noqa: BLE001 - replayed verbatim at prepare
                    self._speculative_leverage_outcomes[symbol] = (
                        "uncertain",
                        leverage,
                        exc,
                        deadline_ns,
                    )
                else:
                    self._venue_leverage[symbol] = leverage

        return join

    def _consume_speculative_outcome(
        self, command: OrderCommand
    ) -> tuple[str, BaseException] | None:
        """Pop this command's stored speculative answer, if still current."""

        stored = self._speculative_leverage_outcomes.pop(command.symbol, None)
        if stored is None:
            return None
        kind, leverage, error, deadline_ns = stored
        if leverage != float(command.leverage) or self.clock.wall_time_ns() > deadline_ns:
            # The speculation answered a different question, or too long ago:
            # fall through to the live call.
            return None
        return kind, error

    def retain_confirmed_leverage(
        self,
        venue_leverage: Mapping[str, float],
        *,
        positioned_symbols: AbstractSet[str],
    ) -> None:
        """Forget cached leverage the venue contradicts, or no longer vouches for.

        A symbol the venue reports with a DIFFERENT leverage is contradicted:
        drop it, always, under either authority setting below. That is the case
        that protects the sizing.

        A symbol that IS positioned but whose ``leverage`` field did not parse
        is no evidence either way, so the cache stands: Bybit blanks fields per
        margin mode (it did exactly that to the account-wide wallet totals on
        2026-08-04), and treating blank as contradiction would drop every symbol
        on every pass and hand back the round trip this cache exists to avoid.

        A symbol with no open position is the case ``sole_leverage_authority``
        decides. Bybit keeps a symbol's leverage after the position closes, so
        what this process last set is still what the venue holds -- unless
        somebody else changed it. While the owner hand-traded this account, they
        could, so a flat symbol was dropped and its next entry paid one
        ``set_leverage``: measured at 188-194 ms, on every fresh entry. With the
        owner no longer hand-trading (2026-08-08), nothing else writes leverage
        here, so the cache survives going flat and that round trip disappears.
        Pass ``sole_leverage_authority=False`` to restore the old behaviour the
        moment hand-trading resumes.
        """

        confirmed = {str(symbol).upper(): float(value) for symbol, value in venue_leverage.items()}
        positioned = {str(symbol).upper() for symbol in positioned_symbols}
        for symbol in [
            symbol
            for symbol, cached in self._venue_leverage.items()
            if (symbol not in positioned and not self.sole_leverage_authority)
            or (symbol in confirmed and confirmed[symbol] != cached)
        ]:
            del self._venue_leverage[symbol]

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
        if not command.reduce_only and self._venue_leverage.get(command.symbol) != float(
            command.leverage
        ):
            speculative = self._consume_speculative_outcome(command)
            if speculative is not None and speculative[0] == "uncertain":
                # The pre-commit speculative call already asked the venue this
                # exact question and got no usable answer. Propagate it here —
                # before any claim exists — exactly as the inline call below
                # would have, so the request is released and retried.
                raise speculative[1]
            try:
                if speculative is not None:
                    # A stored definite reject replays without a second round
                    # trip; the venue already refused this exact leverage
                    # moments ago.
                    raise speculative[1]
                self.client.set_leverage(
                    symbol=command.symbol,
                    buy_leverage=command.leverage,
                    sell_leverage=command.leverage,
                )
                self._venue_leverage[command.symbol] = float(command.leverage)
            # Only the DEFINITE reject becomes an unaccepted ack. An
            # uncertain leverage response must keep propagating: the request
            # stays pending and the next pass retries the call, which is what
            # test_uncertain_leverage_response_retries_before_single_order_attempt
            # pins. Catching it here would turn a retryable venue hiccup into a
            # lost entry.
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
                bid_qty=market_input.bid_qty,
                ask_qty=market_input.ask_qty,
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
            # A refused create is the one signal that what this process believes
            # about the symbol may be wrong — margin refusals read as a create
            # reject. Forget the leverage so the next attempt sets it again.
            self._venue_leverage.pop(command.symbol, None)
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
                decision_mid = None
                if (
                    market_input.bid_price is not None
                    and market_input.ask_price is not None
                    and market_input.bid_price > 0.0
                    and market_input.ask_price > market_input.bid_price
                ):
                    decision_mid = (market_input.bid_price + market_input.ask_price) / 2.0
                self.entry_quotes.register(
                    command_id=command.command_id,
                    symbol=command.symbol,
                    is_buy=command.signed_qty > 0.0,
                    price=float(quote_price),
                    decision_mid=decision_mid,
                )
        else:
            verification = self._verify_entry_attached_stop(
                command,
                acknowledged_ts_ns=local_ack_ts_ns,
            )
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

    def submit_prepared_batch(
        self,
        items: Sequence[tuple[OrderCommand, MarketInputRef]],
    ) -> tuple[ExecutionObservation, ...]:
        """One venue request creates every order in the batch.

        Same per-command semantics as ``submit_prepared``, paid once: a
        successful row acks with quote registration or stop verification, a
        rejected resting-quote row falls back to its market order, a rejected
        market row becomes an unaccepted ack, and a row whose individual
        fallback outcome is unknown emits nothing — the command stays claimed
        and the reconciler's orderLinkId probes resolve it. A transport
        failure of the whole request raises BybitSubmissionUncertain for
        every row, which is exactly what the durable attempt claims are for.
        """

        prepared: list[tuple[OrderCommand, MarketInputRef, dict[str, Any], str | None, str | None, dict[str, Any]]] = []
        for command, market_input in items:
            entry_protection_metadata = self._entry_protection_metadata(command)
            params = self._order_params(command)
            quote_price = self._entry_quote_price(command, market_input)
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
                    {"orderType": "Limit", "price": quote_price, "timeInForce": "GTC"}
                )
                if clip_qty is not None:
                    params["qty"] = clip_qty
            prepared.append(
                (command, market_input, params, quote_price, clip_qty, entry_protection_metadata)
            )

        send_ts_ns = self.clock.wall_time_ns()
        try:
            rows = self.client.place_orders_batch(
                [params for _, _, params, _, _, _ in prepared]
            )
        except BybitRequestRejected:
            # The batch envelope itself was refused — per the venue contract
            # no rows were created, and if any were, each row's orderLinkId
            # makes the resend below idempotent (duplicate-link probe). A
            # broken batch endpoint therefore costs latency, never orders:
            # degrade to the sequential single-order path.
            observations = []
            for command, market_input, params, quote_price, clip_qty, protection in prepared:
                observation = self._place_single_row(
                    command,
                    market_input,
                    params,
                    send_ts_ns=send_ts_ns,
                    quote_price=quote_price,
                    clip_qty=clip_qty,
                    entry_protection_metadata=protection,
                )
                if observation is not None:
                    observations.append(observation)
            return tuple(observations)

        observations = []
        for (command, market_input, _, quote_price, clip_qty, protection), row in zip(
            prepared, rows
        ):
            row_code = row.get("_row_code")
            if row_code in (0, None) or row.get("_idempotent_existing_order"):
                style = "resting_quote" if quote_price is not None else "market"
                observations.append(
                    self._batch_success_ack(
                        command,
                        market_input,
                        row,
                        send_ts_ns=send_ts_ns,
                        execution_style=style,
                        quote_price=quote_price,
                        clip_qty=clip_qty,
                        entry_protection_metadata=protection,
                    )
                )
                continue
            if is_transient_venue_fault(
                {"retCode": row_code, "retMsg": row.get("_row_msg", "")}
            ):
                # "Not now" is not "no": a timed-out row's order may still
                # exist at the venue. Emit nothing — the command stays
                # claimed and the orderLinkId probe ladder resolves it,
                # exactly as the single path does for an uncertain answer.
                continue
            if quote_price is not None:
                # A clean venue reject of the limit row leaves no order under
                # this link id, so the market order the fleet always sent is
                # still available and still protected.
                observation = self._place_single_row(
                    command,
                    market_input,
                    self._order_params(command),
                    send_ts_ns=send_ts_ns,
                    quote_price=None,
                    clip_qty=None,
                    entry_protection_metadata=protection,
                    execution_style="market_after_quote_reject",
                )
                if observation is not None:
                    observations.append(observation)
                continue
            observations.append(
                self._batch_reject_ack(
                    command,
                    error_type="BybitBatchRowRejected",
                    error=f"code={row_code} msg={row.get('_row_msg', '')}"[:500],
                    send_ts_ns=send_ts_ns,
                    execution_style="market",
                    entry_protection_metadata=protection,
                )
            )
        return tuple(observations)

    def _place_single_row(
        self,
        command: OrderCommand,
        market_input: MarketInputRef,
        params: Mapping[str, Any],
        *,
        send_ts_ns: int,
        quote_price: str | None,
        clip_qty: str | None,
        entry_protection_metadata: Mapping[str, Any],
        execution_style: str | None = None,
    ) -> ExecutionObservation | None:
        """One batch row placed individually, keeping batch-row semantics.

        Returns None when the outcome is unknown: the command stays claimed
        for the probe ladder rather than aborting its siblings.
        """

        style = execution_style or ("resting_quote" if quote_price is not None else "market")
        try:
            result = self.client.place_order(**dict(params))
        except BybitRequestRejected as exc:
            if quote_price is not None:
                return self._place_single_row(
                    command,
                    market_input,
                    self._order_params(command),
                    send_ts_ns=send_ts_ns,
                    quote_price=None,
                    clip_qty=None,
                    entry_protection_metadata=entry_protection_metadata,
                    execution_style="market_after_quote_reject",
                )
            return self._batch_reject_ack(
                command,
                error_type=type(exc).__name__,
                error=str(exc)[:500],
                send_ts_ns=send_ts_ns,
                execution_style=style,
                entry_protection_metadata=entry_protection_metadata,
            )
        except BybitSubmissionUncertain:
            return None
        return self._batch_success_ack(
            command,
            market_input,
            result,
            send_ts_ns=send_ts_ns,
            execution_style=style,
            quote_price=quote_price,
            clip_qty=clip_qty,
            entry_protection_metadata=entry_protection_metadata,
        )

    def _batch_success_ack(
        self,
        command: OrderCommand,
        market_input: MarketInputRef,
        result: Mapping[str, Any],
        *,
        send_ts_ns: int,
        execution_style: str,
        quote_price: str | None,
        clip_qty: str | None,
        entry_protection_metadata: Mapping[str, Any],
    ) -> ExecutionObservation:
        """Mirror of the single-path success ack for one batch row."""

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
            verification = "deferred_resting_quote"
            if self.entry_quotes is not None and not idempotent_existing_order:
                decision_mid = None
                if (
                    market_input.bid_price is not None
                    and market_input.ask_price is not None
                    and market_input.bid_price > 0.0
                    and market_input.ask_price > market_input.bid_price
                ):
                    decision_mid = (market_input.bid_price + market_input.ask_price) / 2.0
                self.entry_quotes.register(
                    command_id=command.command_id,
                    symbol=command.symbol,
                    is_buy=command.signed_qty > 0.0,
                    price=float(quote_price),
                    decision_mid=decision_mid,
                )
        else:
            verification = self._verify_entry_attached_stop(
                command,
                acknowledged_ts_ns=local_ack_ts_ns,
            )
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
            "batch_submission": True,
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
        return ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=exchange_ack_ts_ns,
            local_receive_ts_ns=local_ack_ts_ns,
            accepted=True,
            venue_order_id=str(result.get("orderId") or ""),
            metadata=metadata,
        )

    def _batch_reject_ack(
        self,
        command: OrderCommand,
        *,
        error_type: str,
        error: str,
        send_ts_ns: int,
        execution_style: str,
        entry_protection_metadata: Mapping[str, Any],
    ) -> ExecutionObservation:
        """Mirror of the single-path definite-reject ack for one batch row."""

        # A refused create means what this process believes about the symbol
        # may be wrong; forget the leverage so the next attempt re-asserts it.
        self._venue_leverage.pop(command.symbol, None)
        return ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=0,
            local_receive_ts_ns=self.clock.wall_time_ns(),
            accepted=False,
            rejection_key=f"bybit-demo:{command.command_id}:place_order_failed",
            metadata={
                "local_socket_send_ts_ns": send_ts_ns,
                "exchange_ack_ts_status": "unavailable",
                "error_type": error_type,
                "error": error,
                "requested_leverage": command.leverage,
                "execution_style": execution_style,
                "batch_submission": True,
                **entry_protection_metadata,
            },
        )

    def _verify_entry_attached_stop(
        self,
        command: OrderCommand,
        *,
        acknowledged_ts_ns: int = 0,
    ) -> str:
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
                    acknowledged_ts_ns=acknowledged_ts_ns,
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
