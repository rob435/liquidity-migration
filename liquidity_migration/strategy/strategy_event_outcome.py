"""Hash-chained strategy decision outcomes keyed to durable input events.

The strategy-event tape is committed before its callback executes, so callback
decisions cannot truthfully be embedded in that input record.  This companion
tape is appended after the callback and binds an explicit (possibly empty)
sorted decision-key list to the already-durable raw ``StrategyEvent.event_id``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json


_GENESIS_HASH = hashlib.sha256(b"liquidity-migration-strategy-event-decision-tape-v1").hexdigest()


def _event_id(value: object) -> str:
    event_id = str(value or "")
    prefix = "strategy-event-"
    digest = event_id.removeprefix(prefix)
    if (
        not event_id.startswith(prefix)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("strategy decision outcome has an invalid event id")
    return event_id


def _decision_keys(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("strategy decision outcome decision_keys must be a list")
    keys = tuple(str(value) for value in values)
    if any(not key for key in keys):
        raise ValueError("strategy decision outcome has an empty decision key")
    if len(set(keys)) != len(keys):
        raise ValueError("strategy decision outcome repeats a decision key")
    if keys != tuple(sorted(keys)):
        raise ValueError("strategy decision outcome decision keys are not canonically sorted")
    return keys


@dataclass(frozen=True, slots=True)
class StrategyEventDecisionOutcome:
    event_id: str
    decision_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _event_id(self.event_id))
        object.__setattr__(self, "decision_keys", _decision_keys(list(self.decision_keys)))

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "decision_keys": list(self.decision_keys),
        }

    @classmethod
    def from_dict(cls, row: object) -> "StrategyEventDecisionOutcome":
        if not isinstance(row, dict) or set(row) != {"event_id", "decision_keys"}:
            raise ValueError("strategy decision outcome has invalid fields")
        return cls(
            event_id=_event_id(row["event_id"]),
            decision_keys=_decision_keys(row["decision_keys"]),
        )


def _next_tape_hash(prior_hash: str, outcome: StrategyEventDecisionOutcome) -> str:
    return hashlib.sha256(prior_hash.encode("ascii") + canonical_json({"outcome": outcome.to_dict()})).hexdigest()


def _append_private_line(path: Path, data: bytes) -> None:
    """Append one durable evidence row and force owner-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("strategy outcome tape append made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_strategy_event_decision_tape_bytes(
    data: bytes,
) -> tuple[tuple[StrategyEventDecisionOutcome, ...], str]:
    """Parse and fully verify captured companion-outcome tape bytes."""

    outcomes: list[StrategyEventDecisionOutcome] = []
    tape_hash = _GENESIS_HASH
    seen: set[str] = set()
    for line_number, raw in enumerate(data.splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n"):
            raise ValueError(f"strategy decision tape has a partial line at {line_number}")
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid strategy decision tape JSON at line {line_number}") from exc
        if not isinstance(row, dict) or set(row) != {
            "schema_version",
            "prior_tape_hash",
            "tape_hash",
            "outcome",
        }:
            raise ValueError(f"strategy decision tape has invalid fields at line {line_number}")
        if int(row.get("schema_version") or 0) != 1:
            raise ValueError(f"unknown strategy decision tape schema at line {line_number}")
        if str(row.get("prior_tape_hash") or "") != tape_hash:
            raise ValueError(f"strategy decision tape chain break at line {line_number}")
        outcome = StrategyEventDecisionOutcome.from_dict(row.get("outcome"))
        expected_hash = _next_tape_hash(tape_hash, outcome)
        if str(row.get("tape_hash") or "") != expected_hash:
            raise ValueError(f"strategy decision tape hash mismatch at line {line_number}")
        if outcome.event_id in seen:
            raise ValueError(f"duplicate strategy decision outcome at line {line_number}")
        outcomes.append(outcome)
        seen.add(outcome.event_id)
        tape_hash = expected_hash
    return tuple(outcomes), tape_hash


def load_strategy_event_decision_tape(
    path: str | Path,
) -> tuple[tuple[StrategyEventDecisionOutcome, ...], str]:
    """Read and fully verify one companion outcome tape."""

    resolved = Path(path)
    if not resolved.exists():
        return (), _GENESIS_HASH
    snapshot = read_stable_file(
        resolved,
        label="strategy decision tape",
    )
    return load_strategy_event_decision_tape_bytes(snapshot.data)


class JsonlStrategyEventDecisionTape:
    """Append-only companion outcomes for a durable strategy-event tape."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._outcomes, self._tape_hash = load_strategy_event_decision_tape(self.path)
        self._seen = {outcome.event_id for outcome in self._outcomes}

    @property
    def tape_hash(self) -> str:
        return self._tape_hash

    def append(self, event_id: str, decision_keys: Sequence[str]) -> StrategyEventDecisionOutcome:
        outcome = StrategyEventDecisionOutcome(
            event_id=event_id,
            decision_keys=tuple(decision_keys),
        )
        if outcome.event_id in self._seen:
            raise ValueError(f"duplicate strategy decision outcome: {outcome.event_id}")
        prior_hash = self._tape_hash
        tape_hash = _next_tape_hash(prior_hash, outcome)
        record = {
            "schema_version": 1,
            "prior_tape_hash": prior_hash,
            "tape_hash": tape_hash,
            "outcome": outcome.to_dict(),
        }
        _append_private_line(self.path, canonical_json(record) + b"\n")
        self._outcomes = (*self._outcomes, outcome)
        self._seen.add(outcome.event_id)
        self._tape_hash = tape_hash
        return outcome
