"""Binance USD-M perpetual futures: stream names, message shapes, REST tables.

Every shard opens one combined-stream URL that names its streams, so there is
no subscribe frame to send and each frame arrives as
`{"stream": ..., "data": ...}`. The venue routes streams by URL path: `/public`
carries the high-frequency streams (the depth streams, `bookTicker`, `trade`)
and `/market` everything else (`aggTrade`, `markPrice`, `ticker`, `kline`,
`!forceOrder@arr`); a connection receives only its own path's streams and
silently drops the rest (a path-less URL is `/public`; the legacy path was
retired 2026-04-23). `connection_group` names the path, and the recorder gives
each shard one group. The venue caps a connection at 1024 streams, accepts 10
incoming messages a second, pings every 3 minutes, and closes every connection
at its 24-hour mark; the recorder's shard loop reconnects and `on_subscribed`
re-anchors the deep books.

The book comes two ways. `depth<N>@100ms` pushes a whole small book, so each
frame is a snapshot. `depth@100ms` pushes differences that only mean something
next to a REST snapshot, so `book:1000` records the diff stream plus one
`/fapi/v1/depth` snapshot per symbol on each connect, and the reader joins them
by update id. Binance publishes its own previous id (`pu`) on the depth
streams; the top of book has none, so those rows carry 0.

Open interest is a REST poll: nothing pushes it.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from market_tape.config import ConfigError, Feed
from market_tape.schema import TICKER_INT_FIELDS, book_row, kline_row, liquidation_row, ticker_row, trade_row
from market_tape.venues import Emit

PUBLIC_USDM_WS = "wss://fstream.binance.com"
PUBLIC_REST = "https://fapi.binance.com"

#: `book:1` is the top-of-book stream, 5/10/20 the partial books, and 1000 the
#: diff stream anchored by a 1000-level REST snapshot.
BOOK_LEVELS = (1, 5, 10, 20, 1000)
DIFF_BOOK_LEVELS = 1000
DIFF_BOOK_SUFFIX = "depth@100ms"
LIQUIDATION_STREAM = "!forceOrder@arr"
KLINE_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M")

#: One `/fapi/v1/depth?limit=1000` costs 20 of the 2400 request weight an IP
#: gets each minute; the snapshots keep to 1200 of it, so one a second.
DEPTH_REQUEST_WEIGHT = 20
SNAPSHOT_WEIGHT_BUDGET = 1200
SNAPSHOT_PAUSE_SECONDS = 60.0 * DEPTH_REQUEST_WEIGHT / SNAPSHOT_WEIGHT_BUDGET
OPEN_INTEREST_PAUSE_SECONDS = 0.05

MARK_PRICE_FIELDS = {"p": "mark_price", "i": "index_price", "r": "funding_rate", "T": "next_funding_time_ms"}
DAY_TICKER_FIELDS = {"c": "last_price", "q": "turnover_24h", "v": "volume_24h", "P": "price_change_24h_pct"}
#: Binance states the 24h change in percent; the contract stores a fraction.
PERCENT_FIELDS = frozenset({"price_change_24h_pct"})
#: Live subscribe and unsubscribe requests carry at most this many streams each.
LIVE_REQUEST_STREAMS = 50
PREMIUM_TABLE_FIELDS = ("markPrice", "indexPrice", "lastFundingRate", "nextFundingTime")
DAY_TABLE_FIELDS = ("lastPrice", "quoteVolume", "volume")
BOOK_TABLE_FIELDS = ("bidPrice", "bidQty", "askPrice", "askQty")


def fetch_public_json(url: str, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "market-tape"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if isinstance(payload, Mapping) and "code" in payload and "msg" in payload:
        raise RuntimeError(f"venue refused {url}: {str(payload)[:200]}")
    return payload


def _table(payload: Any) -> list[dict[str, Any]]:
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def fetch_instruments(rest_base: str) -> list[dict[str, Any]]:
    payload = fetch_public_json(f"{rest_base}/fapi/v1/exchangeInfo")
    return _table(payload.get("symbols")) if isinstance(payload, Mapping) else []


def fetch_tickers(rest_base: str) -> list[dict[str, Any]]:
    """One row per symbol, carrying the fields three whole-market tables hold."""

    merged: dict[str, dict[str, Any]] = {}
    for path, fields in (
        ("/fapi/v1/premiumIndex", PREMIUM_TABLE_FIELDS),
        ("/fapi/v1/ticker/24hr", DAY_TABLE_FIELDS),
        ("/fapi/v1/ticker/bookTicker", BOOK_TABLE_FIELDS),
    ):
        for row in _table(fetch_public_json(f"{rest_base}{path}")):
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            target = merged.setdefault(symbol, {"symbol": symbol})
            for name in fields:
                if row.get(name) is not None:
                    target[name] = row[name]
    return [merged[symbol] for symbol in sorted(merged)]


def fetch_depth(rest_base: str, symbol: str) -> dict[str, Any]:
    payload = fetch_public_json(f"{rest_base}/fapi/v1/depth?symbol={symbol}&limit={DIFF_BOOK_LEVELS}")
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"depth for {symbol} is not a table: {str(payload)[:200]}")
    return dict(payload)


def fetch_open_interest(rest_base: str, symbol: str) -> dict[str, Any]:
    payload = fetch_public_json(f"{rest_base}/fapi/v1/openInterest?symbol={symbol}")
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"open interest for {symbol} is not a table: {str(payload)[:200]}")
    return dict(payload)


def diff_book_symbols(topics: Iterable[str]) -> list[str]:
    return [topic.partition("@")[0].upper() for topic in topics if topic.partition("@")[2] == DIFF_BOOK_SUFFIX]


def _ns(value: Any) -> int:
    try:
        return int(value or 0) * 1_000_000
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _level(price: Any, size: Any) -> list[list[str]]:
    if price in (None, "") or size in (None, ""):
        return []
    return [[str(price), str(size)]]


def _ticker_values(data: Mapping[str, Any], fields: Mapping[str, str]) -> dict[str, float | int]:
    values: dict[str, float | int] = {}
    for venue_name, stored_name in fields.items():
        number = _float(data.get(venue_name))
        if number is None:
            continue
        values[stored_name] = int(number) if stored_name in TICKER_INT_FIELDS else number
    return values


@dataclass(slots=True)
class DiffBookState:
    update_id: int = 0
    seen: bool = False


class BinanceAdapter:
    name = "binance"

    def __init__(self, *, market: str = "usdm", ws_url: str | None = None, rest_url: str | None = None) -> None:
        if market != "usdm":
            raise ConfigError(f"the Binance recorder records the usdm market, not {market!r}")
        self.market = market
        self.ws_url = (ws_url or PUBLIC_USDM_WS).rstrip("/")
        self.rest_url = (rest_url or PUBLIC_REST).rstrip("/")
        # Written by the writer thread in normalize, cleared by a shard thread
        # in on_connected, so both take the lock.
        self.diff_books: dict[str, DiffBookState] = {}
        self.diff_book_lock = threading.Lock()
        # The request weight budget is per IP, so every shard's snapshots queue
        # through one pacer.
        self.next_snapshot_at = 0.0
        self.snapshot_lock = threading.Lock()
        # Latest mark per symbol, for the open-interest poll to price its count.
        self.marks: dict[str, float] = {}
        self.request_ids = itertools.count(1)

    # ---------------------------------------------------------------- feeds

    def validate_feeds(self, feeds: Iterable[Feed]) -> None:
        for feed in feeds:
            if feed.name == "book" and feed.levels not in BOOK_LEVELS:
                raise ConfigError(f"Binance offers book levels {BOOK_LEVELS}, not {feed.text}")
            if feed.name == "kline" and feed.arg not in KLINE_INTERVALS:
                raise ConfigError(f"Binance kline intervals are {list(KLINE_INTERVALS)}, not {feed.text}")

    def topics(self, symbol: str, feeds: Iterable[Feed]) -> list[str]:
        lower = symbol.lower()
        result = []
        for feed in feeds:
            if feed.name == "book":
                levels = feed.levels
                if levels == 1:
                    result.append(f"{lower}@bookTicker")
                elif levels == DIFF_BOOK_LEVELS:
                    result.append(f"{lower}@{DIFF_BOOK_SUFFIX}")
                else:
                    result.append(f"{lower}@depth{levels}@100ms")
            elif feed.name == "trades":
                result.append(f"{lower}@aggTrade")
            elif feed.name == "ticker":
                result.append(f"{lower}@markPrice@1s")
                result.append(f"{lower}@ticker")
            elif feed.name == "liquidations":
                # One stream carries every symbol's liquidations; the recorder
                # claims a repeated topic once.
                result.append(LIQUIDATION_STREAM)
            elif feed.name == "kline":
                result.append(f"{lower}@kline_{feed.arg}")
        return result

    def connection_group(self, topic: str) -> str:
        suffix = topic.partition("@")[2]
        if suffix.startswith("depth") or suffix in ("bookTicker", "trade"):
            return "public"
        return "market"

    def connection_url(self, topics: list[str]) -> str:
        groups = {self.connection_group(topic) for topic in topics}
        if len(groups) != 1:
            raise ValueError(f"one Binance connection carries one path, got {sorted(groups)}")
        return f"{self.ws_url}/{groups.pop()}/stream?streams={'/'.join(topics)}"

    def subscribe_messages(self, topics: list[str]) -> list[str]:
        return []

    def add_messages(self, topics: list[str]) -> list[str]:
        return self._live_requests("SUBSCRIBE", topics)

    def remove_messages(self, topics: list[str]) -> list[str]:
        return self._live_requests("UNSUBSCRIBE", topics)

    def _live_requests(self, method: str, topics: list[str]) -> list[str]:
        messages = []
        for start in range(0, len(topics), LIVE_REQUEST_STREAMS):
            messages.append(
                json.dumps({"method": method, "params": topics[start : start + LIVE_REQUEST_STREAMS], "id": next(self.request_ids)})
            )
        return messages

    # ------------------------------------------------------- book anchoring

    def on_subscribed(self, topics: list[str], emit: Emit, stop: threading.Event) -> None:
        symbols = diff_book_symbols(topics)
        if not symbols:
            return
        with self.diff_book_lock:
            for symbol in symbols:
                self.diff_books.pop(symbol, None)
        for symbol in symbols:
            if not self._snapshot_slot(stop):
                return
            try:
                emit(self.depth_snapshot_row(symbol))
            except Exception as exc:  # noqa: BLE001 - one symbol's missing anchor must not cost the others theirs
                logging.warning("book snapshot for %s failed: %s", symbol, exc)

    def _snapshot_slot(self, stop: threading.Event) -> bool:
        """Wait for this fetch's turn; False when the stop event ended the wait."""

        with self.snapshot_lock:
            now = time.monotonic()
            turn = max(now, self.next_snapshot_at)
            self.next_snapshot_at = turn + SNAPSHOT_PAUSE_SECONDS
            delay = turn - now
        return not (stop.wait(delay) if delay > 0.0 else stop.is_set())

    def depth_snapshot_row(self, symbol: str) -> dict[str, Any]:
        payload = fetch_depth(self.rest_url, symbol)
        received_ns = time.time_ns()
        return book_row(
            venue=self.name,
            symbol=symbol,
            snapshot=True,
            depth=DIFF_BOOK_LEVELS,
            local_receive_ts_ns=received_ns,
            exchange_system_ts_ns=_ns(payload.get("E")),
            exchange_engine_ts_ns=_ns(payload.get("T")),
            bids=payload.get("bids") or [],
            asks=payload.get("asks") or [],
            update_id=int(payload.get("lastUpdateId") or 0),
            previous_update_id=0,
        )

    # ----------------------------------------------------------------- lanes

    def start_lanes(
        self, feeds_by_symbol: Mapping[str, tuple[Feed, ...]], emit: Emit, stop: threading.Event
    ) -> list[threading.Thread]:
        by_interval: dict[float, list[str]] = {}
        for symbol, feeds in feeds_by_symbol.items():
            for feed in feeds:
                if feed.name == "open_interest":
                    by_interval.setdefault(feed.seconds, []).append(symbol)
        threads = []
        for seconds, symbols in sorted(by_interval.items()):
            thread = threading.Thread(
                target=self._open_interest_lane,
                args=(sorted(symbols), seconds, emit, stop),
                name=f"tape-binance-oi-{seconds:g}s",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        return threads

    def _open_interest_lane(self, symbols: list[str], seconds: float, emit: Emit, stop: threading.Event) -> None:
        while not stop.is_set():
            started = time.monotonic()
            for symbol in symbols:
                if stop.is_set():
                    return
                try:
                    emit(self.open_interest_row(symbol))
                except Exception as exc:  # noqa: BLE001 - one symbol's failed poll must not end the lane
                    logging.warning("open interest poll for %s failed: %s", symbol, exc)
                if stop.wait(OPEN_INTEREST_PAUSE_SECONDS):
                    return
            stop.wait(max(0.0, seconds - (time.monotonic() - started)))

    def open_interest_row(self, symbol: str) -> dict[str, Any]:
        payload = fetch_open_interest(self.rest_url, symbol)
        received_ns = time.time_ns()
        count = _float(payload.get("openInterest")) or 0.0
        values: dict[str, float | int] = {"open_interest": count}
        mark = self.marks.get(symbol)
        if mark is not None:
            values["open_interest_value"] = count * mark
        return ticker_row(
            venue=self.name,
            symbol=symbol,
            local_receive_ts_ns=received_ns,
            exchange_system_ts_ns=_ns(payload.get("time")),
            message_type="poll",
            values=values,
        )

    # --------------------------------------------------------------- tables

    def fetch_tables(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "instruments": fetch_instruments(self.rest_url),
            "tickers": fetch_tickers(self.rest_url),
        }

    def listed_symbols(self, instruments: Iterable[Mapping[str, Any]], *, quote: str | None) -> list[str]:
        symbols = set()
        for row in instruments:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("status")) != "TRADING":
                continue
            if str(row.get("contractType")) != "PERPETUAL":
                continue
            if quote is not None and (
                str(row.get("quoteAsset")) != quote or str(row.get("marginAsset", quote)) != quote
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
            turnover = _float(row.get("quoteVolume"))
            if symbol and turnover is not None:
                result[symbol] = turnover
        return result

    def turnover_ranked(self, tickers: Iterable[Mapping[str, Any]]) -> list[str]:
        ranked = []
        for row in tickers:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").upper()
            turnover = _float(row.get("quoteVolume"))
            if symbol and turnover is not None:
                ranked.append((turnover, symbol))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [symbol for _, symbol in ranked]

    def funding_rates(self, tickers: Iterable[Mapping[str, Any]]) -> dict[str, float]:
        rates: dict[str, float] = {}
        for row in tickers:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").upper()
            rate = _float(row.get("lastFundingRate"))
            if symbol and rate is not None:
                rates[symbol] = rate
        return rates

    # ------------------------------------------------------------- messages

    def normalize(self, raw: str | bytes, received_ns: int) -> list[dict[str, Any]]:
        message = json.loads(raw)
        if not isinstance(message, Mapping):
            return []
        stream = str(message.get("stream") or "")
        data = message.get("data")
        if stream == LIQUIDATION_STREAM:
            return self._liquidations(data, received_ns)
        if not isinstance(data, Mapping):
            return []
        suffix = stream.partition("@")[2]
        if suffix == "bookTicker":
            return self._top_of_book(data, received_ns)
        if suffix.startswith("depth"):
            return self._book(data, suffix, received_ns)
        if suffix == "aggTrade":
            return self._trade(data, received_ns)
        if suffix.startswith("markPrice"):
            return self._ticker(data, MARK_PRICE_FIELDS, received_ns)
        if suffix == "ticker":
            return self._ticker(data, DAY_TICKER_FIELDS, received_ns)
        if suffix.startswith("kline_"):
            return self._kline(data, suffix[len("kline_") :], received_ns)
        return []

    def _top_of_book(self, data: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        symbol = str(data.get("s") or "").upper()
        if not symbol:
            return []
        return [
            book_row(
                venue=self.name,
                symbol=symbol,
                snapshot=True,
                depth=1,
                local_receive_ts_ns=received_ns,
                exchange_system_ts_ns=_ns(data.get("E")),
                exchange_engine_ts_ns=_ns(data.get("T")),
                bids=_level(data.get("b"), data.get("B")),
                asks=_level(data.get("a"), data.get("A")),
                update_id=int(data.get("u") or 0),
                previous_update_id=0,
            )
        ]

    def _book(self, data: Mapping[str, Any], suffix: str, received_ns: int) -> list[dict[str, Any]]:
        symbol = str(data.get("s") or "").upper()
        levels = suffix.partition("@")[0][len("depth") :]
        if not symbol or (levels and not levels.isdigit()):
            return []
        snapshot = bool(levels)
        update_id = int(data.get("u") or 0)
        previous_update_id = int(data.get("pu") or 0)
        return [
            book_row(
                venue=self.name,
                symbol=symbol,
                snapshot=snapshot,
                depth=int(levels) if levels else DIFF_BOOK_LEVELS,
                local_receive_ts_ns=received_ns,
                exchange_system_ts_ns=_ns(data.get("E")),
                exchange_engine_ts_ns=_ns(data.get("T")),
                bids=data.get("b") or [],
                asks=data.get("a") or [],
                update_id=update_id,
                previous_update_id=previous_update_id,
                first_update_id=int(data.get("U") or 0),
                sequence_gap=False if snapshot else self._diff_gap(symbol, update_id, previous_update_id),
            )
        ]

    def _diff_gap(self, symbol: str, update_id: int, previous_update_id: int) -> bool:
        with self.diff_book_lock:
            state = self.diff_books.setdefault(symbol, DiffBookState())
            gap = not state.seen or previous_update_id != state.update_id
            state.update_id = update_id
            state.seen = True
        return gap

    def _trade(self, data: Mapping[str, Any], received_ns: int) -> list[dict[str, Any]]:
        symbol = str(data.get("s") or "").upper()
        price = _float(data.get("p"))
        qty = _float(data.get("q"))
        if not symbol or price is None or qty is None:
            return []
        return [
            trade_row(
                venue=self.name,
                symbol=symbol,
                local_receive_ts_ns=received_ns,
                exchange_ts_ns=_ns(data.get("T") or data.get("E")),
                trade_id=str(data.get("a") or ""),
                price=price,
                qty=qty,
                # `m` is "the buyer was the maker", so the aggressor sold.
                side="Sell" if data.get("m") else "Buy",
            )
        ]

    def _ticker(self, data: Mapping[str, Any], fields: Mapping[str, str], received_ns: int) -> list[dict[str, Any]]:
        symbol = str(data.get("s") or "").upper()
        if not symbol:
            return []
        values = _ticker_values(data, fields)
        for name in PERCENT_FIELDS & values.keys():
            values[name] = float(values[name]) / 100.0
        mark = values.get("mark_price")
        if mark is not None:
            self.marks[symbol] = float(mark)
        return [
            ticker_row(
                venue=self.name,
                symbol=symbol,
                local_receive_ts_ns=received_ns,
                exchange_system_ts_ns=_ns(data.get("E")),
                message_type="delta",
                values=values,
            )
        ]

    def _liquidations(self, data: Any, received_ns: int) -> list[dict[str, Any]]:
        events = data if isinstance(data, list) else [data]
        output = []
        for event in events:
            if not isinstance(event, Mapping):
                continue
            order = event.get("o")
            if not isinstance(order, Mapping):
                continue
            symbol = str(order.get("s") or "").upper()
            # The order closes the position, so the position sat the other way.
            position_side = {"SELL": "Buy", "BUY": "Sell"}.get(str(order.get("S") or "").upper(), "")
            qty = _float(order.get("q"))
            price = _float(order.get("p"))
            if not symbol or not position_side or qty is None or price is None:
                continue
            output.append(
                liquidation_row(
                    venue=self.name,
                    symbol=symbol,
                    local_receive_ts_ns=received_ns,
                    exchange_system_ts_ns=_ns(event.get("E")),
                    exchange_ts_ns=_ns(order.get("T")),
                    position_side=position_side,
                    qty=qty,
                    bankruptcy_price=price,
                )
            )
        return output

    def _kline(self, data: Mapping[str, Any], interval: str, received_ns: int) -> list[dict[str, Any]]:
        symbol = str(data.get("s") or "").upper()
        candle = data.get("k")
        if not symbol or not isinstance(candle, Mapping):
            return []
        try:
            return [
                kline_row(
                    venue=self.name,
                    symbol=symbol,
                    interval=str(candle.get("i") or interval),
                    local_receive_ts_ns=received_ns,
                    exchange_system_ts_ns=_ns(data.get("E")),
                    start_ms=int(candle.get("t") or 0),
                    end_ms=int(candle.get("T") or 0),
                    open=float(candle.get("o") or 0.0),
                    high=float(candle.get("h") or 0.0),
                    low=float(candle.get("l") or 0.0),
                    close=float(candle.get("c") or 0.0),
                    volume=float(candle.get("v") or 0.0),
                    turnover=float(candle.get("q") or 0.0),
                    confirmed=bool(candle.get("x", False)),
                )
            ]
        except (TypeError, ValueError):
            return []
