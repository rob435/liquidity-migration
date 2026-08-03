"""One ordered event clock for historical and live strategy scheduling.

Arrival adapters differ: historical runs supply a timestamped tape, while demo
daemons receive timer and confirmed-bar wakeups.  Every adapter must
turn that arrival into :class:`StrategyEvent` and use the same dispatcher.  The
dispatcher owns ordering, virtual-clock advancement, duplicate detection, and
a hash-chained input tape; strategy callbacks do not read ambient time.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generic, Mapping, Protocol, Sequence, TypeVar

from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.deterministic_runtime import Clock, VirtualClock
from liquidity_migration.core.deterministic_serialization import canonical_json, json_safe


_GENESIS_HASH = hashlib.sha256(b"liquidity-migration-strategy-event-tape-v1").hexdigest()
_PHASES = {
    "startup": 0,
    "market_boundary": 10,
    "confirmed_bar": 10,
    "timer": 20,
}


@dataclass(frozen=True, slots=True)
class StrategyEvent:
    """A replayable scheduling input with an explicit total-order key."""

    event_ts_ns: int
    ingest_ts_ns: int
    source: str
    source_sequence: int
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.event_ts_ns <= 0 or self.ingest_ts_ns <= 0:
            raise ValueError("strategy event timestamps must be positive")
        if not self.source.strip() or not self.kind.strip():
            raise ValueError("strategy event source and kind are required")
        if self.source_sequence < 0:
            raise ValueError("strategy event source_sequence cannot be negative")
        if self.kind not in _PHASES:
            raise ValueError(f"unknown strategy event kind: {self.kind}")
        # Fail at the boundary instead of discovering non-deterministic payloads
        # while hashing or replaying a tape later.
        canonical_json({"payload": json_safe(dict(self.payload))})

    @property
    def event_id(self) -> str:
        material = {
            "event_ts_ns": self.event_ts_ns,
            "kind": self.kind,
            "payload": json_safe(dict(self.payload)),
            "source": self.source,
            "source_sequence": self.source_sequence,
        }
        return "strategy-event-" + hashlib.sha256(canonical_json(material)).hexdigest()

    @property
    def order_key(self) -> tuple[int, int, str, int, str]:
        return (
            self.event_ts_ns,
            _PHASES[self.kind],
            self.source,
            self.source_sequence,
            self.event_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_ts_ns": self.event_ts_ns,
            "ingest_ts_ns": self.ingest_ts_ns,
            "source": self.source,
            "source_sequence": self.source_sequence,
            "kind": self.kind,
            "payload": json_safe(dict(self.payload)),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "StrategyEvent":
        expected_fields = {
            "event_id",
            "event_ts_ns",
            "ingest_ts_ns",
            "source",
            "source_sequence",
            "kind",
            "payload",
        }
        if set(row) != expected_fields or not isinstance(row.get("payload"), Mapping):
            raise ValueError("strategy event has unexpected, missing, or invalid fields")
        event = cls(
            event_ts_ns=int(row["event_ts_ns"]),
            ingest_ts_ns=int(row["ingest_ts_ns"]),
            source=str(row["source"]),
            source_sequence=int(row["source_sequence"]),
            kind=str(row["kind"]),
            payload=dict(row["payload"]),
        )
        if str(row.get("event_id") or "") != event.event_id:
            raise ValueError("strategy event id does not match canonical contents")
        return event


def _next_tape_hash(prior_hash: str, event: StrategyEvent) -> str:
    return hashlib.sha256(
        prior_hash.encode("ascii") + canonical_json({"event": event.to_dict()})
    ).hexdigest()


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
                raise OSError("strategy event tape append made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class StrategyEventRecorder(Protocol):
    @property
    def tape_hash(self) -> str: ...

    @property
    def prior_events(self) -> tuple[StrategyEvent, ...]: ...

    def append(self, event: StrategyEvent) -> str: ...


@dataclass(slots=True)
class MemoryStrategyEventTape:
    """In-memory tape used by deterministic tests and ephemeral callers."""

    _events: list[StrategyEvent] = field(default_factory=list)
    _tape_hash: str = _GENESIS_HASH

    @property
    def tape_hash(self) -> str:
        return self._tape_hash

    @property
    def prior_events(self) -> tuple[StrategyEvent, ...]:
        return tuple(self._events)

    def append(self, event: StrategyEvent) -> str:
        self._tape_hash = _next_tape_hash(self._tape_hash, event)
        self._events.append(event)
        return self._tape_hash


class JsonlStrategyEventTape:
    """Append-only canonical JSONL tape with a verified rolling hash chain."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        loaded, self._tape_hash = load_strategy_event_tape(self.path)
        # A list appends in O(1); the tuple rebuild made every append O(tape
        # age), quadratic over a long-lived daemon.
        self._events: list[StrategyEvent] = list(loaded)

    @property
    def tape_hash(self) -> str:
        return self._tape_hash

    @property
    def prior_events(self) -> tuple[StrategyEvent, ...]:
        return tuple(self._events)

    def append(self, event: StrategyEvent) -> str:
        prior_hash = self._tape_hash
        tape_hash = _next_tape_hash(prior_hash, event)
        record = {
            "schema_version": 1,
            "prior_tape_hash": prior_hash,
            "tape_hash": tape_hash,
            "event": event.to_dict(),
        }
        _append_private_line(self.path, canonical_json(record) + b"\n")
        self._events.append(event)
        self._tape_hash = tape_hash
        return tape_hash


def load_strategy_event_tape_bytes(data: bytes) -> tuple[tuple[StrategyEvent, ...], str]:
    """Parse and fully verify captured strategy-event tape bytes."""

    import json

    events: list[StrategyEvent] = []
    tape_hash = _GENESIS_HASH
    previous_order_key: tuple[int, int, str, int, str] | None = None
    seen: set[str] = set()
    for line_number, raw in enumerate(data.splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n"):
            raise ValueError(f"strategy event tape has a partial line at {line_number}")
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid strategy event tape JSON at line {line_number}") from exc
        if not isinstance(row, Mapping) or set(row) != {
            "schema_version",
            "prior_tape_hash",
            "tape_hash",
            "event",
        }:
            raise ValueError(f"strategy event tape has invalid fields at line {line_number}")
        if int(row.get("schema_version", 0)) != 1:
            raise ValueError(f"unknown strategy event tape schema at line {line_number}")
        if str(row.get("prior_tape_hash") or "") != tape_hash:
            raise ValueError(f"strategy event tape chain break at line {line_number}")
        raw_event = row.get("event")
        if not isinstance(raw_event, Mapping):
            raise ValueError(f"strategy event tape has an invalid event at line {line_number}")
        event = StrategyEvent.from_dict(raw_event)
        expected_hash = _next_tape_hash(tape_hash, event)
        if str(row.get("tape_hash") or "") != expected_hash:
            raise ValueError(f"strategy event tape hash mismatch at line {line_number}")
        if event.event_id in seen:
            raise ValueError(f"duplicate strategy event at line {line_number}")
        if previous_order_key is not None and event.order_key < previous_order_key:
            raise ValueError(f"strategy event tape moves backward at line {line_number}")
        events.append(event)
        seen.add(event.event_id)
        previous_order_key = event.order_key
        tape_hash = expected_hash
    return tuple(events), tape_hash


def load_strategy_event_tape(path: str | Path) -> tuple[tuple[StrategyEvent, ...], str]:
    """Read and fully verify a strategy event tape; corruption fails closed."""

    resolved = Path(path)
    if not resolved.exists():
        return (), _GENESIS_HASH
    snapshot = read_stable_file(
        resolved,
        label="strategy event tape",
    )
    return load_strategy_event_tape_bytes(snapshot.data)


T = TypeVar("T")


class DeterministicEventClock(Generic[T]):
    """Validate, record, and dispatch every strategy scheduling input."""

    def __init__(self, *, clock: Clock, recorder: StrategyEventRecorder | None = None) -> None:
        self.clock = clock
        self.recorder = recorder or MemoryStrategyEventTape()
        prior = self.recorder.prior_events
        self._last_order_key = prior[-1].order_key if prior else None
        self._seen_event_ids = {event.event_id for event in prior}

    @property
    def tape_hash(self) -> str:
        return self.recorder.tape_hash

    def dispatch(self, event: StrategyEvent, handler: Callable[[StrategyEvent], T]) -> T:
        if event.event_id in self._seen_event_ids:
            raise ValueError(f"duplicate strategy event: {event.event_id}")
        if self._last_order_key is not None and event.order_key < self._last_order_key:
            raise ValueError("strategy event clock cannot move backward")
        if isinstance(self.clock, VirtualClock):
            self.clock.advance_to_wall_ns(event.event_ts_ns)
        # The input is durable before strategy side effects. If the handler
        # crashes, replay sees the attempted input and downstream idempotency
        # keys decide whether work must be resumed.
        self.recorder.append(event)
        self._seen_event_ids.add(event.event_id)
        self._last_order_key = event.order_key
        return handler(event)

    def replay(
        self,
        events: Sequence[StrategyEvent],
        handler: Callable[[StrategyEvent], T],
    ) -> tuple[T, ...]:
        """Dispatch a preordered tape through exactly the live callback path."""

        return tuple(self.dispatch(event, handler) for event in events)
