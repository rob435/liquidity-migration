"""Timestamped target-tape replay through the production account kernel.

This is the replacement interface for historical engines.  A strategy emits
the same target intents it would emit live at each causal market boundary.  The
replay feeds those inputs through :class:`AccountKernelRuntime`; it never accepts
a finished trade row and never synthesizes lifecycle events afterward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict
from typing import Mapping, Sequence

from .account_kernel import (
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
    TargetBatchResult,
)
from .account_service import RequestedIntent, SleeveAdapterKind
from .deterministic_runtime import VirtualClock
from .execution_adapters import ExecutionTwinConfig, L2BookSnapshot, MarketOrderExecutionTwin
from .execution_adapters import BookLevel
from .strategy_event_clock import (
    DeterministicEventClock,
    JsonlStrategyEventTape,
    StrategyEvent,
)
from .strategy_runtime import AccountCycleResult, AccountKernelRuntime, AdaptedIntent
from .strategy_targets import component_target_intent


@dataclass(frozen=True, slots=True)
class HistoricalReplayCycle:
    batch_id: str
    wall_ts_ns: int
    books: Mapping[str, L2BookSnapshot]
    intents: tuple[RequestedIntent, ...]
    risk_snapshot: AccountRiskSnapshot

    def __post_init__(self) -> None:
        if not self.batch_id or self.wall_ts_ns <= 0 or not self.books or not self.intents:
            raise ValueError("historical replay cycle requires batch, time, books, and intents")


@dataclass(frozen=True, slots=True)
class HistoricalReplayResult:
    batches: tuple[TargetBatchResult, ...]
    final_state_hash: str
    event_tape_hash: str


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


@dataclass(frozen=True, slots=True)
class HistoricalSubmissionFeedback:
    accepted: bool
    rejection_keys: tuple[str, ...]
    target_committed: bool


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


def historical_cycles_from_decisions(
    decisions: Sequence[HistoricalTargetDecision],
    *,
    equity_usdt: float,
) -> tuple[HistoricalReplayCycle, ...]:
    """Group actual strategy decisions into atomic account-time batches."""

    if equity_usdt <= 0.0:
        raise ValueError("historical replay equity must be positive")
    grouped: dict[int, list[HistoricalTargetDecision]] = defaultdict(list)
    for decision in decisions:
        grouped[decision.wall_ts_ns].append(decision)
    cycles: list[HistoricalReplayCycle] = []
    ordinal = 0
    for wall_ts_ns in sorted(grouped):
        ordered = sorted(
            grouped[wall_ts_ns],
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
            placed = False
            for layer, keys in zip(layers, layer_keys):
                if target_key not in keys:
                    layer.append(item)
                    keys.add(target_key)
                    placed = True
                    break
            if not placed:
                layers.append([item])
                layer_keys.append({target_key})
        for layer in layers:
            ordinal += 1
            books: dict[str, L2BookSnapshot] = {}
            for item in layer:
                symbol = item.intent.intent.symbol.upper()
                price = float(item.reference_price)
                prior = books.get(symbol)
                if prior is not None and prior.bids[0].price != price:
                    raise ValueError(
                        "same-symbol decisions in one atomic historical batch must "
                        f"share a reference price: {symbol} has "
                        f"{prior.bids[0].price:g} and {price:g}"
                    )
                books[symbol] = L2BookSnapshot(
                    symbol=symbol,
                    sequence=ordinal,
                    previous_sequence=ordinal - 1 if ordinal > 1 else None,
                    exchange_ts_ns=wall_ts_ns,
                    local_receive_ts_ns=wall_ts_ns,
                    bids=(BookLevel(price, 1e15),),
                    asks=(BookLevel(price, 1e15),),
                )
            batch_id = f"strategy-kernel/{wall_ts_ns}/{ordinal}"
            cycles.append(HistoricalReplayCycle(
                batch_id=batch_id,
                wall_ts_ns=wall_ts_ns,
                books=books,
                intents=tuple(item.intent for item in layer),
                risk_snapshot=AccountRiskSnapshot(
                    equity_usdt,
                    equity_usdt,
                    f"historical-fixed:{equity_usdt:g}:{wall_ts_ns}:{ordinal}",
                    wall_ts_ns,
                ),
            ))
    return tuple(cycles)


def synthetic_historical_rules(
    decisions: Sequence[HistoricalTargetDecision],
) -> dict[str, InstrumentRules]:
    """Numerically fine execution rules for bar-only historical tapes.

    These are explicitly synthetic and cannot support a venue-rule parity claim.
    They prevent bar replay from inventing a local notional floor; demo/paper
    parity must use demo-verified rules instead.
    """

    leverage_by_symbol: dict[str, float] = {}
    observed_by_symbol: dict[str, int] = {}
    for item in decisions:
        symbol = item.intent.intent.symbol.upper()
        leverage_by_symbol[symbol] = max(
            leverage_by_symbol.get(symbol, 1.0),
            float(item.intent.intent.leverage),
            1.0,
        )
        observed_by_symbol[symbol] = max(
            observed_by_symbol.get(symbol, 0), item.wall_ts_ns
        )
    return {
        symbol: InstrumentRules(
            symbol=symbol,
            qty_step=1e-12,
            min_qty=1e-12,
            min_notional=0.0,
            max_order_qty=1e15,
            max_leverage=max_leverage,
            source="synthetic_bar_replay_no_venue_rule_claim",
            environment="historical_synthetic",
            observed_ts_ns=observed_by_symbol[symbol],
        )
        for symbol, max_leverage in sorted(leverage_by_symbol.items())
    }


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


class HistoricalAccountReplay:
    """Causal common-kernel replay; only the execution adapter is simulated."""

    def __init__(
        self,
        root: str | Path,
        *,
        account_id: str,
        risk_policy: AccountRiskPolicy,
        instrument_rules: Mapping[str, InstrumentRules],
        execution_config: ExecutionTwinConfig,
        id_seed: str,
    ) -> None:
        self.root = Path(root)
        self.account_id = account_id
        self.risk_policy = risk_policy
        self.instrument_rules = dict(instrument_rules)
        self.execution_config = execution_config
        self.id_seed = id_seed

    def run(self, cycles: Sequence[HistoricalReplayCycle]) -> HistoricalReplayResult:
        ordered = list(cycles)
        if any(right.wall_ts_ns < left.wall_ts_ns for left, right in zip(ordered, ordered[1:])):
            raise ValueError("historical replay cycles must be ordered by non-decreasing wall time")
        session = HistoricalAccountSession(
            self.root,
            account_id=self.account_id,
            risk_policy=self.risk_policy,
            instrument_rules=self.instrument_rules,
            execution_config=self.execution_config,
            id_seed=self.id_seed,
        )
        for cycle in ordered:
            session.process_cycle(cycle)
        return HistoricalReplayResult(
            batches=tuple(output.target_result for output in session.outputs),
            final_state_hash=session.final_state_hash,
            event_tape_hash=session.event_tape_hash,
        )


class HistoricalAccountSession:
    """Persistent chronological historical port around the production kernel.

    Unlike :class:`HistoricalAccountReplay`, this surface returns each cycle's
    result immediately so strategy state can react to risk or execution facts
    before producing its next decision. One execution twin is retained for the
    whole session, preserving rate-limit and deterministic adapter state.
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
    ) -> None:
        self.root = Path(root)
        self.account_id = account_id
        self.risk_policy = risk_policy
        self.instrument_rules = {
            symbol.upper(): rules for symbol, rules in instrument_rules.items()
        }
        self.execution_config = execution_config
        self.id_seed = id_seed
        self.clock: VirtualClock | None = None
        self.event_clock: DeterministicEventClock[AccountCycleResult] | None = None
        self.kernel: AccountExecutionKernel | None = None
        self.runtime: AccountKernelRuntime | None = None
        self.execution_adapter = MarketOrderExecutionTwin(
            books={},
            instrument_rules=self.instrument_rules,
            config=self.execution_config,
            name="historical",
            id_seed=self.id_seed + ":execution",
        )
        self.outputs: list[AccountCycleResult] = []
        self._last_wall_ts_ns = 0
        self._synthetic_sequence = 0
        self._strategy_event_sequence = 0

    def _ensure_started(self, wall_ts_ns: int) -> None:
        if self.kernel is not None:
            return
        self.clock = VirtualClock(current_wall_ns=wall_ts_ns, current_monotonic_ns=0)
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
        )
        self.runtime = AccountKernelRuntime(self.kernel)

    def process_cycle(self, cycle: HistoricalReplayCycle) -> AccountCycleResult:
        if cycle.wall_ts_ns < self._last_wall_ts_ns:
            raise ValueError("historical account session cannot move backward in wall time")
        self._ensure_started(cycle.wall_ts_ns)
        assert self.clock is not None and self.runtime is not None and self.event_clock is not None
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
        market_inputs = {
            symbol.upper(): book.market_ref(
                input_key=f"replay:{cycle.batch_id}:{symbol.upper()}:{book.sequence}",
                source="historical_l2_tape",
            )
            for symbol, book in cycle.books.items()
        }
        self.execution_adapter.books = {
            symbol.upper(): book for symbol, book in cycle.books.items()
        }
        output = self.runtime.process_cycle(
            batch_id=cycle.batch_id,
            intents=[AdaptedIntent(item.adapter(), item.intent) for item in cycle.intents],
            market_inputs=market_inputs,
            risk_snapshot=cycle.risk_snapshot,
            risk_policy=self.risk_policy,
            instrument_rules=self.instrument_rules,
            execution_adapter=self.execution_adapter,
        )
        self.outputs.append(output)
        self._last_wall_ts_ns = cycle.wall_ts_ns
        return output

    def submit_decisions(
        self,
        decisions: Sequence[HistoricalTargetDecision],
        *,
        equity_usdt: float,
        batch_prefix: str = "strategy-kernel",
        market_prices: Mapping[str, float] | None = None,
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
                state = self.kernel._state_ref()
                required_symbols.update(
                    str(target.get("symbol") or "").upper()
                    for target in state.component_targets.values()
                    if abs(float(target.get("signed_qty") or 0.0)) > 0.0
                )
                required_symbols.update(
                    symbol
                    for symbol, position in state.positions.items()
                    if abs(position.signed_qty) > 0.0
                )
                required_symbols.update(state.working_symbols())
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
                    exchange_ts_ns=wall_ts_ns,
                    local_receive_ts_ns=wall_ts_ns,
                    bids=(BookLevel(price, 1e15),),
                    asks=(BookLevel(price, 1e15),),
                )
            cycle = HistoricalReplayCycle(
                batch_id=f"{batch_prefix}/{wall_ts_ns}/{sequence}/{layer_index}",
                wall_ts_ns=wall_ts_ns,
                books=books,
                intents=tuple(item.intent for item in layer),
                risk_snapshot=AccountRiskSnapshot(
                    equity_usdt,
                    equity_usdt,
                    f"historical-fixed:{equity_usdt:g}:{wall_ts_ns}:{sequence}",
                    wall_ts_ns,
                ),
            )
            outputs.append(self.process_cycle(cycle))
        return tuple(outputs)

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

    @property
    def event_tape_hash(self) -> str:
        if self.event_clock is None:
            return JsonlStrategyEventTape(
                self.root / "strategy_event_tape.jsonl"
            ).tape_hash
        return self.event_clock.tape_hash
