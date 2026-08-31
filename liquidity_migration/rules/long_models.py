"""Typed LONG request and response models for the native Rust reducer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping


LONG_MODEL_SCHEMA_VERSION = 1


class DecisionAction(StrEnum):
    """State changes returned by the native LONG reducer."""

    WAIT = "wait"
    ENTER = "enter"
    HOLD = "hold"
    EXIT = "exit"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class DecisionInput:
    decision_ts_ms: int
    symbol: str
    signal_ts_ms: int = 0
    signal_close: float = 0.0
    market_price: float | None = None
    observed_low: float | None = None
    equity_usdt: float = 1.0
    feature_row: Mapping[str, Any] | None = None

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": LONG_MODEL_SCHEMA_VERSION,
            "decision_ts_ms": self.decision_ts_ms,
            "symbol": self.symbol,
            "signal_ts_ms": self.signal_ts_ms,
            "signal_close": self.signal_close,
            "market_price": self.market_price,
            "observed_low": self.observed_low,
            "equity_usdt": self.equity_usdt,
            "feature_row": dict(self.feature_row or {}),
        }


@dataclass(frozen=True, slots=True)
class PriorState:
    requested: bool = False
    filled: bool = False
    entry_ts_ms: int = 0
    entry_price: float = 0.0
    target_notional_usdt: float = 0.0
    stop_loss_fraction: float = 0.0
    stop_decay_after_ms: int = 0
    decayed_stop_loss_fraction: float = 0.0
    max_hold_deadline_ts_ms: int = 0
    entry_valid_until_ms: int = 0
    cooldown_until_ms: int = 0
    attempted_signal_ts_ms: int = 0
    active_positions: int = 0

    def as_json_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DecisionOutput:
    action: DecisionAction
    reason: str
    decision_ts_ms: int
    symbol: str
    signal_ts_ms: int = 0
    entry_reason: str = ""
    position_weight: float = 0.0
    target_fraction_of_equity: float = 0.0
    target_notional_usdt: float = 0.0
    entry_leverage: float = 0.0
    stop_loss_fraction: float = 0.0
    stop_decay_after_ms: int = 0
    decayed_stop_loss_fraction: float = 0.0
    max_hold_duration_ms: int = 0
    entry_valid_until_ms: int = 0
    wake_at_or_below: float | None = None

    def as_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": LONG_MODEL_SCHEMA_VERSION,
            "action": self.action.value,
            "reason": self.reason,
            "decision_ts_ms": self.decision_ts_ms,
            "symbol": self.symbol,
            "signal_ts_ms": self.signal_ts_ms,
            "entry_reason": self.entry_reason,
            "position_weight": self.position_weight,
            "target_fraction_of_equity": self.target_fraction_of_equity,
            "target_notional_usdt": self.target_notional_usdt,
            "entry_leverage": self.entry_leverage,
            "stop_loss_fraction": self.stop_loss_fraction,
            "stop_decay_after_ms": self.stop_decay_after_ms,
            "decayed_stop_loss_fraction": self.decayed_stop_loss_fraction,
            "max_hold_duration_ms": self.max_hold_duration_ms,
            "entry_valid_until_ms": self.entry_valid_until_ms,
            "wake_at_or_below": self.wake_at_or_below,
        }


__all__ = [
    "DecisionAction",
    "DecisionInput",
    "DecisionOutput",
    "LONG_MODEL_SCHEMA_VERSION",
    "PriorState",
]
