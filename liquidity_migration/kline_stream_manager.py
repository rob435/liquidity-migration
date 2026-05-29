"""Orchestrator for the WS-driven kline pipeline.

Wires three independent components into a single lifecycle:

- ``KlineStore`` (in-memory bars, periodic disk flush)
- ``BybitKlineStreamPool`` (multi-connection WS subscriptions)
- A bootstrap path that parallel-REST-fills history at startup
- A universe-refresh thread that polls instruments every hour for new
  listings + delistings and reconciles the pool's subscriptions

The cycle's ``_download_recent_1h_klines`` does NOT call this module
directly; it consumes the store via ``manager.store()``. The store is the
contract; the manager is the wiring that keeps it fresh.

Lifecycle:

  1. ``start()``:
     - recover from the flush file if present
     - subscribe the WS pool to the current symbol universe so live bars
       start flowing immediately
     - in parallel, bootstrap historical bars (last ``lookback_days``) for
       any symbol the store does not already cover. Block until
       ``bootstrap_completion_threshold`` of the universe is covered.
     - start the universe-refresh thread
     - start the watchdog + flush threads
  2. cycle reads via ``manager.store()`` — store has the data, REST fallback
     covers any symbol not yet present
  3. ``stop()`` tears everything down cleanly: refresh thread, watchdog,
     flush thread, pool, then one final flush so the next process restart
     recovers the latest state.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from ._common import MS_PER_DAY, MS_PER_HOUR
from .bybit import (
    BybitKlineStreamPool,
    BybitMarketData,
    BybitRestRateLimiter,
)
from .kline_store import KlineStore


_logger = logging.getLogger("liquidity_migration.kline_stream_manager")


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class _BootstrapResult:
    symbols_attempted: int = 0
    symbols_succeeded: int = 0
    symbols_skipped_already_covered: int = 0
    symbols_failed: int = 0
    bars_inserted: int = 0
    elapsed_seconds: float = 0.0
    last_error: str = ""


@dataclass(slots=True)
class KlineStreamManager:
    """Owns the store, pool, bootstrap, and universe-refresh thread.

    The manager is dependency-injectable so unit tests can swap out the REST
    market client (``market_data``), the pool (``pool``), and the universe
    fetcher (``universe_fetcher``) without touching the live exchange.
    """

    market_data: BybitMarketData
    cache_root: Path
    lookback_days: int = 45
    bootstrap_workers: int = 16
    universe_refresh_interval_seconds: float = 3600.0
    bootstrap_completion_threshold: float = 0.95
    bootstrap_timeout_seconds: float = 1200.0
    bootstrap_max_attempts_per_symbol: int = 2
    flush_interval_seconds: float = 30.0
    retain_days: int = 90
    interval_minutes: int = 60
    topics_per_connection: int = 180
    stale_warning_seconds: float = 60.0
    stale_reconnect_seconds: float = 180.0
    watchdog_interval_seconds: float = 10.0
    universe_fetcher: Callable[[], list[str]] | None = None
    pool: BybitKlineStreamPool | None = None
    store_factory: Callable[..., KlineStore] | None = None

    # internal state
    _store: KlineStore = field(init=False, repr=False)
    _refresh_thread: threading.Thread | None = field(init=False, repr=False, default=None)
    _refresh_stop: threading.Event = field(init=False, repr=False)
    _started: bool = field(init=False, repr=False, default=False)
    _stopped: bool = field(init=False, repr=False, default=False)
    _universe: set[str] = field(init=False, repr=False)
    _bootstrap_result: _BootstrapResult = field(init=False, repr=False)
    _universe_refreshes: int = field(init=False, repr=False, default=0)
    _universe_refresh_errors: int = field(init=False, repr=False, default=0)
    _last_universe_refresh_ms: int = field(init=False, repr=False, default=0)
    _start_time_monotonic: float = field(init=False, repr=False, default=0.0)
    _lock: threading.RLock = field(init=False, repr=False)
    # Cycle-wake signal: an Event the daemon's run loop waits on, set when a
    # NEW confirmed bar boundary lands (first symbol to deliver a new hour),
    # so the daemon fires its cycle the instant fresh data arrives (WS-event-
    # driven) instead of polling on a wall-clock timer. None = not wired
    # (legacy timer path / tests).
    _cycle_wake_event: threading.Event | None = field(init=False, repr=False, default=None)
    _max_confirmed_ts_ms: int = field(init=False, repr=False, default=0)

    def __post_init__(self) -> None:
        self.cache_root = Path(self.cache_root).expanduser()
        if self.lookback_days <= 0:
            raise ValueError("lookback_days must be positive")
        if not (0.0 < self.bootstrap_completion_threshold <= 1.0):
            raise ValueError("bootstrap_completion_threshold must be in (0, 1]")
        if self.bootstrap_workers <= 0:
            raise ValueError("bootstrap_workers must be positive")
        if self.universe_refresh_interval_seconds < 0.0:
            raise ValueError("universe_refresh_interval_seconds must be non-negative")
        store_factory = self.store_factory or _default_store_factory
        self._store = store_factory(
            cache_root=self.cache_root,
            retain_days=self.retain_days,
            flush_interval_seconds=self.flush_interval_seconds,
        )
        self._refresh_stop = threading.Event()
        self._universe = set()
        self._bootstrap_result = _BootstrapResult()
        self._lock = threading.RLock()
        if self.pool is None:
            self.pool = BybitKlineStreamPool(
                interval_minutes=self.interval_minutes,
                topics_per_connection=self.topics_per_connection,
                stale_warning_seconds=self.stale_warning_seconds,
                stale_reconnect_seconds=self.stale_reconnect_seconds,
                watchdog_interval_seconds=self.watchdog_interval_seconds,
            )

    # -- public API ----------------------------------------------------

    def store(self) -> KlineStore:
        return self._store

    def set_cycle_wake_event(self, event: threading.Event | None) -> None:
        """Register the Event the manager sets when a new confirmed bar boundary
        lands. The daemon's run loop waits on this to fire WS-event-driven
        cycles. Safe to call before or after start()."""
        self._cycle_wake_event = event

    def is_started(self) -> bool:
        return self._started and not self._stopped

    def start(self, *, shutdown_event: threading.Event | None = None) -> dict[str, Any]:
        """Start the manager: recover, bootstrap, subscribe WS, start threads.

        Blocks until ``bootstrap_completion_threshold`` of the universe is
        covered or ``bootstrap_timeout_seconds`` elapses, whichever first.
        If ``shutdown_event`` is supplied and gets set during bootstrap,
        the method returns early so the daemon can stop responsively
        instead of waiting for systemd's TimeoutStopSec to expire.

        **Ordering is intentional:** bootstrap runs BEFORE pool subscribe.
        Earlier ordering (pool first, then bootstrap) starved the REST
        bootstrap workers via WS event GIL pressure — 567 symbols took
        383s and only 100 succeeded before the deadline cancelled the
        remaining 467. Bootstrap-first lets REST run uncontested in
        ~100s; the brief window where a bar closes during bootstrap is
        recovered by the cycle's REST fallback on the next tick.
        """
        if self._started:
            return self._start_stats(blocked=False)
        self._started = True
        self._start_time_monotonic = time.monotonic()
        recovered = self._store.recover_from_disk()
        if recovered:
            _logger.info("kline_store recovered %d rows from flush file", recovered)
        universe = self._fetch_universe()
        with self._lock:
            self._universe = set(universe)
        # Trim the recovered store to the active universe — a prior
        # daemon run may have subscribed a wider universe (e.g. before
        # universe scoping landed on the long sleeve), and those legacy
        # bars would otherwise sit in memory for 90 days waiting on
        # retain_days eviction. Skipped when the universe is empty so
        # a transient REST blip on the universe fetch doesn't blow the
        # store away — the empty-universe-fetch protection in
        # force_refresh_universe applies the same logic at runtime.
        if self._universe:
            dropped = self._store.keep_only_symbols(self._universe)
            if dropped:
                _logger.info(
                    "kline_store trimmed %d legacy rows outside the %d-symbol universe",
                    dropped, len(self._universe),
                )
        if shutdown_event is not None and shutdown_event.is_set():
            _logger.info("kline_stream_manager start aborted: shutdown requested before bootstrap")
            return self._start_stats(blocked=False)
        self._bootstrap_universe(self._universe, shutdown_event=shutdown_event)
        if shutdown_event is not None and shutdown_event.is_set():
            _logger.info("kline_stream_manager start aborted: shutdown requested after bootstrap")
            return self._start_stats(blocked=True)
        self._subscribe_pool(self._universe)
        if self.flush_interval_seconds > 0.0:
            self._store.start_flush_thread()
        if self.pool is not None:
            self.pool.start_watchdog()
        if self.universe_refresh_interval_seconds > 0.0:
            self._start_refresh_thread()
        return self._start_stats(blocked=True)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._refresh_stop.set()
        refresh = self._refresh_thread
        self._refresh_thread = None
        if refresh is not None:
            refresh.join(timeout=5.0)
        if self.pool is not None:
            try:
                self.pool.close()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("pool.close failed: %s", exc)
        try:
            self._store.stop_flush_thread()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("store.stop_flush_thread failed: %s", exc)
        # One last flush so a clean restart picks up the latest state.
        try:
            self._store.flush_to_disk()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("final flush failed: %s", exc)

    def universe_symbols(self) -> list[str]:
        """Return the manager's current universe as a sorted list.

        Used by daemons that want to keep their ticker WS subscriptions
        in sync with the kline universe (e.g. long sleeve scopes both to
        the top-N rather than all USDT-perps). Snapshot taken under the
        lock so concurrent universe_refresh doesn't tear the view."""
        with self._lock:
            return sorted(self._universe)

    def force_refresh_universe(self) -> dict[str, int]:
        """Synchronously re-fetch the universe + diff against the pool.

        Exposed for tests and operator-triggered manual refresh."""
        new_universe = set(self._fetch_universe())
        # An empty fetch is almost always a transient REST failure (the
        # default fetcher returns [] on exception, the long fetcher
        # returns [] when the tickers REST call fails). Treating "no
        # symbols" as "all symbols delisted" would unsubscribe the pool
        # from every kline topic, killing the live feed until the next
        # refresh succeeds — a single REST blip would silently sever
        # the WS pipeline. Skip the diff in that case; existing
        # subscriptions stay live, and the next refresh tick retries.
        if not new_universe:
            with self._lock:
                size = len(self._universe)
            _logger.warning(
                "universe refresh returned empty set; keeping existing %d subscriptions",
                size,
            )
            self._universe_refresh_errors += 1
            self._last_universe_refresh_ms = _utc_now_ms()
            return {"added": 0, "removed": 0, "size": size}
        with self._lock:
            previous = set(self._universe)
            additions = new_universe - previous
            removals = previous - new_universe
            self._universe = new_universe
        if self.pool is not None and (additions or removals):
            try:
                self.pool.update_subscriptions(new_universe)
            except Exception as exc:  # noqa: BLE001
                _logger.exception("pool.update_subscriptions failed: %s", exc)
        # Bootstrap any newly-added symbols (one REST call each).
        if additions:
            self._bootstrap_universe(additions, label="universe-refresh")
        self._universe_refreshes += 1
        self._last_universe_refresh_ms = _utc_now_ms()
        return {
            "added": len(additions),
            "removed": len(removals),
            "size": len(new_universe),
        }

    def stats(self) -> dict[str, Any]:
        store_stats = self._store.stats()
        pool_stats = self.pool.stats() if self.pool is not None else {}
        # Newest-ts lag is the headline operational metric: are we receiving
        # fresh bars or has the WS pipeline silently stalled?
        newest_ts_ms = store_stats.get("newest_ts_ms")
        if newest_ts_ms is None:
            newest_ts_lag_seconds: float | None = None
        else:
            newest_ts_lag_seconds = max((_utc_now_ms() - int(newest_ts_ms)) / 1000.0, 0.0)
        return {
            "started": self._started,
            "stopped": self._stopped,
            "universe_size": len(self._universe),
            "universe_refreshes": self._universe_refreshes,
            "universe_refresh_errors": self._universe_refresh_errors,
            "last_universe_refresh_ms": self._last_universe_refresh_ms,
            "newest_ts_lag_seconds": newest_ts_lag_seconds,
            "store": store_stats,
            "pool": pool_stats,
            "bootstrap": {
                "symbols_attempted": self._bootstrap_result.symbols_attempted,
                "symbols_succeeded": self._bootstrap_result.symbols_succeeded,
                "symbols_skipped_already_covered": self._bootstrap_result.symbols_skipped_already_covered,
                "symbols_failed": self._bootstrap_result.symbols_failed,
                "bars_inserted": self._bootstrap_result.bars_inserted,
                "elapsed_seconds": self._bootstrap_result.elapsed_seconds,
                "last_error": self._bootstrap_result.last_error,
            },
        }

    # -- internals ------------------------------------------------------

    def _subscribe_pool(self, symbols: set[str]) -> None:
        if self.pool is None:
            return
        try:
            self.pool.subscribe(sorted(symbols), self._on_bar)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("pool.subscribe failed (continuing with REST fallback): %s", exc)

    def _on_bar(self, symbol: str, bar: dict[str, Any], confirmed: bool) -> None:
        """Pool → store fan-in. One call per WS bar.

        When a CONFIRMED bar lands that advances to a NEW bar boundary (i.e. the
        first symbol to deliver a fresh hour), set the cycle-wake Event so the
        daemon fires its cycle immediately. Gating on the boundary advance means
        the ~566-symbol burst at each hour close coalesces into a SINGLE wake,
        not one per symbol — no debounce storm. Runs on pybit's WS thread, so
        the work is just an int compare + an O(1) Event.set()."""
        inserted = self._store.add_bar(symbol, bar, confirmed=confirmed)
        if not (confirmed and inserted) or self._cycle_wake_event is None:
            return
        try:
            bar_ts = int(bar.get("start") or bar.get("ts_ms") or bar.get("startTime") or 0)
        except (TypeError, ValueError):
            return
        # Only advance the wake high-water mark for a boundary at/behind the
        # present hour. A bar timestamped > 1h ahead (clock skew / malformed frame
        # — the store accepts up to 2h ahead for storage) would otherwise poison
        # _max_confirmed_ts_ms and suppress every genuine boundary wake until
        # wall-clock caught up (degrading to heartbeat cadence). Such a bar was
        # still stored above; it just must not gate the cycle-wake.
        if self._max_confirmed_ts_ms < bar_ts <= _utc_now_ms() + MS_PER_HOUR:
            self._max_confirmed_ts_ms = bar_ts
            self._cycle_wake_event.set()

    def _fetch_universe(self) -> list[str]:
        if self.universe_fetcher is not None:
            return list(self.universe_fetcher())
        try:
            rows = self.market_data.get_instruments_info()
        except Exception as exc:  # noqa: BLE001
            self._universe_refresh_errors += 1
            _logger.warning("universe fetch failed: %s", exc)
            return []
        return _default_universe_filter(rows)

    def _bootstrap_universe(
        self,
        symbols: set[str] | list[str] | None,
        *,
        label: str = "bootstrap",
        shutdown_event: threading.Event | None = None,
    ) -> None:
        if not symbols:
            return
        symbols_list = sorted(symbols)
        start = time.monotonic()
        # "Already covered" must mean coverage of the FULL lookback window,
        # not just the most recent bar. A daemon that just recovered a flush
        # file with the latest hour but nothing older would otherwise skip
        # bootstrap and operate on a near-empty store indefinitely. Check
        # both ends of the window so bootstrap re-fills any historical gap.
        now_ms = _utc_now_ms()
        recent_bar_ts_ms = _floor_hour_ms(now_ms) - MS_PER_HOUR
        lookback_ms = self.lookback_days * MS_PER_DAY
        end_ms = recent_bar_ts_ms
        start_ms = end_ms - lookback_ms
        already_covered = self._store.symbols_with_coverage_in_window(
            start_ms=start_ms, end_ms=end_ms,
        )
        targets = [s for s in symbols_list if s not in already_covered]
        skipped = len(symbols_list) - len(targets)
        self._bootstrap_result.symbols_attempted += len(symbols_list)
        self._bootstrap_result.symbols_skipped_already_covered += skipped
        if not targets:
            self._bootstrap_result.elapsed_seconds = round(time.monotonic() - start, 3)
            _logger.info(
                "%s skipped: all %d symbols already covered", label, len(symbols_list),
            )
            return
        deadline = start + self.bootstrap_timeout_seconds
        # A separate rate-limiter for bootstrap so it doesn't fight the cycle's
        # demo rate-limiter at startup; conservative defaults so the bootstrap
        # uses ~half the per-IP budget.
        shared_limiter = BybitRestRateLimiter(max_requests=12, per_seconds=1.0)
        threshold_count = max(
            int(len(self._universe) * self.bootstrap_completion_threshold),
            1,
        )
        succeeded = 0
        failed = 0
        bars_inserted = 0
        last_error = ""
        threshold_logged = False
        # ThreadPoolExecutor with as_completed iterates through every result
        # so the stats accurately reflect what's in the store. The "early
        # exit" log is informational only — the executor's `with` block
        # blocks until every future completes regardless of `break`, so an
        # early-exit-then-break would only undercount stats without saving
        # time. If true non-blocking start is needed, the executor would
        # have to live past this method (significant refactor).
        with ThreadPoolExecutor(
            max_workers=self.bootstrap_workers,
            thread_name_prefix="kline-bootstrap",
        ) as executor:
            futures = {
                executor.submit(
                    self._bootstrap_symbol,
                    symbol,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    shared_limiter=shared_limiter,
                ): symbol
                for symbol in targets
            }
            shutdown_triggered = False
            for future in _as_completed_with_deadline(
                futures, deadline, shutdown_event=shutdown_event,
            ):
                if shutdown_event is not None and shutdown_event.is_set():
                    # Daemon shutdown requested mid-bootstrap. Cancel every
                    # remaining future so the executor's `with` block exits
                    # quickly instead of waiting on slow REST calls — this
                    # is what was triggering systemd's 90s SIGKILL.
                    shutdown_triggered = True
                    for f in list(futures):
                        f.cancel()
                    break
                symbol = futures[future]
                try:
                    inserted = future.result()
                    if inserted > 0:
                        succeeded += 1
                        bars_inserted += inserted
                    else:
                        # No bars returned: treat as failure so REST fallback
                        # picks it up on cycle.
                        failed += 1
                except Exception as exc:  # noqa: BLE001 - bootstrap is best-effort
                    failed += 1
                    last_error = f"{symbol}: {exc}"[:240]
                # Log when we cross the completion threshold for visibility,
                # but keep iterating so every future's result is counted.
                if not threshold_logged:
                    covered_now = len(self._store.symbols_with_coverage_through(recent_bar_ts_ms))
                    if covered_now >= threshold_count and time.monotonic() > start + 1.0:
                        _logger.info(
                            "%s completion threshold %.0f%% reached with %d/%d "
                            "symbols covered; remaining %d still running",
                            label,
                            self.bootstrap_completion_threshold * 100.0,
                            covered_now,
                            len(self._universe),
                            len(futures) - succeeded - failed,
                        )
                        threshold_logged = True
            if shutdown_triggered:
                _logger.info(
                    "%s aborted: shutdown requested with %d/%d done",
                    label, succeeded + failed, len(targets),
                )
        elapsed = time.monotonic() - start
        self._bootstrap_result.symbols_succeeded += succeeded
        self._bootstrap_result.symbols_failed += failed
        self._bootstrap_result.bars_inserted += bars_inserted
        self._bootstrap_result.elapsed_seconds = round(elapsed, 3)
        if last_error:
            self._bootstrap_result.last_error = last_error
        _logger.info(
            "%s complete: targets=%d succeeded=%d failed=%d bars=%d elapsed=%.1fs",
            label, len(targets), succeeded, failed, bars_inserted, elapsed,
        )

    def _bootstrap_symbol(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int,
        shared_limiter: BybitRestRateLimiter,
    ) -> int:
        """Fetch + insert one symbol's history. Returns bars inserted."""
        last_exc: Exception | None = None
        for attempt in range(max(self.bootstrap_max_attempts_per_symbol, 1)):
            try:
                shared_limiter.acquire()
                rows = self.market_data.get_klines(
                    symbol, str(self.interval_minutes), start_ms, end_ms,
                )
                bars = [_kline_row_to_bar_dict(row) for row in rows]
                return self._store.bootstrap_symbol(symbol, bars)
            except Exception as exc:  # noqa: BLE001 - retry once then escalate
                last_exc = exc
                if attempt + 1 < self.bootstrap_max_attempts_per_symbol:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise
        if last_exc is not None:  # pragma: no cover - loop always returns or raises
            raise last_exc
        return 0

    def _start_refresh_thread(self) -> None:
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name="kline-universe-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def _refresh_loop(self) -> None:
        while not self._refresh_stop.wait(timeout=self.universe_refresh_interval_seconds):
            try:
                self.force_refresh_universe()
            except Exception as exc:  # noqa: BLE001
                self._universe_refresh_errors += 1
                _logger.exception("universe refresh failed: %s", exc)

    def _start_stats(self, *, blocked: bool) -> dict[str, Any]:
        stats = self.stats()
        stats["blocked_on_bootstrap"] = blocked
        return stats


# -- helpers -----------------------------------------------------------


def _default_store_factory(*, cache_root: Path, retain_days: int, flush_interval_seconds: float) -> KlineStore:
    return KlineStore(
        cache_root=cache_root,
        retain_days=retain_days,
        flush_interval_seconds=flush_interval_seconds,
    )


def _default_universe_filter(rows: list[dict[str, Any]]) -> list[str]:
    """Active linear USDT-perp symbols by venue status."""
    symbols: list[str] = []
    for row in rows:
        status = row.get("status")
        quote = row.get("quoteCoin") or row.get("quote_coin")
        settle = row.get("settleCoin") or row.get("settle_coin")
        contract_type = row.get("contractType") or row.get("contract_type")
        is_prelisting = bool(row.get("isPreListing") or row.get("is_prelisting"))
        if status != "Trading" or is_prelisting:
            continue
        if quote != "USDT" or settle != "USDT":
            continue
        if contract_type not in (None, "LinearPerpetual", "Linear", "linear"):
            continue
        symbol = row.get("symbol")
        if isinstance(symbol, str) and symbol:
            symbols.append(symbol)
    return sorted(set(symbols))


def _kline_row_to_bar_dict(row: dict[str, Any]) -> dict[str, Any]:
    """The cycle's _normalize_klines output is the canonical shape, but
    REST returns lists. ``BybitMarketData.get_klines`` already returns rows
    as the venue's list[str] form via the raw payload. We convert into the
    store's expected dict (matching the WS bar shape) here."""
    if isinstance(row, dict):
        # Already-normalised — _normalize_klines path.
        return {
            "start": row.get("ts_ms"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume_base"),
            "turnover": row.get("turnover_quote"),
        }
    # pybit raw list shape: [ts, open, high, low, close, volume, turnover]
    return {
        "start": row[0],
        "open": row[1],
        "high": row[2],
        "low": row[3],
        "close": row[4],
        "volume": row[5],
        "turnover": row[6],
    }


def _as_completed_with_deadline(
    futures: dict,
    deadline: float,
    *,
    shutdown_event: threading.Event | None = None,
):
    """Like ``concurrent.futures.as_completed`` but bounded by ``deadline``
    (a monotonic timestamp). Yields futures as they complete; stops when the
    deadline is past.

    Caps the per-call ``wait`` timeout at 1s and (when given a
    ``shutdown_event``) checks it on every poll tick so a stalled REST
    burst can't leave the bootstrap unresponsive to SIGTERM until the
    next completion. The caller's per-yield shutdown check only fires
    when a future is yielded — without this internal check, a worker
    pool stuck in a slow REST batch could delay shutdown by tens of
    seconds (each REST call's full duration)."""
    remaining = set(futures)
    while remaining:
        timeout = max(deadline - time.monotonic(), 0.0)
        timeout = min(timeout, 1.0)
        from concurrent.futures import wait, FIRST_COMPLETED
        done, _ = wait(remaining, timeout=timeout, return_when=FIRST_COMPLETED)
        if not done:
            # Poll tick. Surface shutdown immediately by stopping the
            # generator — the executor's `with` block will then cancel
            # the in-flight futures.
            if shutdown_event is not None and shutdown_event.is_set():
                return
            if time.monotonic() < deadline:
                continue
            # Deadline reached: cancel pending and stop.
            for fut in remaining:
                fut.cancel()
            return
        for fut in done:
            remaining.discard(fut)
            yield fut


def _floor_hour_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % MS_PER_HOUR)
