"""Tests for the WS-driven kline store.

Covers thread-safety, eviction, idempotent add, get-by-range, persistence
round-trip, and the flush thread. Tests deliberately exercise the same
schema contract the cycle's _download_recent_1h_klines uses so the store is
a verified drop-in for the REST path.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.kline_store import (
    WS_STORE_SOURCE,
    KlineStore,
    _empty_klines_frame,
    _parse_ws_kline_event,
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


def test_add_bar_skips_unconfirmed_event() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    accepted = store.add_bar("BTCUSDT", _ws_bar(0), confirmed=False)
    assert accepted is False
    assert store.row_count() == 0
    assert store.stats()["adds_skipped_unconfirmed"] == 1


def test_add_bar_inserts_confirmed_event() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    accepted = store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR, close=42.0), confirmed=True)
    assert accepted is True
    frame = store.get_klines(["BTCUSDT"], start_ms=0, end_ms=10 * MS_PER_HOUR)
    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["symbol"] == "BTCUSDT"
    assert row["ts_ms"] == MS_PER_HOUR
    assert row["close"] == pytest.approx(42.0)
    assert row["source"] == WS_STORE_SOURCE


def test_add_bar_is_idempotent_on_same_ts_ms() -> None:
    """A reconnect that re-emits the most recent bar must not duplicate the
    row; the second add overwrites the first in place."""
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR, close=100.0), confirmed=True)
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR, close=200.0), confirmed=True)
    frame = store.get_klines(["BTCUSDT"], start_ms=0, end_ms=10 * MS_PER_HOUR)
    assert frame.height == 1
    assert frame.row(0, named=True)["close"] == pytest.approx(200.0)


def test_add_bar_rejects_unparseable_payload() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    assert store.add_bar("BTCUSDT", {"open": "1"}, confirmed=True) is False
    assert store.add_bar("BTCUSDT", {"start": "not_a_number"}, confirmed=True) is False
    assert store.row_count() == 0


def test_add_bar_skips_when_symbol_empty() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    assert store.add_bar("", _ws_bar(0), confirmed=True) is False
    assert store.row_count() == 0


def test_eviction_drops_bars_past_retain_days() -> None:
    """Add a fresh bar — old bars beyond retain_days × MS_PER_DAY must be
    purged on the same insert, keeping the heap flat."""
    store = KlineStore(cache_root=None, retain_days=2, flush_interval_seconds=0.0)
    base = 100 * MS_PER_DAY
    # Two old bars (3 days ago) and one fresh bar.
    store.add_bar("BTCUSDT", _ws_bar(base - 3 * MS_PER_DAY), confirmed=True)
    store.add_bar("ETHUSDT", _ws_bar(base - 3 * MS_PER_DAY), confirmed=True)
    # Both should still be present until the eviction reference advances.
    assert store.row_count() == 2
    # New bar advances the reference timestamp; both old bars now exceed
    # retain_days (2d) and must be evicted.
    store.add_bar("XRPUSDT", _ws_bar(base), confirmed=True)
    stats = store.stats()
    assert stats["rows"] == 1
    assert stats["adds_evicted"] >= 2
    # Symbols that lost all their bars should drop out entirely so the
    # symbols_with_coverage_through view stays clean.
    assert "BTCUSDT" not in store.symbols_with_coverage_through(0)
    assert "ETHUSDT" not in store.symbols_with_coverage_through(0)
    assert store.symbol_count() == 1


def test_insert_drops_bar_older_than_retain_window_immediately() -> None:
    """An out-of-window historical bar from a broken upstream must be
    silently dropped — never inserted, never evicted later."""
    store = KlineStore(cache_root=None, retain_days=1, flush_interval_seconds=0.0)
    store.add_bar("BTCUSDT", _ws_bar(100 * MS_PER_DAY), confirmed=True)
    # 5 days older than the most recent reference; outside retain window.
    inserted = store.add_bar("ETHUSDT", _ws_bar(95 * MS_PER_DAY), confirmed=True)
    assert inserted is False
    assert "ETHUSDT" not in store.symbols_with_coverage_through(0)


def test_far_future_bar_is_rejected_and_does_not_mass_evict() -> None:
    """H6: a corrupt far-future ts must not advance the eviction reference and
    evict every legitimate bar (total store loss → silent REST fallback)."""
    from liquidity_migration.kline_store import _utc_now_ms

    store = KlineStore(cache_root=None, retain_days=2, flush_interval_seconds=0.0)
    now = _utc_now_ms()
    assert store.add_bar("BTCUSDT", _ws_bar(now - MS_PER_HOUR), confirmed=True) is True
    assert store.add_bar("ETHUSDT", _ws_bar(now - 2 * MS_PER_HOUR), confirmed=True) is True
    assert store.symbol_count() == 2
    # A bar 30 days in the future (e.g. ns-vs-ms parse glitch) is corrupt.
    inserted = store.add_bar("XRPUSDT", _ws_bar(now + 30 * MS_PER_DAY), confirmed=True)
    assert inserted is False
    # The two legitimate symbols survive — NOT mass-evicted.
    assert store.symbol_count() == 2
    frame = store.get_klines(["BTCUSDT"], start_ms=now - MS_PER_HOUR, end_ms=now)
    assert frame.height == 1


def test_bootstrap_skips_far_future_bars() -> None:
    """H6: bulk bootstrap must drop corrupt far-future bars, not let them
    poison the eviction reference for the good bars in the same batch."""
    from liquidity_migration.kline_store import _utc_now_ms

    store = KlineStore(cache_root=None, retain_days=2, flush_interval_seconds=0.0)
    now = _utc_now_ms()
    accepted = store.bootstrap_symbol(
        "BTCUSDT",
        [_ws_bar(now - MS_PER_HOUR), _ws_bar(now + 30 * MS_PER_DAY)],
    )
    assert accepted == 1
    assert store.symbol_count() == 1


def test_get_klines_returns_inclusive_window() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    for hour in range(10):
        ts = hour * MS_PER_HOUR
        store.add_bar("BTCUSDT", _ws_bar(ts, close=float(hour)), confirmed=True)
    frame = store.get_klines(
        ["BTCUSDT"], start_ms=2 * MS_PER_HOUR, end_ms=5 * MS_PER_HOUR,
    )
    assert frame["ts_ms"].to_list() == [
        2 * MS_PER_HOUR,
        3 * MS_PER_HOUR,
        4 * MS_PER_HOUR,
        5 * MS_PER_HOUR,
    ]
    # Both endpoints are inclusive.
    assert frame.height == 4


def test_get_klines_empty_for_missing_symbol() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR), confirmed=True)
    frame = store.get_klines(["NOPE"], start_ms=0, end_ms=10 * MS_PER_HOUR)
    assert frame.is_empty()
    # Schema must match _empty_klines so the cycle's concat works.
    assert frame.columns == list(_empty_klines_frame().columns)


def test_get_klines_returns_empty_when_window_inverted() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR), confirmed=True)
    frame = store.get_klines(["BTCUSDT"], start_ms=100 * MS_PER_HOUR, end_ms=10 * MS_PER_HOUR)
    assert frame.is_empty()


def test_get_klines_sorts_by_symbol_then_ts() -> None:
    """The cycle's concat depends on a stable (symbol, ts_ms) ordering."""
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    # Add out of order.
    store.add_bar("ETHUSDT", _ws_bar(3 * MS_PER_HOUR), confirmed=True)
    store.add_bar("BTCUSDT", _ws_bar(2 * MS_PER_HOUR), confirmed=True)
    store.add_bar("BTCUSDT", _ws_bar(1 * MS_PER_HOUR), confirmed=True)
    frame = store.get_klines(["BTCUSDT", "ETHUSDT"], start_ms=0, end_ms=10 * MS_PER_HOUR)
    symbols = frame["symbol"].to_list()
    ts = frame["ts_ms"].to_list()
    assert symbols == ["BTCUSDT", "BTCUSDT", "ETHUSDT"]
    assert ts == [MS_PER_HOUR, 2 * MS_PER_HOUR, 3 * MS_PER_HOUR]


def test_symbols_with_coverage_through_reflects_bar_freshness() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    store.add_bar("BTCUSDT", _ws_bar(5 * MS_PER_HOUR), confirmed=True)
    store.add_bar("ETHUSDT", _ws_bar(2 * MS_PER_HOUR), confirmed=True)
    covered = store.symbols_with_coverage_through(4 * MS_PER_HOUR)
    assert covered == {"BTCUSDT"}
    # Lower the threshold so both qualify.
    assert store.symbols_with_coverage_through(MS_PER_HOUR) == {"BTCUSDT", "ETHUSDT"}


def test_symbols_with_coverage_in_window_requires_both_ends() -> None:
    """The bootstrap-skip check uses this: a symbol is "already covered"
    only if its stored bars span the FULL [start_ms, end_ms] window.
    Recovery from a flush file with only the latest hour must NOT make
    the symbol look "covered" — otherwise bootstrap would skip and the
    cycle would run on partial history forever."""
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    # ABC has the full window (bars at hour 1 + hour 10).
    store.add_bar("ABCUSDT", _ws_bar(MS_PER_HOUR), confirmed=True)
    store.add_bar("ABCUSDT", _ws_bar(10 * MS_PER_HOUR), confirmed=True)
    # DEF only has the latest bar.
    store.add_bar("DEFUSDT", _ws_bar(10 * MS_PER_HOUR), confirmed=True)
    # GHI only has the oldest bar (newest end not covered).
    store.add_bar("GHIUSDT", _ws_bar(MS_PER_HOUR), confirmed=True)
    covered = store.symbols_with_coverage_in_window(
        start_ms=MS_PER_HOUR, end_ms=10 * MS_PER_HOUR,
    )
    assert covered == {"ABCUSDT"}, f"only ABC spans the window, got {covered}"


def test_bootstrap_symbol_accepts_rest_bars_bulk() -> None:
    """Bootstrap is REST-fed: caller knows the bars are confirmed."""
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    bars = [_ws_bar(i * MS_PER_HOUR, close=float(i)) for i in range(1, 6)]
    inserted = store.bootstrap_symbol("BTCUSDT", bars)
    assert inserted == 5
    assert store.row_count() == 5


def test_bootstrap_symbol_idempotent_with_overlapping_ts() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    bars = [_ws_bar(MS_PER_HOUR, close=10.0)]
    store.bootstrap_symbol("BTCUSDT", bars)
    # Re-bootstrapping the same bar must not double-insert.
    store.bootstrap_symbol("BTCUSDT", bars)
    assert store.row_count() == 1


def test_bootstrap_does_not_overwrite_live_ws_bar() -> None:
    """At cold-start the bootstrap REST backfill and the live WS stream race.
    A freshly-closed bar can arrive on WS first; the bootstrap then refetches
    that hour from REST. The WS bar must win — it reflects the latest venue
    state. Without this guard the older REST snapshot silently overwrites the
    fresh WS row."""
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    # 1. Live WS bar arrives first with close=200.0 (the "fresh" value).
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR, close=200.0), confirmed=True)
    # 2. Bootstrap REST backfill includes the same hour but with close=100.0
    # (the "stale" REST snapshot). Bootstrap must NOT overwrite.
    accepted = store.bootstrap_symbol("BTCUSDT", [_ws_bar(MS_PER_HOUR, close=100.0)])
    assert accepted == 0  # bootstrap reports 0 inserts because the slot was taken
    frame = store.get_klines(["BTCUSDT"], start_ms=0, end_ms=10 * MS_PER_HOUR)
    assert frame.height == 1
    assert frame.row(0, named=True)["close"] == pytest.approx(200.0)


def test_bootstrap_fills_gaps_around_live_ws_bar() -> None:
    """Bootstrap should still fill OTHER hours — only the hours the live WS
    stream already has are protected. This guarantees we don't lose 89 days
    of historical bars just because the live WS landed a single bar first."""
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    store.add_bar("BTCUSDT", _ws_bar(2 * MS_PER_HOUR, close=222.0), confirmed=True)
    accepted = store.bootstrap_symbol(
        "BTCUSDT",
        [
            _ws_bar(1 * MS_PER_HOUR, close=111.0),  # new gap-fill
            _ws_bar(2 * MS_PER_HOUR, close=999.0),  # would overwrite WS
            _ws_bar(3 * MS_PER_HOUR, close=333.0),  # new gap-fill
        ],
    )
    assert accepted == 2  # only the two gaps
    frame = store.get_klines(["BTCUSDT"], start_ms=0, end_ms=10 * MS_PER_HOUR)
    assert frame.height == 3
    closes = {row["ts_ms"]: row["close"] for row in frame.to_dicts()}
    assert closes[1 * MS_PER_HOUR] == pytest.approx(111.0)
    assert closes[2 * MS_PER_HOUR] == pytest.approx(222.0)  # WS preserved
    assert closes[3 * MS_PER_HOUR] == pytest.approx(333.0)


def test_concurrent_add_and_get_thread_safety() -> None:
    """Hammer the store from two threads: one inserts bars, one reads
    rectangular windows. The lock should keep the read stable and never
    crash on a partial dict mutation."""
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    base = 1_000_000_000
    n_bars = 200
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            barrier.wait()
            for i in range(n_bars):
                ts = base + i * MS_PER_HOUR
                store.add_bar("BTCUSDT", _ws_bar(ts, close=float(i)), confirmed=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            barrier.wait()
            for _ in range(n_bars):
                frame = store.get_klines(["BTCUSDT"], start_ms=0, end_ms=base + n_bars * MS_PER_HOUR)
                # Frame should always be internally consistent: no missing
                # columns, no NaN values written by a partial dict mutation.
                assert frame.columns[:2] == ["ts_ms", "symbol"]
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert not errors, f"thread-safety violation: {errors!r}"
    assert store.row_count() == n_bars


def test_flush_and_recover_round_trip(tmp_path: Path) -> None:
    """Write the store to disk, build a fresh store, recover from disk —
    every (symbol, ts_ms, close) tuple must round-trip exactly."""
    store_a = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    for hour in range(5):
        ts = hour * MS_PER_HOUR
        store_a.add_bar("BTCUSDT", _ws_bar(ts, close=float(hour)), confirmed=True)
        store_a.add_bar("ETHUSDT", _ws_bar(ts, close=float(hour + 100)), confirmed=True)
    rows_written = store_a.flush_to_disk()
    assert rows_written == 10

    store_b = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    rows_recovered = store_b.recover_from_disk()
    assert rows_recovered == 10
    frame = store_b.get_klines(["BTCUSDT", "ETHUSDT"], start_ms=0, end_ms=10 * MS_PER_HOUR)
    assert frame.height == 10
    btc = frame.filter(pl.col("symbol") == "BTCUSDT")
    assert btc["close"].to_list() == [0.0, 1.0, 2.0, 3.0, 4.0]
    eth = frame.filter(pl.col("symbol") == "ETHUSDT")
    assert eth["close"].to_list() == [100.0, 101.0, 102.0, 103.0, 104.0]


def test_flush_empty_store_removes_stale_file(tmp_path: Path) -> None:
    """A previously-flushed file must be cleared when the store empties so a
    later recovery does not pull in already-evicted bars."""
    store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR), confirmed=True)
    store.flush_to_disk()
    flush_path = tmp_path / ".cache" / "ws_klines" / "store.parquet"
    assert flush_path.exists()
    # Force the symbol out by adding a much-newer bar with retain_days=0 path
    # via a manual flush_to_disk of an empty store.
    store2 = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    rows = store2.flush_to_disk()
    assert rows == 0
    assert not flush_path.exists()


def test_recover_skips_corrupt_file(tmp_path: Path) -> None:
    flush_dir = tmp_path / ".cache" / "ws_klines"
    flush_dir.mkdir(parents=True)
    (flush_dir / "store.parquet").write_bytes(b"not a parquet file")
    store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    assert store.recover_from_disk() == 0
    assert store.row_count() == 0


def test_recover_missing_file_is_noop(tmp_path: Path) -> None:
    store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    assert store.recover_from_disk() == 0


def test_recover_evicts_bars_outside_retain_window(tmp_path: Path) -> None:
    """A long-lived flush file plus a short retain_days on recovery must not
    bring back already-stale bars."""
    base = 10 * MS_PER_DAY
    store_a = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    store_a.add_bar("BTCUSDT", _ws_bar(base - 8 * MS_PER_DAY), confirmed=True)
    store_a.add_bar("BTCUSDT", _ws_bar(base), confirmed=True)
    store_a.flush_to_disk()
    store_b = KlineStore(cache_root=tmp_path, retain_days=3, flush_interval_seconds=0.0)
    store_b.recover_from_disk()
    # Only the recent bar survives.
    rows = store_b.row_count()
    assert rows == 1


def test_flush_thread_runs_periodically(tmp_path: Path) -> None:
    """Start the flush thread, add a bar, wait long enough for two flushes,
    and verify the file appears + the flush counter advances."""
    store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.05)
    store.start_flush_thread()
    try:
        store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR), confirmed=True)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if store.stats()["flushes_total"] >= 1:
                break
            time.sleep(0.02)
        assert store.stats()["flushes_total"] >= 1
        assert (tmp_path / ".cache" / "ws_klines" / "store.parquet").exists()
    finally:
        store.stop_flush_thread()


def test_flush_thread_disabled_when_interval_zero(tmp_path: Path) -> None:
    store = KlineStore(cache_root=tmp_path, flush_interval_seconds=0.0)
    store.start_flush_thread()
    # No-op: no flush thread should be created at all.
    assert store._flush_thread is None  # type: ignore[attr-defined]


def test_flush_thread_disabled_without_cache_root() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.05)
    store.start_flush_thread()
    assert store._flush_thread is None  # type: ignore[attr-defined]
    # flush_to_disk is a no-op too.
    assert store.flush_to_disk() == 0


def test_stats_reflect_per_op_counters() -> None:
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR), confirmed=False)
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR), confirmed=True)
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR), confirmed=True)
    store.get_klines(["BTCUSDT"], start_ms=0, end_ms=10 * MS_PER_HOUR)
    stats = store.stats()
    assert stats["adds_total"] == 2
    assert stats["adds_skipped_unconfirmed"] == 1
    assert stats["reads_total"] == 1
    assert stats["symbols"] == 1
    assert stats["rows"] == 1


def test_parser_accepts_pybit_camelcase_payload() -> None:
    """Real Bybit V5 WS kline messages use camelCase ``start`` plus string
    floats; the parser must accept the live shape verbatim."""
    bar = {
        "start": 1700000000000,
        "end": 1700003600000,
        "interval": "60",
        "open": "32000.5",
        "close": "32500.0",
        "high": "32550.0",
        "low": "31980.0",
        "volume": "150.5",
        "turnover": "4837500.25",
        "confirm": True,
    }
    parsed = _parse_ws_kline_event(bar)
    assert parsed is not None
    assert parsed.ts_ms == 1700000000000
    assert parsed.close == pytest.approx(32500.0)
    assert parsed.turnover_quote == pytest.approx(4837500.25)
    assert parsed.source == WS_STORE_SOURCE


def test_parser_returns_none_for_negative_or_partial_payload() -> None:
    # Missing required field
    assert _parse_ws_kline_event({"start": 1, "open": "1"}) is None
    # Non-numeric float
    assert _parse_ws_kline_event({"start": 1, "open": "x", "high": "1", "low": "1",
                                  "close": "1", "volume": "1", "turnover": "1"}) is None


def test_bootstrap_symbol_completes_full_universe_in_a_few_seconds() -> None:
    """Regression guard for the O(N²) eviction bug that made cold-start
    bootstrap take 15+ minutes in production.

    The bug: _insert_bar called _max_ts_with_new (full scan of all symbols)
    and _evict_old_locked (full scan of all symbols × all bars) on EVERY
    inserted bar. With 567 symbols × 1083 bars/symbol, this was O(N²) under
    the store's RLock — workers competed for the lock and effective
    throughput collapsed.

    Post-fix this completes in well under a second on commodity hardware
    and ~2.5s on the 2-core ubuntu-latest CI runner. A real regression
    would take MINUTES, not seconds, so the 10s budget catches the bug
    without flaking on slow runners.

    Reduced to 250 symbols × 500 bars (= 125k row total) — keeps the test
    fast enough on CI while still exercising the same O(N²) path that
    blew up under the prior implementation.
    """
    store = KlineStore(cache_root=None, retain_days=90, flush_interval_seconds=0.0)
    base_ts = 100 * MS_PER_DAY
    n_symbols = 250
    n_bars = 500
    bars = [_ws_bar(base_ts + i * MS_PER_HOUR) for i in range(n_bars)]
    start = time.monotonic()
    for i in range(n_symbols):
        store.bootstrap_symbol(f"SYM{i:04d}USDT", bars)
    elapsed = time.monotonic() - start
    assert elapsed < 10.0, (
        f"bootstrap of {n_symbols} × {n_bars} bars took {elapsed:.2f}s — "
        f"eviction may have regressed to O(N²) (was 15+ minutes in production before the fix)"
    )
    assert store.row_count() == n_symbols * n_bars


def test_keep_only_symbols_drops_everything_outside_the_set() -> None:
    """Used at manager startup to trim legacy bars when the universe
    scope shrinks between runs (e.g. long sleeve recovered with 567
    symbols' worth of data from a prior wider-universe daemon but now
    only tracks the top-50)."""
    store = KlineStore(cache_root=None, retain_days=90, flush_interval_seconds=0.0)
    for sym in ("BTCUSDT", "ETHUSDT", "DOGEUSDT", "SHIBUSDT"):
        store.add_bar(sym, _ws_bar(MS_PER_HOUR), confirmed=True)
    assert store.symbol_count() == 4

    dropped = store.keep_only_symbols(["BTCUSDT", "ETHUSDT"])
    assert dropped == 2  # DOGE + SHIB had 1 bar each
    assert store.symbol_count() == 2
    assert {row["symbol"] for row in store.get_klines(
        ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "SHIBUSDT"],
        start_ms=0, end_ms=10 * MS_PER_HOUR,
    ).to_dicts()} == {"BTCUSDT", "ETHUSDT"}


def test_keep_only_symbols_is_a_noop_when_universe_unchanged() -> None:
    store = KlineStore(cache_root=None, retain_days=90, flush_interval_seconds=0.0)
    store.add_bar("BTCUSDT", _ws_bar(MS_PER_HOUR), confirmed=True)
    dropped = store.keep_only_symbols(["BTCUSDT", "ETHUSDT"])
    assert dropped == 0
    assert store.symbol_count() == 1


def test_amortized_eviction_does_not_skip_long_overdue_purges() -> None:
    """Eviction is amortized to fire once per hour-window, but bars that
    have been stale for many hours still get purged the next time it fires.
    A single very-old bar plus a much-later insert must still trigger
    eviction of the old bar."""
    store = KlineStore(cache_root=None, retain_days=1, flush_interval_seconds=0.0)
    # Old bar at t=0, within retention as the only entry.
    store.add_bar("OLD", _ws_bar(0, close=50.0), confirmed=True)
    assert "OLD" in store.symbols_with_coverage_through(0)
    # New bar 5 days later — eviction window threshold (1h) is crossed
    # easily and the old bar (5d > retain_days=1) must be purged.
    store.add_bar("NEW", _ws_bar(5 * MS_PER_DAY, close=100.0), confirmed=True)
    assert "OLD" not in store.symbols_with_coverage_through(0)
    assert "NEW" in store.symbols_with_coverage_through(5 * MS_PER_DAY)
