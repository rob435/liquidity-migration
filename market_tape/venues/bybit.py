"""Bybit v5 public linear perpetuals: stream names, message shapes, REST tables.

Streams (one topic per symbol per feed): `orderbook.<levels>.<SYMBOL>` for
levels 1, 50, 200, 500, 1000; `publicTrade.<SYMBOL>`; `tickers.<SYMBOL>`;
`allLiquidation.<SYMBOL>`; `kline.<interval>.<SYMBOL>`. The ticker already
carries open interest, so an `open_interest` poll is refused here. One
connection's subscription list is capped by the venue at 21,000 characters and
sets no topic count; 150 topics of ~22 characters stays well inside.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from market_tape.config import ConfigError, Feed
from market_tape.schema import book_row, kline_row, liquidation_row, ticker_row, trade_row
from market_tape.venues import Emit

PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
PUBLIC_REST = "https://api.bybit.com"
BOOK_LEVELS = (1, 50, 200, 500, 1000)
KLINE_INTERVALS = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W", "1M": "M",
}
TICKER_FIELDS = {
    "lastPrice": "last_price",
    "markPrice": "mark_price",
    "indexPrice": "index_price",
    "openInterest": "open_interest",
    "openInterestValue": "open_interest_value",
    "fundingRate": "funding_rate",
    "nextFundingTime": "next_funding_time_ms",
    "bid1Price": "bid_price",
    "bid1Size": "bid_size",
    "ask1Price": "ask_price",
    "ask1Size": "ask_size",
    "turnover24h": "turnover_24h",
    "volume24h": "volume_24h",
    "price24hPcnt": "price_change_24h_pct",
}


def fetch_public_json(url: str, timeout: float = 20.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "market-tape"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
        raise RuntimeError(f"venue refused {url}: {str(payload)[:200]}")
    return payload


def fetch_instruments(rest_base: str, category: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(20):
        params = {"category": category, "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        payload = fetch_public_json(f"{rest_base}/v5/market/instruments-info?{urllib.parse.urlencode(params)}")
        result = payload.get("result") or {}
        page = result.get("list") or []
        rows.extend(row for row in page if isinstance(row, dict))
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
    return rows


def fetch_tickers(rest_base: str, category: str) -> list[dict[str, Any]]:
    payload = fetch_public_json(f"{rest_base}/v5/market/tickers?category={category}")
    result = payload.get("result") or {}
    return [row for row in (result.get("list") or []) if isinstance(row, dict)]


@dataclass(slots=True)
class SequenceState:
    update_id: int = 0
    cross_sequence: int = 0
    healthy: bool = False


class BybitAdapter:
    name = "bybit"

    def __init__(self, *, market: str = "linear", ws_url: str | None = None, rest_url: str | None = None) -> None:
        if market != "linear":
            raise ConfigError(f"the Bybit recorder records the linear market, not {market!r}")
        self.market = market
        self.ws_url = ws_url or PUBLIC_LINEAR_WS
        self.rest_url = rest_url or PUBLIC_REST
        self.sequences: dict[str, SequenceState] = {}

    # ---------------------------------------------------------------- feeds

    def validate_feeds(self, feeds: Iterable[Feed]) -> None:
        for feed in feeds:
            if feed.name == "book" and feed.levels not in BOOK_LEVELS:
                raise ConfigError(f"Bybit offers book levels {BOOK_LEVELS}, not {feed.text}")
            if feed.name == "kline" and feed.arg not in KLINE_INTERVALS:
                raise ConfigError(f"Bybit kline intervals are {sorted(KLINE_INTERVALS)}, not {feed.text}")
            if feed.name == "open_interest":
                raise ConfigError("Bybit pushes open interest on the ticker; drop the open_interest feed")

    def topics(self, symbol: str, feeds: Iterable[Feed]) -> list[str]:
        result = []
        for feed in feeds:
            if feed.name == "book":
                result.append(f"orderbook.{feed.levels}.{symbol}")
            elif feed.name == "trades":
                result.append(f"publicTrade.{symbol}")
            elif feed.name == "ticker":
                result.append(f"tickers.{symbol}")
            elif feed.name == "liquidations":
                result.append(f"allLiquidation.{symbol}")
            elif feed.name == "kline":
                result.append(f"kline.{KLINE_INTERVALS[str(feed.arg)]}.{symbol}")
        return result

    def connection_url(self, topics: list[str]) -> str:
        return self.ws_url

    def connection_group(self, topic: str) -> str:
        return ""

    def book_topics(self, topics: Iterable[str]) -> list[str]:
        return [topic for topic in topics if topic.startswith("orderbook.")]

    def subscribe_messages(self, topics: list[str]) -> list[str]:
        return [json.dumps({"op": "subscribe", "args": topics[start : start + 10]}) for start in range(0, len(topics), 10)]

    def add_messages(self, topics: list[str]) -> list[str]:
        return self.subscribe_messages(topics)

    def remove_messages(self, topics: list[str]) -> list[str]:
        return [json.dumps({"op": "unsubscribe", "args": topics[start : start + 10]}) for start in range(0, len(topics), 10)]

    def on_subscribed(self, topics: list[str], emit: Emit, stop: threading.Event) -> None:
        return None

    def start_lanes(self, feeds_by_symbol: Mapping[str, tuple[Feed, ...]], emit: Emit, stop: threading.Event) -> list[threading.Thread]:
        return []

    # --------------------------------------------------------------- tables

    def fetch_tables(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "instruments": fetch_instruments(self.rest_url, self.market),
            "tickers": fetch_tickers(self.rest_url, self.market),
        }

    def listed_symbols(self, instruments: Iterable[Mapping[str, Any]], *, quote: str | None) -> list[str]:
        symbols = set()
        for row in instruments:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("status")) != "Trading":
                continue
            if str(row.get("contractType")) != "LinearPerpetual":
                continue
            if quote is not None and (
                str(row.get("quoteCoin")) != quote or str(row.get("settleCoin", quote)) != quote
            ):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if symbol and symbol.isalnum():
                symbols.add(symbol)
        return sorted(symbols)

    def turnovers(self, tickers: Iterable[Mapping[str, Any]]) -> dict[str, float]:
        result: dict[str, float] = {}
        for row in tickers:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").upper()
            try:
                turnover = float(row.get("turnover24h") or 0.0)
            except (TypeError, ValueError):
                continue
            if symbol:
                result[symbol] = turnover
        return result

    def turnover_ranked(self, tickers: Iterable[Mapping[str, Any]]) -> list[str]:
        turnovers = self.turnovers(tickers)
        return sorted(turnovers, key=lambda symbol: (-turnovers[symbol], symbol))

    def funding_rates(self, tickers: Iterable[Mapping[str, Any]]) -> dict[str, float]:
        rates: dict[str, float] = {}
        for row in tickers:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").upper()
            raw = row.get("fundingRate")
            if not symbol or raw in (None, ""):
                continue
            try:
                rates[symbol] = float(raw)
            except (TypeError, ValueError):
                continue
        return rates

    # ------------------------------------------------------------- messages

    def normalize(self, raw: str | bytes, received_ns: int) -> list[dict[str, Any]]:
        message = json.loads(raw)
        if not isinstance(message, Mapping):
            return []
        topic = str(message.get("topic") or "")
        if topic.startswith("orderbook."):
            return self._book(message, topic, received_ns)
        if topic.startswith("publicTrade."):
            return self._trades(message, received_ns)
        if topic.startswith("tickers."):
            return self._ticker(message, received_ns)
        if topic.startswith("allLiquidation."):
            return self._liquidations(message, received_ns)
        if topic.startswith("kline."):
            return self._klines(message, topic, received_ns)
        return []

    def _book(self, message: Mapping[str, Any], topic: str, received_ns: int) -> list[dict[str, Any]]:
        data = message.get("data")
        if not isinstance(data, Mapping):
            return []
        symbol = str(data.get("s") or "").upper()
        if not symbol:
            return []
        try:
            depth = int(topic.split(".", 2)[1])
        except (IndexError, ValueError):
            return []
        update_id = int(data.get("u") or 0)
        cross_sequence = int(data.get("seq") or 0)
        message_type = str(message.get("type") or "").lower()
        snapshot = message_type == "snapshot" or update_id == 1
        previous = self.sequences.setdefault(topic, SequenceState())
        gap = not snapshot and (
            not previous.healthy
            or (cross_sequence > 0 and previous.cross_sequence > 0 and cross_sequence <= previous.cross_sequence)
            or (update_id > 0 and previous.update_id > 0 and update_id <= previous.update_id)
        )
        row = book_row(
            venue=self.name,
            symbol=symbol,
            snapshot=snapshot,
            depth=depth,
            local_receive_ts_ns=received_ns,
            exchange_system_ts_ns=int(message.get("ts") or 0) * 1_000_000,
            exchange_engine_ts_ns=int(message.get("cts") or 0) * 1_000_000,
            bids=data.get("b") or [],
            asks=data.get("a") or [],
            update_id=update_id,
            previous_update_id=previous.update_id,
            cross_sequence=cross_sequence,
            previous_cross_sequence=previous.cross_sequence,
            restart_snapshot=update_id == 1,
            sequence_gap=gap,
        )
        previous.update_id = update_id
        previous.cross_sequence = cross_sequence
        previous.healthy = snapshot or not gap
        return [row]

    def _trades(self, message: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        rows = message.get("data")
        if not isinstance(rows, list):
            return []
        output = []
        for trade in rows:
            if not isinstance(trade, Mapping):
                continue
            symbol = str(trade.get("s") or "").upper()
            side = str(trade.get("S") or "")
            if not symbol or side not in {"Buy", "Sell"}:
                continue
            output.append(
                trade_row(
                    venue=self.name,
                    symbol=symbol,
                    local_receive_ts_ns=received_ns,
                    exchange_ts_ns=int(trade.get("T") or message.get("ts") or 0) * 1_000_000,
                    trade_id=str(trade.get("i") or ""),
                    price=float(trade.get("p") or 0.0),
                    qty=float(trade.get("v") or 0.0),
                    side=side,
                )
            )
        return output

    def _ticker(self, message: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        data = message.get("data")
        if not isinstance(data, Mapping):
            return []
        symbol = str(data.get("symbol") or "").upper()
        if not symbol:
            return []
        values: dict[str, float | int] = {}
        for venue_name, stored_name in TICKER_FIELDS.items():
            raw = data.get(venue_name)
            if raw in (None, ""):
                continue
            try:
                values[stored_name] = int(raw) if venue_name == "nextFundingTime" else float(raw)
            except (TypeError, ValueError):
                continue
        return [
            ticker_row(
                venue=self.name,
                symbol=symbol,
                local_receive_ts_ns=received_ns,
                exchange_system_ts_ns=int(message.get("ts") or 0) * 1_000_000,
                message_type=str(message.get("type") or "").lower(),
                cross_sequence=int(message.get("cs") or 0),
                values=values,
            )
        ]

    def _liquidations(self, message: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        data = message.get("data")
        rows = data if isinstance(data, list) else [data]
        output = []
        for liquidation in rows:
            if not isinstance(liquidation, Mapping):
                continue
            symbol = str(liquidation.get("s") or "").upper()
            position_side = str(liquidation.get("S") or "")
            if not symbol or position_side not in {"Buy", "Sell"}:
                continue
            output.append(
                liquidation_row(
                    venue=self.name,
                    symbol=symbol,
                    local_receive_ts_ns=received_ns,
                    exchange_system_ts_ns=int(message.get("ts") or 0) * 1_000_000,
                    exchange_ts_ns=int(liquidation.get("T") or 0) * 1_000_000,
                    position_side=position_side,
                    qty=float(liquidation.get("v") or 0.0),
                    bankruptcy_price=float(liquidation.get("p") or 0.0),
                )
            )
        return output

    def _klines(self, message: Mapping[str, Any], topic: str, received_ns: int) -> list[dict[str, Any]]:
        parts = topic.split(".", 2)
        if len(parts) != 3:
            return []
        venue_interval, symbol = parts[1], parts[2].upper()
        interval = next((name for name, code in KLINE_INTERVALS.items() if code == venue_interval), venue_interval)
        rows = message.get("data")
        if not isinstance(rows, list):
            return []
        output = []
        for candle in rows:
            if not isinstance(candle, Mapping):
                continue
            try:
                output.append(
                    kline_row(
                        venue=self.name,
                        symbol=symbol,
                        interval=interval,
                        local_receive_ts_ns=received_ns,
                        exchange_system_ts_ns=int(message.get("ts") or 0) * 1_000_000,
                        start_ms=int(candle.get("start") or 0),
                        end_ms=int(candle.get("end") or 0),
                        open=float(candle.get("open") or 0.0),
                        high=float(candle.get("high") or 0.0),
                        low=float(candle.get("low") or 0.0),
                        close=float(candle.get("close") or 0.0),
                        volume=float(candle.get("volume") or 0.0),
                        turnover=float(candle.get("turnover") or 0.0),
                        confirmed=bool(candle.get("confirm", False)),
                    )
                )
            except (TypeError, ValueError):
                continue
        return output
