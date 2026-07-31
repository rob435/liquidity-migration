"""Account-level loss halt behaviour: a restart keeps the day's anchor, a stale feed is
not read as flat, and a trip does not clear itself on the next tick.
"""

from __future__ import annotations

import datetime as dt

import pytest

from liquidity_migration.policy.account_loss_guard import (
    LOSS_GUARD_BLOCKED,
    LOSS_GUARD_OK,
    LOSS_GUARD_TRIPPED,
    AccountLossGuard,
)

SEC = 1_000_000_000


def _ts(day: str, hour: int = 12) -> int:
    moment = dt.datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, tzinfo=dt.timezone.utc
    )
    return int(moment.timestamp() * SEC)


def test_first_reading_of_a_day_sets_the_anchor_and_never_trips() -> None:
    guard = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    ts = _ts("2026-07-30")
    state, detail = guard.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts)
    assert (state, detail) == (LOSS_GUARD_OK, "")
    assert guard.opening_equity == 250_000.0


def test_a_loss_below_the_ceiling_is_not_a_trip() -> None:
    guard = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    ts = _ts("2026-07-30")
    guard.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts)
    state, _ = guard.evaluate(
        equity_usdt=249_001.0, equity_ts_ns=ts + SEC, now_ns=ts + SEC
    )
    assert state == LOSS_GUARD_OK


def test_reaching_the_ceiling_trips_and_the_trip_is_permanent() -> None:
    guard = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    ts = _ts("2026-07-30")
    guard.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts)
    state, detail = guard.evaluate(
        equity_usdt=249_000.0, equity_ts_ns=ts + SEC, now_ns=ts + SEC
    )
    assert state == LOSS_GUARD_TRIPPED
    assert "1,000.00 USDT reached" in detail

    # Equity recovering fully does not un-trip it.
    state, _ = guard.evaluate(
        equity_usdt=260_000.0, equity_ts_ns=ts + 2 * SEC, now_ns=ts + 2 * SEC
    )
    assert state == LOSS_GUARD_TRIPPED
    # Nor does a new day.
    later = _ts("2026-07-31")
    state, _ = guard.evaluate(equity_usdt=260_000.0, equity_ts_ns=later, now_ns=later)
    assert state == LOSS_GUARD_TRIPPED


def test_only_an_explicit_reset_clears_a_trip() -> None:
    guard = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    ts = _ts("2026-07-30")
    guard.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts)
    guard.evaluate(equity_usdt=248_000.0, equity_ts_ns=ts + SEC, now_ns=ts + SEC)
    assert guard.tripped
    guard.reset()
    assert not guard.tripped
    state, _ = guard.evaluate(
        equity_usdt=248_000.0, equity_ts_ns=ts + 2 * SEC, now_ns=ts + 2 * SEC
    )
    assert state == LOSS_GUARD_OK


def test_a_new_utc_day_re_anchors_the_budget() -> None:
    guard = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    day1 = _ts("2026-07-30")
    guard.evaluate(equity_usdt=250_000.0, equity_ts_ns=day1, now_ns=day1)
    guard.evaluate(equity_usdt=249_500.0, equity_ts_ns=day1 + SEC, now_ns=day1 + SEC)
    day2 = _ts("2026-07-31")
    guard.evaluate(equity_usdt=249_500.0, equity_ts_ns=day2, now_ns=day2)
    assert guard.opening_equity == 249_500.0
    # The prior day's 500 loss no longer counts against the new day.
    state, _ = guard.evaluate(
        equity_usdt=248_600.0, equity_ts_ns=day2 + SEC, now_ns=day2 + SEC
    )
    assert state == LOSS_GUARD_OK


def test_stale_equity_blocks_new_risk_but_does_not_trip() -> None:
    guard = AccountLossGuard(
        max_daily_loss_usdt=1_000.0, max_equity_staleness_ns=120 * SEC
    )
    ts = _ts("2026-07-30")
    guard.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts)
    state, detail = guard.evaluate(
        equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts + 121 * SEC
    )
    assert state == LOSS_GUARD_BLOCKED
    assert "stale" in detail
    assert not guard.tripped, "staleness must never flatten the book"


def test_a_feed_that_recovers_returns_to_ok() -> None:
    guard = AccountLossGuard(
        max_daily_loss_usdt=1_000.0, max_equity_staleness_ns=120 * SEC
    )
    ts = _ts("2026-07-30")
    guard.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts)
    assert (
        guard.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts + 200 * SEC)[0]
        == LOSS_GUARD_BLOCKED
    )
    fresh = ts + 210 * SEC
    assert (
        guard.evaluate(equity_usdt=249_900.0, equity_ts_ns=fresh, now_ns=fresh)[0]
        == LOSS_GUARD_OK
    )


def test_never_having_read_equity_blocks() -> None:
    guard = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    state, detail = guard.evaluate(equity_usdt=None, equity_ts_ns=None, now_ns=_ts("2026-07-30"))
    assert state == LOSS_GUARD_BLOCKED
    assert "no account equity" in detail


def test_a_backwards_clock_blocks_rather_than_trusting_the_reading() -> None:
    guard = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    ts = _ts("2026-07-30")
    state, detail = guard.evaluate(
        equity_usdt=250_000.0, equity_ts_ns=ts + 5 * SEC, now_ns=ts
    )
    assert state == LOSS_GUARD_BLOCKED
    assert "future" in detail


def test_a_restart_that_restores_its_anchor_keeps_the_days_budget() -> None:
    """The hole this closes: without a persisted anchor a crash-loop grants an
    unlimited daily budget, one restart at a time."""
    ts = _ts("2026-07-30")
    first = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    first.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts)
    first.evaluate(equity_usdt=249_200.0, equity_ts_ns=ts + SEC, now_ns=ts + SEC)
    saved = first.snapshot()

    revived = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    revived.restore(saved)
    assert revived.opening_equity == 250_000.0
    state, _ = revived.evaluate(
        equity_usdt=249_000.0, equity_ts_ns=ts + 2 * SEC, now_ns=ts + 2 * SEC
    )
    assert state == LOSS_GUARD_TRIPPED


def test_a_restart_without_the_anchor_would_have_refreshed_the_budget() -> None:
    """Documents precisely why the snapshot exists."""
    ts = _ts("2026-07-30")
    naive = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    naive.evaluate(equity_usdt=249_200.0, equity_ts_ns=ts + SEC, now_ns=ts + SEC)
    assert naive.opening_equity == 249_200.0
    state, _ = naive.evaluate(
        equity_usdt=249_000.0, equity_ts_ns=ts + 2 * SEC, now_ns=ts + 2 * SEC
    )
    assert state == LOSS_GUARD_OK


def test_a_restored_trip_stays_tripped() -> None:
    ts = _ts("2026-07-30")
    first = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    first.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts)
    first.evaluate(equity_usdt=248_000.0, equity_ts_ns=ts + SEC, now_ns=ts + SEC)
    revived = AccountLossGuard(max_daily_loss_usdt=1_000.0)
    revived.restore(first.snapshot())
    assert revived.tripped
    assert (
        revived.evaluate(equity_usdt=260_000.0, equity_ts_ns=ts + 2 * SEC, now_ns=ts + 2 * SEC)[0]
        == LOSS_GUARD_TRIPPED
    )


def test_an_unset_ceiling_never_trips_but_still_blocks_on_blindness() -> None:
    """Demo default: the machinery runs and is observable without a ceiling."""
    guard = AccountLossGuard(max_daily_loss_usdt=None, max_equity_staleness_ns=120 * SEC)
    ts = _ts("2026-07-30")
    guard.evaluate(equity_usdt=250_000.0, equity_ts_ns=ts, now_ns=ts)
    state, _ = guard.evaluate(
        equity_usdt=1_000.0, equity_ts_ns=ts + SEC, now_ns=ts + SEC
    )
    assert state == LOSS_GUARD_OK
    state, _ = guard.evaluate(
        equity_usdt=1_000.0, equity_ts_ns=ts + SEC, now_ns=ts + 500 * SEC
    )
    assert state == LOSS_GUARD_BLOCKED


def test_a_non_positive_ceiling_is_rejected_at_construction() -> None:
    for bad in (0.0, -1.0):
        with pytest.raises(ValueError, match="must be positive"):
            AccountLossGuard(max_daily_loss_usdt=bad)
