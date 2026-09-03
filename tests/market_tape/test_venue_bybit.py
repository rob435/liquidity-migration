"""The Bybit adapter: stream names, subscribe frames, message shapes, REST tables."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from market_tape.config import ConfigError, parse_feed
from market_tape.venues import adapter_for
from market_tape.venues.bybit import PUBLIC_LINEAR_WS, PUBLIC_REST, BybitAdapter


def rows(adapter: BybitAdapter, message: dict[str, Any], received_ns: int) -> list[dict[str, Any]]:
    return adapter.normalize(json.dumps(message), received_ns)


def book_message(kind: str = "snapshot", update: int = 10, sequence: int = 100) -> dict[str, Any]:
    return {
        "topic": "orderbook.50.AGIUSDT",
        "type": kind,
        "ts": 1_800_000_000_000,
        "cts": 1_799_999_999_999,
        "data": {
            "s": "AGIUSDT",
            "b": [["0.001", "20"]],
            "a": [["0.0011", "30"]],
            "u": update,
            "seq": sequence,
        },
    }


def test_the_adapter_records_the_linear_market_only() -> None:
    adapter = adapter_for("bybit", market="linear")
    assert isinstance(adapter, BybitAdapter)
    assert adapter.name == "bybit"
    assert adapter.ws_url == PUBLIC_LINEAR_WS
    assert adapter.rest_url == PUBLIC_REST
    assert BybitAdapter(market="linear", ws_url="ws://local", rest_url="http://local").ws_url == "ws://local"
    with pytest.raises(ConfigError, match="linear market"):
        BybitAdapter(market="inverse")
    with pytest.raises(ValueError, match="no adapter"):
        adapter_for("kraken", market="linear")


def test_the_normalizer_preserves_book_order_and_public_trade_arrivals() -> None:
    adapter = BybitAdapter()
    snapshot = rows(adapter, book_message(), 1_800_000_000_010_000_000)[0]
    delta = rows(adapter, book_message("delta", 11, 101), 1_800_000_000_020_000_000)[0]
    regression = rows(adapter, book_message("delta", 9, 99), 1_800_000_000_030_000_000)[0]
    trades = rows(
        adapter,
        {
            "topic": "publicTrade.AGIUSDT",
            "ts": 1_800_000_000_040,
            "data": [
                {"s": "AGIUSDT", "S": "Buy", "p": "0.0011", "v": "100", "i": "one"},
                {"s": "AGIUSDT", "S": "Sell", "p": "0.0010", "v": "80", "i": "two"},
            ],
        },
        1_800_000_000_040_000_000,
    )

    assert snapshot["kind"] == "orderbook_snapshot"
    assert snapshot["venue"] == "bybit"
    assert snapshot["depth"] == 50
    assert snapshot["exchange_system_ts_ns"] == 1_800_000_000_000_000_000
    assert snapshot["exchange_engine_ts_ns"] == 1_799_999_999_999_000_000
    assert snapshot["bids"] == [["0.001", "20"]]
    assert snapshot["cross_sequence"] == 100
    assert not snapshot["sequence_gap"]
    assert not snapshot["restart_snapshot"]
    assert delta["kind"] == "orderbook_delta"
    assert delta["previous_update_id"] == 10
    assert delta["previous_cross_sequence"] == 100
    assert not delta["sequence_gap"]
    assert regression["sequence_gap"]
    assert [(row["side"], row["trade_id"]) for row in trades] == [("Buy", "one"), ("Sell", "two")]
    assert len({row["local_receive_ts_ns"] for row in trades}) == 1
    assert trades[0]["exchange_ts_ns"] == 1_800_000_000_040_000_000
    assert trades[0]["venue"] == "bybit"


def test_a_first_update_id_reads_as_a_restart_snapshot() -> None:
    adapter = BybitAdapter()
    row = rows(adapter, book_message("delta", update=1, sequence=1), 1_800_000_000_010_000_000)[0]

    assert row["kind"] == "orderbook_snapshot"
    assert row["restart_snapshot"]
    assert not row["sequence_gap"]


def test_book_depths_keep_independent_sequence_state() -> None:
    adapter = BybitAdapter()
    deep = rows(adapter, book_message(), 1_800_000_000_010_000_000)[0]
    touch_message = book_message(update=50, sequence=500)
    touch_message["topic"] = "orderbook.1.AGIUSDT"
    touch = rows(adapter, touch_message, 1_800_000_000_020_000_000)[0]
    deep_delta = rows(adapter, book_message("delta", 11, 101), 1_800_000_000_030_000_000)[0]

    assert deep["depth"] == 50
    assert touch["depth"] == 1
    assert touch["previous_update_id"] == 0
    assert deep_delta["previous_update_id"] == 10
    assert not deep_delta["sequence_gap"]


def test_the_normalizer_preserves_ticker_deltas_and_liquidations() -> None:
    adapter = BybitAdapter()
    ticker = rows(
        adapter,
        {
            "topic": "tickers.AGIUSDT",
            "type": "delta",
            "ts": 1_800_000_000_000,
            "cs": 42,
            "data": {
                "symbol": "AGIUSDT",
                "markPrice": "0.00105",
                "openInterestValue": "125000",
                "fundingRate": "-0.0001",
                "nextFundingTime": "1800003600000",
                "indexPrice": "",
                "bid1Price": "not a number",
            },
        },
        1_800_000_000_010_000_000,
    )[0]
    liquidation = rows(
        adapter,
        {
            "topic": "allLiquidation.AGIUSDT",
            "type": "snapshot",
            "ts": 1_800_000_000_020,
            "data": [{"T": 1_800_000_000_019, "s": "AGIUSDT", "S": "Buy", "v": "20000", "p": "0.0009"}],
        },
        1_800_000_000_020_000_000,
    )[0]

    assert ticker == {
        "kind": "ticker",
        "venue": "bybit",
        "symbol": "AGIUSDT",
        "local_receive_ts_ns": 1_800_000_000_010_000_000,
        "exchange_system_ts_ns": 1_800_000_000_000_000_000,
        "message_type": "delta",
        "cross_sequence": 42,
        "values": {
            "mark_price": 0.00105,
            "open_interest_value": 125000.0,
            "funding_rate": -0.0001,
            "next_funding_time_ms": 1_800_003_600_000,
        },
    }
    assert liquidation["position_side"] == "Buy"
    assert liquidation["qty"] == 20000.0
    assert liquidation["bankruptcy_price"] == 0.0009
    assert liquidation["exchange_ts_ns"] == 1_800_000_000_019_000_000
    assert liquidation["venue"] == "bybit"


def test_the_normalizer_reads_venue_candles() -> None:
    adapter = BybitAdapter()
    candles = rows(
        adapter,
        {
            "topic": "kline.1.AGIUSDT",
            "type": "snapshot",
            "ts": 1_800_000_060_100,
            "data": [
                {
                    "start": 1_800_000_000_000,
                    "end": 1_800_000_059_999,
                    "open": "1.0",
                    "high": "2.0",
                    "low": "0.5",
                    "close": "1.5",
                    "volume": "100",
                    "turnover": "150",
                    "confirm": True,
                },
                {"start": 1_800_000_060_000, "end": 1_800_000_119_999, "open": "1.5", "confirm": False},
            ],
        },
        1_800_000_060_150_000_000,
    )

    assert [row["kind"] for row in candles] == ["kline", "kline"]
    assert candles[0]["interval"] == "1m"
    assert candles[0]["symbol"] == "AGIUSDT"
    assert candles[0]["confirmed"] and not candles[1]["confirmed"]
    assert (candles[0]["open"], candles[0]["high"], candles[0]["low"], candles[0]["close"]) == (1.0, 2.0, 0.5, 1.5)
    assert (candles[0]["volume"], candles[0]["turnover"]) == (100.0, 150.0)
    assert candles[1]["close"] == 0.0
    assert candles[0]["exchange_system_ts_ns"] == 1_800_000_060_100_000_000


def test_a_control_frame_or_an_unknown_topic_writes_nothing() -> None:
    adapter = BybitAdapter()
    assert rows(adapter, {"success": True, "op": "subscribe"}, 1) == []
    assert rows(adapter, {"topic": "position", "data": {"s": "AGIUSDT"}}, 1) == []
    assert rows(adapter, {"topic": "orderbook.50.AGIUSDT", "data": []}, 1) == []
    assert adapter.normalize(json.dumps([1, 2]), 1) == []


def test_only_the_book_topics_are_worth_re_anchoring() -> None:
    adapter = BybitAdapter()
    topics = [
        "orderbook.50.AGIUSDT",
        "publicTrade.AGIUSDT",
        "tickers.AGIUSDT",
        "allLiquidation.AGIUSDT",
        "orderbook.1.AGIUSDT",
    ]
    assert adapter.book_topics(topics) == ["orderbook.50.AGIUSDT", "orderbook.1.AGIUSDT"]
    assert adapter.book_topics([]) == []


def test_each_feed_names_its_venue_topic() -> None:
    adapter = BybitAdapter()
    feeds = [parse_feed(text) for text in ("book:50", "book:1", "trades", "ticker", "liquidations", "kline:1m")]

    assert adapter.topics("AGIUSDT", feeds) == [
        "orderbook.50.AGIUSDT",
        "orderbook.1.AGIUSDT",
        "publicTrade.AGIUSDT",
        "tickers.AGIUSDT",
        "allLiquidation.AGIUSDT",
        "kline.1.AGIUSDT",
    ]
    assert adapter.topics("AGIUSDT", [parse_feed("kline:1h")]) == ["kline.60.AGIUSDT"]
    assert adapter.topics("AGIUSDT", []) == []


def test_a_feed_bybit_does_not_publish_is_refused() -> None:
    adapter = BybitAdapter()
    adapter.validate_feeds([parse_feed(text) for text in ("book:1", "book:50", "book:1000", "trades", "ticker", "liquidations", "kline:1m")])

    with pytest.raises(ConfigError, match="book levels"):
        adapter.validate_feeds([parse_feed("book:7")])
    with pytest.raises(ConfigError, match="open interest on the ticker"):
        adapter.validate_feeds([parse_feed("open_interest:60")])
    with pytest.raises(ConfigError, match="kline intervals"):
        adapter.validate_feeds([parse_feed("kline:2m")])


def test_topics_are_subscribed_ten_at_a_time() -> None:
    adapter = BybitAdapter()
    topics = [f"tickers.SYM{index}USDT" for index in range(25)]

    messages = adapter.subscribe_messages(topics)

    assert len(messages) == 3
    args = [json.loads(text)["args"] for text in messages]
    assert [len(chunk) for chunk in args] == [10, 10, 5]
    assert [topic for chunk in args for topic in chunk] == topics
    assert {json.loads(text)["op"] for text in messages} == {"subscribe"}
    assert adapter.subscribe_messages([]) == []
    assert adapter.connection_url(topics) == PUBLIC_LINEAR_WS
    assert adapter.on_subscribed(topics, lambda row: None, threading.Event()) is None
    assert adapter.start_lanes({}, lambda row: None, threading.Event()) == []


def _perp(symbol: str, *, quote: str = "USDT", status: str = "Trading", contract: str = "LinearPerpetual", **extra: str) -> dict:
    return {"symbol": symbol, "status": status, "quoteCoin": quote, "settleCoin": quote, "contractType": contract, **extra}


def test_the_listed_universe_is_the_trading_crypto_usdt_perpetuals() -> None:
    adapter = BybitAdapter()
    instruments = [
        _perp("BTCUSDT"),
        _perp("MYXUSDT", symbolType="innovation"),
        _perp("ETHPERP", quote="USDC"),
        _perp("BTC-26SEP26", contract="LinearFutures"),
        _perp("OLDUSDT", status="Closed"),
        _perp("solusdt"),
        # Listed as perpetuals in the same category, told apart only by symbolType.
        _perp("NVDAUSDT", symbolType="stock", underlyingTicker="NVDA", marketRegion="US"),
        _perp("SOXLUSDT", symbolType="ETF", underlyingTicker="SOXL"),
        _perp("XAUUSDT", symbolType="commodity"),
        _perp("NEWTHINGUSDT", symbolType="index"),
        "not a row",
    ]

    assert adapter.listed_symbols(instruments, quote="USDT") == ["BTCUSDT", "MYXUSDT", "SOLUSDT"]
    assert adapter.listed_symbols(instruments, quote=None) == ["BTCUSDT", "ETHPERP", "MYXUSDT", "SOLUSDT"]
    assert adapter.listed_symbols([], quote="USDT") == []
    # A label the venue has not used yet is outside the domain too, and is
    # counted so it shows up in the journal instead of vanishing.
    assert adapter.excluded_listed(instruments, quote="USDT") == {"ETF": 1, "commodity": 1, "index": 1, "stock": 1}
    assert adapter.excluded_listed(instruments, quote="USDC") == {}


def test_turnover_ranks_highest_first_and_funding_reads_as_a_fraction() -> None:
    adapter = BybitAdapter()
    tickers = [
        {"symbol": "AGIUSDT", "turnover24h": "1000", "fundingRate": "-0.0012"},
        {"symbol": "BTCUSDT", "turnover24h": "9000000", "fundingRate": "0.0001"},
        {"symbol": "ETHUSDT", "turnover24h": "1000", "fundingRate": ""},
        {"symbol": "BADUSDT", "turnover24h": "n/a", "fundingRate": "n/a"},
        {"symbol": "NEWUSDT"},
        "not a row",
    ]

    assert adapter.turnover_ranked(tickers) == ["BTCUSDT", "AGIUSDT", "ETHUSDT", "NEWUSDT"]
    assert adapter.funding_rates(tickers) == {"AGIUSDT": -0.0012, "BTCUSDT": 0.0001}


def test_live_subscription_changes_and_the_24h_change_field() -> None:
    adapter = BybitAdapter()
    topics = [f"publicTrade.S{index}" for index in range(12)]

    added = [json.loads(text) for text in adapter.add_messages(topics)]
    removed = [json.loads(text) for text in adapter.remove_messages(topics[:3])]

    assert [message["op"] for message in added] == ["subscribe", "subscribe"]
    assert [len(message["args"]) for message in added] == [10, 2]
    assert removed == [{"op": "unsubscribe", "args": topics[:3]}]

    rows = adapter.normalize(
        json.dumps(
            {
                "topic": "tickers.AGIUSDT",
                "type": "delta",
                "ts": 1_800_000_000_000,
                "data": {"symbol": "AGIUSDT", "price24hPcnt": "-0.1234", "turnover24h": "42"},
            }
        ),
        7,
    )
    assert rows[0]["values"] == {"price_change_24h_pct": -0.1234, "turnover_24h": 42.0}
    assert adapter.turnovers([{"symbol": "a", "turnover24h": "5"}, {"symbol": "b", "turnover24h": "x"}, {"symbol": "c"}]) == {
        "A": 5.0,
        "C": 0.0,
    }
