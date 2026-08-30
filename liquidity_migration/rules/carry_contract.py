"""Pure CARRY lifecycle decisions shared by live production and research.

The registered carry rule decides a daily weight book.  This reducer owns
everything between that book and the Rust target-book seam: durable sizing
anchors, settled and pre-settlement exits, the next-day drop exit, admission,
resize classification, and the exact bytes to publish.  It performs no I/O.

Rust remains the execution authority.  The values in :class:`ExecutionRules`
mirror ``target_book::PlanRules::FLEET`` and are fenced by the checked-in
cross-language replay fixture.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Mapping

from liquidity_migration.rules.engine_targets import (
    EngineTarget,
    ParsedTargetBook,
    render_target_book,
)


CARRY_CONTRACT_SCHEMA_VERSION = 1
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS


@dataclass(frozen=True, slots=True)
class ExecutionRules:
    """Versioned physics on both sides of the Python/Rust seam."""

    schema_version: int = CARRY_CONTRACT_SCHEMA_VERSION
    entry_floor_usdt: float = 6.0
    resize_floor_usdt: float = 1.0
    resize_floor_fraction: float = 0.05
    engine_entry_cutoff_ms: int = 15 * 60 * 1000
    signal_validity_ms: int = 6 * HOUR_MS
    book_validity_ms: int = 30 * HOUR_MS
    presettlement_window_ms: int = 15 * 60 * 1000

    def __post_init__(self) -> None:
        if self.schema_version != CARRY_CONTRACT_SCHEMA_VERSION:
            raise ValueError("unsupported CARRY execution-rules schema")
        for name in ("entry_floor_usdt", "resize_floor_usdt"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if not math.isfinite(self.resize_floor_fraction) or not 0.0 <= self.resize_floor_fraction < 1.0:
            raise ValueError("resize_floor_fraction must be in [0, 1)")
        for name in (
            "engine_entry_cutoff_ms",
            "signal_validity_ms",
            "book_validity_ms",
            "presettlement_window_ms",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.signal_validity_ms <= self.engine_entry_cutoff_ms:
            raise ValueError("signal validity must clear the engine entry cutoff")
        if self.book_validity_ms <= self.signal_validity_ms:
            raise ValueError("book validity must outlive signal validity")

    @property
    def entry_publish_deadline_offset_ms(self) -> int:
        return self.signal_validity_ms - self.engine_entry_cutoff_ms

    def as_json_dict(self) -> dict[str, int | float]:
        return asdict(self)


FLEET_EXECUTION_RULES = ExecutionRules()


@dataclass(frozen=True)
class CarryDecision:
    """One registered daily weight decision."""

    decision_ts_ms: int
    weights: Mapping[str, float]
    universe_size: int
    replay_days: int
    gross: float

    def __post_init__(self) -> None:
        if type(self.decision_ts_ms) is not int or self.decision_ts_ms <= 0:
            raise ValueError("CARRY decision time must be a positive integer")
        if type(self.universe_size) is not int or self.universe_size < 0:
            raise ValueError("CARRY universe size must be a nonnegative integer")
        if type(self.replay_days) is not int or self.replay_days < 0:
            raise ValueError("CARRY replay days must be a nonnegative integer")
        normalized: dict[str, float] = {}
        for symbol, raw_weight in self.weights.items():
            if not symbol or symbol != symbol.upper() or not symbol.isalnum():
                raise ValueError("CARRY decision contains an invalid symbol")
            weight = float(raw_weight)
            if not math.isfinite(weight) or weight <= 0.0:
                raise ValueError("CARRY decision weights must be finite and positive")
            normalized[symbol] = weight
        if len(normalized) != len(self.weights):
            raise ValueError("CARRY decision contains duplicate symbols")
        gross = sum(normalized.values())
        if not math.isfinite(self.gross):
            raise ValueError("CARRY decision gross must be finite")
        object.__setattr__(self, "weights", normalized)
        # ``gross`` is derived state. Normalize compatibility callers that
        # still pass the old field instead of letting two values disagree.
        object.__setattr__(self, "gross", gross)

    def with_weights(self, weights: Mapping[str, float]) -> CarryDecision:
        normalized = dict(sorted((str(symbol), float(weight)) for symbol, weight in weights.items()))
        return CarryDecision(
            decision_ts_ms=self.decision_ts_ms,
            weights=normalized,
            universe_size=self.universe_size,
            replay_days=self.replay_days,
            gross=sum(normalized.values()),
        )

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": CARRY_CONTRACT_SCHEMA_VERSION,
            "decision_ts_ms": self.decision_ts_ms,
            "weights": dict(sorted(self.weights.items())),
            "universe_size": self.universe_size,
            "replay_days": self.replay_days,
            "gross": self.gross,
        }


@dataclass(frozen=True, slots=True)
class Holding:
    symbol: str
    side: str
    qty: float
    entry_px: float
    mark_px: float | None = None

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper() or not self.symbol.isalnum():
            raise ValueError("CARRY holding symbol is invalid")
        if self.side not in {"long", "short"}:
            raise ValueError("CARRY holding side must be long or short")
        if not math.isfinite(self.qty) or self.qty <= 0.0:
            raise ValueError("CARRY holding quantity must be positive")
        if not math.isfinite(self.entry_px) or self.entry_px <= 0.0:
            raise ValueError("CARRY holding entry price must be positive")
        if self.mark_px is not None and (
            not math.isfinite(self.mark_px) or self.mark_px <= 0.0
        ):
            raise ValueError("CARRY holding mark must be null or positive")

    @property
    def signed_notional_usdt(self) -> float | None:
        if self.mark_px is None:
            return None
        sign = -1.0 if self.side == "short" else 1.0
        return sign * self.qty * self.mark_px


@dataclass(frozen=True, slots=True)
class SettledFundingObservation:
    symbol: str
    settlement_ts_ms: int
    rate: float

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper() or not self.symbol.isalnum():
            raise ValueError("CARRY settled-funding symbol is invalid")
        if type(self.settlement_ts_ms) is not int or self.settlement_ts_ms <= 0:
            raise ValueError("CARRY settlement time must be positive")
        if not math.isfinite(self.rate):
            raise ValueError("CARRY settled-funding rate must be finite")


@dataclass(frozen=True, slots=True)
class PresettlementObservation:
    symbol: str
    observed_ts_ms: int
    settlement_ts_ms: int
    running_rate: float
    mark_px: float | None = None
    carry_side: str | None = None
    carry_qty: float | None = None
    carry_avg_entry_px: float | None = None

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper() or not self.symbol.isalnum():
            raise ValueError("CARRY pre-settlement symbol is invalid")
        for name in ("observed_ts_ms", "settlement_ts_ms"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"CARRY pre-settlement {name} must be positive")
        if not math.isfinite(self.running_rate):
            raise ValueError("CARRY pre-settlement rate must be finite")
        if self.mark_px is not None and (not math.isfinite(self.mark_px) or self.mark_px <= 0.0):
            raise ValueError("CARRY pre-settlement mark must be null or positive")
        if self.carry_side not in {None, "long", "short"}:
            raise ValueError("CARRY pre-settlement holding side is invalid")
        holding_values = (self.carry_qty, self.carry_avg_entry_px)
        if self.carry_side is None and any(value is not None for value in holding_values):
            raise ValueError("CARRY pre-settlement holding is incomplete")
        if self.carry_side is not None and any(value is None for value in holding_values):
            raise ValueError("CARRY pre-settlement holding is incomplete")
        for value in holding_values:
            if value is not None and (not math.isfinite(value) or value <= 0.0):
                raise ValueError("CARRY pre-settlement holding values must be positive")


@dataclass(frozen=True, slots=True)
class PresettlementFire:
    decision_ts_ms: int
    symbol: str
    observed_ts_ms: int
    settlement_ts_ms: int
    running_rate: float
    mark_px: float | None
    carry_side: str | None
    carry_qty: float | None
    carry_avg_entry_px: float | None

    @classmethod
    def from_observation(
        cls,
        *,
        decision_ts_ms: int,
        observation: PresettlementObservation,
    ) -> PresettlementFire:
        return cls(
            decision_ts_ms=decision_ts_ms,
            symbol=observation.symbol,
            observed_ts_ms=observation.observed_ts_ms,
            settlement_ts_ms=observation.settlement_ts_ms,
            running_rate=observation.running_rate,
            mark_px=observation.mark_px,
            carry_side=observation.carry_side,
            carry_qty=observation.carry_qty,
            carry_avg_entry_px=observation.carry_avg_entry_px,
        )

    def as_json_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PriorState:
    """Durable reducer state, represented canonically for replay."""

    sizing_anchors: tuple[tuple[int, float], ...] = ()
    fired_exits: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        anchors: dict[int, float] = {}
        for decision_ts_ms, equity_usdt in self.sizing_anchors:
            if type(decision_ts_ms) is not int or decision_ts_ms <= 0:
                raise ValueError("CARRY sizing anchor has an invalid decision time")
            if not math.isfinite(equity_usdt) or equity_usdt <= 0.0:
                raise ValueError("CARRY sizing anchor equity must be positive")
            if decision_ts_ms in anchors:
                raise ValueError("CARRY sizing anchors contain a duplicate decision")
            anchors[decision_ts_ms] = float(equity_usdt)
        if len(anchors) > 2:
            raise ValueError("CARRY sizing anchors retain more than two decisions")
        fired: dict[str, int] = {}
        for symbol, decision_ts_ms in self.fired_exits:
            if not symbol or symbol != symbol.upper() or not symbol.isalnum():
                raise ValueError("CARRY fired-exit symbol is invalid")
            if type(decision_ts_ms) is not int or decision_ts_ms <= 0:
                raise ValueError("CARRY fired-exit decision time is invalid")
            if symbol in fired:
                raise ValueError("CARRY fired exits contain a duplicate symbol")
            fired[symbol] = decision_ts_ms
        object.__setattr__(self, "sizing_anchors", tuple(sorted(anchors.items())))
        object.__setattr__(self, "fired_exits", tuple(sorted(fired.items())))

    def anchor_by_decision(self) -> dict[int, float]:
        return dict(self.sizing_anchors)

    def fired_by_symbol(self) -> dict[str, int]:
        return dict(self.fired_exits)

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": CARRY_CONTRACT_SCHEMA_VERSION,
            "sizing_anchors": [list(row) for row in self.sizing_anchors],
            "fired_exits": [list(row) for row in self.fired_exits],
        }


@dataclass(frozen=True, slots=True)
class SizingAnchorRequest:
    decision_ts_ms: int
    equity_usdt: float

    def __post_init__(self) -> None:
        if type(self.decision_ts_ms) is not int or self.decision_ts_ms <= 0:
            raise ValueError("CARRY sizing-anchor request has an invalid decision time")
        if not math.isfinite(self.equity_usdt) or self.equity_usdt <= 0.0:
            raise ValueError("CARRY sizing-anchor request equity must be positive")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    profile_name: str
    accepted_book_sources: tuple[str, ...]
    exit_bp: float
    early_exit_enabled: bool
    presettlement_exit_enabled: bool
    notional_multiplier: float
    entry_leverage: float
    stop_loss_fraction: float
    max_new_entries_per_cycle: int
    capital_reference_usdt: float = 0.0
    execution: ExecutionRules = FLEET_EXECUTION_RULES

    def __post_init__(self) -> None:
        if not self.profile_name or not self.profile_name.replace("_", "").replace("-", "").isalnum():
            raise ValueError("CARRY profile name must be a plain identifier")
        sources = tuple(dict.fromkeys((self.profile_name, *self.accepted_book_sources)))
        if any(not item or not item.replace("_", "").replace("-", "").isalnum() for item in sources):
            raise ValueError("CARRY accepted book sources must be plain identifiers")
        object.__setattr__(self, "accepted_book_sources", sources)
        for name in ("exit_bp", "notional_multiplier", "entry_leverage", "stop_loss_fraction"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0.0 < self.stop_loss_fraction < 1.0:
            raise ValueError("stop_loss_fraction must be between 0 and 1")
        if type(self.max_new_entries_per_cycle) is not int or self.max_new_entries_per_cycle <= 0:
            raise ValueError("max_new_entries_per_cycle must be a positive integer")
        if not math.isfinite(self.capital_reference_usdt) or self.capital_reference_usdt < 0.0:
            raise ValueError("capital_reference_usdt must be finite and nonnegative")

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": CARRY_CONTRACT_SCHEMA_VERSION,
            "profile_name": self.profile_name,
            "accepted_book_sources": list(self.accepted_book_sources),
            "exit_bp": self.exit_bp,
            "early_exit_enabled": self.early_exit_enabled,
            "presettlement_exit_enabled": self.presettlement_exit_enabled,
            "notional_multiplier": self.notional_multiplier,
            "entry_leverage": self.entry_leverage,
            "stop_loss_fraction": self.stop_loss_fraction,
            "max_new_entries_per_cycle": self.max_new_entries_per_cycle,
            "capital_reference_usdt": self.capital_reference_usdt,
            "execution": self.execution.as_json_dict(),
        }


@dataclass(frozen=True, slots=True)
class DecisionInput:
    now_ms: int
    decision: CarryDecision | None
    upcoming_decision: CarryDecision | None = None
    holdings: tuple[Holding, ...] = ()
    trail_by_symbol: tuple[tuple[str, float], ...] = ()
    entry_blockers: tuple[tuple[str, str], ...] = ()
    account_health_error: str = ""
    equity_usdt: float = 0.0
    sizing_anchor_requests: tuple[SizingAnchorRequest, ...] = ()
    settled_funding: tuple[SettledFundingObservation, ...] = ()
    presettlement: tuple[PresettlementObservation, ...] = ()
    durable_presettlement_fires: tuple[PresettlementFire, ...] = ()
    previous_book: ParsedTargetBook | None = None

    def __post_init__(self) -> None:
        if type(self.now_ms) is not int or self.now_ms <= 0:
            raise ValueError("CARRY reducer time must be positive")
        if not math.isfinite(self.equity_usdt) or self.equity_usdt < 0.0:
            raise ValueError("CARRY reducer equity must be finite and nonnegative")
        symbols = [holding.symbol for holding in self.holdings]
        if len(symbols) != len(set(symbols)):
            raise ValueError("CARRY reducer holdings contain duplicate symbols")
        for name, rows in (
            ("trail", self.trail_by_symbol),
            ("entry blockers", self.entry_blockers),
        ):
            keys = [row[0] for row in rows]
            if len(keys) != len(set(keys)):
                raise ValueError(f"CARRY reducer {name} contain duplicate symbols")
        anchor_decisions = [row.decision_ts_ms for row in self.sizing_anchor_requests]
        if len(anchor_decisions) != len(set(anchor_decisions)):
            raise ValueError("CARRY reducer sizing-anchor requests contain duplicate decisions")

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": CARRY_CONTRACT_SCHEMA_VERSION,
            "now_ms": self.now_ms,
            "decision": self.decision.as_json_dict() if self.decision is not None else None,
            "upcoming_decision": (
                self.upcoming_decision.as_json_dict() if self.upcoming_decision is not None else None
            ),
            "holdings": [asdict(row) for row in self.holdings],
            "trail_by_symbol": [list(row) for row in self.trail_by_symbol],
            "entry_blockers": [list(row) for row in self.entry_blockers],
            "account_health_error": self.account_health_error,
            "equity_usdt": self.equity_usdt,
            "sizing_anchor_requests": [asdict(row) for row in self.sizing_anchor_requests],
            "settled_funding": [asdict(row) for row in self.settled_funding],
            "presettlement": [asdict(row) for row in self.presettlement],
            "durable_presettlement_fires": [row.as_json_dict() for row in self.durable_presettlement_fires],
            "previous_book": (
                {
                    "source": self.previous_book.source,
                    "decision_ts_ms": self.previous_book.decision_ts_ms,
                    "valid_until_ms": self.previous_book.valid_until_ms,
                    "targets": [asdict(row) for row in self.previous_book.targets],
                }
                if self.previous_book is not None
                else None
            ),
        }


class PublicationAction(StrEnum):
    HOLD = "hold"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class PlanSummary:
    desired_book_size: int = 0
    desired_gross_weight: float = 0.0
    planned_exits: int = 0
    planned_entries: int = 0
    planned_resizes: int = 0
    resize_mark_missing_skips: int = 0
    entry_cap_deferrals: int = 0
    entry_validity_expired_skips: int = 0
    entry_dust_skips: int = 0
    engine_blocked_entries: int = 0
    entry_blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class DecisionOutput:
    action: PublicationAction
    target_book_text: str | None
    next_state: PriorState
    effective_decision: CarryDecision | None
    sizing_equity_usdt: float | None
    summary: PlanSummary
    settled_exit_fires: tuple[str, ...] = ()
    presettlement_fires: tuple[PresettlementFire, ...] = ()
    drop_exit_fires: tuple[str, ...] = ()

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": CARRY_CONTRACT_SCHEMA_VERSION,
            "action": self.action.value,
            "target_book_text": self.target_book_text,
            "next_state": self.next_state.as_json_dict(),
            "effective_decision": (
                self.effective_decision.as_json_dict() if self.effective_decision is not None else None
            ),
            "sizing_equity_usdt": self.sizing_equity_usdt,
            "summary": asdict(self.summary),
            "settled_exit_fires": list(self.settled_exit_fires),
            "presettlement_fires": [row.as_json_dict() for row in self.presettlement_fires],
            "drop_exit_fires": list(self.drop_exit_fires),
        }


def anchor_sizing_state(
    prior: PriorState,
    *,
    decision_ts_ms: int,
    equity_usdt: float,
) -> tuple[PriorState, float | None]:
    """Purely retain at most two first-seen equity anchors."""

    if not math.isfinite(equity_usdt) or equity_usdt <= 0.0:
        return prior, None
    anchors = prior.anchor_by_decision()
    anchor = anchors.get(decision_ts_ms)
    if anchor is None:
        anchor = float(equity_usdt)
        anchors[int(decision_ts_ms)] = anchor
        while len(anchors) > 2:
            del anchors[min(anchors)]
    return PriorState(tuple(sorted(anchors.items())), prior.fired_exits), anchor


def _masked_decision(
    decision: CarryDecision,
    *,
    upcoming: CarryDecision | None,
    prior_fired: Mapping[str, int],
    settled_funding: tuple[SettledFundingObservation, ...],
    presettlement: tuple[PresettlementObservation, ...],
    durable_fires: tuple[PresettlementFire, ...],
    now_ms: int,
    config: StrategyConfig,
) -> tuple[
    CarryDecision,
    dict[str, int],
    tuple[str, ...],
    tuple[PresettlementFire, ...],
    tuple[str, ...],
]:
    fired = {symbol: ts for symbol, ts in prior_fired.items() if ts == decision.decision_ts_ms}
    exit_threshold = -(config.exit_bp / 1e4)
    settled_fires: list[str] = []
    if config.early_exit_enabled:
        latest: dict[str, SettledFundingObservation] = {}
        for settled_row in settled_funding:
            if (
                settled_row.symbol in decision.weights
                and decision.decision_ts_ms < settled_row.settlement_ts_ms <= now_ms
                and (
                    settled_row.symbol not in latest
                    or settled_row.settlement_ts_ms
                    > latest[settled_row.symbol].settlement_ts_ms
                )
            ):
                latest[settled_row.symbol] = settled_row
        for symbol, latest_row in sorted(latest.items()):
            if symbol not in fired and not (latest_row.rate < exit_threshold):
                fired[symbol] = decision.decision_ts_ms
                settled_fires.append(symbol)

    presettle_fires: list[PresettlementFire] = []
    if config.early_exit_enabled and config.presettlement_exit_enabled:
        for event in durable_fires:
            if (
                event.decision_ts_ms == decision.decision_ts_ms
                and decision.decision_ts_ms <= event.observed_ts_ms <= now_ms
                and event.settlement_ts_ms > event.observed_ts_ms
                and event.symbol in decision.weights
                and event.symbol not in fired
            ):
                fired[event.symbol] = decision.decision_ts_ms
        seen_observations: set[str] = set()
        for observation in sorted(
            presettlement,
            key=lambda item: (item.symbol, item.observed_ts_ms),
        ):
            if observation.symbol in seen_observations:
                raise ValueError("CARRY reducer pre-settlement observations contain duplicate symbols")
            seen_observations.add(observation.symbol)
            if (
                observation.symbol not in decision.weights
                or observation.symbol in fired
                or not decision.decision_ts_ms <= observation.observed_ts_ms <= now_ms
            ):
                continue
            lead_ms = observation.settlement_ts_ms - observation.observed_ts_ms
            if 0 < lead_ms <= config.execution.presettlement_window_ms and not (
                observation.running_rate < exit_threshold
            ):
                fire = PresettlementFire.from_observation(
                    decision_ts_ms=decision.decision_ts_ms,
                    observation=observation,
                )
                fired[observation.symbol] = decision.decision_ts_ms
                presettle_fires.append(fire)

    desired = {symbol: weight for symbol, weight in decision.weights.items() if symbol not in fired}
    drop_fires: tuple[str, ...] = ()
    if upcoming is not None and upcoming.decision_ts_ms == decision.decision_ts_ms + DAY_MS:
        dropped = sorted(symbol for symbol in desired if symbol not in upcoming.weights)
        desired = {symbol: weight for symbol, weight in desired.items() if symbol in upcoming.weights}
        drop_fires = tuple(dropped)
    return (
        decision.with_weights(desired),
        fired,
        tuple(settled_fires),
        tuple(presettle_fires),
        drop_fires,
    )


def render_target_book_text(
    *,
    desired: Mapping[str, float],
    decision_ts_ms: int,
    sizing_equity_usdt: float,
    config: StrategyConfig,
) -> str:
    entry_valid_until_ms = decision_ts_ms + config.execution.entry_publish_deadline_offset_ms
    return render_target_book(
        source=config.profile_name,
        decision_ts_ms=decision_ts_ms,
        valid_until_ms=decision_ts_ms + config.execution.book_validity_ms,
        targets=[
            EngineTarget(
                symbol=symbol,
                notional_usdt=float(weight) * sizing_equity_usdt * config.notional_multiplier,
                stop_loss_fraction=config.stop_loss_fraction,
                leverage=config.entry_leverage,
                entry_valid_until_ms=entry_valid_until_ms,
            )
            for symbol, weight in sorted(desired.items())
        ],
    )


def decide(
    decision_input: DecisionInput,
    prior_state: PriorState,
    config: StrategyConfig,
) -> DecisionOutput:
    """Reduce one complete CARRY observation without touching external state."""

    state = prior_state
    for request in sorted(
        decision_input.sizing_anchor_requests,
        key=lambda row: row.decision_ts_ms,
    ):
        state, _anchor = anchor_sizing_state(
            state,
            decision_ts_ms=request.decision_ts_ms,
            equity_usdt=request.equity_usdt,
        )

    decision = decision_input.decision
    if decision is None:
        return DecisionOutput(
            action=PublicationAction.HOLD,
            target_book_text=None,
            next_state=state,
            effective_decision=None,
            sizing_equity_usdt=None,
            summary=PlanSummary(entry_blocked_reason="decision_unavailable"),
        )

    effective, fired, settled_fires, presettle_fires, drop_fires = _masked_decision(
        decision,
        upcoming=decision_input.upcoming_decision,
        prior_fired=state.fired_by_symbol(),
        settled_funding=decision_input.settled_funding,
        presettlement=decision_input.presettlement,
        durable_fires=decision_input.durable_presettlement_fires,
        now_ms=decision_input.now_ms,
        config=config,
    )
    state = PriorState(state.sizing_anchors, tuple(sorted(fired.items())))
    healthy = not decision_input.account_health_error and decision_input.equity_usdt > 0.0
    if not healthy:
        reason = "engine_account_health_unavailable"
        previous = decision_input.previous_book
        if previous is None:
            return DecisionOutput(
                PublicationAction.HOLD,
                None,
                state,
                effective,
                None,
                PlanSummary(
                    desired_book_size=len(effective.weights),
                    desired_gross_weight=effective.gross,
                    entry_blocked_reason=reason,
                ),
                settled_fires,
                presettle_fires,
                drop_fires,
            )
        if previous.source not in config.accepted_book_sources:
            raise ValueError(
                f"active target book source {previous.source!r} does not match CARRY profile {config.profile_name!r}"
            )
        retained = [target for target in previous.targets if target.symbol in effective.weights]
        planned_exits = len(previous.targets) - len(retained)
        text = None
        action = PublicationAction.HOLD
        if planned_exits:
            text = render_target_book(
                source=config.profile_name,
                decision_ts_ms=max(1, decision_input.now_ms - 1),
                valid_until_ms=max(2, decision_input.now_ms),
                targets=retained,
            )
            action = PublicationAction.WRITE
        return DecisionOutput(
            action,
            text,
            state,
            effective,
            None,
            PlanSummary(
                desired_book_size=len(effective.weights),
                desired_gross_weight=effective.gross,
                planned_exits=planned_exits,
                entry_blocked_reason=reason,
            ),
            settled_fires,
            presettle_fires,
            drop_fires,
        )

    state, raw_anchor = anchor_sizing_state(
        state,
        decision_ts_ms=decision.decision_ts_ms,
        equity_usdt=decision_input.equity_usdt,
    )
    assert raw_anchor is not None
    sizing_equity = raw_anchor
    if config.capital_reference_usdt > 0.0:
        sizing_equity = min(sizing_equity, config.capital_reference_usdt)

    holdings = {row.symbol: row for row in decision_input.holdings}
    standing_symbols = set(holdings)
    book_desired = {
        symbol: float(weight)
        for symbol, weight in effective.weights.items()
        if symbol in standing_symbols
    }
    trail = dict(decision_input.trail_by_symbol)
    blockers = dict(decision_input.entry_blockers)
    entry_symbols = sorted(
        (symbol for symbol in effective.weights if symbol not in standing_symbols),
        key=lambda symbol: (trail.get(symbol, 0.0), symbol),
    )
    engine_blocked_entries = sum(symbol in blockers for symbol in entry_symbols)
    entry_symbols = [symbol for symbol in entry_symbols if symbol not in blockers]
    entry_validity_expired_skips = 0
    if decision_input.now_ms >= (
        decision.decision_ts_ms + config.execution.entry_publish_deadline_offset_ms
    ):
        entry_validity_expired_skips = len(entry_symbols)
        entry_symbols = []

    planned_entries = 0
    entry_cap_deferrals = 0
    entry_dust_skips = 0
    for symbol in entry_symbols:
        target_notional = effective.weights[symbol] * sizing_equity * config.notional_multiplier
        if abs(target_notional) < config.execution.entry_floor_usdt:
            entry_dust_skips += 1
            continue
        if planned_entries >= config.max_new_entries_per_cycle:
            entry_cap_deferrals += 1
            continue
        book_desired[symbol] = effective.weights[symbol]
        planned_entries += 1

    planned_resizes = 0
    resize_mark_missing_skips = 0
    for symbol in sorted(set(book_desired) & standing_symbols):
        target_notional = book_desired[symbol] * sizing_equity * config.notional_multiplier
        standing_notional = holdings[symbol].signed_notional_usdt
        if standing_notional is None:
            resize_mark_missing_skips += 1
            continue
        threshold = max(
            config.execution.resize_floor_usdt,
            config.execution.resize_floor_fraction * abs(standing_notional),
        )
        if abs(target_notional - standing_notional) > threshold:
            planned_resizes += 1

    text = render_target_book_text(
        desired=book_desired,
        decision_ts_ms=decision.decision_ts_ms,
        sizing_equity_usdt=sizing_equity,
        config=config,
    )
    return DecisionOutput(
        PublicationAction.WRITE,
        text,
        state,
        effective,
        sizing_equity,
        PlanSummary(
            desired_book_size=len(effective.weights),
            desired_gross_weight=effective.gross,
            planned_exits=len(standing_symbols - set(book_desired)),
            planned_entries=planned_entries,
            planned_resizes=planned_resizes,
            resize_mark_missing_skips=resize_mark_missing_skips,
            entry_cap_deferrals=entry_cap_deferrals,
            entry_validity_expired_skips=entry_validity_expired_skips,
            entry_dust_skips=entry_dust_skips,
            engine_blocked_entries=engine_blocked_entries,
        ),
        settled_fires,
        presettle_fires,
        drop_fires,
    )
