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
