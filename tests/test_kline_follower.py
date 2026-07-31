"""Tests for the read-only kline follower (shared WS data planes across sleeves).

The leader flushes its KlineStore to an atomic store.parquet snapshot; the follower
stat-polls that file and re-runs the idempotent recover merge. Pinned: snapshot recovery
and parity, refresh-on-change only, wake-event firing on a new confirmed bar, read-only
behaviour, degradation while the snapshot is missing, and daemon-side factory selection.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import polars as pl

from liquidity_migration.core._common import MS_PER_HOUR
from liquidity_migration.cli.commands import build_parser
from liquidity_migration.strategy.continuous_demo import ContinuousDemoCycleConfig, _signal_source_root
from liquidity_migration.strategy.continuous_demo_daemon import (
    _default_continuous_kline_stream_manager_factory,
    _follower_continuous_kline_stream_manager_factory,
    _select_kline_stream_manager_factory,
)
from liquidity_migration.marketdata.kline_follower import FollowerKlineStreamManager
from liquidity_migration.marketdata.kline_store import KlineStore
from liquidity_migration.strategy.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.strategy.long_native_event_demo_daemon import (
    _default_long_kline_stream_manager_factory,
    _follower_long_kline_stream_manager_factory,
    _select_long_kline_stream_manager_factory,
)


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
    """Recovery only ADDS, so a symbol the leader trimmed must be pruned explicitly or
    it pollutes ``universe_symbols()`` and memory until its bars age out.
    """
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
    """A frozen snapshot (leader down) keeps serving via the REST fallback, but must
    surface the degradation exactly once per episode and expose
    ``snapshot_age_seconds``.
    """
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
        with caplog.at_level(logging.WARNING, logger="liquidity_migration.marketdata.kline_follower"):
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
    for name in (
        "run_bybit_continuous_demo_event_engine.sh",
        "run_bybit_long_demo_event_engine.sh",
    ):
        script = (repo / "scripts" / "runtime" / name).read_text(encoding="utf-8")
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

    long_follow = LongNativeDemoCycleConfig(klines_follow_root="data/leader-root")
    long_own = LongNativeDemoCycleConfig()
    assert _select_long_kline_stream_manager_factory(long_follow, explicit) is explicit
    assert (
        _select_long_kline_stream_manager_factory(long_follow, None)
        is _follower_long_kline_stream_manager_factory
    )
    assert (
        _select_long_kline_stream_manager_factory(long_own, None)
        is _default_long_kline_stream_manager_factory
    )


def test_follower_factory_builds_manager_on_the_leader_root(tmp_path: Path) -> None:
    cfg = ContinuousDemoCycleConfig(klines_follow_root=str(tmp_path))
    manager = _follower_continuous_kline_stream_manager_factory(None, cfg, tmp_path / "own-root")
    assert isinstance(manager, FollowerKlineStreamManager)
    assert manager.stats()["leader_root"] == str(tmp_path)

    long_cfg = LongNativeDemoCycleConfig(klines_follow_root=str(tmp_path))
    long_manager = _follower_long_kline_stream_manager_factory(
        None,
        long_cfg,
        tmp_path / "long-own-root",
    )
    assert isinstance(long_manager, FollowerKlineStreamManager)
    assert long_manager.stats()["leader_root"] == str(tmp_path)


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
            "--execution-environment",
            "demo",
            "--klines-follow-root",
            "data/bybit-continuous-demo-event",
        ]
    )
    assert args.klines_follow_root == "data/bybit-continuous-demo-event"
    # default stays "own pool"
    args_default = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "continuous-event-demo-cycle",
            "--execution-environment",
            "demo",
        ]
    )
    assert args_default.klines_follow_root == ""

    long_args = build_parser().parse_args(
        [
            "--data-root",
            str(tmp_path),
            "long-native-event-demo-cycle",
            "--execution-environment",
            "paper",
            "--klines-follow-root",
            "data/bybit-long-demo-event",
        ]
    )
    assert long_args.klines_follow_root == "data/bybit-long-demo-event"


def test_rmom_parquet_is_read_from_the_followed_root(tmp_path: Path) -> None:
    """End-to-end pin of the one signal-input redirect: with klines_follow_root set,
    _load_rmom_table(_signal_source_root(...)) resolves the LEADER's gate parquet."""
    from liquidity_migration.strategy.continuous_demo import _load_rmom_table

    leader = tmp_path / "leader"
    own = tmp_path / "own"
    leader.mkdir()
    own.mkdir()
    pl.DataFrame(
        {"ts_ms": [0], "symbol": ["AAAUSDT"], "residual_momentum": [0.1]}
    ).with_columns(pl.lit(False).alias("is_provisional")).write_parquet(
        leader / "residual_momentum.parquet"
    )

    demo = ContinuousDemoCycleConfig(klines_follow_root=str(leader))
    table = _load_rmom_table(_signal_source_root(demo, own))
    assert table is not None
    assert table["symbol"].to_list() == ["AAAUSDT"]
    # without the follow root the sleeve's own (empty) root yields no gate
    assert _load_rmom_table(_signal_source_root(ContinuousDemoCycleConfig(), own)) is None


# --------------------------------------------------------------------------
# Follower _last_sig matches the generation actually merged
# --------------------------------------------------------------------------


def test_follower_refresh_records_post_read_signature(tmp_path: Path) -> None:
    """If the leader flushes a newer generation between the follower's stat and
    ``recover_from_disk``'s read, the follower records the signature of the
    generation it actually merged, so the age does not lag a generation.
    """
    base = _hour_floor_now_ms() - 4 * MS_PER_HOUR

    leader = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    for i in range(4):
        leader.add_bar("AAAUSDT", _ws_bar(base + i * MS_PER_HOUR, close=100.0 + i), confirmed=True)
    assert leader.flush_to_disk() > 0

    follower = FollowerKlineStreamManager(leader_root=tmp_path, poll_seconds=3600.0)

    snapshot_path = tmp_path / ".cache" / "ws_klines" / "store.parquet"
    real_recover = follower._store.recover_from_disk

    def recover_then_leader_flushes_again() -> int:
        # Simulate the leader flushing a NEWER generation DURING our read: the
        # merge below sees whichever generation, but the on-disk file ends newer.
        rows = real_recover()
        time.sleep(0.01)  # ensure a distinct mtime_ns
        leader.add_bar("AAAUSDT", _ws_bar(base + 4 * MS_PER_HOUR, close=200.0), confirmed=True)
        leader.flush_to_disk()
        return rows

    follower._store.recover_from_disk = recover_then_leader_flushes_again  # type: ignore[assignment]

    follower._refresh()

    # _last_sig must equal the CURRENT on-disk signature (the post-read
    # generation), not a stale earlier one.
    current_sig = (snapshot_path.stat().st_mtime_ns, snapshot_path.stat().st_size)
    assert follower._last_sig == current_sig

    # And a follow-up refresh with no further leader writes is a no-op (the
    # recorded signature already matches the latest file -> no redundant re-read).
    follower._store.recover_from_disk = real_recover  # type: ignore[assignment]
    refreshes_before = follower._refreshes
    follower._refresh()
    assert follower._refreshes == refreshes_before


def test_follower_refresh_no_change_is_noop(tmp_path: Path) -> None:
    """An unchanged snapshot is a clean no-op: the re-stat-after-read must not perturb the steady-state path."""
    base = _hour_floor_now_ms() - 2 * MS_PER_HOUR
    leader = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    for i in range(2):
        leader.add_bar("AAAUSDT", _ws_bar(base + i * MS_PER_HOUR, close=10.0 + i), confirmed=True)
    assert leader.flush_to_disk() > 0

    follower = FollowerKlineStreamManager(leader_root=tmp_path, poll_seconds=3600.0)
    assert follower._refresh() is True  # first read merges
    refreshes = follower._refreshes
    assert follower._refresh() is False  # no change -> no-op
    assert follower._refreshes == refreshes
