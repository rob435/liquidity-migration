"""Deterministic clocks and identifiers for replay-sensitive execution code.

The execution kernel must not discover time, identity, or scheduling order from
ambient process state. Live adapters use :class:`SystemClock`; historical paths
use :class:`VirtualClock`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Protocol


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
