"""Tests for the KlineStreamManager orchestrator, driven through injected fakes for
BybitMarketData (``get_instruments_info`` + ``get_klines``) and BybitKlineStreamPool:

  1. bootstrap respects the completion threshold and skips already-covered symbols
  2. universe-refresh diffs additions + removals and re-bootstraps new ones
  3. recovery from a flush file populates the store + skips covered symbols
  4. start -> stop tears everything down cleanly
"""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path

import pytest

from liquidity_migration.core._common import MS_PER_HOUR
from liquidity_migration.marketdata.kline_store import KlineStore
from liquidity_migration.marketdata.kline_stream_manager import (
    KlineStreamManager,
    _default_universe_filter,
    _kline_row_to_bar_dict,
)


class _FakeMarketData:
    """Minimal ``BybitMarketData`` stand-in. ``instruments`` is a callable so each test
    can sequence multi-call behaviour (e.g. universe-refresh seeing new listings).
    """

    def __init__(
        self,
        *,
        instruments_factory,
        kline_factory,
    ) -> None:
        self._instruments_factory = instruments_factory
        self._kline_factory = kline_factory
        self.kline_calls: list[str] = []
        self.instrument_calls = 0
        self.rate_limiter = None

    def get_instruments_info(self) -> list[dict]:
        self.instrument_calls += 1
        return list(self._instruments_factory(self.instrument_calls))

    def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list:
        self.kline_calls.append(symbol)
        return [_raw_kline_row(row) for row in self._kline_factory(symbol, interval, start, end)]


class _RecordingPool:
    """Manager-side pool fake: records subscribe / update / close calls."""

    def __init__(self) -> None:
        self.subscribed: list[list[str]] = []
        self.updates: list[set[str]] = []
        self.callbacks: list = []
        self.closed = False
        self.watchdog_started = False
        self.watchdog_stopped = False

    def subscribe(self, symbols, callback) -> None:
        self.subscribed.append(list(symbols))
        self.callbacks.append(callback)

    def update_subscriptions(self, new_symbols: set[str]) -> dict:
        self.updates.append(set(new_symbols))
        return {"added": 0, "removed": 0, "connections": 1}

    def close(self) -> None:
        self.closed = True

    def start_watchdog(self) -> None:
        self.watchdog_started = True

    def stop_watchdog(self) -> None:
        self.watchdog_stopped = True

    def stats(self) -> dict:
        return {"connections": 1}

def _bar_row(ts_ms: int, *, close: float = 100.0) -> dict:
    return {
        "ts_ms": ts_ms,
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "volume_base": 10.0,
        "turnover_quote": 1000.0,
    }


def _raw_kline_row(row: dict | list) -> list:
    if isinstance(row, list):
        return row
    return [
        row["ts_ms"],
        row["open"],
        row["high"],
        row["low"],
        row["close"],
        row["volume_base"],
        row["turnover_quote"],
    ]


def _instruments_payload(symbols: list[str]) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "status": "Trading",
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "contractType": "LinearPerpetual",
            "isPreListing": False,
        }
        for symbol in symbols
    ]


def _build_manager(
    *,
    tmp_path: Path,
    initial_symbols: list[str],
    pool: _RecordingPool | None = None,
    instruments_factory=None,
    kline_factory=None,
    **overrides,
) -> tuple[KlineStreamManager, _RecordingPool, _FakeMarketData]:
    pool = pool or _RecordingPool()
    def _default_instruments(call_n):
        return _instruments_payload(initial_symbols)
    def _default_klines(symbol, interval, start, end):
        # 5 days × 24 bars/day = 120 rows per symbol.
        return [_bar_row(start + i * MS_PER_HOUR, close=float(i)) for i in range(120)]
    market = _FakeMarketData(
        instruments_factory=instruments_factory or _default_instruments,
        kline_factory=kline_factory or _default_klines,
    )
    defaults = dict(
        market_data=market,
        cache_root=tmp_path,
        lookback_days=5,
        bootstrap_workers=4,
        universe_refresh_interval_seconds=0.0,  # disable refresh thread for tests
        bootstrap_completion_threshold=1.0,
        bootstrap_timeout_seconds=10.0,
        flush_interval_seconds=0.0,
        retain_days=30,
        topics_per_connection=10,
        pool=pool,
    )
    defaults.update(overrides)
    manager = KlineStreamManager(**defaults)
    return manager, pool, market


def test_default_universe_filter_keeps_only_linear_usdt_perp_trading() -> None:
    rows = [
        {"symbol": "BTCUSDT", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual"},
        {"symbol": "DOGEUSDT", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual", "isPreListing": True},
        {"symbol": "ETHUSDC", "status": "Trading", "quoteCoin": "USDC", "settleCoin": "USDC", "contractType": "LinearPerpetual"},
        {"symbol": "OLDUSDT", "status": "Settling", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual"},
        {"symbol": "ETHUSDT", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual"},
    ]
    universe = _default_universe_filter(rows)
    assert universe == ["BTCUSDT", "ETHUSDT"]


def test_bootstrap_fills_store_with_history(tmp_path: Path) -> None:
    manager, pool, market = _build_manager(
        tmp_path=tmp_path, initial_symbols=["BTCUSDT", "ETHUSDT"],
    )
    stats = manager.start()
    try:
        # Both symbols bootstrapped — each called get_klines once.
        assert sorted(market.kline_calls) == ["BTCUSDT", "ETHUSDT"]
        # Pool was subscribed before bootstrap so live bars start flowing.
        assert pool.subscribed == [["BTCUSDT", "ETHUSDT"]]
        assert manager.store().row_count() == 240  # 2 × 120
        assert stats["universe_size"] == 2
        assert stats["bootstrap"]["symbols_succeeded"] == 2
        assert stats["bootstrap"]["symbols_failed"] == 0
    finally:
        manager.stop()
    assert pool.closed is True


def test_bootstrap_skips_symbols_with_full_window_coverage_after_recovery(tmp_path: Path) -> None:
    """Bootstrap skips a recovered symbol only when coverage spans BOTH ends of the
    lookback window (newest >= end_ms AND oldest <= start_ms); partial-coverage
    symbols still bootstrap.
    """
    now_ms = int(time.time() * 1000)
    end_ms = (now_ms // MS_PER_HOUR) * MS_PER_HOUR - MS_PER_HOUR
    # lookback_days=5 in _build_manager defaults. Window = end_ms - 5d to end_ms.
    five_days_ms = 5 * 24 * MS_PER_HOUR

    pre_store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    # BTCUSDT: bars at oldest + newest end → spans the full window → skip
    pre_store.add_bar(
        "BTCUSDT",
        {"start": end_ms - five_days_ms, "open": "1", "high": "1",
         "low": "1", "close": "1", "volume": "1", "turnover": "1"},
        confirmed=True,
    )
    pre_store.add_bar(
        "BTCUSDT",
        {"start": end_ms, "open": "1", "high": "1",
         "low": "1", "close": "1", "volume": "1", "turnover": "1"},
        confirmed=True,
    )
    # ETHUSDT: ONLY the latest hour → does NOT span the window → bootstrap
    pre_store.add_bar(
        "ETHUSDT",
        {"start": end_ms, "open": "1", "high": "1",
         "low": "1", "close": "1", "volume": "1", "turnover": "1"},
        confirmed=True,
    )
    pre_store.flush_to_disk()

    manager, pool, market = _build_manager(
        tmp_path=tmp_path, initial_symbols=["BTCUSDT", "ETHUSDT"],
    )
    manager.start()
    try:
        # ETHUSDT must be bootstrapped (didn't span the window).
        # BTCUSDT must NOT be (did span the window).
        assert "ETHUSDT" in market.kline_calls
        assert "BTCUSDT" not in market.kline_calls
        assert manager.stats()["bootstrap"]["symbols_skipped_already_covered"] >= 1
    finally:
        manager.stop()


def test_bootstrap_fetches_the_newest_closed_bar(tmp_path: Path) -> None:
    """A symbol missing ONLY the newest closed bar must gain it at bootstrap.

    The bootstrap window's ``end_ms`` is the newest closed bar's open
    (inclusive intent) while get_klines' end is exclusive — without +interval
    at the call site the fetch returns zero new rows, the one-bar gap survives
    every restart, and the 00:20 daily decision starves whenever the WS
    confirm is missing (live 2026-08-04).
    """
    now_ms = int(time.time() * 1000)
    newest_closed_open_ms = (now_ms // MS_PER_HOUR) * MS_PER_HOUR - MS_PER_HOUR
    window_start_ms = newest_closed_open_ms - 5 * 24 * MS_PER_HOUR

    pre_store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    for ts in range(window_start_ms, newest_closed_open_ms, MS_PER_HOUR):
        pre_store.add_bar(
            "BTCUSDT",
            {"start": ts, "open": "1", "high": "1",
             "low": "1", "close": "1", "volume": "1", "turnover": "1"},
            confirmed=True,
        )
    pre_store.flush_to_disk()

    def _exclusive_end_klines(symbol, interval, start, end):
        # Mirrors BybitMarketData.get_klines: rows strictly below ``end``.
        return [_bar_row(ts, close=1.0) for ts in range(start, end, MS_PER_HOUR)]

    manager, _pool, market = _build_manager(
        tmp_path=tmp_path,
        initial_symbols=["BTCUSDT"],
        kline_factory=_exclusive_end_klines,
    )
    stats = manager.start()
    try:
        assert market.kline_calls == ["BTCUSDT"]
        assert stats["bootstrap"]["symbols_succeeded"] == 1
        assert stats["bootstrap"]["symbols_failed"] == 0
        frame = manager.store().get_klines(
            ["BTCUSDT"],
            start_ms=newest_closed_open_ms,
            end_ms=newest_closed_open_ms,
        )
        assert frame.height == 1
    finally:
        manager.stop()


def test_bootstrap_does_not_skip_symbol_with_only_latest_hour(tmp_path: Path) -> None:
    """The bootstrap skip keys on ``coverage_in_window``, not ``coverage_through``:
    otherwise a restart with a flush file holding one hour leaves the store on a tiny
    snapshot indefinitely.
    """
    now_ms = int(time.time() * 1000)
    end_ms = (now_ms // MS_PER_HOUR) * MS_PER_HOUR - MS_PER_HOUR

    pre_store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    # Just the latest hour — what a recovered short-lived flush would have.
    pre_store.add_bar(
        "BTCUSDT",
        {"start": end_ms, "open": "1", "high": "1",
         "low": "1", "close": "1", "volume": "1", "turnover": "1"},
        confirmed=True,
    )
    pre_store.flush_to_disk()

    manager, _pool, market = _build_manager(
        tmp_path=tmp_path, initial_symbols=["BTCUSDT"],
    )
    manager.start()
    try:
        # Bootstrap MUST have fired for BTCUSDT to refill the window.
        assert "BTCUSDT" in market.kline_calls
        assert manager.stats()["bootstrap"]["symbols_skipped_already_covered"] == 0
    finally:
        manager.stop()


def test_universe_refresh_subscribes_new_listings_and_unsubscribes_delistings(tmp_path: Path) -> None:
    """Refresh sees a new symbol on call 2 and a delisting on call 3."""
    def _instruments(call_n):
        if call_n == 1:
            return _instruments_payload(["BTCUSDT", "ETHUSDT"])
        if call_n == 2:
            return _instruments_payload(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        return _instruments_payload(["BTCUSDT", "SOLUSDT"])  # ETHUSDT delisted

    manager, pool, market = _build_manager(
        tmp_path=tmp_path,
        initial_symbols=["BTCUSDT", "ETHUSDT"],
        instruments_factory=_instruments,
    )
    manager.start()
    try:
        first_kline_calls = list(market.kline_calls)
        result_add = manager.force_refresh_universe()
        assert result_add["added"] == 1
        assert result_add["removed"] == 0
        assert "SOLUSDT" in pool.updates[-1]
        # New listing must be bootstrapped.
        assert "SOLUSDT" in market.kline_calls
        result_remove = manager.force_refresh_universe()
        assert result_remove["added"] == 0
        assert result_remove["removed"] == 1
        # ETHUSDT must be removed from the pool's most recent universe.
        assert "ETHUSDT" not in pool.updates[-1]
        assert manager.stats()["universe_size"] == 2
        # First call's klines are unchanged for already-bootstrapped symbols.
        assert market.kline_calls[:2] == first_kline_calls[:2]
    finally:
        manager.stop()


def test_universe_refresh_skips_diff_when_fetch_returns_empty(tmp_path: Path) -> None:
    """An empty universe fetch is a transient REST failure, not a mass delisting:
    diffing it would unsubscribe every symbol and sever the WS kline feed until the
    next successful refresh. Keep subscriptions, count an error, retry next tick.
    """
    call_count = {"n": 0}

    def _instruments_blip():
        call_count["n"] += 1
        if call_count["n"] >= 2:
            return _instruments_payload([])  # simulate REST failure → empty
        return _instruments_payload(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    manager, pool, _market = _build_manager(
        tmp_path=tmp_path,
        initial_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        instruments_factory=lambda _: _instruments_blip(),
    )
    manager.start()
    try:
        # Sanity: pool starts with all three subscribed.
        pre_universe = set(manager.universe_symbols())
        assert pre_universe == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        # Refresh tick where REST returns nothing. Universe must stay intact.
        result = manager.force_refresh_universe()
        assert result == {"added": 0, "removed": 0, "size": 3}
        post_universe = set(manager.universe_symbols())
        assert post_universe == pre_universe, (
            "empty universe fetch must NOT clear the existing universe"
        )
        # Error counter ticked up so operators can see the blip.
        assert manager.stats()["universe_refresh_errors"] >= 1
        # Pool's last update_subscriptions call must NOT be empty.
        if pool.updates:
            assert pool.updates[-1], "pool.update_subscriptions called with empty set"
    finally:
        manager.stop()


def test_universe_symbols_returns_sorted_current_universe(tmp_path: Path) -> None:
    """The long daemon scopes its public ticker WS to the same universe the kline
    manager bootstraps, so ``universe_symbols()`` must return a sorted snapshot
    consistent with stats.
    """
    manager, _pool, _market = _build_manager(
        tmp_path=tmp_path,
        initial_symbols=["SOLUSDT", "BTCUSDT", "ETHUSDT"],
    )
    manager.start()
    try:
        syms = manager.universe_symbols()
        assert syms == sorted(syms), "universe_symbols must be sorted for callers"
        assert set(syms) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        assert len(syms) == manager.stats()["universe_size"]
    finally:
        manager.stop()


def test_on_bar_dispatch_adds_to_store(tmp_path: Path) -> None:
    """Verify the pool→store fan-in: the callback the pool would call must
    insert a confirmed bar and skip an unconfirmed one."""
    manager, pool, market = _build_manager(
        tmp_path=tmp_path, initial_symbols=["BTCUSDT"],
    )
    manager.start()
    try:
        assert pool.callbacks, "pool was never subscribed"
        callback = pool.callbacks[-1]
        # Use a timestamp inside the retain window — the bootstrap inserted
        # bars in the current 5-day window, so a "now" bar is appended.
        now_ms = int(time.time() * 1000)
        bar = {
            "start": (now_ms // MS_PER_HOUR) * MS_PER_HOUR,
            "open": "1", "high": "1", "low": "1", "close": "9",
            "volume": "1", "turnover": "9",
        }
        # Confirmed bar lands in the store.
        before = manager.store().row_count()
        callback("BTCUSDT", bar, True)
        assert manager.store().row_count() == before + 1
        # Unconfirmed bar is skipped.
        callback("BTCUSDT", bar, False)
        assert manager.store().row_count() == before + 1
    finally:
        manager.stop()


def test_on_bar_sets_cycle_wake_event_on_new_confirmed_boundary(tmp_path: Path) -> None:
    """A confirmed bar at a NEW boundary sets the cycle-wake event so the daemon fires
    immediately. An unconfirmed bar and a same-boundary bar (the per-symbol hour-close
    burst) do not re-fire it -- the boundary-advance gate coalesces the burst.
    """
    manager, pool, market = _build_manager(tmp_path=tmp_path, initial_symbols=["BTCUSDT"])
    wake = threading.Event()
    manager.set_cycle_wake_event(wake)
    manager.start()
    try:
        callback = pool.callbacks[-1]
        now_ms = int(time.time() * 1000)
        h = (now_ms // MS_PER_HOUR) * MS_PER_HOUR
        bar = {"start": h, "open": "1", "high": "1", "low": "1", "close": "9", "volume": "1", "turnover": "9"}

        # Confirmed new-boundary bar -> wake set.
        wake.clear()
        callback("BTCUSDT", bar, True)
        assert wake.is_set()

        # Unconfirmed bar -> no wake (not a confirmed boundary).
        wake.clear()
        callback("BTCUSDT", bar, False)
        assert not wake.is_set()

        # Same-boundary confirmed bar from another symbol (the hour-close burst)
        # -> no re-wake; the boundary didn't advance.
        wake.clear()
        callback("ETHUSDT", bar, True)
        assert not wake.is_set()

        # Next hour -> boundary advances -> wake set again.
        wake.clear()
        callback("BTCUSDT", {**bar, "start": h + MS_PER_HOUR}, True)
        assert wake.is_set()
    finally:
        manager.stop()


def test_start_recovers_from_flush_file(tmp_path: Path) -> None:
    pre_store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    pre_store.add_bar(
        "BTCUSDT",
        {"start": 1000 * MS_PER_HOUR, "open": "1", "high": "1", "low": "1",
         "close": "1", "volume": "1", "turnover": "1"},
        confirmed=True,
    )
    pre_store.flush_to_disk()
    manager, _pool, _market = _build_manager(
        tmp_path=tmp_path, initial_symbols=["BTCUSDT"],
    )
    manager.start()
    try:
        # The recovered bar is still in the store after start.
        frame = manager.store().get_klines(["BTCUSDT"], start_ms=0, end_ms=10**14)
        assert frame.height >= 1
    finally:
        manager.stop()


def test_universe_refresh_handles_empty_change_quietly(tmp_path: Path) -> None:
    manager, pool, market = _build_manager(
        tmp_path=tmp_path, initial_symbols=["BTCUSDT", "ETHUSDT"],
    )
    manager.start()
    try:
        # Refresh with the same universe — no add, no remove, no pool update.
        before_updates = len(pool.updates)
        result = manager.force_refresh_universe()
        assert result == {"added": 0, "removed": 0, "size": 2}
        assert len(pool.updates) == before_updates
    finally:
        manager.stop()


def test_stop_is_idempotent_and_closes_pool(tmp_path: Path) -> None:
    manager, pool, _market = _build_manager(
        tmp_path=tmp_path, initial_symbols=["BTCUSDT"],
    )
    manager.start()
    manager.stop()
    manager.stop()  # idempotent
    assert pool.closed is True


def test_failed_bootstrap_records_error_but_does_not_block_start(tmp_path: Path) -> None:
    def _bad_klines(symbol, interval, start, end):
        if symbol == "ETHUSDT":
            raise RuntimeError("simulated venue error")
        return [_bar_row(start + i * MS_PER_HOUR) for i in range(10)]

    manager, _pool, _market = _build_manager(
        tmp_path=tmp_path,
        initial_symbols=["BTCUSDT", "ETHUSDT"],
        kline_factory=_bad_klines,
        bootstrap_completion_threshold=0.5,  # one good symbol is enough
        bootstrap_max_attempts_per_symbol=1,
    )
    manager.start()
    try:
        stats = manager.stats()
        assert stats["bootstrap"]["symbols_succeeded"] >= 1
        assert stats["bootstrap"]["symbols_failed"] >= 1
        assert "ETHUSDT" in stats["bootstrap"]["last_error"]
    finally:
        manager.stop()


def test_kline_row_conversion_requires_bybit_array_shape() -> None:
    list_row = [1, "1.0", "2.0", "0.5", "1.5", "10.0", "15.0"]
    converted = _kline_row_to_bar_dict(list_row)
    assert converted["start"] == 1
    assert set(converted) == {"start", "open", "high", "low", "close", "volume", "turnover"}
    with pytest.raises(ValueError, match="exactly seven"):
        _kline_row_to_bar_dict(list_row[:-1])


def test_refresh_thread_runs_periodically(tmp_path: Path) -> None:
    """The refresh thread is wired into the lifecycle; ``force_refresh_universe()``
    drives the diff deterministically rather than racing the scheduler, and the timer
    assertion below covers the loop itself.
    """

    refresh_calls = threading.Event()

    def _instruments(call_n):
        if call_n == 1:
            return _instruments_payload(["BTCUSDT"])
        refresh_calls.set()
        return _instruments_payload(["BTCUSDT", "ETHUSDT"])

    manager, pool, market = _build_manager(
        tmp_path=tmp_path,
        initial_symbols=["BTCUSDT"],
        instruments_factory=_instruments,
        universe_refresh_interval_seconds=0.05,
    )
    manager.start()
    try:
        # The refresh thread should fire at least one extra instruments
        # fetch within a generous deadline. The Event makes the wait
        # signal-driven rather than poll-driven. 30s deadline covers
        # slow CI workers.
        assert refresh_calls.wait(timeout=30.0), "refresh thread did not run within 30s"
        # Universe being updated to 2 symbols is the load-bearing
        # assertion — it proves force_refresh_universe ran past the
        # `self._universe = new_universe` step. The refreshes/errors
        # counters are mutated AFTER the universe is applied, so they
        # race the test thread; do not assert on them.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if manager.stats()["universe_size"] >= 2:
                break
            time.sleep(0.02)
        assert manager.stats()["universe_size"] == 2
    finally:
        manager.stop()


def test_construction_rejects_invalid_params(tmp_path: Path) -> None:
    class _Dummy:
        def get_instruments_info(self): return []
        def get_klines(self, *a, **kw): return []

    with pytest.raises(ValueError):
        KlineStreamManager(market_data=_Dummy(), cache_root=tmp_path, lookback_days=0)
    with pytest.raises(ValueError):
        KlineStreamManager(market_data=_Dummy(), cache_root=tmp_path,
                           bootstrap_completion_threshold=1.5)
    with pytest.raises(ValueError):
        KlineStreamManager(market_data=_Dummy(), cache_root=tmp_path, bootstrap_workers=0)


def test_bootstrap_stats_count_every_completion_even_past_threshold(tmp_path: Path) -> None:
    """Reaching the completion threshold must not stop the ``as_completed`` loop from
    counting later results: the executor waits for every future anyway, so an early
    break only undercounts ``symbols_succeeded``.
    """

    # 10 symbols. Threshold = 0.5 means we'd trip at 5 completions. The
    # remaining 5 must still be counted in the stats.
    def _instruments_factory(call_n):
        return [
            {
                "symbol": f"SYM{i:02d}USDT",
                "status": "Trading",
                "quoteCoin": "USDT",
                "settleCoin": "USDT",
                "contractType": "LinearPerpetual",
                "isPreListing": False,
            }
            for i in range(10)
        ]

    def _kline_factory(symbol, interval, start, end):
        return [
            {
                "ts_ms": start + i * MS_PER_HOUR,
                "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                "volume_base": 1.0, "turnover_quote": 1.0,
            }
            for i in range(5)
        ]

    pool = _RecordingPool()
    market = _FakeMarketData(
        instruments_factory=_instruments_factory, kline_factory=_kline_factory,
    )
    manager = KlineStreamManager(
        market_data=market, cache_root=tmp_path,
        lookback_days=2, bootstrap_workers=4,
        universe_refresh_interval_seconds=0.0,
        bootstrap_completion_threshold=0.5,  # trips after 5 of 10 symbols
        bootstrap_timeout_seconds=10.0,
        flush_interval_seconds=0.0, retain_days=30,
        topics_per_connection=10, pool=pool,
    )
    manager.start()
    try:
        stats = manager.stats()["bootstrap"]
        # Every symbol's bootstrap completion was iterated and counted.
        assert stats["symbols_attempted"] == 10
        assert stats["symbols_succeeded"] == 10
        assert stats["symbols_failed"] == 0
        # And the store has bars for all 10 (5 bars × 10 symbols).
        assert manager.store().row_count() == 50
    finally:
        manager.stop()


def test_start_respects_shutdown_event_during_bootstrap(tmp_path: Path) -> None:
    """When ``shutdown_event`` fires during bootstrap, ``manager.start`` must exit
    promptly rather than block until every REST future finishes (90+ seconds ->
    systemd SIGKILL). Remaining futures are cancelled and the loop breaks.
    """

    # Slow REST: each fetch takes 0.5s so we can interleave the shutdown.
    def _slow_klines(symbol, interval, start, end):
        time.sleep(0.5)
        return [
            {"ts_ms": start + i * MS_PER_HOUR, "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 1.0, "volume_base": 1.0, "turnover_quote": 1.0}
            for i in range(2)
        ]

    def _instruments(call_n):
        return _instruments_payload([f"SYM{i:02d}USDT" for i in range(30)])

    pool = _RecordingPool()
    market = _FakeMarketData(
        instruments_factory=_instruments, kline_factory=_slow_klines,
    )
    manager = KlineStreamManager(
        market_data=market, cache_root=tmp_path,
        lookback_days=2, bootstrap_workers=2,  # only 2 workers so we have queued futures
        universe_refresh_interval_seconds=0.0,
        bootstrap_completion_threshold=1.0,
        bootstrap_timeout_seconds=60.0,
        flush_interval_seconds=0.0, retain_days=30,
        topics_per_connection=10, pool=pool,
    )
    shutdown = threading.Event()
    # Fire the shutdown after 0.7s — after the first batch of futures
    # has started but well before all 30 symbols finish.
    threading.Timer(0.7, shutdown.set).start()

    t = time.monotonic()
    manager.start(shutdown_event=shutdown)
    elapsed = time.monotonic() - t
    try:
        # start() should return WELL under the 60s bootstrap timeout —
        # demonstrates the shutdown signal was honored.
        assert elapsed < 30.0, f"start() blocked for {elapsed:.1f}s past shutdown"
        # Some symbols processed before shutdown; the rest were cancelled.
        # Loose bound — exact count depends on scheduler.
        stats = manager.stats()["bootstrap"]
        assert stats["symbols_succeeded"] >= 1, "no symbols succeeded before shutdown?"
        assert stats["symbols_succeeded"] < 30, "shutdown ignored — all symbols completed"
    finally:
        manager.stop()


def test_start_bootstraps_before_subscribing_pool(tmp_path: Path) -> None:
    """The pool must be subscribed AFTER bootstrap. Subscribing first starves REST
    workers via WS GIL pressure -- 100/567 symbols in 383s instead of ~95s.
    """

    call_order: list[str] = []

    def _instruments(call_n):
        call_order.append("instruments")
        return _instruments_payload(["BTCUSDT", "ETHUSDT"])

    def _klines(symbol, interval, start, end):
        call_order.append(f"klines:{symbol}")
        return [
            {"ts_ms": start + i * MS_PER_HOUR, "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 1.0, "volume_base": 1.0, "turnover_quote": 1.0}
            for i in range(3)
        ]

    class _OrderTrackingPool:
        def __init__(self):
            self.subscribed_at: int | None = None

        def subscribe(self, symbols, callback):
            self.subscribed_at = len(call_order)
            call_order.append("pool.subscribe")

        def update_subscriptions(self, syms): return {"added": 0, "removed": 0}
        def close(self): pass
        def start_watchdog(self): pass
        def stop_watchdog(self): pass
        def stats(self): return {}
    pool = _OrderTrackingPool()
    market = _FakeMarketData(instruments_factory=_instruments, kline_factory=_klines)
    manager = KlineStreamManager(
        market_data=market, cache_root=tmp_path,
        lookback_days=2, bootstrap_workers=2,
        universe_refresh_interval_seconds=0.0,
        bootstrap_completion_threshold=1.0,
        flush_interval_seconds=0.0,
        topics_per_connection=10, pool=pool,
    )
    manager.start()
    try:
        # Kline fetches happen BEFORE pool.subscribe.
        klines_indices = [i for i, c in enumerate(call_order) if c.startswith("klines:")]
        subscribe_index = call_order.index("pool.subscribe")
        assert klines_indices, "no klines fetched"
        assert max(klines_indices) < subscribe_index, (
            f"pool subscribed at index {subscribe_index} before last kline at "
            f"{max(klines_indices)} — REST bootstrap will be starved"
        )
    finally:
        manager.stop()


def test_stats_reflect_ws_freshness_via_lag(tmp_path: Path) -> None:
    manager, pool, _market = _build_manager(
        tmp_path=tmp_path, initial_symbols=["BTCUSDT"],
    )
    manager.start()
    try:
        callback = pool.callbacks[-1]
        now_ms = int(time.time() * 1000)
        bar_ts = (now_ms // MS_PER_HOUR) * MS_PER_HOUR
        callback(
            "BTCUSDT",
            {
                "start": bar_ts,
                "open": "1", "high": "1", "low": "1", "close": "1",
                "volume": "1", "turnover": "1",
            },
            True,
        )
        stats = manager.stats()
        assert stats["newest_ts_lag_seconds"] is not None
        assert stats["newest_ts_lag_seconds"] < 3700.0  # within ~1h
    finally:
        manager.stop()


def test_universe_refresh_threshold_log_counts_only_new_targets(tmp_path: Path, caplog) -> None:
    """The bootstrap completion-threshold log must be measured against the symbols
    actually being bootstrapped (the new listings on a refresh), not the full
    universe, whose already-covered denominator fires the log immediately.
    """
    def _instruments(call_n):
        if call_n == 1:
            return _instruments_payload(["BTCUSDT", "ETHUSDT"])
        return _instruments_payload(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    manager, _pool, market = _build_manager(
        tmp_path=tmp_path,
        initial_symbols=["BTCUSDT", "ETHUSDT"],
        instruments_factory=_instruments,
        bootstrap_completion_threshold=1.0,
    )
    manager.start()
    try:
        with caplog.at_level(
            "INFO", logger="liquidity_migration.marketdata.kline_stream_manager"
        ):
            manager.force_refresh_universe()
        # The new listing was bootstrapped.
        assert "SOLUSDT" in market.kline_calls
        # Any 'completion threshold reached' log on the refresh path must be
        # scoped to the 1 new target (denominator == 1), never the 3-symbol
        # universe, not the full one.
        threshold_logs = [
            r.message for r in caplog.records
            if "completion threshold" in r.message
        ]
        for msg in threshold_logs:
            assert "/3" not in msg, msg
    finally:
        manager.stop()


# --- universe_refresh_errors must not double-count ---
#
# Defect: ``universe_refresh_errors`` was double-counted on the default-fetcher
# empty path. When ``force_refresh_universe`` calls ``_fetch_universe`` and the
# default fetcher's ``get_instruments_info`` raises, ``_fetch_universe`` already
# increments ``_universe_refresh_errors`` (line ~413) and returns ``[]``; the
# empty-set guard in ``force_refresh_universe`` then increments it a SECOND time
# for the same underlying error.
#
# These tests pin:
#   * the default-fetcher REST-exception path counts the error exactly ONCE
#     (a double-count reads 2), and
#   * the normal / other empty paths are unchanged (custom-fetcher empty and a
#     default fetch that simply filters to empty each count exactly once; a
#     successful refresh counts zero).


class _Market:
    """Default-fetcher stand-in: get_instruments_info drives the path."""

    def __init__(self, instruments_factory, kline_factory) -> None:
        self._instruments_factory = instruments_factory
        self._kline_factory = kline_factory
        self.instrument_calls = 0
        self.kline_calls: list[str] = []
        # __post_init__ wires a limiter only when this attribute is None.
        self.rate_limiter = None

    def get_instruments_info(self) -> list[dict]:
        self.instrument_calls += 1
        return list(self._instruments_factory(self.instrument_calls))

    def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list:
        self.kline_calls.append(symbol)
        return [_raw_kline_row(row) for row in self._kline_factory(symbol, interval, start, end)]


class _Pool:
    def __init__(self) -> None:
        self.updates: list[set[str]] = []
        self.subscribed: list[list[str]] = []
        self.closed = False

    def subscribe(self, symbols, callback) -> None:
        self.subscribed.append(list(symbols))

    def update_subscriptions(self, new_symbols: set[str]) -> dict:
        self.updates.append(set(new_symbols))
        return {"added": 0, "removed": 0, "connections": 1}

    def close(self) -> None:
        self.closed = True

    def start_watchdog(self) -> None:
        pass

    def stats(self) -> dict:
        return {"connections": 1}


def _build(tmp_path: Path, instruments_factory) -> tuple[KlineStreamManager, _Pool, _Market]:
    pool = _Pool()

    def _klines(symbol, interval, start, end):
        # A single page of bars is plenty for bootstrap to mark covered.
        from liquidity_migration.core._common import MS_PER_HOUR

        return [
            {
                "ts_ms": start + i * MS_PER_HOUR,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume_base": 10.0,
                "turnover_quote": 15.0,
            }
            for i in range(120)
        ]

    market = _Market(instruments_factory, _klines)
    manager = KlineStreamManager(
        market_data=market,
        cache_root=tmp_path,
        lookback_days=5,
        bootstrap_workers=2,
        universe_refresh_interval_seconds=0.0,  # no refresh thread
        bootstrap_completion_threshold=1.0,
        bootstrap_timeout_seconds=10.0,
        flush_interval_seconds=0.0,
        retain_days=30,
        topics_per_connection=10,
        pool=pool,
    )
    return manager, pool, market


def test_default_fetcher_rest_exception_counts_error_once(tmp_path: Path) -> None:
    """First fetch succeeds (start), second raises (refresh blip): the exception must be
    counted exactly once, not in both ``_fetch_universe`` and the empty-set guard.
    """

    def _instruments(call_n: int):
        if call_n >= 2:
            raise RuntimeError("simulated REST 5xx")
        return _instruments_payload(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    manager, pool, _market = _build(tmp_path, _instruments)
    manager.start()
    try:
        assert manager.stats()["universe_refresh_errors"] == 0
        result = manager.force_refresh_universe()
        # Universe is preserved (transient-failure guard).
        assert result == {"added": 0, "removed": 0, "size": 3}
        assert set(manager.universe_symbols()) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        # The single underlying error is counted exactly once, not twice.
        assert manager.stats()["universe_refresh_errors"] == 1
        # Existing subscriptions untouched on the empty path.
        assert pool.updates == []
    finally:
        manager.stop()


def test_default_fetcher_filtered_to_empty_counts_error_once(tmp_path: Path) -> None:
    """The default fetch returns rows that filter to empty with no exception, so
    ``_fetch_universe`` does not count and the empty-set guard must count exactly once.
    """

    def _instruments(call_n: int):
        if call_n >= 2:
            return _instruments_payload([])  # valid call, no symbols
        return _instruments_payload(["BTCUSDT", "ETHUSDT"])

    manager, _pool, _market = _build(tmp_path, _instruments)
    manager.start()
    try:
        assert manager.stats()["universe_refresh_errors"] == 0
        result = manager.force_refresh_universe()
        assert result == {"added": 0, "removed": 0, "size": 2}
        assert manager.stats()["universe_refresh_errors"] == 1
    finally:
        manager.stop()


def test_successful_refresh_counts_no_error(tmp_path: Path) -> None:
    """Normal happy path: a non-empty refresh records zero errors and the diff/return shape is unchanged by the fix."""

    def _instruments(call_n: int):
        return _instruments_payload(["BTCUSDT", "ETHUSDT"])

    manager, _pool, _market = _build(tmp_path, _instruments)
    manager.start()
    try:
        result = manager.force_refresh_universe()
        assert result == {"added": 0, "removed": 0, "size": 2}
        assert manager.stats()["universe_refresh_errors"] == 0
    finally:
        manager.stop()




class _PaginatingMarketData:
    """Mimics ``BybitMarketData``: ``get_klines`` paginates and each page acquires the
    wired ``rate_limiter`` once. ``rate_limiter`` starts None exactly like the daemons
    build it, so the manager must wire its own.
    """

    def __init__(self, *, instruments, pages_per_symbol: int) -> None:
        self._instruments = instruments
        self._pages = pages_per_symbol
        self.rate_limiter = None
        self.kline_calls: list[str] = []

    def get_instruments_info(self) -> list[dict]:
        return list(self._instruments)

    def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list:
        self.kline_calls.append(symbol)
        bars = []
        for page in range(self._pages):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()  # one acquire per HTTP page, like _get
            bars.append([start + page * MS_PER_HOUR, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        return bars


class _CountingLimiter:
    def __init__(self) -> None:
        self.acquires = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            self.acquires += 1


def test_bootstrap_symbol_no_longer_takes_a_manual_limiter() -> None:
    """The limiter lives on the market client, acquiring once per paginated call, so
    ``_bootstrap_symbol`` must no longer accept ``shared_limiter``.
    """
    params = inspect.signature(KlineStreamManager._bootstrap_symbol).parameters
    assert "shared_limiter" not in params


def test_bootstrap_wires_limiter_and_acquires_once_per_page(tmp_path) -> None:
    """Bootstrap rate-limits each paginated ``get_klines`` call, not once per symbol:
    with a 3-page client, a 2-symbol bootstrap must acquire 6 times.
    """
    symbols = ["AAAUSDT", "BBBUSDT"]
    market = _PaginatingMarketData(
        instruments=_instruments_payload(symbols), pages_per_symbol=3,
    )

    class _NoopPool:
        def subscribe(self, *a, **k): pass
        def update_subscriptions(self, *a, **k): return {}
        def close(self): pass
        def start_watchdog(self): pass
        def stop_watchdog(self): pass
        def stats(self): return {}

    manager = KlineStreamManager(
        market_data=market, cache_root=tmp_path,
        lookback_days=2, bootstrap_workers=2,
        universe_refresh_interval_seconds=0.0,
        bootstrap_completion_threshold=1.0,
        bootstrap_timeout_seconds=30.0,
        flush_interval_seconds=0.0, retain_days=30,
        topics_per_connection=10, pool=_NoopPool(),
    )
    # The manager must have wired ITS limiter onto the (None-limiter) client.
    assert market.rate_limiter is not None
    # Swap in a counting limiter on the client to count per-page acquires.
    counter = _CountingLimiter()
    market.rate_limiter = counter
    manager.start()
    try:
        # 2 symbols * 3 pages each = 6 acquires (NOT 2, the once-per-symbol bug).
        assert counter.acquires == 6
        assert sorted(market.kline_calls) == ["AAAUSDT", "BBBUSDT"]
    finally:
        manager.stop()


def test_refresh_bootstrap_honors_shutdown_promptly(tmp_path) -> None:
    """A universe-refresh bootstrap must honor the manager's refresh-stop signal (what
    ``stop()`` sets on SIGTERM): setting ``_refresh_stop`` mid-flight cancels the pool
    and returns promptly instead of running to the 1200s bootstrap deadline.
    """
    refresh_targets = [f"SYM{i:02d}USDT" for i in range(20)]

    def _instruments(call_n):
        # Refresh (call >=2) adds the new listings to bootstrap.
        base = ["BTCUSDT"]
        return _instruments_payload(base + (refresh_targets if call_n >= 2 else []))

    started = threading.Event()

    def _slow_klines(symbol, interval, start, end):
        started.set()
        time.sleep(0.5)  # slow REST page
        return [{
            "ts_ms": start, "open": 1.0, "high": 1.0, "low": 1.0,
            "close": 1.0, "volume_base": 1.0, "turnover_quote": 1.0,
        }]

    class _FakeMarket:
        def __init__(self):
            self.rate_limiter = None
            self._n = 0

        def get_instruments_info(self):
            self._n += 1
            return _instruments(self._n)

        def get_klines(self, symbol, interval, start, end):
            return [_raw_kline_row(row) for row in _slow_klines(symbol, interval, start, end)]

    class _NoopPool:
        def subscribe(self, *a, **k): pass
        def update_subscriptions(self, *a, **k): return {}
        def close(self): pass
        def start_watchdog(self): pass
        def stop_watchdog(self): pass
        def stats(self): return {}

    market = _FakeMarket()
    manager = KlineStreamManager(
        market_data=market, cache_root=tmp_path,
        lookback_days=2, bootstrap_workers=2,
        universe_refresh_interval_seconds=0.0,  # no auto refresh thread
        bootstrap_completion_threshold=1.0,
        bootstrap_timeout_seconds=1200.0,  # the deadline the orphan would run to
        flush_interval_seconds=0.0, retain_days=30,
        topics_per_connection=10, pool=_NoopPool(),
    )
    manager.start()  # bootstraps BTCUSDT only
    try:
        # Fire the refresh in a background thread; it will start a slow bootstrap
        # of the 20 new listings. Set the manager's refresh-stop shortly after the
        # first REST call begins — this is what stop() does on SIGTERM.
        result: dict = {}

        def _run_refresh():
            result["out"] = manager.force_refresh_universe()

        th = threading.Thread(target=_run_refresh)
        t0 = time.monotonic()
        th.start()
        assert started.wait(timeout=5.0), "refresh bootstrap never issued a REST call"
        manager._refresh_stop.set()  # SIGTERM-equivalent mid-bootstrap
        th.join(timeout=10.0)
        elapsed = time.monotonic() - t0
        assert not th.is_alive(), "refresh bootstrap ignored shutdown — still running"
        # Must return WELL under the 1200s deadline (proves the signal was honored).
        assert elapsed < 30.0, f"refresh bootstrap blocked {elapsed:.1f}s past shutdown"
    finally:
        manager._refresh_stop.clear()
        manager.stop()


def test_store_retention_covers_the_bootstrap_lookback(tmp_path) -> None:
    # Eviction is anchored to the NEWEST bar, so a store retention shorter
    # than the manager's lookback evicts the window head as it lands and the
    # reader's full-window coverage check can never pass for a name older
    # than retention (observed live 2026-08-03: LONG served 4/120 symbols
    # with lookback 100d over the old fixed 90d retention). The manager must
    # raise retention to cover its own lookback, with a one-day margin.
    class _Dummy:
        def get_instruments_info(self):
            return []

        def get_klines(self, *args, **kwargs):
            return []

        rate_limiter = None

    manager = KlineStreamManager(market_data=_Dummy(), cache_root=tmp_path, lookback_days=100)
    assert manager.retain_days == 101
    assert manager.store().stats()["retain_days"] == 101

    # An explicitly wider retention is preserved, never shrunk.
    wide = KlineStreamManager(
        market_data=_Dummy(), cache_root=tmp_path, lookback_days=10, retain_days=90
    )
    assert wide.retain_days == 90
    assert wide.store().stats()["retain_days"] == 90
