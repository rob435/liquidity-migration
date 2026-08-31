"""Credential-free Bybit public REST market-data client."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from liquidity_migration.marketdata.bybit_errors import (
    BybitDataError,
    is_rate_limit as _is_rate_limit,
    is_transient_venue_fault as _is_transient_venue_fault,
)
try:
    from pybit.unified_trading import HTTP
except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
    HTTP = None


class _PybitRateLimitLogFilter(logging.Filter):
    """Drop pybit's 10006 (rate limit) retry chatter.

    pybit logs at ERROR twice per 10006 retry, which at ~180 symbols hitting the
    kline endpoint on the hour is 10K-22K identical lines per minute. The
    retries themselves work; only the 10006-specific lines are filtered.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return (
            "ErrCode: 10006" not in message
            and "Hit the API rate limit" not in message
            and "API rate limit will reset" not in message
        )


# pybit instantiates its logger lazily on the first HTTP() call, but addFilter
# on the named-logger handle is idempotent and order-independent.
logging.getLogger("pybit._http_manager").addFilter(_PybitRateLimitLogFilter())


_logger_market_data = logging.getLogger("liquidity_migration.marketdata.bybit")


class BybitRestRateLimiter:
    """Thread-safe sliding-window rate limiter shared across BybitMarketData.

    Bybit public REST allows ~120 requests / 5s per IP per category; the default
    18 req/s keeps concurrent workers off sustained 429s, which pybit handles by
    sleeping 2s per retry. No waiting or lock contention under budget.
    """

    __slots__ = ("_max", "_per", "_timestamps", "_lock", "_throttle_events", "_throttled_seconds")

    def __init__(self, max_requests: int = 18, per_seconds: float = 1.0) -> None:
        if max_requests <= 0:
            raise ValueError("max_requests must be positive")
        if per_seconds <= 0.0:
            raise ValueError("per_seconds must be positive")
        self._max = max_requests
        self._per = per_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()
        self._throttle_events = 0
        self._throttled_seconds = 0.0

    def acquire(self) -> None:
        # Compute the wait UNDER the lock, sleep OUTSIDE it, then re-acquire and
        # re-check: sleeping under the lock serialises the whole shared REST
        # worker pool. A slot is still only claimed when len < max.
        #
        # Stats count once per blocking acquire, accumulating slept time across
        # re-loops; per-loop counting inflates throttled_seconds, the metric used
        # to size the REST budget.
        slept = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self._per
                # `<=`, not `<`: a slot exactly at the window edge has aged out,
                # and leaving it gives wait<=0 and a busy-spin until the clock
                # passes the boundary.
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    if slept > 0.0:
                        self._throttle_events += 1
                        self._throttled_seconds += slept
                    return
                wait = self._per - (now - self._timestamps[0])
                if wait <= 0.0:
                    # Window boundary: the oldest slot rolls off on the next
                    # pop, so re-evaluate without sleeping or double-counting.
                    continue
            time.sleep(wait)
            slept += wait

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_requests": self._max,
                "per_seconds": self._per,
                "throttle_events": self._throttle_events,
                "throttled_seconds": round(self._throttled_seconds, 3),
            }

INTERVAL_MS = {
    "1": 60_000,
    "3": 3 * 60_000,
    "5": 5 * 60_000,
    "15": 15 * 60_000,
    "30": 30 * 60_000,
    "60": 60 * 60_000,
    "120": 2 * 60 * 60_000,
    "240": 4 * 60 * 60_000,
    "360": 6 * 60 * 60_000,
    "720": 12 * 60 * 60_000,
    "D": 24 * 60 * 60_000,
}


def _raise_on_bracketed_empty_window(
    method_name: str,
    symbol: str,
    window_row_counts: list[int],
    *,
    start: int,
    end: int,
) -> None:
    """Refuse a hole strictly inside the fetched range.

    Windows before listing and after delisting are legitimately empty, but one
    that returned nothing between two that returned rows is a dropped range and
    must fail rather than be sealed with a completeness marker.
    """

    if not any(window_row_counts):
        return
    first = next(index for index, count in enumerate(window_row_counts) if count)
    last = len(window_row_counts) - 1 - next(
        index for index, count in enumerate(reversed(window_row_counts)) if count
    )
    if any(count == 0 for count in window_row_counts[first:last]):
        raise BybitDataError(
            f"Bybit {method_name} returned an empty window mid-range for {symbol} "
            f"(startTime={start}, endTime={end}, windows={window_row_counts}); "
            f"refusing to truncate the fetch silently"
        )


@dataclass(slots=True)
class BybitMarketData:
    category: str = "linear"
    testnet: bool = False
    # Public reads may be pinned to api-demo.bybit.com for a maintenance
    # snapshot; strategies normally leave this false. `demo` and `testnet` are
    # distinct Bybit environments and cannot be combined.
    demo: bool = False
    retries: int = 3
    retry_sleep_seconds: float = 0.5
    slow_call_threshold_ms: float = 1000.0
    rate_limiter: BybitRestRateLimiter | None = None
    logical_calls: int = field(init=False, default=0)
    http_calls: int = field(init=False, default=0)
    retry_events: int = field(init=False, default=0)
    rate_limit_events: int = field(init=False, default=0)
    error_events: int = field(init=False, default=0)
    slow_calls: int = field(init=False, default=0)
    total_call_ms: float = field(init=False, default=0.0)
    slow_call_ms: float = field(init=False, default=0.0)
    last_error: str = field(init=False, default="")
    _client: Any = field(init=False, repr=False)
    # The bootstrap worker pool shares ONE BybitMarketData, so every counter
    # mutation is a concurrent read-modify-write and lost increments make
    # stats() under-report. The lock wraps only the arithmetic, never the call.
    _stats_lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if HTTP is None:
            raise RuntimeError("pybit is required for BybitMarketData")
        if self.testnet and self.demo:
            raise ValueError("Bybit market data cannot select both testnet and demo")
        self._stats_lock = threading.Lock()
        session_options: dict[str, Any] = {"testnet": self.testnet}
        if self.demo:
            session_options["demo"] = True
        self._client = HTTP(**session_options)

    def get_instruments_info(
        self,
        *,
        max_pages: int = 50,
        require_complete: bool = False,
    ) -> list[dict[str, Any]]:
        # Bound the cursor walk: each _get is bounded by pybit's 10s but the
        # loop is not, so a stable non-empty nextPageCursor would hang the
        # calling thread. 50 pages * 1000 rows covers the linear universe, and a
        # non-advancing cursor also breaks.
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max(1, int(max_pages))):
            params: dict[str, Any] = {"category": self.category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = self._get("get_instruments_info", **params)
            result = payload.get("result", {})
            rows.extend(result.get("list", []))
            next_cursor = result.get("nextPageCursor") or None
            if not next_cursor:
                return rows
            if next_cursor == cursor:
                if require_complete:
                    raise BybitDataError(
                        "get_instruments_info returned a non-advancing cursor; "
                        "complete instrument coverage cannot be established"
                    )
                return rows
            cursor = next_cursor
        if require_complete:
            raise BybitDataError(
                f"get_instruments_info hit max_pages={max_pages} with a live cursor; "
                "complete instrument coverage cannot be established"
            )
        _logger_market_data.warning(
            "get_instruments_info hit max_pages=%d with a live cursor still set; "
            "returning %d rows (truncated). nextPageCursor may be non-advancing.",
            max_pages,
            len(rows),
        )
        return rows

    def _paged_window_klines(
        self, method_name: str, symbol: str, interval: str, start: int, end: int, *, limit: int
    ) -> list[Any]:
        """Fetch ``[start, end)`` as a sequence of bounded time windows.

        Two easily-broken contracts:

        * ``end`` is EXCLUSIVE while Bybit's own ``end`` is inclusive, so the
          request caps at ``end - 1`` and rows filter ``start <= ts < end``.
          Writing the excluded day's 00:00 bar into a ``date=<end>`` partition
          makes ``pit_coverage`` read one day fresher than reality and puts a
          bogus daily close at the panel tail.
        * An empty window is re-requested once, and an empty window BRACKETED by
          non-empty windows raises: a transient retCode-0 empty response would
          otherwise drop up to ``limit x interval`` bars and let the downloader
          seal the hole with a completeness marker. Bracketing keeps leading and
          trailing empty windows (pre-listing, post-delisting) legitimate.
        """

        interval_ms = INTERVAL_MS[interval] if interval in INTERVAL_MS else int(interval) * 60_000
        rows_by_ts: dict[int, Any] = {}
        window_span_ms = interval_ms * max(limit - 1, 1)
        window_row_counts: list[int] = []
        cursor = start
        while cursor < end:
            window_end = min(end - 1, cursor + window_span_ms)
            batch: list[Any] = []
            for _attempt in range(2):
                payload = self._get(
                    method_name,
                    category=self.category,
                    symbol=symbol,
                    interval=interval,
                    start=cursor,
                    end=window_end,
                    limit=limit,
                )
                batch = payload.get("result", {}).get("list", [])
                if batch:
                    break
            window_row_counts.append(len(batch))
            for item in batch:
                ts = int(item[0])
                if start <= ts < end:
                    rows_by_ts[ts] = item
            if window_end >= end - 1:
                break
            next_cursor = window_end
            cursor = next_cursor if next_cursor > cursor else cursor + interval_ms
        _raise_on_bracketed_empty_window(method_name, symbol, window_row_counts, start=start, end=end)
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def get_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1000) -> list[list[Any]]:
        return self._paged_window_klines("get_kline", symbol, interval, start, end, limit=limit)

    def get_funding_history(self, symbol: str, start: int, end: int, limit: int = 200) -> list[dict[str, Any]]:
        return self._paged_time_range(
            "get_funding_rate_history", "fundingRateTimestamp", symbol=symbol, startTime=start, endTime=end, limit=limit
        )

    def get_tickers(self) -> list[dict[str, Any]]:
        payload = self._get("get_tickers", category=self.category)
        return payload.get("result", {}).get("list", [])

    def get_open_interest(
        self, symbol: str, interval_time: str, start: int, end: int, limit: int = 200
    ) -> list[dict[str, Any]]:
        return self._paged_time_range(
            "get_open_interest",
            "timestamp",
            symbol=symbol,
            intervalTime=interval_time,
            startTime=start,
            endTime=end,
            limit=limit,
        )

    def get_mark_price_klines(
        self, symbol: str, interval: str, start: int, end: int, limit: int = 1000
    ) -> list[dict[str, Any]]:
        return self._paged_window_klines("get_mark_price_kline", symbol, interval, start, end, limit=limit)

    def get_index_price_klines(
        self, symbol: str, interval: str, start: int, end: int, limit: int = 1000
    ) -> list[dict[str, Any]]:
        return self._paged_window_klines("get_index_price_kline", symbol, interval, start, end, limit=limit)

    def get_premium_index_klines(
        self, symbol: str, interval: str, start: int, end: int, limit: int = 1000
    ) -> list[dict[str, Any]]:
        return self._paged_window_klines(
            "get_premium_index_price_kline", symbol, interval, start, end, limit=limit
        )

    def _paged_time_range(self, method_name: str, timestamp_key: str, **params: Any) -> list[dict[str, Any]]:
        rows_by_ts: dict[int, dict[str, Any]] = {}
        start = int(params["startTime"])
        # `endTime` is the caller's EXCLUSIVE bound; Bybit's is inclusive, so
        # the request and the row filter both cap at end - 1.
        end = int(params["endTime"])
        cursor_end = end - 1
        limit = int(params.get("limit", 200))
        # An empty page right after a full page is ambiguous: the symbol's
        # history ended exactly on a page boundary (ordinary for anything
        # listed mid-window), or a transient empty reply that would silently
        # truncate the fetch. It is re-asked before it is believed; an empty
        # FIRST page remains a legitimate no-data result.
        prior_full_page = False
        while cursor_end >= start:
            request_params = {**params, "startTime": start, "endTime": cursor_end}
            payload = self._get(method_name, category=self.category, **request_params)
            batch = payload.get("result", {}).get("list", [])
            if not batch and prior_full_page:
                for _ in range(2):
                    payload = self._get(method_name, category=self.category, **request_params)
                    batch = payload.get("result", {}).get("list", [])
                    if batch:
                        break
            if not batch:
                break
            timestamps = sorted(int(item[timestamp_key]) for item in batch)
            if not timestamps:
                if prior_full_page:
                    raise BybitDataError(
                        f"Bybit {method_name} returned a page with no usable "
                        f"timestamps mid-range (symbol={params.get('symbol')!r}, "
                        f"startTime={start}, endTime={cursor_end}) after a full "
                        f"page; refusing to truncate the fetch silently"
                    )
                break
            for item in batch:
                ts = int(item[timestamp_key])
                if start <= ts < end:
                    rows_by_ts[ts] = item
            oldest = min(timestamps)
            # Safe to exit on `oldest <= start`: rows_by_ts is ts-keyed so
            # overlapping duplicates overwrite cleanly, and the next cursor_end
            # would fall below `start` anyway.
            if len(batch) < limit or oldest <= start:
                break
            next_cursor_end = oldest - 1
            if next_cursor_end >= cursor_end:
                break
            cursor_end = next_cursor_end
            # A full page that did not reach `start`: more rows are expected,
            # so arm the mid-range empty-page guard for the next request.
            prior_full_page = True
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def _get(self, method_name: str, **params: Any) -> dict[str, Any]:
        method = getattr(self._client, method_name)
        last_error: Exception | None = None
        with self._stats_lock:
            self.logical_calls += 1
        for attempt in range(self.retries):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            started = time.perf_counter()
            try:
                with self._stats_lock:
                    self.http_calls += 1
                payload = method(**params)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                ret_code = payload.get("retCode")
                if ret_code != 0:
                    self._record_call(elapsed_ms, error_text=str(payload), rate_limited=_is_rate_limit(payload))
                    raise BybitDataError(f"Bybit {method_name} failed: {payload}")
                self._record_call(elapsed_ms)
                return payload
            except Exception as exc:  # noqa: BLE001 - pybit raises several transport types
                if not isinstance(exc, BybitDataError):
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    self._record_call(elapsed_ms, error_text=str(exc), rate_limited=_is_rate_limit(exc))
                last_error = exc
                # A definite venue reject -- bad symbol, invalid param --
                # will not change on retry, so raise instead of spending the
                # backoff on identical calls. "Not now" answers (busy matching
                # engine, server error) do change, so they keep their retries:
                # same predicate the order path uses, so both classify a venue
                # refusal the same way.
                if isinstance(exc, BybitDataError) and not _is_transient_venue_fault(exc):
                    raise
                if attempt + 1 >= self.retries:
                    break
                with self._stats_lock:
                    self.retry_events += 1
                time.sleep(self.retry_sleep_seconds * (2**attempt))
        raise BybitDataError(f"Bybit {method_name} failed after retries") from last_error

    def _record_call(self, elapsed_ms: float, *, error_text: str = "", rate_limited: bool = False) -> None:
        # Guarded so the shared bootstrap pool cannot lose increments.
        with self._stats_lock:
            self.total_call_ms += elapsed_ms
            if elapsed_ms >= self.slow_call_threshold_ms:
                self.slow_calls += 1
                self.slow_call_ms += elapsed_ms
            if error_text:
                self.error_events += 1
                self.last_error = error_text[:500]
            if rate_limited:
                self.rate_limit_events += 1

    def stats(self) -> dict[str, Any]:
        # Snapshot under the lock so a concurrent mutation cannot produce an
        # internally inconsistent dict.
        with self._stats_lock:
            backoff_events = self.retry_events + self.rate_limit_events + self.slow_calls
            return {
                "logical_calls": self.logical_calls,
                "http_calls": self.http_calls,
                "retry_events": self.retry_events,
                "rate_limit_events": self.rate_limit_events,
                "error_events": self.error_events,
                "slow_calls": self.slow_calls,
                "total_call_ms": round(self.total_call_ms, 3),
                "slow_call_ms": round(self.slow_call_ms, 3),
                "backoff_events": backoff_events,
                "last_error": self.last_error,
            }
