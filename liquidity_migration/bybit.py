from __future__ import annotations

import importlib
import logging
import os
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

try:
    from pybit.unified_trading import HTTP, WebSocket, WebSocketTrading
except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
    HTTP = None
    WebSocket = None
    WebSocketTrading = None


class _PybitRateLimitLogFilter(logging.Filter):
    """Drop pybit's 10006 (rate limit) retry chatter.

    pybit's _handle_retryable_error logs at ERROR level twice for every 10006
    retry -- once before sleeping ("Hit the API rate limit on <url>. Sleeping
    then trying again.") and once after computing the reset window ("API rate
    limit will reset at HH:MM:SS. Sleeping for Nms. Retrying..."). With ~180
    demo symbols hitting the public kline endpoint at top-of-hour, plus pybit's
    default max_retries=3, this produces 10K-22K identical lines per minute
    in the journal. The retries themselves are working as intended (pybit
    sleeps until X-Bapi-Limit-Reset-Timestamp and recovers without our
    wrapper getting involved); the log volume just buries real errors and
    fills disk. Filter only the 10006-specific lines; let other pybit errors
    through untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return (
            "ErrCode: 10006" not in message
            and "Hit the API rate limit" not in message
            and "API rate limit will reset" not in message
        )


# Install the filter at module import. pybit instantiates its logger lazily on
# first HTTP() call, but addFilter is idempotent on the named-logger handle
# regardless of when the underlying logger picks up the filter.
logging.getLogger("pybit._http_manager").addFilter(_PybitRateLimitLogFilter())


class BybitDataError(RuntimeError):
    pass


def _env_flag(name: str) -> bool:
    """True when environment variable ``name`` is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_private_credentials() -> tuple[str | None, str | None, bool]:
    """Return ``(api_key, api_secret, demo)`` from the .env DEMO / REAL_MONEY toggle.

    Two mutually exclusive flags pick the account:

      * ``REAL_MONEY=true`` -> mainnet keys (``BYBIT_REAL_API_KEY`` /
        ``BYBIT_REAL_API_SECRET``), real-money endpoint (``demo=False``).
      * ``DEMO=true`` or unset -> demo keys (``BYBIT_DEMO_API_KEY`` /
        ``BYBIT_DEMO_API_SECRET``), demo endpoint (``demo=True``).

    Demo is the default, so an unset toggle stays on the demo account. Setting
    both ``DEMO`` and ``REAL_MONEY`` true is a contradiction and raises.
    """
    demo = _env_flag("DEMO")
    real_money = _env_flag("REAL_MONEY")
    if demo and real_money:
        raise RuntimeError(
            "DEMO and REAL_MONEY are both set true -- pick one: "
            "DEMO=true for the demo account, REAL_MONEY=true for mainnet."
        )
    if real_money:
        return (
            os.environ.get("BYBIT_REAL_API_KEY"),
            os.environ.get("BYBIT_REAL_API_SECRET"),
            False,
        )
    return (
        os.environ.get("BYBIT_DEMO_API_KEY"),
        os.environ.get("BYBIT_DEMO_API_SECRET"),
        True,
    )


def validate_order_submit_allowed(*, submit_orders: bool, confirm_demo_orders: bool) -> None:
    """Guard automated order submission: explicit confirm flag and demo account only."""
    if not submit_orders:
        return
    if not confirm_demo_orders:
        raise RuntimeError("Refusing to submit orders without --confirm-demo-orders")
    _, _, demo = resolve_private_credentials()
    if not demo:
        raise RuntimeError(
            "Refusing to submit orders with REAL_MONEY=true. "
            "Unset REAL_MONEY or use demo credentials for automated cycles."
        )


class BybitRestRateLimiter:
    """Thread-safe sliding-window rate limiter shared across BybitMarketData
    instances. Bybit public REST endpoints allow ~120 requests / 5 seconds per
    IP per category; we default to a conservative 18 req/s so concurrent demo
    workers don't sustain 429s that pybit then handles by sleeping 2 seconds
    per retry — the dominant tail in entry-cycle latency. Stays out of the
    way (no waiting, no lock contention) when callers stay under budget.
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
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._per
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max:
                wait = self._per - (now - self._timestamps[0])
                if wait > 0.0:
                    self._throttle_events += 1
                    self._throttled_seconds += wait
                    time.sleep(wait)
                    now = time.monotonic()
                    cutoff = now - self._per
                    while self._timestamps and self._timestamps[0] < cutoff:
                        self._timestamps.popleft()
            self._timestamps.append(now)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_requests": self._max,
                "per_seconds": self._per,
                "throttle_events": self._throttle_events,
                "throttled_seconds": round(self._throttled_seconds, 3),
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._throttle_events = 0
            self._throttled_seconds = 0.0


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


@dataclass(slots=True)
class BybitMarketData:
    category: str = "linear"
    testnet: bool = False
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

    def __post_init__(self) -> None:
        if HTTP is None:
            raise RuntimeError("pybit is required for BybitMarketData")
        self._client = HTTP(testnet=self.testnet)

    def get_instruments_info(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"category": self.category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = self._get("get_instruments_info", **params)
            result = payload.get("result", {})
            rows.extend(result.get("list", []))
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                return rows

    def get_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1000) -> list[dict[str, Any]]:
        interval_ms = INTERVAL_MS[interval] if interval in INTERVAL_MS else int(interval) * 60_000
        rows_by_ts: dict[int, Any] = {}
        cursor = start
        window_span_ms = interval_ms * max(limit - 1, 1)
        while cursor <= end:
            window_end = min(end, cursor + window_span_ms)
            payload = self._get(
                "get_kline",
                category=self.category,
                symbol=symbol,
                interval=interval,
                start=cursor,
                end=window_end,
                limit=limit,
            )
            batch = payload.get("result", {}).get("list", [])
            for item in batch:
                ts = int(item[0])
                if start <= ts <= end:
                    rows_by_ts[ts] = item
            if window_end >= end:
                break
            next_cursor = window_end
            cursor = next_cursor if next_cursor > cursor else cursor + interval_ms
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def get_funding_history(self, symbol: str, start: int, end: int, limit: int = 200) -> list[dict[str, Any]]:
        return self._paged_time_range("get_funding_rate_history", "fundingRateTimestamp", symbol=symbol, startTime=start, endTime=end, limit=limit)

    def get_tickers(self) -> list[dict[str, Any]]:
        payload = self._get("get_tickers", category=self.category)
        return payload.get("result", {}).get("list", [])

    def get_open_interest(self, symbol: str, interval_time: str, start: int, end: int, limit: int = 200) -> list[dict[str, Any]]:
        return self._paged_time_range(
            "get_open_interest",
            "timestamp",
            symbol=symbol,
            intervalTime=interval_time,
            startTime=start,
            endTime=end,
            limit=limit,
        )

    def get_mark_price_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1000) -> list[dict[str, Any]]:
        return self._get_price_index_klines("get_mark_price_kline", symbol, interval, start, end, limit=limit)

    def get_index_price_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1000) -> list[dict[str, Any]]:
        return self._get_price_index_klines("get_index_price_kline", symbol, interval, start, end, limit=limit)

    def get_premium_index_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1000) -> list[dict[str, Any]]:
        return self._get_price_index_klines("get_premium_index_price_kline", symbol, interval, start, end, limit=limit)

    def _get_price_index_klines(
        self,
        method_name: str,
        symbol: str,
        interval: str,
        start: int,
        end: int,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        interval_ms = INTERVAL_MS[interval] if interval in INTERVAL_MS else int(interval) * 60_000
        rows_by_ts: dict[int, Any] = {}
        cursor = start
        window_span_ms = interval_ms * max(limit - 1, 1)
        while cursor <= end:
            window_end = min(end, cursor + window_span_ms)
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
            for item in batch:
                ts = int(item[0])
                if start <= ts <= end:
                    rows_by_ts[ts] = item
            if window_end >= end:
                break
            next_cursor = window_end
            cursor = next_cursor if next_cursor > cursor else cursor + interval_ms
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def _paged_time_range(self, method_name: str, timestamp_key: str, **params: Any) -> list[dict[str, Any]]:
        rows_by_ts: dict[int, dict[str, Any]] = {}
        start = int(params["startTime"])
        end = int(params["endTime"])
        cursor_end = end
        limit = int(params.get("limit", 200))
        while cursor_end >= start:
            request_params = {**params, "startTime": start, "endTime": cursor_end}
            payload = self._get(method_name, category=self.category, **request_params)
            batch = payload.get("result", {}).get("list", [])
            if not batch:
                break
            timestamps = sorted(int(item[timestamp_key]) for item in batch)
            if not timestamps:
                break
            for item in batch:
                ts = int(item[timestamp_key])
                if start <= ts <= end:
                    rows_by_ts[ts] = item
            oldest = min(timestamps)
            # Safe to exit on `oldest <= start`: rows_by_ts is keyed by ts, so
            # any duplicates from overlapping pages overwrite cleanly, and the
            # next cursor_end would be `oldest - 1 < start`, exiting the outer
            # `while cursor_end >= start` loop on the following iteration anyway.
            if len(batch) < limit or oldest <= start:
                break
            next_cursor_end = oldest - 1
            if next_cursor_end >= cursor_end:
                break
            cursor_end = next_cursor_end
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def _get(self, method_name: str, **params: Any) -> dict[str, Any]:
        method = getattr(self._client, method_name)
        last_error: Exception | None = None
        self.logical_calls += 1
        for attempt in range(self.retries):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            started = time.perf_counter()
            try:
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
                if attempt + 1 >= self.retries:
                    break
                self.retry_events += 1
                time.sleep(self.retry_sleep_seconds * (2**attempt))
        raise BybitDataError(f"Bybit {method_name} failed after retries") from last_error

    def _record_call(self, elapsed_ms: float, *, error_text: str = "", rate_limited: bool = False) -> None:
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

    def reset_stats(self) -> None:
        self.logical_calls = 0
        self.http_calls = 0
        self.retry_events = 0
        self.rate_limit_events = 0
        self.error_events = 0
        self.slow_calls = 0
        self.total_call_ms = 0.0
        self.slow_call_ms = 0.0
        self.last_error = ""


@dataclass(slots=True)
class BybitPrivateClient:
    category: str = "linear"
    testnet: bool = False
    demo: bool = True
    api_key: str | None = None
    api_secret: str | None = None
    retries: int = 2
    retry_sleep_seconds: float = 0.5
    rate_limiter: BybitRestRateLimiter | None = None
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if HTTP is None:
            raise RuntimeError("pybit is required for BybitPrivateClient")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Bybit private execution requires API key and secret")
        self._client = HTTP(
            testnet=self.testnet,
            demo=self.demo,
            api_key=self.api_key,
            api_secret=self.api_secret,
        )

    def get_wallet_balance(self, *, account_type: str = "UNIFIED", coin: str = "USDT") -> dict[str, Any]:
        payload = self._call("get_wallet_balance", accountType=account_type, coin=coin)
        return payload.get("result", {})

    def place_order(self, **params: Any) -> dict[str, Any]:
        if "orderLinkId" not in params:
            raise ValueError("orderLinkId is required for idempotent Bybit order submission")
        payload = self._call_once("place_order", category=self.category, **params)
        return payload.get("result", {})

    def cancel_order(self, *, symbol: str, order_link_id: str) -> dict[str, Any]:
        payload = self._call("cancel_order", category=self.category, symbol=symbol, orderLinkId=order_link_id)
        return payload.get("result", {})

    def cancel_all_orders(self, *, symbol: str | None = None, settle_coin: str | None = "USDT") -> dict[str, Any]:
        params: dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        payload = self._call("cancel_all_orders", **params)
        return payload.get("result", {})

    def get_open_orders(self, *, symbol: str | None = None, settle_coin: str | None = "USDT") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        payload = self._call("get_open_orders", **params)
        return payload.get("result", {}).get("list", [])

    def get_order_history(
        self,
        *,
        symbol: str | None = None,
        order_link_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": self.category, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        if order_link_id:
            params["orderLinkId"] = order_link_id
        payload = self._call_optional(("get_order_history",), **params)
        return payload.get("result", {}).get("list", []) if payload else []

    def get_trade_history(
        self,
        *,
        symbol: str | None = None,
        order_link_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": self.category, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        if order_link_id:
            params["orderLinkId"] = order_link_id
        payload = self._call_optional(("get_executions", "get_trade_history"), **params)
        return payload.get("result", {}).get("list", []) if payload else []

    def get_positions(self, *, symbol: str | None = None, settle_coin: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        payload = self._call("get_positions", **params)
        return payload.get("result", {}).get("list", [])

    def get_closed_pnl(
        self,
        *,
        symbol: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Closed-PnL records for the account.

        Used by the orphan reconciler to backfill exit_price / realized_pnl on
        trades where the open position vanished from Bybit without our cycle
        recording the close (eg. a manual close on the venue, a stop-loss that
        fired between cycles, or a cycle crash mid-place-order whose order_link_id
        we lost). Without this backfill the reconciler closes the ledger row with
        no exit price and no PnL — accurate that the position is gone, but the
        ledger loses the trade outcome.

        Returns the result.list payload as-is (empty list on missing endpoint).
        """
        params: dict[str, Any] = {"category": self.category, "limit": max(1, min(int(limit), 200))}
        if symbol:
            params["symbol"] = symbol
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        payload = self._call_optional(("get_closed_pnl",), **params)
        return payload.get("result", {}).get("list", []) if payload else []

    def get_funding_settlements(
        self,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 200,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """Funding-settlement rows from the account transaction log.

        Used by the demo<->Bybit reconciliation (E6) to surface the short's
        funding tailwind/drag — funding settles separately from closedPnl, so
        without this it is invisible in the reconciliation. Each row carries a
        signed account cash-flow (``funding``/``cashFlow``/``change``; positive =
        the account received funding). Returns the result.list as-is (empty list
        on a missing endpoint, mirroring get_closed_pnl).

        Bybit caps the transaction log at 200 rows/page. Over a multi-day
        reconciliation lookback a funding-active account easily exceeds one
        page (funding settles every 8h per open position), so follow
        ``nextPageCursor`` to the end. Without this the funding total — a
        first-order driver of the short's edge — was silently truncated to
        the first 200 rows. ``max_pages`` bounds the loop defensively.
        """
        base_params: dict[str, Any] = {
            "accountType": "UNIFIED",
            "category": self.category,
            "type": "SETTLEMENT",
            "limit": max(1, min(int(limit), 200)),
        }
        if start_time_ms is not None:
            base_params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            base_params["endTime"] = int(end_time_ms)
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max(1, int(max_pages))):
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            payload = self._call_optional(("get_transaction_log",), **params)
            if not payload:
                break
            result = payload.get("result", {})
            rows.extend(result.get("list", []))
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return rows

    def set_leverage(self, *, symbol: str, buy_leverage: float = 1.0, sell_leverage: float | None = None) -> dict[str, Any]:
        if buy_leverage <= 0.0:
            raise ValueError("buy_leverage must be positive")
        effective_sell = buy_leverage if sell_leverage is None else sell_leverage
        if effective_sell <= 0.0:
            raise ValueError("sell_leverage must be positive")
        # Retry a transient set_leverage failure rather than silently dropping an
        # otherwise-valid entry. _call_once (not _call) keeps the original error
        # text -- which carries the "110043 not modified" marker -- intact, and a
        # 110043 reject returns immediately without wasting retries.
        attempts = max(self.retries, 1)
        last_error: BybitDataError = BybitDataError("Bybit set_leverage failed")
        for attempt in range(attempts):
            try:
                payload = self._call_once(
                    "set_leverage",
                    category=self.category,
                    symbol=symbol,
                    buyLeverage=_leverage_text(buy_leverage),
                    sellLeverage=_leverage_text(effective_sell),
                )
            except BybitDataError as exc:
                message = str(exc).lower()
                if "110043" in message or "not modified" in message:
                    return {
                        "symbol": symbol,
                        "buyLeverage": _leverage_text(buy_leverage),
                        "sellLeverage": _leverage_text(effective_sell),
                        "retCode": 110043,
                    }
                last_error = exc
                if attempt + 1 >= attempts:
                    raise
                time.sleep(self.retry_sleep_seconds * (2**attempt))
                continue
            return payload.get("result", {})
        raise last_error  # pragma: no cover - the loop always returns or raises

    def set_trading_stop(
        self,
        *,
        symbol: str,
        tpsl_mode: str = "Full",
        position_idx: int = 0,
        stop_loss: str | float | None = None,
        take_profit: str | float | None = None,
        trailing_stop: str | float | None = None,
        active_price: str | float | None = None,
        tp_trigger_by: str | None = "MarkPrice",
        sl_trigger_by: str | None = "MarkPrice",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "tpslMode": tpsl_mode,
            "positionIdx": position_idx,
        }
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if trailing_stop is not None:
            params["trailingStop"] = str(trailing_stop)
        if active_price is not None:
            params["activePrice"] = str(active_price)
        if tp_trigger_by:
            params["tpTriggerBy"] = tp_trigger_by
        if sl_trigger_by:
            params["slTriggerBy"] = sl_trigger_by
        payload = self._call_once("set_trading_stop", **params)
        return payload.get("result", {})

    def _call_optional(self, method_names: Iterable[str], **params: Any) -> dict[str, Any] | None:
        for method_name in method_names:
            if hasattr(self._client, method_name):
                return self._call(method_name, **params)
        return None

    def _call_once(self, method_name: str, **params: Any) -> dict[str, Any]:
        method = getattr(self._client, method_name)
        try:
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            payload = method(**params)
            ret_code = payload.get("retCode")
            if ret_code != 0:
                raise BybitDataError(f"Bybit {method_name} failed: {payload}")
            return payload
        except BybitDataError:
            raise
        except Exception as exc:  # noqa: BLE001 - pybit raises several transport types
            raise BybitDataError(f"Bybit {method_name} failed: {exc}") from exc

    def _call(self, method_name: str, **params: Any) -> dict[str, Any]:
        method = getattr(self._client, method_name)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                if self.rate_limiter is not None:
                    self.rate_limiter.acquire()
                payload = method(**params)
                ret_code = payload.get("retCode")
                if ret_code != 0:
                    raise BybitDataError(f"Bybit {method_name} failed: {payload}")
                return payload
            except Exception as exc:  # noqa: BLE001 - pybit raises several transport types
                last_error = exc
                # A non-zero retCode that is not a rate-limit is a definite venue
                # reject -- retrying the identical request only repeats it and
                # wastes the backoff. Transport errors and rate-limits still retry.
                if isinstance(exc, BybitDataError) and not _is_rate_limit(exc):
                    raise
                if attempt + 1 >= self.retries:
                    break
                time.sleep(self.retry_sleep_seconds * (2**attempt))
        raise BybitDataError(f"Bybit {method_name} failed after retries") from last_error


def _leverage_text(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _is_rate_limit(value: Any) -> bool:
    text = str(value).lower()
    return "10006" in text or "rate limit" in text or "too many visits" in text


@dataclass(slots=True)
class BybitPublicTradeStream:
    category: str = "linear"
    testnet: bool = False
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if WebSocket is None:
            raise RuntimeError("pybit is required for BybitPublicTradeStream")
        _patch_pybit_daemon_ping_timer()
        self._client = WebSocket(testnet=self.testnet, channel_type=self.category)

    def subscribe_public_trades(self, symbols: str | list[str], callback: Any) -> None:
        if isinstance(symbols, str):
            symbol_arg: str | list[str] = symbols
        else:
            symbol_arg = list(symbols)
        self._client.trade_stream(symbol=symbol_arg, callback=callback)

    def close(self) -> None:
        for name in ("exit", "close", "stop"):
            method = getattr(self._client, name, None)
            if callable(method):
                method()
                return


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
        self._client = WebSocket(testnet=self.testnet, demo=self.demo, channel_type=self.category)

    def subscribe_tickers(self, symbols: str | list[str], callback: Any) -> None:
        if isinstance(symbols, str):
            self._client.ticker_stream(symbol=symbols, callback=callback)
            return
        chunk = max(self.subscribe_args_per_message, 1)
        symbol_list = list(symbols)
        for i in range(0, len(symbol_list), chunk):
            slice_ = symbol_list[i : i + chunk]
            self._client.ticker_stream(symbol=slice_, callback=callback)

    def close(self) -> None:
        _close_ws_client(self._client)


@dataclass(slots=True)
class BybitPrivateWebSocketStream:
    category: str = "linear"
    testnet: bool = False
    demo: bool = True
    api_key: str | None = None
    api_secret: str | None = None
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if WebSocket is None:
            raise RuntimeError("pybit is required for BybitPrivateWebSocketStream")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Bybit private websocket stream requires API key and secret")
        _patch_pybit_daemon_ping_timer()
        self._client = WebSocket(
            testnet=self.testnet,
            demo=self.demo,
            channel_type="private",
            api_key=self.api_key,
            api_secret=self.api_secret,
        )

    def subscribe_positions(self, callback: Any) -> None:
        self._client.position_stream(callback=callback)

    def subscribe_orders(self, callback: Any) -> None:
        self._client.order_stream(callback=callback)

    def subscribe_executions(self, callback: Any, *, fast: bool = False) -> None:
        if fast and hasattr(self._client, "fast_execution_stream"):
            self._client.fast_execution_stream(callback=callback)
            return
        self._client.execution_stream(callback=callback)

    def subscribe_wallet(self, callback: Any) -> None:
        """Subscribe to wallet balance pushes. Bybit pushes a per-account
        snapshot every time a balance changes. Required for live equity
        reads to bypass the per-cycle REST get_wallet_balance call."""
        self._client.wallet_stream(callback=callback)

    def close(self) -> None:
        _close_ws_client(self._client)


class BybitTradeRouter:
    """Route order placement + cancellation through WS first, REST as fallback.

    Exposes the same surface as :class:`BybitPrivateClient` so cycle code
    that calls ``trading_client.place_order(**params)`` is a drop-in user:
    no caller change needed. Internally:

      1. If a :class:`BybitWebSocketTradeClient` is wired AND ``order_submit_mode``
         allows WS, try WS first. Bybit's WS trade ack arrives in <50ms.
      2. If WS is unavailable, times out, or returns a non-zero retCode and
         ``rest_fallback`` is true, fall back to ``BybitPrivateClient.place_order``.
         The router never raises just because WS was unavailable — REST is
         the safety net.
      3. If ``order_submit_mode == "rest"``, skip WS entirely. This is the
         opt-out for operators who want to run REST-only for safety.

    Non-order methods (``set_leverage``, ``get_positions``, ``get_open_orders``,
    ``get_wallet_balance``, ``get_trade_history``, ``get_order_history``) pass
    straight through to REST — there is no WS equivalent for those at Bybit.

    Bybit demo currently rejects WS trade order entry; on demo, the router's
    first WS attempt typically returns a non-zero retCode and we transparently
    fall back to REST. The same code path works unchanged when REAL_MONEY is
    flipped on — WS placement starts succeeding and saves ~150-200ms per order.

    Submission stats are exposed via :meth:`stats` so the operator can
    observe whether placement is going WS or REST in production telemetry.
    """

    DEFAULT_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        *,
        rest_client: Any,
        ws_client: Any | None = None,
        order_submit_mode: str = "ws_then_rest",
        rest_fallback: bool = True,
        ws_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if rest_client is None:
            raise ValueError("rest_client is required (it is the failsafe path)")
        if order_submit_mode not in {"ws", "ws_then_rest", "rest"}:
            raise ValueError("order_submit_mode must be ws, ws_then_rest, or rest")
        if order_submit_mode == "ws" and rest_fallback:
            raise ValueError("order_submit_mode='ws' is incompatible with rest_fallback=True")
        if ws_timeout_seconds <= 0.0:
            raise ValueError("ws_timeout_seconds must be positive")
        self._rest = rest_client
        self._ws = ws_client
        self._mode = order_submit_mode
        self._rest_fallback = bool(rest_fallback)
        self._ws_timeout_seconds = float(ws_timeout_seconds)
        self._lock = threading.Lock()
        self._ws_attempts = 0
        self._ws_successes = 0
        self._ws_timeouts = 0
        self._ws_rejects = 0
        self._ws_exceptions = 0
        self._rest_fallbacks = 0
        self._rest_only = 0
        # Incremented when a WS timeout's REST-fallback was suppressed
        # because the probe found the order already at Bybit (the WS submit
        # had reached the venue but the ack network-delayed past the
        # timeout). Tracks how often the probe is saving us from a
        # double-submit race.
        self._ws_timeout_probe_recovered = 0
        self._ws_timeout_probe_attempts = 0

    # -- order placement / cancellation -------------------------------

    def place_order(self, **params: Any) -> dict[str, Any]:
        if "orderLinkId" not in params:
            raise ValueError("orderLinkId is required for idempotent Bybit order submission")
        if self._should_attempt_ws():
            try:
                return self._ws_call_sync("place_order", **params)
            except _RouterWsFailed as failure:
                if not self._rest_fallback:
                    raise
                _logger_trade_router.info(
                    "place_order WS failed (%s); REST fallback symbol=%s link=%s",
                    failure.kind, params.get("symbol"), params.get("orderLinkId"),
                )
                with self._lock:
                    self._rest_fallbacks += 1
                # On a WS timeout the submit may have reached Bybit before
                # the ack network-delayed past _ws_timeout_seconds. A REST
                # resubmit then races: Bybit's per-orderLinkId dedup
                # USUALLY catches it (retCode=110089), but the race window
                # between Bybit ingesting the WS submit and processing the
                # REST submit is not formally guaranteed to dedup. Probe
                # order history by orderLinkId first; if Bybit already has
                # the order, return that instead of resubmitting.
                if failure.kind == "timeout":
                    probed = self._probe_existing_order(
                        symbol=params.get("symbol"),
                        order_link_id=params["orderLinkId"],
                    )
                    if probed is not None:
                        _logger_trade_router.info(
                            "place_order WS timeout but order present on probe; "
                            "skipping REST resubmit symbol=%s link=%s",
                            params.get("symbol"), params["orderLinkId"],
                        )
                        return probed
        else:
            with self._lock:
                self._rest_only += 1
        return self._rest.place_order(**params)

    def cancel_order(self, *, symbol: str, order_link_id: str) -> dict[str, Any]:
        if self._should_attempt_ws():
            try:
                return self._ws_call_sync(
                    "cancel_order", symbol=symbol, orderLinkId=order_link_id,
                )
            except _RouterWsFailed as failure:
                if not self._rest_fallback:
                    raise
                _logger_trade_router.info(
                    "cancel_order WS failed (%s); REST fallback symbol=%s link=%s",
                    failure.kind, symbol, order_link_id,
                )
                with self._lock:
                    self._rest_fallbacks += 1
        else:
            with self._lock:
                self._rest_only += 1
        return self._rest.cancel_order(symbol=symbol, order_link_id=order_link_id)

    # -- pass-throughs (no WS equivalent) -----------------------------

    def __getattr__(self, name: str) -> Any:
        # Forward any other attribute access to the REST client so this
        # router is a true drop-in replacement. set_leverage, get_positions,
        # get_open_orders, get_wallet_balance, get_trade_history,
        # get_order_history, cancel_all_orders — all route through REST.
        # __getattr__ is only invoked for attributes NOT found on the
        # router itself, so place_order/cancel_order keep their override.
        return getattr(self._rest, name)

    # -- introspection ------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self._mode,
                "rest_fallback": self._rest_fallback,
                "ws_wired": self._ws is not None,
                "ws_attempts": self._ws_attempts,
                "ws_successes": self._ws_successes,
                "ws_timeouts": self._ws_timeouts,
                "ws_rejects": self._ws_rejects,
                "ws_exceptions": self._ws_exceptions,
                "rest_fallbacks": self._rest_fallbacks,
                "rest_only": self._rest_only,
                "ws_timeout_probe_attempts": self._ws_timeout_probe_attempts,
                "ws_timeout_probe_recovered": self._ws_timeout_probe_recovered,
            }

    # -- internals ----------------------------------------------------

    def _should_attempt_ws(self) -> bool:
        return self._mode in {"ws", "ws_then_rest"} and self._ws is not None

    def _ws_call_sync(self, method: str, **params: Any) -> dict[str, Any]:
        """Issue a WS call and block (with timeout) for the ack.

        Returns the ack's ``data`` field on success (mirroring REST's
        ``result`` shape so the caller sees identical structure). Raises
        ``_RouterWsFailed`` on any failure mode — timeout, non-zero
        retCode, transport exception. The caller decides whether to
        REST-fallback based on ``rest_fallback``."""
        with self._lock:
            self._ws_attempts += 1
        completed = threading.Event()
        ack_holder: dict[str, Any] = {}

        def _on_ack(message: Any) -> None:
            ack_holder["message"] = message
            completed.set()

        try:
            ws_method = getattr(self._ws, method)
            ws_method(_on_ack, **params)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._ws_exceptions += 1
            raise _RouterWsFailed("exception", str(exc)) from exc

        if not completed.wait(timeout=self._ws_timeout_seconds):
            with self._lock:
                self._ws_timeouts += 1
            raise _RouterWsFailed("timeout", f"no ack within {self._ws_timeout_seconds}s")

        message = ack_holder.get("message")
        if not isinstance(message, Mapping):
            with self._lock:
                self._ws_rejects += 1
            raise _RouterWsFailed("malformed_ack", repr(message)[:200])
        ret_code = message.get("retCode")
        if ret_code != 0:
            with self._lock:
                self._ws_rejects += 1
            ret_msg = str(message.get("retMsg") or message.get("ret_msg") or "")
            raise _RouterWsFailed(
                "rejected", f"retCode={ret_code} retMsg={ret_msg}",
            )
        with self._lock:
            self._ws_successes += 1
        data = message.get("data")
        return dict(data) if isinstance(data, Mapping) else {}

    def _probe_existing_order(
        self, *, symbol: str | None, order_link_id: str,
    ) -> dict[str, Any] | None:
        """Look up an order by orderLinkId after a WS timeout. Returns a
        place_order-shaped dict (``{"orderId", "orderLinkId"}``) when Bybit
        already has the order, else None. Probe failures (transport error,
        endpoint missing) return None — the caller then REST-falls-back as
        before, so the probe never makes things worse than the old path."""
        if not order_link_id:
            return None
        with self._lock:
            self._ws_timeout_probe_attempts += 1
        rows: list[dict[str, Any]] = []
        # Check open orders first (lighter call than history scan, and an
        # order ack-delayed past timeout is still open at the matching
        # engine in the common case).
        try:
            open_rows = self._rest.get_open_orders(symbol=symbol) if symbol else self._rest.get_open_orders()
        except Exception:  # noqa: BLE001 - any transport / endpoint failure → fall through
            open_rows = []
        rows.extend(
            row for row in (open_rows or [])
            if str(row.get("orderLinkId") or "") == order_link_id
        )
        if not rows:
            try:
                history = self._rest.get_order_history(
                    symbol=symbol, order_link_id=order_link_id, limit=10,
                )
            except Exception:  # noqa: BLE001
                history = []
            rows.extend(history or [])
        if not rows:
            return None
        # Pick the most-recently-created row (history is usually newest-first
        # already, but be defensive — only one orderLinkId per UID per window,
        # so this is conservative).
        chosen = max(
            rows,
            key=lambda r: int(r.get("createdTime") or r.get("updatedTime") or 0),
        )
        with self._lock:
            self._ws_timeout_probe_recovered += 1
        return dict(chosen)


class _RouterWsFailed(RuntimeError):
    """Internal signal that the WS submission failed — the router decides
    whether to fall back to REST based on its configuration."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


_logger_trade_router = logging.getLogger("liquidity_migration.bybit.trade_router")


@dataclass(slots=True)
class BybitWebSocketTradeClient:
    category: str = "linear"
    testnet: bool = False
    demo: bool = True
    api_key: str | None = None
    api_secret: str | None = None
    recv_window: int = 1000
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if WebSocketTrading is None:
            raise RuntimeError("pybit is required for BybitWebSocketTradeClient")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Bybit private websocket trading requires API key and secret")
        self._client = WebSocketTrading(
            testnet=self.testnet,
            demo=self.demo,
            api_key=self.api_key,
            api_secret=self.api_secret,
            recv_window=self.recv_window,
        )

    def place_order(self, callback: Any, **params: Any) -> None:
        if "orderLinkId" not in params:
            raise ValueError("orderLinkId is required for idempotent Bybit websocket order submission")
        self._client.place_order(callback, category=self.category, **params)

    def cancel_order(self, callback: Any, *, symbol: str, order_link_id: str) -> None:
        self._client.cancel_order(callback, category=self.category, symbol=symbol, orderLinkId=order_link_id)

    def close(self) -> None:
        _close_ws_client(self._client)


def build_ws_trade_client(
    *,
    category: str,
    testnet: bool,
    demo: bool,
    api_key: str | None,
    api_secret: str | None,
    recv_window: int = 1000,
    attempts: int = 4,
    base_backoff_seconds: float = 0.5,
    max_backoff_seconds: float = 8.0,
    initial_jitter_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[float, float], float] = random.uniform,
) -> BybitWebSocketTradeClient:
    """Build a WS trade client, retrying with jittered exponential backoff.

    pybit's ``WebSocketTrading`` connects synchronously in its constructor and,
    on repeated failure, raises AND permanently stops reconnecting on that
    client ("Too many connection attempts. pybit will no longer try to
    reconnect"). On the demo endpoint this is hit when several daemons open
    auth-WS connections from one IP at once (per-IP connection-attempt rate
    limit). The fix:

      * each retry builds a FRESH client (the old one won't reconnect);
      * an initial random jitter de-syncs the simultaneous boot of the short /
        long / risk daemons so their first attempts don't collide;
      * full-jitter exponential backoff spreads the retries.

    On mainnet the first attempt typically succeeds instantly. If all attempts
    fail the last error is raised so the caller falls back to REST (the
    seatbelt). ``sleep``/``rng`` are injectable for deterministic tests.

    PERMANENT errors (pybit not installed, or missing credentials) raise
    IMMEDIATELY with no jitter/backoff — retrying can't fix them, and this keeps
    credential-less unit tests fast and network-free."""
    if WebSocketTrading is None:
        raise RuntimeError("pybit is required for BybitWebSocketTradeClient")
    if not api_key or not api_secret:
        raise RuntimeError("Bybit private websocket trading requires API key and secret")
    if initial_jitter_seconds > 0.0:
        sleep(rng(0.0, initial_jitter_seconds))
    last_exc: Exception | None = None
    attempts = max(1, attempts)
    for i in range(attempts):
        try:
            return BybitWebSocketTradeClient(
                category=category,
                testnet=testnet,
                demo=demo,
                api_key=api_key,
                api_secret=api_secret,
                recv_window=recv_window,
            )
        except Exception as exc:  # noqa: BLE001 - retry transient connect failures; caller REST-falls-back
            last_exc = exc
            if i + 1 >= attempts:
                break
            backoff = min(max_backoff_seconds, base_backoff_seconds * (2 ** i))
            backoff += rng(0.0, backoff)  # full jitter
            logging.getLogger("liquidity_migration.bybit").info(
                "ws trade client connect attempt %d/%d failed (%s); retrying in %.1fs",
                i + 1, attempts, str(exc)[:140], backoff,
            )
            sleep(backoff)
    raise last_exc if last_exc is not None else RuntimeError("ws trade client build failed")


def _close_ws_client(client: Any, *, timeout_seconds: float = 3.0) -> None:
    """Close a pybit WS client with a hard timeout.

    pybit's exit/close/stop methods can occasionally hang (especially when
    the underlying TCP socket is in a half-closed state). Without a
    timeout, daemon shutdown waits indefinitely for the WS to die — and
    systemd then SIGKILLs the whole process. Running the close on a
    background thread with a join timeout means a stuck close costs us
    `timeout_seconds` per WS instead of unbounded blocking; the resources
    leak (until process exit) but shutdown proceeds."""
    timer = getattr(client, "_agc_ping_timer", None)
    if timer is not None:
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("liquidity_migration.bybit").debug(
                "ws close raised: %s", exc,
            )

    thread = threading.Thread(target=_run, name="ws-close", daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        logging.getLogger("liquidity_migration.bybit").warning(
            "ws close did not return within %.1fs; abandoning thread", timeout_seconds,
        )


def _patch_pybit_daemon_ping_timer() -> None:
    try:
        _websocket_stream = importlib.import_module("pybit._websocket_stream")
    except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
        return
    manager = getattr(_websocket_stream, "_V5WebSocketManager", None)
    if manager is None or getattr(manager, "_agc_daemon_ping_timer", False):
        return

    def _send_initial_ping(self: Any) -> None:
        timer = threading.Timer(self.ping_interval, self._send_custom_ping)
        timer.daemon = True
        self._agc_ping_timer = timer
        timer.start()

    manager._send_initial_ping = _send_initial_ping
    manager._agc_daemon_ping_timer = True


_logger_ws_klines = logging.getLogger("liquidity_migration.bybit.ws_klines")


def _default_kline_websocket_factory(*, testnet: bool, demo: bool, channel_type: str) -> Any:
    """Create a fresh pybit WebSocket client tuned for kline streams."""
    if WebSocket is None:
        raise RuntimeError("pybit is required for BybitKlineStreamPool")
    _patch_pybit_daemon_ping_timer()
    return WebSocket(testnet=testnet, demo=demo, channel_type=channel_type)


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
    # lock (which previously blocked subscribe/stats for backoff×N seconds on a
    # multi-connection reconnect). 0.0 = never attempted, so first reconnect is
    # immediate.
    last_reconnect_monotonic: float = 0.0


class BybitKlineStreamPool:
    """Multi-connection WebSocket pool for 1h kline subscriptions.

    Splits a large symbol universe across N pybit ``WebSocket`` clients (one
    "connection" each, since pybit's WebSocket abstraction owns its own thread
    + reconnect loop). Re-routes the per-bar callbacks into a single
    ``on_bar(symbol, bar, confirmed)`` interface that the store consumes.

    Operations:

    * ``subscribe(symbols, on_bar)``: partitions the symbol set across
      ``topics_per_connection`` slices, opens one connection per slice with
      a small inter-connection delay (Bybit allows 500 connects/IP/5min on
      public; this stays well clear), then subscribes each slice's symbols.
    * ``update_subscriptions(new_symbols)``: diffs against the current
      assignment, unsubscribes removed symbols (per-connection), adds new
      symbols to existing connections with capacity, and creates fresh
      connections when capacity is exhausted.
    * Watchdog: a background thread monitors per-connection
      ``last_message_monotonic``. A connection with no message in
      ``stale_warning_seconds`` is logged; one with no message in
      ``stale_reconnect_seconds`` is torn down and rebuilt with its same
      slice (the WS subscription is re-issued from scratch).
    * ``close()``: stops the watchdog and closes every connection.

    The pool is dependency-injectable: ``websocket_factory`` builds the
    underlying client (default uses pybit's ``WebSocket``); tests pass a fake
    factory so they can synthesise bar events without a live connection.
    """

    DEFAULT_TOPICS_PER_CONNECTION = 180
    DEFAULT_STALE_WARNING_SECONDS = 60.0
    DEFAULT_STALE_RECONNECT_SECONDS = 180.0
    DEFAULT_WATCHDOG_INTERVAL_SECONDS = 10.0
    DEFAULT_CONNECTION_SPACING_SECONDS = 0.1
    DEFAULT_RECONNECT_BACKOFF_SECONDS = 5.0
    # Bybit V5 caps args list per WS subscription message; the conservative
    # cap is 10 (spot tier) but linear/inverse have looser caps. Stay under
    # 10 so a single subscribe call never gets bounced. We then issue
    # multiple subscribe calls under the same WebSocket, which pybit
    # supports (each new kline_stream invocation queues another subscribe
    # frame). The per-symbol-chunk loop is bounded by topics_per_connection.
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
        with self._lock:
            if self._closed:
                raise RuntimeError("pool is closed")
            unique_symbols = sorted({s for s in symbols if s})
            if self._on_bar is None:
                self._on_bar = on_bar
            elif self._on_bar is not on_bar:
                # Re-subscribing with a different callback is supported but
                # rare; the new callback replaces the old for every connection.
                self._on_bar = on_bar
            if not self._connections:
                self._build_initial_connections_locked(unique_symbols)
            else:
                self.update_subscriptions(set(unique_symbols))

    def update_subscriptions(self, new_symbols: set[str]) -> dict[str, int]:
        """Diff the current assignment against ``new_symbols``: subscribe to
        adds, unsubscribe from removals. Returns counts."""
        with self._lock:
            if self._closed:
                raise RuntimeError("pool is closed")
            if self._on_bar is None:
                raise RuntimeError("subscribe() must be called before update_subscriptions()")
            current = set(self._symbol_to_connection)
            adds = sorted(new_symbols - current)
            removes = sorted(current - new_symbols)
            for symbol in removes:
                self._unsubscribe_symbol_locked(symbol)
            for symbol in adds:
                self._subscribe_symbol_locked(symbol)
            return {"added": len(adds), "removed": len(removes), "connections": len(self._connections)}

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
        self._connections.append(state)
        if initial_symbols:
            self._subscribe_to_connection_locked(state, initial_symbols)
        return state

    def _subscribe_symbol_locked(self, symbol: str) -> None:
        # Find an OPEN connection under capacity, else open a new one.
        # Previously the "find under capacity" check didn't filter on
        # state.closed, so a connection waiting on a failed reconnect
        # retry (closed=True, assigned_symbols < cap) could be picked
        # as the target — and kline_stream() on a dead client would
        # either no-op or raise, silently losing the new symbol's WS
        # feed.
        target = next(
            (
                state for state in self._connections
                if not state.closed
                and len(state.assigned_symbols) < self.topics_per_connection
            ),
            None,
        )
        if target is None:
            target = self._open_connection_locked(initial_symbols=[symbol])
            return
        self._subscribe_to_connection_locked(target, [symbol])

    def _subscribe_to_connection_locked(
        self, state: _KlineConnectionState, symbols: list[str]
    ) -> None:
        if not symbols:
            return
        callback = self._make_callback(state)
        # Chunk the subscribe so each WS message stays under Bybit's per-message
        # args cap. pybit accepts repeated kline_stream calls per WebSocket;
        # each issues another subscribe frame on the same connection.
        chunk = self.subscribe_args_per_message
        symbols_list = list(symbols)
        for i in range(0, len(symbols_list), chunk):
            slice_ = symbols_list[i : i + chunk]
            try:
                state.client.kline_stream(
                    interval=self.interval_minutes,
                    symbol=slice_,
                    callback=callback,
                )
            except Exception as exc:  # noqa: BLE001 - log and propagate
                _logger_ws_klines.warning(
                    "kline_stream subscribe failed conn=%d slice=%d/%d: %s",
                    state.index, len(slice_), len(symbols_list), exc,
                )
                raise
            for symbol in slice_:
                state.assigned_symbols.add(symbol)
                self._symbol_to_connection[symbol] = state.index

    def _unsubscribe_symbol_locked(self, symbol: str) -> None:
        index = self._symbol_to_connection.pop(symbol, None)
        if index is None or index >= len(self._connections):
            return
        state = self._connections[index]
        state.assigned_symbols.discard(symbol)
        topic = f"kline.{self.interval_minutes}.{symbol}"
        unsubscribe = getattr(state.client, "unsubscribe", None)
        if callable(unsubscribe):
            try:
                unsubscribe(topic=topic)
            except Exception as exc:  # noqa: BLE001
                _logger_ws_klines.warning(
                    "kline unsubscribe failed conn=%d symbol=%s: %s",
                    state.index, symbol, exc,
                )

    def _make_callback(
        self, state: _KlineConnectionState
    ) -> Callable[[dict[str, Any]], None]:
        """Build a closure that parses pybit's kline message, marks the
        connection alive, and dispatches each bar through ``on_bar``.

        pybit delivers the full message dict: ``{"topic": "kline.60.SYMBOL",
        "data": [{"start": ..., "confirm": True, ...}, ...]}``. The pool's
        contract with consumers is ``on_bar(symbol, bar_dict, confirmed)`` —
        one call per bar in the message."""
        on_bar = self._on_bar
        if on_bar is None:  # defensive — subscribe() always sets this first
            raise RuntimeError("internal error: on_bar callback not set")

        def _callback(message: dict[str, Any]) -> None:
            state.message_count += 1
            state.last_message_monotonic = time.monotonic()
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
                for bar in data:
                    if not isinstance(bar, Mapping):
                        state.dropped_messages += 1
                        continue
                    confirmed = bool(bar.get("confirm", False))
                    try:
                        on_bar(symbol, dict(bar), confirmed)
                    except Exception as exc:  # noqa: BLE001
                        _logger_ws_klines.exception(
                            "on_bar callback raised conn=%d symbol=%s: %s",
                            state.index, symbol, exc,
                        )
            except Exception as exc:  # noqa: BLE001
                state.dropped_messages += 1
                _logger_ws_klines.exception(
                    "kline pool callback crashed conn=%d: %s", state.index, exc,
                )

        return _callback

    # -- watchdog + reconnect ------------------------------------------

    def start_watchdog(self) -> None:
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="kline-pool-watchdog", daemon=True
        )
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
            except Exception as exc:  # noqa: BLE001
                _logger_ws_klines.exception("watchdog tick failed: %s", exc)

    def check_stale_connections(self) -> int:
        """Inspect every connection's ``last_message_monotonic``. Connections
        idle past ``stale_reconnect_seconds`` are torn down and rebuilt with
        the same slice. Returns the number of reconnects performed.

        Also retries any connection where a PRIOR reconnect failed mid-way
        (state.closed=True but assigned_symbols still set) — without this,
        a single transient ``_websocket_factory`` failure would orphan
        every symbol on that slice until the next hourly universe refresh
        re-subscribed them. The watchdog ticks every ~10s, so persistent
        outages still surface to logs while transient blips recover."""
        reconnects = 0
        now = time.monotonic()
        with self._lock:
            to_reconnect: list[int] = []
            for state in list(self._connections):
                if not state.assigned_symbols:
                    continue
                # Per-connection backoff gate: a connection that attempted a
                # reconnect within the last backoff window is left for a later
                # watchdog tick. This replaces the old in-lock time.sleep so the
                # pool lock is never held across the backoff (the sleep blocked
                # subscribe/update_subscriptions/stats for backoff×N seconds on a
                # multi-connection reconnect). backoff < watchdog interval, so the
                # gate never blocks a connection indefinitely.
                if (
                    state.last_reconnect_monotonic > 0.0
                    and now - state.last_reconnect_monotonic < self.reconnect_backoff_seconds
                ):
                    continue
                if state.closed:
                    # Prior reconnect failed and left this slice without a
                    # live client. Retry now (the backoff gate above already
                    # protects the venue from a tight retry storm).
                    to_reconnect.append(state.index)
                    continue
                gap = now - state.last_message_monotonic
                if gap >= self.stale_reconnect_seconds:
                    to_reconnect.append(state.index)
                elif gap >= self.stale_warning_seconds:
                    self._stale_warnings_total += 1
                    _logger_ws_klines.warning(
                        "kline connection idle: conn=%d gap=%.1fs symbols=%d",
                        state.index, gap, len(state.assigned_symbols),
                    )
            for index in to_reconnect:
                self._reconnect_connection_locked(index)
                reconnects += 1
        return reconnects

    def _reconnect_connection_locked(self, index: int) -> None:
        if index >= len(self._connections):
            return
        state = self._connections[index]
        # Snapshot the slice BEFORE clearing — we need to preserve it so a
        # mid-reconnect failure leaves the watchdog enough state to retry
        # on the next tick. Previously assigned_symbols was cleared
        # eagerly; a transient _websocket_factory failure then orphaned
        # every symbol on that slice until the next hourly universe
        # refresh re-subscribed them. Now: keep assigned_symbols intact,
        # only clear on a SUCCESSFUL resubscribe (which rebuilds the set
        # in _subscribe_to_connection_locked).
        slice_symbols = sorted(state.assigned_symbols)
        # Stamp the attempt BEFORE doing any work so the watchdog's backoff gate
        # spaces the next retry even if the factory build below raises. This
        # replaces the old in-lock time.sleep(backoff) that throttled storms at
        # the cost of holding the pool lock for the whole sleep.
        state.last_reconnect_monotonic = time.monotonic()
        _logger_ws_klines.warning(
            "kline connection reconnect conn=%d symbols=%d", index, len(slice_symbols),
        )
        try:
            self._close_state(state)
        except Exception as exc:  # noqa: BLE001
            _logger_ws_klines.warning("close on reconnect failed conn=%d: %s", index, exc)
        try:
            new_client = self._websocket_factory(
                testnet=self.testnet, demo=self.demo, channel_type=self.category,
            )
        except Exception as exc:  # noqa: BLE001
            _logger_ws_klines.exception(
                "kline reconnect failed to build new client conn=%d: %s; "
                "watchdog will retry on next tick", index, exc,
            )
            # State stays closed=True with assigned_symbols populated; the
            # watchdog's closed+assigned branch above picks it up next tick.
            return
        # Successful new client: clear the stale symbol→conn mapping (the
        # closed client's entries are now invalid) and assigned_symbols
        # (subscribe rebuilds it), then resubscribe.
        for symbol in slice_symbols:
            self._symbol_to_connection.pop(symbol, None)
        state.assigned_symbols.clear()
        state.client = new_client
        state.closed = False
        state.last_message_monotonic = time.monotonic()
        state.reconnect_count += 1
        self._reconnects_total += 1
        if slice_symbols:
            try:
                self._subscribe_to_connection_locked(state, slice_symbols)
            except Exception as exc:  # noqa: BLE001
                _logger_ws_klines.exception(
                    "kline reconnect resubscribe failed conn=%d: %s; "
                    "marking closed for retry", index, exc,
                )
                # Subscribe failure: put the slice back on assigned_symbols
                # and mark closed so the watchdog retries.
                state.assigned_symbols.update(slice_symbols)
                self._close_state(state)

    # -- shutdown -------------------------------------------------------

    def close(self) -> None:
        self.stop_watchdog()
        with self._lock:
            self._closed = True
            for state in self._connections:
                try:
                    self._close_state(state)
                except Exception as exc:  # noqa: BLE001
                    _logger_ws_klines.warning(
                        "close failed conn=%d: %s", state.index, exc,
                    )
            self._connections.clear()
            self._symbol_to_connection.clear()

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
            per_conn = [
                {
                    "index": state.index,
                    "topics": len(state.assigned_symbols),
                    "messages": state.message_count,
                    "dropped": state.dropped_messages,
                    "reconnects": state.reconnect_count,
                    "idle_seconds": round(now - state.last_message_monotonic, 3),
                    "closed": state.closed,
                }
                for state in self._connections
            ]
            return {
                "connections": len(self._connections),
                "subscribed_symbols": len(self._symbol_to_connection),
                "reconnects_total": self._reconnects_total,
                "stale_warnings_total": self._stale_warnings_total,
                "per_connection": per_conn,
            }

    def subscribed_symbols(self) -> set[str]:
        with self._lock:
            return set(self._symbol_to_connection)


def _symbol_from_kline_topic(topic: str) -> str | None:
    """Extract the symbol component from a kline topic ``kline.60.SYMBOL``."""
    if not topic.startswith("kline."):
        return None
    parts = topic.split(".", 2)
    if len(parts) != 3 or not parts[2]:
        return None
    return parts[2]
