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


def test_price_index_kline_downloads_use_deduped_time_windows(monkeypatch) -> None:
    interval_ms = bybit.INTERVAL_MS["60"]
    timestamps = [index * interval_ms for index in range(6)]

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.mark_calls = []
            self.index_calls = []
            self.premium_calls = []

        def get_mark_price_kline(self, **params):
            self.mark_calls.append(params)
            return _kline_page(timestamps, params)

        def get_index_price_kline(self, **params):
            self.index_calls.append(params)
            return _kline_page(timestamps, params)

        def get_premium_index_price_kline(self, **params):
            self.premium_calls.append(params)
            return _kline_page(timestamps, params)

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitMarketData()
    mark = client.get_mark_price_klines("BTCUSDT", "60", timestamps[0], timestamps[-1], limit=3)
    index = client.get_index_price_klines("BTCUSDT", "60", timestamps[0], timestamps[-1], limit=3)
    premium = client.get_premium_index_klines("BTCUSDT", "60", timestamps[0], timestamps[-1], limit=3)

    assert [int(row[0]) for row in mark] == timestamps
    assert [int(row[0]) for row in index] == timestamps
    assert [int(row[0]) for row in premium] == timestamps
    assert len(client._client.mark_calls) > 1
    assert len(client._client.index_calls) > 1
    assert len(client._client.premium_calls) > 1


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


def _kline_page(timestamps: list[int], params: dict) -> dict:
    start = int(params["start"])
    end = int(params["end"])
    limit = int(params["limit"])
    rows = [[str(ts), "1.0", "1.1", "0.9", "1.05"] for ts in timestamps if start <= ts <= end]
    return {"retCode": 0, "result": {"list": list(reversed(rows))[:limit]}}
