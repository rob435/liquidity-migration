"""Public market-data plane shared by demo and paper target producers.

This module has no private credentials, account snapshots, order methods, fill
recovery, or venue-mutation imports.  LONG and CONTINUOUS use these helpers in
both environments; the selected account owner is a separate route.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from .account_candidate_universe import (
    FrozenCandidateUniverse,
    continuous_profile_universe_inputs,
    enforce_frozen_candidate_frames,
    load_candidate_universe,
    require_profile_binding,
)
from .bybit_market_data import BybitMarketData, BybitRestRateLimiter
from .artifact_snapshot import read_stable_file
from .config import DEFAULT_EXCLUDED_SYMBOLS, ResearchConfig, UniverseConfig
from .downloaders import _normalize_instruments, _normalize_klines, _normalize_tickers
from .storage import dataset_path, read_dataset, write_dataset
from .universe import build_current_universe_table
from ._common import MS_PER_DAY, MS_PER_HOUR, exact_duration_ms, finite_float

_logger = logging.getLogger(__name__)

_DEMO_INSTRUMENTS_CACHE_TTL_MS = 60 * 60 * 1000
_MATCH_BACKTEST_UNIVERSE_FLOOR = 300


class MarketUniverseConfig(Protocol):
    """Structural public-data config implemented by both strategy configs."""

    @property
    def universe_rank_end(self) -> int: ...

    @property
    def universe_max_symbols(self) -> int: ...

    @property
    def universe_min_turnover_24h(self) -> float: ...

    @property
    def lookback_days(self) -> int: ...


def _float(value: Any) -> float:
    return finite_float(value, default=0.0) or 0.0


def _empty_klines() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_ms": pl.Series([], dtype=pl.Int64),
            "symbol": pl.Series([], dtype=pl.String),
            "open": pl.Series([], dtype=pl.Float64),
            "high": pl.Series([], dtype=pl.Float64),
            "low": pl.Series([], dtype=pl.Float64),
            "close": pl.Series([], dtype=pl.Float64),
            "volume_base": pl.Series([], dtype=pl.Float64),
            "turnover_quote": pl.Series([], dtype=pl.Float64),
            "source": pl.Series([], dtype=pl.String),
        }
    )


def _floor_hour_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % MS_PER_HOUR)


def _kline_window(now_ms: int, *, lookback_days: int) -> tuple[int, int]:
    end_ms = _floor_hour_ms(now_ms) - MS_PER_HOUR
    return end_ms - exact_duration_ms(days=lookback_days), end_ms


def _utc_now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _yyyymmddhhmmss(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y%m%d%H%M%S")


def _column_values(frame: pl.DataFrame, column: str) -> list[str]:
    if frame.is_empty() or column not in frame.columns:
        return []
    return [str(item) for item in frame[column].to_list()]


def _max_int(frame: pl.DataFrame, column: str) -> int:
    if frame.is_empty() or column not in frame.columns:
        return 0
    value = frame[column].max()
    return int(value) if value is not None else 0  # type: ignore[arg-type]


def _price_lookup_from_tickers_and_klines(
    tickers: pl.DataFrame,
    klines: pl.DataFrame,
) -> dict[str, float]:
    output: dict[str, float] = {}
    if not klines.is_empty():
        for row in klines.sort(["symbol", "ts_ms"]).group_by("symbol").tail(1).to_dicts():
            price = _float(row.get("close"))
            if price > 0.0:
                output[str(row["symbol"])] = price
    if not tickers.is_empty():
        for row in tickers.to_dicts():
            symbol = str(row.get("symbol", ""))
            price = _float(row.get("mark_price")) or _float(row.get("last_price"))
            if symbol and price > 0.0:
                output[symbol] = price
    return output


def _resolve_ticker_snapshot(
    public: Any,
    *,
    ticker_cache: Any | None,
    state_cache_stale_seconds: float,
) -> tuple[list[dict[str, Any]], str]:
    """Read fresh public tickers from the cache, falling back to public REST."""

    if ticker_cache is not None:
        try:
            if ticker_cache.is_seeded() and not ticker_cache.is_stale(
                stale_seconds=state_cache_stale_seconds,
            ):
                rows = ticker_cache.snapshot_list(
                    max_age_seconds=state_cache_stale_seconds
                )
                if rows:
                    return rows, "ws_cache"
        except Exception as exc:  # noqa: BLE001 - public REST is the safe fallback
            _logger.warning("ticker cache snapshot failed; REST fallback: %s", exc)
    return public.get_tickers(), "rest"


def _universe_shrink_floor(config: MarketUniverseConfig) -> int:
    requested = config.universe_max_symbols or config.universe_rank_end
    return int(requested * 0.75) if requested > 0 else _MATCH_BACKTEST_UNIVERSE_FLOOR


def _prune_cycle_reports(
    report_dir: Path,
    *,
    prefix: str,
    keep_days: int,
    now_ms: int,
    extensions: tuple[str, ...] = ("json",),
) -> None:
    """Best-effort bounded retention for per-cycle public planning reports."""

    if keep_days <= 0:
        return
    sentinel = report_dir / f".{prefix}prune_sentinel"
    try:
        sentinel_mtime_ms = int(sentinel.stat().st_mtime * 1000)
    except OSError:
        sentinel_mtime_ms = 0
    if sentinel_mtime_ms > 0 and now_ms - sentinel_mtime_ms < 3_600_000:
        return
    cutoff_ts = (now_ms / 1000.0) - keep_days * 86400.0
    try:
        for extension in extensions:
            for path in report_dir.glob(f"{prefix}*.{extension}"):
                try:
                    if path.stat().st_mtime < cutoff_ts:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue
        sentinel.touch()
    except OSError:
        return


def _demo_instruments_cache_paths(cache_root: Path) -> tuple[Path, Path]:
    root = Path(cache_root).expanduser() / ".cache" / "event_demo_instruments"
    return root / "latest.parquet", root / "latest.json"

def _read_demo_instruments_cache(cache_root: Path) -> tuple[pl.DataFrame | None, int]:
    """Return (normalised instruments frame, fetched_ts_ms), or (None, 0)."""
    parquet_path, metadata_path = _demo_instruments_cache_paths(cache_root)
    if not parquet_path.exists() or not metadata_path.exists():
        return None, 0
    try:
        metadata_snapshot = read_stable_file(
            metadata_path,
            label="demo instruments cache metadata",
            require_single_link=False,
        )
        metadata = json.loads(metadata_snapshot.data)
        if not isinstance(metadata, dict):
            return None, 0
        fetched_ts_ms = int(metadata.get("fetched_ts_ms", 0))
        parquet_snapshot = read_stable_file(
            parquet_path,
            label="demo instruments cache parquet",
            require_single_link=False,
        )
        if (
            set(metadata) != {"fetched_ts_ms", "parquet_sha256"}
            or metadata.get("parquet_sha256") != parquet_snapshot.sha256
        ):
            return None, 0
        frame = pl.read_parquet(io.BytesIO(parquet_snapshot.data))
    except (
        OSError,
        RuntimeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        pl.exceptions.PolarsError,
    ):
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
        parquet_snapshot = read_stable_file(
            temp_parquet,
            label="staged demo instruments cache parquet",
            require_single_link=False,
        )
        # Parquet first, metadata (the commit marker) last — see the feature cache.
        temp_parquet.replace(parquet_path)
        temp_metadata.write_text(
            json.dumps(
                {
                    "fetched_ts_ms": int(fetched_ts_ms),
                    "parquet_sha256": parquet_snapshot.sha256,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
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
    config: MarketUniverseConfig,
    snapshot_ts_ms: int,
) -> pl.DataFrame:
    # Listing age belongs to the active component gate downstream. Applying a
    # second implicit 30-day floor here would silently change that profile.
    universe_config = UniverseConfig(
        min_turnover_24h=config.universe_min_turnover_24h,
        min_age_days=0,
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


def _resolve_cycle_universe(
    *,
    public: Any,
    demo: MarketUniverseConfig,
    config: ResearchConfig,
    root: Path,
    cycle_now_ms: int,
    ticker_cache: Any | None,
    state_cache_stale_seconds: float,
    frozen_candidate_universe: FrozenCandidateUniverse | None = None,
) -> tuple[pl.DataFrame, list[str], pl.DataFrame, str]:
    """Resolve one fresh, public-only strategy universe."""

    _ = config  # venue normalization is owned by the supplied public adapter
    instruments = _demo_instruments(public, cache_root=root, now_ms=cycle_now_ms)
    raw_tickers, ticker_source = _resolve_ticker_snapshot(
        public,
        ticker_cache=ticker_cache,
        state_cache_stale_seconds=state_cache_stale_seconds,
    )
    tickers = _normalize_tickers(raw_tickers)
    universe = _build_demo_universe(
        instruments,
        tickers,
        config=demo,
        snapshot_ts_ms=cycle_now_ms,
    )
    shrink_floor = _universe_shrink_floor(demo)
    if universe.height < shrink_floor:
        _logger.warning(
            "universe shrink detected: got %d symbols (floor %d); "
            "busting instruments cache and retrying",
            universe.height,
            shrink_floor,
        )
        _bust_demo_instruments_cache(root)
        instruments = _demo_instruments(public, cache_root=root, now_ms=cycle_now_ms)
        tickers = _normalize_tickers(public.get_tickers())
        universe = _build_demo_universe(
            instruments,
            tickers,
            config=demo,
            snapshot_ts_ms=cycle_now_ms,
        )
        if universe.height < shrink_floor:
            _logger.error(
                "universe shrink persists after cache bust: %d symbols "
                "(floor %d, instruments=%d, tickers=%d)",
                universe.height,
                shrink_floor,
                instruments.height,
                tickers.height,
            )
    candidate_path = str(getattr(demo, "candidate_universe_file", "") or "").strip()
    if candidate_path:
        frozen = frozen_candidate_universe or load_candidate_universe(candidate_path)
        if frozen.path != Path(candidate_path).expanduser().absolute():
            raise ValueError("CONT candidate-universe snapshot belongs to another path")
        require_profile_binding(
            frozen,
            profile="continuous",
            current_inputs=continuous_profile_universe_inputs(demo),
        )
        enforce_frozen_candidate_frames(
            instruments,
            tickers,
            frozen,
            snapshot_ts_ms=cycle_now_ms,
            context="CONT cycle",
        )
        # Newly listed symbols may appear in the current public snapshot but
        # cannot enter this bounded evidence epoch.
        universe = universe.filter(pl.col("symbol").is_in(list(frozen.symbols)))
    elif frozen_candidate_universe is not None:
        raise ValueError("CONT received a frozen candidate universe without a configured path")
    symbols = universe["symbol"].to_list() if not universe.is_empty() else []
    if not symbols:
        raise RuntimeError("public market-data cycle found no tradable symbols")
    return universe, symbols, tickers, ticker_source

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
    write_compact_cache: bool = True,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Return the (symbol, ts_ms) rectangular klines for the demo cycle.

    Sources tried in order:
      1. If ``kline_store`` is supplied, query it first — the WS-driven path
         delivers a hot in-memory window in <50ms vs the REST burst's
         multi-second tail. Symbols not yet covered by the store fall through
         to the REST path below.
      2. The on-disk compact + parquet caches used by the REST fallback.
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
    # The compact cache remains useful during the bootstrap window.
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
        if write_compact_cache:
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
        # After a successful write, drop date= partitions the cycle can never
        # read again. The clock is anchored to end_ms (== now snapped to the
        # latest closed hour in the live cycle — at most 1h behind wall clock,
        # dwarfed by the 3-day safety margin) so the prune is deterministic
        # under test windows. max() with the actual requested window keeps a
        # wider-than-default lookback config safe from its own prune.
        window_days = int(-(-(end_ms - start_ms) // MS_PER_DAY))
        _prune_event_demo_kline_cache(
            cache_root,
            lookback_days=max(_KLINE_CACHE_PRUNE_LOOKBACK_DAYS, window_days),
            now_ms=end_ms,
        )

    frames = [frame for frame in (store_frame, cached, fetched) if not frame.is_empty()]
    output = _dedupe_recent_klines(
        pl.concat(frames, how="diagonal_relaxed") if frames else _empty_klines()
    )
    if write_compact_cache:
        _write_demo_kline_compact_cache(
            cache_root,
            symbols=symbols,
            start_ms=start_ms,
            end_ms=end_ms,
            klines=output,
        )
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
    # Don't write the compact cache here (EVE-7): the sole caller (_download_recent_1h_klines)
    # always rewrites a same-key SUPERSET right after — at the no-fetch path or post-REST-merge —
    # so this write would be immediately overwritten. Skipping it saves one full-window parquet
    # serialization per slow-path cache-miss cycle.
    return output

_KLINE_CACHE_PRUNE_SAFETY_MARGIN_DAYS = 3
# Producers normally read a trailing 45-day public window; older date
# partitions are dead weight on the operational root.
_KLINE_CACHE_PRUNE_LOOKBACK_DAYS = 45
# UTC date of the last prune attempt per dataset dir: the prune is invoked on
# every REST write (~once per ~60s cycle) but only needs to do work when the
# date rolls, so anything else is a single dict lookup.
_kline_cache_last_prune_utc_date: dict[str, str] = {}


def _prune_event_demo_kline_cache(
    cache_root: Path,
    *,
    lookback_days: int = _KLINE_CACHE_PRUNE_LOOKBACK_DAYS,
    now_ms: int | None = None,
) -> list[str]:
    """Delete ``date=YYYY-MM-DD`` partitions older than the demo lookback window.

    The REST writer appends date= partitions to event_demo_klines_1h forever
    (~6.8MB/day/root; live box reached 49 partitions vs a 45-day lookback,
    ~5GB/yr across demo+paper) while nothing ever reads past the trailing
    ``lookback_days``. Cutoff = today - lookback_days - a 3-day safety margin.

    Constraints honoured here:
      - only directories named ``date=<parseable ISO date>`` directly under this
        ONE dataset dir are deleted; symlinks are never followed (skipped);
      - any failure is logged and swallowed — pruning must never break the
        write path;
      - cheap per cycle: the scan is skipped entirely unless the UTC date
        changed since the last prune of this dataset dir.

    Returns the deleted partition dir names (for logging/tests).
    """
    deleted: list[str] = []
    try:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
        dataset_dir = dataset_path(cache_root, "event_demo_klines_1h")
        marker_key = str(dataset_dir)
        if _kline_cache_last_prune_utc_date.get(marker_key) == today.isoformat():
            return deleted
        _kline_cache_last_prune_utc_date[marker_key] = today.isoformat()
        if not dataset_dir.is_dir():
            return deleted
        cutoff = today - timedelta(days=int(lookback_days) + _KLINE_CACHE_PRUNE_SAFETY_MARGIN_DAYS)
        for entry in sorted(dataset_dir.iterdir()):
            name = entry.name
            if not name.startswith("date="):
                continue
            try:
                partition_date = date.fromisoformat(name[len("date="):])
            except ValueError:
                continue
            if partition_date >= cutoff:
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                shutil.rmtree(entry)
            except OSError as exc:
                _logger.warning("kline cache prune failed for %s: %s", entry, exc)
            else:
                deleted.append(name)
        if deleted:
            _logger.info(
                "pruned %d stale event_demo_klines_1h partitions (older than %s) under %s",
                len(deleted),
                cutoff.isoformat(),
                dataset_dir,
            )
    except Exception as exc:  # noqa: BLE001 - the prune must never break the write path
        _logger.warning("event_demo_klines_1h cache prune skipped: %s", exc)
    return deleted


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
        metadata_snapshot = read_stable_file(
            metadata_path,
            label="demo kline compact-cache metadata",
            require_single_link=False,
        )
        metadata = json.loads(metadata_snapshot.data)
    except (OSError, RuntimeError, json.JSONDecodeError, ValueError):
        return _empty_klines()
    expected_metadata = _demo_kline_compact_metadata(
        symbols=symbols,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    if (
        not isinstance(metadata, dict)
        or set(metadata) != set(expected_metadata) | {"parquet_sha256"}
        or any(metadata.get(key) != value for key, value in expected_metadata.items())
    ):
        return _empty_klines()
    try:
        parquet_snapshot = read_stable_file(
            parquet_path,
            label="demo kline compact-cache parquet",
            require_single_link=False,
        )
        if metadata.get("parquet_sha256") != parquet_snapshot.sha256:
            return _empty_klines()
        cached = pl.read_parquet(io.BytesIO(parquet_snapshot.data))
    except (OSError, RuntimeError, ValueError, pl.exceptions.PolarsError):
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
        parquet_snapshot = read_stable_file(
            temp_parquet,
            label="staged demo kline compact-cache parquet",
            require_single_link=False,
        )
        temp_metadata.write_text(
            json.dumps(
                {**metadata, "parquet_sha256": parquet_snapshot.sha256},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
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

def _dedupe_recent_klines(klines: pl.DataFrame) -> pl.DataFrame:
    if klines.is_empty():
        return _empty_klines()
    return klines.unique(subset=["ts_ms", "symbol"], keep="last").sort(["symbol", "ts_ms"])
