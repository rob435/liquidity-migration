"""Event-demo data tests — split from the monolithic test_liquidity_migration_event_demo.py."""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from liquidity_migration import event_demo_data
from liquidity_migration.config import ResearchConfig
from liquidity_migration.event_demo_data import (
    _build_demo_universe,
    _demo_instruments,
    _demo_kline_fetch_ranges,
    _download_recent_1h_klines,
    _resolve_ticker_snapshot,
)
from liquidity_migration.event_demo_data import _prune_event_demo_kline_cache
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR

from _event_demo_fixtures import (
    FailingKlineMarket,
    FakeKlineMarket,
    _RecordingInstrumentsMarket,
    _make_instruments_frame,
    _make_tickers_frame,
)


def _public_config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "universe_rank_end": 0,
        "universe_max_symbols": 0,
        "universe_min_turnover_24h": 0.0,
        "lookback_days": 45,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


def test_demo_kline_fetch_ranges_tails_contiguous_and_backfills_holes() -> None:
    # AAAUSDT has a MID-WINDOW HOLE (bars at 0 and 2h, missing 1h): the fetch
    # range must cover the full window to backfill the hole, NOT just the tail
    # after the latest bar (the old latest-bar-only behaviour left the hole
    # forever and the day failed the >=20-bar filter — BUG-2).
    # BBBUSDT is merely BEHIND (contiguous, latest at 0): a tail fetch suffices.
    # DDDUSDT is absent: full window.
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
        "AAAUSDT": (0, 3 * MS_PER_HOUR),  # hole at 1h -> full-window backfill
        "BBBUSDT": (MS_PER_HOUR, 3 * MS_PER_HOUR),  # contiguous, just behind -> tail
        "DDDUSDT": (0, 3 * MS_PER_HOUR),
    }
    # A fully-contiguous symbol that already reaches end_ms gets no fetch range.
    contiguous = pl.DataFrame(
        [{"symbol": "EEEUSDT", "ts_ms": h * MS_PER_HOUR} for h in range(4)]
    )
    assert _demo_kline_fetch_ranges(["EEEUSDT"], contiguous, start_ms=0, end_ms=3 * MS_PER_HOUR) == {}


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


def test_demo_kline_compact_cache_opt_out_preserves_existing_window(tmp_path: Path) -> None:
    """Gate-only side loads must not replace the main universe compact cache."""
    market = FakeKlineMarket()
    first, _ = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
    )
    assert first.height == 6

    _btc, btc_stats = _download_recent_1h_klines(
        ["BTCUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
        write_compact_cache=False,
    )
    assert btc_stats["fetched_rows"] == 3

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

    assert sorted(second["symbol"].unique().to_list()) == ["AAAUSDT", "BBBUSDT"]
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


def test_download_recent_1h_klines_backfills_store_midwindow_hole(tmp_path: Path) -> None:
    """A store symbol that reaches end_ms but has a MID-WINDOW hole must be forced
    off the fast path and REST-backfilled, not trusted as covered (BUG-2)."""
    from liquidity_migration.kline_store import KlineStore

    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    # AAAUSDT: bars at 0 and 2h (HOLE at 1h). max==2h==end_ms so the latest-bar
    # coverage check would wrongly call it covered.
    for hour in (0, 2):
        store.add_bar(
            "AAAUSDT",
            {"start": hour * MS_PER_HOUR, "open": "1", "high": "1", "low": "1",
             "close": "1", "volume": "1", "turnover": "1"},
            confirmed=True,
        )

    market = FakeKlineMarket()
    output, _stats = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
        kline_store=store,
    )
    # The hole forced a REST fetch (the fast path would have skipped REST).
    assert "AAAUSDT" in {call[0] for call in market.calls}
    # The previously-missing 1h bar is now present in the merged output.
    aaa_ts = set(output.filter(pl.col("symbol") == "AAAUSDT")["ts_ms"].to_list())
    assert {0, MS_PER_HOUR, 2 * MS_PER_HOUR} <= aaa_ts


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
    class _RestPublic:
        def get_tickers(self):
            return [{"symbol": "X", "lastPrice": "1"}]

    rows, source = _resolve_ticker_snapshot(
        _RestPublic(), ticker_cache=None, state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert rows[0]["symbol"] == "X"


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


def test_build_demo_universe_match_backtest_mode_includes_all_trading_perps() -> None:
    """With universe_rank_end == universe_max_symbols == 0 the demo's
    universe is every Trading USDT-perp (ex the hard exclusion list).
    No turnover floor, no rank cap, no 30-day age filter — symbols are
    only filtered out via the strategy's own rank/turnover/age gates
    downstream (matching the backtest's path).
    """
    snapshot_ts_ms = 1_779_440_000_000  # 2026-05-22-ish, past NEWUSDT's launch
    demo_config = _public_config(
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
    demo_config = _public_config(
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



# ---------------------------------------------------------------------------
# event_demo_klines_1h REST-cache prune (date= partitions accumulated forever:
# live box hit 49 partitions vs a 45-day lookback, ~6.8MB/day/root).
# ---------------------------------------------------------------------------

_PRUNE_BASE = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_PRUNE_NOW_MS = int(_PRUNE_BASE.timestamp() * 1000)


def _date_partition_name(days_ago: int) -> str:
    return f"date={(_PRUNE_BASE.date() - timedelta(days=days_ago)).isoformat()}"


def _make_partition(dataset_dir: Path, name: str) -> Path:
    part = dataset_dir / name / "symbol=AAAUSDT"
    part.mkdir(parents=True)
    # a VALID parquet part: the wiring test reads the dataset through the
    # normal cache path before the prune runs
    pl.DataFrame(
        {
            "symbol": ["AAAUSDT"],
            "ts_ms": [0],
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "volume": [1.5],
            "turnover": [157.5],
        }
    ).write_parquet(part / "part.parquet")
    return dataset_dir / name


def test_prune_kline_cache_deletes_only_stale_date_partitions(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "event_demo_klines_1h"
    # lookback 45 + safety margin 3 -> cutoff = base - 48d; older is deleted.
    stale_60 = _make_partition(dataset_dir, _date_partition_name(60))
    stale_49 = _make_partition(dataset_dir, _date_partition_name(49))
    at_cutoff = _make_partition(dataset_dir, _date_partition_name(48))
    recent = _make_partition(dataset_dir, _date_partition_name(10))
    unparseable = _make_partition(dataset_dir, "date=not-a-date")
    non_partition = _make_partition(dataset_dir, "symbol=ZZZUSDT")
    # a stale-dated plain FILE is not a partition dir and must be left alone
    stale_file = dataset_dir / _date_partition_name(65)
    stale_file.write_text("not a directory", encoding="utf-8")
    # a stale-dated SYMLINK must never be followed or deleted
    link_target = tmp_path / "outside"
    link_target.mkdir()
    (link_target / "keep.txt").write_text("precious", encoding="utf-8")
    stale_link = dataset_dir / _date_partition_name(70)
    stale_link.symlink_to(link_target, target_is_directory=True)

    deleted = _prune_event_demo_kline_cache(tmp_path, lookback_days=45, now_ms=_PRUNE_NOW_MS)

    assert sorted(deleted) == sorted([_date_partition_name(60), _date_partition_name(49)])
    assert not stale_60.exists() and not stale_49.exists()
    assert at_cutoff.exists()
    assert recent.exists()
    assert unparseable.exists()
    assert non_partition.exists()
    assert stale_file.exists()
    assert stale_link.is_symlink()
    assert (link_target / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_prune_kline_cache_skipped_until_utc_date_changes(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "event_demo_klines_1h"
    first_stale = _make_partition(dataset_dir, _date_partition_name(60))

    assert _prune_event_demo_kline_cache(tmp_path, lookback_days=45, now_ms=_PRUNE_NOW_MS) == [
        _date_partition_name(60)
    ]
    assert not first_stale.exists()

    # Same UTC date: the scan is skipped entirely — a freshly created stale
    # partition survives and nothing is reported deleted.
    second_stale = _make_partition(dataset_dir, _date_partition_name(59))
    assert _prune_event_demo_kline_cache(tmp_path, lookback_days=45, now_ms=_PRUNE_NOW_MS) == []
    assert second_stale.exists()

    # Date roll: the prune runs again.
    assert _prune_event_demo_kline_cache(
        tmp_path, lookback_days=45, now_ms=_PRUNE_NOW_MS + MS_PER_DAY
    ) == [_date_partition_name(59)]
    assert not second_stale.exists()


def test_prune_kline_cache_tolerates_failures(tmp_path: Path, monkeypatch) -> None:
    import liquidity_migration.event_demo_data as event_demo_data_module

    # (a) one partition's rmtree fails with OSError: the failure is swallowed,
    # the OTHER stale partition is still pruned.
    root_a = tmp_path / "a"
    dataset_a = root_a / "event_demo_klines_1h"
    failing = _make_partition(dataset_a, _date_partition_name(60))
    succeeding = _make_partition(dataset_a, _date_partition_name(55))
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        if Path(path).name == failing.name:
            raise OSError("permission denied (simulated)")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(event_demo_data_module.shutil, "rmtree", flaky_rmtree)
    deleted = _prune_event_demo_kline_cache(root_a, lookback_days=45, now_ms=_PRUNE_NOW_MS)
    assert deleted == [succeeding.name]
    assert failing.exists() and not succeeding.exists()

    # (b) an unexpected non-OSError failure must never escape the prune.
    root_b = tmp_path / "b"
    dataset_b = root_b / "event_demo_klines_1h"
    untouched = _make_partition(dataset_b, _date_partition_name(60))

    def exploding_rmtree(path, *args, **kwargs):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(event_demo_data_module.shutil, "rmtree", exploding_rmtree)
    assert _prune_event_demo_kline_cache(root_b, lookback_days=45, now_ms=_PRUNE_NOW_MS) == []
    assert untouched.exists()


def test_prune_kline_cache_missing_dataset_dir_is_a_noop(tmp_path: Path) -> None:
    assert _prune_event_demo_kline_cache(tmp_path, lookback_days=45, now_ms=_PRUNE_NOW_MS) == []


def test_download_recent_1h_klines_prunes_stale_partitions_after_rest_write(tmp_path: Path) -> None:
    """Call-site wiring: a successful REST write prunes out-of-window date=
    partitions of the SAME dataset (anchored to end_ms, default lookback)."""
    dataset_dir = tmp_path / "event_demo_klines_1h"
    stale = _make_partition(dataset_dir, "date=2020-01-01")
    end_ms = int(time.time() * 1000) // MS_PER_HOUR * MS_PER_HOUR

    output, stats = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=end_ms - 2 * MS_PER_HOUR,
        end_ms=end_ms,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FakeKlineMarket(),
        cache_root=tmp_path,
    )

    assert stats["fetched_rows"] == 3
    assert output.height == 3
    assert not stale.exists(), "out-of-window partition must be pruned after the REST write"
    # today's freshly written partitions survive
    assert read_dataset(tmp_path, "event_demo_klines_1h").height == 3


# --------------------------------------------------------------------------
# universe-pit-4 : _build_demo_universe doc-drift + age-floor behaviour
# (relocated from the audit bucket b01)
# --------------------------------------------------------------------------


def _hour_floor_now_ms() -> int:
    return (int(time.time() * 1000) // MS_PER_HOUR) * MS_PER_HOUR


def test_build_demo_universe_comment_no_longer_references_removed_strategy() -> None:
    """universe-pit-4: the _build_demo_universe justification must not reference
    the removed strategy's prior7_liquidity_rank null-exclusion. The live age
    compensation is the continuous downstream gate; the comment must say so."""
    import inspect

    src = inspect.getsource(event_demo_data._build_demo_universe)
    assert "prior7_liquidity_rank" not in src.split("Historical note")[0]
    assert "_continuous_age_eligible_symbols" in src


def test_build_demo_universe_unlimited_drops_age_floor(monkeypatch) -> None:
    """universe-pit-4 guard: the actual behaviour the comment describes is
    unchanged — unlimited-universe mode (rank_end == max_symbols == 0) drops the
    local 30-day age floor (min_age_days=0) so the downstream continuous gate is
    authoritative; the legacy narrow-universe mode keeps the 30-day floor."""
    captured: list[int] = []

    def spy_build(instruments, tickers, *, universe_config, snapshot_ts_ms):
        del instruments, tickers, snapshot_ts_ms
        captured.append(universe_config.min_age_days)
        return pl.DataFrame()

    monkeypatch.setattr(event_demo_data, "build_current_universe_table", spy_build)
    empty = pl.DataFrame()

    unlimited = _public_config(
        universe_rank_end=0, universe_max_symbols=0,
    )
    event_demo_data._build_demo_universe(
        empty, empty, config=unlimited, snapshot_ts_ms=_hour_floor_now_ms(),
    )
    assert captured[-1] == 0  # age floor dropped in unlimited mode

    narrow = _public_config(
        universe_rank_end=200, universe_max_symbols=50,
    )
    event_demo_data._build_demo_universe(
        empty, empty, config=narrow, snapshot_ts_ms=_hour_floor_now_ms(),
    )
    assert captured[-1] == 30  # legacy floor preserved
