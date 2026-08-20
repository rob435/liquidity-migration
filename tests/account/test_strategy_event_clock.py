from __future__ import annotations

import json

import pytest

from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.strategy_event_clock import (
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
    assert path.stat().st_mode & 0o777 == 0o600

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["event"]["payload"]["heartbeat"] = 2
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(ValueError, match="id|hash"):
        load_strategy_event_tape(path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: {**row, "unsigned_extra": True},
        lambda row: {**row, "event": {**row["event"], "unsigned_extra": True}},
        lambda _row: [],
    ],
)
def test_tape_rejects_unhashed_or_non_object_fields(tmp_path, mutate) -> None:
    path = tmp_path / "strategy-events.jsonl"
    recorder = JsonlStrategyEventTape(path)
    recorder.append(_events()[0])
    row = json.loads(path.read_text())
    path.write_text(json.dumps(mutate(row)) + "\n")

    with pytest.raises(ValueError, match="invalid fields|unexpected"):
        load_strategy_event_tape(path)


def test_every_cycle_kind_the_host_can_set_is_a_kind_the_clock_accepts() -> None:
    # strategy_host._run_one_cycle builds a StrategyEvent straight from
    # _pending_cycle_kind, so a kind assigned anywhere in the host but missing
    # from the clock's table kills the producer at the first such wake — both
    # LONG producers died on the first live price_touch (2026-08-20 ~03:00 UTC).
    import inspect
    import re

    from liquidity_migration.strategy import strategy_host

    kinds = set(
        re.findall(r'_pending_cycle_kind = "([a-z_]+)"', inspect.getsource(strategy_host))
    )
    assert "price_touch" in kinds, "the host seam moved; repoint this test at it"
    for kind in sorted(kinds):
        StrategyEvent(1_000, 1_010, "host", 1, kind, {})
