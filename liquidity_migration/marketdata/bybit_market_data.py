"""Credential-free Bybit public REST and WebSocket market-data clients.

Strategy producers import this plane directly; execution authority lives
elsewhere.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from liquidity_migration.marketdata.bybit_errors import (
    BybitDataError,
    is_rate_limit as _is_rate_limit,
    is_transient_venue_fault as _is_transient_venue_fault,
)
from liquidity_migration.marketdata.ws_frame_gate import KlineFrameGate

try:
    from pybit.unified_trading import HTTP, WebSocket
except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
    HTTP = None
    WebSocket = None


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

@dataclass(slots=True)
class BybitPublicTickerStream:
    category: str = "linear"
    testnet: bool = False
    demo: bool = False
    # Bybit V5 caps args-per-subscribe-message at 10 for public spot/linear/
    # inverse. pybit ships all symbols in one message; chunk so the message
    # never exceeds the cap. Each chunk issues a new ticker_stream call
    # against the same WebSocket — pybit queues multiple subscribe frames
    # on the same connection.
    subscribe_args_per_message: int = 10
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if WebSocket is None:
            raise RuntimeError("pybit is required for BybitPublicTickerStream")
        _patch_pybit_daemon_ping_timer()
        # Ticker messages are deltas. Every one must reach pybit so its cached
        # row remains a complete view of the latest venue state.
        self._client = WebSocket(
            testnet=self.testnet,
            demo=self.demo,
            channel_type=self.category,
            restart_on_error=False,
        )

    def subscribe_tickers(self, symbols: str | list[str], callback: Any) -> None:
        if isinstance(symbols, str):
            self._client.ticker_stream(symbol=symbols, callback=callback)
            return
        chunk = max(self.subscribe_args_per_message, 1)
        symbol_list = list(symbols)
        for i in range(0, len(symbol_list), chunk):
            slice_ = symbol_list[i : i + chunk]
            self._client.ticker_stream(symbol=slice_, callback=callback)

    def stats(self) -> dict[str, int]:
        return {"frames_seen": 0, "frames_dropped_pre_decode": 0}

    def close(self) -> None:
        _close_ws_client(self._client)


def _close_ws_client(client: Any, *, timeout_seconds: float = 3.0) -> None:
    """Close a pybit WS client with a hard timeout.

    pybit's exit/close/stop methods can occasionally hang (especially when
    the underlying TCP socket is in a half-closed state). Without a
    timeout, daemon shutdown waits indefinitely for the WS to die — and
    systemd then SIGKILLs the whole process. Running the close on a
    background thread bounds that wait; a timeout disables the socket loop and
    closes the underlying socket before checking the thread once more."""
    # Stop pybit from starting another internal reconnect while this client is
    # being retired. The pool and host own reconnects.
    if hasattr(client, "handle_error"):
        client.handle_error = False
    if hasattr(client, "exited"):
        client.exited = True

    # Cancel the ping timer under whichever attribute it lives on: our patch sets
    # both _agc_ping_timer and custom_ping_timer, but stock pybit (and a future
    # bump that stops calling our patched _send_initial_ping) uses only
    # custom_ping_timer. Checking both keeps shutdown-cleanup correct regardless
    # of whether the monkeypatch applied — so a pybit upgrade can't silently turn
    # this cancel into a no-op and reintroduce a shutdown-blocking timer thread.
    for attr in ("_agc_ping_timer", "custom_ping_timer"):
        timer = getattr(client, attr, None)
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
    closer = None
    for name in ("exit", "close", "stop"):
        method = getattr(client, name, None)
        if callable(method):
            closer = method
            break
    if closer is None:
        return

    def _run() -> None:
        try:
            closer()
        except Exception as exc:
            logging.getLogger("liquidity_migration.marketdata.bybit").debug(
                "ws close raised: %s",
                exc,
            )

    thread = threading.Thread(target=_run, name="ws-close", daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        websocket_app = getattr(client, "ws", None)
        if websocket_app is not None:
            try:
                websocket_app.keep_running = False
            except Exception:
                pass
            socket = getattr(websocket_app, "sock", None)
            close_socket = getattr(socket, "close", None)
            if callable(close_socket):
                try:
                    close_socket()
                except Exception:
                    pass
            try:
                websocket_app.sock = None
            except Exception:
                pass
        thread.join(timeout=min(max(timeout_seconds, 0.0), 0.5))
        if thread.is_alive():
            logging.getLogger("liquidity_migration.marketdata.bybit").warning(
                "ws close did not return within %.1fs after socket abort",
                timeout_seconds,
            )


def _patch_pybit_daemon_ping_timer() -> None:
    """Ensure pybit's ping timer is a daemon thread that does not block shutdown.

    pybit 5.16.0 already creates a daemon ``custom_ping_timer`` and cancels the
    prior one on every (re)connect, so this is redundant against that version
    and kept only against an older/forked pybit whose ``_send_initial_ping``
    left a non-daemon timer blocking process exit. It cancels any prior timer
    before installing a new one, because the earlier patch overwrote
    ``_agc_ping_timer`` per reconnect and accumulated orphan Timer threads, and
    it mirrors the timer onto both ``custom_ping_timer`` (cancelled by pybit's
    own ``exit()``) and ``_agc_ping_timer`` (cancelled by ``_close_ws_client``)
    so either path kills it.
    """
    try:
        _websocket_stream = importlib.import_module("pybit._websocket_stream")
    except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
        return
    manager = getattr(_websocket_stream, "_V5WebSocketManager", None)
    if manager is None or getattr(manager, "_agc_daemon_ping_timer", False):
        return

    def _send_initial_ping(self: Any) -> None:
        # Cancel any timer still live from a prior connect before replacing it,
        # so a reconnect (pybit re-invokes _send_initial_ping per connect) does
        # not leave an orphan daemon Timer running.
        for attr in ("custom_ping_timer", "_agc_ping_timer"):
            prior = getattr(self, attr, None)
            cancel = getattr(prior, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # noqa: BLE001 - never let cleanup break the ping loop
                    pass
        timer = threading.Timer(self.ping_interval, self._send_custom_ping)
        timer.daemon = True
        # Set both so pybit's exit()/_stop_custom_ping_timer AND our
        # _close_ws_client can each cancel the live timer.
        self.custom_ping_timer = timer
        self._agc_ping_timer = timer
        timer.start()

    manager._send_initial_ping = _send_initial_ping
    manager._agc_daemon_ping_timer = True


_logger_ws_klines = logging.getLogger("liquidity_migration.marketdata.bybit.ws_klines")


class _FrameGatedWebSocketMixin:
    """Let a gate reject a raw frame before pybit spends a json.loads on it.

    pybit's ``_on_message`` decodes every frame as its first statement and
    exposes no earlier hook, so a subclass override is the only seam. Mixed in
    ahead of the pybit class so ``super()`` reaches pybit's own handler.
    """

    def _on_message(self, message: Any) -> None:
        gate = getattr(self, "frame_gate", None)
        if gate is not None and not gate.accepts(message):
            return
        super()._on_message(message)  # type: ignore[misc]


_GATED_WEBSOCKET_BASE: Any = None
_GATED_WEBSOCKET_CLASS: type[Any] | None = None


def _websocket_supports_frame_gate() -> bool:
    """True when the installed pybit still has the decode seam to override."""
    return WebSocket is not None and hasattr(WebSocket, "_on_message")


def _gated_websocket_class() -> type[Any]:
    """Build (once) the pybit WebSocket subclass that consults a frame gate.

    Cached against the class it was built from, so a test that swaps
    ``WebSocket`` for a fake does not inherit a subclass of the real one.
    """
    global _GATED_WEBSOCKET_BASE, _GATED_WEBSOCKET_CLASS
    if _GATED_WEBSOCKET_CLASS is not None and _GATED_WEBSOCKET_BASE is WebSocket:
        return _GATED_WEBSOCKET_CLASS

    class _GatedWebSocket(_FrameGatedWebSocketMixin, WebSocket):
        def __init__(self, *, frame_gate: Any = None, **kwargs: Any) -> None:
            # Before the base constructor: pybit's WebSocket.__init__ ends by
            # connecting, and frames can arrive inside that call.
            self.frame_gate = frame_gate
            super().__init__(**kwargs)

    _GATED_WEBSOCKET_BASE = WebSocket
    _GATED_WEBSOCKET_CLASS = _GatedWebSocket
    return _GatedWebSocket


def _is_already_subscribed_error(exc: BaseException) -> bool:
    """True for pybit's "You have already subscribed to this topic" error.

    pybit's ``_check_callback_directory`` raises this when a topic is still in
    the connection's callback directory. A kline topic whose unsubscribe didn't
    clear that directory (a symbol that churned out of, then back into, the
    universe) triggers it on re-subscribe — handled idempotently by the pool."""
    return "already subscribed" in str(exc).lower()


def _default_kline_websocket_factory(*, testnet: bool, demo: bool, channel_type: str) -> Any:
    """Create a fresh pybit WebSocket client tuned for kline streams.

    The client carries a gate that throws away the once-a-second partial-bar
    frames before they are decoded; only the closed hourly bar is wanted."""
    if WebSocket is None:
        raise RuntimeError("pybit is required for BybitKlineStreamPool")
    _patch_pybit_daemon_ping_timer()
    if not _websocket_supports_frame_gate():
        _logger_ws_klines.warning("pybit exposes no _on_message; kline frames decode ungated")
        return WebSocket(
            testnet=testnet,
            demo=demo,
            channel_type=channel_type,
            restart_on_error=False,
        )
    return _gated_websocket_class()(
        frame_gate=KlineFrameGate(),
        testnet=testnet,
        demo=demo,
        channel_type=channel_type,
        restart_on_error=False,
    )


@dataclass(slots=True)
class _KlineConnectionState:
    """Per-connection bookkeeping for the pool."""

    index: int
    client: Any
    assigned_symbols: set[str]
    last_message_monotonic: float
    reconnect_count: int = 0
    message_count: int = 0
    dropped_messages: int = 0
    closed: bool = False
    # monotonic timestamp of the last reconnect ATTEMPT — the watchdog uses this
    # to space retries per-connection instead of sleeping while holding the pool
    # lock, which would block subscribe/stats for backoff×N seconds on a
    # multi-connection reconnect. 0.0 = never attempted, so first reconnect is
    # immediate.
    last_reconnect_monotonic: float = 0.0

    def mark_frame_seen(self) -> None:
        """Liveness stamp for a frame dropped before decode.

        Only the gate's drop path calls this. A delivered frame is still
        stamped in the pool callback, and pongs and acks must keep stamping
        nothing so a quiet venue-side subscription is still detected.
        """
        self.last_message_monotonic = time.monotonic()


class BybitKlineStreamPool:
    """Multi-connection WebSocket pool for 1h kline subscriptions.

    Splits a large symbol universe across N pybit ``WebSocket`` clients (one
    "connection" each, since pybit's WebSocket abstraction owns its own thread
    + reconnect loop). Re-routes the per-bar callbacks into a single
    ``on_bar(symbol, bar, confirmed)`` interface that the store consumes.
    Connections open with a small delay between them: Bybit allows 500
    connects/IP/5min on public.

    The watchdog thread monitors per-connection ``last_message_monotonic``: a
    connection silent for ``stale_warning_seconds`` is logged, one silent for
    ``stale_reconnect_seconds`` is torn down and rebuilt with its same slice,
    re-issuing the WS subscription from scratch.
    """

    DEFAULT_TOPICS_PER_CONNECTION = 180
    DEFAULT_STALE_WARNING_SECONDS = 60.0
    DEFAULT_STALE_RECONNECT_SECONDS = 180.0
    DEFAULT_WATCHDOG_INTERVAL_SECONDS = 10.0
    DEFAULT_CONNECTION_SPACING_SECONDS = 0.1
    DEFAULT_RECONNECT_BACKOFF_SECONDS = 5.0
    # Bybit V5 caps the args list per WS subscribe message; 10 is the
    # conservative spot-tier cap. Repeated kline_stream calls queue further
    # subscribe frames on the same WebSocket.
    DEFAULT_SUBSCRIBE_ARGS_PER_MESSAGE = 10

    def __init__(
        self,
        *,
        interval_minutes: int = 60,
        category: str = "linear",
        testnet: bool = False,
        demo: bool = False,
        topics_per_connection: int = DEFAULT_TOPICS_PER_CONNECTION,
        stale_warning_seconds: float = DEFAULT_STALE_WARNING_SECONDS,
        stale_reconnect_seconds: float = DEFAULT_STALE_RECONNECT_SECONDS,
        watchdog_interval_seconds: float = DEFAULT_WATCHDOG_INTERVAL_SECONDS,
        connection_spacing_seconds: float = DEFAULT_CONNECTION_SPACING_SECONDS,
        reconnect_backoff_seconds: float = DEFAULT_RECONNECT_BACKOFF_SECONDS,
        subscribe_args_per_message: int = DEFAULT_SUBSCRIBE_ARGS_PER_MESSAGE,
        websocket_factory: Callable[..., Any] | None = None,
    ) -> None:
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        if topics_per_connection <= 0:
            raise ValueError("topics_per_connection must be positive")
        if stale_reconnect_seconds <= stale_warning_seconds:
            raise ValueError("stale_reconnect_seconds must exceed stale_warning_seconds")
        if subscribe_args_per_message <= 0:
            raise ValueError("subscribe_args_per_message must be positive")
        self.interval_minutes = int(interval_minutes)
        self.category = category
        self.testnet = bool(testnet)
        self.demo = bool(demo)
        self.topics_per_connection = int(topics_per_connection)
        self.stale_warning_seconds = float(stale_warning_seconds)
        self.stale_reconnect_seconds = float(stale_reconnect_seconds)
        self.watchdog_interval_seconds = float(watchdog_interval_seconds)
        self.connection_spacing_seconds = float(connection_spacing_seconds)
        self.reconnect_backoff_seconds = float(reconnect_backoff_seconds)
        self.subscribe_args_per_message = int(subscribe_args_per_message)
        self._websocket_factory = websocket_factory or _default_kline_websocket_factory
        self._subscription_lock = threading.RLock()
        self._lock = threading.RLock()
        self._on_bar: Callable[[str, dict[str, Any], bool], None] | None = None
        self._connections: list[_KlineConnectionState] = []
        self._symbol_to_connection: dict[str, int] = {}
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._stale_warnings_total = 0
        self._reconnects_total = 0
        self._closed = False

    # -- subscribe + update --------------------------------------------

    def subscribe(
        self,
        symbols: Iterable[str],
        on_bar: Callable[[str, dict[str, Any], bool], None],
    ) -> None:
        """Subscribe to ``symbols``. Idempotent: re-subscribing the same set
        is a no-op; a different set runs through ``update_subscriptions``."""
        with self._subscription_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("pool is closed")
                unique_symbols = sorted({s for s in symbols if s})
                # A different callback replaces the old one on every connection.
                self._on_bar = on_bar
                if not self._connections:
                    self._build_initial_connections_locked(unique_symbols)
                else:
                    self.update_subscriptions(set(unique_symbols))

    def update_subscriptions(self, new_symbols: set[str]) -> dict[str, int]:
        """Diff the current assignment against ``new_symbols``. Returns counts.

        Each add is ISOLATED: a subscribe failure on one symbol is logged and
        skipped, so one un-subscribable symbol cannot block every new listing
        sorted after it on every hourly refresh. A removal rebuilds its whole
        connection because pybit unsubscribes the entire original batch when
        asked to remove one topic from a multi-topic subscribe frame."""
        with self._subscription_lock:
            return self._update_subscriptions_serialized(new_symbols)

    def _update_subscriptions_serialized(self, new_symbols: set[str]) -> dict[str, int]:
        reconnect_jobs: list[tuple[int, list[str], Any, int, float]] = []
        clients_to_close: list[Any] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("pool is closed")
            if self._on_bar is None:
                raise RuntimeError("subscribe() must be called before update_subscriptions()")
            current = set(self._symbol_to_connection)
            removes = sorted(current - new_symbols)
            affected_connections: set[int] = set()
            for symbol in removes:
                index = self._symbol_to_connection.pop(symbol, None)
                if index is None or index >= len(self._connections):
                    continue
                self._connections[index].assigned_symbols.discard(symbol)
                affected_connections.add(index)
            for index in sorted(affected_connections):
                state = self._connections[index]
                if state.assigned_symbols:
                    job = self._prepare_reconnect_locked(index)
                    if job is not None:
                        reconnect_jobs.append(job)
                elif not state.closed:
                    state.closed = True
                    clients_to_close.append(state.client)

        for client in clients_to_close:
            _close_ws_client(client)
        for job in reconnect_jobs:
            self._reconnect_connection(*job)

        with self._lock:
            if self._closed:
                raise RuntimeError("pool is closed")
            adds = sorted(new_symbols - set(self._symbol_to_connection))
            added = 0
            for symbol in adds:
                try:
                    self._subscribe_symbol_locked(symbol)
                    added += 1
                except Exception as exc:  # noqa: BLE001 - one bad symbol must not drop the rest
                    _logger_ws_klines.warning(
                        "kline subscribe failed for %s; continuing with the rest: %s",
                        symbol,
                        exc,
                    )
            return {"added": added, "removed": len(removes), "connections": len(self._connections)}

    def _build_initial_connections_locked(self, symbols: list[str]) -> None:
        for i in range(0, len(symbols), self.topics_per_connection):
            chunk = symbols[i : i + self.topics_per_connection]
            self._open_connection_locked(initial_symbols=chunk)
            if self.connection_spacing_seconds > 0.0 and i + self.topics_per_connection < len(symbols):
                time.sleep(self.connection_spacing_seconds)

    def _open_connection_locked(self, *, initial_symbols: list[str]) -> _KlineConnectionState:
        index = len(self._connections)
        client = self._websocket_factory(
            testnet=self.testnet,
            demo=self.demo,
            channel_type=self.category,
        )
        state = _KlineConnectionState(
            index=index,
            client=client,
            assigned_symbols=set(),
            last_message_monotonic=time.monotonic(),
        )
        _bind_frame_gate(client, state)
        self._connections.append(state)
        if initial_symbols:
            self._subscribe_to_connection_locked(state, initial_symbols)
        return state

    def _subscribe_symbol_locked(self, symbol: str) -> None:
        # Find an OPEN connection under capacity, else open a new one. The
        # closed filter matters: a connection awaiting a failed reconnect is
        # under capacity, and kline_stream() on a dead client loses the feed.
        target = next(
            (
                state
                for state in self._connections
                if not state.closed and len(state.assigned_symbols) < self.topics_per_connection
            ),
            None,
        )
        if target is None:
            target = self._open_connection_locked(initial_symbols=[symbol])
            return
        self._subscribe_to_connection_locked(target, [symbol])

    def _subscribe_to_connection_locked(self, state: _KlineConnectionState, symbols: list[str]) -> None:
        if not symbols:
            return
        subscribed = self._subscribe_client_chunks(state, symbols)
        for symbol in subscribed:
            state.assigned_symbols.add(symbol)
            self._symbol_to_connection[symbol] = state.index

    def _subscribe_client_chunks(self, state: _KlineConnectionState, symbols: list[str]) -> set[str]:
        callback = self._make_callback(state)
        # Chunk so each WS message stays under Bybit's per-message args cap;
        # repeated kline_stream calls queue further frames on the connection.
        chunk = self.subscribe_args_per_message
        symbols_list = list(symbols)
        subscribed: set[str] = set()
        for i in range(0, len(symbols_list), chunk):
            slice_ = symbols_list[i : i + chunk]
            try:
                state.client.kline_stream(
                    interval=self.interval_minutes,
                    symbol=slice_,
                    callback=callback,
                )
            except Exception as exc:
                if _is_already_subscribed_error(exc):
                    # pybit still holds one of these topics in its callback
                    # directory and rejects the WHOLE frame, so retry a
                    # multi-symbol slice one at a time. A single
                    # already-subscribed topic is ADOPTED: its callback already
                    # feeds the same sink, and a dead topic is rebuilt by the
                    # staleness watchdog. Re-raising would abort the add batch
                    # every refresh and drop later new listings.
                    if len(slice_) > 1:
                        for symbol in slice_:
                            subscribed.update(self._subscribe_client_chunks(state, [symbol]))
                        continue
                    _logger_ws_klines.debug(
                        "kline topic already subscribed conn=%d; adopting %s",
                        state.index,
                        slice_[0],
                    )
                    subscribed.add(slice_[0])
                    continue
                _logger_ws_klines.warning(
                    "kline_stream subscribe failed conn=%d slice=%d/%d: %s",
                    state.index,
                    len(slice_),
                    len(symbols_list),
                    exc,
                )
                raise
            subscribed.update(slice_)
        return subscribed

    def _make_callback(self, state: _KlineConnectionState) -> Callable[[dict[str, Any]], None]:
        """Build a closure that parses pybit's kline message, marks the
        connection alive, and dispatches each bar through ``on_bar``.

        pybit delivers the full message dict ``{"topic": "kline.60.SYMBOL",
        "data": [{"start": ..., "confirm": True, ...}]}``; the consumer contract
        is one ``on_bar(symbol, bar_dict, confirmed)`` call per bar.

        ``self._on_bar`` is read at dispatch time, not build time: capturing it
        here would leave already-subscribed topics firing the OLD sink after a
        re-subscribe with a different callback."""
        if self._on_bar is None:  # defensive — subscribe() always sets this first
            raise RuntimeError("internal error: on_bar callback not set")

        def _callback(message: dict[str, Any]) -> None:
            # One writer per connection; readers may see a sub-tick-old value.
            # Avoids the shared pool lock on this hot path.
            state.message_count += 1
            state.last_message_monotonic = time.monotonic()
            on_bar = self._on_bar
            if on_bar is None:  # defensive — a swap should never clear it to None
                state.dropped_messages += 1
                return
            try:
                topic = message.get("topic", "")
                data = message.get("data", [])
                if not isinstance(topic, str) or not isinstance(data, (list, tuple)):
                    state.dropped_messages += 1
                    return
                symbol = _symbol_from_kline_topic(topic)
                if symbol is None:
                    state.dropped_messages += 1
                    return
                if state.closed or symbol not in state.assigned_symbols:
                    state.dropped_messages += 1
                    return
                for bar in data:
                    if not isinstance(bar, Mapping):
                        state.dropped_messages += 1
                        continue
                    confirmed = bool(bar.get("confirm", False))
                    try:
                        on_bar(symbol, dict(bar), confirmed)
                    except Exception as exc:
                        _logger_ws_klines.exception(
                            "on_bar callback raised conn=%d symbol=%s: %s",
                            state.index,
                            symbol,
                            exc,
                        )
            except Exception as exc:
                state.dropped_messages += 1
                _logger_ws_klines.exception(
                    "kline pool callback crashed conn=%d: %s",
                    state.index,
                    exc,
                )

        return _callback

    # -- watchdog + reconnect ------------------------------------------

    def start_watchdog(self) -> None:
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="kline-pool-watchdog", daemon=True)
        self._watchdog_thread.start()

    def stop_watchdog(self, *, join_timeout: float = 5.0) -> None:
        thread = self._watchdog_thread
        self._watchdog_thread = None
        if thread is None:
            return
        self._watchdog_stop.set()
        thread.join(timeout=join_timeout)

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(timeout=self.watchdog_interval_seconds):
            try:
                self.check_stale_connections()
            except Exception as exc:
                _logger_ws_klines.exception("watchdog tick failed: %s", exc)

    def check_stale_connections(self) -> int:
        """Inspect every connection's ``last_message_monotonic``. Connections
        idle past ``stale_reconnect_seconds`` are torn down and rebuilt with
        the same slice. Returns the number of reconnects performed.

        Also retries any connection whose prior reconnect failed mid-way
        (closed=True with assigned_symbols still set); otherwise one transient
        ``_websocket_factory`` failure orphans that whole slice until the next
        hourly universe refresh."""
        with self._subscription_lock:
            return self._check_stale_connections_serialized()

    def _check_stale_connections_serialized(self) -> int:
        reconnects = 0
        now = time.monotonic()
        with self._lock:
            to_reconnect: list[int] = []
            for state in list(self._connections):
                if not state.assigned_symbols:
                    continue
                # Per-connection backoff gate, so the pool lock is never held
                # across a backoff sleep. backoff < watchdog interval, so the
                # gate cannot block a connection indefinitely.
                if (
                    state.last_reconnect_monotonic > 0.0
                    and now - state.last_reconnect_monotonic < self.reconnect_backoff_seconds
                ):
                    continue
                if state.closed:
                    # Prior reconnect left this slice without a live client;
                    # the backoff gate above prevents a retry storm.
                    to_reconnect.append(state.index)
                    continue
                gap = now - state.last_message_monotonic
                if gap >= self.stale_reconnect_seconds:
                    to_reconnect.append(state.index)
                elif gap >= self.stale_warning_seconds:
                    self._stale_warnings_total += 1
                    _logger_ws_klines.warning(
                        "kline connection idle: conn=%d gap=%.1fs symbols=%d",
                        state.index,
                        gap,
                        len(state.assigned_symbols),
                    )
            reconnect_jobs: list[tuple[int, list[str], Any, int, float]] = []
            for index in to_reconnect:
                job = self._prepare_reconnect_locked(index)
                if job is not None:
                    reconnect_jobs.append(job)
        for index, slice_symbols, old_client, prior_reconnect_count, attempt_monotonic in reconnect_jobs:
            self._reconnect_connection(
                index,
                slice_symbols,
                old_client,
                prior_reconnect_count,
                attempt_monotonic,
            )
            reconnects += 1
        return reconnects

    def _prepare_reconnect_locked(self, index: int) -> tuple[int, list[str], Any, int, float] | None:
        if index >= len(self._connections):
            return None
        state = self._connections[index]
        if not state.assigned_symbols:
            return None
        # Snapshot the slice but keep assigned_symbols intact, so a mid-reconnect
        # failure leaves the watchdog enough state to retry next tick. It is
        # cleared only on a successful resubscribe.
        slice_symbols = sorted(state.assigned_symbols)
        # Stamp the attempt before any work so the backoff gate spaces the next
        # retry even if the factory build below raises.
        attempt_monotonic = time.monotonic()
        state.last_reconnect_monotonic = attempt_monotonic
        state.closed = True
        _logger_ws_klines.warning(
            "kline connection reconnect conn=%d symbols=%d",
            index,
            len(slice_symbols),
        )
        return index, slice_symbols, state.client, state.reconnect_count, attempt_monotonic

    def _reconnect_connection(
        self,
        index: int,
        slice_symbols: list[str],
        old_client: Any,
        prior_reconnect_count: int,
        attempt_monotonic: float,
    ) -> None:
        try:
            _close_ws_client(old_client)
        except Exception as exc:
            _logger_ws_klines.warning("close on reconnect failed conn=%d: %s", index, exc)
        try:
            new_client = self._websocket_factory(
                testnet=self.testnet,
                demo=self.demo,
                channel_type=self.category,
            )
        except Exception as exc:
            _logger_ws_klines.exception(
                "kline reconnect failed to build new client conn=%d: %s; watchdog will retry on next tick",
                index,
                exc,
            )
            # Stays closed=True with assigned_symbols set; the watchdog's
            # closed+assigned branch picks it up next tick.
            return
        new_state = _KlineConnectionState(
            index=index,
            client=new_client,
            assigned_symbols=set(),
            last_message_monotonic=time.monotonic(),
            reconnect_count=prior_reconnect_count + 1,
            last_reconnect_monotonic=attempt_monotonic,
        )
        _bind_frame_gate(new_client, new_state)
        try:
            subscribed = self._subscribe_client_chunks(new_state, slice_symbols)
        except Exception as exc:
            _logger_ws_klines.exception(
                "kline reconnect resubscribe failed conn=%d: %s; marking closed for retry",
                index,
                exc,
            )
            _close_ws_client(new_client)
            return
        close_new_client = False
        with self._lock:
            if self._closed or index >= len(self._connections):
                close_new_client = True
            else:
                old_state = self._connections[index]
                desired_symbols = set(old_state.assigned_symbols)
                install_symbols = sorted(set(subscribed) & desired_symbols)
                if not install_symbols:
                    for symbol in slice_symbols:
                        self._symbol_to_connection.pop(symbol, None)
                    old_state.assigned_symbols.clear()
                    old_state.closed = True
                    close_new_client = True
                elif set(subscribed) != desired_symbols:
                    close_new_client = True
                else:
                    for symbol in slice_symbols:
                        self._symbol_to_connection.pop(symbol, None)
                    new_state.assigned_symbols = set(install_symbols)
                    for symbol in install_symbols:
                        self._symbol_to_connection[symbol] = index
                    self._connections[index] = new_state
                    self._reconnects_total += 1
        if close_new_client:
            _close_ws_client(new_client)

    # -- shutdown -------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop_watchdog()
        with self._lock:
            states = list(self._connections)
            self._connections.clear()
            self._symbol_to_connection.clear()
        for state in states:
            try:
                self._close_state(state)
            except Exception as exc:
                _logger_ws_klines.warning(
                    "close failed conn=%d: %s",
                    state.index,
                    exc,
                )

    @staticmethod
    def _close_state(state: _KlineConnectionState) -> None:
        if state.closed:
            return
        _close_ws_client(state.client)
        state.closed = True

    # -- introspection --------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            per_conn = []
            for state in self._connections:
                frames_seen, frames_dropped = _frame_gate_stats(state.client)
                per_conn.append(
                    {
                        "index": state.index,
                        "topics": len(state.assigned_symbols),
                        "messages": state.message_count,
                        "dropped": state.dropped_messages,
                        "reconnects": state.reconnect_count,
                        "idle_seconds": round(now - state.last_message_monotonic, 3),
                        "closed": state.closed,
                        "frames_seen": frames_seen,
                        "frames_dropped_pre_decode": frames_dropped,
                    }
                )
            return {
                "connections": len(self._connections),
                "subscribed_symbols": len(self._symbol_to_connection),
                "reconnects_total": self._reconnects_total,
                "stale_warnings_total": self._stale_warnings_total,
                "per_connection": per_conn,
            }

def _bind_frame_gate(client: Any, state: _KlineConnectionState) -> None:
    """Point a client's frame gate at this connection's liveness clock.

    A no-op for a client without one, which is what the tests' fake factory
    builds.
    """
    gate = getattr(client, "frame_gate", None)
    if gate is not None:
        gate.on_dropped_frame = state.mark_frame_seen


def _frame_gate_stats(client: Any) -> tuple[int, int]:
    gate = getattr(client, "frame_gate", None)
    if gate is None:
        return 0, 0
    return int(getattr(gate, "frames_seen", 0)), int(getattr(gate, "frames_dropped", 0))


def _symbol_from_kline_topic(topic: str) -> str | None:
    """Extract the symbol component from a kline topic ``kline.60.SYMBOL``."""
    if not topic.startswith("kline."):
        return None
    parts = topic.split(".", 2)
    if len(parts) != 3 or not parts[2]:
        return None
    return parts[2]
