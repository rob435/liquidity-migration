"""Deterministic target-only driver for a bounded demo execution sample.

The driver has no venue client and accepts no credentials.  It publishes one
small HEDGE-adapter component at a time through the canonical account inbox,
waits for the sole owner to converge, then publishes the matching flat target.
Its hash-chained strategy-event tape is replayable by paper/historical tools.

This is execution calibration evidence.  It is deliberately not LONG or
CONTINUOUS strategy-parity evidence and cannot authorize deployment by itself.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

from .account_intent_client import AccountTargetPublisher
from .account_kernel import AccountExecutionKernel, AccountState
from .account_owner_health import (
    TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    require_recent_account_owner_health,
)
from .account_route import AccountRoute
from .account_service import AccountServiceReceipt, SleeveAdapterKind
from .deterministic_runtime import Clock, SystemClock
from .deterministic_serialization import canonical_json
from .strategy_event_clock import (
    DeterministicEventClock,
    JsonlStrategyEventTape,
    StrategyEvent,
)
from .strategy_targets import component_target_intent


CALIBRATION_PLAN_SCHEMA_VERSION = 1
CALIBRATION_EVENT_SOURCE = "demo-execution-calibration-v1"
CALIBRATION_STRATEGY_ID = "execution-calibration-v1"
REGISTERED_CALIBRATION_PLAN_ID = "demo-calibration-20260714-v5"
REGISTERED_CALIBRATION_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BUSDT")
REGISTERED_ROUND_TRIPS_PER_SYMBOL = 5
REGISTERED_NOTIONAL_USDT = 160.0
REGISTERED_LEVERAGE = 2.0
REGISTERED_HOLD_SECONDS = 1.0
REGISTERED_FUNDING_SYMBOL = "BTCUSDT"
REGISTERED_MIN_NOTIONAL_BUFFER = 1.25
REGISTERED_QUANTIZATION_SAFETY_FACTOR = 2.0


@dataclass(frozen=True, slots=True)
class CalibrationStep:
    """One predeclared desired-state transition."""

    sequence: int
    round_trip_index: int
    symbol: str
    phase: str
    signed_notional_usdt: float
    component_id: str
    not_before_ts_ns: int = 0

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.round_trip_index < 0:
            raise ValueError("calibration step sequence/index is invalid")
        if self.phase not in {"open", "close", "funding_open", "funding_close"}:
            raise ValueError(f"unsupported calibration phase {self.phase!r}")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("calibration step symbol must be uppercase")
        if not self.component_id:
            raise ValueError("calibration step component_id is required")
        if not math.isfinite(self.signed_notional_usdt):
            raise ValueError("calibration notional must be finite")
        if self.phase in {"open", "funding_open"} and self.signed_notional_usdt == 0.0:
            raise ValueError("calibration open step requires nonzero notional")
        if self.phase in {"close", "funding_close"} and self.signed_notional_usdt != 0.0:
            raise ValueError("calibration close step must target zero")
        if self.not_before_ts_ns < 0:
            raise ValueError("calibration not-before timestamp cannot be negative")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CalibrationPlan:
    """Prospective bounded sample definition."""

    plan_id: str
    symbols: tuple[str, ...]
    round_trips_per_symbol: int
    notional_usdt: float
    leverage: float
    hold_seconds: float
    funding_symbol: str = ""
    funding_close_not_before_ts_ns: int = 0
    schema_version: int = CALIBRATION_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CALIBRATION_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported calibration plan schema")
        if not self.plan_id or "/" in self.plan_id:
            raise ValueError("calibration plan_id is required and cannot contain '/'")
        if len(self.symbols) < 3 or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("calibration requires at least three unique symbols")
        if any(not value or value != value.upper() for value in self.symbols):
            raise ValueError("calibration symbols must be uppercase")
        if self.round_trips_per_symbol < 0:
            raise ValueError("round-trip count cannot be negative")
        if not math.isfinite(self.notional_usdt) or self.notional_usdt <= 0.0:
            raise ValueError("calibration notional must be finite and positive")
        if not math.isfinite(self.leverage) or not 0.0 < self.leverage <= 10.0:
            raise ValueError("calibration leverage must be in (0, 10]")
        if not math.isfinite(self.hold_seconds) or not 0.0 <= self.hold_seconds <= 60.0:
            raise ValueError("calibration hold_seconds must be in [0, 60]")
        if bool(self.funding_symbol) != bool(self.funding_close_not_before_ts_ns):
            raise ValueError("funding symbol and close timestamp must be supplied together")
        if self.funding_symbol and self.funding_symbol not in self.symbols:
            raise ValueError("funding symbol must be included in calibration symbols")
        if self.round_trips_per_symbol == 0 and not self.funding_symbol:
            raise ValueError("calibration plan has no transitions")

    @property
    def plan_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "symbols": list(self.symbols),
            "round_trips_per_symbol": self.round_trips_per_symbol,
            "notional_usdt": self.notional_usdt,
            "leverage": self.leverage,
            "hold_seconds": self.hold_seconds,
            "funding_symbol": self.funding_symbol,
            "funding_close_not_before_ts_ns": self.funding_close_not_before_ts_ns,
        }

    def steps(self) -> tuple[CalibrationStep, ...]:
        rows: list[CalibrationStep] = []
        sequence = 1
        for round_trip in range(self.round_trips_per_symbol):
            for symbol_index, symbol in enumerate(self.symbols):
                # Exercise both sides without allowing simultaneous exposure.
                sign = 1.0 if (round_trip + symbol_index) % 2 == 0 else -1.0
                component_id = f"round-{round_trip:02d}-{symbol.lower()}"
                rows.append(
                    CalibrationStep(
                        sequence=sequence,
                        round_trip_index=round_trip,
                        symbol=symbol,
                        phase="open",
                        signed_notional_usdt=sign * self.notional_usdt,
                        component_id=component_id,
                    )
                )
                sequence += 1
                rows.append(
                    CalibrationStep(
                        sequence=sequence,
                        round_trip_index=round_trip,
                        symbol=symbol,
                        phase="close",
                        signed_notional_usdt=0.0,
                        component_id=component_id,
                    )
                )
                sequence += 1
        if self.funding_symbol:
            component_id = f"funding-{self.funding_symbol.lower()}"
            rows.append(
                CalibrationStep(
                    sequence=sequence,
                    round_trip_index=self.round_trips_per_symbol,
                    symbol=self.funding_symbol,
                    phase="funding_open",
                    signed_notional_usdt=self.notional_usdt,
                    component_id=component_id,
                )
            )
            sequence += 1
            rows.append(
                CalibrationStep(
                    sequence=sequence,
                    round_trip_index=self.round_trips_per_symbol,
                    symbol=self.funding_symbol,
                    phase="funding_close",
                    signed_notional_usdt=0.0,
                    component_id=component_id,
                    not_before_ts_ns=self.funding_close_not_before_ts_ns,
                )
            )
        return tuple(rows)


def require_registered_calibration_plan(plan: CalibrationPlan) -> None:
    """Refuse drift from the prospective V5 sample contract."""

    expected = {
        "plan_id": REGISTERED_CALIBRATION_PLAN_ID,
        "symbols": REGISTERED_CALIBRATION_SYMBOLS,
        "round_trips_per_symbol": REGISTERED_ROUND_TRIPS_PER_SYMBOL,
        "notional_usdt": REGISTERED_NOTIONAL_USDT,
        "leverage": REGISTERED_LEVERAGE,
        "hold_seconds": REGISTERED_HOLD_SECONDS,
    }
    observed = {
        "plan_id": plan.plan_id,
        "symbols": plan.symbols,
        "round_trips_per_symbol": plan.round_trips_per_symbol,
        "notional_usdt": plan.notional_usdt,
        "leverage": plan.leverage,
        "hold_seconds": plan.hold_seconds,
    }
    if observed != expected:
        raise ValueError("calibration plan differs from the preregistered fixed sample")
    if plan.funding_symbol not in {"", REGISTERED_FUNDING_SYMBOL}:
        raise ValueError("calibration funding hold differs from the preregistered BTCUSDT hold")


def require_quantization_safe_minimum_buffer(
    plan: CalibrationPlan,
    observed_min_notional_by_symbol: dict[str, float],
) -> None:
    """Keep 25% headroom after venue-step rounding, not only before it.

    For any positive step notional ``x <= requested``, rounding toward zero
    produces ``floor(requested / x) * x >= requested / 2``. Requiring twice
    the desired buffer therefore makes the guarantee independent of the
    current price/step boundary. A step larger than the request is still
    rejected by the account service rather than silently accepted as zero.
    """

    missing = sorted(set(plan.symbols) - set(observed_min_notional_by_symbol))
    if missing:
        raise ValueError(f"calibration symbols lack observed minima: {missing}")
    unsafe: list[str] = []
    for symbol in plan.symbols:
        observed_minimum = float(observed_min_notional_by_symbol[symbol])
        if not math.isfinite(observed_minimum) or observed_minimum <= 0.0:
            raise ValueError(f"calibration observed minimum is invalid for {symbol}")
        required = observed_minimum * REGISTERED_MIN_NOTIONAL_BUFFER * REGISTERED_QUANTIZATION_SAFETY_FACTOR
        if plan.notional_usdt + 1e-12 < required:
            unsafe.append(f"{symbol}:{required:.12g}")
    if unsafe:
        raise ValueError(
            "calibration notional lacks the quantization-safe registered minimum buffer for " + ",".join(unsafe)
        )


@dataclass(frozen=True, slots=True)
class CalibrationStepResult:
    sequence: int
    event_id: str
    request_id: str
    batch_id: str
    command_ids: tuple[str, ...]
    final_state_hash: str


def calibration_event(plan: CalibrationPlan, step: CalibrationStep, *, now_ns: int) -> StrategyEvent:
    """Create the sole live scheduling input for one plan step."""

    if now_ns <= 0:
        raise ValueError("calibration event time must be positive")
    payload = {
        "schema_version": CALIBRATION_PLAN_SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "step": step.to_dict(),
        "leverage": plan.leverage,
    }
    return StrategyEvent(
        event_ts_ns=now_ns,
        ingest_ts_ns=now_ns,
        source=CALIBRATION_EVENT_SOURCE,
        source_sequence=step.sequence,
        kind="timer",
        payload=payload,
    )


def validate_recorded_prefix(
    plan: CalibrationPlan,
    events: Sequence[StrategyEvent],
) -> tuple[CalibrationStep, ...]:
    """Require a recorded tape to be an exact prefix of the current plan."""

    steps = plan.steps()
    if len(events) > len(steps):
        raise ValueError("calibration tape contains more events than the plan")
    for expected, event in zip(steps, events, strict=False):
        if event.source != CALIBRATION_EVENT_SOURCE or event.source_sequence != expected.sequence:
            raise ValueError("calibration tape source/sequence does not match the plan")
        payload = dict(event.payload)
        if (
            payload.get("schema_version") != CALIBRATION_PLAN_SCHEMA_VERSION
            or payload.get("plan_id") != plan.plan_id
            or payload.get("plan_hash") != plan.plan_hash
            or payload.get("step") != expected.to_dict()
            or float(payload.get("leverage") or 0.0) != plan.leverage
        ):
            raise ValueError("calibration tape event does not match the prospective plan")
    return steps[: len(events)]


def _position_qty(state: AccountState, symbol: str) -> float:
    position = state.positions.get(symbol)
    return 0.0 if position is None else float(position.signed_qty)


def _is_flat_boundary(state: AccountState, *, tolerance: float) -> bool:
    return (
        not state.working_order_ids
        and all(abs(_position_qty(state, symbol)) <= tolerance for symbol in state.positions)
        and all(abs(float(value)) <= tolerance for value in state.aggregate_targets.values())
        and not state.component_targets
    )


class DemoExecutionCalibrationDriver:
    """Run or resume one bounded plan through the canonical target inbox."""

    def __init__(
        self,
        *,
        route: AccountRoute,
        tape_path: str | Path,
        clock: Clock | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None,
        transition_timeout_seconds: float = 90.0,
        poll_seconds: float = 0.2,
        quantity_tolerance: float = 1e-12,
    ) -> None:
        if route.environment != "demo":
            raise ValueError("execution calibration requires a demo account route")
        if transition_timeout_seconds <= 0.0 or poll_seconds <= 0.0:
            raise ValueError("calibration timeouts must be positive")
        if quantity_tolerance < 0.0:
            raise ValueError("quantity tolerance cannot be negative")
        self.route = route
        self.clock = clock or SystemClock()
        self.sleeper = sleeper
        self.progress = progress or (lambda _message: None)
        self.transition_timeout_seconds = transition_timeout_seconds
        self.poll_seconds = poll_seconds
        self.quantity_tolerance = quantity_tolerance
        self.publisher = AccountTargetPublisher(route, clock=self.clock)
        self.kernel = AccountExecutionKernel(route.account_root, account_id=route.account_id)
        self.recorder = JsonlStrategyEventTape(tape_path)
        self.event_clock: DeterministicEventClock[object] = DeterministicEventClock(
            clock=self.clock,
            recorder=self.recorder,
        )

    def _require_health(self, *, allow_reconciliation_transition: bool = False) -> None:
        deadline = time.monotonic() + 10.0
        transient_fragments = [
            "changed while binding",
            "journal sequence mismatch",
            "journal state hash mismatch",
        ]
        if allow_reconciliation_transition:
            # Immediately after a target is published, venue REST can observe a
            # fill before the private execution consumer commits it locally (or
            # the reverse). This exact mismatch is an expected propagation
            # state, but only inside the bounded post-publication wait.
            transient_fragments.append("account owner is blocked: account reconciliation mismatch:")
        while True:
            try:
                require_recent_account_owner_health(
                    self.route.account_root,
                    environment="demo",
                    max_age_ns=TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
                    expected_account_id=self.route.account_id,
                )
                return
            except RuntimeError as exc:
                if not any(fragment in str(exc) for fragment in transient_fragments):
                    raise
                if time.monotonic() >= deadline:
                    if allow_reconciliation_transition:
                        detail = (
                            f"account-owner health did not recover within the bounded post-target transition: {exc}"
                        )
                    else:
                        detail = f"account-owner health never rebound to the current journal: {exc}"
                    raise RuntimeError(detail) from exc
                self.sleeper(0.1)

    def _publish_event(self, event: StrategyEvent) -> object:
        payload = dict(event.payload)
        raw_step = payload.get("step")
        if not isinstance(raw_step, dict):
            raise ValueError("calibration event lacks a step")
        step = CalibrationStep(**raw_step)
        action = "entry" if step.signed_notional_usdt else "exit"
        decision_ts_ms = event.event_ts_ns // 1_000_000
        metadata: dict[str, object] = {
            "execution_calibration": True,
            "calibration_plan_id": str(payload["plan_id"]),
            "calibration_plan_hash": str(payload["plan_hash"]),
            "calibration_step_sequence": step.sequence,
            "calibration_phase": step.phase,
            "calibration_round_trip_index": step.round_trip_index,
        }
        if action == "entry":
            metadata.update(
                {
                    "signal_ts_ms": decision_ts_ms,
                    "signal_valid_until_ms": decision_ts_ms + 600_000,
                }
            )
        intent = component_target_intent(
            adapter_kind=SleeveAdapterKind.HEDGE,
            action=action,
            decision_ts_ms=decision_ts_ms,
            strategy_id=CALIBRATION_STRATEGY_ID,
            component_id=step.component_id,
            symbol=step.symbol,
            signed_notional_usdt=step.signed_notional_usdt,
            leverage=float(payload["leverage"]),
            reason=f"demo_execution_calibration_{step.phase}",
            metadata=metadata,
        )
        return self.publisher.publish(
            batch_id=f"{payload['plan_id']}/step/{step.sequence:03d}",
            intents=(intent,),
            created_ts_ns=event.event_ts_ns,
        )

    def _receipt(self, request_id: str) -> AccountServiceReceipt:
        deadline = time.monotonic() + self.transition_timeout_seconds
        while time.monotonic() < deadline:
            self._require_health(allow_reconciliation_transition=True)
            for request, receipt in self.publisher.inbox.completed_requests():
                if request.request_id != request_id:
                    continue
                if not receipt.accepted:
                    raise RuntimeError(f"calibration request {request_id} rejected: {','.join(receipt.rejection_keys)}")
                if not receipt.command_ids:
                    raise RuntimeError(f"calibration request {request_id} emitted no order command")
                return receipt
            self.sleeper(self.poll_seconds)
        raise TimeoutError(f"calibration request {request_id} did not complete")

    def _converged(
        self,
        *,
        step: CalibrationStep,
        target_key: str,
        receipt: AccountServiceReceipt,
    ) -> tuple[bool, str]:
        state = self.kernel.state()
        desired = state.component_target_desires.get(target_key)
        if desired is None:
            return False, state.rolling_state_hash
        desired_qty = float(desired.get("signed_qty") or 0.0)
        aggregate_qty = float(state.aggregate_targets.get(step.symbol) or 0.0)
        position_qty = _position_qty(state, step.symbol)
        orders = [state.orders.get(command_id) for command_id in receipt.command_ids]
        if any(order is None or order.status != "filled" for order in orders):
            return False, state.rolling_state_hash
        if state.working_order_count(step.symbol, tolerance=self.quantity_tolerance):
            return False, state.rolling_state_hash
        if step.signed_notional_usdt == 0.0:
            converged = (
                abs(desired_qty) <= self.quantity_tolerance
                and abs(aggregate_qty) <= self.quantity_tolerance
                and abs(position_qty) <= self.quantity_tolerance
                and target_key not in state.component_targets
            )
        else:
            converged = (
                abs(desired_qty) > self.quantity_tolerance
                and math.copysign(1.0, desired_qty) == math.copysign(1.0, step.signed_notional_usdt)
                and abs(aggregate_qty - desired_qty) <= self.quantity_tolerance
                and abs(position_qty - desired_qty) <= self.quantity_tolerance
                and target_key in state.component_targets
            )
        return converged, state.rolling_state_hash

    def _wait_convergence(
        self,
        *,
        step: CalibrationStep,
        target_key: str,
        receipt: AccountServiceReceipt,
    ) -> str:
        deadline = time.monotonic() + self.transition_timeout_seconds
        last_hash = ""
        while time.monotonic() < deadline:
            self._require_health(allow_reconciliation_transition=True)
            converged, last_hash = self._converged(
                step=step,
                target_key=target_key,
                receipt=receipt,
            )
            if converged:
                return last_hash
            self.sleeper(self.poll_seconds)
        raise TimeoutError(f"calibration step {step.sequence} did not converge; state_hash={last_hash}")

    def _wait_not_before(self, step: CalibrationStep) -> None:
        next_progress = time.monotonic()
        while step.not_before_ts_ns and self.clock.wall_time_ns() < step.not_before_ts_ns:
            self._require_health()
            state = self.kernel.state()
            if _is_flat_boundary(state, tolerance=self.quantity_tolerance):
                raise RuntimeError("funding hold became flat before its registered close time")
            remaining_seconds = (step.not_before_ts_ns - self.clock.wall_time_ns()) / 1_000_000_000
            if time.monotonic() >= next_progress:
                self.progress(f"funding_hold_remaining_seconds={max(remaining_seconds, 0.0):.1f}")
                next_progress = time.monotonic() + 60.0
            self.sleeper(min(max(remaining_seconds, 0.0), 1.0))

    def run(self, plan: CalibrationPlan, *, resume: bool = False) -> tuple[CalibrationStepResult, ...]:
        steps = plan.steps()
        prior_events = self.recorder.prior_events
        validate_recorded_prefix(plan, prior_events)
        if prior_events and not resume:
            raise RuntimeError("calibration tape already exists; use explicit resume")

        results: list[CalibrationStepResult] = []
        # Reconstruct immutable request identities for the recorded prefix. Only
        # the final recorded state needs convergence; earlier requests are
        # already superseded by later plan steps.
        for event in prior_events:
            published = self._publish_event(event)
            request_id = published.request.request_id  # type: ignore[attr-defined]
            receipt = self._receipt(request_id)
            results.append(
                CalibrationStepResult(
                    sequence=event.source_sequence,
                    event_id=event.event_id,
                    request_id=request_id,
                    batch_id=receipt.batch_id,
                    command_ids=receipt.command_ids,
                    final_state_hash=receipt.final_state_hash,
                )
            )
        if prior_events:
            last_step = steps[len(prior_events) - 1]
            last_result = results[-1]
            target_key = (
                f"{SleeveAdapterKind.HEDGE.value}/{CALIBRATION_STRATEGY_ID}/{last_step.component_id}/{last_step.symbol}"
            )
            last_receipt = self._receipt(last_result.request_id)
            self._wait_convergence(
                step=last_step,
                target_key=target_key,
                receipt=last_receipt,
            )

        if not prior_events and not _is_flat_boundary(self.kernel.state(), tolerance=self.quantity_tolerance):
            raise RuntimeError("calibration requires a wholly flat account boundary")

        for step in steps[len(prior_events) :]:
            if step.phase in {"close", "funding_close"}:
                if step.phase == "close" and plan.hold_seconds:
                    self.sleeper(plan.hold_seconds)
                self._wait_not_before(step)
            elif not _is_flat_boundary(self.kernel.state(), tolerance=self.quantity_tolerance):
                raise RuntimeError("calibration refuses simultaneous or foreign account exposure")

            self._require_health()
            self.progress(
                f"calibration_step_start sequence={step.sequence} phase={step.phase} "
                f"symbol={step.symbol} notional={step.signed_notional_usdt:g}"
            )
            event = calibration_event(plan, step, now_ns=self.clock.wall_time_ns())
            published = self.event_clock.dispatch(event, self._publish_event)
            request_id = published.request.request_id  # type: ignore[attr-defined]
            target_key = f"{SleeveAdapterKind.HEDGE.value}/{CALIBRATION_STRATEGY_ID}/{step.component_id}/{step.symbol}"
            receipt = self._receipt(request_id)
            state_hash = self._wait_convergence(
                step=step,
                target_key=target_key,
                receipt=receipt,
            )
            results.append(
                CalibrationStepResult(
                    sequence=step.sequence,
                    event_id=event.event_id,
                    request_id=request_id,
                    batch_id=receipt.batch_id,
                    command_ids=receipt.command_ids,
                    final_state_hash=state_hash,
                )
            )
            self.progress(
                f"calibration_step_converged sequence={step.sequence} "
                f"commands={len(receipt.command_ids)} state_hash={state_hash}"
            )

        if not _is_flat_boundary(self.kernel.state(), tolerance=self.quantity_tolerance):
            raise RuntimeError("calibration plan ended without a flat account boundary")
        return tuple(results)
