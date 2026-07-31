"""Paper-only passive-execution A/B adapter (registered experiment arm B).

Implements `docs/research_findings.md` on the paper owner: eligible CONTINUOUS
entry commands are assigned by component (trade) id hash parity to arm A (the
unchanged market-IOC twin) or arm B (post-only limit at the touch, re-pegged on
touch moves, falling back to a market walk after the frozen timeout or an adverse
move through the initial limit). Everything else — exits, reductions,
non-CONTINUOUS sleeves, unresolvable components — takes arm A unchanged.

Fill model:

- Strict crossing: the opposite touch must reach the resting limit price. No
  queue-position credit for level exhaustion, so arm B's fill rate is
  understated.
- A passive fill takes the full remaining quantity at the limit; the registered
  metrics carry the touch quantity so size effects stay checkable.
- The fallback market walk reproduces the arm-A twin exactly (same taker fee,
  residual slippage, depth policy), so the arms differ only in the passive
  attempt.

Pending state is in-memory. On restart the paper owner cancels every non-terminal
working order (`recover_orphaned_working_orders`) and the convergence loop
re-plans, so nothing is left phantom-working.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from liquidity_migration.account.account_contracts import AccountState, MarketInputRef, OrderCommand
from liquidity_migration.core.deterministic_runtime import Clock, DeterministicIds
from liquidity_migration.account.execution_adapters import (
    ExecutionObservation,
    ExecutionObservationType,
    L2BookSnapshot,
    MarketOrderExecutionTwin,
    _market_input_validation_error,
)

# Frozen experiment parameters (changing them is a new registration).
PASSIVE_TIMEOUT_NS = 20_000_000_000
PASSIVE_CHASE_THRESHOLD_BPS = 10.0
PASSIVE_MAKER_FEE_BPS = 2.0
EXECUTION_ARM_METADATA_KEY = "execution_arm"


def execution_arm_for_component(component_id: str) -> str:
    """Deterministic arm assignment by trade-id hash parity (registered)."""

    digest = hashlib.sha256(component_id.encode("utf-8")).digest()
    return "B" if digest[-1] & 1 else "A"


def _with_arm_metadata(
    observation: ExecutionObservation,
    *,
    arm: str,
    eligible: bool,
) -> ExecutionObservation:
    return replace(
        observation,
        metadata={
            **dict(observation.metadata),
            EXECUTION_ARM_METADATA_KEY: arm,
            "execution_arm_eligible": eligible,
        },
    )


@dataclass(slots=True)
class _PendingPassiveOrder:
    command: OrderCommand
    component_id: str
    limit_price: float
    initial_limit_price: float
    placed_ts_ns: int
    deadline_ns: int
    repeg_count: int = 0
    filled_qty: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class PassivePaperExecutionAdapter:
    """Paper execution adapter running the registered passive A/B experiment."""

    def __init__(
        self,
        *,
        market_provider: Any,
        twin: MarketOrderExecutionTwin,
        component_resolver: Callable[[OrderCommand], tuple[str, str]],
        clock: Clock,
        timeout_ns: int = PASSIVE_TIMEOUT_NS,
        chase_threshold_bps: float = PASSIVE_CHASE_THRESHOLD_BPS,
        maker_fee_bps: float = PASSIVE_MAKER_FEE_BPS,
        reduce_only_max_decision_age_ns: int | None = None,
    ) -> None:
        self.name = twin.name
        self.market_provider = market_provider
        self.twin = twin
        self.component_resolver = component_resolver
        self.clock = clock
        self.timeout_ns = int(timeout_ns)
        self.chase_threshold_bps = float(chase_threshold_bps)
        self.maker_fee_bps = float(maker_fee_bps)
        if (
            reduce_only_max_decision_age_ns is not None
            and (
                isinstance(reduce_only_max_decision_age_ns, bool)
                or not isinstance(reduce_only_max_decision_age_ns, int)
            )
        ):
            raise TypeError("reduce-only decision-age limit must be integer nanoseconds")
        if (
            reduce_only_max_decision_age_ns is not None
            and reduce_only_max_decision_age_ns < twin.config.max_decision_age_ns
        ):
            raise ValueError(
                "reduce-only decision-age limit cannot be below the entry decision-age limit"
            )
        self.reduce_only_max_decision_age_ns = reduce_only_max_decision_age_ns
        self.ids = DeterministicIds(f"{twin.name}:passive-arm-b")
        self._pending: dict[str, _PendingPassiveOrder] = {}

    # ------------------------------------------------------------------ submit

    def submit(
        self,
        command: OrderCommand,
        market_input: MarketInputRef,
    ) -> Iterable[ExecutionObservation]:
        book = self.market_provider.execution_book(market_input.input_key)
        self.twin.books[command.symbol.upper()] = book
        component_id, sleeve = self.component_resolver(command)
        eligible = (
            bool(component_id)
            and sleeve == "continuous"
            and not command.reduce_only
        )
        arm = execution_arm_for_component(component_id) if eligible else "A"
        if arm == "A":
            tagged: list[ExecutionObservation] = []
            submit_kwargs: dict[str, Any] = {}
            if command.reduce_only and self.reduce_only_max_decision_age_ns is not None:
                submit_kwargs = {
                    "decision_age_limit_ns": self.reduce_only_max_decision_age_ns,
                    "decision_age_limit_source": "paper_owner_market_freshness",
                }
            for observation in self.twin.submit(command, market_input, **submit_kwargs):
                if not isinstance(observation, ExecutionObservation):
                    raise TypeError("paper twin must yield ExecutionObservation values")
                tagged.append(_with_arm_metadata(observation, arm="A", eligible=eligible))
            return tuple(tagged)
        return self._submit_passive(
            command,
            market_input,
            book=book,
            component_id=component_id,
        )

    def _rejection(self, command: OrderCommand, *, reason: str, now_ns: int) -> tuple[ExecutionObservation, ...]:
        return (
            _with_arm_metadata(
                ExecutionObservation(
                    observation_type=ExecutionObservationType.ACK,
                    command_id=command.command_id,
                    exchange_ts_ns=now_ns,
                    local_receive_ts_ns=now_ns,
                    accepted=False,
                    rejection_key=f"execution:{command.command_id}:{reason}",
                    metadata={
                        "reason": reason,
                        "execution_model_scope": self.twin.config.model_scope,
                    },
                ),
                arm="B",
                eligible=True,
            ),
        )

    def _submit_passive(
        self,
        command: OrderCommand,
        market_input: MarketInputRef,
        *,
        book: L2BookSnapshot,
        component_id: str,
    ) -> tuple[ExecutionObservation, ...]:
        now_ns = self.clock.wall_time_ns()
        rules = self.twin.instrument_rules.get(command.symbol.upper())
        book_error = book.validation_error(expected_symbol=command.symbol.upper())
        if rules is None or book_error:
            return self._rejection(
                command,
                reason=book_error or "missing_book_or_rules",
                now_ns=now_ns,
            )
        market_error = _market_input_validation_error(
            market_input, expected_symbol=command.symbol.upper()
        )
        if market_error:
            return self._rejection(command, reason=market_error, now_ns=now_ns)
        if command.created_ts_ns <= 0:
            raise ValueError("passive command requires an explicit positive created_ts_ns")
        decision_age_ns = command.created_ts_ns - market_input.local_receive_ts_ns
        if decision_age_ns < 0:
            return self._rejection(command, reason="future_decision_book", now_ns=now_ns)
        if decision_age_ns > self.twin.config.max_decision_age_ns:
            return self._rejection(command, reason="stale_decision", now_ns=now_ns)
        if command.qty < rules.min_qty:
            return self._rejection(command, reason="quantity_rule", now_ns=now_ns)
        if not command.reduce_only and command.qty * command.reference_price < rules.min_notional:
            return self._rejection(command, reason="minimum_notional", now_ns=now_ns)

        buy = command.signed_qty > 0.0
        limit_price = book.bids[0].price if buy else book.asks[0].price
        touch_qty = book.bids[0].qty if buy else book.asks[0].qty
        venue_order_id = self.ids.make("passive-order", command.command_id)
        ack = ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=now_ns,
            local_receive_ts_ns=now_ns,
            accepted=True,
            venue_order_id=venue_order_id,
            metadata={
                "passive_limit_price": limit_price,
                "passive_touch_qty": touch_qty,
                "passive_deadline_ns": now_ns + self.timeout_ns,
                "passive_chase_threshold_bps": self.chase_threshold_bps,
                "book_sequence": book.sequence,
                "execution_model_scope": self.twin.config.model_scope,
            },
        )
        self._pending[command.command_id] = _PendingPassiveOrder(
            command=command,
            component_id=component_id,
            limit_price=limit_price,
            initial_limit_price=limit_price,
            placed_ts_ns=now_ns,
            deadline_ns=now_ns + self.timeout_ns,
        )
        return (_with_arm_metadata(ack, arm="B", eligible=True),)

    # -------------------------------------------------------------------- poll

    def pending_count(self) -> int:
        return len(self._pending)

    def poll(self, *, now_ns: int | None = None) -> tuple[ExecutionObservation, ...]:
        """Advance every pending passive order against the current live book."""

        clock_ns = self.clock.wall_time_ns() if now_ns is None else int(now_ns)
        observations: list[ExecutionObservation] = []
        for command_id in list(self._pending):
            pending = self._pending[command_id]
            symbol = pending.command.symbol.upper()
            book = self.market_provider.recorder.current_book(symbol)
            usable = book is not None and not book.validation_error(expected_symbol=symbol)
            if not usable:
                if clock_ns >= pending.deadline_ns:
                    observations.extend(self._cancel(pending, reason="passive_no_live_book", now_ns=clock_ns))
                continue
            assert book is not None
            buy = pending.command.signed_qty > 0.0
            best_bid = book.bids[0].price
            best_ask = book.asks[0].price
            crossed = (best_ask <= pending.limit_price) if buy else (best_bid >= pending.limit_price)
            if crossed:
                observations.extend(self._passive_fill(pending, book=book, now_ns=clock_ns))
                continue
            adverse_bound = (
                pending.initial_limit_price * (1.0 + self.chase_threshold_bps / 10_000.0)
                if buy
                else pending.initial_limit_price * (1.0 - self.chase_threshold_bps / 10_000.0)
            )
            chased = (best_ask >= adverse_bound) if buy else (best_bid <= adverse_bound)
            if clock_ns >= pending.deadline_ns or chased:
                observations.extend(
                    self._fallback_fill(
                        pending,
                        book=book,
                        now_ns=clock_ns,
                        reason="chase" if chased and clock_ns < pending.deadline_ns else "timeout",
                    )
                )
                continue
            touch = best_bid if buy else best_ask
            if touch != pending.limit_price:
                pending.limit_price = touch
                pending.repeg_count += 1
        return tuple(observations)

    def _experiment_metadata(
        self,
        pending: _PendingPassiveOrder,
        *,
        passive_fill: bool,
        fallback_reason: str,
        now_ns: int,
    ) -> dict[str, Any]:
        return {
            "passive_fill": passive_fill,
            "passive_limit_price": pending.limit_price,
            "passive_initial_limit_price": pending.initial_limit_price,
            "passive_repeg_count": pending.repeg_count,
            "passive_fallback_reason": fallback_reason,
            "passive_time_to_terminal_ns": now_ns - pending.placed_ts_ns,
            "execution_model_scope": self.twin.config.model_scope,
        }

    def _passive_fill(
        self,
        pending: _PendingPassiveOrder,
        *,
        book: L2BookSnapshot,
        now_ns: int,
    ) -> tuple[ExecutionObservation, ...]:
        command = pending.command
        remaining = command.qty - pending.filled_qty
        signed_qty = math.copysign(remaining, command.signed_qty)
        notional = remaining * pending.limit_price
        fill = ExecutionObservation(
            observation_type=ExecutionObservationType.FILL,
            command_id=command.command_id,
            exchange_ts_ns=book.exchange_ts_ns,
            local_receive_ts_ns=max(book.local_receive_ts_ns, now_ns),
            execution_id=self.ids.make("passive-fill", command.command_id),
            signed_qty=signed_qty,
            price=pending.limit_price,
            fee_usdt=notional * self.maker_fee_bps / 10_000.0,
            metadata={
                **self._experiment_metadata(
                    pending, passive_fill=True, fallback_reason="", now_ns=now_ns
                ),
                "maker_fee_bps": self.maker_fee_bps,
                "book_sequence": book.sequence,
            },
        )
        del self._pending[command.command_id]
        return (_with_arm_metadata(fill, arm="B", eligible=True),)

    def _fallback_fill(
        self,
        pending: _PendingPassiveOrder,
        *,
        book: L2BookSnapshot,
        now_ns: int,
        reason: str,
    ) -> tuple[ExecutionObservation, ...]:
        command = pending.command
        buy = command.signed_qty > 0.0
        levels = (
            book.asks[: self.twin.config.visible_book_depth]
            if buy
            else book.bids[: self.twin.config.visible_book_depth]
        )
        remaining = command.qty - pending.filled_qty
        direction = 1.0 if buy else -1.0
        observations: list[ExecutionObservation] = []
        experiment = self._experiment_metadata(
            pending, passive_fill=False, fallback_reason=reason, now_ns=now_ns
        )
        for index, level in enumerate(levels):
            if remaining <= 1e-12:
                break
            fill_qty = min(remaining, level.qty)
            remaining -= fill_qty
            pending.filled_qty += fill_qty
            fill_price = level.price * (
                1.0 + direction * self.twin.config.residual_adverse_slippage_bps / 10_000.0
            )
            if fill_price <= 0.0 or not math.isfinite(fill_price):
                raise ValueError("passive fallback produced an invalid fill price")
            observations.append(
                ExecutionObservation(
                    observation_type=ExecutionObservationType.FILL,
                    command_id=command.command_id,
                    exchange_ts_ns=book.exchange_ts_ns + index,
                    local_receive_ts_ns=max(book.local_receive_ts_ns, now_ns),
                    execution_id=self.ids.make(
                        "passive-fallback-fill", command.command_id, str(index)
                    ),
                    signed_qty=math.copysign(fill_qty, command.signed_qty),
                    price=fill_price,
                    fee_usdt=fill_qty * fill_price * self.twin.config.fee_bps / 10_000.0,
                    metadata={
                        **experiment,
                        "taker_fee_bps": self.twin.config.fee_bps,
                        "residual_adverse_slippage_bps": self.twin.config.residual_adverse_slippage_bps,
                        "book_sequence": book.sequence,
                    },
                )
            )
        if remaining > 1e-12:
            status = "partially_filled_cancelled" if pending.filled_qty > 1e-12 else "cancelled"
            observations.append(
                ExecutionObservation(
                    observation_type=ExecutionObservationType.ORDER_STATUS,
                    command_id=command.command_id,
                    exchange_ts_ns=book.exchange_ts_ns + len(levels),
                    local_receive_ts_ns=max(book.local_receive_ts_ns, now_ns),
                    status=status,
                    cumulative_filled_qty=pending.filled_qty,
                    rejection_key=f"execution:{command.command_id}:passive_fallback_no_liquidity",
                    metadata=experiment,
                )
            )
        del self._pending[command.command_id]
        return tuple(
            _with_arm_metadata(obs, arm="B", eligible=True) for obs in observations
        )

    def _cancel(
        self,
        pending: _PendingPassiveOrder,
        *,
        reason: str,
        now_ns: int,
    ) -> tuple[ExecutionObservation, ...]:
        command = pending.command
        status = "partially_filled_cancelled" if pending.filled_qty > 1e-12 else "cancelled"
        observation = ExecutionObservation(
            observation_type=ExecutionObservationType.ORDER_STATUS,
            command_id=command.command_id,
            exchange_ts_ns=now_ns,
            local_receive_ts_ns=now_ns,
            status=status,
            cumulative_filled_qty=pending.filled_qty,
            rejection_key=f"execution:{command.command_id}:{reason}",
            metadata=self._experiment_metadata(
                pending, passive_fill=False, fallback_reason=reason, now_ns=now_ns
            ),
        )
        del self._pending[command.command_id]
        return (_with_arm_metadata(observation, arm="B", eligible=True),)


def recover_orphaned_working_orders(
    state: AccountState,
    *,
    now_ns: int,
    tolerance: float = 0.0,
) -> tuple[ExecutionObservation, ...]:
    """Terminal-cancel paper working orders left by a restart.

    Passive pending state is in-memory, so after a restart any non-terminal
    acknowledged order has no live manager. A blanket cancel is exact here and
    the convergence loop re-plans the remainder.
    """

    observations: list[ExecutionObservation] = []
    for command_id, order in sorted(state.orders.items()):
        if order.status not in {"acknowledged", "partially_filled"}:
            continue
        if abs(order.remaining_signed_qty) <= tolerance:
            continue
        filled = abs(order.filled_signed_qty)
        observations.append(
            ExecutionObservation(
                observation_type=ExecutionObservationType.ORDER_STATUS,
                command_id=command_id,
                exchange_ts_ns=now_ns,
                local_receive_ts_ns=now_ns,
                status="partially_filled_cancelled" if filled > 1e-12 else "cancelled",
                cumulative_filled_qty=filled,
                rejection_key=f"execution:{command_id}:paper_restart_recovery_cancel",
                metadata={"reason": "paper_restart_recovery_cancel"},
            )
        )
    return tuple(observations)
