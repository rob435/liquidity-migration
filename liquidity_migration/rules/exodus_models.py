"""Typed Exodus event and stopped-state codecs for native Rust replays."""

from __future__ import annotations

import dataclasses
import hashlib
import math
from collections.abc import Mapping
from typing import Any

from liquidity_migration.core.deterministic_serialization import canonical_json


EXODUS_STATE_SCHEMA_VERSION = 4


def carry_presettlement_event_id(
    *,
    environment: str,
    source_config_id: str,
    decision_ts_ms: int,
    settlement_ts_ms: int,
    symbol: str,
) -> str:
    """Return the semantic identity shared by native CARRY and Exodus."""

    identity = {
        "schema_version": 1,
        "environment": environment,
        "source_config_id": source_config_id,
        "decision_ts_ms": decision_ts_ms,
        "settlement_ts_ms": settlement_ts_ms,
        "symbol": symbol,
    }
    return "carry-presettlement-" + hashlib.sha256(
        canonical_json(identity)
    ).hexdigest()


def _plain_symbol(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.upper()
        or not value.isalnum()
    ):
        raise ValueError(f"{label} must be uppercase alphanumeric")
    return value


def _positive_float(value: object, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f"{label} must be positive and finite")
    return float(value)


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusTrigger:
    """The exact CARRY handoff fields consumed by native Exodus."""

    event_id: str
    environment: str
    source_profile: str
    source_config_id: str
    decision_ts_ms: int
    fired_ts_ms: int
    settlement_ts_ms: int
    symbol: str
    mark_px: float | None
    carry_side: str | None
    carry_qty: float | None

    def __post_init__(self) -> None:
        for name in ("event_id", "environment", "source_profile", "source_config_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Exodus trigger {name} must be a non-empty string")
        _plain_symbol(self.symbol, label="Exodus trigger symbol")
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.decision_ts_ms,
                self.fired_ts_ms,
                self.settlement_ts_ms,
            )
        ):
            raise ValueError("Exodus trigger timestamps must be positive integers")
        if self.fired_ts_ms < self.decision_ts_ms:
            raise ValueError("Exodus trigger cannot fire before its CARRY decision")
        if self.settlement_ts_ms <= self.fired_ts_ms:
            raise ValueError("Exodus trigger settlement must follow its fire")
        if self.carry_side not in {None, "long", "short"}:
            raise ValueError("Exodus trigger carry side is invalid")
        if self.mark_px is not None:
            object.__setattr__(
                self,
                "mark_px",
                _positive_float(self.mark_px, label="Exodus trigger mark_px"),
            )
        if self.carry_qty is not None:
            object.__setattr__(
                self,
                "carry_qty",
                _positive_float(self.carry_qty, label="Exodus trigger carry_qty"),
            )
        if self.carry_side is None and self.carry_qty is not None:
            raise ValueError("Exodus trigger cannot carry quantity without a side")
        if self.carry_side is not None and self.carry_qty is None:
            raise ValueError("Exodus trigger holding is incomplete")
        expected_event_id = carry_presettlement_event_id(
            environment=self.environment,
            source_config_id=self.source_config_id,
            decision_ts_ms=self.decision_ts_ms,
            settlement_ts_ms=self.settlement_ts_ms,
            symbol=self.symbol,
        )
        if self.event_id != expected_event_id:
            raise ValueError("Exodus trigger event id is invalid")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExodusTrigger:
        expected = {
            "event_id",
            "environment",
            "source_profile",
            "source_config_id",
            "decision_ts_ms",
            "fired_ts_ms",
            "settlement_ts_ms",
            "symbol",
            "mark_px",
            "carry_side",
            "carry_qty",
        }
        if set(payload) != expected:
            raise ValueError("Exodus trigger has unexpected or missing fields")
        return cls(**payload)


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusOpenRecord:
    """One open Exodus short read from a stopped takeover source."""

    symbol: str
    notional_usdt: float
    settlement_ts_ms: int
    fired_ts_ms: int
    target_qty: float | None = None

    def __post_init__(self) -> None:
        _plain_symbol(self.symbol, label="Exodus open-state symbol")
        object.__setattr__(
            self,
            "notional_usdt",
            _positive_float(
                self.notional_usdt,
                label="Exodus open-state notional_usdt",
            ),
        )
        if any(
            type(value) is not int or value <= 0
            for value in (self.settlement_ts_ms, self.fired_ts_ms)
        ):
            raise ValueError("Exodus open-state timestamps must be positive integers")
        if self.target_qty is not None:
            object.__setattr__(
                self,
                "target_qty",
                _positive_float(
                    self.target_qty,
                    label="Exodus open-state target_qty",
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _records_from_payload(payload: Mapping[str, Any]) -> tuple[ExodusOpenRecord, ...]:
    keys = set(payload)
    if keys == {"open"}:
        schema_version = 1
    elif keys == {"schema_version", "open"}:
        schema_version = payload["schema_version"]
    else:
        raise ValueError("Exodus state must contain exactly schema_version and open")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ValueError("unsupported Exodus state schema_version")
    rows = payload["open"]
    if not isinstance(rows, list):
        raise ValueError("Exodus state open must be an array")
    expected = {"symbol", "notional_usdt", "settlement_ts_ms", "fired_ts_ms"}
    if schema_version == 2:
        expected.add("target_qty")
    records: list[ExodusOpenRecord] = []
    symbols: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError(f"Exodus state row {index} has an invalid shape")
        try:
            record = ExodusOpenRecord(
                symbol=raw["symbol"],
                notional_usdt=raw["notional_usdt"],
                settlement_ts_ms=raw["settlement_ts_ms"],
                fired_ts_ms=raw["fired_ts_ms"],
                target_qty=raw.get("target_qty"),
            )
        except ValueError as exc:
            raise ValueError(f"Exodus state row {index} is invalid: {exc}") from exc
        if record.symbol in symbols:
            raise ValueError(f"Exodus state row {index} repeats a symbol")
        symbols.add(record.symbol)
        records.append(record)
    return tuple(sorted(records, key=lambda record: record.symbol))


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusState:
    """Typed Exodus state used by the Rust reducer replay contract."""

    open_records: tuple[ExodusOpenRecord, ...] = ()
    consumed_event_ids: tuple[str, ...] = ()
    entry_closed_ts_ms_by_symbol: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        symbols = [record.symbol for record in self.open_records]
        if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
            raise ValueError("Exodus open state must be uniquely sorted by symbol")
        if tuple(sorted(self.consumed_event_ids)) != self.consumed_event_ids:
            raise ValueError("Exodus consumed event ids must be sorted")
        if len(set(self.consumed_event_ids)) != len(self.consumed_event_ids):
            raise ValueError("Exodus consumed event ids contain duplicates")
        if any(
            not isinstance(value, str)
            or not value.startswith("carry-presettlement-")
            or len(value) != 84
            for value in self.consumed_event_ids
        ):
            raise ValueError("Exodus state contains an invalid consumed event id")
        closed_symbols = [symbol for symbol, _ in self.entry_closed_ts_ms_by_symbol]
        if closed_symbols != sorted(closed_symbols) or len(closed_symbols) != len(
            set(closed_symbols)
        ):
            raise ValueError("Exodus closed-entry state must be uniquely sorted")
        if any(
            _plain_symbol(symbol, label="Exodus closed-entry symbol") not in symbols
            or type(ts_ms) is not int
            or ts_ms <= 0
            for symbol, ts_ms in self.entry_closed_ts_ms_by_symbol
        ):
            raise ValueError("Exodus closed-entry state has an invalid row")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXODUS_STATE_SCHEMA_VERSION,
            "consumed_event_ids": list(self.consumed_event_ids),
            "entry_closed_ts_ms_by_symbol": dict(
                self.entry_closed_ts_ms_by_symbol
            ),
            "open": [record.to_dict() for record in self.open_records],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExodusState:
        keys = set(payload)
        if keys in ({"open"}, {"schema_version", "open"}):
            return cls(open_records=_records_from_payload(payload))
        if keys == {"schema_version", "consumed_event_ids", "open"}:
            if payload["schema_version"] != 3:
                raise ValueError("unsupported Exodus takeover state schema")
            raw_closed: Mapping[str, Any] = {}
        elif keys == {
            "schema_version",
            "consumed_event_ids",
            "entry_closed_ts_ms_by_symbol",
            "open",
        }:
            if payload["schema_version"] != EXODUS_STATE_SCHEMA_VERSION:
                raise ValueError("unsupported Exodus takeover state schema")
            candidate = payload["entry_closed_ts_ms_by_symbol"]
            if not isinstance(candidate, Mapping):
                raise ValueError(
                    "Exodus entry_closed_ts_ms_by_symbol must be an object"
                )
            raw_closed = candidate
        else:
            raise ValueError("Exodus state has unexpected or missing fields")
        consumed = payload["consumed_event_ids"]
        if not isinstance(consumed, list) or any(
            not isinstance(row, str) for row in consumed
        ):
            raise ValueError("Exodus consumed_event_ids must be an array of strings")
        records = _records_from_payload(
            {"schema_version": 2, "open": payload["open"]}
        )
        return cls(
            open_records=records,
            consumed_event_ids=tuple(consumed),
            entry_closed_ts_ms_by_symbol=tuple(
                sorted((str(symbol), ts_ms) for symbol, ts_ms in raw_closed.items())
            ),
        )


__all__ = [
    "EXODUS_STATE_SCHEMA_VERSION",
    "ExodusOpenRecord",
    "ExodusState",
    "ExodusTrigger",
    "carry_presettlement_event_id",
]
