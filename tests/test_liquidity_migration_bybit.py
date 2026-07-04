from __future__ import annotations

import logging
import sys
import threading
from types import SimpleNamespace

import pytest

from liquidity_migration import bybit
from liquidity_migration.bybit import BybitKlineStreamPool


def test_pybit_rate_limit_log_filter_drops_10006_messages(caplog) -> None:
    # pybit logs the rate-limit retries at ERROR level on its _http_manager
    # logger; that produces tens of thousands of identical lines per minute on
    # the demo VPS at top-of-hour. liquidity_migration.bybit installs a filter
    # on pybit._http_manager that drops only the 10006 lines.
    logger = logging.getLogger("pybit._http_manager")
    with caplog.at_level(logging.ERROR, logger="pybit._http_manager"):
        logger.error(
            "Too many visits. Exceeded the API Rate Limit. (ErrCode: 10006). "
            "Hit the API rate limit on https://api.bybit.com/v5/market/kline. "
            "Sleeping then trying again."
        )
        logger.error("API rate limit will reset at 13:00:09. Sleeping for 2000 ms. Retrying...")
        logger.error("connection reset by peer (ErrCode: 502). Retrying...")
    messages = [record.getMessage() for record in caplog.records]
    assert all("10006" not in m for m in messages), messages
    assert all("rate limit" not in m.lower() for m in messages), messages
    assert any("502" in m for m in messages), messages


def test_bybit_market_data_constructs_with_slotted_client(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitMarketData(testnet=True)

    assert client._client.testnet is True


def test_bybit_private_client_constructs_demo_session(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    assert client._client.kwargs["demo"] is True
    assert client._client.kwargs["api_key"] == "key"
    assert client._client.kwargs["api_secret"] == "secret"


def test_get_funding_settlements_follows_pagination_cursor(monkeypatch) -> None:
    """H4: the funding total must not be truncated to the first 200-row page —
    follow nextPageCursor to the end."""
    pages = {
        None: {
            "retCode": 0,
            "result": {"list": [{"funding": "1"}, {"funding": "2"}], "nextPageCursor": "page2"},
        },
        "page2": {
            "retCode": 0,
            "result": {"list": [{"funding": "3"}], "nextPageCursor": ""},
        },
    }

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.calls: list[dict] = []

        def get_transaction_log(self, **params):
            self.calls.append(params)
            return pages[params.get("cursor")]

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    rows = client.get_funding_settlements(start_time_ms=1_000, end_time_ms=2_000)

    assert [row["funding"] for row in rows] == ["1", "2", "3"]
    # Two pages fetched: the cursor was followed exactly once.
    assert len(client._client.calls) == 2
    assert client._client.calls[1]["cursor"] == "page2"


def test_get_closed_pnl_follows_pagination_cursor(monkeypatch) -> None:
    """A re-entered symbol's closures can exceed one 200-row page; the orphan-close
    backfill must follow nextPageCursor or it could miss the real closing record
    (audit pass2 #6). Single-page behaviour (no cursor) is unchanged."""
    pages = {
        None: {"retCode": 0, "result": {"list": [{"orderId": "c1"}, {"orderId": "c2"}], "nextPageCursor": "p2"}},
        "p2": {"retCode": 0, "result": {"list": [{"orderId": "c3"}], "nextPageCursor": ""}},
    }

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.calls: list[dict] = []

        def get_closed_pnl(self, **params):
            self.calls.append(params)
            return pages[params.get("cursor")]

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    rows = client.get_closed_pnl(symbol="FOOUSDT")

    assert [row["orderId"] for row in rows] == ["c1", "c2", "c3"]
    assert len(client._client.calls) == 2
    assert client._client.calls[1]["cursor"] == "p2"


def test_bybit_public_trade_stream_subscribes_symbols(monkeypatch) -> None:
    class FakeWebSocket:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.trade_calls = []
            self.closed = False

        def trade_stream(self, **params):
            self.trade_calls.append(params)

        def exit(self):
            self.closed = True

    monkeypatch.setattr(bybit, "WebSocket", FakeWebSocket)

    client = bybit.BybitPublicTradeStream(testnet=True)
    callback = object()
    client.subscribe_public_trades(["BTCUSDT", "ETHUSDT"], callback)
    client.close()

    assert client._client.kwargs == {"testnet": True, "channel_type": "linear"}
    assert client._client.trade_calls == [{"symbol": ["BTCUSDT", "ETHUSDT"], "callback": callback}]
    assert client._client.closed is True


def test_bybit_public_ticker_stream_subscribes_symbols(monkeypatch) -> None:
    class FakeWebSocket:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.ticker_calls = []
            self.closed = False

        def ticker_stream(self, **params):
            self.ticker_calls.append(params)

        def exit(self):
            self.closed = True

    monkeypatch.setattr(bybit, "WebSocket", FakeWebSocket)

    client = bybit.BybitPublicTickerStream(testnet=True, demo=True)
    callback = object()
    client.subscribe_tickers(["BTCUSDT", "ETHUSDT"], callback)
    client.close()

    assert client._client.kwargs == {"testnet": True, "demo": True, "channel_type": "linear"}
    assert client._client.ticker_calls == [{"symbol": ["BTCUSDT", "ETHUSDT"], "callback": callback}]
    assert client._client.closed is True


def test_bybit_private_websocket_stream_subscribes_private_topics(monkeypatch) -> None:
    class FakeWebSocket:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []

        def position_stream(self, **params):
            self.calls.append(("position", params))

        def order_stream(self, **params):
            self.calls.append(("order", params))

        def execution_stream(self, **params):
            self.calls.append(("execution", params))

        def fast_execution_stream(self, **params):
            self.calls.append(("fast_execution", params))

    monkeypatch.setattr(bybit, "WebSocket", FakeWebSocket)

    client = bybit.BybitPrivateWebSocketStream(api_key="key", api_secret="secret", demo=True)
    callback = object()
    client.subscribe_positions(callback)
    client.subscribe_orders(callback)
    client.subscribe_executions(callback, fast=True)

    assert client._client.kwargs == {
        "testnet": False,
        "demo": True,
        "channel_type": "private",
        "api_key": "key",
        "api_secret": "secret",
    }
    assert client._client.calls == [
        ("position", {"callback": callback}),
        ("order", {"callback": callback}),
        ("fast_execution", {"callback": callback}),
    ]


def test_bybit_pybit_ping_timer_patch_uses_daemon_timer(monkeypatch) -> None:
    class FakeManager:
        ping_interval = 1000

        def _send_custom_ping(self):
            pass

    monkeypatch.setitem(sys.modules, "pybit._websocket_stream", SimpleNamespace(_V5WebSocketManager=FakeManager))

    bybit._patch_pybit_daemon_ping_timer()
    manager = FakeManager()
    manager._send_initial_ping()

    assert manager._agc_ping_timer.daemon is True
    manager._agc_ping_timer.cancel()


def test_bybit_websocket_trade_client_wraps_place_and_cancel(monkeypatch) -> None:
    class FakeWebSocketTrading:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []

        def place_order(self, callback, **params):
            self.calls.append(("place", callback, params))

        def cancel_order(self, callback, **params):
            self.calls.append(("cancel", callback, params))

    monkeypatch.setattr(bybit, "WebSocketTrading", FakeWebSocketTrading)

    client = bybit.BybitWebSocketTradeClient(api_key="key", api_secret="secret", demo=True, recv_window=1000)
    callback = object()
    client.place_order(callback, symbol="BTCUSDT", side="Buy", orderType="Market", qty="0.001", orderLinkId="agc")
    client.cancel_order(callback, symbol="BTCUSDT", order_link_id="agc")

    assert client._client.kwargs == {
        "testnet": False,
        "demo": True,
        "api_key": "key",
        "api_secret": "secret",
        "recv_window": 1000,
    }
    assert client._client.calls == [
        (
            "place",
            callback,
            {
                "category": "linear",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "orderType": "Market",
                "qty": "0.001",
                "orderLinkId": "agc",
            },
        ),
        ("cancel", callback, {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": "agc"}),
    ]


def test_resolve_private_credentials_toggle(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.setenv("BYBIT_REAL_API_KEY", "real-k")
    monkeypatch.setenv("BYBIT_REAL_API_SECRET", "real-s")

    # No toggle set -> demo is the default.
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    assert bybit.resolve_private_credentials() == ("demo-k", "demo-s", True)

    # DEMO=true -> demo account.
    monkeypatch.setenv("DEMO", "true")
    assert bybit.resolve_private_credentials() == ("demo-k", "demo-s", True)

    # REAL_MONEY=true -> mainnet account.
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setenv("REAL_MONEY", "true")
    assert bybit.resolve_private_credentials() == ("real-k", "real-s", False)

    # Both true is a contradiction and raises.
    monkeypatch.setenv("DEMO", "true")
    with pytest.raises(RuntimeError, match="both set true"):
        bybit.resolve_private_credentials()


def test_bybit_private_client_accepts_mainnet(monkeypatch) -> None:
    constructed: dict = {}

    class FakeHTTP:
        def __init__(self, **kwargs):
            constructed.update(kwargs)

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=False)
    assert constructed["demo"] is False


def test_bybit_private_client_wraps_order_and_trade_history(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.order_history_calls = []
            self.execution_calls = []

        def get_order_history(self, **params):
            self.order_history_calls.append(params)
            return {"retCode": 0, "result": {"list": [{"orderLinkId": params["orderLinkId"], "orderStatus": "Filled"}]}}

        def get_executions(self, **params):
            self.execution_calls.append(params)
            return {"retCode": 0, "result": {"list": [{"orderLinkId": params["orderLinkId"], "execQty": "1"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    orders = client.get_order_history(symbol="BTCUSDT", order_link_id="lm-link")
    trades = client.get_trade_history(symbol="BTCUSDT", order_link_id="lm-link")

    assert orders[0]["orderStatus"] == "Filled"
    assert trades[0]["execQty"] == "1"
    assert client._client.order_history_calls == [
        {"category": "linear", "limit": 50, "symbol": "BTCUSDT", "orderLinkId": "lm-link"}
    ]
    assert client._client.execution_calls == [
        {"category": "linear", "limit": 50, "symbol": "BTCUSDT", "orderLinkId": "lm-link"}
    ]


def test_bybit_private_client_wraps_cancel_all_and_positions_by_settle(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.cancel_all_calls = []
            self.position_calls = []

        def cancel_all_orders(self, **params):
            self.cancel_all_calls.append(params)
            return {"retCode": 0, "result": {"success": "1"}}

        def get_positions(self, **params):
            self.position_calls.append(params)
            return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": "1"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    cancelled = client.cancel_all_orders(settle_coin="USDT")
    positions = client.get_positions(settle_coin="USDT")

    assert cancelled["success"] == "1"
    assert positions[0]["symbol"] == "BTCUSDT"
    assert client._client.cancel_all_calls == [{"category": "linear", "settleCoin": "USDT"}]
    assert client._client.position_calls == [{"category": "linear", "limit": 200, "settleCoin": "USDT"}]


def test_bybit_private_client_wraps_open_orders_by_settle(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.open_order_calls = []

        def get_open_orders(self, **params):
            self.open_order_calls.append(params)
            return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "orderStatus": "New"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    orders = client.get_open_orders()

    assert orders[0]["orderStatus"] == "New"
    assert client._client.open_order_calls == [{"category": "linear", "limit": 50, "settleCoin": "USDT"}]


def test_bybit_private_client_paginates_open_orders(monkeypatch) -> None:
    pages = {
        None: {"retCode": 0, "result": {"list": [{"orderId": "o1"}], "nextPageCursor": "p2"}},
        "p2": {"retCode": 0, "result": {"list": [{"orderId": "o2"}], "nextPageCursor": ""}},
    }

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.open_order_calls = []

        def get_open_orders(self, **params):
            self.open_order_calls.append(params)
            return pages[params.get("cursor")]

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    orders = client.get_open_orders(settle_coin="USDT")

    assert [row["orderId"] for row in orders] == ["o1", "o2"]
    assert client._client.open_order_calls == [
        {"category": "linear", "limit": 50, "settleCoin": "USDT"},
        {"category": "linear", "limit": 50, "settleCoin": "USDT", "cursor": "p2"},
    ]


def test_bybit_private_client_paginates_positions(monkeypatch) -> None:
    pages = {
        None: {"retCode": 0, "result": {"list": [{"symbol": "AAAUSDT"}], "nextPageCursor": "p2"}},
        "p2": {"retCode": 0, "result": {"list": [{"symbol": "BBBUSDT"}], "nextPageCursor": ""}},
    }

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.position_calls = []

        def get_positions(self, **params):
            self.position_calls.append(params)
            return pages[params.get("cursor")]

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)

    positions = client.get_positions(settle_coin="USDT")

    assert [row["symbol"] for row in positions] == ["AAAUSDT", "BBBUSDT"]
    assert client._client.position_calls == [
        {"category": "linear", "limit": 200, "settleCoin": "USDT"},
        {"category": "linear", "limit": 200, "settleCoin": "USDT", "cursor": "p2"},
    ]


def test_bybit_private_client_sets_demo_leverage(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.leverage_calls = []

        def set_leverage(self, **params):
            self.leverage_calls.append(params)
            return {"retCode": 0, "result": {"symbol": params["symbol"]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    result = client.set_leverage(symbol="BTCUSDT", buy_leverage=1.0, sell_leverage=1.0)

    assert result == {"symbol": "BTCUSDT"}
    assert client._client.leverage_calls == [
        {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "1", "sellLeverage": "1"}
    ]


def test_bybit_private_client_treats_existing_leverage_as_success(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.leverage_calls = []

        def set_leverage(self, **params):
            self.leverage_calls.append(params)
            return {"retCode": 110043, "retMsg": "leverage not modified", "result": {}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    result = client.set_leverage(symbol="BTCUSDT", buy_leverage=1.0, sell_leverage=1.0)

    assert result == {"symbol": "BTCUSDT", "buyLeverage": "1", "sellLeverage": "1", "retCode": 110043}


def test_bybit_private_client_treats_pybit_existing_leverage_exception_as_success(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_leverage(self, **params):
            del params
            raise RuntimeError("110043: leverage not modified")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    result = client.set_leverage(symbol="BTCUSDT", buy_leverage=1.0, sell_leverage=1.0)

    assert result == {"symbol": "BTCUSDT", "buyLeverage": "1", "sellLeverage": "1", "retCode": 110043}


def test_bybit_private_client_wraps_trading_stop(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.trading_stop_calls = []

        def set_trading_stop(self, **params):
            self.trading_stop_calls.append(params)
            return {"retCode": 0, "result": {"ok": True}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    result = client.set_trading_stop(
        symbol="BTCUSDT",
        stop_loss="120",
        take_profit="80",
        trailing_stop="2.5",
        active_price="95",
    )

    assert result == {"ok": True}
    assert client._client.trading_stop_calls == [
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "tpslMode": "Full",
            "positionIdx": 0,
            "stopLoss": "120",
            "takeProfit": "80",
            "trailingStop": "2.5",
            "activePrice": "95",
            "tpTriggerBy": "MarkPrice",
            "slTriggerBy": "MarkPrice",
        }
    ]


def test_kline_download_chunks_full_range_when_bybit_returns_newest_first(monkeypatch) -> None:
    interval_ms = bybit.INTERVAL_MS["60"]
    timestamps = [index * interval_ms for index in range(10)]

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls = []

        def get_kline(self, **params):
            self.calls.append(params)
            start = int(params["start"])
            end = int(params["end"])
            limit = int(params["limit"])
            rows = [
                [str(ts), "1", "2", "0.5", "1.5", "10", "15"]
                for ts in timestamps
                if start <= ts <= end
            ]
            return {"retCode": 0, "result": {"list": list(reversed(rows))[:limit]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitMarketData()
    rows = client.get_klines("BTCUSDT", "60", timestamps[0], timestamps[-1], limit=3)

    assert [int(row[0]) for row in rows] == timestamps
    assert len(client._client.calls) > 1
    assert max(int(call["end"]) - int(call["start"]) for call in client._client.calls) <= interval_ms * 2


def test_bybit_market_data_records_retry_and_rate_limit_stats(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls = 0

        def get_tickers(self, **params):
            del params
            self.calls += 1
            if self.calls == 1:
                return {"retCode": 10006, "retMsg": "Too many visits. Exceeded the API Rate Limit."}
            return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitMarketData(retry_sleep_seconds=0.0)
    rows = client.get_tickers()
    stats = client.stats()

    assert rows == [{"symbol": "BTCUSDT"}]
    assert stats["logical_calls"] == 1
    assert stats["http_calls"] == 2
    assert stats["retry_events"] == 1
    assert stats["rate_limit_events"] == 1
    assert stats["error_events"] == 1
    assert stats["backoff_events"] == 2
    assert "10006" in stats["last_error"]


def test_time_range_download_pages_backward_when_bybit_returns_newest_first(monkeypatch) -> None:
    timestamps = [index * 1000 for index in range(10)]

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.funding_calls = []
            self.oi_calls = []

        def get_funding_rate_history(self, **params):
            self.funding_calls.append(params)
            return _newest_first_page(timestamps, params, "fundingRateTimestamp", limit_key="limit")

        def get_open_interest(self, **params):
            self.oi_calls.append(params)
            return _newest_first_page(timestamps, params, "timestamp", limit_key="limit")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitMarketData()
    funding = client.get_funding_history("BTCUSDT", timestamps[0], timestamps[-1], limit=3)
    oi = client.get_open_interest("BTCUSDT", "1h", timestamps[0], timestamps[-1], limit=3)

    assert [int(row["fundingRateTimestamp"]) for row in funding] == timestamps
    assert [int(row["timestamp"]) for row in oi] == timestamps
    assert len(client._client.funding_calls) > 1
    assert len(client._client.oi_calls) > 1


def _newest_first_page(timestamps: list[int], params: dict, timestamp_key: str, *, limit_key: str) -> dict:
    start = int(params["startTime"])
    end = int(params["endTime"])
    limit = int(params[limit_key])
    rows = [{timestamp_key: str(ts), "fundingRate": "0.0001", "openInterest": "100"} for ts in timestamps if start <= ts <= end]
    return {"retCode": 0, "result": {"list": list(reversed(rows))[:limit]}}


def test_bybit_rest_rate_limiter_throttles_within_window() -> None:
    import time as _time
    limiter = bybit.BybitRestRateLimiter(max_requests=3, per_seconds=0.2)
    started = _time.monotonic()
    for _ in range(6):
        limiter.acquire()
    elapsed = _time.monotonic() - started
    # 6 acquires at 3 per 0.2s must take at least one full window beyond the
    # first 3 immediate acquires; anything below 0.18s means the limiter is
    # silently letting bursts through.
    assert elapsed >= 0.18, f"limiter let burst through in {elapsed:.3f}s"
    stats = limiter.stats()
    assert stats["throttle_events"] >= 1
    assert stats["throttled_seconds"] > 0.0


def test_bybit_rest_rate_limiter_no_throttle_under_budget() -> None:
    limiter = bybit.BybitRestRateLimiter(max_requests=10, per_seconds=1.0)
    for _ in range(5):
        limiter.acquire()
    assert limiter.stats()["throttle_events"] == 0


def test_bybit_market_data_routes_get_through_rate_limiter(monkeypatch) -> None:
    """BybitMarketData must call rate_limiter.acquire() before each pybit HTTP
    call. This is the only way concurrent kline workers stay under Bybit's
    public REST budget; without it, pybit handles the 429 by sleeping 2s per
    retry, which previously caused ~30 spam lines per demo entry cycle.
    """
    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls = 0

        def get_tickers(self, **_kwargs):
            self.calls += 1
            return {"retCode": 0, "result": {"list": []}}

    class RecordingLimiter:
        def __init__(self) -> None:
            self.acquires = 0

        def acquire(self) -> None:
            self.acquires += 1

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    limiter = RecordingLimiter()
    client = bybit.BybitMarketData(rate_limiter=limiter)  # type: ignore[arg-type]

    client.get_tickers()
    client.get_tickers()

    assert limiter.acquires == 2
    assert client._client.calls == 2


def test_bybit_private_client_routes_call_through_rate_limiter(monkeypatch) -> None:
    """BybitPrivateClient must acquire the shared rate limiter before every
    pybit HTTP call on BOTH _call and _call_once paths. Mirrors the public-side
    test. Without this, parallel place_orders bypass the budget that protects
    against Bybit's per-account REST throttles.
    """
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = 0

        def place_order(self, **_kwargs):
            self.calls += 1
            return {"retCode": 0, "result": {"orderId": "x"}}

        def get_wallet_balance(self, **_kwargs):
            self.calls += 1
            return {"retCode": 0, "result": {"list": []}}

    class RecordingLimiter:
        def __init__(self) -> None:
            self.acquires = 0

        def acquire(self) -> None:
            self.acquires += 1

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    limiter = RecordingLimiter()
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        rate_limiter=limiter,  # type: ignore[arg-type]
    )

    # place_order goes through _call_once (single-shot, no retries).
    client.place_order(symbol="AAAUSDT", side="Buy", qty="1", orderType="Market", orderLinkId="lm-test-1")
    # get_wallet_balance goes through _call (retry-capable).
    client.get_wallet_balance()

    assert limiter.acquires == 2
    assert client._client.calls == 2


def test_bybit_private_client_rate_limiter_acquires_each_retry(monkeypatch) -> None:
    """When _call retries on a failed pybit call, each attempt must hit the
    limiter — otherwise a tight retry burst escapes the budget that's there to
    protect us.
    """
    attempt_counter = {"n": 0}

    class FlakyHTTP:
        def __init__(self, **kwargs):
            pass

        def get_wallet_balance(self, **_kwargs):
            attempt_counter["n"] += 1
            if attempt_counter["n"] < 2:
                return {"retCode": 10006, "retMsg": "rate limit"}
            return {"retCode": 0, "result": {"list": []}}

    class RecordingLimiter:
        def __init__(self) -> None:
            self.acquires = 0

        def acquire(self) -> None:
            self.acquires += 1

    monkeypatch.setattr(bybit, "HTTP", FlakyHTTP)
    limiter = RecordingLimiter()
    client = bybit.BybitPrivateClient(
        api_key="k",
        api_secret="s",
        demo=True,
        retries=3,
        retry_sleep_seconds=0.0,
        rate_limiter=limiter,  # type: ignore[arg-type]
    )

    client.get_wallet_balance()
    assert attempt_counter["n"] == 2
    assert limiter.acquires == 2


def test_validate_order_submit_allowed_blocks_mainnet(monkeypatch) -> None:
    monkeypatch.setenv("REAL_MONEY", "true")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setenv("BYBIT_REAL_API_KEY", "k")
    monkeypatch.setenv("BYBIT_REAL_API_SECRET", "s")
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        bybit.validate_order_submit_allowed(submit_orders=True, confirm_demo_orders=True)


def test_validate_order_submit_allowed_requires_confirm_flag(monkeypatch) -> None:
    monkeypatch.delenv("REAL_MONEY", raising=False)
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "s")
    with pytest.raises(RuntimeError, match="confirm-demo-orders"):
        bybit.validate_order_submit_allowed(submit_orders=True, confirm_demo_orders=False)


def test_api_key_allows_order_submit_rejects_read_only_key() -> None:
    allowed, reason = bybit.api_key_allows_order_submit(
        {
            "readOnly": 1,
            "permissions": {"ContractTrade": ["Order", "Position"]},
        }
    )
    assert allowed is False
    assert "readOnly=1" in reason


def test_api_key_allows_order_submit_requires_contract_trade_permissions() -> None:
    allowed, reason = bybit.api_key_allows_order_submit(
        {
            "readOnly": 0,
            "permissions": {"ContractTrade": ["Order"]},
        }
    )
    assert allowed is False
    assert "Position" in reason


def test_api_key_allows_order_submit_rejects_malformed_metadata() -> None:
    allowed, reason = bybit.api_key_allows_order_submit({"readOnly": 0})
    assert allowed is False
    assert "missing permissions" in reason

    allowed, reason = bybit.api_key_allows_order_submit({"permissions": {"ContractTrade": ["Order", "Position"]}})
    assert allowed is False
    assert "missing readOnly" in reason


def test_private_client_get_api_key_information(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_api_key_information(self):
            return {
                "retCode": 0,
                "result": {
                    "readOnly": 0,
                    "permissions": {"ContractTrade": ["Order", "Position"]},
                },
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    info = client.get_api_key_information()
    assert info["readOnly"] == 0
    assert bybit.api_key_allows_order_submit(info) == (True, "")


def test_private_credentials_present_uses_active_account(monkeypatch) -> None:
    from liquidity_migration.event_demo import _private_credentials_present

    monkeypatch.setenv("REAL_MONEY", "true")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("BYBIT_DEMO_API_KEY", raising=False)
    monkeypatch.setenv("BYBIT_REAL_API_KEY", "k")
    monkeypatch.setenv("BYBIT_REAL_API_SECRET", "s")
    assert _private_credentials_present() is True


def test_build_ws_trade_client_retries_then_succeeds(monkeypatch) -> None:
    """The multi-daemon demo connect-storm fix: a transient connect failure is
    retried with backoff (fresh client each attempt) until it succeeds."""
    monkeypatch.setattr(bybit, "WebSocketTrading", object)  # pybit "present"
    calls = {"n": 0}

    class _Fake:
        def __init__(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("connection failed. Too many connection attempts")

    monkeypatch.setattr(bybit, "BybitWebSocketTradeClient", _Fake)
    slept: list[float] = []
    client = bybit.build_ws_trade_client(
        category="linear", testnet=False, demo=True, api_key="k", api_secret="s",
        sleep=lambda s: slept.append(s), rng=lambda a, b: 0.0,
    )
    assert isinstance(client, _Fake)
    assert calls["n"] == 3  # failed twice, succeeded on the third fresh client
    assert len(slept) >= 2  # backoff slept between retries


def test_build_ws_trade_client_raises_after_max_attempts(monkeypatch) -> None:
    """All attempts fail -> raise so the caller falls back to REST (seatbelt)."""
    monkeypatch.setattr(bybit, "WebSocketTrading", object)

    class _AlwaysFail:
        def __init__(self, **kwargs):
            raise RuntimeError("too many connection attempts")

    monkeypatch.setattr(bybit, "BybitWebSocketTradeClient", _AlwaysFail)
    with pytest.raises(RuntimeError, match="too many"):
        bybit.build_ws_trade_client(
            category="linear", testnet=False, demo=True, api_key="k", api_secret="s",
            attempts=3, sleep=lambda s: None, rng=lambda a, b: 0.0,
        )


def test_build_ws_trade_client_fails_fast_on_missing_creds(monkeypatch) -> None:
    """Permanent errors (no creds) raise immediately with NO jitter/backoff —
    keeps credential-less unit tests fast and network-free."""
    monkeypatch.setattr(bybit, "WebSocketTrading", object)
    slept: list[float] = []
    with pytest.raises(RuntimeError, match="API key"):
        bybit.build_ws_trade_client(
            category="linear", testnet=False, demo=True, api_key=None, api_secret=None,
            sleep=lambda s: slept.append(s), rng=lambda a, b: 0.0,
        )
    assert slept == []


def test_bybit_market_data_does_not_retry_definite_reject(monkeypatch) -> None:
    """EXC-3: a definite (non-rate-limit) venue reject must raise IMMEDIATELY, not burn
    the full retry budget + exponential backoff re-issuing the identical failing call."""
    import pytest as _pytest

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls = 0

        def get_tickers(self, **params):
            del params
            self.calls += 1
            return {"retCode": 10001, "retMsg": "params error: invalid symbol"}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitMarketData(retry_sleep_seconds=0.0, retries=3)
    with _pytest.raises(bybit.BybitDataError):
        client.get_tickers()
    stats = client.stats()
    assert stats["http_calls"] == 1, "a definite reject must NOT be retried"
    assert stats["retry_events"] == 0


def test_bybit_market_data_still_retries_rate_limit(monkeypatch) -> None:
    """EXC-3 guard: a rate-limit reject must STILL retry (only definite rejects short-circuit)."""
    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.calls = 0

        def get_tickers(self, **params):
            del params
            self.calls += 1
            if self.calls == 1:
                return {"retCode": 10006, "retMsg": "Too many visits. Exceeded the API Rate Limit."}
            return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitMarketData(retry_sleep_seconds=0.0, retries=3)
    assert client.get_tickers() == [{"symbol": "BTCUSDT"}]
    assert client.stats()["http_calls"] == 2
    assert client.stats()["retry_events"] == 1


def test_private_call_definite_reject_no_retry_and_message_preserved(monkeypatch) -> None:
    """REGRESSION (audit 2026-06-12, live-measured): pybit 5.x raises InvalidRequestError
    for a non-zero retCode BEFORE the wrapper's own retCode check, so definite venue
    rejects were retried with backoff (846ms on a cancel-nonexistent) and the final
    raise dropped the retCode/retMsg — every ledgered error read a bare
    'failed after retries'. Rejects must raise immediately WITH the venue message."""
    InvalidRequestError = type("InvalidRequestError", (Exception,), {})  # pybit shape, no hard dep

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.cancel_calls = 0

        def cancel_order(self, **params):
            self.cancel_calls += 1
            raise InvalidRequestError("order not exists or too late to cancel (ErrCode: 110001)")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    client.retry_sleep_seconds = 0.0
    with pytest.raises(bybit.BybitDataError) as excinfo:
        client.cancel_order(symbol="BTCUSDT", order_link_id="lm-x")
    assert client._client.cancel_calls == 1  # definite reject: NO retry
    assert "110001" in str(excinfo.value)  # venue message survives into str(exc)


def test_private_call_transport_retries_and_final_message_carries_cause(monkeypatch) -> None:
    """Transport errors still retry; the exhausted-retries raise now carries the last
    error's text (callers ledger f"{exc}" — __cause__ never reached those columns)."""
    class FakeHTTP:
        def __init__(self, **kwargs):
            self.calls = 0

        def cancel_order(self, **params):
            self.calls += 1
            raise ConnectionError("connection reset by venue")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=True)
    client.retry_sleep_seconds = 0.0
    with pytest.raises(bybit.BybitDataError) as excinfo:
        client.cancel_order(symbol="BTCUSDT", order_link_id="lm-x")
    assert client._client.calls == client.retries  # transport: retried to exhaustion
    assert "connection reset by venue" in str(excinfo.value)


# ---------------------------------------------------------------------------
# audit2
# B2 bybit._is_rate_limit must classify on retCode/retMsg, not the whole payload.
# ---------------------------------------------------------------------------

def test_is_rate_limit_ignores_orderid_substring() -> None:
    # orderId contains "10006" but retCode is a definite reject -> NOT a rate limit.
    assert bybit._is_rate_limit({"retCode": 110001, "result": {"orderId": "a10006b"}}) is False


def test_is_rate_limit_true_on_retcode_10006() -> None:
    assert bybit._is_rate_limit({"retCode": 10006, "retMsg": "Too many visits"}) is True


def test_is_rate_limit_non_throttle_retmsg_not_matched_via_payload() -> None:
    # A risk/leverage reject whose retMsg legitimately contains "rate limit" must not
    # be misclassified when the code is a definite reject... but the retMsg text itself
    # is the venue's throttle signal, so a retMsg-level match is still honoured:
    assert bybit._is_rate_limit({"retCode": 110043, "retMsg": "set leverage not modified"}) is False


def test_is_rate_limit_string_fallback() -> None:
    assert bybit._is_rate_limit("rate limit exceeded") is True
    assert bybit._is_rate_limit("position closed") is False


# ---------------------------------------------------------------------------
# B3 WS trade client carries the demo-only submit guard (defense in depth).
# ---------------------------------------------------------------------------

class _FakeWsTrading:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

    def place_order(self, callback, **params):
        self.calls.append(("place", params))

    def cancel_order(self, callback, **params):
        self.calls.append(("cancel", params))


def test_ws_trade_client_blocks_real_money_submit_without_confirm(monkeypatch) -> None:
    monkeypatch.setattr(bybit, "WebSocketTrading", _FakeWsTrading)
    client = bybit.BybitWebSocketTradeClient(api_key="k", api_secret="s", demo=False)
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.place_order(object(), symbol="BTCUSDT", side="Buy", orderType="Market", qty="0.001", orderLinkId="x")
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.cancel_order(object(), symbol="BTCUSDT", order_link_id="x")
    assert client._client.calls == []  # nothing reached the venue


def test_ws_trade_client_allows_real_money_with_confirm(monkeypatch) -> None:
    monkeypatch.setattr(bybit, "WebSocketTrading", _FakeWsTrading)
    client = bybit.BybitWebSocketTradeClient(api_key="k", api_secret="s", demo=False, confirm_real_money=True)
    client.place_order(object(), symbol="BTCUSDT", side="Buy", orderType="Market", qty="0.001", orderLinkId="x")
    assert client._client.calls == [("place", {
        "category": "linear", "symbol": "BTCUSDT", "side": "Buy",
        "orderType": "Market", "qty": "0.001", "orderLinkId": "x",
    })]


def test_ws_trade_client_demo_path_unaffected(monkeypatch) -> None:
    monkeypatch.setattr(bybit, "WebSocketTrading", _FakeWsTrading)
    client = bybit.BybitWebSocketTradeClient(api_key="k", api_secret="s", demo=True)
    client.place_order(object(), symbol="BTCUSDT", side="Buy", orderType="Market", qty="0.001", orderLinkId="x")
    assert len(client._client.calls) == 1


# ==========================================================================
# Relocated from the audit bucket b01 (exec-router, ratelimit-rest,
# realmoney-safety, ws-pool findings).
# ==========================================================================


# --------------------------------------------------------------------------
# exec-router-2 / exec-router-5 : BybitPrivateClient.place_order idempotency
# --------------------------------------------------------------------------


def _make_private_client(monkeypatch, fake_http_cls) -> bybit.BybitPrivateClient:
    monkeypatch.setattr(bybit, "HTTP", fake_http_cls)
    return bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=True)


def test_place_order_duplicate_link_returns_existing_open_order(monkeypatch) -> None:
    """exec-router-2: a 110089 duplicate-orderLinkId reject must NOT raise; the
    order is already at Bybit under this idempotency key, so place_order probes
    by orderLinkId and returns the existing order. Pre-fix this raised
    BybitDataError -> the caller recorded an error and wrote no ledger row while
    the position was live (an orphan)."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {"orderId": "live-1", "orderLinkId": "agc-1", "orderStatus": "New"}
                    ],
                    "nextPageCursor": "",
                },
            }

    client = _make_private_client(monkeypatch, FakeHTTP)
    result = client.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="agc-1",
    )
    assert result["orderId"] == "live-1"
    assert result["orderLinkId"] == "agc-1"


def test_place_order_duplicate_link_raises_when_order_not_findable(monkeypatch) -> None:
    """exec-router-2: if Bybit reports a duplicate but the order cannot be found
    on either open-orders or history, surface the original reject rather than
    silently swallowing it (returning a phantom success would be worse)."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}

        def get_order_history(self, **_params):
            return {"retCode": 0, "result": {"list": []}}

    client = _make_private_client(monkeypatch, FakeHTTP)
    with pytest.raises(bybit.BybitDataError):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="agc-x",
        )


def test_place_order_non_duplicate_reject_still_raises(monkeypatch) -> None:
    """exec-router-2: only 110089 is treated as idempotent success; any other
    non-zero retCode must still raise so genuine rejects are not masked."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110007, "retMsg": "insufficient balance", "result": {}}

    client = _make_private_client(monkeypatch, FakeHTTP)
    with pytest.raises(bybit.BybitDataError, match="110007"):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="agc-y",
        )


def test_place_order_duplicate_link_uses_history_only_for_active_status(monkeypatch) -> None:
    """exec-router-2: a Rejected/Cancelled history row does NOT count as present
    (the submit did not take), so the dup-link path must fall through to raise
    rather than returning a dead order."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}

        def get_order_history(self, **_params):
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {"orderId": "dead", "orderLinkId": "agc-z", "orderStatus": "Rejected"}
                    ]
                },
            }

    client = _make_private_client(monkeypatch, FakeHTTP)
    with pytest.raises(bybit.BybitDataError):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="agc-z",
        )


def test_place_order_duplicate_link_ignores_wrong_history_link(monkeypatch) -> None:
    """A history row is usable only when its orderLinkId matches the requested link."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 110089, "retMsg": "orderLinkID exists", "result": {}}

        def get_open_orders(self, **_params):
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}

        def get_order_history(self, **_params):
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {"orderId": "other", "orderLinkId": "agc-other", "orderStatus": "Filled"}
                    ]
                },
            }

    client = _make_private_client(monkeypatch, FakeHTTP)
    with pytest.raises(bybit.BybitDataError):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="agc-z",
        )


def test_is_duplicate_order_link_matches_code_and_message() -> None:
    """exec-router-2: classify by retCode 110089 AND by message text so a
    re-worded retMsg still resolves as a duplicate."""
    assert bybit._is_duplicate_order_link("Bybit place_order failed: {'retCode': 110089}")
    assert bybit._is_duplicate_order_link("orderLinkID exists, duplicate")
    assert not bybit._is_duplicate_order_link("retCode 110007 insufficient balance")


def test_safe_int_degrades_on_malformed_timestamp() -> None:
    """exec-router-5: the probe's row selection (and any createdTime parse) must
    not raise on a non-numeric venue timestamp. _safe_int returns 0 instead of
    propagating a ValueError that would turn a recoverable fallback into a hard
    place_order crash."""
    assert bybit._safe_int("1700000000000") == 1700000000000
    assert bybit._safe_int("not-a-number") == 0
    assert bybit._safe_int(None) == 0
    assert bybit._safe_int({"createdTime": "x"}) == 0


# --------------------------------------------------------------------------
# ratelimit-rest-2 : shared BybitMarketData counters are lock-guarded
# --------------------------------------------------------------------------


def test_market_data_counters_no_lost_update_under_threads(monkeypatch) -> None:
    """ratelimit-rest-2: the bootstrap pool shares ONE BybitMarketData across 16
    threads. Concurrent _get / _record_call must not lose counter increments.
    Pre-fix the unlocked read-modify-write dropped increments; the lock makes
    logical_calls/http_calls/total_call_ms exact."""

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet

        def get_tickers(self, **_kwargs):
            return {"retCode": 0, "result": {"list": []}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    market = bybit.BybitMarketData()

    workers = 16
    per_worker = 50
    barrier = threading.Barrier(workers)

    def hammer() -> None:
        barrier.wait()  # release all threads together to maximise contention
        for _ in range(per_worker):
            market.get_tickers()

    threads = [threading.Thread(target=hammer) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = market.stats()
    assert stats["logical_calls"] == workers * per_worker
    assert stats["http_calls"] == workers * per_worker


# --------------------------------------------------------------------------
# ratelimit-rest-3 : get_instruments_info bounds its cursor walk
# --------------------------------------------------------------------------


def test_get_instruments_info_bounds_non_advancing_cursor(monkeypatch) -> None:
    """ratelimit-rest-3: a stable, non-empty nextPageCursor must NOT loop
    forever. The walk breaks on a non-advancing cursor. Pre-fix the unbounded
    `while True` hung whatever thread called it."""

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls = 0

        def get_instruments_info(self, **_params):
            self.calls += 1
            # Always returns the SAME non-empty cursor -> would loop forever.
            return {
                "retCode": 0,
                "result": {"list": [{"symbol": f"S{self.calls}"}], "nextPageCursor": "STUCK"},
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    market = bybit.BybitMarketData()
    rows = market.get_instruments_info()
    # A stable cursor is detected as non-advancing on the SECOND fetch (the new
    # cursor equals the previous), so the walk stops at 2 calls rather than
    # looping forever. Pre-fix this `while True` never terminated.
    assert market._client.calls == 2
    assert len(rows) == 2


def test_get_instruments_info_caps_at_max_pages(monkeypatch) -> None:
    """ratelimit-rest-3: even with an always-advancing cursor the walk is capped
    at max_pages so a pathological venue response cannot spin unbounded."""

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls = 0

        def get_instruments_info(self, **_params):
            self.calls += 1
            return {
                "retCode": 0,
                "result": {
                    "list": [{"symbol": f"S{self.calls}"}],
                    "nextPageCursor": f"cursor-{self.calls}",  # always advances
                },
            }

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    market = bybit.BybitMarketData()
    rows = market.get_instruments_info(max_pages=3)
    assert market._client.calls == 3
    assert len(rows) == 3


# --------------------------------------------------------------------------
# ratelimit-rest-5 : throttle counted once + no busy-spin at window edge
# --------------------------------------------------------------------------


def test_rate_limiter_counts_throttle_once_per_blocked_acquire(monkeypatch) -> None:
    """ratelimit-rest-5: a single blocked acquire records exactly ONE
    throttle_event and accumulates the real slept time once. Pre-fix the
    per-loop counting inflated throttle_events/throttled_seconds when the loop
    re-evaluated still-full. We drive a deterministic clock + sleep so the
    re-loop is forced, proving the count stays at one."""
    clock = {"t": 1000.0}
    slept: list[float] = []

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        # Advance the clock by LESS than requested on the first wake so the loop
        # re-evaluates while still inside the window (the inflation trigger),
        # then fully on the second.
        if len(slept) == 1:
            clock["t"] += seconds / 2.0
        else:
            clock["t"] += seconds

    monkeypatch.setattr(bybit.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(bybit.time, "sleep", fake_sleep)

    limiter = bybit.BybitRestRateLimiter(max_requests=1, per_seconds=1.0)
    limiter.acquire()  # fills the single slot at t=1000
    limiter.acquire()  # blocks; first wake is early, re-loops, second wake claims

    stats = limiter.stats()
    # The block is ONE logical throttle even though the loop slept twice.
    assert stats["throttle_events"] == 1, stats
    assert stats["throttled_seconds"] > 0.0
    # No busy-spin: every wait the loop computed was a real sleep, not a spin.
    assert all(s > 0.0 for s in slept), slept


def test_rate_limiter_no_busy_spin_at_window_boundary(monkeypatch) -> None:
    """ratelimit-rest-5: an oldest slot exactly AT the window cutoff is popped
    (<= cutoff), so the limiter never enters the wait<=0 continue-loop that
    busy-spun until the clock advanced. We hold the clock fixed at the boundary
    and require the acquire to complete (a spin would hang)."""
    clock = {"t": 500.0}

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_sleep(seconds: float) -> None:  # pragma: no cover - must not be reached
        raise AssertionError(f"unexpected sleep({seconds}); boundary slot should free immediately")

    monkeypatch.setattr(bybit.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(bybit.time, "sleep", fake_sleep)

    limiter = bybit.BybitRestRateLimiter(max_requests=1, per_seconds=1.0)
    limiter.acquire()  # slot at t=500
    # Advance the clock to EXACTLY one window later: the slot is at the cutoff.
    clock["t"] = 501.0
    # With the strict `<` pop this would compute wait==0 and continue-spin
    # forever (fake_sleep raises if ever called). With `<=` the slot frees and
    # the acquire returns immediately without sleeping.
    limiter.acquire()
    assert limiter.stats()["throttle_events"] == 0


# --------------------------------------------------------------------------
# realmoney-safety-1 : the signing client refuses real-money submits by default
# --------------------------------------------------------------------------


def test_private_client_refuses_real_money_submit_by_default(monkeypatch) -> None:
    """realmoney-safety-1: a demo=False (real-money) client must refuse to
    place/cancel/set-leverage unless confirm_real_money=True was threaded in at
    construction. Pre-fix there was no per-submit account assertion at the
    signing layer; the demo-only invariant lived only at config validation."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):  # pragma: no cover - must not be reached
            raise AssertionError("real-money submit should have been blocked")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=False)
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="rm-1",
        )
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.cancel_order(symbol="BTCUSDT", order_link_id="rm-1")


def test_private_client_real_money_reads_are_never_gated(monkeypatch) -> None:
    """realmoney-safety-1: the guard only blocks STATE-CHANGING submissions;
    read-only calls (get_*) on a real-money client must still work so reconcile
    / balance checks are unaffected."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_wallet_balance(self, **_params):
            return {"retCode": 0, "result": {"list": [{"coin": "USDT"}]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=False)
    balance = client.get_wallet_balance()
    assert balance["list"][0]["coin"] == "USDT"


def test_private_client_real_money_submit_allowed_with_explicit_optin(monkeypatch) -> None:
    """realmoney-safety-1: an explicit confirm_real_money=True opt-in lets a
    real-money client submit — the gate is a deliberate switch, not a hard wall,
    so a future validated real-money path can thread the opt-in through."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "rm-ok"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(
        api_key="k", api_secret="s", demo=False, confirm_real_money=True,
    )
    result = client.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="rm-2",
    )
    assert result["orderId"] == "rm-ok"


def test_private_client_demo_submit_unaffected(monkeypatch) -> None:
    """realmoney-safety-1: the guard is a no-op for demo clients (the only mode
    that runs today)."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "demo-ok"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitPrivateClient(api_key="k", api_secret="s", demo=True)
    result = client.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="demo-1",
    )
    assert result["orderId"] == "demo-ok"


# --------------------------------------------------------------------------
# realmoney-safety-3 : malformed REAL_MONEY toggle fails loud
# --------------------------------------------------------------------------


def test_resolve_credentials_rejects_ambiguous_real_money(monkeypatch) -> None:
    """realmoney-safety-3: a set-but-unrecognised REAL_MONEY value (e.g.
    'enabled') must raise at resolution rather than silently coercing to demo.
    Pre-fix the operator who typed REAL_MONEY=enabled got demo with no warning."""
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setenv("REAL_MONEY", "enabled")
    with pytest.raises(RuntimeError, match="not a recognised boolean"):
        bybit.resolve_private_credentials()


def test_resolve_credentials_accepts_recognised_falsey_values(monkeypatch) -> None:
    """realmoney-safety-3: explicit falsey values (false/0/off/empty) stay demo
    without raising — the fail-safe whitelist remains permissive."""
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.delenv("DEMO", raising=False)
    for falsey in ("false", "0", "off", "no", ""):
        monkeypatch.setenv("REAL_MONEY", falsey)
        assert bybit.resolve_private_credentials() == ("demo-k", "demo-s", True)


def test_resolve_credentials_logs_resolved_account(monkeypatch, caplog) -> None:
    """realmoney-safety-3: resolution emits a single INFO line naming the
    resolved account so 'which account did this process use' is auditable from
    the log. Pre-fix there was no resolved-account telemetry anywhere."""
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-k")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-s")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("REAL_MONEY", raising=False)
    with caplog.at_level("INFO", logger="liquidity_migration.bybit.account"):
        bybit.resolve_private_credentials()
    assert any("resolved account: demo" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# ws-pool-3 : ping-timer patch cancels priors; _close_ws_client cleans either
# --------------------------------------------------------------------------


def test_ping_timer_patch_cancels_prior_timer_on_reconnect(monkeypatch) -> None:
    """ws-pool-3: a reconnect re-invokes _send_initial_ping. The patched version
    must cancel the prior timer before installing a new one, so reconnects do
    not accumulate orphan daemon Timer threads. Pre-fix each reconnect overwrote
    _agc_ping_timer without cancelling it."""

    class FakeManager:
        ping_interval = 1000

        def _send_custom_ping(self):
            pass

    monkeypatch.setitem(
        sys.modules, "pybit._websocket_stream",
        SimpleNamespace(_V5WebSocketManager=FakeManager),
    )
    bybit._patch_pybit_daemon_ping_timer()
    manager = FakeManager()

    manager._send_initial_ping()
    first = manager._agc_ping_timer
    assert first.daemon is True
    # Mirror onto the stock attribute too, so pybit's own exit() can cancel it.
    assert manager.custom_ping_timer is first

    manager._send_initial_ping()  # simulate a reconnect
    second = manager._agc_ping_timer
    assert second is not first
    # The prior timer was cancelled (not orphaned): a cancelled Timer's thread
    # winds down promptly, so a short join completes. Pre-fix the prior timer was
    # overwritten WITHOUT cancel and would have run its full ping_interval.
    first.join(timeout=2.0)
    assert not first.is_alive()
    second.cancel()
    second.join(timeout=2.0)


def test_close_ws_client_cancels_stock_custom_ping_timer() -> None:
    """ws-pool-3: _close_ws_client must cancel the ping timer even when only the
    STOCK pybit attribute (custom_ping_timer) is set — so a pybit bump that
    stops calling our patched _send_initial_ping cannot silently turn the cancel
    into a no-op and reintroduce a shutdown-blocking timer thread."""
    cancelled = {"v": False}

    class FakeTimer:
        def cancel(self) -> None:
            cancelled["v"] = True

    class FakeClient:
        def __init__(self) -> None:
            # Only the stock attribute is present (no _agc_ping_timer).
            self.custom_ping_timer = FakeTimer()

        def exit(self) -> None:
            pass

    client = FakeClient()
    bybit._close_ws_client(client, timeout_seconds=1.0)
    assert cancelled["v"] is True


# --------------------------------------------------------------------------
# ws-pool-4 : a callback swap re-routes bars on already-subscribed connections
# --------------------------------------------------------------------------


class _SwapFakeWebSocket:
    def __init__(self) -> None:
        self.callback = None

    def kline_stream(self, *, interval, symbol, callback) -> None:
        del interval, symbol
        self.callback = callback

    def unsubscribe(self, *, topic: str) -> None:
        del topic

    def close(self) -> None:
        pass

    def inject_bar(self, symbol: str) -> None:
        assert self.callback is not None
        self.callback(
            {
                "topic": f"kline.60.{symbol}",
                "data": [{"start": 0, "confirm": True, "close": "1.0"}],
            }
        )


class _SwapFactory:
    def __init__(self) -> None:
        self.built: list[_SwapFakeWebSocket] = []

    def __call__(self, *, testnet: bool, demo: bool, channel_type: str) -> _SwapFakeWebSocket:
        del testnet, demo, channel_type
        ws = _SwapFakeWebSocket()
        self.built.append(ws)
        return ws


def test_callback_swap_reroutes_existing_connection() -> None:
    """ws-pool-4: after subscribe() replaces the callback, an ALREADY-subscribed
    connection must fire the NEW sink (the closure dereferences self._on_bar
    live, not at build time). Pre-fix the closure captured the old on_bar, so
    bars on the existing connection kept hitting the OLD sink — contradicting the
    documented 'replaces for every connection' guarantee."""
    factory = _SwapFactory()
    pool = BybitKlineStreamPool(
        interval_minutes=60,
        topics_per_connection=10,
        connection_spacing_seconds=0.0,
        websocket_factory=factory,
    )
    old_bars: list[str] = []
    new_bars: list[str] = []
    try:
        pool.subscribe(["BTCUSDT"], lambda s, b, c: old_bars.append(s))
        ws = factory.built[0]
        ws.inject_bar("BTCUSDT")
        assert old_bars == ["BTCUSDT"]

        # Swap the callback (same symbol set -> no new connection built).
        pool.subscribe(["BTCUSDT"], lambda s, b, c: new_bars.append(s))
        assert len(factory.built) == 1  # no new connection
        ws.inject_bar("BTCUSDT")  # fire on the SAME existing connection
        # The new sink got the bar; the old sink did not see a second one.
        assert new_bars == ["BTCUSDT"]
        assert old_bars == ["BTCUSDT"]
    finally:
        pool.close()


# ---------------------------------------------------------------------------
# _paged_time_range mid-pagination empty-page guard (relocated from audit
# bucket iD). An empty mid-range page must NOT silently truncate a
# ``_paged_time_range`` fetch: the Bybit funding-rate / open-interest paths walk
# ``endTime`` backwards; if the provider returns an empty list *after* a full
# page (a transient hiccup that returns ``[]`` instead of raising), the old code
# ``break``-ed and returned only the rows fetched before the hole. Downstream,
# ``_download_symbol_dataset`` would see ``frame.height > 0`` and write the
# FULL-requested-range completeness marker, producing a permanent silent gap in
# funding / open_interest that ``_marked_complete`` never re-fetches. The fix
# raises ``BybitDataError`` on a mid-range empty page so the symbol-range is
# retried rather than marked complete, while a *first-page* empty (genuine
# "no data in range") still returns cleanly.
# ---------------------------------------------------------------------------


def _make_market_data(monkeypatch, responses_by_end_time):
    """Build a BybitMarketData whose FakeHTTP serves canned funding/OI pages.

    ``responses_by_end_time`` maps the per-request ``endTime`` (the backwards
    pagination cursor) to the ``result.list`` payload to return for that call.
    """

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls: list[dict] = []

        def _serve(self, **params):
            self.calls.append(params)
            end_time = int(params["endTime"])
            return {"retCode": 0, "result": {"list": responses_by_end_time[end_time]}}

        # Both funding-history and open-interest route through _paged_time_range.
        def get_funding_rate_history(self, **params):
            return self._serve(**params)

        def get_open_interest(self, **params):
            return self._serve(**params)

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    # retries=1 keeps the test fast and deterministic; the guard fires before
    # any retry/backoff because BybitDataError(non-rate-limit) raises immediately.
    return bybit.BybitMarketData(testnet=True, retries=1, retry_sleep_seconds=0.0)


def _funding_row(ts: int) -> dict[str, str]:
    return {"fundingRateTimestamp": str(ts), "fundingRate": "0.0001"}


def test_mid_range_empty_page_raises_instead_of_truncating(monkeypatch) -> None:
    """A full page followed by an empty page mid-range must raise, not truncate.

    limit=2, range [0, 10]. First request (endTime=10) returns a FULL page
    (ts 9, 8) so pagination continues with endTime = 8 - 1 = 7. The second
    request (endTime=7) returns [] -- a transient hole. Old behaviour: break and
    return only [8, 9]. New behaviour: raise BybitDataError so the fetch fails
    and the downloader retries rather than writing a full-range marker.
    """
    responses = {
        10: [_funding_row(9), _funding_row(8)],  # full page -> keep paginating
        7: [],  # transient empty mid-range -> must NOT silently truncate
    }
    client = _make_market_data(monkeypatch, responses)

    with pytest.raises(bybit.BybitDataError) as excinfo:
        client.get_funding_history("BTCUSDT", start=0, end=10, limit=2)

    assert "mid-range" in str(excinfo.value)
    # Both pages were actually requested before the guard fired.
    assert len(client._client.calls) == 2
    assert int(client._client.calls[1]["endTime"]) == 7


def test_first_page_empty_returns_cleanly_no_data_in_range(monkeypatch) -> None:
    """A genuinely empty range (first page empty) returns [] without raising."""
    responses = {10: []}
    client = _make_market_data(monkeypatch, responses)

    rows = client.get_funding_history("BTCUSDT", start=0, end=10, limit=2)

    assert rows == []
    # Only one request was made; no spurious retry/extra pagination.
    assert len(client._client.calls) == 1


def test_full_then_short_page_completes_normally(monkeypatch) -> None:
    """The happy multi-page path is unchanged: a full page then a short
    (< limit) page terminates cleanly and returns the union, ascending by ts."""
    responses = {
        10: [_funding_row(9), _funding_row(8)],  # full page (len == limit)
        7: [_funding_row(5)],  # short page (len < limit) -> natural end of data
    }
    client = _make_market_data(monkeypatch, responses)

    rows = client.get_funding_history("BTCUSDT", start=0, end=10, limit=2)

    assert [int(r["fundingRateTimestamp"]) for r in rows] == [5, 8, 9]
    assert len(client._client.calls) == 2


def test_open_interest_mid_range_empty_also_guarded(monkeypatch) -> None:
    """open_interest shares _paged_time_range, so the same guard must apply.

    Its timestamp key is "timestamp"; reproduce the full-then-empty hole.
    """

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls: list[dict] = []

        def get_open_interest(self, **params):
            self.calls.append(params)
            end_time = int(params["endTime"])
            pages = {
                10: [
                    {"timestamp": "9", "openInterest": "1"},
                    {"timestamp": "8", "openInterest": "2"},
                ],
                7: [],  # transient mid-range empty
            }
            return {"retCode": 0, "result": {"list": pages[end_time]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitMarketData(testnet=True, retries=1, retry_sleep_seconds=0.0)

    with pytest.raises(bybit.BybitDataError):
        client.get_open_interest("BTCUSDT", "5min", start=0, end=10, limit=2)
