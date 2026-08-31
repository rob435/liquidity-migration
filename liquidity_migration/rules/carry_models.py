"""Typed CARRY research inputs and native takeover state."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


CARRY_MODEL_SCHEMA_VERSION = 1
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS

_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"
CARRY_CONFIG_PATH = _CONFIGS_DIR / "lane2_carry_hold_v7.json"
CARRY_PROFILE_NAME = "carry_hold_v7_live_v1"


def _plain_symbol(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.upper()
        or not value.isalnum()
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _finite(value: object, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} must be finite")
    return float(value)


@dataclass(frozen=True)
class CarryDecision:
    """One registered daily weight decision used by the Rust replay."""

    decision_ts_ms: int
    weights: Mapping[str, float]
    universe_size: int
    replay_days: int
    gross: float

    def __post_init__(self) -> None:
        if type(self.decision_ts_ms) is not int or self.decision_ts_ms <= 0:
            raise ValueError("CARRY decision time must be a positive integer")
        if type(self.universe_size) is not int or self.universe_size < 0:
            raise ValueError("CARRY universe size must be a non-negative integer")
        if type(self.replay_days) is not int or self.replay_days < 0:
            raise ValueError("CARRY replay days must be a non-negative integer")
        normalized: dict[str, float] = {}
        for symbol, raw_weight in self.weights.items():
            typed_symbol = _plain_symbol(symbol, label="CARRY decision symbol")
            weight = _finite(raw_weight, label="CARRY decision weight")
            if weight <= 0.0:
                raise ValueError("CARRY decision weights must be positive")
            normalized[typed_symbol] = weight
        if len(normalized) != len(self.weights):
            raise ValueError("CARRY decision contains duplicate symbols")
        _finite(self.gross, label="CARRY decision gross")
        object.__setattr__(self, "weights", dict(sorted(normalized.items())))
        object.__setattr__(self, "gross", sum(normalized.values()))

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": CARRY_MODEL_SCHEMA_VERSION,
            "decision_ts_ms": self.decision_ts_ms,
            "weights": dict(self.weights),
            "universe_size": self.universe_size,
            "replay_days": self.replay_days,
            "gross": self.gross,
        }


@dataclass(frozen=True, slots=True)
class SettledFundingObservation:
    symbol: str
    settlement_ts_ms: int
    rate: float

    def __post_init__(self) -> None:
        _plain_symbol(self.symbol, label="CARRY settled-funding symbol")
        if type(self.settlement_ts_ms) is not int or self.settlement_ts_ms <= 0:
            raise ValueError("CARRY settlement time must be positive")
        object.__setattr__(
            self,
            "rate",
            _finite(self.rate, label="CARRY settled-funding rate"),
        )


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
        _plain_symbol(self.symbol, label="CARRY pre-settlement symbol")
        for name in ("observed_ts_ms", "settlement_ts_ms"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"CARRY pre-settlement {name} must be positive")
        object.__setattr__(
            self,
            "running_rate",
            _finite(self.running_rate, label="CARRY pre-settlement rate"),
        )
        if self.mark_px is not None:
            mark_px = _finite(self.mark_px, label="CARRY pre-settlement mark")
            if mark_px <= 0.0:
                raise ValueError("CARRY pre-settlement mark must be positive")
            object.__setattr__(self, "mark_px", mark_px)
        if self.carry_side not in {None, "long", "short"}:
            raise ValueError("CARRY pre-settlement holding side is invalid")
        holding_values = (self.carry_qty, self.carry_avg_entry_px)
        if self.carry_side is None and any(value is not None for value in holding_values):
            raise ValueError("CARRY pre-settlement holding is incomplete")
        if self.carry_side is not None and any(value is None for value in holding_values):
            raise ValueError("CARRY pre-settlement holding is incomplete")
        for name in ("carry_qty", "carry_avg_entry_px"):
            value = getattr(self, name)
            if value is None:
                continue
            normalized = _finite(value, label=f"CARRY pre-settlement {name}")
            if normalized <= 0.0:
                raise ValueError("CARRY pre-settlement holding values must be positive")
            object.__setattr__(self, name, normalized)


@dataclass(frozen=True, slots=True)
class PriorState:
    """Durable CARRY lifecycle fields accepted by native takeover."""

    sizing_anchors: tuple[tuple[int, float], ...] = ()
    fired_exits: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        anchors: dict[int, float] = {}
        for decision_ts_ms, equity_usdt in self.sizing_anchors:
            if type(decision_ts_ms) is not int or decision_ts_ms <= 0:
                raise ValueError("CARRY sizing anchor has an invalid decision time")
            equity = _finite(equity_usdt, label="CARRY sizing anchor equity")
            if equity <= 0.0:
                raise ValueError("CARRY sizing anchor equity must be positive")
            if decision_ts_ms in anchors:
                raise ValueError("CARRY sizing anchors contain a duplicate decision")
            anchors[decision_ts_ms] = equity
        if len(anchors) > 2:
            raise ValueError("CARRY sizing anchors retain more than two decisions")

        fired: dict[str, int] = {}
        for symbol, decision_ts_ms in self.fired_exits:
            typed_symbol = _plain_symbol(symbol, label="CARRY fired-exit symbol")
            if type(decision_ts_ms) is not int or decision_ts_ms <= 0:
                raise ValueError("CARRY fired-exit decision time is invalid")
            if typed_symbol in fired:
                raise ValueError("CARRY fired exits contain a duplicate symbol")
            fired[typed_symbol] = decision_ts_ms
        object.__setattr__(self, "sizing_anchors", tuple(sorted(anchors.items())))
        object.__setattr__(self, "fired_exits", tuple(sorted(fired.items())))

    def anchor_by_decision(self) -> dict[int, float]:
        return dict(self.sizing_anchors)

    def fired_by_symbol(self) -> dict[str, int]:
        return dict(self.fired_exits)

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": CARRY_MODEL_SCHEMA_VERSION,
            "sizing_anchors": [list(row) for row in self.sizing_anchors],
            "fired_exits": [list(row) for row in self.fired_exits],
        }


__all__ = [
    "CARRY_MODEL_SCHEMA_VERSION",
    "CARRY_CONFIG_PATH",
    "CARRY_PROFILE_NAME",
    "CarryDecision",
    "DAY_MS",
    "HOUR_MS",
    "PresettlementObservation",
    "PriorState",
    "SettledFundingObservation",
]
