"""Timestamped target-tape replay through the production account kernel.

This is the replacement interface for historical engines.  A strategy emits
the same target intents it would emit live at each causal market boundary.  The
replay feeds those inputs through :class:`AccountKernelRuntime`; it never accepts
a finished trade row and never synthesizes lifecycle events afterward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from liquidity_migration.account.account_contracts import (
    AccountEventType,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    AccountState,
    InstrumentRules,
    MarketInputRef,
    PositionState,
    TargetBatchResult,
)
from liquidity_migration.account.account_kernel import AccountExecutionKernel
from liquidity_migration.account.account_route import AccountRoute
from liquidity_migration.account.account_service import (
    AccountExecutionService,
    AccountServiceReceipt,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
    prepare_account_request_intents,
)
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.execution_adapters import (
    BookLevel, ExecutionTwinConfig, L2BookSnapshot, MarketOrderExecutionTwin,
)
from liquidity_migration.account.strategy_event_clock import (
    DeterministicEventClock,
    JsonlStrategyEventTape,
    StrategyEvent,
)
from liquidity_migration.account.strategy_runtime import AccountCycleResult, AccountKernelRuntime, AdaptedIntent
from liquidity_migration.account.strategy_targets import component_target_intent


@dataclass(frozen=True, slots=True)
class HistoricalReplayCycle:
    batch_id: str
    wall_ts_ns: int
    books: Mapping[str, L2BookSnapshot]
    intents: tuple[RequestedIntent, ...]
    risk_snapshot: AccountRiskSnapshot
    monotonic_ns: int | None = None
    market_inputs: Mapping[str, MarketInputRef] | None = None
    command_symbols: frozenset[str] | None = None
    require_strict_risk_reduction: bool = False
    request_content_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.batch_id or self.wall_ts_ns <= 0 or not self.books or not self.intents:
            raise ValueError("historical replay cycle requires batch, time, books, and intents")
        if self.monotonic_ns is not None and self.monotonic_ns < 0:
            raise ValueError("historical replay monotonic time cannot be negative")
        book_symbols = {str(symbol).upper() for symbol in self.books}
        if any(book.symbol.upper() != str(symbol).upper() for symbol, book in self.books.items()):
            raise ValueError("historical replay book keys must match book symbols")
        if self.market_inputs is not None:
            market_symbols = {str(symbol).upper() for symbol in self.market_inputs}
            if market_symbols != book_symbols:
                raise ValueError("exact market inputs and replay books must cover the same symbols")
            for symbol, market in self.market_inputs.items():
                if market.symbol.upper() != str(symbol).upper():
                    raise ValueError("historical replay market-input keys must match input symbols")
        if self.command_symbols is not None:
            normalized_commands = {str(symbol).upper() for symbol in self.command_symbols}
            if not normalized_commands or not normalized_commands.issubset(book_symbols):
                raise ValueError("historical replay command symbols require matching books")
        if type(self.require_strict_risk_reduction) is not bool:
            raise ValueError("historical replay strict-reduction mode must be a boolean")
        if self.request_content_hash is not None and (
            len(self.request_content_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.request_content_hash)
        ):
            raise ValueError("historical replay request hash must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class HistoricalTargetDecision:
    """One strategy-emitted target at its causal decision boundary."""

    wall_ts_ns: int
    intent: RequestedIntent
    reference_price: float
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.wall_ts_ns <= 0 or self.reference_price <= 0.0:
            raise ValueError("historical target decision requires positive time and price")


@dataclass(slots=True)
class _HistoricalServiceInputs:
    """Mutable deterministic ports read only by the production account owner."""

    instrument_rules: Mapping[str, InstrumentRules]
    market_inputs: dict[str, MarketInputRef] = field(default_factory=dict)
    risk_snapshot: AccountRiskSnapshot | None = None

    def bind(
        self,
        *,
        market_inputs: Mapping[str, MarketInputRef],
        risk_snapshot: AccountRiskSnapshot,
    ) -> None:
        self.market_inputs = {
            str(symbol).upper(): market
            for symbol, market in market_inputs.items()
        }
        self.risk_snapshot = risk_snapshot

    def current(
        self,
        symbols: Sequence[str],
        *,
        batch_id: str,
    ) -> Mapping[str, MarketInputRef]:
        del batch_id
        normalized = [str(symbol).upper() for symbol in symbols]
        missing = sorted(set(normalized) - set(self.market_inputs))
        if missing:
            raise RuntimeError(
                "historical account service lacks current markets for: "
                + ", ".join(missing)
            )
        return {symbol: self.market_inputs[symbol] for symbol in normalized}

    def current_snapshot(self, *, batch_id: str) -> AccountRiskSnapshot:
        del batch_id
        if self.risk_snapshot is None:
            raise RuntimeError("historical account service snapshot is not bound")
        return self.risk_snapshot

    def current_rules(
        self,
        symbols: Sequence[str],
    ) -> Mapping[str, InstrumentRules]:
        normalized = [str(symbol).upper() for symbol in symbols]
        missing = sorted(set(normalized) - set(self.instrument_rules))
        if missing:
            raise RuntimeError(
                "historical account service lacks instrument rules for: "
                + ", ".join(missing)
            )
        return {symbol: self.instrument_rules[symbol] for symbol in normalized}


@dataclass(frozen=True, slots=True)
class _HistoricalSnapshotProvider:
    inputs: _HistoricalServiceInputs

    def current(self, *, batch_id: str) -> AccountRiskSnapshot:
        return self.inputs.current_snapshot(batch_id=batch_id)


@dataclass(frozen=True, slots=True)
class _HistoricalRulesProvider:
    inputs: _HistoricalServiceInputs

    def current(self, symbols: Sequence[str]) -> Mapping[str, InstrumentRules]:
        return self.inputs.current_rules(symbols)


@dataclass(frozen=True, slots=True)
class HistoricalSubmissionFeedback:
    accepted: bool
    rejection_keys: tuple[str, ...]
    target_committed: bool


@dataclass(frozen=True, slots=True)
class HistoricalOwnerSubmission:
    """Immediate exact account-owner result without reconstructing trade rows."""

    receipt: AccountServiceReceipt
    feedback: HistoricalSubmissionFeedback
    command_ids: tuple[str, ...]


def historical_submission_feedback(
    outputs: Sequence[AccountCycleResult],
) -> HistoricalSubmissionFeedback:
    """Normalize immediate account risk and execution feedback."""

    rejection_keys = [
        key
        for output in outputs
        for key in output.target_result.rejection_keys
    ]
    for output in outputs:
        for event in output.execution_events:
            if (
                event.event_type == AccountEventType.ACK.value
                and event.payload.get("accepted") is False
            ):
                rejection_keys.append(
                    str(event.payload.get("rejection_key") or "execution_rejected")
                )
            elif event.event_type == AccountEventType.ORDER_STATUS.value:
                rejection_key = str(event.payload.get("rejection_key") or "")
                if rejection_key:
                    rejection_keys.append(rejection_key)
    normalized = tuple(sorted(set(rejection_keys)))
    return HistoricalSubmissionFeedback(
        accepted=not normalized,
        rejection_keys=normalized,
        target_committed=any(output.target_result.accepted for output in outputs),
    )


def neutralize_historical_decisions(
    decisions: Sequence[HistoricalTargetDecision],
    *,
    reason: str,
    source: str,
) -> tuple[HistoricalTargetDecision, ...]:
    """Build explicit zero replacements after committed entry execution rejects."""

    output: list[HistoricalTargetDecision] = []
    for decision in decisions:
        intent = decision.intent.intent
        adapter_kind = SleeveAdapterKind(decision.intent.adapter_kind)
        output.append(HistoricalTargetDecision(
            wall_ts_ns=decision.wall_ts_ns,
            reference_price=decision.reference_price,
            intent=component_target_intent(
                adapter_kind=adapter_kind,
                action="exit",
                decision_ts_ms=decision.wall_ts_ns // 1_000_000,
                strategy_id=intent.strategy_id,
                component_id=intent.component_id,
                symbol=intent.symbol,
                signed_notional_usdt=0.0,
                leverage=intent.leverage,
                reason=reason,
                metadata={
                    "source": source,
                    "owner_sleeve": adapter_kind.value,
                    "rejected_entry_decision_key": intent.decision_key,
                },
            ),
        ))
    return tuple(output)


def submit_historical_decisions(
    decisions: Sequence["HistoricalTargetDecision"],
    *,
    kernel_session: "HistoricalAccountSession | None",
    kernel_decision_sink: "list[HistoricalTargetDecision] | None",
    equity_usdt: float,
    batch_prefix: str,
    market_prices_for: "Callable[[set[str]], Mapping[str, float]]",
) -> tuple[bool, tuple[str, ...], bool]:
    """Submit one historical decision batch and report normalized feedback.

    Shared submission core for every historical engine.  The modeled decision
    reference is the executable book for a symbol being changed in this batch
    (for example a stop/TP price); supplying a bar close for that same symbol
    would create two contradictory books.  ``HistoricalAccountSession`` injects
    every decision reference itself, so ``market_prices_for`` receives the
    batch's upper-cased symbols and must return marks only for the other
    active symbols.
    """

    if not decisions:
        return True, (), False
    if kernel_decision_sink is not None:
        kernel_decision_sink.extend(decisions)
    if kernel_session is None:
        return True, (), False
    decision_symbols = {
        decision.intent.intent.symbol.upper() for decision in decisions
    }
    outputs = kernel_session.submit_decisions(
        list(decisions),
        equity_usdt=equity_usdt,
        batch_prefix=batch_prefix,
        market_prices=dict(market_prices_for(decision_symbols)),
    )
    feedback = historical_submission_feedback(outputs)
    return feedback.accepted, feedback.rejection_keys, feedback.target_committed


def neutralize_rejected_entry_decisions(
    decisions: Sequence["HistoricalTargetDecision"],
    *,
    source: str,
    error_label: str,
    submit: "Callable[[list[HistoricalTargetDecision]], tuple[bool, tuple[str, ...], bool]]",
) -> None:
    """Replace committed-but-execution-rejected entries with explicit zeros.

    An accepted account target followed by a rejected execution is not an open
    position, but leaving the desired target behind would silently retry or
    reopen it on a later unrelated cycle.
    """

    cancellations = neutralize_historical_decisions(
        decisions,
        reason="entry_execution_rejected",
        source=source,
    )
    accepted, rejection_keys, _ = submit(list(cancellations))
    if not accepted:
        raise RuntimeError(f"{error_label}: " + ", ".join(rejection_keys))


def synthetic_historical_rules_for_symbols(
    symbols: Sequence[str],
    *,
    max_leverage: float,
    observed_ts_ns: int,
) -> dict[str, InstrumentRules]:
    """Create explicitly synthetic rules before an online strategy session."""

    if max_leverage <= 0.0 or observed_ts_ns <= 0:
        raise ValueError("synthetic historical rules require positive leverage and observation time")
    return {
        symbol: InstrumentRules(
            symbol=symbol,
            qty_step=1e-12,
            min_qty=1e-12,
            min_notional=0.0,
            max_order_qty=1e15,
            max_leverage=float(max_leverage),
            source="synthetic_bar_replay_no_venue_rule_claim",
            environment="historical_synthetic",
            observed_ts_ns=int(observed_ts_ns),
        )
        for symbol in sorted({str(value).strip().upper() for value in symbols if str(value).strip()})
    }


def _active_kernel_symbols(state: AccountState) -> set[str]:
    """Symbols the kernel still owns a desired target, exposure, or working order on."""
    symbols = {
        str(target.get("symbol") or "").upper()
        for target in state.component_targets.values()
        if abs(float(target.get("signed_qty") or 0.0)) > 0.0
    }
    symbols.update(symbol for symbol, position in state.positions.items() if abs(position.signed_qty) > 0.0)
    symbols.update(state.working_symbols())
    return symbols


class HistoricalAccountSession:
    """Persistent chronological historical port around the production kernel.

    Returns each cycle's result immediately so strategy state can react to risk
    or execution facts before producing its next decision. One execution twin
    is retained for the whole session, preserving rate-limit and deterministic
    adapter state.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        account_id: str,
        risk_policy: AccountRiskPolicy,
        instrument_rules: Mapping[str, InstrumentRules],
        execution_config: ExecutionTwinConfig,
        id_seed: str,
        execution_id_seed: str | None = None,
        unsafe_single_process_inplace_research: bool = False,
        route: AccountRoute | None = None,
    ) -> None:
        self.root = Path(root)
        self.account_id = account_id
        self.risk_policy = risk_policy
        self.instrument_rules = {
            symbol.upper(): rules for symbol, rules in instrument_rules.items()
        }
        self.execution_config = execution_config
        self.id_seed = id_seed
        self.execution_id_seed = execution_id_seed or self.id_seed + ":execution"
        self.unsafe_single_process_inplace_research = bool(
            unsafe_single_process_inplace_research
        )
        if route is not None and (
            route.account_id != account_id
            or Path(route.account_root).resolve(strict=False)
            != self.root.resolve(strict=False)
        ):
            raise ValueError("historical account route does not match session root/account")
        self.route = route
        self.clock: VirtualClock | None = None
        self.event_clock: DeterministicEventClock[Any] | None = None
        self.kernel: AccountExecutionKernel | None = None
        self.runtime: AccountKernelRuntime | None = None
        self.account_service: AccountExecutionService | None = None
        self._service_inputs = _HistoricalServiceInputs(self.instrument_rules)
        self.execution_adapter = MarketOrderExecutionTwin(
            books={},
            instrument_rules=self.instrument_rules,
            config=self.execution_config,
            name="historical",
            id_seed=self.execution_id_seed,
        )
        self.outputs: list[AccountCycleResult] = []
        self._last_wall_ts_ns = 0
        self._last_monotonic_ns = -1
        self._synthetic_sequence = 0
        self._strategy_event_sequence = 0

    def _ensure_started(self, wall_ts_ns: int, monotonic_ns: int | None = None) -> None:
        if self.kernel is not None:
            return
        self.clock = VirtualClock(
            current_wall_ns=wall_ts_ns,
            current_monotonic_ns=0 if monotonic_ns is None else monotonic_ns,
        )
        recorder = JsonlStrategyEventTape(self.root / "strategy_event_tape.jsonl")
        self.event_clock = DeterministicEventClock(
            clock=self.clock,
            recorder=recorder,
        )
        self._strategy_event_sequence = max(
            (
                event.source_sequence
                for event in recorder.prior_events
                if event.source == f"historical:{self.account_id}"
            ),
            default=0,
        )
        self.kernel = AccountExecutionKernel(
            self.root,
            account_id=self.account_id,
            clock=self.clock,
            id_seed=self.id_seed,
            unsafe_single_process_inplace_research=(
                self.unsafe_single_process_inplace_research
            ),
        )
        self.runtime = AccountKernelRuntime(self.kernel)
        if self.route is not None:
            self.account_service = AccountExecutionService(
                route=self.route,
                kernel=self.kernel,
                market_provider=self._service_inputs,
                snapshot_provider=_HistoricalSnapshotProvider(self._service_inputs),
                rules_provider=_HistoricalRulesProvider(self._service_inputs),
                risk_policy=self.risk_policy,
                execution_adapter=self.execution_adapter,
                clock=self.clock,
                max_market_age_ns=self.execution_config.max_decision_age_ns,
                max_snapshot_age_ns=self.execution_config.max_decision_age_ns,
                convergence_retry_backoff_ns=0,
                max_convergence_retries=8,
            )

    def process_cycle(self, cycle: HistoricalReplayCycle) -> AccountCycleResult:
        if cycle.wall_ts_ns < self._last_wall_ts_ns:
            raise ValueError(
                "historical account session cannot move backward in wall time: "
                f"batch={cycle.batch_id!r} cycle={cycle.wall_ts_ns} "
                f"last={self._last_wall_ts_ns}"
            )
        if cycle.monotonic_ns is not None and cycle.monotonic_ns < self._last_monotonic_ns:
            raise ValueError(
                "historical account session cannot move backward in monotonic time: "
                f"batch={cycle.batch_id!r} cycle={cycle.monotonic_ns} "
                f"last={self._last_monotonic_ns}"
            )
        self._ensure_started(cycle.wall_ts_ns, cycle.monotonic_ns)
        assert self.clock is not None and self.runtime is not None and self.event_clock is not None
        exact_market_by_symbol = (
            {
                str(symbol).upper(): market
                for symbol, market in cycle.market_inputs.items()
            }
            if cycle.market_inputs is not None
            else {}
        )
        if cycle.monotonic_ns is not None:
            if cycle.wall_ts_ns < self.clock.current_wall_ns:
                raise ValueError("historical account session cannot retime an exact wall clock")
            if cycle.monotonic_ns < self.clock.current_monotonic_ns:
                raise ValueError("historical account session cannot retime an exact monotonic clock")
            # The captured journal records both clocks. Set the exact pair
            # before dispatch; dispatch then observes a zero wall-time delta.
            self.clock.current_wall_ns = cycle.wall_ts_ns
            self.clock.current_monotonic_ns = cycle.monotonic_ns
        self._strategy_event_sequence += 1
        event = StrategyEvent(
            event_ts_ns=cycle.wall_ts_ns,
            ingest_ts_ns=cycle.wall_ts_ns,
            source=f"historical:{self.account_id}",
            source_sequence=self._strategy_event_sequence,
            kind="market_boundary",
            payload={
                "batch_id": cycle.batch_id,
                "books": [
                    {
                        "symbol": symbol.upper(),
                        "sequence": book.sequence,
                        "exchange_ts_ns": book.exchange_ts_ns,
                        "input_key": (
                            exact_market_by_symbol[symbol.upper()].input_key
                            if symbol.upper() in exact_market_by_symbol else ""
                        ),
                    }
                    for symbol, book in sorted(cycle.books.items())
                ],
                "decision_keys": sorted(
                    item.intent.decision_key for item in cycle.intents
                ),
            },
        )
        return self.event_clock.dispatch(
            event,
            lambda _event: self._execute_cycle_event(cycle),
        )

    def _execute_cycle_event(self, cycle: HistoricalReplayCycle) -> AccountCycleResult:
        assert self.runtime is not None
        if cycle.market_inputs is None:
            market_inputs = {
                symbol.upper(): book.market_ref(
                    input_key=f"replay:{cycle.batch_id}:{symbol.upper()}:{book.sequence}",
                    source="historical_l2_tape",
                )
                for symbol, book in cycle.books.items()
            }
        else:
            market_inputs = {
                symbol.upper(): market for symbol, market in cycle.market_inputs.items()
            }
        self.execution_adapter.books = {
            symbol.upper(): book for symbol, book in cycle.books.items()
        }
        adapted = [AdaptedIntent(item.adapter(), item.intent) for item in cycle.intents]
        require_strict_risk_reduction = cycle.require_strict_risk_reduction
        if cycle.request_content_hash is not None and cycle.command_symbols is not None:
            assert self.kernel is not None
            preview_targets = [
                item.adapter.desired_target(
                    item.intent,
                    market_inputs[item.intent.symbol.upper()],
                    self.instrument_rules[item.intent.symbol.upper()],
                )
                for item in adapted
            ]
            require_strict_risk_reduction = (
                self.kernel.targets_are_strictly_risk_reducing(
                    preview_targets,
                    quantity_tolerance=self.risk_policy.quantity_tolerance,
                )
            )
        self._service_inputs.bind(
            market_inputs=market_inputs,
            risk_snapshot=cycle.risk_snapshot,
        )
        output = self.runtime.process_cycle(
            batch_id=cycle.batch_id,
            intents=adapted,
            market_inputs=market_inputs,
            risk_snapshot=cycle.risk_snapshot,
            risk_policy=self.risk_policy,
            instrument_rules=self.instrument_rules,
            execution_adapter=self.execution_adapter,
            command_symbols=cycle.command_symbols,
            require_strict_risk_reduction=require_strict_risk_reduction,
            request_content_hash=cycle.request_content_hash,
        )
        self.outputs.append(output)
        self._last_wall_ts_ns = cycle.wall_ts_ns
        self._last_monotonic_ns = self.clock.monotonic_ns() if self.clock is not None else -1
        return output

    def _targets_converged(self) -> bool:
        if self.kernel is None:
            return True
        state = self.kernel._state_ref()
        tolerance = self.risk_policy.quantity_tolerance
        symbols = set(state.aggregate_targets) | set(state.positions)
        symbols.update(state.working_symbols(tolerance=tolerance))
        return all(
            abs(
                float(state.aggregate_targets.get(symbol, 0.0))
                - float(
                    state.positions.get(symbol, PositionState()).signed_qty
                )
            )
            <= tolerance
            and state.working_order_count(symbol, tolerance=tolerance) == 0
            for symbol in symbols
        )

    def converge_until_stable(
        self,
        *,
        max_batches: int = 16,
    ) -> tuple[TargetBatchResult, ...]:
        """Drive the owner until converged or an explicit admission blocks it."""

        if max_batches <= 0:
            raise ValueError("historical convergence max_batches must be positive")
        if self._targets_converged():
            return ()
        if self.account_service is None:
            raise RuntimeError(
                "historical target state requires route-bound production convergence"
            )
        results: list[TargetBatchResult] = []
        for _ in range(max_batches):
            result = self.account_service.converge_once()
            if result is None:
                return tuple(results)
            results.append(result)
            if not result.accepted:
                # Residual convergence is entry-like, so a capital, health,
                # market, or rules gate may validly leave the accepted desired
                # state for a later owner pass. Not an exit failure.
                return tuple(results)
            state = self.kernel._state_ref() if self.kernel is not None else None
            execution_rejection_keys: list[str] = []
            if state is not None:
                for command in result.commands:
                    order = state.orders.get(command.command_id)
                    if order is not None and order.status == "rejected":
                        execution_rejection_keys.append(
                            order.rejection_key
                            or f"{order.command_id}:execution_status:{order.status}"
                        )
            execution_rejections = tuple(sorted(execution_rejection_keys))
            if execution_rejections:
                raise RuntimeError(
                    "historical account convergence execution rejected "
                    f"{result.batch_id!r}: {execution_rejections}"
                )
            if self._targets_converged():
                return tuple(results)
        raise RuntimeError(
            "historical account convergence exceeded its deterministic batch cap"
        )

    def submit_decisions(
        self,
        decisions: Sequence[HistoricalTargetDecision],
        *,
        equity_usdt: float,
        batch_prefix: str = "strategy-kernel",
        market_prices: Mapping[str, float] | None = None,
        exact_batch_id: str | None = None,
        request_content_hash: str | None = None,
        market_observed_ts_ns: int | None = None,
    ) -> tuple[AccountCycleResult, ...]:
        """Submit one causal timestamp's decisions and return immediate feedback."""

        if not decisions:
            raise ValueError("historical account session requires at least one decision")
        if equity_usdt <= 0.0:
            raise ValueError("historical account session equity must be positive")
        wall_times = {item.wall_ts_ns for item in decisions}
        if len(wall_times) != 1:
            raise ValueError("one online decision submission must share one wall timestamp")
        wall_ts_ns = next(iter(wall_times))
        market_ts_ns = (
            wall_ts_ns
            if market_observed_ts_ns is None
            else int(market_observed_ts_ns)
        )
        if market_ts_ns <= 0 or market_ts_ns > wall_ts_ns:
            raise ValueError(
                "historical market observation time must be positive and no later "
                "than request creation"
            )
        ordered = sorted(
            decisions,
            key=lambda item: (
                0 if item.intent.intent.signed_notional_usdt == 0.0 else 1,
                item.intent.intent.symbol,
                item.intent.intent.target_key,
                item.intent.intent.decision_key,
            ),
        )
        layers: list[list[HistoricalTargetDecision]] = []
        layer_keys: list[set[str]] = []
        for item in ordered:
            target_key = item.intent.intent.target_key
            for layer, keys in zip(layers, layer_keys):
                if target_key not in keys:
                    layer.append(item)
                    keys.add(target_key)
                    break
            else:
                layers.append([item])
                layer_keys.append({target_key})

        if exact_batch_id is not None:
            if not str(exact_batch_id).strip():
                raise ValueError("exact historical account batch id cannot be blank")
            if len(layers) != 1:
                raise ValueError(
                    "one immutable target request cannot require multiple target-replacement layers"
                )

        outputs: list[AccountCycleResult] = []
        for layer_index, layer in enumerate(layers, start=1):
            self._synthetic_sequence += 1
            sequence = self._synthetic_sequence
            required_symbols = {
                item.intent.intent.symbol.upper() for item in layer
            }
            if self.kernel is not None:
                # Internal read-only view. A deep copy per decision would make
                # a durable historical replay quadratic in prior orders.
                required_symbols.update(_active_kernel_symbols(self.kernel._state_ref()))
            prices = {
                str(symbol).upper(): float(price)
                for symbol, price in (market_prices or {}).items()
            }
            for item in layer:
                symbol = item.intent.intent.symbol.upper()
                decision_price = float(item.reference_price)
                supplied = prices.get(symbol)
                if supplied is not None and supplied != decision_price:
                    raise ValueError(
                        f"decision and account market prices differ for {symbol}: "
                        f"{decision_price:g} vs {supplied:g}"
                    )
                prices[symbol] = decision_price
            missing_prices = sorted(required_symbols - set(prices))
            if missing_prices:
                raise ValueError(
                    "online historical account batch lacks current prices for active symbols: "
                    + ", ".join(missing_prices)
                )
            books: dict[str, L2BookSnapshot] = {}
            for symbol in sorted(required_symbols):
                price = prices[symbol]
                books[symbol] = L2BookSnapshot(
                    symbol=symbol,
                    sequence=sequence,
                    previous_sequence=sequence - 1 if sequence > 1 else None,
                    exchange_ts_ns=market_ts_ns,
                    local_receive_ts_ns=market_ts_ns,
                    bids=(BookLevel(price, 1e15),),
                    asks=(BookLevel(price, 1e15),),
                )
            cycle = HistoricalReplayCycle(
                batch_id=(
                    str(exact_batch_id)
                    if exact_batch_id is not None
                    else f"{batch_prefix}/{wall_ts_ns}/{sequence}/{layer_index}"
                ),
                wall_ts_ns=wall_ts_ns,
                books=books,
                intents=tuple(item.intent for item in layer),
                risk_snapshot=AccountRiskSnapshot(
                    equity_usdt,
                    equity_usdt,
                    f"historical-fixed:{equity_usdt:g}:{wall_ts_ns}:{sequence}",
                    wall_ts_ns,
                ),
                command_symbols=(
                    frozenset(
                        item.intent.intent.symbol.upper()
                        for item in layer
                    )
                    if request_content_hash is not None
                    else None
                ),
                request_content_hash=request_content_hash,
            )
            outputs.append(self.process_cycle(cycle))
        return tuple(outputs)

    def submit_request(
        self,
        request: AccountTargetRequest,
        *,
        equity_usdt: float,
        market_prices: Mapping[str, float],
        market_observed_ts_ns: int | None = None,
    ) -> tuple[AccountCycleResult, ...]:
        """Replay one production-published request without losing its identity.

        The request's immutable batch id, created clock, intent order, and
        content hash flow into the production account kernel. Market prices are
        an explicit deterministic port; no decision price is reconstructed
        from a finished trade row.
        """

        if type(request) is not AccountTargetRequest:
            raise TypeError("historical account request replay requires AccountTargetRequest")
        if request.account_id != self.account_id:
            raise ValueError(
                "historical target request account does not match the replay session: "
                f"{request.account_id!r} != {self.account_id!r}"
            )
        normalized_prices = {
            str(symbol).upper(): float(price)
            for symbol, price in market_prices.items()
        }
        decisions: list[HistoricalTargetDecision] = []
        for item, prepared_intent in prepare_account_request_intents(request):
            symbol = prepared_intent.symbol.upper()
            try:
                reference_price = normalized_prices[symbol]
            except KeyError as exc:
                raise ValueError(
                    f"historical target request lacks a market price for {symbol}"
                ) from exc
            metadata_price = prepared_intent.metadata.get("decision_reference_price")
            if metadata_price is not None and float(metadata_price) != reference_price:
                raise ValueError(
                    f"published decision and historical market prices differ for {symbol}: "
                    f"{float(metadata_price):g} vs {reference_price:g}"
                )
            decisions.append(
                HistoricalTargetDecision(
                    wall_ts_ns=request.created_ts_ns,
                    intent=RequestedIntent(
                        adapter_kind=item.adapter_kind,
                        intent=prepared_intent,
                    ),
                    reference_price=reference_price,
                    metadata={
                        "request_id": request.request_id,
                        "request_content_hash": request.content_hash(),
                    },
                )
            )
        return self.submit_decisions(
            decisions,
            equity_usdt=equity_usdt,
            market_prices=normalized_prices,
            exact_batch_id=request.batch_id,
            request_content_hash=request.content_hash(),
            market_observed_ts_ns=market_observed_ts_ns,
        )

    def submit_request_via_owner(
        self,
        request: AccountTargetRequest,
        *,
        equity_usdt: float,
        market_prices: Mapping[str, float],
        market_observed_ts_ns: int | None = None,
    ) -> HistoricalOwnerSubmission:
        """Dispatch one published request through ``AccountExecutionService``."""

        if type(request) is not AccountTargetRequest:
            raise TypeError("historical owner replay requires AccountTargetRequest")
        if request.account_id != self.account_id:
            raise ValueError(
                "historical target request account does not match the replay session: "
                f"{request.account_id!r} != {self.account_id!r}"
            )
        if equity_usdt <= 0.0:
            raise ValueError("historical account session equity must be positive")
        if request.created_ts_ns < self._last_wall_ts_ns:
            raise ValueError(
                "historical account session cannot move backward in wall time: "
                f"batch={request.batch_id!r} cycle={request.created_ts_ns} "
                f"last={self._last_wall_ts_ns}"
            )
        market_ts_ns = (
            request.created_ts_ns
            if market_observed_ts_ns is None
            else int(market_observed_ts_ns)
        )
        if market_ts_ns <= 0 or market_ts_ns > request.created_ts_ns:
            raise ValueError(
                "historical market observation time must be positive and no later "
                "than request creation"
            )
        normalized_prices = {
            str(symbol).upper(): float(price)
            for symbol, price in market_prices.items()
        }
        for item in request.intents:
            symbol = item.intent.symbol.upper()
            if symbol not in normalized_prices:
                raise ValueError(
                    f"historical target request lacks a market price for {symbol}"
                )
            metadata_price = item.intent.metadata.get("decision_reference_price")
            if (
                metadata_price is not None
                and float(metadata_price) != normalized_prices[symbol]
            ):
                raise ValueError(
                    "published decision and historical market prices differ for "
                    f"{symbol}: {float(metadata_price):g} vs "
                    f"{normalized_prices[symbol]:g}"
                )

        self._ensure_started(request.created_ts_ns)
        assert self.kernel is not None
        assert self.clock is not None
        assert self.event_clock is not None
        if self.account_service is None or self.route is None:
            raise RuntimeError(
                "historical owner replay requires a verified account route"
            )
        account_service = self.account_service
        request.require_route(self.route)
        required_symbols = {item.intent.symbol.upper() for item in request.intents}
        required_symbols.update(_active_kernel_symbols(self.kernel._state_ref()))
        missing_prices = sorted(required_symbols - set(normalized_prices))
        if missing_prices:
            raise ValueError(
                "online historical account batch lacks current prices for active "
                "symbols: " + ", ".join(missing_prices)
            )

        self._synthetic_sequence += 1
        sequence = self._synthetic_sequence
        books = {
            symbol: L2BookSnapshot(
                symbol=symbol,
                sequence=sequence,
                previous_sequence=sequence - 1 if sequence > 1 else None,
                exchange_ts_ns=market_ts_ns,
                local_receive_ts_ns=market_ts_ns,
                bids=(BookLevel(normalized_prices[symbol], 1e15),),
                asks=(BookLevel(normalized_prices[symbol], 1e15),),
            )
            for symbol in sorted(required_symbols)
        }
        market_inputs = {
            symbol: book.market_ref(
                input_key=(
                    f"owner-replay:{request.batch_id}:{symbol}:{sequence}"
                ),
                source="historical_owner_hourly_close",
            )
            for symbol, book in books.items()
        }
        snapshot = AccountRiskSnapshot(
            equity_usdt,
            equity_usdt,
            (
                f"historical-owner-fixed:{equity_usdt:g}:"
                f"{request.created_ts_ns}:{sequence}"
            ),
            request.created_ts_ns,
        )
        self._service_inputs.bind(
            market_inputs=market_inputs,
            risk_snapshot=snapshot,
        )
        self.execution_adapter.books = books
        self._strategy_event_sequence += 1
        event = StrategyEvent(
            event_ts_ns=request.created_ts_ns,
            ingest_ts_ns=request.created_ts_ns,
            source=f"historical:{self.account_id}",
            source_sequence=self._strategy_event_sequence,
            kind="market_boundary",
            payload={
                "batch_id": request.batch_id,
                "request_id": request.request_id,
                "request_content_hash": request.content_hash(),
                "books": [
                    {
                        "symbol": symbol,
                        "sequence": book.sequence,
                        "exchange_ts_ns": book.exchange_ts_ns,
                        "input_key": market_inputs[symbol].input_key,
                    }
                    for symbol, book in sorted(books.items())
                ],
                "decision_keys": sorted(
                    item.intent.decision_key for item in request.intents
                ),
            },
        )
        receipt = self.event_clock.dispatch(
            event,
            lambda _event: account_service.handle(request),
        )
        if not isinstance(receipt, AccountServiceReceipt):
            raise RuntimeError("historical account owner returned an invalid receipt")
        self._last_wall_ts_ns = request.created_ts_ns
        self._last_monotonic_ns = self.clock.monotonic_ns()
        final_state = self.kernel._state_ref()
        rejection_keys = set(receipt.rejection_keys)
        for command_id in receipt.command_ids:
            order = final_state.orders.get(command_id)
            if order is not None and order.rejection_key:
                rejection_keys.add(order.rejection_key)
        feedback = HistoricalSubmissionFeedback(
            accepted=not rejection_keys,
            rejection_keys=tuple(sorted(rejection_keys)),
            target_committed=receipt.accepted,
        )
        return HistoricalOwnerSubmission(
            receipt=receipt,
            feedback=feedback,
            command_ids=receipt.command_ids,
        )

    @property
    def final_state_hash(self) -> str:
        if self.kernel is None:
            kernel = AccountExecutionKernel(
                self.root,
                account_id=self.account_id,
                id_seed=self.id_seed,
            )
            return kernel._state_ref().state_hash()
        return self.kernel._state_ref().state_hash()
