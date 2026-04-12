from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import aiohttp

from config import Settings

LOGGER = logging.getLogger(__name__)


RATE_LIMIT_RETCODE = 10006


class MissingCandlesError(RuntimeError):
    pass


def is_rate_limited_payload(payload: dict) -> bool:
    return payload.get("retCode") == RATE_LIMIT_RETCODE


def interval_to_milliseconds(interval: str) -> int:
    interval = interval.strip()
    if interval == "D":
        return 24 * 60 * 60 * 1000
    if interval == "W":
        return 7 * 24 * 60 * 60 * 1000
    if interval == "M":
        return 30 * 24 * 60 * 60 * 1000
    match = re.fullmatch(r"(?i)(\d+)([mhdw])", interval)
    if match:
        value = int(match.group(1))
        unit = match.group(2).lower()
        factors = {
            "m": 60 * 1000,
            "h": 60 * 60 * 1000,
            "d": 24 * 60 * 60 * 1000,
            "w": 7 * 24 * 60 * 60 * 1000,
        }
        return value * factors[unit]
    return int(interval) * 60 * 1000


@dataclass(slots=True)
class BootstrapPayload:
    price_history: dict[str, list[tuple[int, float]]]
    btc_daily_history: list[tuple[int, float]]
    btcdom_history: list[tuple[int, float]]


@dataclass(slots=True)
class CacheStats:
    cache_rows: int
    cache_hits: int
    cache_misses: int
    cached_candles_stored: int
    bybit_http_requests: int
    binance_http_requests: int


@dataclass(slots=True)
class InstrumentSpec:
    symbol: str
    qty_step: Decimal
    min_order_qty: Decimal
    tick_size: Decimal


@dataclass(slots=True)
class VenuePosition:
    symbol: str
    side: str
    size: Decimal
    avg_price: Decimal
    position_idx: int


@dataclass(slots=True)
class ClosedPnlRecord:
    symbol: str
    order_id: str
    avg_entry_price: Decimal
    avg_exit_price: Decimal
    closed_pnl: Decimal
    updated_time_ms: int


@dataclass(slots=True)
class OrderAck:
    order_id: str
    order_link_id: str


@dataclass(slots=True)
class WalletBalance:
    total_equity_usd: Decimal
    total_available_balance_usd: Decimal


@dataclass(slots=True)
class HistoricalCandle:
    start_time_ms: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float


def round_decimal(value: Decimal, step: Decimal, rounding) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).quantize(Decimal("1"), rounding=rounding) * step


class BybitTradeClient:
    def __init__(self, session: aiohttp.ClientSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self._timeout = aiohttp.ClientTimeout(total=20)
        self._instrument_cache: dict[str, InstrumentSpec] = {}

    def enabled(self) -> bool:
        return bool(
            self.settings.execution_submit_orders
            and self.settings.bybit_api_key
            and self.settings.bybit_api_secret
        )

    def _auth_headers(self, *, timestamp_ms: int, payload: str) -> dict[str, str]:
        api_key = self.settings.bybit_api_key
        api_secret = self.settings.bybit_api_secret
        if not api_key or not api_secret:
            raise RuntimeError("Bybit API credentials are missing")
        recv_window = str(self.settings.bybit_recv_window)
        sign_source = f"{timestamp_ms}{api_key}{recv_window}{payload}"
        signature = hmac.new(
            api_secret.encode("utf-8"),
            sign_source.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": str(timestamp_ms),
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict | None = None,
    ) -> dict:
        if not self.enabled():
            raise RuntimeError("Real Bybit trading is not enabled")
        timestamp_ms = int(time.time() * 1000)
        query_string = urlencode(params or {})
        body_text = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
        payload = query_string if method.upper() == "GET" else body_text
        headers = self._auth_headers(timestamp_ms=timestamp_ms, payload=payload)
        url = f"{self.settings.bybit_trade_base_url.rstrip('/')}{path}"
        request_kwargs: dict = {"headers": headers, "timeout": self._timeout}
        if params:
            request_kwargs["params"] = params
        if method.upper() != "GET":
            request_kwargs["data"] = body_text
        async with self.session.request(method.upper(), url, **request_kwargs) as response:
            response.raise_for_status()
            payload_json = await response.json()
        if payload_json.get("retCode") != 0:
            raise RuntimeError(f"Bybit trade error: {payload_json}")
        return payload_json["result"]

    async def fetch_instrument_spec(self, symbol: str) -> InstrumentSpec:
        cached = self._instrument_cache.get(symbol)
        if cached is not None:
            return cached
        async with self.session.get(
            f"{self.settings.bybit_rest_base_url.rstrip('/')}/v5/market/instruments-info",
            params={"category": self.settings.bybit_category, "symbol": symbol},
            timeout=self._timeout,
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit market error: {payload}")
        items = payload.get("result", {}).get("list", [])
        if not items:
            raise RuntimeError(f"No Bybit instrument metadata found for {symbol}")
        item = items[0]
        lot_size = item["lotSizeFilter"]
        price_filter = item["priceFilter"]
        spec = InstrumentSpec(
            symbol=symbol,
            qty_step=Decimal(lot_size["qtyStep"]),
            min_order_qty=Decimal(lot_size["minOrderQty"]),
            tick_size=Decimal(price_filter["tickSize"]),
        )
        self._instrument_cache[symbol] = spec
        return spec

    async def get_position(self, symbol: str) -> VenuePosition | None:
        result = await self._request(
            "GET",
            "/v5/position/list",
            params={"category": self.settings.bybit_category, "symbol": symbol},
        )
        for item in result.get("list", []):
            size = Decimal(item.get("size", "0"))
            side = item.get("side", "")
            if size <= 0 or side in {"", "None"}:
                continue
            return VenuePosition(
                symbol=item["symbol"],
                side=side,
                size=size,
                avg_price=Decimal(item.get("avgPrice", "0")),
                position_idx=int(item.get("positionIdx", 0)),
            )
        return None

    async def get_wallet_balance(self) -> WalletBalance:
        result = await self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED", "coin": "USDT"},
        )
        items = result.get("list", [])
        if not items:
            raise RuntimeError("Bybit wallet balance response was empty")
        account = items[0]
        return WalletBalance(
            total_equity_usd=Decimal(account.get("totalEquity", "0")),
            total_available_balance_usd=Decimal(account.get("totalAvailableBalance", "0")),
        )

    async def get_latest_closed_pnl(self, symbol: str) -> ClosedPnlRecord | None:
        result = await self._request(
            "GET",
            "/v5/position/closed-pnl",
            params={
                "category": self.settings.bybit_category,
                "symbol": symbol,
                "limit": 1,
            },
        )
        items = result.get("list", [])
        if not items:
            return None
        item = items[0]
        return ClosedPnlRecord(
            symbol=item["symbol"],
            order_id=item["orderId"],
            avg_entry_price=Decimal(item.get("avgEntryPrice", "0")),
            avg_exit_price=Decimal(item.get("avgExitPrice", "0")),
            closed_pnl=Decimal(item.get("closedPnl", "0")),
            updated_time_ms=int(item.get("updatedTime", "0")),
        )

    async def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        order_link_id: str,
        reduce_only: bool = False,
    ) -> OrderAck:
        result = await self._request(
            "POST",
            "/v5/order/create",
            body={
                "category": self.settings.bybit_category,
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": format(quantity.normalize(), "f"),
                "positionIdx": 0,
                "orderLinkId": order_link_id,
                "reduceOnly": reduce_only,
            },
        )
        return OrderAck(
            order_id=result["orderId"],
            order_link_id=result["orderLinkId"],
        )

    async def set_trading_stop(
        self,
        *,
        symbol: str,
        position_idx: int,
        take_profit: Decimal,
        stop_loss: Decimal,
    ) -> None:
        await self._request(
            "POST",
            "/v5/position/trading-stop",
            body={
                "category": self.settings.bybit_category,
                "symbol": symbol,
                "positionIdx": position_idx,
                "tpslMode": "Full",
                "takeProfit": format(take_profit.normalize(), "f"),
                "stopLoss": format(stop_loss.normalize(), "f"),
                "tpOrderType": "Market",
                "slOrderType": "Market",
            },
        )

    async def wait_for_position(
        self,
        symbol: str,
    ) -> VenuePosition:
        for _ in range(self.settings.trade_fill_poll_attempts):
            position = await self.get_position(symbol)
            if position is not None and position.size > 0:
                return position
            await asyncio.sleep(self.settings.trade_fill_poll_delay_seconds)
        raise RuntimeError(f"Timed out waiting for filled Bybit position on {symbol}")


class BybitMarketDataClient:
    def __init__(self, session: aiohttp.ClientSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self._rest_timeout = aiohttp.ClientTimeout(total=20)
        self._cache_conn: sqlite3.Connection | None = None
        self._cache_hits = 0
        self._cache_misses = 0
        self._cached_candles_stored = 0
        self._bybit_http_requests = 0
        self._binance_http_requests = 0

    async def _get_json(self, path: str, params: dict[str, str | int]) -> dict:
        url = f"{self.settings.bybit_rest_base_url.rstrip('/')}{path}"
        for attempt in range(self.settings.rate_limit_retries + 1):
            self._bybit_http_requests += 1
            async with self.session.get(url, params=params, timeout=self._rest_timeout) as response:
                response.raise_for_status()
                payload = await response.json()
            if payload.get("retCode") == 0:
                return payload
            if is_rate_limited_payload(payload) and attempt < self.settings.rate_limit_retries:
                delay = self.settings.rate_limit_backoff_seconds * (2 ** attempt)
                LOGGER.warning(
                    "Bybit rate limit hit for %s with params=%s. Retrying in %.1fs (%s/%s)",
                    path,
                    params,
                    delay,
                    attempt + 1,
                    self.settings.rate_limit_retries,
                )
                await asyncio.sleep(delay)
                continue
            raise RuntimeError(f"Bybit error: {payload}")
        raise RuntimeError(f"Bybit error: exhausted retries for {path} {params}")

    async def _get_binance_json(self, path: str, params: dict[str, str | int]) -> list | dict:
        url = f"{self.settings.binance_futures_base_url.rstrip('/')}{path}"
        retry_statuses = {418, 429}
        for attempt in range(self.settings.rate_limit_retries + 1):
            self._binance_http_requests += 1
            async with self.session.get(url, params=params, timeout=self._rest_timeout) as response:
                if response.status in retry_statuses and attempt < self.settings.rate_limit_retries:
                    delay = self.settings.rate_limit_backoff_seconds * (2 ** attempt)
                    LOGGER.warning(
                        "Binance rate limit hit for %s with params=%s. Retrying in %.1fs (%s/%s)",
                        path,
                        params,
                        delay,
                        attempt + 1,
                        self.settings.rate_limit_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                payload = await response.json()
            if isinstance(payload, dict) and payload.get("code") not in (None, 0):
                raise RuntimeError(f"Binance error: {payload}")
            return payload
        raise RuntimeError(f"Binance error: exhausted retries for {path} {params}")

    def _get_cache_conn(self) -> sqlite3.Connection | None:
        if not self.settings.backtest_cache_enabled:
            return None
        if self._cache_conn is None:
            cache_path = Path(self.settings.backtest_cache_path).expanduser()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(cache_path)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS historical_candles (
                    source TEXT NOT NULL,
                    category TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    start_time_ms INTEGER NOT NULL,
                    open_price REAL NOT NULL,
                    high_price REAL NOT NULL,
                    low_price REAL NOT NULL,
                    close_price REAL NOT NULL,
                    PRIMARY KEY (source, category, symbol, interval, start_time_ms)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_historical_candles_lookup
                ON historical_candles (source, category, symbol, interval, start_time_ms)
                """
            )
            self._cache_conn = connection
        return self._cache_conn

    def close_cache(self) -> None:
        if self._cache_conn is not None:
            self._cache_conn.close()
            self._cache_conn = None

    def cached_candle_count(self) -> int:
        connection = self._get_cache_conn()
        if connection is None:
            return 0
        row = connection.execute("SELECT COUNT(*) FROM historical_candles").fetchone()
        return int(row[0]) if row is not None else 0

    def cache_stats_snapshot(self) -> CacheStats:
        return CacheStats(
            cache_rows=self.cached_candle_count(),
            cache_hits=self._cache_hits,
            cache_misses=self._cache_misses,
            cached_candles_stored=self._cached_candles_stored,
            bybit_http_requests=self._bybit_http_requests,
            binance_http_requests=self._binance_http_requests,
        )

    def _cache_expected_starts(
        self,
        *,
        start_ms: int,
        end_ms: int,
        interval_ms: int,
    ) -> list[int]:
        now_ms = int(time.time() * 1000)
        starts: list[int] = []
        cursor = start_ms
        while cursor < end_ms:
            if cursor + interval_ms <= now_ms:
                starts.append(cursor)
            cursor += interval_ms
        return starts

    def _load_cached_ohlc_range(
        self,
        *,
        source: str,
        category: str,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        interval_ms: int,
    ) -> list[HistoricalCandle] | None:
        connection = self._get_cache_conn()
        if connection is None:
            return None
        expected_starts = self._cache_expected_starts(
            start_ms=start_ms,
            end_ms=end_ms,
            interval_ms=interval_ms,
        )
        if not expected_starts:
            return []
        rows = connection.execute(
            """
            SELECT start_time_ms, open_price, high_price, low_price, close_price
            FROM historical_candles
            WHERE source = ? AND category = ? AND symbol = ? AND interval = ?
              AND start_time_ms >= ? AND start_time_ms < ?
            ORDER BY start_time_ms ASC
            """,
            (source, category, symbol, interval, start_ms, end_ms),
        ).fetchall()
        if len(rows) != len(expected_starts):
            self._cache_misses += 1
            return None
        starts = [int(row[0]) for row in rows]
        if starts != expected_starts:
            self._cache_misses += 1
            return None
        self._cache_hits += 1
        return [
            HistoricalCandle(
                start_time_ms=int(row[0]),
                open_price=float(row[1]),
                high_price=float(row[2]),
                low_price=float(row[3]),
                close_price=float(row[4]),
            )
            for row in rows
        ]

    def _store_cached_ohlc_range(
        self,
        *,
        source: str,
        category: str,
        symbol: str,
        interval: str,
        candles: list[HistoricalCandle],
    ) -> None:
        if not candles:
            return
        connection = self._get_cache_conn()
        if connection is None:
            return
        connection.executemany(
            """
            INSERT OR REPLACE INTO historical_candles (
                source, category, symbol, interval, start_time_ms,
                open_price, high_price, low_price, close_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    source,
                    category,
                    symbol,
                    interval,
                    candle.start_time_ms,
                    candle.open_price,
                    candle.high_price,
                    candle.low_price,
                    candle.close_price,
                )
                for candle in candles
            ],
        )
        connection.commit()
        self._cached_candles_stored += len(candles)

    async def fetch_closed_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        category: str | None = None,
    ) -> list[tuple[int, float]]:
        category = category or self.settings.bybit_category
        now_ms = int(time.time() * 1000)
        interval_ms = interval_to_milliseconds(interval)
        attempts = [limit + 1, limit + 4, min(1000, limit + 16)]

        if self.settings.backtest_cache_enabled:
            aligned_end_ms = (now_ms // interval_ms) * interval_ms
            for request_limit in attempts:
                start_ms = aligned_end_ms - (request_limit * interval_ms)
                candles = await self.fetch_closed_ohlc_range(
                    symbol=symbol,
                    interval=interval,
                    start_ms=start_ms,
                    end_ms=aligned_end_ms,
                    category=category,
                )
                if len(candles) >= limit:
                    tail = candles[-limit:]
                    return [(candle.start_time_ms, candle.close_price) for candle in tail]

        for request_limit in attempts:
            payload = await self._get_json(
                "/v5/market/kline",
                {
                    "category": category,
                    "symbol": symbol,
                    "interval": interval,
                    "limit": request_limit,
                },
            )
            rows = payload["result"]["list"]
            candles = []
            for row in rows:
                start_time = int(row[0])
                close_price = float(row[4])
                if start_time + interval_ms <= now_ms:
                    candles.append((start_time, close_price))
            candles.sort(key=lambda item: item[0])
            if len(candles) >= limit:
                candles = candles[-limit:]
                self._ensure_contiguous(candles, interval_ms, symbol, interval)
                return candles
        raise MissingCandlesError(
            f"Unable to fetch {limit} closed {interval} candles for {symbol}"
        )

    async def fetch_closed_ohlc_range(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        *,
        category: str | None = None,
    ) -> list[HistoricalCandle]:
        if end_ms <= start_ms:
            return []
        category = category or self.settings.bybit_category
        interval_ms = interval_to_milliseconds(interval)
        cached = self._load_cached_ohlc_range(
            source="bybit",
            category=category,
            symbol=symbol,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
            interval_ms=interval_ms,
        )
        if cached is not None:
            return cached
        max_candles_per_request = 1000
        max_window_ms = interval_ms * max_candles_per_request
        now_ms = int(time.time() * 1000)
        candles: list[HistoricalCandle] = []
        cursor_start = start_ms
        while cursor_start < end_ms:
            window_end = min(end_ms, cursor_start + max_window_ms)
            payload = await self._get_json(
                "/v5/market/kline",
                {
                    "category": category,
                    "symbol": symbol,
                    "interval": interval,
                    "start": cursor_start,
                    "end": window_end - 1,
                    "limit": max_candles_per_request,
                },
            )
            rows = payload["result"]["list"]
            window_candles: list[HistoricalCandle] = []
            for row in rows:
                candle_start_ms = int(row[0])
                if candle_start_ms < start_ms or candle_start_ms >= end_ms:
                    continue
                if candle_start_ms + interval_ms > now_ms:
                    continue
                window_candles.append(
                    HistoricalCandle(
                        start_time_ms=candle_start_ms,
                        open_price=float(row[1]),
                        high_price=float(row[2]),
                        low_price=float(row[3]),
                        close_price=float(row[4]),
                    )
                )
            window_candles.sort(key=lambda item: item.start_time_ms)
            candles.extend(window_candles)
            cursor_start = window_end
        deduped: dict[int, HistoricalCandle] = {candle.start_time_ms: candle for candle in candles}
        ordered = [deduped[key] for key in sorted(deduped)]
        if ordered:
            self._ensure_contiguous(
                [(candle.start_time_ms, candle.close_price) for candle in ordered],
                interval_ms,
                symbol,
                interval,
            )
            self._store_cached_ohlc_range(
                source="bybit",
                category=category,
                symbol=symbol,
                interval=interval,
                candles=ordered,
            )
        return ordered

    async def fetch_closed_klines_range(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        *,
        category: str | None = None,
    ) -> list[tuple[int, float]]:
        candles = await self.fetch_closed_ohlc_range(
            symbol=symbol,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
            category=category,
        )
        return [(candle.start_time_ms, candle.close_price) for candle in candles]

    def _ensure_contiguous(
        self,
        candles: list[tuple[int, float]],
        interval_ms: int,
        symbol: str,
        interval: str,
    ) -> None:
        for current, nxt in zip(candles, candles[1:]):
            if nxt[0] - current[0] != interval_ms:
                raise MissingCandlesError(
                    f"Missing {interval} candles for {symbol}: {current[0]} -> {nxt[0]}"
                )

    async def fetch_btcdom_klines(self) -> list[tuple[int, float]]:
        symbol = self.settings.btcdom_symbol.strip().upper()
        if symbol.endswith(".P"):
            symbol = symbol[:-2]
        interval_ms = interval_to_milliseconds(self.settings.btcdom_interval)
        if self.settings.backtest_cache_enabled:
            now_ms = int(time.time() * 1000)
            aligned_end_ms = (now_ms // interval_ms) * interval_ms
            start_ms = aligned_end_ms - (
                (self.settings.btcdom_history_lookback + 4) * interval_ms
            )
            candles = await self.fetch_btcdom_klines_range(start_ms=start_ms, end_ms=aligned_end_ms)
            if len(candles) >= self.settings.btcdom_history_lookback:
                return candles[-self.settings.btcdom_history_lookback :]
        payload = await self._get_binance_json(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": self.settings.btcdom_interval,
                "limit": self.settings.btcdom_history_lookback + 4,
            },
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"Binance error: unexpected payload for {symbol}: {payload}")
        now_ms = int(time.time() * 1000)
        candles: list[tuple[int, float]] = []
        for row in payload:
            start_time = int(row[0])
            close_price = float(row[4])
            if start_time + interval_ms <= now_ms:
                candles.append((start_time, close_price))
        candles.sort(key=lambda item: item[0])
        if len(candles) < self.settings.btcdom_history_lookback:
            raise MissingCandlesError(
                f"Unable to fetch {self.settings.btcdom_history_lookback} closed "
                f"{self.settings.btcdom_interval} candles for {symbol}"
            )
        candles = candles[-self.settings.btcdom_history_lookback :]
        self._ensure_contiguous(candles, interval_ms, symbol, self.settings.btcdom_interval)
        return candles

    async def fetch_btcdom_klines_range(
        self,
        *,
        start_ms: int,
        end_ms: int,
    ) -> list[tuple[int, float]]:
        symbol = self.settings.btcdom_symbol.strip().upper()
        if symbol.endswith(".P"):
            symbol = symbol[:-2]
        interval_ms = interval_to_milliseconds(self.settings.btcdom_interval)
        if end_ms <= start_ms:
            return []
        cached = self._load_cached_ohlc_range(
            source="binance_futures",
            category="",
            symbol=symbol,
            interval=self.settings.btcdom_interval,
            start_ms=start_ms,
            end_ms=end_ms,
            interval_ms=interval_ms,
        )
        if cached is not None:
            return [(candle.start_time_ms, candle.close_price) for candle in cached]
        max_candles_per_request = 1500
        max_window_ms = interval_ms * max_candles_per_request
        now_ms = int(time.time() * 1000)
        candles: list[HistoricalCandle] = []
        cursor_start = start_ms
        while cursor_start < end_ms:
            window_end = min(end_ms, cursor_start + max_window_ms)
            payload = await self._get_binance_json(
                "/fapi/v1/klines",
                {
                    "symbol": symbol,
                    "interval": self.settings.btcdom_interval,
                    "startTime": cursor_start,
                    "endTime": window_end - 1,
                    "limit": max_candles_per_request,
                },
            )
            if not isinstance(payload, list):
                raise RuntimeError(f"Binance error: unexpected payload for {symbol}: {payload}")
            for row in payload:
                candle_start_ms = int(row[0])
                if candle_start_ms < start_ms or candle_start_ms >= end_ms:
                    continue
                if candle_start_ms + interval_ms > now_ms:
                    continue
                candles.append(
                    HistoricalCandle(
                        start_time_ms=candle_start_ms,
                        open_price=float(row[1]),
                        high_price=float(row[2]),
                        low_price=float(row[3]),
                        close_price=float(row[4]),
                    )
                )
            cursor_start = window_end
        deduped = {candle.start_time_ms: candle for candle in candles}
        ordered = [deduped[key] for key in sorted(deduped)]
        if ordered:
            self._ensure_contiguous(
                [(candle.start_time_ms, candle.close_price) for candle in ordered],
                interval_ms,
                symbol,
                self.settings.btcdom_interval,
            )
            self._store_cached_ohlc_range(
                source="binance_futures",
                category="",
                symbol=symbol,
                interval=self.settings.btcdom_interval,
                candles=ordered,
            )
        return [(candle.start_time_ms, candle.close_price) for candle in ordered]

    async def bootstrap(self) -> BootstrapPayload:
        semaphore = asyncio.Semaphore(self.settings.bootstrap_concurrency)
        price_history: dict[str, list[tuple[int, float]]] = {}

        async def load_symbol(symbol: str) -> None:
            async with semaphore:
                candles = await self.fetch_closed_klines(
                    symbol=symbol,
                    interval=self.settings.candle_interval,
                    limit=self.settings.state_window,
                )
                price_history[symbol] = candles

        await asyncio.gather(*(load_symbol(symbol) for symbol in self.settings.tracked_symbols))
        btc_daily_history = await self.fetch_closed_klines(
            symbol="BTCUSDT",
            interval="D",
            limit=self.settings.btc_daily_lookback,
        )
        btcdom_history = await self.fetch_btcdom_klines()
        return BootstrapPayload(
            price_history=price_history,
            btc_daily_history=btc_daily_history,
            btcdom_history=btcdom_history,
        )

    async def stream_candles(
        self,
        symbols: list[str],
        on_provisional_candle,
        on_closed_candle,
        on_emerging_event,
        on_confirmed_event,
    ) -> None:
        topics = [f"kline.{self.settings.candle_interval}.{symbol}" for symbol in symbols]
        subscribe_message = {"op": "subscribe", "args": topics}
        ping_message = json.dumps({"op": "ping"})

        async with self.session.ws_connect(
            self.settings.bybit_ws_base_url,
            heartbeat=self.settings.websocket_ping_seconds,
            timeout=30,
        ) as websocket:
            LOGGER.info("Connected to Bybit WebSocket for %s symbols", len(symbols))
            await websocket.send_json(subscribe_message)
            LOGGER.info("Subscribed to kline topics")
            while True:
                try:
                    message = await websocket.receive(timeout=self.settings.websocket_ping_seconds)
                except asyncio.TimeoutError:
                    await websocket.send_str(ping_message)
                    continue

                if message.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(message.data)
                    topic = payload.get("topic", "")
                    if not topic.startswith("kline."):
                        continue
                    for candle in payload.get("data", []):
                        symbol = topic.split(".")[-1]
                        close_time_ms = int(candle["start"])
                        close_price = float(candle["close"])
                        if candle.get("confirm"):
                            appended = on_closed_candle(symbol, close_time_ms, close_price)
                            if appended:
                                on_confirmed_event(close_time_ms)
                        else:
                            appended = on_provisional_candle(symbol, close_time_ms, close_price)
                            if appended:
                                on_emerging_event(close_time_ms)
                elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    raise ConnectionError("WebSocket disconnected")
                elif message.type == aiohttp.WSMsgType.CLOSE:
                    raise ConnectionError("WebSocket closed")
