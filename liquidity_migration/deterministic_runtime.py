"""Deterministic runtime primitives for replay-sensitive execution code.

The execution kernel must not discover time, identity, or scheduling order from
ambient process state.  Live adapters may use :class:`SystemClock`; historical
and fault tests use :class:`VirtualClock` and :class:`VirtualScheduler`.
"""

from __future__ import annotations

import heapq
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


class Clock(Protocol):
    """Clock port used by deterministic domain code."""

    def wall_time_ns(self) -> int: ...

    def monotonic_ns(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Production clock adapter.  Keep it outside replay comparisons."""

    def wall_time_ns(self) -> int:
        return time.time_ns()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


@dataclass(slots=True)
class VirtualClock:
    """Explicit clock whose progress is controlled only by the input tape."""

    current_wall_ns: int = 1_000_000_000
    current_monotonic_ns: int = 0

    def __post_init__(self) -> None:
        if self.current_wall_ns <= 0:
            raise ValueError("current_wall_ns must be positive")
        if self.current_monotonic_ns < 0:
            raise ValueError("current_monotonic_ns cannot be negative")

    def wall_time_ns(self) -> int:
        return self.current_wall_ns

    def monotonic_ns(self) -> int:
        return self.current_monotonic_ns

    def advance_ns(self, delta_ns: int) -> None:
        if delta_ns < 0:
            raise ValueError("virtual time cannot move backwards")
        self.current_wall_ns += delta_ns
        self.current_monotonic_ns += delta_ns

    def advance_to_wall_ns(self, wall_ns: int) -> None:
        if wall_ns < self.current_wall_ns:
            raise ValueError("virtual wall time cannot move backwards")
        self.advance_ns(wall_ns - self.current_wall_ns)


_ID_NAMESPACE = uuid.UUID("9f6d676e-ef33-49ef-a4ba-f63a574e7da2")


@dataclass(frozen=True, slots=True)
class DeterministicIds:
    """Stable UUIDs derived from a seed and an explicit semantic key."""

    seed: str

    def make(self, kind: str, *parts: object) -> str:
        material = "\x1f".join((self.seed, kind, *(str(part) for part in parts)))
        return str(uuid.uuid5(_ID_NAMESPACE, material))


@dataclass(slots=True)
class SeededRandom:
    """Small explicit wrapper which prevents accidental global RNG use."""

    seed: int | str
    _random: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._random = random.Random(self.seed)

    def random(self) -> float:
        return self._random.random()

    def uniform(self, low: float, high: float) -> float:
        return self._random.uniform(low, high)

    def randint(self, low: int, high: int) -> int:
        return self._random.randint(low, high)

    def choice(self, values: list[Any]) -> Any:
        if not values:
            raise ValueError("cannot choose from an empty list")
        return self._random.choice(values)

    def state(self) -> object:
        return self._random.getstate()

    def restore(self, state: object) -> None:
        self._random.setstate(state)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    due_monotonic_ns: int
    ordinal: int
    task_id: str
    kind: str
    payload: Mapping[str, Any]


@dataclass(slots=True)
class VirtualScheduler:
    """Deterministic data scheduler; callers dispatch task kinds themselves.

    It deliberately stores data instead of callbacks so a schedule can be
    serialized, replayed, hashed, and reconstructed after a crash.
    """

    clock: VirtualClock
    ids: DeterministicIds
    _heap: list[tuple[int, int, ScheduledTask]] = field(default_factory=list, init=False, repr=False)
    _next_ordinal: int = field(default=0, init=False)

    def schedule_at(
        self,
        due_monotonic_ns: int,
        *,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        task_key: str = "",
    ) -> ScheduledTask:
        if due_monotonic_ns < self.clock.monotonic_ns():
            raise ValueError("cannot schedule a task in the virtual past")
        ordinal = self._next_ordinal
        self._next_ordinal += 1
        task_id = self.ids.make("task", task_key or ordinal, due_monotonic_ns, kind)
        task = ScheduledTask(
            due_monotonic_ns=due_monotonic_ns,
            ordinal=ordinal,
            task_id=task_id,
            kind=kind,
            payload=dict(payload or {}),
        )
        heapq.heappush(self._heap, (due_monotonic_ns, ordinal, task))
        return task

    def schedule_after(
        self,
        delay_ns: int,
        *,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        task_key: str = "",
    ) -> ScheduledTask:
        if delay_ns < 0:
            raise ValueError("delay_ns cannot be negative")
        return self.schedule_at(
            self.clock.monotonic_ns() + delay_ns,
            kind=kind,
            payload=payload,
            task_key=task_key,
        )

    def pop_due(self) -> list[ScheduledTask]:
        due: list[ScheduledTask] = []
        now = self.clock.monotonic_ns()
        while self._heap and self._heap[0][0] <= now:
            due.append(heapq.heappop(self._heap)[2])
        return due

    def advance_to_next(self) -> list[ScheduledTask]:
        if not self._heap:
            return []
        due_ns = self._heap[0][0]
        self.clock.advance_ns(due_ns - self.clock.monotonic_ns())
        return self.pop_due()

    def pending(self) -> tuple[ScheduledTask, ...]:
        return tuple(row[2] for row in sorted(self._heap))
