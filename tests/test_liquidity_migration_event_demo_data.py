"""Event-demo data tests — split from the monolithic test_liquidity_migration_event_demo.py."""

from __future__ import annotations

import shutil
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.config import ResearchConfig
from liquidity_migration.event_demo import (
    EventDemoCycleConfig,
    _build_demo_features,
    _build_demo_universe,
    _collect_private_snapshots,
    _demo_feature_cache_fingerprint,
    _demo_feature_cache_paths,
    _demo_instruments,
    _demo_kline_fetch_ranges,
    _download_recent_1h_klines,
    _refresh_positions_and_orders,
    warm_demo_kline_cache,
)
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration._common import MS_PER_HOUR

from _event_demo_fixtures import *  # noqa: F401,F403  (shared fakes/helpers)
from _event_demo_fixtures import (  # noqa: F401  explicit for the linters
    FailingKlineMarket,
    FakeKlineMarket,
    FakeRiskClient,
    MinimalEventMarket,
    _ClosedPnlClient,
    _RecordingInstrumentsMarket,
    _feature_cache_klines,
    _feature_cache_universe,
    _make_instruments_frame,
    _make_tickers_frame,
    _open_trade_row,
    _patch_minimal_event_cycle,
)


def test_demo_kline_cache_avoids_refetching_complete_window(tmp_path: Path) -> None:
    market = FakeKlineMarket()

    first, first_stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
    )

    assert first.height == 6
    assert first_stats["fetch_symbols"] == 2
    assert first_stats["fetched_rows"] == 6
    assert market.calls == [
        ("AAAUSDT", "60", 0, 2 * MS_PER_HOUR),
        ("BBBUSDT", "60", 0, 2 * MS_PER_HOUR),
    ]
    cached = read_dataset(tmp_path, "event_demo_klines_1h")
    assert cached.height == 6
    assert read_dataset(tmp_path, "klines_1h").is_empty()

    second, second_stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),
        cache_root=tmp_path,
    )

    assert second.height == 6
    assert second_stats["cache_rows"] == 6
    assert second_stats["cache_symbols"] == 2
    assert second_stats["fetch_symbols"] == 0
    assert second_stats["fetched_rows"] == 0


def test_demo_kline_cache_fetches_only_new_hour(tmp_path: Path) -> None:
    market = FakeKlineMarket()

    initial, _ = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=0,
        end_ms=MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
    )
    assert initial.height == 2

    market.calls.clear()
    updated, stats = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
    )

    assert market.calls == [("AAAUSDT", "60", 2 * MS_PER_HOUR, 2 * MS_PER_HOUR)]
    assert updated.height == 3
    assert stats["cache_rows"] == 2
    assert stats["fetch_symbols"] == 1
    assert stats["fetched_rows"] == 1
    assert read_dataset(tmp_path, "event_demo_klines_1h").height == 3


def test_demo_kline_fetch_ranges_uses_latest_bar_per_symbol() -> None:
    cached = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "ts_ms": 0},
            {"symbol": "AAAUSDT", "ts_ms": 2 * MS_PER_HOUR},
            {"symbol": "BBBUSDT", "ts_ms": 0},
            {"symbol": "CCCUSDT", "ts_ms": 3 * MS_PER_HOUR},
        ]
    )

    ranges = _demo_kline_fetch_ranges(
        ["AAAUSDT", "BBBUSDT", "DDDUSDT"],
        cached,
        start_ms=0,
        end_ms=3 * MS_PER_HOUR,
    )

    assert ranges == {
        "AAAUSDT": (3 * MS_PER_HOUR, 3 * MS_PER_HOUR),
        "BBBUSDT": (MS_PER_HOUR, 3 * MS_PER_HOUR),
        "DDDUSDT": (0, 3 * MS_PER_HOUR),
    }


def test_demo_kline_compact_cache_serves_repeat_window(tmp_path: Path) -> None:
    cached_rows = []
    for symbol in ("AAAUSDT", "BBBUSDT"):
        for ts_ms in (0, MS_PER_HOUR, 2 * MS_PER_HOUR):
            cached_rows.append(
                {
                    "symbol": symbol,
                    "ts_ms": ts_ms,
                    "open": 100.0,
                    "high": 110.0,
                    "low": 90.0,
                    "close": 105.0,
                    "volume": 1.5,
                    "turnover": 157.5,
                }
            )
    write_dataset(pl.DataFrame(cached_rows), tmp_path, "event_demo_klines_1h")

    first, first_stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),
        cache_root=tmp_path,
    )
    shutil.rmtree(tmp_path / "event_demo_klines_1h")

    second, second_stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),
        cache_root=tmp_path,
    )

    assert first.height == 6
    assert first_stats["fetch_symbols"] == 0
    assert second.height == 6
    assert second_stats["cache_rows"] == 6
    assert second_stats["fetch_symbols"] == 0


def test_download_recent_1h_klines_uses_store_fast_path(tmp_path: Path) -> None:
    """With a fully-covering kline_store, REST is never called and the output
    is sourced entirely from the store."""
    from liquidity_migration.kline_store import KlineStore

    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    for hour in range(3):
        ts = hour * MS_PER_HOUR
        for symbol in ("AAAUSDT", "BBBUSDT"):
            store.add_bar(
                symbol,
                {
                    "start": ts,
                    "open": "100", "high": "110", "low": "90", "close": "105",
                    "volume": "1.5", "turnover": "157.5",
                },
                confirmed=True,
            )

    output, stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),  # REST must NOT be called
        cache_root=tmp_path,
        kline_store=store,
    )
    assert output.height == 6
    assert stats["store_rows"] == 6
    assert stats["store_symbols"] == 2
    assert stats["fetch_symbols"] == 0
    assert stats["fetched_rows"] == 0


def test_download_recent_1h_klines_store_full_coverage_skips_disk_cache(tmp_path: Path) -> None:
    """When the WS store fully covers the universe at end_ms, the cycle
    must skip the on-disk parquet cache read entirely. Reading the full
    dataset costs 5-10s on a populated cache; the store serves the same
    in <50ms. Asserted by writing a SENTINEL row to the disk cache that
    would corrupt the output if read — the fast path must skip it."""
    from liquidity_migration.kline_store import KlineStore
    from liquidity_migration.storage import write_dataset

    # Disk cache holds a sentinel row that would surface if read.
    sentinel = pl.DataFrame([{
        "symbol": "AAAUSDT", "ts_ms": 999 * MS_PER_HOUR,
        "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0,
        "volume_base": 0.0, "turnover_quote": 0.0, "source": "DISK_SENTINEL",
    }])
    write_dataset(sentinel, tmp_path, "event_demo_klines_1h")

    # Store has the FULL universe covered at end_ms.
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    for hour in range(3):
        ts = hour * MS_PER_HOUR
        for symbol in ("AAAUSDT", "BBBUSDT"):
            store.add_bar(
                symbol,
                {"start": ts, "open": "100", "high": "110", "low": "90",
                 "close": "105", "volume": "1", "turnover": "1"},
                confirmed=True,
            )

    output, stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),
        cache_root=tmp_path,
        kline_store=store,
    )
    assert output.height == 6
    # Disk cache stat shows 0 — we didn't read it.
    assert stats["cache_rows"] == 0
    assert stats["cache_symbols"] == 0
    assert stats["store_rows"] == 6
    # Sentinel never made it into the output.
    assert "DISK_SENTINEL" not in output["source"].to_list()


def test_download_recent_1h_klines_falls_back_to_rest_for_uncovered_symbols(tmp_path: Path) -> None:
    """Hybrid path: store covers one symbol, REST fills the other."""
    from liquidity_migration.kline_store import KlineStore

    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    for hour in range(3):
        store.add_bar(
            "AAAUSDT",
            {
                "start": hour * MS_PER_HOUR,
                "open": "1", "high": "1", "low": "1", "close": "1",
                "volume": "1", "turnover": "1",
            },
            confirmed=True,
        )

    market = FakeKlineMarket()
    output, stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
        kline_store=store,
    )
    # BBBUSDT only — AAAUSDT was served from the store.
    fetched_symbols = sorted({call[0] for call in market.calls})
    assert fetched_symbols == ["BBBUSDT"]
    # Output has bars for both symbols.
    assert output.height == 6
    assert sorted(output["symbol"].unique().to_list()) == ["AAAUSDT", "BBBUSDT"]
    assert stats["store_rows"] == 3
    assert stats["store_symbols"] == 1
    assert stats["fetch_symbols"] == 1
    assert stats["fetched_rows"] >= 3


def test_download_recent_1h_klines_ignores_store_failure_gracefully(tmp_path: Path) -> None:
    """A broken kline_store must never break the cycle — REST takes over."""

    class _BrokenStore:
        def symbols_with_coverage_through(self, ts_ms):
            raise RuntimeError("store offline")

        def get_klines(self, symbols, *, start_ms, end_ms):  # pragma: no cover
            raise AssertionError("should not be called after coverage failure")

    market = FakeKlineMarket()
    output, stats = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=0,
        end_ms=MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
        kline_store=_BrokenStore(),
    )
    assert output.height >= 1
    assert stats["fetched_rows"] >= 1


def test_download_recent_1h_klines_without_store_keeps_legacy_behavior(tmp_path: Path) -> None:
    """Pre-existing call site (no kline_store) must behave identically to
    before: cache + REST path, no new stats blow-up."""
    market = FakeKlineMarket()
    output, stats = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=0,
        end_ms=MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
    )
    assert output.height == 2
    assert stats["fetched_rows"] == 2
    # Store-related stat keys are present but zero when no store is wired.
    assert stats["store_rows"] == 0
    assert stats["store_symbols"] == 0


def test_resolve_ticker_snapshot_prefers_fresh_cache() -> None:
    """When the ticker cache is seeded + fresh, _resolve_ticker_snapshot
    returns the cache snapshot and never touches REST."""
    from liquidity_migration.event_demo import _resolve_ticker_snapshot
    from liquidity_migration.ws_state_cache import TickerCache

    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])

    class _FailingPublic:
        def get_tickers(self):
            raise AssertionError("REST must not be called when cache is fresh")

    rows, source = _resolve_ticker_snapshot(
        _FailingPublic(), ticker_cache=cache, state_cache_stale_seconds=60.0,
    )
    assert source == "ws_cache"
    assert rows[0]["symbol"] == "BTCUSDT"


def test_resolve_ticker_snapshot_falls_back_to_rest_when_unseeded() -> None:
    from liquidity_migration.event_demo import _resolve_ticker_snapshot
    from liquidity_migration.ws_state_cache import TickerCache

    cache = TickerCache()  # never seeded

    class _RestPublic:
        def get_tickers(self):
            return [{"symbol": "RESTUSDT", "lastPrice": "1"}]

    rows, source = _resolve_ticker_snapshot(
        _RestPublic(), ticker_cache=cache, state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert rows[0]["symbol"] == "RESTUSDT"


def test_resolve_ticker_snapshot_falls_back_when_cache_stale() -> None:
    """An old seed (stale) must trigger REST fallback even if the cache has
    rows. Critical for safety: trading on a stale price snapshot is worse
    than waiting one REST roundtrip."""
    import time as _time
    from liquidity_migration.event_demo import _resolve_ticker_snapshot
    from liquidity_migration.ws_state_cache import TickerCache

    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])
    # Force last_event timestamp to be ancient.
    cache._stats.last_event_monotonic = _time.monotonic() - 1000.0

    class _RestPublic:
        def get_tickers(self):
            return [{"symbol": "FRESHUSDT", "lastPrice": "1"}]

    rows, source = _resolve_ticker_snapshot(
        _RestPublic(), ticker_cache=cache, state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert rows[0]["symbol"] == "FRESHUSDT"


def test_resolve_ticker_snapshot_with_no_cache_uses_rest() -> None:
    from liquidity_migration.event_demo import _resolve_ticker_snapshot

    class _RestPublic:
        def get_tickers(self):
            return [{"symbol": "X", "lastPrice": "1"}]

    rows, source = _resolve_ticker_snapshot(
        _RestPublic(), ticker_cache=None, state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert rows[0]["symbol"] == "X"


def test_resolve_private_snapshot_prefers_fresh_cache() -> None:
    from liquidity_migration.event_demo import EventDemoCycleConfig, _resolve_private_snapshot
    from liquidity_migration.ws_state_cache import PrivateStateCache

    cache = PrivateStateCache()
    cache.seed(
        equity_usdt=12_500.0,
        positions=[{"symbol": "BTCUSDT", "size": "1.0"}],
        open_orders=[],
    )

    class _FailingClient:
        def get_positions(self, **kwargs):
            raise AssertionError("REST must not be called when cache is fresh")

        def get_open_orders(self, **kwargs):
            raise AssertionError("REST must not be called when cache is fresh")

        def get_wallet_balance(self, **kwargs):
            raise AssertionError("REST must not be called when cache is fresh")

    snap, source = _resolve_private_snapshot(
        _FailingClient(),
        EventDemoCycleConfig(),
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )
    assert source == "ws_cache"
    assert snap["equity_usdt"] == 12_500.0
    assert snap["raw_positions"][0]["symbol"] == "BTCUSDT"
    assert snap["raw_open_orders"] == []


def test_resolve_private_snapshot_falls_back_to_rest_when_cache_stale() -> None:
    import time as _time
    from liquidity_migration.event_demo import EventDemoCycleConfig, _resolve_private_snapshot
    from liquidity_migration.ws_state_cache import PrivateStateCache

    cache = PrivateStateCache()
    cache.seed(equity_usdt=10_000.0)
    cache._stats.last_event_monotonic = _time.monotonic() - 1000.0

    # trading_client=None hits the neutral REST snapshot path.
    snap, source = _resolve_private_snapshot(
        None,
        EventDemoCycleConfig(fallback_equity_usdt=5_000.0),
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    # REST neutral snapshot returns the fallback equity, not the cached 10_000.
    assert snap["equity_usdt"] == 5_000.0


def test_resolve_private_snapshot_falls_back_to_rest_when_cache_unseeded() -> None:
    from liquidity_migration.event_demo import EventDemoCycleConfig, _resolve_private_snapshot
    from liquidity_migration.ws_state_cache import PrivateStateCache

    cache = PrivateStateCache()
    snap, source = _resolve_private_snapshot(
        None,
        EventDemoCycleConfig(fallback_equity_usdt=5_000.0),
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert snap["equity_usdt"] == 5_000.0


def test_build_demo_features_cache_returns_identical_frame_on_hit(tmp_path: Path) -> None:
    """The feature build is a pure function of (klines, universe). With a
    cache_root, an unchanged input must serve a parquet cache hit identical to
    a fresh recompute — this is what lets 59 of every 60 demo cycles skip the
    whole feature pipeline."""
    klines = _feature_cache_klines()
    universe = _feature_cache_universe()

    fresh = _build_demo_features(klines, universe)
    cold = _build_demo_features(klines, universe, cache_root=tmp_path)  # miss -> compute + write
    parquet_path, metadata_path = _demo_feature_cache_paths(tmp_path)
    assert parquet_path.exists() and metadata_path.exists()

    warm = _build_demo_features(klines, universe, cache_root=tmp_path)  # hit -> parquet read
    assert not fresh.is_empty()
    assert warm.equals(fresh)
    assert cold.equals(fresh)


def test_build_demo_features_cache_misses_when_a_bar_is_appended(tmp_path: Path) -> None:
    """A new closed bar must change the fingerprint so the cache recomputes —
    a stale feature frame would silently freeze the entry signal."""
    klines = _feature_cache_klines()
    universe = _feature_cache_universe()
    _build_demo_features(klines, universe, cache_root=tmp_path)

    next_bar = klines.filter(pl.col("symbol") == "SYM00USDT").tail(1).with_columns(
        pl.col("ts_ms") + MS_PER_HOUR
    )
    grown = pl.concat([klines, next_bar])
    assert _demo_feature_cache_fingerprint(grown, universe) != _demo_feature_cache_fingerprint(klines, universe)

    recomputed = _build_demo_features(grown, universe, cache_root=tmp_path)
    assert recomputed.equals(_build_demo_features(grown, universe))


def test_build_demo_features_cache_survives_subday_age_drift(tmp_path: Path) -> None:
    """listing_age_days creeps up every cycle — it is (now - launch_time)/day.
    The cache fingerprint must key on whole-day ages, so an otherwise-unchanged
    universe still hits across cycles. Without this the feature cache misses
    100% of the time in production (the bug live telemetry caught)."""
    klines = _feature_cache_klines()
    universe = _feature_cache_universe()  # whole-number listing_age_days
    drifted = universe.with_columns(
        (pl.col("listing_age_days").cast(pl.Float64) + 0.37).alias("listing_age_days")
    )
    assert _demo_feature_cache_fingerprint(klines, universe) == _demo_feature_cache_fingerprint(klines, drifted)

    fresh = _build_demo_features(klines, universe)
    _build_demo_features(klines, universe, cache_root=tmp_path)  # miss -> compute + write
    parquet_path, _ = _demo_feature_cache_paths(tmp_path)
    written_at = parquet_path.stat().st_mtime_ns

    warm = _build_demo_features(klines, drifted, cache_root=tmp_path)  # must HIT despite drift
    assert parquet_path.stat().st_mtime_ns == written_at, "cache rewritten — fingerprint missed on sub-day drift"
    assert warm.equals(fresh)


def test_build_demo_features_without_cache_root_writes_nothing(tmp_path: Path) -> None:
    """cache_root=None (the default, used by tests and any non-cycle caller)
    must never touch disk."""
    klines = _feature_cache_klines()
    universe = _feature_cache_universe()
    _build_demo_features(klines, universe)
    assert not (tmp_path / ".cache" / "event_demo_features").exists()


def test_event_demo_cycles_dataset_is_date_partitioned(tmp_path: Path) -> None:
    """event_demo_cycles is append-only telemetry written every cycle. It must
    be date-partitioned so the per-cycle write stays bounded to the current
    day's rows instead of read+rewriting the whole (unbounded) dataset — and it
    must still round-trip cleanly through read_dataset for the tribunal."""
    day_ms = 24 * 60 * 60 * 1000
    day1 = 1_700_000_000_000
    day2 = day1 + day_ms
    rows = [
        {"cycle_id": "c1", "ts_ms": day1, "mode": "submit"},
        {"cycle_id": "c2", "ts_ms": day1 + 60_000, "mode": "submit"},
        {"cycle_id": "c3", "ts_ms": day2, "mode": "submit"},
    ]
    for row in rows:
        write_dataset(pl.DataFrame([row]), tmp_path, "event_demo_cycles", partition_by=("date",))

    date_parts = sorted(p.name for p in (tmp_path / "event_demo_cycles").glob("date=*"))
    assert len(date_parts) == 2, f"expected one partition per day, got {date_parts}"

    loaded = read_dataset(tmp_path, "event_demo_cycles")
    assert sorted(loaded["cycle_id"].to_list()) == ["c1", "c2", "c3"]


def test_demo_instruments_cache_serves_within_ttl(tmp_path: Path) -> None:
    """get_instruments_info is a large REST call but contract specs change ~daily.
    A second cycle inside the TTL must serve the cached frame, not refetch."""
    market = _RecordingInstrumentsMarket()
    now = 1_700_000_000_000
    first = _demo_instruments(market, cache_root=tmp_path, now_ms=now)
    assert market.instrument_calls == 1
    assert first["symbol"].to_list() == ["AAAUSDT", "BBBUSDT"]

    second = _demo_instruments(market, cache_root=tmp_path, now_ms=now + 59 * 60 * 1000)
    assert market.instrument_calls == 1, "within-TTL cycle must not refetch instruments"
    assert second.equals(first)


def test_demo_instruments_cache_refetches_after_ttl(tmp_path: Path) -> None:
    market = _RecordingInstrumentsMarket()
    now = 1_700_000_000_000
    _demo_instruments(market, cache_root=tmp_path, now_ms=now)
    _demo_instruments(market, cache_root=tmp_path, now_ms=now + 61 * 60 * 1000)
    assert market.instrument_calls == 2, "a cycle past the TTL must refetch instruments"


def test_demo_instruments_falls_back_to_stale_cache_on_fetch_error(tmp_path: Path) -> None:
    """A transient instruments-endpoint outage must not fail the whole cycle —
    contract specs barely change, so a stale cache is safe to reuse."""
    market = _RecordingInstrumentsMarket()
    now = 1_700_000_000_000
    cached = _demo_instruments(market, cache_root=tmp_path, now_ms=now)

    class _BrokenInstrumentsMarket:
        def get_instruments_info(self) -> list[dict[str, str]]:
            raise RuntimeError("bybit instruments endpoint down")

    served = _demo_instruments(_BrokenInstrumentsMarket(), cache_root=tmp_path, now_ms=now + 2 * 60 * 60 * 1000)
    assert served.equals(cached)


def test_warm_demo_kline_cache_populates_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """warm_demo_kline_cache pre-fetches the universe's 1h klines into the same
    event_demo_klines_1h cache a cycle reads — so the post-bar-close cycle finds
    the cache warm and skips the per-symbol REST burst."""
    monkeypatch.setattr(
        "liquidity_migration.event_demo._build_demo_universe",
        lambda *args, **kwargs: pl.DataFrame({"symbol": ["AAAUSDT", "BBBUSDT"]}),
    )

    class _WarmMarket:
        def get_instruments_info(self) -> list[dict[str, str]]:
            return [{"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"}]

        def get_tickers(self) -> list[dict[str, str]]:
            return [{"symbol": "AAAUSDT", "markPrice": "100", "lastPrice": "100"}]

        def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list[list[str]]:
            return [
                [str(ts_ms), "100", "110", "90", "105", "1.5", "157.5"]
                for ts_ms in range(start, end + 1, MS_PER_HOUR)
            ]

    stats = warm_demo_kline_cache(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(lookback_days=1, workers=1),
        market_client=_WarmMarket(),
        now_ms=100 * MS_PER_HOUR,
    )
    assert stats["symbols"] == 2
    cached = read_dataset(tmp_path, "event_demo_klines_1h")
    assert not cached.is_empty()
    assert set(cached["symbol"].to_list()) == {"AAAUSDT", "BBBUSDT"}


def test_warm_demo_kline_cache_handles_empty_universe(tmp_path: Path) -> None:
    """An empty universe (no tradable symbols) must yield a zero-stats no-op,
    not an error — the warmer runs unattended on a background thread."""
    stats = warm_demo_kline_cache(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(),
        market_client=MinimalEventMarket(),
        now_ms=100 * MS_PER_HOUR,
    )
    assert stats == {"symbols": 0, "fetch_symbols": 0, "fetched_rows": 0, "cache_rows": 0}


def test_collect_private_snapshots_neutral_without_client() -> None:
    """With no trading client the snapshot must be the same neutral result the
    old serial path produced: fallback equity, empty orders/positions, no errors."""
    snapshot = _collect_private_snapshots(None, EventDemoCycleConfig(fallback_equity_usdt=12_345.0))
    assert snapshot["equity_usdt"] == 12_345.0
    assert snapshot["raw_open_orders"] == []
    assert snapshot["raw_positions"] == []
    assert snapshot["wallet_error"] == ""
    assert snapshot["open_order_error"] == ""
    assert snapshot["position_error"] == ""


def test_collect_private_snapshots_gathers_all_three_from_client() -> None:
    """The concurrent fan-out must still return each endpoint's data correctly."""

    class _FakeClient:
        def get_wallet_balance(self, **_kwargs: object) -> dict[str, object]:
            return {"list": [{"totalEquity": "8000", "coin": [{"coin": "USDT", "equity": "8000"}]}]}

        def get_open_orders(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"symbol": "AAAUSDT", "orderLinkId": "lm-en-1"}]

        def get_positions(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"symbol": "BBBUSDT", "size": "3"}]

    snapshot = _collect_private_snapshots(_FakeClient(), EventDemoCycleConfig())
    assert snapshot["equity_usdt"] == 8000.0
    assert snapshot["raw_open_orders"] == [{"symbol": "AAAUSDT", "orderLinkId": "lm-en-1"}]
    assert snapshot["raw_positions"] == [{"symbol": "BBBUSDT", "size": "3"}]
    assert snapshot["wallet_error"] == ""


def test_refresh_positions_and_orders_returns_both_results() -> None:
    """The post-trade refetch runs positions + open orders concurrently; with no
    client both come back as the neutral empty result."""
    (positions, position_error), (orders, open_order_error) = _refresh_positions_and_orders(
        None, settle_coin="USDT"
    )
    assert positions == [] and position_error == ""
    assert orders == [] and open_order_error == ""


def test_build_demo_universe_match_backtest_mode_includes_all_trading_perps() -> None:
    """With universe_rank_end == universe_max_symbols == 0 the demo's
    universe is every Trading USDT-perp (ex the hard exclusion list).
    No turnover floor, no rank cap, no 30-day age filter — symbols are
    only filtered out via the strategy's own rank/turnover/age gates
    downstream (matching the backtest's path).
    """
    snapshot_ts_ms = 1_779_440_000_000  # 2026-05-22-ish, past NEWUSDT's launch
    demo_config = EventDemoCycleConfig(
        universe_rank_end=0,
        universe_max_symbols=0,
        universe_min_turnover_24h=0.0,
    )
    universe = _build_demo_universe(
        _make_instruments_frame(),
        _make_tickers_frame(),
        config=demo_config,
        snapshot_ts_ms=snapshot_ts_ms,
    )
    symbols = set(universe["symbol"].to_list())
    # BTC, BAN, NEW all included. BUSDUSDT is on the hard-exclude list.
    assert "BTCUSDT" in symbols
    assert "BANUSDT" in symbols
    assert "NEWUSDT" in symbols, "NEWUSDT (5 days old) must be included in match-the-backtest mode"
    assert "BUSDUSDT" not in symbols


def test_build_demo_universe_legacy_mode_applies_30_day_age_floor() -> None:
    """Narrow-universe demo (universe_rank_end > 0) keeps the 30-day age
    safety floor that pre-dates the match-the-backtest unification —
    documents the behavior delta so operators downgrading to legacy
    mode know what they get."""
    snapshot_ts_ms = 1_779_440_000_000  # NEWUSDT is only ~5 days old here
    demo_config = EventDemoCycleConfig(
        universe_rank_end=400,
        universe_max_symbols=400,
        universe_min_turnover_24h=0.0,
    )
    universe = _build_demo_universe(
        _make_instruments_frame(),
        _make_tickers_frame(),
        config=demo_config,
        snapshot_ts_ms=snapshot_ts_ms,
    )
    symbols = set(universe["symbol"].to_list())
    assert "BTCUSDT" in symbols
    assert "BANUSDT" in symbols  # ~500 days old
    assert "NEWUSDT" not in symbols, "Legacy narrow-universe mode keeps the 30-day age floor"

