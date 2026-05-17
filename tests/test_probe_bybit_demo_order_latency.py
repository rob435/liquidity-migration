from __future__ import annotations

import pytest

from scripts import probe_bybit_demo_order_latency as probe


def test_demo_order_latency_probe_cancels_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMarket:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_instruments_info(self):
            return [
                {
                    "symbol": "BTCUSDT",
                    "lotSizeFilter": {"minOrderQty": "0.001", "qtyStep": "0.001"},
                    "priceFilter": {"tickSize": "0.1"},
                }
            ]

        def get_tickers(self):
            return [{"symbol": "BTCUSDT", "markPrice": "100000"}]

    class FakePrivate:
        instances: list["FakePrivate"] = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.open_links: set[str] = set()
            self.cancel_calls: list[str] = []
            FakePrivate.instances.append(self)

        def place_order(self, **params):
            link = str(params["orderLinkId"])
            self.open_links.add(link)
            return {"orderId": "probe-order-1"}

        def cancel_order(self, *, symbol: str, order_link_id: str):
            del symbol
            self.cancel_calls.append(order_link_id)
            self.open_links.discard(order_link_id)
            return {}

        def get_open_orders(self, *, symbol: str):
            del symbol
            return [{"orderLinkId": link} for link in sorted(self.open_links)]

    monkeypatch.setenv("CONFIRM_DEMO_ORDERS", "1")
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "secret")
    monkeypatch.setenv("PROBE_COUNT", "1")
    monkeypatch.setenv("PROBE_CANCEL_VERIFY_SECONDS", "0")
    monkeypatch.setattr(probe, "BybitMarketData", FakeMarket)
    monkeypatch.setattr(probe, "BybitPrivateClient", FakePrivate)

    assert probe.main() == 0

    private = FakePrivate.instances[0]
    assert private.cancel_calls
    assert private.open_links == set()


def test_demo_order_latency_probe_raises_if_cancel_verification_fails() -> None:
    class StubbornPrivate:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel_order(self, *, symbol: str, order_link_id: str):
            del symbol, order_link_id
            self.cancel_calls += 1
            return {}

        def get_open_orders(self, *, symbol: str):
            del symbol
            return [{"orderLinkId": "agc-probe-stuck"}]

    private = StubbornPrivate()

    with pytest.raises(RuntimeError, match="still open"):
        probe._verify_cancelled(
            private,
            symbol="BTCUSDT",
            order_link_id="agc-probe-stuck",
            timeout_seconds=0.0,
        )

    assert private.cancel_calls == 1
