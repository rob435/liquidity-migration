from __future__ import annotations

import json

import pytest

from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.strategy_event_clock import (
    DeterministicEventClock,
    JsonlStrategyEventTape,
    MemoryStrategyEventTape,
    StrategyEvent,
    load_strategy_event_tape,
)


def _events() -> tuple[StrategyEvent, ...]:
    return (
        StrategyEvent(1_000, 1_010, "market", 1, "market_boundary", {"bar": 1}),
        StrategyEvent(1_000, 1_011, "timer", 1, "timer", {"heartbeat": 1}),
        StrategyEvent(2_000, 2_010, "market", 2, "confirmed_bar", {"bar": 2}),
    )


def test_same_tape_uses_one_order_and_produces_one_hash() -> None:
    outputs: list[tuple[str, int]] = []
    first_tape = MemoryStrategyEventTape()
    first = DeterministicEventClock(
        clock=VirtualClock(current_wall_ns=1_000), recorder=first_tape
    )
    first.replay(
        _events(),
        lambda event: outputs.append((event.kind, event.source_sequence)),
    )

    second_outputs: list[tuple[str, int]] = []
    second_tape = MemoryStrategyEventTape()
    second = DeterministicEventClock(
        clock=VirtualClock(current_wall_ns=1_000), recorder=second_tape
    )
    second.replay(
        _events(),
        lambda event: second_outputs.append((event.kind, event.source_sequence)),
    )

    assert outputs == second_outputs
    assert first.tape_hash == second.tape_hash
    assert first.clock.wall_time_ns() == 2_000


def test_same_timestamp_phase_order_and_duplicate_identity_fail_closed() -> None:
    dispatcher = DeterministicEventClock(
        clock=VirtualClock(current_wall_ns=1_000),
        recorder=MemoryStrategyEventTape(),
    )
    timer = StrategyEvent(1_000, 1_000, "timer", 1, "timer")
    dispatcher.dispatch(timer, lambda _event: None)
    with pytest.raises(ValueError, match="backward"):
        dispatcher.dispatch(
            StrategyEvent(1_000, 1_001, "market", 1, "market_boundary"),
            lambda _event: None,
        )
    with pytest.raises(ValueError, match="duplicate"):
        dispatcher.dispatch(timer, lambda _event: None)


def test_jsonl_tape_round_trips_and_detects_tampering(tmp_path) -> None:
    path = tmp_path / "strategy-events.jsonl"
    recorder = JsonlStrategyEventTape(path)
    dispatcher = DeterministicEventClock(
        clock=VirtualClock(current_wall_ns=1_000), recorder=recorder
    )
    dispatcher.replay(_events(), lambda _event: None)

    loaded, tape_hash = load_strategy_event_tape(path)
    assert loaded == _events()
    assert tape_hash == dispatcher.tape_hash

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["event"]["payload"]["heartbeat"] = 2
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(ValueError, match="id|hash"):
        load_strategy_event_tape(path)
