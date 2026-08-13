"""Pre-boundary quiescence: the owner loop goes quiet before a known boundary.

The daily intent lands at a fixed UTC boundary and used to hit a pass that was
mid-Telegram-send or mid-wallet-read roughly one boundary in ten. These tests
pin the window predicate (strictly before the anchor, day rollover included),
the anchor parser, and the deferral shape the runner arms with it.
"""

from __future__ import annotations

import pytest

from liquidity_migration.runtime.account_service_runner import (
    PRE_BOUNDARY_QUIESCE_WINDOW_SECONDS,
    PreBoundaryQuiescence,
    parse_pre_boundary_anchors,
)


def _at(hour: int, minute: int, second: float) -> float:
    """A wall timestamp whose UTC time-of-day is exactly hour:minute:second."""

    day_start = 86400.0 * 20_000  # any whole UTC day
    return day_start + hour * 3600 + minute * 60 + second


def test_parse_accepts_lists_and_rejects_malformed_anchors() -> None:
    assert parse_pre_boundary_anchors("") == ()
    assert parse_pre_boundary_anchors("00:20") == ((0, 20),)
    assert parse_pre_boundary_anchors("12:00, 00:20,00:20") == ((0, 20), (12, 0))
    for bad in ("0020", "24:00", "12:60", "aa:bb", "1:2:3"):
        with pytest.raises(ValueError):
            parse_pre_boundary_anchors(bad)


def test_window_is_strictly_before_the_anchor() -> None:
    quiesce = PreBoundaryQuiescence([(0, 20)], window_seconds=2.0)
    # Well before the window: quiet time has not started.
    assert not quiesce.active(_at(0, 19, 57.9))
    # Window opens at anchor - W.
    assert quiesce.active(_at(0, 19, 58.0))
    assert quiesce.active(_at(0, 19, 59.5))
    # The anchor instant itself is outside: from here the arrived intent's own
    # deferral (arrival_pending) takes over.
    assert not quiesce.active(_at(0, 20, 0.0))
    assert not quiesce.active(_at(0, 20, 1.0))


def test_window_rolls_over_midnight() -> None:
    quiesce = PreBoundaryQuiescence([(0, 0)], window_seconds=2.0)
    assert not quiesce.active(_at(23, 59, 57.0))
    assert quiesce.active(_at(23, 59, 58.5))
    assert quiesce.active(_at(23, 59, 59.9))
    assert not quiesce.active(_at(0, 0, 0.0))


def test_any_configured_anchor_arms_the_window() -> None:
    quiesce = PreBoundaryQuiescence([(0, 20), (12, 0)], window_seconds=2.0)
    assert quiesce.active(_at(11, 59, 59.0))
    assert quiesce.active(_at(0, 19, 59.0))
    assert not quiesce.active(_at(6, 0, 0.0))


def test_unset_spec_disables_quiescence_entirely() -> None:
    quiesce = PreBoundaryQuiescence(parse_pre_boundary_anchors(""), window_seconds=2.0)
    assert not quiesce.enabled
    assert not quiesce.active(_at(0, 19, 59.0))
    assert not quiesce.active(_at(0, 20, 0.0))


def test_deferral_arms_inside_the_window() -> None:
    """The runner ORs this predicate into the existing intent-arrival
    deferrals (reconciler pending polls, funding queries); inside the window
    the combined deferral must report deferred even with no intent pending."""

    now = {"ts": _at(0, 19, 59.0)}
    quiesce = PreBoundaryQuiescence(
        [(0, 20)],
        window_seconds=PRE_BOUNDARY_QUIESCE_WINDOW_SECONDS,
        wall_clock=lambda: now["ts"],
    )
    arrival_pending_answers = {"pending": False}

    def intent_imminent_or_pending() -> bool:
        # The exact closure shape the runner assigns to
        # reconciler.pending_poll_deferral / funding_reconciler.query_deferral.
        return arrival_pending_answers["pending"] or quiesce.active()

    assert intent_imminent_or_pending()
    now["ts"] = _at(0, 20, 30.0)
    assert not intent_imminent_or_pending()
    arrival_pending_answers["pending"] = True
    assert intent_imminent_or_pending()


def test_window_must_be_positive_and_finite() -> None:
    with pytest.raises(ValueError):
        PreBoundaryQuiescence([(0, 20)], window_seconds=0.0)
    with pytest.raises(ValueError):
        PreBoundaryQuiescence([(0, 20)], window_seconds=float("inf"))


def test_the_window_is_bounded_above() -> None:
    """An oversized window would silently suppress the wallet refresh the
    loss guard evaluates, and every Telegram send, for the whole day."""

    with pytest.raises(ValueError, match="cannot exceed 10 seconds"):
        PreBoundaryQuiescence(((0, 20),), window_seconds=10.5, wall_clock=lambda: 0.0)
    # The documented maximum itself is legal.
    PreBoundaryQuiescence(((0, 20),), window_seconds=10.0, wall_clock=lambda: 0.0)
