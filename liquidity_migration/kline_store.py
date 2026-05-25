"""In-memory 1h kline store for the WS-driven kline-delivery path.

The cross-sectional momentum strategy fires at daily-bar close; its alpha
decays within ~1h. The legacy REST-pull path takes 3-4h to deliver fresh
1h klines to the feature pipeline (one REST round-trip per ~400 universe
symbols, rate-limited), so entries fire 3-4h after ready_ts and trade away
most of the post-pump reversion edge.

This module is the receiving end of the WS push path: a thread-safe in-memory
store keyed by (symbol, ts_ms) that the WS pool writes to as confirmed bars
land, and the cycle's feature build reads from instead of REST-fetching.

The store guarantees:

- Idempotent ``add_bar`` on ``(symbol, ts_ms)``. A bar that arrives twice
  (e.g. a reconnect that re-flushes the last bar of the previous slice) is a
  no-op overwrite, never a duplicate row.
- Bounded memory: bars older than ``retain_days`` are evicted on every add,
  so a long-running daemon's heap stays flat.
- Single-lock concurrency: one ``threading.RLock`` guards both the per-symbol
  bar dicts and the metadata maps. Read and write paths are short, so the
  lock is held for microseconds and contention is negligible at the ~673
  symbols × 24 bars/day × 30s flush cadence we operate at.
- Persistence round-trip: a background thread serialises the entire store to
  a single parquet file every ``flush_interval_seconds``. On restart, the
  store calls ``recover_from_disk()`` to repopulate from that file before the
  WS pool starts catching up.

The output schema of ``get_klines`` is intentionally identical to the schema
``_download_recent_1h_klines`` returns (``_empty_klines`` in event_demo.py):
columns ``ts_ms``, ``symbol``, ``open``, ``high``, ``low``, ``close``,
``volume_base``, ``turnover_quote``, ``source``. The integration in
``_download_recent_1h_klines`` is then a drop-in: hit the store for covered
symbols, REST-fall-back for the rest, concat.
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

from ._common import MS_PER_DAY, MS_PER_HOUR


_logger = logging.getLogger("liquidity_migration.kline_store")

# Eviction is amortized: each insert only re-scans for stale bars when the
# global newest_ts has advanced by at least this many ms since the last
# eviction. At the WS path's 1-bar-per-symbol-per-hour cadence this fires
# every hour; in bootstrap (which inserts bars OLDER than the current max)
# it never fires per-bar, so a 1083-bar symbol load is O(bars) instead of
# O(bars * symbols * bars_per_symbol). Stale bars accumulate for up to one
# eviction-window before being purged — at 1h that's a single hour of
# unrelated symbols × 567 symbols ≈ 567 bars ≈ 50KB of slack, acceptable.
_EVICTION_INTERVAL_MS = MS_PER_HOUR

WS_STORE_SOURCE = "bybit_ws_kline"

_KLINE_SCHEMA: dict[str, pl.DataType] = {
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
    """Best-effort parse of a single bar dict from pybit's WS kline payload.

    pybit forwards the venue's raw bar object in the ``data`` array of each
    message; the canonical fields are camelCase strings (``start``, ``open``,
    ``high``, ``low``, ``close``, ``volume``, ``turnover``). We also tolerate
    underscored names and numerics, so the store can be fed by alternative
    upstreams in tests without a translation shim.
    """

    def _pick(*names: str) -> Any:
        for name in names:
            if name in bar and bar[name] is not None:
                return bar[name]
        return None

    raw_ts = _pick("start", "ts_ms", "startTime", "t")
    if raw_ts is None:
        return None
    try:
        ts_ms = int(raw_ts)
    except (TypeError, ValueError):
        return None

    def _coerce(*names: str) -> float | None:
        raw = _pick(*names)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    open_ = _coerce("open", "o")
    high = _coerce("high", "h")
    low = _coerce("low", "l")
    close = _coerce("close", "c")
    volume = _coerce("volume", "volume_base", "v")
    turnover = _coerce("turnover", "turnover_quote", "q")
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
    ) -> None:
        if retain_days <= 0:
            raise ValueError("retain_days must be positive")
        if flush_interval_seconds < 0.0:
            raise ValueError("flush_interval_seconds must be non-negative")
        self._retain_ms = int(retain_days) * MS_PER_DAY
        self._retain_days = int(retain_days)
        self._flush_interval_seconds = float(flush_interval_seconds)
        self._lock = threading.RLock()
        self._bars: dict[str, dict[int, _Bar]] = {}
        self._adds_total = 0
        self._adds_skipped_unconfirmed = 0
        self._adds_evicted = 0
        self._reads_total = 0
        self._cache_root: Path | None = Path(cache_root).expanduser() if cache_root is not None else None
        if self._cache_root is not None:
            self._flush_dir = self._cache_root / ".cache" / "ws_klines"
            self._flush_path = self._flush_dir / "store.parquet"
        else:
            self._flush_dir = None
            self._flush_path = None
        self._flush_thread: threading.Thread | None = None
        self._flush_stop = threading.Event()
        self._flushes_total = 0
        self._flush_errors = 0
        self._last_flush_monotonic: float | None = None
        self._last_flush_rows = 0
        # Incremental newest-ts cache: maintained in _insert_bar / recovery
        # so _max_ts_with_new is O(1) instead of O(symbols * bars). At 567
        # symbols × ~1000 bars/symbol this took the bootstrap hot path from
        # tens of minutes to a few seconds.
        self._global_max_ts_ms: int = 0
        # Eviction is only rescanned when global_max_ts has advanced by at
        # least _EVICTION_INTERVAL_MS since the last sweep — see the constant
        # above for why this matters during bootstrap.
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

        Single lock acquire + single deferred eviction sweep at the end —
        per-bar eviction here was the bootstrap hot-spot before this
        rewrite (each insert ran a full-store scan; 1083 bars × 567
        symbols × O(N²) made the daemon take 20+ minutes per cold start).
        """
        if not symbol:
            return 0
        parsed_bars: list[_Bar] = []
        for bar in bars:
            parsed = _parse_ws_kline_event(bar)
            if parsed is not None:
                parsed_bars.append(parsed)
        if not parsed_bars:
            return 0
        accepted = 0
        with self._lock:
            reference_ts = max(self._global_max_ts_ms, max(p.ts_ms for p in parsed_bars))
            symbol_bars = self._bars.setdefault(symbol, {})
            for parsed in parsed_bars:
                if reference_ts - parsed.ts_ms > self._retain_ms:
                    continue
                symbol_bars[parsed.ts_ms] = parsed
                accepted += 1
            self._adds_total += accepted
            if reference_ts > self._global_max_ts_ms:
                self._global_max_ts_ms = reference_ts
            # Single amortized eviction at the end of the bulk load. During
            # cold-start bootstrap this is the only sweep for the whole
            # symbol; without it the loop above was effectively serialized
            # under the store lock and starved every other worker.
            if reference_ts - self._last_eviction_ref_ts_ms >= _EVICTION_INTERVAL_MS:
                self._evict_old_locked(reference_ts_ms=reference_ts)
                self._last_eviction_ref_ts_ms = reference_ts
        return accepted

    def _insert_bar(self, symbol: str, bar: _Bar) -> bool:
        if not symbol:
            return False
        with self._lock:
            # Cached global newest_ts replaces the prior O(symbols * bars)
            # scan. Reads + writes to _global_max_ts_ms happen only under
            # the store lock so the value is always consistent with _bars.
            reference_ts = self._global_max_ts_ms
            if bar.ts_ms > reference_ts:
                reference_ts = bar.ts_ms
            if reference_ts - bar.ts_ms > self._retain_ms:
                return False
            symbol_bars = self._bars.setdefault(symbol, {})
            symbol_bars[bar.ts_ms] = bar
            self._adds_total += 1
            if reference_ts > self._global_max_ts_ms:
                self._global_max_ts_ms = reference_ts
            # Amortized eviction — see _EVICTION_INTERVAL_MS comment. The WS
            # path inserts 1 bar/symbol/hour so this fires every hour; the
            # bootstrap path uses bootstrap_symbol() instead and only runs
            # one sweep per symbol.
            if reference_ts - self._last_eviction_ref_ts_ms >= _EVICTION_INTERVAL_MS:
                self._evict_old_locked(reference_ts_ms=reference_ts)
                self._last_eviction_ref_ts_ms = reference_ts
            return True

    def _evict_old_locked(self, *, reference_ts_ms: int) -> None:
        cutoff_ts = reference_ts_ms - self._retain_ms
        empty_symbols: list[str] = []
        for symbol, symbol_bars in self._bars.items():
            stale = [ts for ts in symbol_bars if ts < cutoff_ts]
            if stale:
                for ts in stale:
                    del symbol_bars[ts]
                self._adds_evicted += len(stale)
            if not symbol_bars:
                empty_symbols.append(symbol)
        for symbol in empty_symbols:
            del self._bars[symbol]

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
        # Column-major collection: building lists of primitives and handing
        # them to polars is ~3x faster than allocating one dict per bar and
        # then re-shaping in the DataFrame constructor. For a 584K-bar read
        # this drops from ~900ms → ~300ms — the cycle's klines stage was the
        # single largest item before the optimization.
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
        # of what the caller passed — matches the old .sort() behavior.
        sorted_symbols = sorted(symbols)
        with self._lock:
            self._reads_total += 1
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
        # We iterated outer-keyed by `symbols` and inner-keyed by sorted
        # ts_ms, so the columns are already in (symbol, ts_ms) order. Skip
        # the explicit .sort() — it would otherwise re-pay an O(N log N)
        # cost on the same data we just emitted in order.
        return pl.DataFrame(
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

        Called by the manager after the universe is set so the store
        doesn't keep paying memory for symbols outside the active
        universe (e.g. legacy data recovered from a prior daemon run
        that subscribed a wider universe before scoping landed). Bars
        for kept symbols are preserved exactly; this is a set-trim,
        not an eviction-by-age."""
        keep = set(symbols)
        dropped = 0
        with self._lock:
            stale = [s for s in self._bars if s not in keep]
            for symbol in stale:
                dropped += len(self._bars[symbol])
                del self._bars[symbol]
            if dropped > 0:
                self._adds_evicted += dropped
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

    def oldest_ts_ms(self) -> int | None:
        with self._lock:
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
            row_count = sum(len(symbol_bars) for symbol_bars in self._bars.values())
            newest = self.newest_ts_ms()
            oldest = self.oldest_ts_ms()
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
        primitive values into column lists — ~400ms before, ~50ms now for
        614K rows. Dict construction + DataFrame build + parquet write all
        happen WITHOUT the lock so WS bar inserts aren't stalled by the
        ~30s flush cadence.
        """
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
                self._last_flush_monotonic = time.monotonic()
            return 0
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
            with self._lock:
                self._flush_errors += 1
            _logger.warning("kline_store flush write failed: %s", exc)
            return 0
        with self._lock:
            self._flushes_total += 1
            self._last_flush_rows = len(ts_col)
            self._last_flush_monotonic = time.monotonic()
        return len(ts_col)

    def recover_from_disk(self) -> int:
        """Repopulate the in-memory store from ``store.parquet`` if present.

        Called once during ``KlineStreamManager.start``. Returns the number of
        rows recovered. A missing or empty file is a no-op (store starts
        empty); a corrupt file is logged and skipped so the daemon still
        starts with a clean slate and the bootstrap path can repopulate.
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
        missing = [name for name in _KLINE_SCHEMA if name not in frame.columns]
        if missing:
            _logger.warning("kline_store recovery file missing columns %s; ignoring", missing)
            return 0
        with self._lock:
            recovered = 0
            max_ts = self._global_max_ts_ms
            for row in frame.iter_rows(named=True):
                symbol = str(row.get("symbol", "") or "")
                if not symbol:
                    continue
                try:
                    ts_ms = int(row["ts_ms"])
                except (TypeError, ValueError, KeyError):
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
                        source=str(row.get("source") or WS_STORE_SOURCE),
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
