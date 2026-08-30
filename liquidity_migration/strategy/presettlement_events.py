"""Typed, durable CARRY pre-settlement events consumed by Exodus.

CARRY owns the exit signal and records the exact attributed position it is
abandoning. Exodus reads this tape independently; it never imports CARRY's
mutable cycle state or reconstructs the trigger from market data.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.policy.execution_environment import EXECUTION_ENVIRONMENT_VALUES
from liquidity_migration.strategy.strategy_event_clock import (
    JsonlStrategyEventTape,
    StrategyEvent,
    load_strategy_event_tape,
)


PRESETTLEMENT_EVENT_SCHEMA_VERSION = 1
PRESETTLEMENT_EVENT_KIND = "presettlement_exit"
PRESETTLEMENT_EVENT_SOURCE = "carry_hold"


@dataclasses.dataclass(frozen=True, slots=True)
class CarryPresettlementEvent:
    """One exact, replayable handoff from CARRY to Exodus."""

    environment: str
    source_profile: str
    source_config_id: str
    decision_ts_ms: int
    fired_ts_ms: int
    settlement_ts_ms: int
    symbol: str
    running_rate: float
    mark_px: float | None
    carry_side: str | None
    carry_qty: float | None
    carry_avg_entry_px: float | None

    def __post_init__(self) -> None:
        if self.environment not in EXECUTION_ENVIRONMENT_VALUES:
            raise ValueError("pre-settlement event has an unknown environment")
        if not self.source_profile.strip() or not self.source_config_id.strip():
            raise ValueError("pre-settlement event source identity is incomplete")
        if not self.symbol or self.symbol != self.symbol.upper() or not self.symbol.isalnum():
            raise ValueError("pre-settlement event symbol must be uppercase alphanumeric")
        if self.carry_side not in {None, "long", "short"}:
            raise ValueError("pre-settlement event carry_side is invalid")
        if any(
            type(value) is not int or value <= 0
            for value in (self.decision_ts_ms, self.fired_ts_ms, self.settlement_ts_ms)
        ):
            raise ValueError("pre-settlement event timestamps must be positive integers")
        if self.settlement_ts_ms <= self.fired_ts_ms:
            raise ValueError("pre-settlement event settlement must follow the fire")
        if not math.isfinite(self.running_rate):
            raise ValueError("pre-settlement event running_rate must be finite")
        for name, value in (
            ("mark_px", self.mark_px),
            ("carry_qty", self.carry_qty),
            ("carry_avg_entry_px", self.carry_avg_entry_px),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0.0):
                raise ValueError(f"pre-settlement event {name} must be null or positive and finite")
        if self.carry_side is None and (self.carry_qty is not None or self.carry_avg_entry_px is not None):
            raise ValueError("pre-settlement event cannot carry quantity without a side")
        if self.carry_side is not None and (self.carry_qty is None or self.carry_avg_entry_px is None):
            raise ValueError("pre-settlement event holding is incomplete")

    @property
    def event_id(self) -> str:
        """Stable semantic id; a retry cannot open a second Exodus record."""

        identity = {
            "schema_version": PRESETTLEMENT_EVENT_SCHEMA_VERSION,
            "environment": self.environment,
            "source_config_id": self.source_config_id,
            "decision_ts_ms": self.decision_ts_ms,
            "settlement_ts_ms": self.settlement_ts_ms,
            "symbol": self.symbol,
        }
        return "carry-presettlement-" + hashlib.sha256(canonical_json(identity)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PRESETTLEMENT_EVENT_SCHEMA_VERSION,
            "event_id": self.event_id,
            "environment": self.environment,
            "source_profile": self.source_profile,
            "source_config_id": self.source_config_id,
            "decision_ts_ms": self.decision_ts_ms,
            "fired_ts_ms": self.fired_ts_ms,
            "settlement_ts_ms": self.settlement_ts_ms,
            "symbol": self.symbol,
            "running_rate": self.running_rate,
            "mark_px": self.mark_px,
            "carry_side": self.carry_side,
            "carry_qty": self.carry_qty,
            "carry_avg_entry_px": self.carry_avg_entry_px,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CarryPresettlementEvent":
        expected = {
            "schema_version",
            "event_id",
            "environment",
            "source_profile",
            "source_config_id",
            "decision_ts_ms",
            "fired_ts_ms",
            "settlement_ts_ms",
            "symbol",
            "running_rate",
            "mark_px",
            "carry_side",
            "carry_qty",
            "carry_avg_entry_px",
        }
        if set(payload) != expected:
            raise ValueError("pre-settlement event has unexpected or missing fields")
        if payload["schema_version"] != PRESETTLEMENT_EVENT_SCHEMA_VERSION:
            raise ValueError("unsupported pre-settlement event schema")
        event = cls(
            environment=str(payload["environment"]),
            source_profile=str(payload["source_profile"]),
            source_config_id=str(payload["source_config_id"]),
            decision_ts_ms=payload["decision_ts_ms"],
            fired_ts_ms=payload["fired_ts_ms"],
            settlement_ts_ms=payload["settlement_ts_ms"],
            symbol=str(payload["symbol"]),
            running_rate=payload["running_rate"],
            mark_px=payload["mark_px"],
            carry_side=(None if payload["carry_side"] is None else str(payload["carry_side"])),
            carry_qty=payload["carry_qty"],
            carry_avg_entry_px=payload["carry_avg_entry_px"],
        )
        if payload["event_id"] != event.event_id:
            raise ValueError("pre-settlement event id does not match its contents")
        return event

    def to_strategy_event(self) -> StrategyEvent:
        sequence = int(self.event_id.rsplit("-", 1)[1][:16], 16)
        return StrategyEvent(
            event_ts_ns=self.fired_ts_ms * 1_000_000,
            ingest_ts_ns=self.fired_ts_ms * 1_000_000,
            source=f"{PRESETTLEMENT_EVENT_SOURCE}:{self.environment}",
            source_sequence=sequence,
            kind=PRESETTLEMENT_EVENT_KIND,
            payload=self.to_dict(),
        )


def _typed_event(event: StrategyEvent) -> CarryPresettlementEvent:
    if event.kind != PRESETTLEMENT_EVENT_KIND:
        raise ValueError(f"pre-settlement tape contains event kind {event.kind!r}")
    typed = CarryPresettlementEvent.from_dict(event.payload)
    if event.source != f"{PRESETTLEMENT_EVENT_SOURCE}:{typed.environment}":
        raise ValueError("pre-settlement event source disagrees with its environment")
    return typed


def load_carry_presettlement_events(
    path: str | Path,
) -> tuple[CarryPresettlementEvent, ...]:
    """Load and fully verify the hash chain and every typed payload."""

    events, _tape_hash = load_strategy_event_tape(path)
    return tuple(_typed_event(event) for event in events)


def append_carry_presettlement_event(
    path: str | Path,
    event: CarryPresettlementEvent,
) -> tuple[str, bool]:
    """Append once by semantic id; return ``(tape_hash, appended)``."""

    tape = JsonlStrategyEventTape(path)
    for prior in tape.prior_events:
        typed = _typed_event(prior)
        if typed.event_id == event.event_id:
            if typed != event:
                raise ValueError("pre-settlement event id already exists with different contents")
            # A prior process can die after the complete row reaches the page
            # cache but before its file or directory sync completes. Seeing the
            # row proves identity, not durability, so a semantic retry repairs
            # both before CARRY is allowed to persist its exit mask.
            tape.ensure_durable()
            return tape.tape_hash, False
    strategy_event = event.to_strategy_event()
    if tape.prior_events and strategy_event.order_key < tape.prior_events[-1].order_key:
        raise ValueError("pre-settlement event tape cannot move backward")
    return tape.append(strategy_event), True
