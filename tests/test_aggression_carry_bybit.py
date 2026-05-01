from __future__ import annotations

from aggression_carry import bybit


def test_bybit_market_data_constructs_with_slotted_client(monkeypatch) -> None:
    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)

    client = bybit.BybitMarketData(testnet=True)

    assert client._client.testnet is True
