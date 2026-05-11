from __future__ import annotations

from aggression_carry import bybit


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


def test_bybit_private_client_refuses_non_demo_session(monkeypatch) -> None:
    constructed = False

    class FakeHTTP:
        def __init__(self, **kwargs):
            nonlocal constructed
            constructed = True
            self.kwargs = kwargs

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    try:
        bybit.BybitPrivateClient(api_key="key", api_secret="secret", demo=False)
    except RuntimeError as exc:
        assert "demo-only" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("private client should fail closed outside demo mode")

    assert constructed is False


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
    orders = client.get_order_history(symbol="BTCUSDT", order_link_id="agc-link")
    trades = client.get_trade_history(symbol="BTCUSDT", order_link_id="agc-link")

    assert orders[0]["orderStatus"] == "Filled"
    assert trades[0]["execQty"] == "1"
    assert client._client.order_history_calls == [
        {"category": "linear", "limit": 50, "symbol": "BTCUSDT", "orderLinkId": "agc-link"}
    ]
    assert client._client.execution_calls == [
        {"category": "linear", "limit": 50, "symbol": "BTCUSDT", "orderLinkId": "agc-link"}
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
    assert client._client.position_calls == [{"category": "linear", "settleCoin": "USDT"}]


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
    assert client._client.open_order_calls == [{"category": "linear", "settleCoin": "USDT"}]


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
            "trailingStop": "2.5",
            "activePrice": "95",
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
