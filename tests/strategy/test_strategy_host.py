"""Host-level wake behavior: deadlines outrank the debounce, journal
commits end the wait, and wake labeling stays clean."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from liquidity_migration.core.config import ResearchConfig
from liquidity_migration.strategy.strategy_host import StrategyHostDaemon


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
    # Old behavior labeled the pass confirmed_bar and pushed the due deadline
    # a full cycle later; the deadline cycle reads the same fresh data the
    # bar announced, so it goes first and consumes the wake.
    daemon = _host(tmp_path)
    daemon._next_wake_deadline_ts_ms = _now_ms() - 1
    daemon._bar_event.set()

    daemon._wait_for_next_cycle_event()

    assert daemon._pending_cycle_kind == "market_boundary"
    assert daemon._deadline_fired_ts_ms == daemon._next_wake_deadline_ts_ms


def test_bar_wake_without_journal_flag_labels_confirmed_bar(tmp_path: Path) -> None:
    daemon = _host(tmp_path)
    daemon._bar_event.set()

    daemon._wait_for_next_cycle_event()

    assert daemon._pending_cycle_kind == "confirmed_bar"
    assert daemon._cycles_kline_triggered == 1
    assert daemon._cycles_journal_triggered == 0


def test_journal_flagged_wake_labels_journal_change_and_clears_the_flag(tmp_path: Path) -> None:
    daemon = _host(tmp_path)
    daemon._journal_wake_pending = True
    daemon._bar_event.set()

    daemon._wait_for_next_cycle_event()

    assert daemon._pending_cycle_kind == "journal_change"
    assert daemon._cycles_journal_triggered == 1
    assert daemon._journal_wake_pending is False

    # The consumed flag must not relabel the next plain bar wake.
    daemon._bar_event.clear()
    daemon._bar_event.set()
    daemon._wait_for_next_cycle_event()
    assert daemon._pending_cycle_kind == "confirmed_bar"


def test_journal_commit_rename_ends_the_event_wait(tmp_path: Path) -> None:
    journal_dir = tmp_path / "account" / "journal" / "transactions"
    journal_dir.mkdir(parents=True)
    # Short idle floor: a broken wake fails this test in seconds as kind
    # "timer" instead of hanging out the production 60s floor.
    daemon = _host(tmp_path, journal_change_wake_dir=journal_dir, interval_seconds=6.0)
    daemon._start_journal_watch_thread()
    try:
        # Let the watch adopt the directory (inotify) or take its mtime
        # baseline (poll fallback) before the commit lands.
        time.sleep(0.6)
        tmp = journal_dir / ".segment.tmp"
        tmp.write_bytes(b"{}\n")
        os.replace(tmp, journal_dir / "00000001-00000001-abcd.json")

        started = time.monotonic()
        daemon._wait_for_next_cycle_event()
        elapsed = time.monotonic() - started

        assert daemon._pending_cycle_kind == "journal_change"
        assert daemon._cycles_journal_triggered == 1
        # Idle floor is 60s; the wake must beat it by an order of magnitude.
        assert elapsed < 5.0, f"journal wake took {elapsed:.2f}s"
    finally:
        daemon._stop_journal_watch_thread()


def test_no_journal_watch_thread_without_a_wake_dir_or_in_timer_mode(tmp_path: Path) -> None:
    event_mode = _host(tmp_path)
    event_mode._start_journal_watch_thread()
    assert event_mode._journal_watch_thread is None

    timer_mode = _host(tmp_path, event_driven_cycle=False, journal_change_wake_dir=tmp_path / "j")
    timer_mode._start_journal_watch_thread()
    assert timer_mode._journal_watch_thread is None


def test_journal_watch_select_failure_degrades_to_poll_pace_not_a_hot_loop(
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

    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    daemon = _host(tmp_path, journal_change_wake_dir=journal_dir)
    daemon._start_journal_watch_thread()
    try:
        time.sleep(0.6)
    finally:
        daemon._stop_journal_watch_thread()

    # Rebuild-after-failure must run at the poll cadence (~0.25s), never a
    # full-speed construct/select/close spin.
    assert calls["select"] < 10, f"select ran {calls['select']} times in 0.6s - hot loop"
