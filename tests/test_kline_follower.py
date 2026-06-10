"""Tests for the read-only kline follower (shared WS data plane across sleeves).

The leader (continuous DEMO daemon) flushes its KlineStore to an atomic
store.parquet snapshot; the follower (paper shadow) stat-polls that file and
re-runs the idempotent recover merge. These tests pin the contract: snapshot
recovery + parity, refresh-on-change only, wake-event firing on a new confirmed
bar, strict read-only behaviour, graceful degradation while the snapshot is
missing, and the daemon-side factory selection."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import polars as pl

from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.cli import build_parser
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig, _signal_source_root
from liquidity_migration.continuous_demo_daemon import (
    _default_continuous_kline_stream_manager_factory,
    _follower_continuous_kline_stream_manager_factory,
    _select_kline_stream_manager_factory,
)
from liquidity_migration.kline_follower import FollowerKlineStreamManager
from liquidity_migration.kline_store import KlineStore


def _ws_bar(ts_ms: int, *, close: float = 100.0) -> dict:
    return {
        "start": str(ts_ms),
        "open": str(close - 1.0),
        "high": str(close + 1.0),
        "low": str(close - 2.0),
        "close": str(close),
        "volume": "1000",
        "turnover": str(1000.0 * close),
    }


def _hour_floor_now_ms() -> int:
    return (int(time.time() * 1000) // MS_PER_HOUR) * MS_PER_HOUR


def _leader_with_bars(root: Path, *, symbols: tuple[str, ...] = ("AAAUSDT", "BBBUSDT"), hours: int = 4) -> KlineStore:
    leader = KlineStore(cache_root=root, flush_interval_seconds=0.0)
    base = _hour_floor_now_ms() - hours * MS_PER_HOUR
    for symbol in symbols:
        for i in range(hours):
            assert leader.add_bar(symbol, _ws_bar(base + i * MS_PER_HOUR, close=100.0 + i), confirmed=True)
    assert leader.flush_to_disk() > 0
    return leader


def test_follower_recovers_leader_snapshot_and_serves_identical_klines(tmp_path: Path) -> None:
    leader = _leader_with_bars(tmp_path)
    follower = FollowerKlineStreamManager(leader_root=tmp_path, poll_seconds=3600.0)
    try:
        start_stats = follower.start()
        assert start_stats["mode"] == "follower"
        assert start_stats["snapshot_present"] is True
        assert follower.is_started()
        assert follower.store().row_count() == leader.row_count()
        now = _hour_floor_now_ms()
        window = dict(start_ms=now - 10 * MS_PER_HOUR, end_ms=now + MS_PER_HOUR)
        got = follower.store().get_klines(["AAAUSDT", "BBBUSDT"], **window)
        want = leader.get_klines(["AAAUSDT", "BBBUSDT"], **window)
        assert got.sort(["symbol", "ts_ms"]).equals(want.sort(["symbol", "ts_ms"]))
    finally:
        follower.stop()


def test_follower_refreshes_only_on_snapshot_change_and_fires_wake_event(tmp_path: Path) -> None:
    leader = _leader_with_bars(tmp_path)
    follower = FollowerKlineStreamManager(leader_root=tmp_path, poll_seconds=3600.0)
    try:
        follower.start()
        refreshes_after_start = follower.stats()["refreshes"]
        # unchanged snapshot -> no re-read
        assert follower._refresh() is False
        assert follower.stats()["refreshes"] == refreshes_after_start

        wake = threading.Event()
        follower.set_cycle_wake_event(wake)
        new_bar_ts = _hour_floor_now_ms()
        assert leader.add_bar("AAAUSDT", _ws_bar(new_bar_ts, close=200.0), confirmed=True)
        assert leader.flush_to_disk() > 0
        assert follower._refresh() is True
        assert wake.is_set()
        frame = follower.store().get_klines(
            ["AAAUSDT"], start_ms=new_bar_ts, end_ms=new_bar_ts + MS_PER_HOUR,
        )
        assert frame.height == 1
        assert frame["close"][0] == 200.0
    finally:
        follower.stop()


def test_follower_is_strictly_read_only(tmp_path: Path) -> None:
    _leader_with_bars(tmp_path)
    snapshot = tmp_path / ".cache" / "ws_klines" / "store.parquet"
    sig_before = (snapshot.stat().st_mtime_ns, snapshot.stat().st_size)
    follower = FollowerKlineStreamManager(leader_root=tmp_path, poll_seconds=3600.0)
    try:
        follower.start()
        follower._refresh()
        # never starts the store's flush thread; never rewrites the leader snapshot
        assert follower.store()._flush_thread is None
        assert (snapshot.stat().st_mtime_ns, snapshot.stat().st_size) == sig_before
    finally:
        follower.stop()


def test_follower_degrades_gracefully_while_snapshot_missing(tmp_path: Path) -> None:
    follower = FollowerKlineStreamManager(leader_root=tmp_path, poll_seconds=3600.0)
    try:
        start_stats = follower.start()
        assert start_stats["snapshot_present"] is False
        assert follower.store().row_count() == 0
        assert follower.universe_symbols() == []
        # leader appears later -> next poll picks it up
        _leader_with_bars(tmp_path)
        assert follower._refresh() is True
        assert follower.store().row_count() > 0
        assert follower.universe_symbols() == ["AAAUSDT", "BBBUSDT"]
    finally:
        follower.stop()


def test_follower_prunes_symbols_the_leader_trimmed(tmp_path: Path) -> None:
    """Leader universe trims (restart/refresh) -> the departed symbol must leave the
    follower too: recovery only ADDS, so without the prune the stale symbol would
    pollute universe_symbols() and memory until its bars age out."""
    leader = _leader_with_bars(tmp_path, symbols=("AAAUSDT", "BBBUSDT", "OLDUSDT"))
    follower = FollowerKlineStreamManager(leader_root=tmp_path, poll_seconds=3600.0)
    try:
        follower.start()
        assert "OLDUSDT" in follower.universe_symbols()
        leader.keep_only_symbols(["AAAUSDT", "BBBUSDT"])
        new_bar_ts = _hour_floor_now_ms()
        assert leader.add_bar("AAAUSDT", _ws_bar(new_bar_ts, close=150.0), confirmed=True)
        assert leader.flush_to_disk() > 0
        assert follower._refresh() is True
        assert follower.universe_symbols() == ["AAAUSDT", "BBBUSDT"]
        assert not follower.store().has_symbol("OLDUSDT")
        assert follower.stats()["last_pruned_rows"] > 0
    finally:
        follower.stop()


def test_follower_warns_once_when_leader_snapshot_goes_stale(tmp_path: Path, caplog) -> None:
    """Leader daemon down -> snapshot freezes -> the follower keeps serving (REST
    fallback covers correctness) but must surface the degradation exactly once
    per episode, and expose snapshot_age_seconds for telemetry."""
    import logging
    import os

    _leader_with_bars(tmp_path)
    snapshot = tmp_path / ".cache" / "ws_klines" / "store.parquet"
    follower = FollowerKlineStreamManager(
        leader_root=tmp_path, poll_seconds=3600.0, stale_warning_seconds=60.0,
    )
    try:
        follower.start()
        assert follower.stats()["snapshot_age_seconds"] < 60.0
        # age the snapshot past the threshold
        old = time.time() - 3600.0
        os.utime(snapshot, (old, old))
        follower._last_sig = None  # re-stat picks up the aged mtime
        with caplog.at_level(logging.WARNING, logger="liquidity_migration.kline_follower"):
            follower._refresh()
            follower._refresh()  # second pass must NOT re-warn (once per episode)
        stale_warnings = [r for r in caplog.records if "has not changed" in r.message]
        assert len(stale_warnings) == 1
        assert follower.stats()["snapshot_age_seconds"] > 60.0
        assert follower.stats()["poll_thread_alive"] is True
    finally:
        follower.stop()


def test_follower_factory_refuses_circular_self_follow(tmp_path: Path) -> None:
    import pytest

    cfg = ContinuousDemoCycleConfig(klines_follow_root=str(tmp_path))
    with pytest.raises(ValueError, match="self-follow|own data root"):
        _follower_continuous_kline_stream_manager_factory(None, cfg, tmp_path)


def test_run_script_refuses_circular_self_follow() -> None:
    repo = Path(__file__).resolve().parents[1]
    script = (repo / "scripts" / "run_bybit_continuous_demo_event_engine.sh").read_text(
        encoding="utf-8"
    )
    assert 'if [[ "$KLINES_FOLLOW_ROOT" == "$DATA_ROOT" ]]' in script
    assert "circular self-follow" in script


def test_daemon_factory_selection_prefers_explicit_then_follow_root() -> None:
    follow_cfg = ContinuousDemoCycleConfig(klines_follow_root="data/leader-root")
    own_cfg = ContinuousDemoCycleConfig()

    def explicit(*_args: object) -> None:  # sentinel injected factory (tests)
        return None

    assert _select_kline_stream_manager_factory(follow_cfg, explicit) is explicit
    assert (
        _select_kline_stream_manager_factory(follow_cfg, None)
        is _follower_continuous_kline_stream_manager_factory
    )
    assert (
        _select_kline_stream_manager_factory(own_cfg, None)
        is _default_continuous_kline_stream_manager_factory
    )


def test_follower_factory_builds_manager_on_the_leader_root(tmp_path: Path) -> None:
    cfg = ContinuousDemoCycleConfig(klines_follow_root=str(tmp_path))
    manager = _follower_continuous_kline_stream_manager_factory(None, cfg, tmp_path / "own-root")
    assert isinstance(manager, FollowerKlineStreamManager)
    assert manager.stats()["leader_root"] == str(tmp_path)


def test_signal_source_root_follows_the_leader_for_the_rmom_gate(tmp_path: Path) -> None:
    own = tmp_path / "own"
    leader = tmp_path / "leader"
    assert _signal_source_root(ContinuousDemoCycleConfig(), own) == own
    assert (
        _signal_source_root(ContinuousDemoCycleConfig(klines_follow_root=str(leader)), own)
        == leader
    )


def test_cli_klines_follow_root_parses_into_config(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--klines-follow-root",
            "data/bybit-continuous-demo-event",
        ]
    )
    assert args.klines_follow_root == "data/bybit-continuous-demo-event"
    # default stays "own pool"
    args_default = build_parser().parse_args(
        ["--data-root", str(tmp_path), "continuous-event-demo-cycle"]
    )
    assert args_default.klines_follow_root == ""


def test_rmom_parquet_is_read_from_the_followed_root(tmp_path: Path) -> None:
    """End-to-end pin of the one signal-input redirect: with klines_follow_root set,
    _load_rmom_table(_signal_source_root(...)) resolves the LEADER's gate parquet."""
    from liquidity_migration.continuous_demo import _load_rmom_table

    leader = tmp_path / "leader"
    own = tmp_path / "own"
    leader.mkdir()
    own.mkdir()
    pl.DataFrame(
        {"ts_ms": [0], "symbol": ["AAAUSDT"], "residual_momentum": [0.1]}
    ).write_parquet(leader / "residual_momentum.parquet")

    demo = ContinuousDemoCycleConfig(klines_follow_root=str(leader))
    table = _load_rmom_table(_signal_source_root(demo, own))
    assert table is not None
    assert table["symbol"].to_list() == ["AAAUSDT"]
    # without the follow root the sleeve's own (empty) root yields no gate
    assert _load_rmom_table(_signal_source_root(ContinuousDemoCycleConfig(), own)) is None
