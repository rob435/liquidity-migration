from __future__ import annotations

from typing import Any

import pytest

import liquidity_migration.venue.bybit as bybit
from liquidity_migration.core.venue_realm import VenueRealm


class FakeHttp:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.position_pages = [
            {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}], "nextPageCursor": "two"}},
            {"retCode": 0, "result": {"list": [{"symbol": "ETHUSDT"}], "nextPageCursor": ""}},
        ]

    def get_positions(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("get_positions", params))
        return self.position_pages.pop(0)

    def get_wallet_balance(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("get_wallet_balance", params))
        return {"retCode": 0, "result": {"list": [{"totalEquity": "100"}]}}

    def get_transaction_log(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("get_transaction_log", params))
        return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}


def reader(monkeypatch: pytest.MonkeyPatch) -> bybit.BybitAccountReader:
    monkeypatch.delenv("REAL_MONEY", raising=False)
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setattr(bybit, "HTTP", FakeHttp)
    return bybit.BybitAccountReader(api_key="key", api_secret="secret", realm=VenueRealm.DEMO)


def test_reader_exposes_no_order_mutation_api(monkeypatch: pytest.MonkeyPatch) -> None:
    client = reader(monkeypatch)
    for name in ("place_order", "place_orders_batch", "amend_order", "cancel_order", "set_leverage", "set_trading_stop"):
        assert not hasattr(client, name), name
    with pytest.raises(RuntimeError, match="refuses mutating method"):
        client._call("place_order", symbol="BTCUSDT")


def test_cursor_reads_every_page_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client = reader(monkeypatch)
    rows = client.get_positions(settle_coin="USDT")
    assert [row["symbol"] for row in rows] == ["BTCUSDT", "ETHUSDT"]
    assert client._client.calls == [
        ("get_positions", {"category": "linear", "limit": 200, "settleCoin": "USDT"}),
        (
            "get_positions",
            {"category": "linear", "limit": 200, "settleCoin": "USDT", "cursor": "two"},
        ),
    ]


def test_cursor_rejects_a_non_object_result(monkeypatch: pytest.MonkeyPatch) -> None:
    client = reader(monkeypatch)
    client._client.position_pages = [{"retCode": 0, "result": None}]

    with pytest.raises(bybit.BybitDataError, match="invalid result object"):
        client.get_positions(settle_coin="USDT")


def test_wallet_read_preserves_result(monkeypatch: pytest.MonkeyPatch) -> None:
    client = reader(monkeypatch)
    assert client.get_wallet_balance() == {"list": [{"totalEquity": "100"}]}


def test_transaction_window_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = reader(monkeypatch)
    with pytest.raises(ValueError, match="at most seven days"):
        client.get_account_transactions(
            transaction_type="TRADE",
            start_time_ms=1,
            end_time_ms=8 * 24 * 60 * 60 * 1000,
        )


def test_demo_reader_rejects_mainnet_transport_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REAL_MONEY", raising=False)
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setattr(bybit, "HTTP", FakeHttp)
    with pytest.raises(RuntimeError, match="contradicts"):
        bybit.BybitAccountReader(api_key="key", api_secret="secret", realm="demo", demo=False)
