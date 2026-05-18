from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BinanceDataError(RuntimeError):
    pass


BINANCE_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


@dataclass(slots=True)
class BinanceUSDMData:
    base_url: str = "https://fapi.binance.com"
    retries: int = 3
    retry_sleep_seconds: float = 0.75
    timeout_seconds: float = 15.0
    calls: int = field(init=False, default=0)
    retry_events: int = field(init=False, default=0)
    error_events: int = field(init=False, default=0)
    last_error: str = field(init=False, default="")

    def get_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1500) -> list[list[Any]]:
        return self._paged_kline("/fapi/v1/klines", symbol, interval, start, end, limit=limit)

    def get_mark_price_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1500) -> list[list[Any]]:
        return self._paged_kline("/fapi/v1/markPriceKlines", symbol, interval, start, end, limit=limit)

    def get_index_price_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1500) -> list[list[Any]]:
        return self._paged_kline("/fapi/v1/indexPriceKlines", symbol, interval, start, end, limit=limit, pair_param=True)

    def get_premium_index_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1500) -> list[list[Any]]:
        return self._paged_kline("/fapi/v1/premiumIndexKlines", symbol, interval, start, end, limit=limit)

    def get_funding_history(self, symbol: str, start: int, end: int, limit: int = 1000) -> list[dict[str, Any]]:
        return self._paged_forward(
            "/fapi/v1/fundingRate",
            symbol=symbol,
            start=start,
            end=end,
            limit=min(limit, 1000),
            timestamp_key="fundingTime",
            extra_params={},
        )

    def get_open_interest_hist(self, symbol: str, period: str, start: int, end: int, limit: int = 500) -> list[dict[str, Any]]:
        start = _recent_history_start(start, end, days=30)
        start = _ceil_to_period(start, period)
        end = _floor_to_period(end, period)
        if start > end:
            return []
        return self._paged_forward(
            "/futures/data/openInterestHist",
            symbol=symbol,
            start=start,
            end=end,
            limit=min(limit, 500),
            timestamp_key="timestamp",
            extra_params={"period": period},
            step_ms=BINANCE_INTERVAL_MS[period],
        )

    def get_taker_buy_sell_volume(self, symbol: str, period: str, start: int, end: int, limit: int = 500) -> list[dict[str, Any]]:
        start = _recent_history_start(start, end, days=30)
        start = _ceil_to_period(start, period)
        end = _floor_to_period(end, period)
        if start > end:
            return []
        return self._paged_forward(
            "/futures/data/takerlongshortRatio",
            symbol=symbol,
            start=start,
            end=end,
            limit=min(limit, 500),
            timestamp_key="timestamp",
            extra_params={"period": period},
            step_ms=BINANCE_INTERVAL_MS[period],
        )

    def _paged_kline(
        self,
        path: str,
        symbol: str,
        interval: str,
        start: int,
        end: int,
        *,
        limit: int,
        pair_param: bool = False,
    ) -> list[list[Any]]:
        interval_ms = BINANCE_INTERVAL_MS[interval]
        rows_by_ts: dict[int, list[Any]] = {}
        cursor = start
        while cursor <= end:
            params: dict[str, Any] = {
                "interval": interval,
                "startTime": cursor,
                "endTime": end,
                "limit": min(limit, 1500),
            }
            params["pair" if pair_param else "symbol"] = symbol
            batch = self._get(path, params)
            if not isinstance(batch, list):
                raise BinanceDataError(f"Binance {path} returned non-list payload")
            if not batch:
                break
            for row in batch:
                ts = int(row[0])
                if start <= ts <= end:
                    rows_by_ts[ts] = row
            latest = max(int(row[0]) for row in batch)
            next_cursor = latest + interval_ms
            if next_cursor <= cursor or next_cursor > end:
                break
            cursor = next_cursor
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def _paged_forward(
        self,
        path: str,
        *,
        symbol: str,
        start: int,
        end: int,
        limit: int,
        timestamp_key: str,
        extra_params: dict[str, Any],
        step_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        rows_by_ts: dict[int, dict[str, Any]] = {}
        cursor = start
        while cursor <= end:
            params = {
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end,
                "limit": limit,
                **extra_params,
            }
            batch = self._get(path, params)
            if not isinstance(batch, list):
                raise BinanceDataError(f"Binance {path} returned non-list payload")
            if not batch:
                break
            for row in batch:
                ts = int(row[timestamp_key])
                if start <= ts <= end:
                    rows_by_ts[ts] = row
            latest = max(int(row[timestamp_key]) for row in batch)
            next_cursor = latest + (step_ms or 1)
            if len(batch) < limit or next_cursor <= cursor or next_cursor > end:
                break
            cursor = next_cursor
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        url = f"{self.base_url}{path}?{urlencode(params)}"
        for attempt in range(self.retries):
            try:
                self.calls += 1
                request = Request(url, headers={"User-Agent": "model050426-data-layer/1.0"})
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and "code" in payload and int(payload.get("code") or 0) < 0:
                    raise BinanceDataError(f"Binance {path} failed: {payload}")
                return payload
            except Exception as exc:  # noqa: BLE001 - urllib raises transport-specific errors
                last_error = exc
                self.error_events += 1
                self.last_error = str(exc)[:500]
                if attempt + 1 >= self.retries:
                    break
                self.retry_events += 1
                time.sleep(self.retry_sleep_seconds * (2**attempt))
        raise BinanceDataError(f"Binance {path} failed after retries") from last_error

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "retry_events": self.retry_events,
            "error_events": self.error_events,
            "last_error": self.last_error,
        }


def _recent_history_start(start: int, end: int, *, days: int) -> int:
    window_ms = days * 24 * 60 * 60_000
    latest_available_start = int(time.time() * 1000) - window_ms
    return max(start, end - window_ms + 1, latest_available_start)


def _ceil_to_period(value: int, period: str) -> int:
    step = BINANCE_INTERVAL_MS[period]
    return value if value % step == 0 else ((value // step) + 1) * step


def _floor_to_period(value: int, period: str) -> int:
    step = BINANCE_INTERVAL_MS[period]
    return (value // step) * step
