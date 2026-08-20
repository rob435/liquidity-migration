"""Deadline-driven producer passes: a time-based exit fires when its deadline
passes, not when the polling grid next happens to run.

Each cycle reports the earliest future instant a time rule can change its
book (LONG: a max-hold stop coming due or a v12 decayed stop arming; CARRY:
the daily 00:20 UTC decision boundary). The daemon cuts its wait short at
that instant and mints the resulting cycle as ``market_boundary``. A deadline
fires at most one cycle — if that cycle cannot clear it, the grid retries —
so a suppressed exit can never turn the deadline into a hot loop.
"""

from __future__ import annotations

import time
from pathlib import Path

import polars as pl

from liquidity_migration.account.account_intent_client import ExitFirstPublication
from liquidity_migration.account.account_route import ensure_account_route
from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.execution_environment import account_id_for_environment
from liquidity_migration.strategy.carry_demo import (
    DAY_MS,
    DECISION_KLINE_LAG_MS,
    next_carry_decision_deadline_ts_ms,
)
from liquidity_migration.strategy.long_native_event_demo import (
    LongNativeDemoCycleConfig,
    _next_time_deadline_ts_ms,
)
from liquidity_migration.strategy.long_native_event_demo_daemon import LongNativeDemoDaemon
from liquidity_migration.strategy.strategy_target_replay import PublishedTargetCyclePayload

MS_PER_HOUR = 3_600_000


def _open_trade(
    trade_id: str,
    *,
    max_hold_deadline_ts_ms: int | None = None,
    status: str = "open",
    side: str = "long",
    qty: str = "0.001",
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "trade_id": trade_id,
        "sleeve": "long",
        "symbol": "BTCUSDT",
        "side": side,
        "status": status,
        "qty": qty,
    }
    if max_hold_deadline_ts_ms is not None:
        row["max_hold_deadline_ts_ms"] = max_hold_deadline_ts_ms
    row.update(extra)
    return row


# ----------------------------------------------------------------------
# The LONG cycle's reported deadline.


def test_next_time_deadline_is_the_earliest_future_clock() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame(
        [
            _open_trade("late", max_hold_deadline_ts_ms=now + 48 * MS_PER_HOUR),
            _open_trade("soon", max_hold_deadline_ts_ms=now + 2 * MS_PER_HOUR),
            # Already due: this pass plans its exit, so it owes no future wake.
            _open_trade("due", max_hold_deadline_ts_ms=now - MS_PER_HOUR),
        ]
    )
    assert _next_time_deadline_ts_ms(trades, now_ms=now) == now + 2 * MS_PER_HOUR


def test_next_time_deadline_includes_the_decay_arming_clock() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame(
        [
            _open_trade(
                "decaying",
                max_hold_deadline_ts_ms=now + 72 * MS_PER_HOUR,
                stop_decay_after_ms=48 * MS_PER_HOUR,
                decayed_stop_loss_pct=0.05,
                entry_ts_ms=now - 47 * MS_PER_HOUR,
                entry_price=100.0,
            ),
        ]
    )
    # Decay arms in one hour, well before the max hold: it wins.
    assert _next_time_deadline_ts_ms(trades, now_ms=now) == now + MS_PER_HOUR


def test_next_time_deadline_ignores_trades_a_pass_could_not_act_on() -> None:
    now = 2_000_000_000_000
    trades = pl.DataFrame(
        [
            _open_trade("closed", status="closed", max_hold_deadline_ts_ms=now + MS_PER_HOUR),
            _open_trade("short", side="short", max_hold_deadline_ts_ms=now + MS_PER_HOUR),
            _open_trade("no-qty", qty="0", max_hold_deadline_ts_ms=now + MS_PER_HOUR),
            # Decay contract without an attributed entry fill: clock unstarted.
            _open_trade(
                "unfilled",
                stop_decay_after_ms=MS_PER_HOUR,
                decayed_stop_loss_pct=0.05,
                entry_ts_ms=0,
            ),
            # Fill timestamp without a fill price: the decay exit itself
            # requires the price anchor, so this pass could not act either.
            _open_trade(
                "priceless",
                stop_decay_after_ms=MS_PER_HOUR,
                decayed_stop_loss_pct=0.05,
                entry_ts_ms=now - MS_PER_HOUR,
                entry_price=0.0,
            ),
        ]
    )
    assert _next_time_deadline_ts_ms(trades, now_ms=now) is None
    assert _next_time_deadline_ts_ms(pl.DataFrame(), now_ms=now) is None


# ----------------------------------------------------------------------
# The CARRY daily decision boundary.


def test_carry_deadline_is_todays_boundary_before_it_and_tomorrows_after() -> None:
    day_ts = 2_000 * DAY_MS
    boundary = day_ts + DECISION_KLINE_LAG_MS
    assert next_carry_decision_deadline_ts_ms(day_ts) == boundary
    assert next_carry_decision_deadline_ts_ms(boundary - 1) == boundary
    assert next_carry_decision_deadline_ts_ms(boundary) == boundary + DAY_MS
    assert next_carry_decision_deadline_ts_ms(day_ts + 14 * MS_PER_HOUR) == boundary + DAY_MS


# ----------------------------------------------------------------------
# The daemon's wait, cut short at the deadline.


def _build_daemon(
    tmp_path: Path,
    *,
    interval_seconds: float,
    event_driven_cycle: bool,
    clock: VirtualClock,
    seen: list[dict] | None = None,
) -> LongNativeDemoDaemon:
    def cycle_runner(data_root, **kwargs):
        if seen is not None:
            seen.append(dict(kwargs))
        demo = kwargs["demo_config"]
        route = ensure_account_route(
            account_id=account_id_for_environment(demo.execution_environment),
            environment=demo.execution_environment,
            account_root=demo.account_execution_root,
            inbox_root=demo.account_intent_inbox_root,
        )
        return PublishedTargetCyclePayload(
            {
                "cycle": {"cycle_id": "c1", "mode": "demo_target"},
                "report_dir": str(data_root),
                "next_time_deadline_ts_ms": kwargs.get("_test_next_deadline"),
            },
            publication=ExitFirstPublication((), (), ()),
            route=route,
        )

    return LongNativeDemoDaemon(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=LongNativeDemoCycleConfig(
            execution_environment="demo",
            account_intent_inbox_root=str(tmp_path / "inbox"),
            account_execution_root=str(tmp_path / "account"),
            ws_klines_enabled=False,
        ),
        interval_seconds=interval_seconds,
        cycle_runner=cycle_runner,
        event_driven_cycle=event_driven_cycle,
        min_cycle_interval_seconds=0.0,
        clock=clock,
    )


def test_run_one_cycle_adopts_and_clears_the_reported_deadline(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000_000_000_000)
    daemon = _build_daemon(tmp_path, interval_seconds=0.0, event_driven_cycle=True, clock=clock)

    original_runner = daemon._cycle_runner

    def with_deadline(data_root, **kwargs):
        payload = original_runner(data_root, **kwargs)
        payload["next_time_deadline_ts_ms"] = 2_000_000_123_456
        return payload

    daemon._cycle_runner = with_deadline
    daemon._run_one_cycle()
    assert daemon._next_wake_deadline_ts_ms == 2_000_000_123_456

    def without_deadline(data_root, **kwargs):
        payload = original_runner(data_root, **kwargs)
        payload["next_time_deadline_ts_ms"] = None
        return payload

    daemon._cycle_runner = without_deadline
    clock.advance_ns(1_000_000)
    daemon._run_one_cycle()
    assert daemon._next_wake_deadline_ts_ms is None


def test_event_wait_fires_market_boundary_when_the_deadline_is_due(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000_000_000_000)
    now_ms = 2_000_000_000_000
    daemon = _build_daemon(tmp_path, interval_seconds=30.0, event_driven_cycle=True, clock=clock)
    daemon._next_wake_deadline_ts_ms = now_ms  # due right now

    started = time.monotonic()
    daemon._wait_for_next_cycle_event()
    assert time.monotonic() - started < 5.0, "a due deadline must not sleep the idle ceiling out"
    assert daemon._pending_cycle_kind == "market_boundary"
    assert daemon._cycles_deadline_triggered == 1


def test_a_fired_deadline_never_hot_loops_the_grid(tmp_path: Path) -> None:
    """The exit the deadline asked for can be suppressed (already unresolved);
    the deadline must fire once and hand the retry back to the grid."""

    clock = VirtualClock(current_wall_ns=2_000_000_000_000_000_000)
    now_ms = 2_000_000_000_000
    daemon = _build_daemon(tmp_path, interval_seconds=0.2, event_driven_cycle=True, clock=clock)
    daemon._next_wake_deadline_ts_ms = now_ms

    daemon._wait_for_next_cycle_event()
    assert daemon._pending_cycle_kind == "market_boundary"

    # Same stale deadline, already fired: the wait falls back to the grid.
    daemon._wait_for_next_cycle_event()
    assert daemon._pending_cycle_kind == "timer"
    assert daemon._cycles_deadline_triggered == 1

    # A strictly later deadline is news again once it comes due.
    clock.advance_ns(2_000_000)
    daemon._next_wake_deadline_ts_ms = now_ms + 1
    daemon._wait_for_next_cycle_event()
    assert daemon._pending_cycle_kind == "market_boundary"
    assert daemon._cycles_deadline_triggered == 2


def test_a_due_deadline_outranks_a_pending_confirmed_bar(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000_000_000_000)
    daemon = _build_daemon(tmp_path, interval_seconds=30.0, event_driven_cycle=True, clock=clock)
    daemon._next_wake_deadline_ts_ms = 2_000_000_000_000
    daemon._bar_event.set()

    daemon._wait_for_next_cycle_event()
    # The deadline cycle reads the same fresh data the bar announced, so it
    # goes first at every debounce setting (this daemon runs debounce 0; the
    # host tests pin the same precedence at the production 2.0s). The set bar
    # event still ends the next wait.
    assert daemon._pending_cycle_kind == "market_boundary"
    assert daemon._cycles_deadline_triggered == 1


def test_timer_wait_cuts_short_for_a_deadline_without_a_spurious_overrun(tmp_path: Path) -> None:
    """The timer-grid path (the degraded no-WS mode since carry went
    event-driven), woken early at the decision boundary with the grid
    anchor preserved."""

    clock = VirtualClock(current_wall_ns=2_000_000_000_000_000_000)
    now_ms = 2_000_000_000_000
    daemon = _build_daemon(tmp_path, interval_seconds=0.3, event_driven_cycle=False, clock=clock)
    daemon._next_cycle_at = time.monotonic()
    anchor = daemon._next_cycle_at
    daemon._next_wake_deadline_ts_ms = now_ms  # due right now

    started = time.monotonic()
    daemon._wait_for_next_cycle_timer()
    assert time.monotonic() - started < 0.25, "the deadline must cut the grid sleep short"
    assert daemon._pending_cycle_kind == "market_boundary"
    assert daemon._cycles_deadline_triggered == 1
    assert daemon._next_cycle_at == anchor, "the grid anchor must survive an early wake"

    # The fired deadline is spent: the next wait is a plain grid slot.
    daemon._wait_for_next_cycle_timer()
    assert daemon._pending_cycle_kind == "timer"
    assert daemon._cycle_overruns == 0


def test_a_deadline_cycle_crossing_its_slot_is_not_an_overrun(tmp_path: Path) -> None:
    """A deadline pass late in a slot legitimately spends the remainder; the
    next timer wait must not alarm as if the cycle outran its interval."""

    clock = VirtualClock(current_wall_ns=2_000_000_000_000_000_000)
    daemon = _build_daemon(tmp_path, interval_seconds=0.2, event_driven_cycle=False, clock=clock)
    daemon._next_cycle_at = time.monotonic()
    daemon._next_wake_deadline_ts_ms = 2_000_000_000_000  # due right now

    daemon._wait_for_next_cycle_timer()
    assert daemon._pending_cycle_kind == "market_boundary"
    # The deadline cycle runs long enough to cross the 0.2s slot boundary.
    time.sleep(0.25)
    daemon._wait_for_next_cycle_timer()
    assert daemon._cycle_overruns == 0, "borrowed slot time is not a cycle overrun"

    # A plain cycle crossing its slot still alarms.
    time.sleep(0.25)
    daemon._wait_for_next_cycle_timer()
    assert daemon._cycle_overruns == 1
