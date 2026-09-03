"""The Binance USD-M adapter: stream names, message shapes, REST tables."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable

import pytest

from market_tape.config import ConfigError, parse_feed
from market_tape.schema import parse_row
from market_tape.venues import adapter_for
from market_tape.venues import binance as venue
from market_tape.venues.binance import BinanceAdapter

RECEIVED = 1_700_000_000_123_456_789


def frame(stream: str, data: Any) -> str:
    return json.dumps({"stream": stream, "data": data})


def feeds(*texts: str) -> tuple[Any, ...]:
    return tuple(parse_feed(text) for text in texts)


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def fake_rest(
    monkeypatch: pytest.MonkeyPatch, reply: Callable[[str], Any], *, on_call: Callable[[str], None] | None = None
) -> list[str]:
    """Answer every REST call from `reply`; return the list of URLs called."""

    calls: list[str] = []

    def urlopen(request: Any, timeout: float | None = None) -> FakeResponse:
        calls.append(request.full_url)
        if on_call is not None:
            on_call(request.full_url)
        return FakeResponse(reply(request.full_url))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


# ------------------------------------------------------------------- streams


def test_market_and_defaults() -> None:
    adapter = adapter_for("binance", market="usdm")
    assert isinstance(adapter, BinanceAdapter)
    assert (adapter.name, adapter.market) == ("binance", "usdm")
    assert adapter.ws_url == "wss://fstream.binance.com"
    assert adapter.rest_url == "https://fapi.binance.com"
    with pytest.raises(ConfigError):
        BinanceAdapter(market="spot")


def test_only_the_book_topics_are_worth_re_anchoring() -> None:
    adapter = BinanceAdapter()
    topics = [
        "btcusdt@depth@100ms",
        "btcusdt@aggTrade",
        "btcusdt@markPrice@1s",
        "btcusdt@bookTicker",
        "btcusdt@depth20@100ms",
        "btcusdt@forceOrder",
    ]
    assert adapter.book_topics(topics) == [
        "btcusdt@depth@100ms",
        "btcusdt@bookTicker",
        "btcusdt@depth20@100ms",
    ]
    # The recorder's own config takes no book on this venue, so the hourly
    # re-anchor is a no-op there.
    assert adapter.book_topics(["btcusdt@aggTrade", "btcusdt@markPrice@1s"]) == []


def test_topics_name_one_stream_per_feed() -> None:
    adapter = BinanceAdapter()
    assert adapter.topics("BTCUSDT", feeds("book:1")) == ["btcusdt@bookTicker"]
    assert adapter.topics("BTCUSDT", feeds("book:5")) == ["btcusdt@depth5@100ms"]
    assert adapter.topics("BTCUSDT", feeds("book:20")) == ["btcusdt@depth20@100ms"]
    assert adapter.topics("BTCUSDT", feeds("book:1000")) == ["btcusdt@depth@100ms"]
    assert adapter.topics("BTCUSDT", feeds("trades")) == ["btcusdt@aggTrade"]
    assert adapter.topics("BTCUSDT", feeds("kline:15m")) == ["btcusdt@kline_15m"]
    assert adapter.topics("BTCUSDT", feeds("open_interest:300")) == []


def test_ticker_takes_two_streams_and_liquidations_one_for_the_market() -> None:
    adapter = BinanceAdapter()
    assert adapter.topics("ETHUSDT", feeds("ticker")) == ["ethusdt@markPrice@1s", "ethusdt@ticker"]
    assert adapter.topics("ETHUSDT", feeds("liquidations")) == ["!forceOrder@arr"]
    assert adapter.topics("BTCUSDT", feeds("liquidations")) == ["!forceOrder@arr"]


def test_connection_url_combines_the_streams_under_their_path() -> None:
    adapter = BinanceAdapter()
    assert adapter.connection_url(["btcusdt@bookTicker", "btcusdt@depth@100ms"]) == (
        "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/btcusdt@depth@100ms"
    )
    assert adapter.connection_url(["btcusdt@aggTrade", "btcusdt@markPrice@1s", "!forceOrder@arr"]) == (
        "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@markPrice@1s/!forceOrder@arr"
    )
    assert adapter.subscribe_messages(["btcusdt@bookTicker"]) == []


def test_streams_route_by_path_and_one_connection_carries_one_path() -> None:
    # Measured on the host 2026-09-02: a path-less or /public connection delivers
    # only the depth, bookTicker and trade streams; the rest flow on /market only.
    adapter = BinanceAdapter()
    public = ["btcusdt@depth@100ms", "btcusdt@depth20@100ms", "btcusdt@bookTicker", "btcusdt@trade"]
    market = ["btcusdt@aggTrade", "btcusdt@markPrice@1s", "btcusdt@ticker", "btcusdt@kline_1m", "!forceOrder@arr"]
    assert {adapter.connection_group(topic) for topic in public} == {"public"}
    assert {adapter.connection_group(topic) for topic in market} == {"market"}
    with pytest.raises(ValueError, match="one path"):
        adapter.connection_url(["btcusdt@bookTicker", "btcusdt@aggTrade"])


def test_validate_feeds_refuses_what_the_venue_does_not_offer() -> None:
    adapter = BinanceAdapter()
    adapter.validate_feeds(feeds("book:1", "book:5", "book:10", "book:20", "book:1000", "kline:1M", "open_interest:60"))
    with pytest.raises(ConfigError):
        adapter.validate_feeds(feeds("book:50"))
    with pytest.raises(ConfigError):
        adapter.validate_feeds(feeds("book:200"))
    with pytest.raises(ConfigError):
        adapter.validate_feeds(feeds("kline:5s"))


# -------------------------------------------------------------------- books


def test_top_of_book_is_a_one_level_snapshot() -> None:
    adapter = BinanceAdapter()
    raw = frame(
        "btcusdt@bookTicker",
        {
            "e": "bookTicker",
            "u": 400900217,
            "E": 1568014460893,
            "T": 1568014460891,
            "s": "BTCUSDT",
            "b": "25.35190000",
            "B": "31.21000000",
            "a": "25.36520000",
            "A": "40.66000000",
        },
    )
    (row,) = adapter.normalize(raw, RECEIVED)
    assert row == {
        "kind": "orderbook_snapshot",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "depth": 1,
        "local_receive_ts_ns": RECEIVED,
        "exchange_system_ts_ns": 1568014460893_000_000,
        "exchange_engine_ts_ns": 1568014460891_000_000,
        "bids": [["25.35190000", "31.21000000"]],
        "asks": [["25.36520000", "40.66000000"]],
        "update_id": 400900217,
        "previous_update_id": 0,
        "first_update_id": 0,
        "cross_sequence": 0,
        "previous_cross_sequence": 0,
        "restart_snapshot": False,
        "sequence_gap": False,
    }
    typed = parse_row(row, default_venue="binance")
    assert typed.bids == ((25.3519, 31.21),) and typed.asks == ((25.3652, 40.66),)


def partial_book_frame(levels: int) -> str:
    return frame(
        f"btcusdt@depth{levels}@100ms",
        {
            "e": "depthUpdate",
            "E": 1571889248277,
            "T": 1571889248276,
            "s": "BTCUSDT",
            "U": 390497796,
            "u": 390497878,
            "pu": 390497794,
            "b": [["7403.89", "0.002"], ["7403.90", "3.906"]],
            "a": [["7405.96", "3.340"]],
        },
    )


def test_partial_book_is_a_snapshot_at_its_own_depth() -> None:
    adapter = BinanceAdapter()
    (row,) = adapter.normalize(partial_book_frame(20), RECEIVED)
    assert row == {
        "kind": "orderbook_snapshot",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "depth": 20,
        "local_receive_ts_ns": RECEIVED,
        "exchange_system_ts_ns": 1571889248277_000_000,
        "exchange_engine_ts_ns": 1571889248276_000_000,
        "bids": [["7403.89", "0.002"], ["7403.90", "3.906"]],
        "asks": [["7405.96", "3.340"]],
        "update_id": 390497878,
        "previous_update_id": 390497794,
        "first_update_id": 390497796,
        "cross_sequence": 0,
        "previous_cross_sequence": 0,
        "restart_snapshot": False,
        "sequence_gap": False,
    }
    typed = parse_row(row, default_venue="binance")
    assert typed.snapshot and typed.depth == 20
    assert adapter.normalize(partial_book_frame(5), RECEIVED)[0]["depth"] == 5


def diff_frame(first: int, last: int, previous: int) -> str:
    return frame(
        "btcusdt@depth@100ms",
        {
            "e": "depthUpdate",
            "E": 1571889248277,
            "T": 1571889248276,
            "s": "BTCUSDT",
            "U": first,
            "u": last,
            "pu": previous,
            "b": [["7403.89", "0.002"]],
            "a": [],
        },
    )


def test_diff_book_row_carries_the_venue_ids() -> None:
    adapter = BinanceAdapter()
    (row,) = adapter.normalize(diff_frame(100, 105, 95), RECEIVED)
    assert row == {
        "kind": "orderbook_delta",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "depth": 1000,
        "local_receive_ts_ns": RECEIVED,
        "exchange_system_ts_ns": 1571889248277_000_000,
        "exchange_engine_ts_ns": 1571889248276_000_000,
        "bids": [["7403.89", "0.002"]],
        "asks": [],
        "update_id": 105,
        "previous_update_id": 95,
        "first_update_id": 100,
        "cross_sequence": 0,
        "previous_cross_sequence": 0,
        "restart_snapshot": False,
        "sequence_gap": True,
    }
    assert not parse_row(row, default_venue="binance").snapshot


def test_diff_book_flags_the_one_break_in_the_chain() -> None:
    adapter = BinanceAdapter()
    chain = [(100, 105, 95), (106, 110, 105), (115, 120, 112), (121, 125, 120)]
    flags = [adapter.normalize(diff_frame(*ids), RECEIVED)[0]["sequence_gap"] for ids in chain]
    assert flags == [True, False, True, False]


def test_a_partial_book_frame_does_not_feed_the_diff_chain() -> None:
    adapter = BinanceAdapter()
    assert adapter.normalize(diff_frame(100, 105, 95), RECEIVED)[0]["sequence_gap"] is True
    adapter.normalize(partial_book_frame(20), RECEIVED)
    assert adapter.normalize(diff_frame(106, 110, 105), RECEIVED)[0]["sequence_gap"] is False


# ------------------------------------------------------------------- trades


def agg_trade_frame(buyer_is_maker: bool) -> str:
    return frame(
        "btcusdt@aggTrade",
        {
            "e": "aggTrade",
            "E": 123456789,
            "a": 5933014,
            "s": "BTCUSDT",
            "p": "0.001",
            "q": "100",
            "f": 100,
            "l": 105,
            "T": 123456785,
            "m": buyer_is_maker,
        },
    )


def test_trade_side_follows_who_took_the_book() -> None:
    adapter = BinanceAdapter()
    (row,) = adapter.normalize(agg_trade_frame(True), RECEIVED)
    assert row == {
        "kind": "public_trade",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "local_receive_ts_ns": RECEIVED,
        "exchange_ts_ns": 123456785_000_000,
        "trade_id": "5933014",
        "price": 0.001,
        "qty": 100.0,
        "side": "Sell",
    }
    assert parse_row(row, default_venue="binance").side == "Sell"
    assert adapter.normalize(agg_trade_frame(False), RECEIVED)[0]["side"] == "Buy"


# ------------------------------------------------------------------ tickers


def test_mark_price_frame_carries_mark_index_and_funding() -> None:
    adapter = BinanceAdapter()
    raw = frame(
        "btcusdt@markPrice@1s",
        {
            "e": "markPriceUpdate",
            "E": 1562305380000,
            "s": "BTCUSDT",
            "p": "11794.15000000",
            "i": "11784.62659091",
            "P": "11784.25641265",
            "r": "0.00038167",
            "T": 1562306400000,
        },
    )
    (row,) = adapter.normalize(raw, RECEIVED)
    assert row == {
        "kind": "ticker",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "local_receive_ts_ns": RECEIVED,
        "exchange_system_ts_ns": 1562305380000_000_000,
        "message_type": "delta",
        "cross_sequence": 0,
        "values": {
            "mark_price": 11794.15,
            "index_price": 11784.62659091,
            "funding_rate": 0.00038167,
            "next_funding_time_ms": 1562306400000,
        },
    }
    typed = parse_row(row, default_venue="binance")
    assert typed.values["next_funding_time_ms"] == 1562306400000
    assert isinstance(typed.values["next_funding_time_ms"], int)


def test_day_ticker_frame_carries_last_price_and_the_two_volumes() -> None:
    adapter = BinanceAdapter()
    raw = frame(
        "btcusdt@ticker",
        {
            "e": "24hrTicker",
            "E": 1562305380000,
            "s": "BTCUSDT",
            "p": "0.0015",
            "c": "0.0025",
            "o": "0.0010",
            "h": "0.0025",
            "l": "0.0010",
            "v": "10000",
            "q": "18",
        },
    )
    (row,) = adapter.normalize(raw, RECEIVED)
    assert row == {
        "kind": "ticker",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "local_receive_ts_ns": RECEIVED,
        "exchange_system_ts_ns": 1562305380000_000_000,
        "message_type": "delta",
        "cross_sequence": 0,
        "values": {"last_price": 0.0025, "turnover_24h": 18.0, "volume_24h": 10000.0},
    }
    assert parse_row(row, default_venue="binance").values["last_price"] == 0.0025


# ------------------------------------------------------------- liquidations


def force_order_frame(order_side: str) -> str:
    return frame(
        "!forceOrder@arr",
        {
            "e": "forceOrder",
            "E": 1568014460893,
            "o": {
                "s": "BTCUSDT",
                "S": order_side,
                "o": "LIMIT",
                "f": "IOC",
                "q": "0.014",
                "p": "9910",
                "ap": "9910",
                "X": "FILLED",
                "l": "0.014",
                "z": "0.014",
                "T": 1568014460893,
            },
        },
    )


def test_liquidation_position_side_is_the_other_side_of_the_order() -> None:
    adapter = BinanceAdapter()
    (row,) = adapter.normalize(force_order_frame("SELL"), RECEIVED)
    assert row == {
        "kind": "liquidation",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "local_receive_ts_ns": RECEIVED,
        "exchange_system_ts_ns": 1568014460893_000_000,
        "exchange_ts_ns": 1568014460893_000_000,
        "position_side": "Buy",
        "qty": 0.014,
        "bankruptcy_price": 9910.0,
    }
    assert parse_row(row, default_venue="binance").position_side == "Buy"
    assert adapter.normalize(force_order_frame("BUY"), RECEIVED)[0]["position_side"] == "Sell"


# ------------------------------------------------------------------- klines


def test_kline_frame_is_one_candle() -> None:
    adapter = BinanceAdapter()
    raw = frame(
        "btcusdt@kline_1m",
        {
            "e": "kline",
            "E": 1638747660000,
            "s": "BTCUSDT",
            "k": {
                "t": 1638747660000,
                "T": 1638747719999,
                "s": "BTCUSDT",
                "i": "1m",
                "f": 100,
                "L": 200,
                "o": "0.0010",
                "c": "0.0020",
                "h": "0.0025",
                "l": "0.0015",
                "v": "1000",
                "n": 100,
                "x": True,
                "q": "1.0000",
            },
        },
    )
    (row,) = adapter.normalize(raw, RECEIVED)
    assert row == {
        "kind": "kline",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "interval": "1m",
        "local_receive_ts_ns": RECEIVED,
        "exchange_system_ts_ns": 1638747660000_000_000,
        "start_ms": 1638747660000,
        "end_ms": 1638747719999,
        "open": 0.001,
        "high": 0.0025,
        "low": 0.0015,
        "close": 0.002,
        "volume": 1000.0,
        "turnover": 1.0,
        "confirmed": True,
    }
    assert parse_row(row, default_venue="binance").confirmed


def test_frames_that_are_not_market_data_make_no_rows() -> None:
    adapter = BinanceAdapter()
    assert adapter.normalize(json.dumps({"result": None, "id": 1}), RECEIVED) == []
    assert adapter.normalize(json.dumps([1, 2]), RECEIVED) == []
    assert adapter.normalize(frame("btcusdt@forceOrder", {"e": "forceOrder"}), RECEIVED) == []


# ------------------------------------------------------------------- tables


INSTRUMENTS = [
    {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT", "marginAsset": "USDT"},
    {"symbol": "ETHUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT", "marginAsset": "USDT"},
    {"symbol": "BTCUSD_240329", "contractType": "CURRENT_QUARTER", "status": "TRADING", "quoteAsset": "USD", "marginAsset": "BTC"},
    {"symbol": "SOLUSDT", "contractType": "PERPETUAL", "status": "SETTLING", "quoteAsset": "USDT", "marginAsset": "USDT"},
    {"symbol": "ADAUSDC", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDC", "marginAsset": "USDC"},
    {"symbol": "BTCUSD", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USD", "marginAsset": "BTC"},
]

TICKERS = [
    {"symbol": "BTCUSDT", "quoteVolume": "9000000", "lastFundingRate": "-0.0012"},
    {"symbol": "ETHUSDT", "quoteVolume": "12000000", "lastFundingRate": "0.0001"},
    {"symbol": "ADAUSDC", "quoteVolume": "500", "lastFundingRate": ""},
    {"symbol": "XRPUSDT", "lastFundingRate": "-0.0003"},
]


def test_listed_symbols_keeps_trading_perpetuals() -> None:
    adapter = BinanceAdapter()
    assert adapter.listed_symbols(INSTRUMENTS, quote="USDT") == ["BTCUSDT", "ETHUSDT"]
    assert adapter.listed_symbols(INSTRUMENTS, quote="USDC") == ["ADAUSDC"]
    assert adapter.listed_symbols(INSTRUMENTS, quote=None) == ["ADAUSDC", "BTCUSD", "BTCUSDT", "ETHUSDT"]


def test_turnover_ranked_and_funding_rates() -> None:
    adapter = BinanceAdapter()
    assert adapter.turnover_ranked(TICKERS) == ["ETHUSDT", "BTCUSDT", "ADAUSDC"]
    assert adapter.funding_rates(TICKERS) == {"BTCUSDT": -0.0012, "ETHUSDT": 0.0001, "XRPUSDT": -0.0003}


def test_fetch_tables_merges_the_three_ticker_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    replies: dict[str, Any] = {
        "/fapi/v1/exchangeInfo": {"symbols": INSTRUMENTS},
        "/fapi/v1/premiumIndex": [
            {"symbol": "BTCUSDT", "markPrice": "60000.1", "indexPrice": "60001.2", "lastFundingRate": "-0.0012", "nextFundingTime": 1700000000000},
        ],
        "/fapi/v1/ticker/24hr": [{"symbol": "BTCUSDT", "lastPrice": "60000.5", "quoteVolume": "9000000", "volume": "150"}],
        "/fapi/v1/ticker/bookTicker": [
            {"symbol": "BTCUSDT", "bidPrice": "60000.0", "bidQty": "3", "askPrice": "60000.2", "askQty": "4"},
            {"symbol": "ETHUSDT", "bidPrice": "3000.0", "bidQty": "9", "askPrice": "3000.1", "askQty": "8"},
        ],
    }
    calls = fake_rest(monkeypatch, lambda url: replies[url.partition("fapi.binance.com")[2]])
    tables = BinanceAdapter().fetch_tables()
    assert [url.partition("fapi.binance.com")[2] for url in calls] == list(replies)
    assert tables["instruments"] == INSTRUMENTS
    assert tables["tickers"] == [
        {
            "symbol": "BTCUSDT",
            "markPrice": "60000.1",
            "indexPrice": "60001.2",
            "lastFundingRate": "-0.0012",
            "nextFundingTime": 1700000000000,
            "lastPrice": "60000.5",
            "quoteVolume": "9000000",
            "volume": "150",
            "bidPrice": "60000.0",
            "bidQty": "3",
            "askPrice": "60000.2",
            "askQty": "4",
        },
        {"symbol": "ETHUSDT", "bidPrice": "3000.0", "bidQty": "9", "askPrice": "3000.1", "askQty": "8"},
    ]


def test_a_refused_request_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rest(monkeypatch, lambda url: {"code": -1121, "msg": "Invalid symbol."})
    with pytest.raises(RuntimeError):
        BinanceAdapter().depth_snapshot_row("NOPEUSDT")


# ------------------------------------------------- the book anchor on connect


DEPTH_REPLIES = {
    "BTCUSDT": {
        "lastUpdateId": 77,
        "E": 1589436922972,
        "T": 1589436922959,
        "bids": [["60000.0", "3"]],
        "asks": [["60000.2", "4"]],
    },
    "ETHUSDT": {"lastUpdateId": 88, "E": 1589436923972, "T": 1589436923959, "bids": [], "asks": [["3000.1", "8"]]},
}


def depth_reply(url: str) -> Any:
    return DEPTH_REPLIES[url.partition("symbol=")[2].partition("&")[0]]


SHARD_TOPICS = ["btcusdt@depth@100ms", "btcusdt@aggTrade", "ethusdt@depth@100ms", "ethusdt@bookTicker"]


def test_on_subscribed_anchors_every_diff_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(venue, "SNAPSHOT_PAUSE_SECONDS", 0.05)
    calls = fake_rest(monkeypatch, depth_reply)
    adapter = BinanceAdapter()
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    adapter.on_subscribed(SHARD_TOPICS, rows.append, threading.Event())
    elapsed = time.monotonic() - started

    assert calls == [
        "https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000",
        "https://fapi.binance.com/fapi/v1/depth?symbol=ETHUSDT&limit=1000",
    ]
    assert elapsed >= 0.05
    assert [row["symbol"] for row in rows] == ["BTCUSDT", "ETHUSDT"]
    assert [row["update_id"] for row in rows] == [77, 88]
    assert all(row["kind"] == "orderbook_snapshot" and row["depth"] == 1000 for row in rows)
    assert rows[0]["bids"] == [["60000.0", "3"]]
    assert rows[0]["exchange_system_ts_ns"] == 1589436922972_000_000
    assert rows[0]["exchange_engine_ts_ns"] == 1589436922959_000_000
    assert rows[0]["local_receive_ts_ns"] > 0
    assert parse_row(rows[0], default_venue="binance").update_id == 77


def test_on_subscribed_does_nothing_for_a_shard_with_no_diff_book(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_rest(monkeypatch, depth_reply)
    rows: list[dict[str, Any]] = []
    BinanceAdapter().on_subscribed(["btcusdt@bookTicker", "btcusdt@aggTrade"], rows.append, threading.Event())
    assert (calls, rows) == ([], [])


def test_a_set_stop_event_ends_the_pacing_early(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_rest(monkeypatch, depth_reply)
    stop = threading.Event()
    stop.set()
    rows: list[dict[str, Any]] = []
    BinanceAdapter().on_subscribed(SHARD_TOPICS, rows.append, stop)
    assert (calls, rows) == ([], [])


def test_stopping_part_way_leaves_the_rest_unfetched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(venue, "SNAPSHOT_PAUSE_SECONDS", 0.0)
    calls = fake_rest(monkeypatch, depth_reply)
    stop = threading.Event()
    rows: list[dict[str, Any]] = []

    def emit(row: Any) -> None:
        rows.append(row)
        stop.set()

    BinanceAdapter().on_subscribed(SHARD_TOPICS, emit, stop)
    assert len(calls) == 1 and [row["symbol"] for row in rows] == ["BTCUSDT"]


def test_connecting_again_restarts_the_diff_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(venue, "SNAPSHOT_PAUSE_SECONDS", 0.0)
    fake_rest(monkeypatch, depth_reply)
    adapter = BinanceAdapter()
    adapter.normalize(diff_frame(100, 105, 95), RECEIVED)
    assert adapter.normalize(diff_frame(106, 110, 105), RECEIVED)[0]["sequence_gap"] is False
    adapter.on_subscribed(SHARD_TOPICS, lambda row: None, threading.Event())
    assert adapter.normalize(diff_frame(111, 115, 110), RECEIVED)[0]["sequence_gap"] is True


# ------------------------------------------------------ the open interest lane


def test_open_interest_row_is_priced_by_the_last_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rest(monkeypatch, lambda url: {"symbol": "BTCUSDT", "openInterest": "10659.509", "time": 1589437530011})
    adapter = BinanceAdapter()
    row = adapter.open_interest_row("BTCUSDT")
    assert row["values"] == {"open_interest": 10659.509}
    assert (row["message_type"], row["exchange_system_ts_ns"]) == ("poll", 1589437530011_000_000)

    adapter.normalize(frame("btcusdt@markPrice@1s", {"s": "BTCUSDT", "E": 1, "p": "100"}), RECEIVED)
    priced = adapter.open_interest_row("BTCUSDT")
    assert priced["values"] == {"open_interest": 10659.509, "open_interest_value": 1065950.9}
    assert parse_row(priced, default_venue="binance").values["open_interest"] == 10659.509


def test_lanes_run_only_for_the_symbols_that_ask_for_a_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_rest(monkeypatch, lambda url: {"symbol": "X", "openInterest": "1", "time": 1})
    adapter = BinanceAdapter()
    stop = threading.Event()
    stop.set()
    lanes = adapter.start_lanes(
        {"BTCUSDT": feeds("open_interest:300", "trades"), "ETHUSDT": feeds("open_interest:60"), "SOLUSDT": feeds("trades")},
        lambda row: None,
        stop,
    )
    for lane in lanes:
        lane.join(5.0)
    assert [lane.name for lane in lanes] == ["tape-binance-oi-60s", "tape-binance-oi-300s"]
    assert not any(lane.is_alive() for lane in lanes)
    assert calls == []

    assert adapter.start_lanes({"BTCUSDT": feeds("trades")}, lambda row: None, stop) == []


def test_live_subscription_requests_carry_ids_and_the_24h_change_is_a_fraction() -> None:
    adapter = BinanceAdapter()
    topics = [f"s{index}@aggTrade" for index in range(60)]

    added = [json.loads(text) for text in adapter.add_messages(topics)]
    removed = [json.loads(text) for text in adapter.remove_messages(topics[:2])]

    assert [message["method"] for message in added] == ["SUBSCRIBE", "SUBSCRIBE"]
    assert [len(message["params"]) for message in added] == [50, 10]
    assert [message["id"] for message in added] == [1, 2]
    assert removed == [{"method": "UNSUBSCRIBE", "params": topics[:2], "id": 3}]

    rows = adapter.normalize(
        json.dumps({"stream": "btcusdt@ticker", "data": {"e": "24hrTicker", "E": 1700000000000, "s": "BTCUSDT", "c": "60000", "q": "5e9", "v": "80000", "P": "-2.5"}}),
        9,
    )
    assert rows[0]["values"] == {"last_price": 60000.0, "turnover_24h": 5e9, "volume_24h": 80000.0, "price_change_24h_pct": -0.025}
    assert adapter.turnovers([{"symbol": "BTCUSDT", "quoteVolume": "5e9"}, {"symbol": "BAD", "quoteVolume": "n/a"}]) == {"BTCUSDT": 5e9}
