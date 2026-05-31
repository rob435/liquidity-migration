"""Extracted from event_demo.py — see that module's docstring.

This sibling holds a cohesive slice of the event-demo machinery. It
imports shared helpers/configs from event_demo.py (the hub); the hub
re-imports this module's public names at the bottom so external callers
(`from liquidity_migration.event_demo import X`) keep working unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import polars as pl

from .bybit import BybitMarketData, BybitRestRateLimiter
from .config import DEFAULT_EXCLUDED_SYMBOLS, ResearchConfig, UniverseConfig
from .downloaders import _normalize_instruments, _normalize_klines
from .storage import read_dataset, write_dataset
from .universe import build_current_universe_table
from ._common import MS_PER_HOUR
from .volume_features import build_volume_features
from .volume_events import (
    _enriched_event_features,
)


from .event_demo import (  # noqa: F401  (shared hub helpers)
    _DEMO_INSTRUMENTS_CACHE_TTL_MS,
    EventDemoCycleConfig,
    _empty_klines,
)

_logger = logging.getLogger(__name__)


def _demo_instruments_cache_paths(cache_root: Path) -> tuple[Path, Path]:
    root = Path(cache_root).expanduser() / ".cache" / "event_demo_instruments"
    return root / "latest.parquet", root / "latest.json"

def _read_demo_instruments_cache(cache_root: Path) -> tuple[pl.DataFrame | None, int]:
    """Return (normalised instruments frame, fetched_ts_ms), or (None, 0)."""
    parquet_path, metadata_path = _demo_instruments_cache_paths(cache_root)
    if not parquet_path.exists() or not metadata_path.exists():
        return None, 0
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        fetched_ts_ms = int(metadata.get("fetched_ts_ms", 0))
        frame = pl.read_parquet(parquet_path)
    except (OSError, json.JSONDecodeError, ValueError, TypeError, pl.exceptions.PolarsError):
        return None, 0
    if frame.is_empty():
        return None, 0
    return frame, fetched_ts_ms

def _bust_demo_instruments_cache(cache_root: Path) -> None:
    """Delete the instruments cache so the next _demo_instruments call refetches."""
    parquet_path, metadata_path = _demo_instruments_cache_paths(cache_root)
    parquet_path.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)

def _write_demo_instruments_cache(cache_root: Path, instruments: pl.DataFrame, fetched_ts_ms: int) -> None:
    if instruments.is_empty():
        return
    parquet_path, metadata_path = _demo_instruments_cache_paths(cache_root)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    temp_parquet = parquet_path.with_name(f".{parquet_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp_metadata = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        instruments.write_parquet(temp_parquet)
        # Parquet first, metadata (the commit marker) last — see the feature cache.
        temp_parquet.replace(parquet_path)
        temp_metadata.write_text(json.dumps({"fetched_ts_ms": int(fetched_ts_ms)}, sort_keys=True), encoding="utf-8")
        temp_metadata.replace(metadata_path)
    except (OSError, pl.exceptions.PolarsError):
        temp_parquet.unlink(missing_ok=True)
        temp_metadata.unlink(missing_ok=True)

def _demo_instruments(public: Any, *, cache_root: Path, now_ms: int) -> pl.DataFrame:
    """Normalised Bybit instruments, cached with a TTL.

    Contract specs (tick size, lot step, listing date, status) change roughly
    daily, but get_instruments_info is a large multi-hundred-symbol REST call
    otherwise made on every ~60s cycle. A 1h TTL removes it from ~99% of
    cycles. Membership stays correct: the universe is instruments INNER JOIN
    the always-fresh tickers snapshot, so a symbol that stops trading drops out
    via tickers even while its cached instruments row lingers. On a fetch
    failure with a cache present we serve the stale specs rather than failing
    the whole cycle."""
    cached, fetched_ts_ms = _read_demo_instruments_cache(cache_root)
    if cached is not None and 0 <= now_ms - fetched_ts_ms < _DEMO_INSTRUMENTS_CACHE_TTL_MS:
        return cached
    try:
        fresh = _normalize_instruments(public.get_instruments_info())
    except Exception as exc:  # noqa: BLE001 - a stale spec cache beats failing the cycle
        if cached is not None:
            _logger.warning("instruments fetch failed; reusing cached specs: %s", exc)
            return cached
        raise
    _write_demo_instruments_cache(cache_root, fresh, now_ms)
    return fresh

def _build_demo_universe(
    instruments: pl.DataFrame,
    tickers: pl.DataFrame,
    *,
    config: EventDemoCycleConfig,
    snapshot_ts_ms: int,
) -> pl.DataFrame:
    # In match-the-backtest mode (universe_rank_end == universe_max_symbols
    # == 0), drop the 30-day age floor so the demo includes the same
    # 7-29-day-old listings the backtest would (the backtest doesn't pre-
    # filter by age — it lets `prior7_liquidity_rank` being null exclude
    # symbols with insufficient history naturally inside the strategy
    # filter). When the legacy narrow-universe is active the 30-day
    # safety floor stays in place to mirror prior demo behaviour.
    unlimited_universe = (
        config.universe_rank_end == 0 and config.universe_max_symbols == 0
    )
    min_age_days = 0 if unlimited_universe else 30
    universe_config = UniverseConfig(
        min_turnover_24h=config.universe_min_turnover_24h,
        min_age_days=min_age_days,
        rank_start=1,
        rank_end=config.universe_rank_end,
        max_symbols=config.universe_max_symbols,
        exclude_symbols=DEFAULT_EXCLUDED_SYMBOLS,
    )
    return build_current_universe_table(
        instruments,
        tickers,
        universe_config=universe_config,
        snapshot_ts_ms=snapshot_ts_ms,
    )

def _download_recent_1h_klines(
    symbols: list[str],
    *,
    start_ms: int,
    end_ms: int,
    config: ResearchConfig,
    workers: int,
    market_client: Any | None,
    cache_root: Path | None = None,
    kline_store: Any | None = None,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Return the (symbol, ts_ms) rectangular klines for the demo cycle.

    Sources tried in order:
      1. If ``kline_store`` is supplied, query it first — the WS-driven path
         delivers a hot in-memory window in <50ms vs the REST burst's
         multi-second tail. Symbols not yet covered by the store fall through
         to the REST path below.
      2. The on-disk compact + parquet caches (legacy REST-only fast path).
      3. REST fetches for any remaining ranges.
    """
    stats = {
        "cache_rows": 0,
        "cache_symbols": 0,
        "fetch_symbols": len(symbols),
        "fetched_rows": 0,
        "output_rows": 0,
        "store_rows": 0,
        "store_symbols": 0,
        "store_max_ts_ms": 0,
    }
    if end_ms < start_ms:
        return _empty_klines(), stats
    if not symbols:
        stats["fetch_symbols"] = 0
        return _empty_klines(), stats

    # 1) WS store fast path. The store may not cover every symbol (newly
    # listed, mid-bootstrap), so we explicitly split into store-covered and
    # store-uncovered subsets and fall back to REST only for the uncovered.
    store_frame = _empty_klines()
    store_fully_covers = False
    if kline_store is not None:
        try:
            covered_set = kline_store.symbols_with_coverage_through(end_ms)
        except Exception as exc:  # noqa: BLE001 - store must never break the cycle
            _logger.warning("kline_store coverage query failed; ignoring store: %s", exc)
            covered_set = set()
        covered_symbols = [s for s in symbols if s in covered_set]
        if covered_symbols:
            try:
                store_frame = kline_store.get_klines(
                    covered_symbols, start_ms=start_ms, end_ms=end_ms,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("kline_store get_klines failed; ignoring store: %s", exc)
                store_frame = _empty_klines()
                covered_symbols = []
            else:
                # `symbols_with_coverage_through` only checks the LATEST bar, so a
                # symbol can read as "covered" while carrying a mid-window hole a
                # WS reconnect dropped (pybit never replays confirmed bars). Drop
                # those from the store fast path so the REST fallback below
                # backfills the hole — otherwise the gappy day silently falls
                # below the >=20-hourly-bar filter and that coin's daily features
                # vanish for the day.
                incomplete = _window_incomplete_symbols(store_frame, start_ms=start_ms, end_ms=end_ms)
                if incomplete:
                    store_frame = store_frame.filter(~pl.col("symbol").is_in(list(incomplete)))
                    covered_symbols = [s for s in covered_symbols if s not in incomplete]
        store_fully_covers = bool(covered_symbols) and len(covered_symbols) == len(symbols)
        if not store_frame.is_empty():
            stats["store_rows"] = store_frame.height
            stats["store_symbols"] = store_frame.select("symbol").unique().height
            stats["store_max_ts_ms"] = int(store_frame.select(pl.col("ts_ms").max()).item() or 0)

    # FAST PATH: if the WS store fully covers the universe at end_ms,
    # skip the on-disk cache read entirely. Reading the full parquet
    # dataset costs 5-10s for ~400 symbols × 45 days; the store
    # serves the same data in <50ms. Only matters once the bootstrap
    # has populated the store — until then we still hit the disk cache.
    #
    # Two further optimizations:
    #   1. KlineStore.get_klines() already returns (symbol, ts_ms) sorted
    #      + dedup'd (the store keys bars by ts_ms, so duplicates are
    #      impossible by construction). Re-running unique()+sort() here is
    #      a 100-300ms tax on the cycle's hot loop.
    #   2. The on-disk compact cache is only read on the SLOW path (when
    #      the store doesn't cover everything). On the fast path we don't
    #      consume it, so writing it every cycle is pure I/O cost — the
    #      store has its own flush file that bootstrap recovers from on
    #      restart. Skip the write entirely under full coverage.
    if store_fully_covers and not store_frame.is_empty():
        stats["fetch_symbols"] = 0
        stats["output_rows"] = store_frame.height
        return store_frame, stats

    # 2) On-disk caches still apply to symbols not yet in the store, so the
    # legacy fast path is preserved for the bootstrap window.
    cached = _read_demo_kline_cache(cache_root, symbols=symbols, start_ms=start_ms, end_ms=end_ms)
    if not cached.is_empty():
        stats["cache_rows"] = cached.height
        stats["cache_symbols"] = cached.select("symbol").unique().height

    # Merge what we have so far so the REST fetch only fills genuine gaps.
    combined = _concat_recent_klines(store_frame, cached)
    fetch_ranges = _demo_kline_fetch_ranges(symbols, combined, start_ms=start_ms, end_ms=end_ms)
    stats["fetch_symbols"] = len(fetch_ranges)
    if not fetch_ranges:
        output = _dedupe_recent_klines(combined)
        stats["output_rows"] = output.height
        _write_demo_kline_compact_cache(
            cache_root, symbols=symbols, start_ms=start_ms, end_ms=end_ms, klines=output,
        )
        return output, stats

    # 3) REST fallback for remaining ranges.
    fetched = _fetch_recent_1h_klines(
        fetch_ranges,
        config=config,
        workers=workers,
        market_client=market_client,
    )
    stats["fetched_rows"] = fetched.height
    if cache_root is not None and not fetched.is_empty():
        write_dataset(fetched, cache_root, "event_demo_klines_1h")

    frames = [frame for frame in (store_frame, cached, fetched) if not frame.is_empty()]
    output = _dedupe_recent_klines(
        pl.concat(frames, how="diagonal_relaxed") if frames else _empty_klines()
    )
    _write_demo_kline_compact_cache(cache_root, symbols=symbols, start_ms=start_ms, end_ms=end_ms, klines=output)
    stats["output_rows"] = output.height
    return output, stats

def _concat_recent_klines(*frames: pl.DataFrame) -> pl.DataFrame:
    """Concat any number of klines frames, skipping empties. Returns the
    canonical empty-klines frame if all inputs are empty so downstream
    schema-sensitive code (group_by, sort) doesn't blow up."""
    populated = [frame for frame in frames if not frame.is_empty()]
    if not populated:
        return _empty_klines()
    return _dedupe_recent_klines(pl.concat(populated, how="diagonal_relaxed"))

def _read_demo_kline_cache(
    cache_root: Path | None,
    *,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    if cache_root is None:
        return _empty_klines()
    compact = _read_demo_kline_compact_cache(cache_root, symbols=symbols, start_ms=start_ms, end_ms=end_ms)
    if not compact.is_empty():
        return compact
    cached = read_dataset(cache_root, "event_demo_klines_1h")
    if cached.is_empty() or "symbol" not in cached.columns or "ts_ms" not in cached.columns:
        return _empty_klines()
    output = cached.filter(pl.col("symbol").is_in(symbols) & pl.col("ts_ms").is_between(start_ms, end_ms))
    _write_demo_kline_compact_cache(cache_root, symbols=symbols, start_ms=start_ms, end_ms=end_ms, klines=output)
    return output

def _demo_kline_compact_cache_paths(cache_root: Path) -> tuple[Path, Path]:
    root = Path(cache_root).expanduser() / ".cache" / "event_demo_klines_1h"
    return root / "latest_window.parquet", root / "latest_window.json"

def _demo_kline_compact_metadata(*, symbols: list[str], start_ms: int, end_ms: int) -> dict[str, Any]:
    return {
        "symbols": sorted({str(symbol) for symbol in symbols}),
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
    }

def _read_demo_kline_compact_cache(
    cache_root: Path,
    *,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
) -> pl.DataFrame:
    parquet_path, metadata_path = _demo_kline_compact_cache_paths(cache_root)
    if not parquet_path.exists() or not metadata_path.exists():
        return _empty_klines()
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_klines()
    if metadata != _demo_kline_compact_metadata(symbols=symbols, start_ms=start_ms, end_ms=end_ms):
        return _empty_klines()
    try:
        cached = pl.read_parquet(parquet_path)
    except (OSError, pl.exceptions.PolarsError):
        return _empty_klines()
    if cached.is_empty() or "symbol" not in cached.columns or "ts_ms" not in cached.columns:
        return _empty_klines()
    return cached.filter(pl.col("symbol").is_in(symbols) & pl.col("ts_ms").is_between(start_ms, end_ms))

def _write_demo_kline_compact_cache(
    cache_root: Path | None,
    *,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    klines: pl.DataFrame,
) -> None:
    if cache_root is None or klines.is_empty():
        return
    parquet_path, metadata_path = _demo_kline_compact_cache_paths(cache_root)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = _demo_kline_compact_metadata(symbols=symbols, start_ms=start_ms, end_ms=end_ms)
    output = _dedupe_recent_klines(klines)
    temp_parquet = parquet_path.with_name(f".{parquet_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp_metadata = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        output.write_parquet(temp_parquet)
        temp_metadata.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        temp_parquet.replace(parquet_path)
        temp_metadata.replace(metadata_path)
    except (OSError, pl.exceptions.PolarsError):
        temp_parquet.unlink(missing_ok=True)
        temp_metadata.unlink(missing_ok=True)

def _window_incomplete_symbols(
    klines: pl.DataFrame, *, start_ms: int, end_ms: int
) -> set[str]:
    """Symbols in ``klines`` carrying a MID-WINDOW 1h hole inside ``[start, end]``.

    1h bars are keyed uniquely on the hour grid, so a symbol's in-window bars are
    contiguous iff their count equals ``(max - min) / 1h + 1``. A WS reconnect
    drops confirmed bars that pybit never replays, leaving a hole BELOW the latest
    bar — invisible to a latest-bar coverage check. Returned symbols are forced
    off the store fast path so REST backfills the hole."""
    if klines.is_empty() or "symbol" not in klines.columns or "ts_ms" not in klines.columns:
        return set()
    windowed = klines.filter(pl.col("ts_ms").is_between(start_ms, end_ms))
    if windowed.is_empty():
        return set()
    agg = windowed.group_by("symbol").agg(
        pl.col("ts_ms").min().alias("lo"),
        pl.col("ts_ms").max().alias("hi"),
        pl.len().alias("n"),
    )
    incomplete = agg.filter(pl.col("n") < ((pl.col("hi") - pl.col("lo")) // MS_PER_HOUR + 1))
    return {str(s) for s in incomplete["symbol"].to_list()}


def _demo_kline_fetch_ranges(
    symbols: list[str],
    cached: pl.DataFrame,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[str, tuple[int, int]]:
    if cached.is_empty() or "symbol" not in cached.columns or "ts_ms" not in cached.columns:
        return {symbol: (start_ms, end_ms) for symbol in symbols}

    stats_by_symbol = {
        str(row["symbol"]): (int(row["lo"]), int(row["hi"]), int(row["n"]))
        for row in cached.filter(pl.col("ts_ms").is_between(start_ms, end_ms))
        .group_by("symbol")
        .agg(
            pl.col("ts_ms").min().alias("lo"),
            pl.col("ts_ms").max().alias("hi"),
            pl.len().alias("n"),
        )
        .iter_rows(named=True)
    }
    ranges: dict[str, tuple[int, int]] = {}
    for symbol in symbols:
        info = stats_by_symbol.get(symbol)
        if info is None:
            ranges[symbol] = (start_ms, end_ms)
            continue
        lo, hi, n = info
        if n < (hi - lo) // MS_PER_HOUR + 1:
            # Mid-window hole (a WS-reconnect dropout pybit never replays): refetch
            # the FULL window so REST backfills the missing interior hours; the
            # dedupe merge drops the overlap. A latest-bar-only tail fetch
            # (max+1h..end) would leave the hole forever and the gappy day would
            # silently fail the >=20-hourly-bar filter.
            ranges[symbol] = (start_ms, end_ms)
            continue
        fetch_start = max(hi + MS_PER_HOUR, start_ms)
        if fetch_start <= end_ms:
            ranges[symbol] = (fetch_start, end_ms)
    return ranges

def _fetch_recent_1h_klines(
    fetch_ranges: dict[str, tuple[int, int]],
    *,
    config: ResearchConfig,
    workers: int,
    market_client: Any | None,
) -> pl.DataFrame:
    if not fetch_ranges:
        return _empty_klines()

    def fetch_with_client(client: Any, symbol: str, window: tuple[int, int]) -> list[dict[str, Any]]:
        start_ms, end_ms = window
        return _normalize_klines(symbol, client.get_klines(symbol, "60", start_ms, end_ms), source="bybit_demo_cycle")

    rows: list[dict[str, Any]] = []
    if market_client is not None or workers <= 1:
        client = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        for symbol, window in fetch_ranges.items():
            rows.extend(fetch_with_client(client, symbol, window))
        return _dedupe_recent_klines(pl.DataFrame(rows, infer_schema_length=None)) if rows else _empty_klines()

    # Share one rate limiter across all worker threads. Each thread instantiates
    # its own BybitMarketData but routes _get() through this shared limiter so
    # the process as a whole stays under Bybit's public REST budget
    # (~120 req/5s per IP per category). Without this, 8 workers x 300 symbols
    # saturate the budget in seconds and pybit then sleeps 2s per 429.
    shared_limiter = BybitRestRateLimiter(
        max_requests=_demo_rest_rate_limit_per_second(),
        per_seconds=1.0,
    )

    def fetch_symbol(symbol: str) -> list[dict[str, Any]]:
        local_client = BybitMarketData(
            category=config.exchange.category,
            testnet=config.exchange.testnet,
            rate_limiter=shared_limiter,
        )
        return fetch_with_client(local_client, symbol, fetch_ranges[symbol])

    max_workers = max(1, min(workers, len(fetch_ranges)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_symbol, symbol): symbol for symbol in fetch_ranges}
        for future in as_completed(futures):
            rows.extend(future.result())
    return _dedupe_recent_klines(pl.DataFrame(rows, infer_schema_length=None)) if rows else _empty_klines()

def _demo_rest_rate_limit_per_second() -> int:
    raw = os.environ.get("BYBIT_REST_RATE_LIMIT_PER_SECOND", "").strip()
    if not raw:
        return 18
    try:
        value = int(raw)
    except ValueError:
        return 18
    return value if value > 0 else 18

def _demo_private_rest_rate_limit_per_second() -> int:
    """Bybit per-account private REST budget for place_order et al is roughly
    20 req/s sustained. We default to 15 to leave headroom for risk-engine
    private calls hitting the same account from a separate process.
    """
    raw = os.environ.get("BYBIT_PRIVATE_REST_RATE_LIMIT_PER_SECOND", "").strip()
    if not raw:
        return 15
    try:
        value = int(raw)
    except ValueError:
        return 15
    return value if value > 0 else 15

def _dedupe_recent_klines(klines: pl.DataFrame) -> pl.DataFrame:
    if klines.is_empty():
        return _empty_klines()
    return klines.unique(subset=["ts_ms", "symbol"], keep="last").sort(["symbol", "ts_ms"])

def _demo_feature_cache_paths(cache_root: Path) -> tuple[Path, Path]:
    root = Path(cache_root).expanduser() / ".cache" / "event_demo_features"
    return root / "latest.parquet", root / "latest.json"

def _demo_feature_cache_fingerprint(klines: pl.DataFrame, universe: pl.DataFrame) -> dict[str, Any]:
    """Cheap content fingerprint of the (klines, universe) feature-build inputs.

    The demo loop ticks every ~60s but 1h klines only change when a bar closes,
    so 59 of every 60 cycles feed _build_demo_features identical inputs. Counts
    + min/max ts + column sums uniquely identify a kline set for this purpose:
    the only between-cycle change is appended bars, and any appended bar moves
    row count, max ts, and the sums together. One aggregation pass, sub-ms.

    The universe is fingerprinted by row count plus the sum of WHOLE-day listing
    ages. `listing_age_days` itself is `(snapshot_ts_ms - launch_time_ms)/day`,
    which creeps up every single cycle — fingerprinting the raw float would miss
    100% of the time. The feature build only consumes the age at day resolution
    (symbol_age_days is an Int64 cast), and a membership change moves the kline
    close/turnover sums anyway, so whole-day granularity is the correct key: it
    holds steady across a trading hour and turns over only on a real day roll."""
    k = klines.select(
        pl.len().alias("rows"),
        pl.col("ts_ms").min().alias("min_ts"),
        pl.col("ts_ms").max().alias("max_ts"),
        pl.col("symbol").n_unique().alias("symbols"),
        pl.col("close").sum().alias("close_sum"),
        pl.col("turnover_quote").sum().alias("turnover_sum"),
    ).row(0)
    fingerprint: dict[str, Any] = {
        "kline_rows": int(k[0] or 0),
        "kline_min_ts": int(k[1] or 0),
        "kline_max_ts": int(k[2] or 0),
        "kline_symbols": int(k[3] or 0),
        "kline_close_sum": round(float(k[4] or 0.0), 6),
        "kline_turnover_sum": round(float(k[5] or 0.0), 3),
    }
    if not universe.is_empty() and "listing_age_days" in universe.columns:
        u = universe.select(
            pl.len().alias("rows"),
            pl.col("listing_age_days").cast(pl.Int64, strict=False).sum().alias("age_days_sum"),
        ).row(0)
        fingerprint["universe_rows"] = int(u[0] or 0)
        fingerprint["universe_age_days_sum"] = int(u[1] or 0)
    else:
        fingerprint["universe_rows"] = int(universe.height)
        fingerprint["universe_age_days_sum"] = 0
    return fingerprint

def _read_demo_feature_cache(cache_root: Path, fingerprint: dict[str, Any]) -> pl.DataFrame | None:
    parquet_path, metadata_path = _demo_feature_cache_paths(cache_root)
    if not parquet_path.exists() or not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if metadata != fingerprint:
        return None
    try:
        return pl.read_parquet(parquet_path)
    except (OSError, pl.exceptions.PolarsError):
        return None

def _write_demo_feature_cache(cache_root: Path, fingerprint: dict[str, Any], features: pl.DataFrame) -> None:
    if features.is_empty():
        return
    parquet_path, metadata_path = _demo_feature_cache_paths(cache_root)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    temp_parquet = parquet_path.with_name(f".{parquet_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temp_metadata = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        features.write_parquet(temp_parquet)
        # Replace the parquet first, then the metadata: the metadata file is the
        # commit marker. A crash between the two replaces leaves stale metadata
        # paired with fresh data -> next read mismatches -> safe recompute.
        temp_parquet.replace(parquet_path)
        temp_metadata.write_text(json.dumps(fingerprint, sort_keys=True), encoding="utf-8")
        temp_metadata.replace(metadata_path)
    except (OSError, pl.exceptions.PolarsError):
        temp_parquet.unlink(missing_ok=True)
        temp_metadata.unlink(missing_ok=True)

def _build_demo_features(
    klines: pl.DataFrame,
    universe: pl.DataFrame,
    *,
    cache_root: Path | None = None,
) -> pl.DataFrame:
    if klines.is_empty():
        return pl.DataFrame()
    fingerprint: dict[str, Any] | None = None
    if cache_root is not None:
        fingerprint = _demo_feature_cache_fingerprint(klines, universe)
        cached = _read_demo_feature_cache(cache_root, fingerprint)
        if cached is not None:
            return cached
    features = _enriched_event_features(build_volume_features(klines), klines, pl.DataFrame())
    if not universe.is_empty() and "listing_age_days" in universe.columns:
        ages = universe.select(["symbol", "listing_age_days"]).unique(subset=["symbol"], keep="first")
        for column in ("symbol_age_days", "pit_age_days"):
            if column in features.columns:
                features = features.drop(column)
        features = (
            features.join(ages, on="symbol", how="left")
            .with_columns(
                [
                    pl.col("listing_age_days").cast(pl.Int64, strict=False).alias("symbol_age_days"),
                    pl.col("listing_age_days").cast(pl.Float64, strict=False).alias("pit_age_days"),
                ]
            )
            .drop("listing_age_days")
        )
    if fingerprint is not None:
        _write_demo_feature_cache(cache_root, fingerprint, features)
    return features
