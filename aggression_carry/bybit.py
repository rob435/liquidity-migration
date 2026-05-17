from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

try:
    from pybit.unified_trading import HTTP, WebSocket, WebSocketTrading
except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
    HTTP = None
    WebSocket = None
    WebSocketTrading = None


class BybitDataError(RuntimeError):
    pass


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

    def get_recent_trades(self, symbol: str, limit: int = 1000) -> list[dict[str, Any]]:
        payload = self._get("get_public_trade_history", category=self.category, symbol=symbol, limit=limit)
        return payload.get("result", {}).get("list", [])

    def get_funding_history(self, symbol: str, start: int, end: int, limit: int = 200) -> list[dict[str, Any]]:
        return self._paged_time_range("get_funding_rate_history", "fundingRateTimestamp", symbol=symbol, startTime=start, endTime=end, limit=limit)

    def get_tickers(self) -> list[dict[str, Any]]:
        payload = self._get("get_tickers", category=self.category)
        return payload.get("result", {}).get("list", [])

    def get_orderbook(self, symbol: str, limit: int = 25) -> dict[str, Any]:
        payload = self._get("get_orderbook", category=self.category, symbol=symbol, limit=limit)
        return payload.get("result", {})

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
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if HTTP is None:
            raise RuntimeError("pybit is required for BybitPrivateClient")
        if not self.demo:
            raise RuntimeError("BybitPrivateClient is wired for demo-only trading here; demo=False is refused")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Bybit demo execution requires API key and secret")
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

    def set_leverage(self, *, symbol: str, buy_leverage: float = 1.0, sell_leverage: float | None = None) -> dict[str, Any]:
        if buy_leverage <= 0.0:
            raise ValueError("buy_leverage must be positive")
        effective_sell = buy_leverage if sell_leverage is None else sell_leverage
        if effective_sell <= 0.0:
            raise ValueError("sell_leverage must be positive")
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
                return {"symbol": symbol, "buyLeverage": _leverage_text(buy_leverage), "sellLeverage": _leverage_text(effective_sell), "retCode": 110043}
            raise
        return payload.get("result", {})

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
                payload = method(**params)
                ret_code = payload.get("retCode")
                if ret_code != 0:
                    raise BybitDataError(f"Bybit {method_name} failed: {payload}")
                return payload
            except Exception as exc:  # noqa: BLE001 - pybit raises several transport types
                last_error = exc
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
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if WebSocket is None:
            raise RuntimeError("pybit is required for BybitPublicTickerStream")
        self._client = WebSocket(testnet=self.testnet, demo=self.demo, channel_type=self.category)

    def subscribe_tickers(self, symbols: str | list[str], callback: Any) -> None:
        symbol_arg: str | list[str] = symbols if isinstance(symbols, str) else list(symbols)
        self._client.ticker_stream(symbol=symbol_arg, callback=callback)

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
        if not self.demo:
            raise RuntimeError("BybitPrivateWebSocketStream is wired for demo-only trading here; demo=False is refused")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Bybit demo websocket stream requires API key and secret")
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

    def close(self) -> None:
        _close_ws_client(self._client)


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
        if not self.demo:
            raise RuntimeError("BybitWebSocketTradeClient is wired for demo-only trading here; demo=False is refused")
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Bybit demo websocket trading requires API key and secret")
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


def _close_ws_client(client: Any) -> None:
    for name in ("exit", "close", "stop"):
        method = getattr(client, name, None)
        if callable(method):
            method()
            return
