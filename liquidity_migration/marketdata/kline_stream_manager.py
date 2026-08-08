"""Orchestrator for the WS-driven kline pipeline.

Wires four components into a single lifecycle:

- ``KlineStore`` (in-memory bars, periodic disk flush)
- ``BybitKlineStreamPool`` (multi-connection WS subscriptions)
- A bootstrap path that parallel-REST-fills history at startup
- A universe-refresh thread that polls instruments hourly for listings and
  delistings and reconciles the pool's subscriptions

The cycle's ``_download_recent_1h_klines`` consumes the store via
``manager.store()`` rather than calling this module, and REST fallback covers
any symbol not yet present. The store is the contract; the manager is the
wiring that keeps it fresh.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from liquidity_migration.core._common import MS_PER_HOUR, exact_duration_ms
from liquidity_migration.marketdata.bybit_market_data import (
    BybitKlineStreamPool,
    BybitMarketData,
    BybitRestRateLimiter,
)
from liquidity_migration.marketdata.kline_store import KlineStore


_logger = logging.getLogger("liquidity_migration.marketdata.kline_stream_manager")


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
    # Per-IP REST budget for the bootstrap. get_klines paginates, so the limiter
    # must sit on the market client (one acquire per HTTP call), not per symbol.
    bootstrap_rest_max_requests: int = 12
    bootstrap_rest_per_seconds: float = 1.0
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
    _lock: threading.RLock = field(init=False, repr=False)
    # Set when a NEW confirmed bar boundary lands, so the daemon's run loop
    # fires on fresh data instead of a wall-clock timer. None until wired.
    _cycle_wake_event: threading.Event | None = field(init=False, repr=False, default=None)
    _max_confirmed_ts_ms: int = field(init=False, repr=False, default=0)
    # Shared REST limiter wired onto the market client so EACH paginated _get
    # acquires once (see ratelimit-rest-1). Created in __post_init__.
    _bootstrap_limiter: BybitRestRateLimiter = field(init=False, repr=False)

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
        # The store must retain at least the bootstrap lookback: eviction is
        # anchored to the NEWEST bar, so a retention shorter than the lookback
        # evicts the window head as it lands and the reader's full-window
        # check can never pass for a name older than retention (observed live
        # 2026-08-03: LONG served 4/120 symbols). +1 day so the head is never
        # clipped between hourly refreshes.
        self.retain_days = max(int(self.retain_days), int(self.lookback_days) + 1)
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
        # Shared REST limiter on the market client so every paginated
        # get_klines call is throttled. A caller-supplied limiter wins.
        self._bootstrap_limiter = BybitRestRateLimiter(
            max_requests=self.bootstrap_rest_max_requests,
            per_seconds=self.bootstrap_rest_per_seconds,
        )
        if self.market_data.rate_limiter is None:
            self.market_data.rate_limiter = self._bootstrap_limiter
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

    def start(self, *, shutdown_event: threading.Event | None = None) -> dict[str, Any]:
        """Start the manager: recover, bootstrap, subscribe WS, start threads.

        Blocks until ``bootstrap_completion_threshold`` of the universe is
        covered or ``bootstrap_timeout_seconds`` elapses, whichever first.
        If ``shutdown_event`` is supplied and gets set during bootstrap,
        the method returns early so the daemon can stop responsively
        instead of waiting for systemd's TimeoutStopSec to expire.

        Ordering matters: bootstrap runs BEFORE the pool subscribe, because WS
        event GIL pressure starves the REST bootstrap workers otherwise. A bar
        closing during bootstrap is recovered by the cycle's REST fallback.
        """
        if self._started:
            return self._start_stats(blocked=False)
        self._started = True
        recovered = self._store.recover_from_disk()
        if recovered:
            _logger.info("kline_store recovered %d rows from flush file", recovered)
        universe = self._fetch_universe()
        with self._lock:
            self._universe = set(universe)
        # Trim the recovered store to the active universe: a prior run may have
        # subscribed a wider one, and those bars would otherwise wait out
        # retain_days in memory. Skipped on an empty universe so a transient
        # REST blip cannot wipe the store.
        if self._universe:
            dropped = self._store.keep_only_symbols(self._universe)
            if dropped:
                _logger.info(
                    "kline_store trimmed %d rows outside the %d-symbol universe",
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
        """The current universe, sorted. Daemons keep their ticker WS
        subscriptions in sync with it. Snapshot taken under the lock so a
        concurrent universe refresh cannot tear the view."""
        with self._lock:
            return sorted(self._universe)

    def force_refresh_universe(self) -> dict[str, int]:
        """Synchronously re-fetch the universe + diff against the pool.

        Exposed for tests and operator-triggered manual refresh."""
        # Snapshot the counter so the empty-set guard does not double-count a
        # fetch error already recorded by the default fetcher.
        errors_before_fetch = self._universe_refresh_errors
        new_universe = set(self._fetch_universe())
        # An empty fetch is almost always a transient REST failure; reading it
        # as "all symbols delisted" would unsubscribe the pool from every kline
        # topic. Skip the diff, keep the subscriptions, retry next tick.
        if not new_universe:
            with self._lock:
                size = len(self._universe)
            _logger.warning(
                "universe refresh returned empty set; keeping existing %d subscriptions",
                size,
            )
            # Count an empty custom/default result exactly once.
            if self._universe_refresh_errors == errors_before_fetch:
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
        # Bootstrap newly-added symbols. Passing _refresh_stop lets a SIGTERM
        # mid-refresh cancel the in-flight REST worker pool instead of orphaning it.
        if additions:
            self._bootstrap_universe(
                additions, label="universe-refresh", shutdown_event=self._refresh_stop,
            )
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
            # Unclamped on purpose. A negative lag means the newest stored bar
            # is stamped ahead of local now — the host clock is behind the
            # venue — and that is the one condition under which the cycle's
            # whole window can be an hour or more behind the market. Clamping
            # it to 0.0 reported "perfectly fresh" at exactly that moment.
            newest_ts_lag_seconds = (_utc_now_ms() - int(newest_ts_ms)) / 1000.0
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

        A confirmed bar that advances to a NEW bar boundary sets the cycle-wake
        Event. Gating on the boundary coalesces the hourly whole-universe burst
        into a single wake. Runs on pybit's WS thread, so the work is one int
        compare plus an O(1) Event.set()."""
        inserted = self._store.add_bar(symbol, bar, confirmed=confirmed)
        if not (confirmed and inserted) or self._cycle_wake_event is None:
            return
        try:
            bar_ts = int(bar.get("start") or bar.get("ts_ms") or bar.get("startTime") or 0)
        except (TypeError, ValueError):
            return
        # Only advance the wake high-water mark for a boundary at/behind the
        # present hour: a bar >1h ahead (clock skew, malformed frame) is still
        # stored but would otherwise suppress every genuine wake until wall
        # clock caught up. _on_bar runs on N pybit WS threads, so the
        # compare-and-set must be under the lock; set the Event outside it.
        wake = False
        with self._lock:
            if self._max_confirmed_ts_ms < bar_ts <= _utc_now_ms() + MS_PER_HOUR:
                self._max_confirmed_ts_ms = bar_ts
                wake = True
        if wake:
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
        # "Already covered" means the FULL lookback window, not just the most
        # recent bar: a store holding only the latest hour would otherwise skip
        # bootstrap forever. Check both ends so historical gaps get re-filled.
        now_ms = _utc_now_ms()
        recent_bar_ts_ms = _floor_hour_ms(now_ms) - MS_PER_HOUR
        lookback_ms = exact_duration_ms(days=self.lookback_days)
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
        # Completion threshold is measured against the set actually being
        # bootstrapped (``targets``), not the full universe: on the
        # universe-refresh path ``symbols`` is only the new listings, and a
        # full-universe denominator is either trivially met or unreachable.
        target_set = set(targets)
        threshold_count = max(
            int(len(targets) * self.bootstrap_completion_threshold),
            1,
        )
        succeeded = 0
        failed = 0
        bars_inserted = 0
        last_error = ""
        threshold_logged = False
        # as_completed iterates every result so the stats reflect the store.
        # The "early exit" log is informational: the `with` block blocks until
        # every future completes regardless, so breaking early would only
        # undercount stats. A non-blocking start needs the executor to outlive
        # this method.
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
                ): symbol
                for symbol in targets
            }
            shutdown_triggered = False
            for future in _as_completed_with_deadline(
                futures, deadline, shutdown_event=shutdown_event,
            ):
                if shutdown_event is not None and shutdown_event.is_set():
                    # Shutdown mid-bootstrap: cancel the remaining futures so
                    # the `with` block exits before systemd's SIGKILL timeout
                    # rather than waiting on slow REST calls.
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
                    covered_now = len(
                        self._store.symbols_with_coverage_through(recent_bar_ts_ms) & target_set
                    )
                    if covered_now >= threshold_count and time.monotonic() > start + 1.0:
                        _logger.info(
                            "%s completion threshold %.0f%% reached with %d/%d "
                            "symbols covered; remaining %d still running",
                            label,
                            self.bootstrap_completion_threshold * 100.0,
                            covered_now,
                            len(targets),
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
    ) -> int:
        """Fetch + insert one symbol's history. Returns bars inserted.

        Rate-limiting is NOT done here: the shared limiter is wired onto
        ``self.market_data`` (__post_init__), so each paginated get_klines HTTP
        call acquires once. A manual acquire-per-symbol under-counted the multi-page
        fetches and let bootstrap run at ~2x the per-IP budget (ratelimit-rest-1)."""
        last_exc: Exception | None = None
        # ``end_ms`` is the newest CLOSED bar's open (inclusive intent), while
        # get_klines' end is EXCLUSIVE — without the +interval the bootstrap
        # can never fetch its newest target bar, and a symbol missing only
        # that bar re-fetches zero rows on every restart (live 2026-08-04).
        interval_ms = self.interval_minutes * 60_000
        for attempt in range(max(self.bootstrap_max_attempts_per_symbol, 1)):
            try:
                rows = self.market_data.get_klines(
                    symbol, str(self.interval_minutes), start_ms, end_ms + interval_ms,
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
        quote = row.get("quoteCoin")
        settle = row.get("settleCoin")
        contract_type = row.get("contractType")
        is_prelisting = bool(row.get("isPreListing"))
        if status != "Trading" or is_prelisting:
            continue
        if quote != "USDT" or settle != "USDT":
            continue
        if contract_type != "LinearPerpetual":
            continue
        symbol = row.get("symbol")
        if isinstance(symbol, str) and symbol:
            symbols.append(symbol)
    return sorted(set(symbols))


def _kline_row_to_bar_dict(row: list[Any]) -> dict[str, Any]:
    """Convert Bybit's seven-field REST array to the WS bar shape."""
    if len(row) != 7:
        raise ValueError("Bybit kline row must contain exactly seven fields")
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
    ``shutdown_event``) checks it every poll tick, because the caller's
    per-yield shutdown check only fires when a future is yielded — a worker
    pool stuck in a slow REST batch could delay shutdown by tens of
    seconds (each REST call's full duration)."""
    remaining = set(futures)
    while remaining:
        timeout = max(deadline - time.monotonic(), 0.0)
        timeout = min(timeout, 1.0)
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
