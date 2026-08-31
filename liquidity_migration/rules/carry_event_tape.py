"""Read the recorded CARRY pre-settlement event tape for research replay."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.rules.exodus_models import carry_presettlement_event_id


_EVENT_TAPE_VERSION = 1
_CARRY_EVENT_VERSION = 1
_EVENT_KIND = "presettlement_exit"
_EVENT_SOURCE = "carry_hold"
_EVENT_TAPE_GENESIS_HASH = hashlib.sha256(
    b"liquidity-migration-strategy-event-tape-v1"
).hexdigest()
_EXECUTION_ENVIRONMENTS = frozenset({"demo", "mainnet"})


class CarryEventTapeError(ValueError):
    """The recorded event tape cannot be decoded without guessing."""


def _json_object(data: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CarryEventTapeError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise CarryEventTapeError(f"{label} must contain an object")
    if canonical_json(value) + b"\n" != data:
        raise CarryEventTapeError(f"{label} is not canonical JSON")
    return value


def _positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise CarryEventTapeError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise CarryEventTapeError(f"{label} must be a non-negative integer")
    return value


def _finite(value: object, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise CarryEventTapeError(f"{label} must be finite")
    return float(value)


def _positive_float(value: object, *, label: str) -> float:
    parsed = _finite(value, label=label)
    if parsed <= 0.0:
        raise CarryEventTapeError(f"{label} must be positive")
    return parsed


def _symbol(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.upper()
        or not value.isalnum()
    ):
        raise CarryEventTapeError(
            "CARRY pre-settlement event symbol must be uppercase alphanumeric"
        )
    return value


@dataclass(frozen=True, slots=True)
class CarryPresettlementEvent:
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
        if self.environment not in _EXECUTION_ENVIRONMENTS:
            raise CarryEventTapeError(
                "CARRY pre-settlement event environment is invalid"
            )
        if (
            not isinstance(self.source_profile, str)
            or not self.source_profile.strip()
            or not isinstance(self.source_config_id, str)
            or not self.source_config_id.strip()
        ):
            raise CarryEventTapeError(
                "CARRY pre-settlement event source identity is incomplete"
            )
        _symbol(self.symbol)
        _positive_int(
            self.decision_ts_ms,
            label="CARRY pre-settlement decision timestamp",
        )
        _positive_int(self.fired_ts_ms, label="CARRY pre-settlement fire timestamp")
        _positive_int(
            self.settlement_ts_ms,
            label="CARRY pre-settlement settlement timestamp",
        )
        if self.fired_ts_ms < self.decision_ts_ms:
            raise CarryEventTapeError(
                "CARRY pre-settlement event fires before its decision"
            )
        if self.settlement_ts_ms <= self.fired_ts_ms:
            raise CarryEventTapeError(
                "CARRY pre-settlement settlement must follow its fire"
            )
        _finite(self.running_rate, label="CARRY pre-settlement running rate")
        for label, value in (
            ("mark price", self.mark_px),
            ("carry quantity", self.carry_qty),
            ("carry average entry", self.carry_avg_entry_px),
        ):
            if value is not None:
                _positive_float(value, label=f"CARRY pre-settlement {label}")
        if self.carry_side not in {None, "long", "short"}:
            raise CarryEventTapeError("CARRY pre-settlement side is invalid")
        holding_values = (self.carry_qty, self.carry_avg_entry_px)
        if self.carry_side is None and any(value is not None for value in holding_values):
            raise CarryEventTapeError(
                "CARRY pre-settlement event cannot carry quantity without a side"
            )
        if self.carry_side is not None and any(value is None for value in holding_values):
            raise CarryEventTapeError(
                "CARRY pre-settlement event holding is incomplete"
            )

    @property
    def event_id(self) -> str:
        return carry_presettlement_event_id(
            environment=self.environment,
            source_config_id=self.source_config_id,
            decision_ts_ms=self.decision_ts_ms,
            settlement_ts_ms=self.settlement_ts_ms,
            symbol=self.symbol,
        )


def _carry_event(payload: object) -> CarryPresettlementEvent:
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
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise CarryEventTapeError(
            "CARRY pre-settlement event has unexpected or missing fields"
        )
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != _CARRY_EVENT_VERSION
    ):
        raise CarryEventTapeError("unsupported CARRY pre-settlement event schema")
    for name in ("environment", "source_profile", "source_config_id", "symbol"):
        if not isinstance(payload[name], str):
            raise CarryEventTapeError(
                f"CARRY pre-settlement {name} must be a string"
            )
    carry_side = payload["carry_side"]
    if carry_side is not None and not isinstance(carry_side, str):
        raise CarryEventTapeError(
            "CARRY pre-settlement carry_side must be null or a string"
        )
    event = CarryPresettlementEvent(
        environment=payload["environment"],
        source_profile=payload["source_profile"],
        source_config_id=payload["source_config_id"],
        decision_ts_ms=payload["decision_ts_ms"],
        fired_ts_ms=payload["fired_ts_ms"],
        settlement_ts_ms=payload["settlement_ts_ms"],
        symbol=payload["symbol"],
        running_rate=payload["running_rate"],
        mark_px=payload["mark_px"],
        carry_side=carry_side,
        carry_qty=payload["carry_qty"],
        carry_avg_entry_px=payload["carry_avg_entry_px"],
    )
    if payload["event_id"] != event.event_id:
        raise CarryEventTapeError(
            "CARRY pre-settlement event id does not match its contents"
        )
    return event


def decode_carry_presettlement_events(
    data: bytes,
) -> tuple[CarryPresettlementEvent, ...]:
    if data and not data.endswith(b"\n"):
        raise CarryEventTapeError("CARRY event tape has an unterminated final row")
    tape_hash = _EVENT_TAPE_GENESIS_HASH
    prior_order: tuple[int, str, int, str] | None = None
    generic_ids: set[str] = set()
    semantic_ids: set[str] = set()
    output: list[CarryPresettlementEvent] = []
    for line_number, raw in enumerate(data.splitlines(keepends=True), start=1):
        row = _json_object(raw, label=f"CARRY event tape row {line_number}")
        if set(row) != {"schema_version", "prior_tape_hash", "tape_hash", "event"}:
            raise CarryEventTapeError(
                f"CARRY event tape row {line_number} has invalid fields"
            )
        if (
            type(row["schema_version"]) is not int
            or row["schema_version"] != _EVENT_TAPE_VERSION
            or row["prior_tape_hash"] != tape_hash
        ):
            raise CarryEventTapeError(
                f"CARRY event tape chain breaks at row {line_number}"
            )
        event = row["event"]
        event_fields = {
            "event_id",
            "event_ts_ns",
            "ingest_ts_ns",
            "source",
            "source_sequence",
            "kind",
            "payload",
        }
        if not isinstance(event, Mapping) or set(event) != event_fields:
            raise CarryEventTapeError(
                f"CARRY strategy event at row {line_number} has invalid fields"
            )
        event_ts_ns = _positive_int(
            event["event_ts_ns"], label="CARRY event timestamp"
        )
        _positive_int(event["ingest_ts_ns"], label="CARRY event ingest timestamp")
        source_sequence = _nonnegative_int(
            event["source_sequence"], label="CARRY event source sequence"
        )
        if (
            not isinstance(event["source"], str)
            or not isinstance(event["kind"], str)
            or not isinstance(event["event_id"], str)
            or event["kind"] != _EVENT_KIND
        ):
            raise CarryEventTapeError(
                f"CARRY strategy event at row {line_number} is invalid"
            )
        identity = {
            "event_ts_ns": event_ts_ns,
            "kind": event["kind"],
            "payload": event["payload"],
            "source": event["source"],
            "source_sequence": source_sequence,
        }
        generic_id = "strategy-event-" + hashlib.sha256(
            canonical_json(identity)
        ).hexdigest()
        if event["event_id"] != generic_id or generic_id in generic_ids:
            raise CarryEventTapeError(
                f"CARRY strategy event id is invalid at row {line_number}"
            )
        generic_ids.add(generic_id)
        order = (event_ts_ns, event["source"], source_sequence, generic_id)
        if prior_order is not None and order < prior_order:
            raise CarryEventTapeError(
                f"CARRY event tape moves backward at row {line_number}"
            )
        prior_order = order
        next_hash = hashlib.sha256(
            tape_hash.encode("ascii") + canonical_json({"event": dict(event)})
        ).hexdigest()
        if row["tape_hash"] != next_hash:
            raise CarryEventTapeError(
                f"CARRY event tape hash is invalid at row {line_number}"
            )
        tape_hash = next_hash
        typed = _carry_event(event["payload"])
        if event["source"] != f"{_EVENT_SOURCE}:{typed.environment}":
            raise CarryEventTapeError(
                "CARRY event source disagrees with its environment"
            )
        if typed.event_id in semantic_ids:
            raise CarryEventTapeError("CARRY event tape repeats one semantic fire")
        semantic_ids.add(typed.event_id)
        output.append(typed)
    return tuple(sorted(output, key=lambda event: event.event_id))


def load_carry_presettlement_events(
    path: str | Path,
) -> tuple[CarryPresettlementEvent, ...]:
    resolved = Path(path)
    if not resolved.exists():
        return ()
    try:
        data = read_stable_file(
            resolved,
            label="CARRY pre-settlement event tape",
            reject_empty=False,
            require_single_link=True,
            max_bytes=64 * 1024 * 1024,
        ).data
    except (OSError, ValueError) as exc:
        raise CarryEventTapeError(str(exc)) from exc
    return decode_carry_presettlement_events(data)


def in_scope_carry_presettlement_events(
    events: tuple[CarryPresettlementEvent, ...],
    *,
    environment: str,
    source_profile: str,
    source_config_id: str,
) -> tuple[CarryPresettlementEvent, ...]:
    return tuple(
        event
        for event in events
        if event.environment == environment
        and event.source_profile == source_profile
        and event.source_config_id == source_config_id
    )


__all__ = [
    "CarryEventTapeError",
    "CarryPresettlementEvent",
    "decode_carry_presettlement_events",
    "in_scope_carry_presettlement_events",
    "load_carry_presettlement_events",
]
