"""Wedged-command detection: the kernel suppresses all command generation while an order is
working, and two ordinary events make "working" permanent.
"""

from __future__ import annotations

from dataclasses import dataclass

from liquidity_migration.wedged_command_watch import (
    DEFAULT_WEDGE_AFTER_NS,
    WEDGE_AMBIGUOUS_SUBMISSION,
    WEDGE_NEVER_SUBMITTED,
    wedged_commands,
)

SEC = 1_000_000_000
NOW = 1_000_000 * SEC


@dataclass
class _Order:
    command_id: str = "c1"
    symbol: str = "LAUSDT"
    signed_qty: float = 100.0
    reduce_only: bool = False
    status: str = "commanded"
    submission_attempts: int = 0
    last_submission_started_ts_ns: int = 0
    created_ts_ns: int = NOW


def test_a_fresh_command_is_in_flight_not_wedged() -> None:
    order = _Order(created_ts_ns=NOW - 1 * SEC)
    assert wedged_commands([order], now_ns=NOW) == ()


def test_an_ambiguous_submission_wedges_once_it_ages_out() -> None:
    """place_order lost its answer. It cannot be resent and cannot be assumed
    dead, so today it retries every ~2s forever with no escalation."""

    order = _Order(
        submission_attempts=1,
        last_submission_started_ts_ns=NOW - DEFAULT_WEDGE_AFTER_NS - SEC,
    )
    found = wedged_commands([order], now_ns=NOW)
    assert len(found) == 1
    assert found[0].kind == WEDGE_AMBIGUOUS_SUBMISSION
    assert found[0].symbol == "LAUSDT"


def test_a_never_submitted_command_wedges_too() -> None:
    """The SIGKILL case: journaled, then the process died before dispatch."""

    order = _Order(created_ts_ns=NOW - DEFAULT_WEDGE_AFTER_NS - SEC)
    found = wedged_commands([order], now_ns=NOW)
    assert len(found) == 1
    assert found[0].kind == WEDGE_NEVER_SUBMITTED


def test_queue_time_before_dispatch_does_not_count_against_the_command() -> None:
    """Age runs from the attempt when there was one. A command that waited in a
    queue is not wedged for the time it spent waiting."""

    order = _Order(
        submission_attempts=1,
        created_ts_ns=NOW - 10 * DEFAULT_WEDGE_AFTER_NS,
        last_submission_started_ts_ns=NOW - 1 * SEC,
    )
    assert wedged_commands([order], now_ns=NOW) == ()


def test_only_commanded_orders_can_wedge() -> None:
    old = NOW - DEFAULT_WEDGE_AFTER_NS - SEC
    for status in ("filled", "rejected", "cancelled", "partially_filled_cancelled"):
        order = _Order(status=status, created_ts_ns=old)
        assert wedged_commands([order], now_ns=NOW) == (), status


def test_a_frozen_open_position_outranks_a_stuck_exit() -> None:
    """A non-reduce-only wedge is strictly worse: the position it froze is still
    open and the system can no longer close it."""

    old = NOW - DEFAULT_WEDGE_AFTER_NS - SEC
    stuck_exit = _Order(command_id="exit", symbol="AAA", reduce_only=True, created_ts_ns=old)
    frozen_entry = _Order(
        command_id="entry", symbol="ZZZ", reduce_only=False, created_ts_ns=old
    )

    found = wedged_commands([stuck_exit, frozen_entry], now_ns=NOW)
    assert [w.command_id for w in found] == ["entry", "exit"]
    assert found[0].blocks_exit is True
    assert found[1].blocks_exit is False


def test_the_description_names_the_lost_exit_path() -> None:
    order = _Order(
        submission_attempts=1,
        last_submission_started_ts_ns=NOW - DEFAULT_WEDGE_AFTER_NS - SEC,
    )
    detail = wedged_commands([order], now_ns=NOW)[0].describe()
    assert "LAUSDT" in detail
    assert "no exit path" in detail


def test_a_command_with_no_usable_clock_is_not_guessed_at() -> None:
    order = _Order(created_ts_ns=0, last_submission_started_ts_ns=0)
    assert wedged_commands([order], now_ns=NOW) == ()


def test_the_bound_is_configurable_for_a_tighter_real_money_posture() -> None:
    order = _Order(created_ts_ns=NOW - 30 * SEC)
    assert wedged_commands([order], now_ns=NOW) == ()
    assert len(wedged_commands([order], now_ns=NOW, wedge_after_ns=10 * SEC)) == 1
