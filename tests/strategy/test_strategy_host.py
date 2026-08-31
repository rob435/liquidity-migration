"""Host-level wake behavior: deadlines outrank debounce and engine heartbeats."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.rules.engine_targets import render_target_book, write_target_book
from liquidity_migration.strategy.strategy_event_clock import StrategyEvent
from liquidity_migration.strategy.strategy_host import StrategyHostDaemon
from liquidity_migration.strategy.target_book_evidence import PublishedTargetCyclePayload


@dataclass
class _HostConfig:
    execution_environment: str = "demo"
    notional_multiplier: float = 1.0
    entry_leverage: float = 1.0
    ws_klines_enabled: bool = False


class _Host(StrategyHostDaemon):
    _sleeve_label = "hosttest"
    _flat_cycle_payload = True

    def _strategy_profile_name(self) -> str:
        return "host-test-profile"

    def _format_cycle_summary(self, payload: dict[str, Any]) -> str:
        return "host-test cycle"


def _host(tmp_path: Path, **kwargs: Any) -> _Host:
    kwargs.setdefault("min_cycle_interval_seconds", 0.05)
    return _Host(
        tmp_path / "host",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_HostConfig(),
        cycle_runner=lambda *args, **kw: None,
        **kwargs,
    )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def test_event_wait_fires_a_near_deadline_instead_of_sleeping_the_debounce(tmp_path: Path) -> None:
    # Production debounce is 2.0s; the deadline is 300ms away. The wait must
    # wake for the deadline, not sleep the debounce over it.
    daemon = _host(tmp_path, min_cycle_interval_seconds=2.0)
    daemon._next_wake_deadline_ts_ms = _now_ms() + 300

    started = time.monotonic()
    daemon._wait_for_next_cycle_event()
    elapsed = time.monotonic() - started

    assert daemon._pending_cycle_kind == "market_boundary"
    assert elapsed < 1.2, f"deadline wake took {elapsed:.2f}s; the debounce delayed it"


def test_a_due_deadline_outranks_a_pending_bar_wake(tmp_path: Path) -> None:
    # The deadline cycle reads the same fresh data the bar announced, so it
    # goes first and consumes the wake.
    daemon = _host(tmp_path)
    daemon._next_wake_deadline_ts_ms = _now_ms() - 1
    daemon._bar_event.set()

    daemon._wait_for_next_cycle_event()

    assert daemon._pending_cycle_kind == "market_boundary"
    assert daemon._deadline_fired_ts_ms == daemon._next_wake_deadline_ts_ms


def test_bar_wake_without_engine_flag_labels_confirmed_bar(tmp_path: Path) -> None:
    daemon = _host(tmp_path)
    daemon._bar_event.set()

    daemon._wait_for_next_cycle_event()

    assert daemon._pending_cycle_kind == "confirmed_bar"
    assert daemon._cycles_kline_triggered == 1
    assert daemon._cycles_engine_triggered == 0


def test_engine_flagged_wake_labels_engine_change_and_clears_the_flag(tmp_path: Path) -> None:
    daemon = _host(tmp_path)
    daemon._engine_wake_pending = True
    daemon._bar_event.set()

    daemon._wait_for_next_cycle_event()

    assert daemon._pending_cycle_kind == "engine_change"
    assert daemon._cycles_engine_triggered == 1
    assert daemon._engine_wake_pending is False

    # The consumed flag must not relabel the next plain bar wake.
    daemon._bar_event.clear()
    daemon._bar_event.set()
    daemon._wait_for_next_cycle_event()
    assert daemon._pending_cycle_kind == "confirmed_bar"


def test_engine_heartbeat_rename_ends_the_event_wait(tmp_path: Path) -> None:
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir(parents=True)
    # Short idle floor: a broken wake fails this test in seconds as kind
    # "timer" instead of hanging out the production 60s floor.
    daemon = _host(tmp_path, engine_change_wake_dir=engine_dir, interval_seconds=6.0)
    daemon._start_engine_watch_thread()
    try:
        # The starter does not return until inotify is armed or the polling
        # fallback has taken its baseline.  Publishing immediately exercises
        # that readiness contract instead of relying on scheduler timing.
        assert daemon._engine_watch_ready.is_set()
        heartbeat = daemon._engine_heartbeat_file
        assert heartbeat is not None
        assert heartbeat.parent == engine_dir
        tmp = heartbeat.with_name(f".{heartbeat.name}.tmp")
        tmp.write_bytes(b"{}\n")
        os.replace(tmp, heartbeat)

        started = time.monotonic()
        daemon._wait_for_next_cycle_event()
        elapsed = time.monotonic() - started

        assert daemon._pending_cycle_kind == "engine_change"
        assert daemon._cycles_engine_triggered == 1
        # Idle floor is 60s; the wake must beat it by an order of magnitude.
        assert elapsed < 5.0, f"engine wake took {elapsed:.2f}s"
    finally:
        daemon._stop_engine_watch_thread()


def test_no_engine_watch_thread_without_a_wake_dir_or_in_timer_mode(tmp_path: Path) -> None:
    event_mode = _host(tmp_path)
    event_mode._start_engine_watch_thread()
    assert event_mode._engine_watch_thread is None

    timer_mode = _host(tmp_path, event_driven_cycle=False, engine_change_wake_dir=tmp_path / "e")
    timer_mode._start_engine_watch_thread()
    assert timer_mode._engine_watch_thread is None


def test_engine_watch_select_failure_degrades_to_poll_pace_not_a_hot_loop(
    tmp_path: Path, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    import liquidity_migration.strategy.strategy_host as host_module

    class _FakeWatch:
        def __init__(self, directory: Path) -> None:
            self.fd = 999

        def drain(self) -> bool:
            return False

        def close(self) -> None:
            pass

    calls = {"select": 0}

    class _FailingSelect:
        @staticmethod
        def select(*args: Any, **kwargs: Any) -> Any:
            calls["select"] += 1
            raise ValueError("fd out of range in select()")

    monkeypatch.setattr(host_module, "DirectoryRenameWatch", _FakeWatch)
    monkeypatch.setattr(host_module, "select", _FailingSelect)

    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    daemon = _host(tmp_path, engine_change_wake_dir=engine_dir)
    daemon._start_engine_watch_thread()
    try:
        time.sleep(0.6)
    finally:
        daemon._stop_engine_watch_thread()

    # Rebuild-after-failure must run at the poll cadence (~0.25s), never a
    # full-speed construct/select/close spin.
    assert calls["select"] < 10, f"select ran {calls['select']} times in 0.6s - hot loop"


def test_a_wake_landing_at_the_deadline_instant_folds_into_the_boundary(tmp_path: Path) -> None:
    from liquidity_migration.core.deterministic_runtime import VirtualClock

    clock = VirtualClock(current_wall_ns=2_000_000_000_000_000_000)
    daemon = _host(tmp_path, min_cycle_interval_seconds=0.0, clock=clock)
    deadline_ms = 2_000_000_000_000 + 100
    daemon._next_wake_deadline_ts_ms = deadline_ms

    real_wait = daemon._bar_event.wait

    def wake_as_the_deadline_passes(timeout: float | None = None) -> bool:
        # The engine heartbeat and deadline instant coincide: by the time
        # the wait returns for the wake, the deadline is already due.
        clock.advance_ns(200 * 1_000_000)
        daemon._engine_wake_pending = True
        daemon._bar_event.set()
        return real_wait(0)

    daemon._bar_event.wait = wake_as_the_deadline_passes  # type: ignore[method-assign]
    daemon._wait_for_next_cycle_event()

    # The boundary must not queue behind a full engine-change cycle; the
    # pending wake is consumed by the boundary cycle itself.
    assert daemon._pending_cycle_kind == "market_boundary"
    assert daemon._deadline_fired_ts_ms == deadline_ms


# ----------------------------------------------------------------------
# Price-touch wakes. A cycle reports the prices at which a live tick could
# change its book; the ticker callback ends the wait when a push crosses one.


def _ticker_push(symbol: str, price: float) -> dict[str, Any]:
    """One public ticker WS frame in Bybit's envelope."""

    return {"topic": f"tickers.{symbol}", "data": {"symbol": symbol, "markPrice": str(price)}}


def _price_wake_host(tmp_path: Path, **kwargs: Any) -> tuple[_Host, Any]:
    """A host whose cycle reports whatever price levels the test sets.

    Driven through the real ``_run_one_cycle`` adoption path rather than by
    poking the registry, so the test exercises what production runs.
    """

    book = tmp_path / "price-wake-targets.json"
    write_target_book(
        book,
        render_target_book(
            source="hosttest",
            decision_ts_ms=1,
            valid_until_ms=2,
            targets=[],
        ),
    )
    reported: dict[str, Any] = {"levels": []}

    def runner(*_args: Any, **_kwargs: Any) -> PublishedTargetCyclePayload:
        return PublishedTargetCyclePayload(
            {
                "cycle_id": "price-wake-1",
                "ts_ms": 1,
                "price_wake_levels": list(reported["levels"]),
            },
            target_book_path=book,
        )

    kwargs.setdefault("min_cycle_interval_seconds", 0.05)
    daemon = _Host(
        tmp_path / "host",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_HostConfig(),
        cycle_runner=runner,
        **kwargs,
    )
    return daemon, reported


def test_a_touched_price_level_ends_the_wait_instead_of_the_idle_floor(tmp_path: Path) -> None:
    # Fails without the fix: the ticker callback only fed the cache, so the
    # predicate was evaluated by whatever cycle happened to run next — up to
    # the whole idle floor later.
    daemon, reported = _price_wake_host(tmp_path, interval_seconds=30.0)
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_below": 100.0}]
    daemon._run_one_cycle()
    daemon._bar_event.clear()

    # Above the level: nothing to look at.
    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 100.5))
    assert daemon._bar_event.is_set() is False

    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 99.5))
    assert daemon._bar_event.is_set() is True

    started = time.monotonic()
    daemon._wait_for_next_cycle_event()
    elapsed = time.monotonic() - started

    assert daemon._pending_cycle_kind == "price_touch"
    assert elapsed < 5.0, f"price wake took {elapsed:.2f}s against a 30s idle floor"


def test_price_touch_rebinds_the_fired_map_instead_of_mutating_it(tmp_path: Path) -> None:
    """The cycle thread iterates `_price_wake_fired` without a lock, so the WS
    thread must rebind a fresh dict on insert: an in-place insert can kill a
    cycle pass mid-comprehension ("dictionary changed size during iteration").
    """

    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_below": 100.0}]
    daemon._run_one_cycle()
    daemon._bar_event.clear()

    before = daemon._price_wake_fired
    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 99.5))
    assert daemon._bar_event.is_set() is True
    assert daemon._price_wake_fired is not before
    # The object a mid-iteration reader may still hold is untouched.
    assert before == {}
    assert daemon._price_wake_fired == {"AAAUSDT": (100.0, None)}


def test_an_unwatched_symbol_never_wakes_the_loop(tmp_path: Path) -> None:
    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_below": 100.0}]
    daemon._run_one_cycle()
    daemon._bar_event.clear()

    # Same price, different symbol: the cheap per-symbol lookup misses.
    daemon._handle_ticker_message(_ticker_push("BBBUSDT", 1.0))

    assert daemon._bar_event.is_set() is False


def test_distinct_crossed_symbols_are_not_globally_debounced(tmp_path: Path) -> None:
    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [
        {"symbol": "AAAUSDT", "at_or_below": 100.0},
        {"symbol": "BBBUSDT", "at_or_below": 50.0},
    ]
    daemon._run_one_cycle()
    daemon._bar_event.clear()

    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 99.0))
    assert daemon._bar_event.is_set() is True
    daemon._bar_event.clear()

    daemon._handle_ticker_message(_ticker_push("BBBUSDT", 49.0))
    assert daemon._bar_event.is_set() is True
    assert set(daemon._price_wake_fired) == {"AAAUSDT", "BBBUSDT"}


def test_a_churning_tick_stream_cannot_spin_cycles(tmp_path: Path) -> None:
    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_below": 100.0}]
    daemon._run_one_cycle()
    daemon._bar_event.clear()

    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 99.0))
    assert daemon._bar_event.is_set() is True

    # A cycle consumes the wake; the next hundred ticks are all still below
    # the same registered level and must not re-arm it.
    daemon._bar_event.clear()
    for tick in range(100):
        daemon._handle_ticker_message(_ticker_push("AAAUSDT", 99.0 - tick * 0.001))
    assert daemon._bar_event.is_set() is False

    # The same registration stays silent until a cycle changes the level.
    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 98.0))
    assert daemon._bar_event.is_set() is False


def test_a_level_that_cannot_clear_wakes_once_until_it_is_rearmed(tmp_path: Path) -> None:
    """A breached-but-unexitable stop must not spin the sleeve at 0.5 Hz.

    The wake fires once per registration. A cycle that re-arms the very same
    level (its exit still unresolved) keeps the latch; a cycle that arms a
    different level -- the stop decayed further, or a new trade -- re-arms
    the wake.
    """

    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_below": 95.0}]
    daemon._run_one_cycle()
    daemon._bar_event.clear()

    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 94.0))
    assert daemon._bar_event.is_set() is True

    # The cycle that wake started re-arms the SAME level: still latched.
    daemon._bar_event.clear()
    daemon._run_one_cycle()
    daemon._bar_event.clear()
    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 93.0))
    assert daemon._bar_event.is_set() is False

    # A DIFFERENT level for the symbol re-arms the wake.
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_below": 92.0}]
    daemon._run_one_cycle()
    daemon._bar_event.clear()
    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 91.0))
    assert daemon._bar_event.is_set() is True


def test_each_cycle_replaces_the_previous_cycle_s_levels(tmp_path: Path) -> None:
    # Fails without the fix in the second half: stale levels must not pile up
    # across cycles, and the new cycle's level must actually be armed.
    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_below": 100.0}]
    daemon._run_one_cycle()

    reported["levels"] = [{"symbol": "BBBUSDT", "at_or_below": 50.0}]
    daemon._run_one_cycle()
    daemon._bar_event.clear()

    # The retired level is gone, not merely outvoted.
    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 1.0))
    assert daemon._bar_event.is_set() is False

    daemon._handle_ticker_message(_ticker_push("BBBUSDT", 49.0))
    assert daemon._bar_event.is_set() is True


def test_only_the_first_level_a_falling_price_reaches_is_kept(tmp_path: Path) -> None:
    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [
        {"symbol": "AAAUSDT", "at_or_below": 90.0},
        {"symbol": "AAAUSDT", "at_or_below": 100.0},
        {"symbol": "AAAUSDT", "at_or_below": 80.0},
    ]
    daemon._run_one_cycle()

    assert daemon._price_wake_floor_by_symbol == {"AAAUSDT": 100.0}


def test_a_rising_price_level_wakes_on_the_way_up(tmp_path: Path) -> None:
    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_above": 100.0}]
    daemon._run_one_cycle()
    daemon._bar_event.clear()

    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 99.5))
    assert daemon._bar_event.is_set() is False

    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 100.0))
    assert daemon._bar_event.is_set() is True


def test_an_engine_change_outranks_a_price_touch(tmp_path: Path) -> None:
    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_below": 100.0}]
    daemon._run_one_cycle()
    daemon._bar_event.clear()

    daemon._handle_ticker_message(_ticker_push("AAAUSDT", 99.0))
    daemon._engine_wake_pending = True

    daemon._wait_for_next_cycle_event()

    # The commit means the book itself moved; its cycle reads the touched
    # price anyway.
    assert daemon._pending_cycle_kind == "engine_change"
    # The consumed price flag must not relabel the next plain bar wake.
    daemon._bar_event.clear()
    daemon._bar_event.set()
    daemon._wait_for_next_cycle_event()
    assert daemon._pending_cycle_kind == "confirmed_bar"


def test_the_timer_grid_pays_no_per_tick_price_work(tmp_path: Path) -> None:
    # The timer grid ignores the wake event, so watching prices for it would
    # be pure per-tick cost on the WS thread.
    daemon, reported = _price_wake_host(tmp_path, event_driven_cycle=False)
    reported["levels"] = [{"symbol": "AAAUSDT", "at_or_below": 100.0}]
    daemon._run_one_cycle()

    assert daemon._price_wake_floor_by_symbol == {}


def test_a_broken_level_row_never_costs_the_cache_its_update(tmp_path: Path) -> None:
    daemon, reported = _price_wake_host(tmp_path)
    reported["levels"] = [
        {"symbol": "", "at_or_below": 100.0},
        {"symbol": "AAAUSDT", "at_or_below": "cheap"},
        {"symbol": "AAAUSDT", "at_or_below": 0.0},
        {"symbol": "AAAUSDT", "at_or_below": True},
        "not a row",
        {"symbol": "BBBUSDT", "at_or_below": 7},
    ]
    daemon._run_one_cycle()

    # Only the usable one survives; an int level is a price.
    assert daemon._price_wake_floor_by_symbol == {"BBBUSDT": 7.0}

    daemon._handle_ticker_message(_ticker_push("BBBUSDT", 6.0))
    assert daemon._bar_event.is_set() is True
    assert daemon._ticker_cache.get("BBBUSDT") is not None


def test_a_previously_live_ticker_stream_is_rebuilt_after_it_goes_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _host(tmp_path, state_cache_stale_seconds=60.0)

    class _StaleCache:
        @staticmethod
        def is_seeded() -> bool:
            return True

        @staticmethod
        def seconds_since_last_ws_event() -> float:
            return 120.0

    calls: list[str] = []
    daemon._ticker_cache = _StaleCache()  # type: ignore[assignment]
    daemon._ticker_stream = object()
    daemon._ticker_stream_installed_monotonic = time.monotonic() - 120.0
    monkeypatch.setattr(daemon, "_close_ticker_stream", lambda: calls.append("close"))
    monkeypatch.setattr(daemon, "_open_ticker_stream", lambda: calls.append("open"))

    daemon._check_ws_health()

    assert calls == ["close", "open"]
    assert daemon._ws_ticker_stale_ticks == 1


def test_cycle_payload_is_not_decorated_with_ws_plane_stats(tmp_path: Path) -> None:
    """The post-cycle ws_klines/ws_state attach was computed-and-dropped: every
    sleeve persists its cycle row inside its own runner, before the host sees
    the payload, and no formatter, capture, or watchdog column read the keys.
    The host must neither attach them nor pay the manager stats() call."""

    class _FakeKlineManager:
        def __init__(self) -> None:
            self.stats_calls = 0

        def store(self) -> None:
            return None

        def stats(self) -> dict[str, Any]:
            self.stats_calls += 1
            return {"store": {"rows": 1}}

        def start(self, **_kwargs: Any) -> None:
            return None

        def stop(self) -> None:
            return None

    book = tmp_path / "host-attach-targets.json"
    write_target_book(
        book,
        render_target_book(
            source="hosttest",
            decision_ts_ms=1,
            valid_until_ms=2,
            targets=[],
        ),
    )

    def runner(*_args: Any, **_kwargs: Any) -> PublishedTargetCyclePayload:
        return PublishedTargetCyclePayload(
            {"cycle_id": "host-attach-1", "ts_ms": 1},
            target_book_path=book,
        )

    manager = _FakeKlineManager()
    daemon = _Host(
        tmp_path / "host",
        config=ResearchConfig(data_root=tmp_path),
        demo_config=_HostConfig(),
        cycle_runner=runner,
        kline_stream_manager=manager,
        min_cycle_interval_seconds=0.05,
    )
    event = StrategyEvent(
        event_ts_ns=1_000_000_000,
        ingest_ts_ns=1_000_000_000,
        source="hosttest:demo",
        source_sequence=1,
        kind="startup",
        payload={"execution_environment": "demo", "strategy_profile": "host-test-profile"},
    )

    payload = daemon._execute_cycle_event(event)

    assert payload is not None
    assert "ws_klines" not in payload
    assert "ws_state" not in payload
    assert manager.stats_calls == 0
