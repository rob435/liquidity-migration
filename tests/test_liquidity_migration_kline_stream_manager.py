"""Tests for the KlineStreamManager orchestrator.

The manager is built so REST + WS dependencies inject through dataclass
fields: a fake BybitMarketData covers ``get_instruments_info`` +
``get_klines``, a fake BybitKlineStreamPool covers subscribe + update +
close. Tests verify the four lifecycle pillars:

  1. bootstrap respects the completion threshold and skips already-covered
     symbols
  2. universe-refresh diffs additions + removals and re-bootstraps new ones
  3. recovery from a flush file populates the store + skips bootstrap for
     already-covered symbols
  4. start → stop tears everything down cleanly
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.kline_store import KlineStore
from liquidity_migration.kline_stream_manager import (
    KlineStreamManager,
    _default_universe_filter,
    _kline_row_to_bar_dict,
)


class _FakeMarketData:
    """Minimal BybitMarketData stand-in for tests.

    ``instruments`` is a callable so each test can sequence multi-call
    behaviour (e.g. universe-refresh seeing new listings on call N+1)."""

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

    def get_instruments_info(self) -> list[dict]:
        self.instrument_calls += 1
        return list(self._instruments_factory(self.instrument_calls))

    def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list:
        self.kline_calls.append(symbol)
        return list(self._kline_factory(symbol, interval, start, end))


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

    def subscribed_symbols(self) -> set[str]:
        if not self.subscribed:
            return set()
        return set(self.subscribed[-1])


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
        # Store has the bars.
        assert manager.store().row_count() == 240  # 2 × 120
        # Universe size matches.
        assert stats["universe_size"] == 2
        assert stats["bootstrap"]["symbols_succeeded"] == 2
        assert stats["bootstrap"]["symbols_failed"] == 0
    finally:
        manager.stop()
    assert pool.closed is True


def test_bootstrap_skips_symbols_with_full_window_coverage_after_recovery(tmp_path: Path) -> None:
    """If a recovered symbol covers the FULL lookback window, bootstrap
    skips it. Coverage must span both ends of the window (newest >= end_ms
    AND oldest <= start_ms) — covering only the latest hour is NOT
    enough; bootstrap fires for partial-coverage symbols too."""
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


def test_bootstrap_does_not_skip_symbol_with_only_latest_hour(tmp_path: Path) -> None:
    """Regression: previously, bootstrap-skip used coverage_through(end_ms),
    so a symbol with just the latest hour was considered "covered" and
    bootstrap was skipped. Restart-with-flush-file would then leave the
    store on a tiny snapshot indefinitely. Now coverage_in_window is used,
    so partial-coverage symbols re-bootstrap on every start."""
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
        # Universe size reflects the final state.
        assert manager.stats()["universe_size"] == 2
        # First call's klines are unchanged for already-bootstrapped symbols.
        assert market.kline_calls[:2] == first_kline_calls[:2]
    finally:
        manager.stop()


def test_universe_refresh_skips_diff_when_fetch_returns_empty(tmp_path: Path) -> None:
    """A REST blip during universe refresh used to unsubscribe the pool
    from every symbol because the empty fetch was diffed against the
    existing universe (every previous symbol counted as "delisted").
    That silently severed the WS kline feed until the next refresh
    succeeded. Now an empty fetch is treated as a transient failure:
    keep existing subscriptions, count an error, retry next tick."""
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
    """The long daemon scopes its public ticker WS to the same universe
    the kline manager bootstraps. Manager exposes universe_symbols() as
    the contract; must return a sorted snapshot consistent with stats."""
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


def test_kline_row_normalization_accepts_dict_and_list_shapes() -> None:
    dict_row = {
        "ts_ms": 1, "open": 1.0, "high": 2.0, "low": 0.5,
        "close": 1.5, "volume_base": 10.0, "turnover_quote": 15.0,
    }
    list_row = [1, "1.0", "2.0", "0.5", "1.5", "10.0", "15.0"]
    a = _kline_row_to_bar_dict(dict_row)
    b = _kline_row_to_bar_dict(list_row)
    assert a["start"] == 1
    assert b["start"] == 1
    # Both contain the keys the parser expects.
    for d in (a, b):
        for key in ("start", "open", "high", "low", "close", "volume", "turnover"):
            assert key in d


def test_refresh_thread_runs_periodically(tmp_path: Path) -> None:
    """Verifies the refresh thread is wired into the lifecycle. Uses
    force_refresh_universe() to drive the diff deterministically rather
    than racing the scheduler — the thread's mere existence + the manual
    refresh prove the integration; the thread loop itself is exercised
    by the timer assertion below."""

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
    """Regression: the early-exit-then-break in _bootstrap_universe used to
    stop iterating the as_completed loop the moment the threshold was
    reached. The executor's `with` block waited for all futures anyway,
    but their results never incremented the stats — so symbols_succeeded
    undercounted the actual bootstrapped symbols. After the fix, every
    completion is counted."""

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
    """Regression: when shutdown_event fires during bootstrap, manager.start
    must exit promptly instead of blocking until every REST future finishes.
    The previous behaviour blocked for 90+ seconds → systemd SIGKILL.

    Triggered by setting the shutdown event AFTER the bootstrap loop has
    picked up at least one future. The remaining futures get cancelled,
    the for-loop breaks, and start() returns within seconds."""

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
    """Regression: the pool must be subscribed AFTER bootstrap, not before.

    Earlier ordering (pool first, then bootstrap) starved REST workers via
    WS GIL pressure — bootstrap got 100/567 symbols in 383s instead of
    completing in ~95s. This test pins the order so a refactor can't
    accidentally re-introduce the regression."""

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
        def subscribed_symbols(self): return set()

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
