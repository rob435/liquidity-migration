"""In-memory 1h kline store for the WS-driven kline-delivery path.

The receiving end of the WS push path: a thread-safe store keyed by
(symbol, ts_ms) that the WS pool writes to as confirmed bars land and the
cycle's feature build reads instead of REST-fetching. A full-universe REST
pull takes hours, which is longer than the strategy's alpha survives.

Guarantees:

- ``add_bar`` is idempotent on ``(symbol, ts_ms)``: a re-delivered bar is an
  overwrite, never a duplicate row.
- Bounded memory: bars older than ``retain_days`` are evicted on add.
- One ``threading.RLock`` guards both the per-symbol bar dicts and the metadata
  maps; read and write paths are short.
- A background thread serialises the store to a single parquet file every
  ``flush_interval_seconds``; ``recover_from_disk()`` repopulates from it.

``get_klines`` returns the same schema as ``_download_recent_1h_klines``
(``_empty_klines`` in event_demo_data.py) — ``ts_ms``, ``symbol``, ``open``,
``high``, ``low``, ``close``, ``volume_base``, ``turnover_quote``, ``source``
— so the two are interchangeable per symbol.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core._common import MS_PER_HOUR, exact_duration_ms


_logger = logging.getLogger("liquidity_migration.marketdata.kline_store")

# Eviction is amortized: an insert re-scans for stale bars only once the global
# newest_ts has advanced this far since the last sweep. Bootstrap inserts bars
# OLDER than the current max, so it never sweeps per-bar. Cost is up to one
# window of stale bars held longer than retain_days.
_EVICTION_INTERVAL_MS = MS_PER_HOUR

# A bar timestamped implausibly far in the future is corrupt. Accepting one
# advances ``_global_max_ts_ms``, pushing ``_evict_old_locked``'s cutoff
# (reference - retain) into the future and evicting the entire store. 1h bars
# can legitimately start up to the next boundary plus clock skew ahead.
_MAX_FUTURE_TS_SLACK_MS = exact_duration_ms(hours=2)


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


WS_STORE_SOURCE = "bybit_ws_kline"

_KLINE_SCHEMA: dict[str, type[pl.DataType]] = {  # values are polars dtype classes, not instances
    "ts_ms": pl.Int64,
    "symbol": pl.String,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume_base": pl.Float64,
    "turnover_quote": pl.Float64,
    "source": pl.String,
}


def _empty_klines_frame() -> pl.DataFrame:
    return pl.DataFrame({name: pl.Series([], dtype=dtype) for name, dtype in _KLINE_SCHEMA.items()})


@dataclass(slots=True)
class _Bar:
    """One 1h bar. Stored per-symbol keyed by ``ts_ms`` in the store's dict."""

    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume_base: float
    turnover_quote: float
    source: str

    def to_dict(self, symbol: str) -> dict[str, Any]:
        return {
            "ts_ms": self.ts_ms,
            "symbol": symbol,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume_base": self.volume_base,
            "turnover_quote": self.turnover_quote,
            "source": self.source,
        }


def _parse_ws_kline_event(bar: Mapping[str, Any]) -> _Bar | None:
    """Parse a single bar dict from pybit's WS kline payload.

    pybit forwards the venue's raw bar object in the ``data`` array of each
    message using ``start``, ``open``, ``high``, ``low``, ``close``, ``volume``,
    and ``turnover``.
    """

    raw_ts = bar.get("start")
    if raw_ts is None:
        return None
    try:
        ts_ms = int(raw_ts)
    except (TypeError, ValueError):
        return None

    def _coerce(name: str) -> float | None:
        raw = bar.get(name)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    open_ = _coerce("open")
    high = _coerce("high")
    low = _coerce("low")
    close = _coerce("close")
    volume = _coerce("volume")
    turnover = _coerce("turnover")
    if None in (open_, high, low, close, volume, turnover):
        return None
    return _Bar(
        ts_ms=ts_ms,
        open=float(open_),
        high=float(high),
        low=float(low),
        close=float(close),
        volume_base=float(volume),
        turnover_quote=float(turnover),
        source=WS_STORE_SOURCE,
    )


class KlineStore:
    """Thread-safe in-memory 1h klines per symbol with periodic disk flush.

    Append-and-read patterns are dominated by the WS thread(s) appending one
    bar per symbol per hour and a cycle thread reading a windowed dataframe
    every ~60s, so a single RLock is the simplest correct design and the lock
    is held for microseconds at a time. Holding the lock across a flush would
    be unacceptable; we copy the store under the lock and serialise outside.

    The flush thread is opt-out (``flush_interval_seconds=0`` disables it) so
    unit tests can exercise the store without disk I/O.
    """

    DEFAULT_RETAIN_DAYS = 90
    DEFAULT_FLUSH_INTERVAL_SECONDS = 30.0

    def __init__(
        self,
        *,
        cache_root: str | Path | None,
        retain_days: int = DEFAULT_RETAIN_DAYS,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        max_future_ts_slack_ms: int = _MAX_FUTURE_TS_SLACK_MS,
    ) -> None:
        if retain_days <= 0:
            raise ValueError("retain_days must be positive")
        if flush_interval_seconds < 0.0:
            raise ValueError("flush_interval_seconds must be non-negative")
        self._retain_ms = exact_duration_ms(days=retain_days)
        self._retain_days = int(retain_days)
        # Configurable far-future bar reject window (default = module constant).
        self._max_future_ts_slack_ms = int(max_future_ts_slack_ms)
        self._flush_interval_seconds = float(flush_interval_seconds)
        self._lock = threading.RLock()
        self._bars: dict[str, dict[int, _Bar]] = {}
        self._adds_total = 0
        self._adds_skipped_unconfirmed = 0
        self._adds_evicted = 0
        self._reads_total = 0
        # Single-entry materialized-window cache keyed on (sorted symbols, start,
        # end, mutation-version). _adds_total + _adds_evicted change IFF _bars
        # changes on every mutation path EXCEPT recover_from_disk, which must
        # invalidate the cache explicitly. Callers get a clone.
        self._window_cache: tuple[tuple, pl.DataFrame] | None = None
        # (version, oldest_ts_ms, row_count) cache for stats(), on the same
        # mutation version — recovery must invalidate this too.
        self._stats_scan_cache: tuple[int, int | None, int] | None = None
        # Skip a periodic flush when nothing changed since the last one. -1
        # forces the first flush. Same mutation version as the window cache.
        self._last_flush_version = -1
        self._cache_root: Path | None = Path(cache_root).expanduser() if cache_root is not None else None
        self._flush_dir: Path | None
        self._flush_path: Path | None
        if self._cache_root is not None:
            self._flush_dir = self._cache_root / ".cache" / "ws_klines"
            self._flush_path = self._flush_dir / "store.parquet"
        else:
            self._flush_dir = None
            self._flush_path = None
        self._flush_thread: threading.Thread | None = None
        self._flush_stop = threading.Event()
        # Serializes a whole flush (snapshot + write + rename + bookkeeping).
        # stop_flush_thread() can return while the loop's flush is still in
        # flight, and stop() then flushes directly, so two can overlap and race
        # on _last_flush_version/_last_flush_rows. Distinct from the data RLock:
        # WS inserts must never block on a parquet write.
        self._flush_io_lock = threading.Lock()
        self._flushes_total = 0
        self._flush_errors = 0
        self._last_flush_rows = 0
        # Incremental newest-ts cache maintained in _insert_bar / recovery so
        # _max_ts_with_new is O(1) instead of O(symbols * bars).
        self._global_max_ts_ms: int = 0
        self._last_eviction_ref_ts_ms: int = 0

    # -- write path -----------------------------------------------------

    def add_bar(self, symbol: str, bar: Mapping[str, Any], *, confirmed: bool) -> bool:
        """Add a single bar from a WS payload.

        Returns True if the bar was accepted, False if rejected (unconfirmed,
        unparseable, or older than ``retain_days``). Idempotent on
        ``(symbol, ts_ms)``: re-delivery of an already-stored bar overwrites
        the values in place.
        """
        if not confirmed:
            with self._lock:
                self._adds_skipped_unconfirmed += 1
            return False
        parsed = _parse_ws_kline_event(bar)
        if parsed is None:
            return False
        return self._insert_bar(symbol, parsed)

    def bootstrap_symbol(self, symbol: str, bars: Iterable[Mapping[str, Any]]) -> int:
        """Bulk-load historical bars for ``symbol``. Caller is responsible for
        confirming every bar is fully closed (REST-returned bars always are).

        Bootstrap never overwrites a ts_ms already present: the REST backfill
        and the live WS stream race at cold start, and the WS feed reflects the
        most recent venue state, so it wins on ties.

        One lock acquire and one deferred eviction sweep at the end; per-bar
        eviction here is O(N^2) over the whole store and dominates cold start.
        """
        if not symbol:
            return 0
        max_acceptable_ts = _utc_now_ms() + self._max_future_ts_slack_ms
        parsed_bars: list[_Bar] = []
        for bar in bars:
            parsed = _parse_ws_kline_event(bar)
            if parsed is not None and parsed.ts_ms <= max_acceptable_ts:
                parsed_bars.append(parsed)
        if not parsed_bars:
            return 0
        accepted = 0
        with self._lock:
            reference_ts = max(self._global_max_ts_ms, max(p.ts_ms for p in parsed_bars))
            # Never let the eviction reference exceed now + slack.
            reference_ts = min(reference_ts, max_acceptable_ts)
            symbol_bars = self._bars.setdefault(symbol, {})
            for parsed in parsed_bars:
                if reference_ts - parsed.ts_ms > self._retain_ms:
                    continue
                # setdefault — gap-fill only. WS-delivered bars at the same
                # ts_ms are preserved (see docstring).
                if symbol_bars.setdefault(parsed.ts_ms, parsed) is parsed:
                    accepted += 1
            self._adds_total += accepted
            if reference_ts > self._global_max_ts_ms:
                self._global_max_ts_ms = reference_ts
            # Single amortized eviction at the end of the bulk load; per-bar
            # sweeps here serialize every other worker under the store lock.
            if reference_ts - self._last_eviction_ref_ts_ms >= _EVICTION_INTERVAL_MS:
                self._evict_old_locked(reference_ts_ms=reference_ts)
                self._last_eviction_ref_ts_ms = reference_ts
        return accepted

    def _insert_bar(self, symbol: str, bar: _Bar) -> bool:
        if not symbol:
            return False
        max_acceptable_ts = _utc_now_ms() + self._max_future_ts_slack_ms
        if bar.ts_ms > max_acceptable_ts:
            # Corrupt far-future bar — see _MAX_FUTURE_TS_SLACK_MS. Dropping it
            # protects every other symbol's bars from mass-eviction.
            return False
        with self._lock:
            # _global_max_ts_ms is read and written only under the store lock,
            # so it stays consistent with _bars.
            reference_ts = self._global_max_ts_ms
            if bar.ts_ms > reference_ts:
                reference_ts = bar.ts_ms
            reference_ts = min(reference_ts, max_acceptable_ts)
            if reference_ts - bar.ts_ms > self._retain_ms:
                return False
            symbol_bars = self._bars.setdefault(symbol, {})
            symbol_bars[bar.ts_ms] = bar
            self._adds_total += 1
            if reference_ts > self._global_max_ts_ms:
                self._global_max_ts_ms = reference_ts
            # Amortized eviction — see _EVICTION_INTERVAL_MS.
            if reference_ts - self._last_eviction_ref_ts_ms >= _EVICTION_INTERVAL_MS:
                self._evict_old_locked(reference_ts_ms=reference_ts)
                self._last_eviction_ref_ts_ms = reference_ts
            return True

    def _recompute_global_max_locked(self) -> None:
        """Recompute _global_max_ts_ms from the bars actually present.

        The hot paths only ever advance _global_max_ts_ms, so a path that
        REMOVES bars (eviction-by-age, keep_only_symbols) can drop the lone
        symbol holding the max and leave the cache reporting stale data as
        fresh. Cheap relative to the trim that just ran. Must hold self._lock.
        """
        self._global_max_ts_ms = max(
            (max(symbol_bars) for symbol_bars in self._bars.values() if symbol_bars),
            default=0,
        )

    def _evict_old_locked(self, *, reference_ts_ms: int) -> None:
        cutoff_ts = reference_ts_ms - self._retain_ms
        empty_symbols: list[str] = []
        removed_any = False
        for symbol, symbol_bars in self._bars.items():
            stale = [ts for ts in symbol_bars if ts < cutoff_ts]
            if stale:
                for ts in stale:
                    del symbol_bars[ts]
                self._adds_evicted += len(stale)
                removed_any = True
            if not symbol_bars:
                empty_symbols.append(symbol)
        for symbol in empty_symbols:
            del self._bars[symbol]
        # A trim of the lone global-max holder must not leave a stale, too-fresh
        # newest_ts (ws-pool-2). Only rescan when bars were actually removed.
        if removed_any:
            self._recompute_global_max_locked()

    # -- read path ------------------------------------------------------

    def get_klines(
        self,
        symbols: Iterable[str],
        *,
        start_ms: int,
        end_ms: int,
    ) -> pl.DataFrame:
        """Return klines in (symbol, ts_ms) rectangular form for the given
        symbols within ``[start_ms, end_ms]`` inclusive.

        The output schema matches ``_empty_klines`` so the cycle's
        ``_download_recent_1h_klines`` integration is a drop-in concat. The
        bars are sorted by (symbol, ts_ms).
        """
        if end_ms < start_ms:
            return _empty_klines_frame()
        # Column-major collection: lists of primitives handed to polars are
        # ~3x faster than a dict per bar re-shaped by the DataFrame constructor.
        ts_col: list[int] = []
        symbol_col: list[str] = []
        open_col: list[float] = []
        high_col: list[float] = []
        low_col: list[float] = []
        close_col: list[float] = []
        volume_col: list[float] = []
        turnover_col: list[float] = []
        source_col: list[str] = []
        # Sort symbols so the output is in (symbol, ts_ms) order regardless
        # of what the caller passed.
        sorted_symbols = sorted(symbols)
        with self._lock:
            self._reads_total += 1
            # A cache hit means the mutation version is unchanged, so the
            # cached frame is current. Callers get a clone.
            version = self._adds_total + self._adds_evicted
            cache_key = (tuple(sorted_symbols), start_ms, end_ms, version)
            cached = self._window_cache
            if cached is not None and cached[0] == cache_key:
                return cached[1].clone()
            for symbol in sorted_symbols:
                symbol_bars = self._bars.get(symbol)
                if not symbol_bars:
                    continue
                for ts_ms in sorted(symbol_bars):
                    if ts_ms < start_ms:
                        continue
                    if ts_ms > end_ms:
                        break
                    bar = symbol_bars[ts_ms]
                    ts_col.append(ts_ms)
                    symbol_col.append(symbol)
                    open_col.append(bar.open)
                    high_col.append(bar.high)
                    low_col.append(bar.low)
                    close_col.append(bar.close)
                    volume_col.append(bar.volume_base)
                    turnover_col.append(bar.turnover_quote)
                    source_col.append(bar.source)
        if not ts_col:
            return _empty_klines_frame()
        # Already emitted in (symbol, ts_ms) order by the loop above, so no
        # .sort() — it would re-pay O(N log N) on sorted data.
        frame = pl.DataFrame(
            {
                "ts_ms": ts_col,
                "symbol": symbol_col,
                "open": open_col,
                "high": high_col,
                "low": low_col,
                "close": close_col,
                "volume_base": volume_col,
                "turnover_quote": turnover_col,
                "source": source_col,
            },
            schema=_KLINE_SCHEMA,
        )
        # cache_key holds the build-time version, so a store mutation during
        # the build makes the next read miss rather than serve a stale frame.
        self._window_cache = (cache_key, frame)
        return frame.clone()

    def symbols_with_coverage_through(self, ts_ms: int) -> set[str]:
        """Symbols that have a bar with ``bar.ts_ms >= ts_ms``.

        Cycle code uses this to decide which symbols hit the store fast path
        and which must fall back to REST. ``ts_ms`` is typically the cycle's
        ``end_ms`` (the bar-end alignment) so any symbol present at the most
        recent closed bar is "covered"."""
        covered: set[str] = set()
        with self._lock:
            for symbol, symbol_bars in self._bars.items():
                if not symbol_bars:
                    continue
                if max(symbol_bars) >= ts_ms:
                    covered.add(symbol)
        return covered

    def keep_only_symbols(self, symbols: Iterable[str]) -> int:
        """Drop every symbol NOT in ``symbols``. Returns rows dropped.

        Called once the universe is set, so a recovery from a prior run that
        subscribed a wider universe stops paying memory for it. A set-trim, not
        an eviction-by-age: bars for kept symbols are preserved exactly."""
        keep = set(symbols)
        dropped = 0
        with self._lock:
            stale = [s for s in self._bars if s not in keep]
            for symbol in stale:
                dropped += len(self._bars[symbol])
                del self._bars[symbol]
            if dropped > 0:
                self._adds_evicted += dropped
                # Trimming may have removed the lone global-max holder; keep
                # newest_ts honest rather than over-reporting freshness (ws-pool-2).
                self._recompute_global_max_locked()
        return dropped

    def symbols_with_coverage_in_window(self, *, start_ms: int, end_ms: int) -> set[str]:
        """Symbols whose stored bars span the FULL ``[start_ms, end_ms]``
        window — i.e. oldest bar <= start_ms AND newest bar >= end_ms.

        Bootstrap uses this (not ``symbols_with_coverage_through``) so a
        recovered store with only the latest hour does NOT prevent a fresh
        bootstrap from filling in the historical window. Without this, a
        daemon that restarts with a stale flush file skips bootstrap and
        the cycle operates on a partial dataset indefinitely."""
        covered: set[str] = set()
        with self._lock:
            for symbol, symbol_bars in self._bars.items():
                if not symbol_bars:
                    continue
                ts_list = symbol_bars.keys()
                if max(ts_list) >= end_ms and min(ts_list) <= start_ms:
                    covered.add(symbol)
        return covered

    def newest_ts_ms(self) -> int | None:
        with self._lock:
            # Cached: _global_max_ts_ms is updated incrementally on every
            # insert and on recovery, so this is O(1). Returns None when the
            # store has never received any bars.
            return self._global_max_ts_ms if self._global_max_ts_ms > 0 else None

    def _oldest_ts_ms_locked(self) -> int | None:
        best: int | None = None
        for symbol_bars in self._bars.values():
            if not symbol_bars:
                continue
            local_min = min(symbol_bars)
            if best is None or local_min < best:
                best = local_min
        return best

    def stats(self) -> dict[str, Any]:
        with self._lock:
            symbol_count = len(self._bars)
            newest = self.newest_ts_ms()
            # oldest_ts_ms + row_count cost an O(symbols x bars) scan and stats()
            # is polled every heartbeat, so cache the pair on the same mutation
            # version as the window cache: the scan then runs only when the
            # store actually changed (ws-dataplane-7).
            version = self._adds_total + self._adds_evicted
            cached = self._stats_scan_cache
            if cached is not None and cached[0] == version:
                oldest = cached[1]
                row_count = cached[2]
            else:
                oldest = self._oldest_ts_ms_locked()
                row_count = sum(len(symbol_bars) for symbol_bars in self._bars.values())
                self._stats_scan_cache = (version, oldest, row_count)
            # 56-byte estimate matches the dataclass overhead in CPython 3.11+
            # with slots (8 ints + 6 floats + 1 str pointer, ~56-72 bytes).
            estimated_bytes = row_count * 72 + symbol_count * 224
            return {
                "symbols": symbol_count,
                "rows": row_count,
                "oldest_ts_ms": oldest,
                "newest_ts_ms": newest,
                "adds_total": self._adds_total,
                "adds_skipped_unconfirmed": self._adds_skipped_unconfirmed,
                "adds_evicted": self._adds_evicted,
                "reads_total": self._reads_total,
                "flushes_total": self._flushes_total,
                "flush_errors": self._flush_errors,
                "last_flush_rows": self._last_flush_rows,
                "retain_days": self._retain_days,
                "estimated_bytes": estimated_bytes,
            }

    # -- flush + recover ------------------------------------------------

    def start_flush_thread(self) -> None:
        """Start the background flush thread. No-op if disabled or no cache_root."""
        if self._flush_thread is not None and self._flush_thread.is_alive():
            return
        if self._cache_root is None or self._flush_interval_seconds <= 0.0:
            return
        self._flush_stop.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="kline-store-flush", daemon=True
        )
        self._flush_thread.start()

    def stop_flush_thread(self, *, join_timeout: float = 10.0) -> None:
        thread = self._flush_thread
        self._flush_thread = None
        if thread is None:
            return
        self._flush_stop.set()
        thread.join(timeout=join_timeout)

    def _flush_loop(self) -> None:
        while not self._flush_stop.wait(timeout=self._flush_interval_seconds):
            try:
                self.flush_to_disk()
            except Exception as exc:  # noqa: BLE001 - never let a flush kill the thread
                _logger.warning("kline_store flush failed: %s", exc)

    def flush_to_disk(self) -> int:
        """Serialise the whole store to ``store.parquet``.

        Returns the number of rows written. Uses an atomic rename so a partial
        write never leaves a corrupt file in place for the next recovery.

        Snapshot collection holds the store lock only long enough to copy
        primitives into column lists; the DataFrame build and parquet write
        happen without it so WS inserts are not stalled by the flush cadence.

        _flush_io_lock serializes whole flushes so the final stop() flush cannot
        overlap a lingering loop flush and race on the version/rows bookkeeping.
        """
        with self._flush_io_lock:
            return self._flush_to_disk_locked()

    def _flush_to_disk_locked(self) -> int:
        if self._cache_root is None or self._flush_path is None or self._flush_dir is None:
            return 0
        ts_col: list[int] = []
        symbol_col: list[str] = []
        open_col: list[float] = []
        high_col: list[float] = []
        low_col: list[float] = []
        close_col: list[float] = []
        volume_col: list[float] = []
        turnover_col: list[float] = []
        source_col: list[str] = []
        with self._lock:
            version = self._adds_total + self._adds_evicted
            if version == self._last_flush_version:
                # Nothing changed since the last flush — skip the full re-serialize.
                return 0
            for symbol, symbol_bars in self._bars.items():
                for ts_ms in sorted(symbol_bars):
                    bar = symbol_bars[ts_ms]
                    ts_col.append(ts_ms)
                    symbol_col.append(symbol)
                    open_col.append(bar.open)
                    high_col.append(bar.high)
                    low_col.append(bar.low)
                    close_col.append(bar.close)
                    volume_col.append(bar.volume_base)
                    turnover_col.append(bar.turnover_quote)
                    source_col.append(bar.source)
        if not ts_col:
            # Drop any existing flush file: an empty store should not appear
            # to recover stale data on next startup.
            try:
                self._flush_path.unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                self._flushes_total += 1
                self._last_flush_rows = 0
                self._last_flush_version = version
            return 0
        temp_path: Path | None = None
        try:
            self._flush_dir.mkdir(parents=True, exist_ok=True)
            frame = pl.DataFrame(
                {
                    "ts_ms": ts_col,
                    "symbol": symbol_col,
                    "open": open_col,
                    "high": high_col,
                    "low": low_col,
                    "close": close_col,
                    "volume_base": volume_col,
                    "turnover_quote": turnover_col,
                    "source": source_col,
                },
                schema=_KLINE_SCHEMA,
            )
            temp_path = self._flush_path.with_name(
                f".{self._flush_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )
            frame.write_parquet(temp_path)
            temp_path.replace(self._flush_path)
        except (OSError, pl.exceptions.PolarsError) as exc:
            # A failed write/rename must not leak the temp file: each retry uses a
            # fresh time_ns name, so leftovers would accumulate one per failure
            # for the daemon's lifetime.
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            with self._lock:
                self._flush_errors += 1
            _logger.warning("kline_store flush write failed: %s", exc)
            return 0
        with self._lock:
            self._flushes_total += 1
            self._last_flush_rows = len(ts_col)
            self._last_flush_version = version
        return len(ts_col)

    def recover_from_disk(self) -> int:
        """Repopulate the in-memory store from ``store.parquet`` if present.

        Called once during ``KlineStreamManager.start``. Returns the number of
        rows recovered. A missing or empty file is a no-op (store starts
        empty); a corrupt file is logged and skipped so the daemon still starts
        with a clean slate and the bootstrap path can repopulate.
        """
        if self._cache_root is None or self._flush_path is None:
            return 0
        if not self._flush_path.exists():
            return 0
        try:
            frame = pl.read_parquet(self._flush_path)
        except (OSError, pl.exceptions.PolarsError) as exc:
            _logger.warning("kline_store recovery read failed; starting empty: %s", exc)
            return 0
        if frame.is_empty():
            return 0
        if set(frame.columns) != set(_KLINE_SCHEMA):
            _logger.warning("kline_store recovery file has a non-canonical schema; ignoring")
            return 0
        try:
            frame = frame.select(list(_KLINE_SCHEMA)).cast(_KLINE_SCHEMA, strict=True)
        except (TypeError, ValueError, pl.exceptions.PolarsError) as exc:
            _logger.warning("kline_store recovery schema validation failed; ignoring: %s", exc)
            return 0
        if frame.filter(pl.col("source") != WS_STORE_SOURCE).height:
            _logger.warning("kline_store recovery file has a non-canonical source; ignoring")
            return 0
        with self._lock:
            recovered = 0
            max_ts = self._global_max_ts_ms
            # Same future-ts bound as _insert_bar: a corrupt far-future bar in
            # the flush file would advance _global_max_ts_ms and mass-evict the
            # store on startup.
            max_acceptable_ts = _utc_now_ms() + self._max_future_ts_slack_ms
            for row in frame.iter_rows(named=True):
                symbol = str(row.get("symbol", "") or "")
                if not symbol:
                    continue
                try:
                    ts_ms = int(row["ts_ms"])
                except (TypeError, ValueError, KeyError):
                    continue
                if ts_ms > max_acceptable_ts:
                    continue
                try:
                    bar = _Bar(
                        ts_ms=ts_ms,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume_base=float(row["volume_base"]),
                        turnover_quote=float(row["turnover_quote"]),
                        source=str(row["source"]),
                    )
                except (TypeError, ValueError, KeyError):
                    continue
                symbol_bars = self._bars.setdefault(symbol, {})
                symbol_bars[ts_ms] = bar
                recovered += 1
                if ts_ms > max_ts:
                    max_ts = ts_ms
            self._global_max_ts_ms = max_ts
            if self._bars and max_ts > 0:
                self._evict_old_locked(reference_ts_ms=max_ts)
                self._last_eviction_ref_ts_ms = max_ts
            # recovery adds bars without bumping _adds_total (the window-cache
            # version), so invalidate both caches explicitly. (Startup-only path.)
            self._window_cache = None
            self._stats_scan_cache = None
        return recovered

    # -- introspection --------------------------------------------------

    def has_symbol(self, symbol: str) -> bool:
        with self._lock:
            return bool(self._bars.get(symbol))

    def symbol_count(self) -> int:
        with self._lock:
            return len(self._bars)

    def row_count(self) -> int:
        with self._lock:
            return sum(len(symbol_bars) for symbol_bars in self._bars.values())
