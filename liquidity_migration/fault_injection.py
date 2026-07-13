"""Seeded delivery faults for WebSocket/REST/timer recovery tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from .deterministic_runtime import DeterministicIds, SeededRandom, VirtualClock, VirtualScheduler


@dataclass(frozen=True, slots=True)
class TapeMessage:
    message_id: str
    channel: str
    event_ts_ns: int
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DeliveryFaultPolicy:
    drop_probability: float = 0.0
    duplicate_probability: float = 0.0
    max_delay_ns: int = 0
    max_duplicates: int = 1
    never_drop_channels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("drop_probability", self.drop_probability),
            ("duplicate_probability", self.duplicate_probability),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{label} must be in [0, 1]")
        if self.max_delay_ns < 0 or self.max_duplicates < 0:
            raise ValueError("delay and duplicate bounds cannot be negative")


@dataclass(frozen=True, slots=True)
class DeliveredMessage:
    delivery_id: str
    source_message_id: str
    channel: str
    event_ts_ns: int
    delivery_ts_ns: int
    duplicate_index: int
    payload: Mapping[str, Any]


@dataclass(slots=True)
class DeterministicFaultInjector:
    """Transform one input tape into a repeatable faulty delivery tape."""

    seed: int | str
    policy: DeliveryFaultPolicy
    start_wall_ns: int = 1_000_000_000
    _rng: SeededRandom = field(init=False, repr=False)
    _ids: DeterministicIds = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = SeededRandom(self.seed)
        self._ids = DeterministicIds(str(self.seed))

    def transform(self, messages: Iterable[TapeMessage]) -> tuple[DeliveredMessage, ...]:
        source = sorted(messages, key=lambda item: (item.event_ts_ns, item.message_id))
        if not source:
            return ()
        origin = min(item.event_ts_ns for item in source)
        clock = VirtualClock(current_wall_ns=self.start_wall_ns, current_monotonic_ns=0)
        scheduler = VirtualScheduler(clock=clock, ids=self._ids)
        for message in source:
            protected = message.channel in self.policy.never_drop_channels
            if not protected and self._rng.random() < self.policy.drop_probability:
                continue
            copies = 1
            while copies <= self.policy.max_duplicates and self._rng.random() < self.policy.duplicate_probability:
                copies += 1
            for duplicate_index in range(copies):
                delay_ns = self._rng.randint(0, self.policy.max_delay_ns) if self.policy.max_delay_ns else 0
                due_ns = message.event_ts_ns - origin + delay_ns
                scheduler.schedule_at(
                    due_ns,
                    kind="deliver",
                    task_key=f"{message.message_id}:{duplicate_index}",
                    payload={
                        "message": asdict(message),
                        "duplicate_index": duplicate_index,
                    },
                )
        delivered: list[DeliveredMessage] = []
        while scheduler.pending():
            for task in scheduler.advance_to_next():
                raw = task.payload["message"]
                duplicate_index = int(task.payload["duplicate_index"])
                delivered.append(DeliveredMessage(
                    delivery_id=self._ids.make("delivery", raw["message_id"], duplicate_index, task.due_monotonic_ns),
                    source_message_id=str(raw["message_id"]),
                    channel=str(raw["channel"]),
                    event_ts_ns=int(raw["event_ts_ns"]),
                    delivery_ts_ns=self.start_wall_ns + task.due_monotonic_ns,
                    duplicate_index=duplicate_index,
                    payload=dict(raw["payload"]),
                ))
        return tuple(delivered)
