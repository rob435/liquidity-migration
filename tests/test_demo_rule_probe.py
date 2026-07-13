from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.demo_rule_probe import probe_demo_instrument_rule


REPO_ROOT = Path(__file__).resolve().parents[1]


def _instrument() -> dict[str, Any]:
    return {
        "symbol": "BUSDT",
        "lotSizeFilter": {
            "qtyStep": "0.1",
            "minOrderQty": "0.1",
            "minNotionalValue": "1",
            "maxMktOrderQty": "100000",
        },
        "priceFilter": {"tickSize": "0.1"},
        "leverageFilter": {"maxLeverage": "25"},
    }


class _ProbeClient:
    def __init__(self, *, threshold: float = 5.0, unknown_failure: bool = False) -> None:
        self.threshold = threshold
        self.unknown_failure = unknown_failure
        self.accepted: list[str] = []
        self.cancelled: list[str] = []
        self.leverage: list[tuple[str, float, float]] = []

    def set_leverage(self, *, symbol: str, buy_leverage: float, sell_leverage: float) -> None:
        self.leverage.append((symbol, buy_leverage, sell_leverage))

    def place_order(self, **params: Any) -> dict[str, str]:
        if self.unknown_failure:
            raise RuntimeError("ErrCode: 10006 rate limit")
        if float(params["qty"]) * float(params["price"]) + 1e-12 < self.threshold:
            raise RuntimeError("Order notional value below the lower limit (ErrCode: 110094)")
        link = str(params["orderLinkId"])
        self.accepted.append(link)
        return {"orderId": link}

    def cancel_order(self, *, symbol: str, order_link_id: str) -> None:
        assert symbol == "BUSDT"
        self.cancelled.append(order_link_id)


def test_probe_finds_smallest_accepted_demo_qty_step() -> None:
    client = _ProbeClient(threshold=5.0)

    rule, evidence = probe_demo_instrument_rule(
        client,
        instrument_row=_instrument(),
        ticker_row={"symbol": "BUSDT", "bid1Price": "10.1"},
        observed_ts_ns=123456789,
        max_probe_notional_usdt=20.0,
        leverage=10.0,
    )

    assert rule.environment == "demo"
    assert rule.source == "bybit_demo_post_only_acceptance_probe"
    assert rule.qty_step == 0.1
    assert rule.min_qty == 0.1
    assert rule.min_notional == pytest.approx(5.0)
    assert evidence.probe_price == 10.0
    assert evidence.lowest_accepted_qty == pytest.approx(0.5)
    assert evidence.highest_rejected_qty == pytest.approx(0.4)
    assert client.cancelled == client.accepted
    assert client.leverage == [("BUSDT", 10.0, 10.0)]


def test_probe_does_not_misclassify_transport_or_rate_failure_as_minimum() -> None:
    with pytest.raises(RuntimeError, match="non-threshold probe failure"):
        probe_demo_instrument_rule(
            _ProbeClient(unknown_failure=True),
            instrument_row=_instrument(),
            ticker_row={"symbol": "BUSDT", "bid1Price": "10.1"},
            observed_ts_ns=123456789,
            max_probe_notional_usdt=20.0,
        )


def test_probe_fails_when_explicit_cap_cannot_reach_demo_minimum() -> None:
    with pytest.raises(RuntimeError, match="no accepted order"):
        probe_demo_instrument_rule(
            _ProbeClient(threshold=50.0),
            instrument_row=_instrument(),
            ticker_row={"symbol": "BUSDT", "bid1Price": "10.1"},
            observed_ts_ns=123456789,
            max_probe_notional_usdt=20.0,
        )


def test_probe_cli_checks_explicit_conditional_order_view() -> None:
    text = (REPO_ROOT / "scripts" / "probe_bybit_demo_rules.py").read_text()

    assert 'client.get_open_orders(settle_coin="USDT")' in text
    assert 'order_filter="StopOrder"' in text
    assert "_open_orders_all_kinds(client)" in text
    assert '"artifact_sha256": ""' in text
    assert "os.fsync" in text
