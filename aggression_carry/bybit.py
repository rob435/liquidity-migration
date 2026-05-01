from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

try:
    from pybit.unified_trading import HTTP
except ModuleNotFoundError:  # pragma: no cover - dependency may be absent before install
    HTTP = None


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
        rows: list[Any] = []
        interval_ms = INTERVAL_MS[interval] if interval in INTERVAL_MS else int(interval) * 60_000
        cursor = start
        seen: set[int] = set()
        while cursor < end:
            payload = self._get(
                "get_kline",
                category=self.category,
                symbol=symbol,
                interval=interval,
                start=cursor,
                end=end,
                limit=limit,
            )
            batch = payload.get("result", {}).get("list", [])
            if not batch:
                break
            rows.extend(batch)
            timestamps = sorted(int(item[0]) for item in batch if int(item[0]) not in seen)
            seen.update(timestamps)
            if not timestamps:
                break
            next_cursor = max(timestamps) + interval_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < limit:
                break
        return sorted(rows, key=lambda item: int(item[0]))

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

    def _paged_time_range(self, method_name: str, timestamp_key: str, **params: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = int(params["startTime"])
        end = int(params["endTime"])
        limit = int(params.get("limit", 200))
        seen: set[int] = set()
        while cursor < end:
            request_params = {**params, "startTime": cursor, "endTime": end}
            payload = self._get(method_name, category=self.category, **request_params)
            batch = payload.get("result", {}).get("list", [])
            if not batch:
                break
            rows.extend(batch)
            timestamps = sorted(int(item[timestamp_key]) for item in batch if int(item[timestamp_key]) not in seen)
            seen.update(timestamps)
            if not timestamps:
                break
            next_cursor = max(timestamps) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < limit:
                break
        return sorted(rows, key=lambda item: int(item[timestamp_key]))

    def _get(self, method_name: str, **params: Any) -> dict[str, Any]:
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
