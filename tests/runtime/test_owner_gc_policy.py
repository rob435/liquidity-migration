"""The owner keeps tidying-up pauses (garbage collection) off the order path."""

from __future__ import annotations

import gc
from pathlib import Path

import pytest

from liquidity_migration.runtime.account_service_runner import (
    OWNER_GC_YOUNG_THRESHOLD,
    OwnerGcPolicy,
)


class _RecordingGc:
    """Stands in for the ``gc`` module so the order of calls is visible."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.threshold: tuple[int, ...] = (2000, 10, 10)

    def collect(self, *generation: int) -> int:
        self.calls.append(f"collect{generation if generation else ''}")
        return 0

    def freeze(self) -> None:
        self.calls.append("freeze")

    def get_threshold(self) -> tuple[int, ...]:
        return self.threshold

    def set_threshold(self, *values: int) -> None:
        self.threshold = values
        self.calls.append(f"set_threshold{values}")


@pytest.fixture()
def recording_gc(monkeypatch: pytest.MonkeyPatch) -> _RecordingGc:
    import liquidity_migration.runtime.account_service_runner as runner

    fake = _RecordingGc()
    monkeypatch.setattr(runner, "gc", fake)
    return fake


def test_the_first_settled_pass_collects_before_it_freezes(recording_gc: _RecordingGc) -> None:
    """Freezing keeps whatever is tracked at that instant for the life of the
    process. Freeze first and the startup pass's rubbish is kept forever, so
    the collect has to come first -- this is an ordering test, not a
    both-happened test.
    """

    policy = OwnerGcPolicy()
    assert policy.settle() is True

    assert recording_gc.calls == [
        "collect",
        "freeze",
        f"set_threshold({OWNER_GC_YOUNG_THRESHOLD}, 10, 10)",
    ]
    assert policy.warm is True


def test_the_warm_up_keeps_the_older_generation_thresholds(recording_gc: _RecordingGc) -> None:
    recording_gc.threshold = (700, 17, 23)
    OwnerGcPolicy().settle()

    assert recording_gc.threshold == (OWNER_GC_YOUNG_THRESHOLD, 17, 23)


def test_every_later_pass_runs_a_full_collect_not_a_young_one(
    recording_gc: _RecordingGc,
) -> None:
    """A young-generation-only collect resets the counter that drives the older
    generations, so nothing ever sweeps them. Measured over 20,000 passes that
    is 1,200,038 live objects and 1,811 MB against a 118 MB baseline. The
    collect here must take no generation argument.
    """

    policy = OwnerGcPolicy()
    policy.settle()
    recording_gc.calls.clear()

    assert policy.settle() is True
    assert policy.settle() is True

    assert recording_gc.calls == ["collect", "collect"]


def test_a_waiting_intent_defers_the_collect_once_warm(recording_gc: _RecordingGc) -> None:
    policy = OwnerGcPolicy()
    policy.settle()
    recording_gc.calls.clear()

    assert policy.settle(defer=True) is False

    assert recording_gc.calls == []


def test_a_waiting_intent_never_defers_the_one_time_warm_up(
    recording_gc: _RecordingGc,
) -> None:
    """Otherwise a busy start leaves the process on the interpreter's defaults
    indefinitely, which is the case this whole policy exists to leave.
    """

    policy = OwnerGcPolicy()

    assert policy.settle(defer=True) is True
    assert policy.warm is True
    assert "freeze" in recording_gc.calls


def test_the_policy_still_reclaims_cycles_made_after_the_freeze() -> None:
    """Against the live interpreter, not a stub: the startup graph leaves the
    collector's view, and rubbish made afterwards is still collected. That
    second half is why the process cannot grow without bound.
    """

    original_threshold = gc.get_threshold()
    try:
        assert OwnerGcPolicy().settle() is True

        assert gc.get_freeze_count() > 0
        assert gc.get_threshold()[0] == OWNER_GC_YOUNG_THRESHOLD

        class _Node:
            peer: object

        first, second = _Node(), _Node()
        first.peer, second.peer = second, first
        del first, second

        assert gc.collect() >= 2
    finally:
        gc.unfreeze()
        gc.set_threshold(*original_threshold)


def test_the_owner_loop_settles_gc_at_the_end_of_every_pass() -> None:
    """Source order, because the value is entirely in where the call sits.

    The collect has to come after every stage that allocates and immediately
    before the loop blocks, so it never delays work in this pass and there is
    nothing left to collect in the next one.
    """

    repo = Path(__file__).resolve().parents[2]
    source = (repo / "liquidity_migration" / "runtime" / "account_service_runner.py").read_text(
        encoding="utf-8"
    )
    loop = source[source.index("        while True:") :]

    assert "gc_policy = OwnerGcPolicy()" in source
    assert loop.count("gc_policy.settle(") == 1, "one collect per pass, not one per stage"

    settle = loop.index("gc_policy.settle(")
    wait = loop.index("intent_watch.wait(")
    order_path = loop.index("run_ready_request_or_converge(")
    safety_flat = loop.index("service.run_safety_flat_once(inbox)")
    notification = loop.index("deliver_notification_batch(")

    assert order_path < settle, "the order path must not queue behind a collect"
    assert safety_flat < settle, "a breach's flat must not queue behind a collect"
    assert notification < settle, "collect after the stages that allocate, not before"
    assert settle < wait, "the collect belongs immediately before the loop blocks"

    assert "gc_policy.settle(defer=intent_watch.arrival_pending())" in loop, (
        "a waiting intent must start its pass without paying for the collect"
    )
