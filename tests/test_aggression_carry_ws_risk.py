from __future__ import annotations

from pathlib import Path

import polars as pl

from aggression_carry import ws_risk
from aggression_carry.config import ResearchConfig
from aggression_carry.storage import read_dataset, write_dataset
from aggression_carry.ws_risk import EventWebSocketRiskConfig, EventWebSocketRiskEngine


class FakePrivateClient:
    def __init__(self) -> None:
        self.positions = [
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "avgPrice": "100",
                "markPrice": "100",
                "positionValue": "100",
                "unrealisedPnl": "0",
                "stopLoss": "112",
                "takeProfit": "80",
            }
        ]
        self.orders: list[dict[str, object]] = []

    def get_positions(self, *, settle_coin: str | None = None):
        return self.positions

    def place_order(self, **params):
        self.orders.append(params)
        return {"orderId": "rest-order-1"}

    def get_trade_history(self, *, symbol: str | None = None, order_link_id: str | None = None, limit: int = 50):
        return [{"orderLinkId": order_link_id, "execQty": "1", "execPrice": "113", "execValue": "113", "execFee": "0.01"}]


class FakePrivateStream:
    def __init__(self) -> None:
        self.subscriptions: list[str] = []

    def subscribe_positions(self, callback):
        self.subscriptions.append("position")

    def subscribe_orders(self, callback):
        self.subscriptions.append("order")

    def subscribe_executions(self, callback, *, fast: bool = False):
        self.subscriptions.append("fast_execution" if fast else "execution")

    def close(self):
        pass


class FakePublicStream:
    def __init__(self) -> None:
        self.symbols: list[str] = []

    def subscribe_tickers(self, symbols, callback):
        self.symbols.extend(symbols if isinstance(symbols, list) else [symbols])

    def close(self):
        pass


class FakeTradeClient:
    def __init__(self) -> None:
        self.orders: list[dict[str, object]] = []

    def place_order(self, callback, **params):
        self.orders.append(params)

    def close(self):
        pass


def test_ws_risk_triggers_rest_fallback_exit_from_ticker(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert private_client.orders[0]["reduceOnly"] is True
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert engine.state.exits[0]["exit_reason"] == "stop_loss"


def test_ws_then_rest_records_demo_trade_socket_limit_and_uses_rest(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="ws_then_rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})

    assert "demo WebSocket Trade order entry is unavailable" in engine.state.ws_order_unavailable
    assert engine.trade_client is None
    assert private_client.orders[0]["orderType"] == "Market"


def test_ws_risk_uses_mainnet_public_ticker_stream_for_demo_market_data(tmp_path: Path, monkeypatch) -> None:
    _write_open_trade(tmp_path)
    constructed: dict[str, object] = {}

    class RecordingPublicStream(FakePublicStream):
        def __init__(self, **kwargs):
            super().__init__()
            constructed.update(kwargs)

    monkeypatch.setattr(ws_risk, "BybitPublicTickerStream", RecordingPublicStream)
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            repair_stops=False,
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
        ),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
    )

    engine.bootstrap()

    assert constructed["demo"] is False
    assert constructed["category"] == "linear"
    assert constructed["testnet"] is False


def test_ws_risk_ws_order_closes_from_execution_stream(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    trade_client = FakeTradeClient()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="ws",
            rest_fallback=False,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
        ),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
        trade_client=trade_client,
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})
    link = str(engine.state.orders[0]["order_link_id"])
    engine.on_execution_message(
        {"data": [{"symbol": "AAAUSDT", "orderLinkId": link, "execQty": "1", "execPrice": "113", "execValue": "113"}]}
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert trade_client.orders[0]["reduceOnly"] is True
    assert engine.state.orders[0]["submit_mode"] == "ws_submitted"
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"


def test_ws_risk_stale_stream_forces_rest_reconcile(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            stale_ws_seconds=0.1,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    private_client.positions[0]["markPrice"] = "113"
    engine.state.last_ws_event_monotonic -= 1.0
    engine.on_idle()

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert any("websocket stale" in error for error in engine.state.errors)
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"


def _write_open_trade(root: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "t1",
                    "symbol": "AAAUSDT",
                    "side": "short",
                    "status": "open",
                    "qty": "1",
                    "entry_price": 100.0,
                    "stop_price": 112.0,
                    "take_profit_price": 80.0,
                    "planned_exit_ts_ms": 9_999_999_999_999,
                }
            ]
        ),
        root,
        "event_demo_trades",
        partition_by=(),
    )
