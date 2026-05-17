from __future__ import annotations

import time
from pathlib import Path

import polars as pl

from aggression_carry import ws_risk
from aggression_carry.config import ResearchConfig
from aggression_carry.storage import read_dataset, write_dataset
from aggression_carry.ws_risk import (
    EventWebSocketRiskConfig,
    EventWebSocketRiskEngine,
    _read_telegram_dedupe_keys,
)


class FakePrivateClient:
    def __init__(
        self,
        *,
        confirm_fills: bool = True,
        fail_trade_history: bool = False,
        open_orders: list[dict[str, object]] | None = None,
        fail_open_orders: bool = False,
    ) -> None:
        self.confirm_fills = confirm_fills
        self.fail_trade_history = fail_trade_history
        self.open_orders = open_orders or []
        self.fail_open_orders = fail_open_orders
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

    def get_open_orders(self, *, symbol: str | None = None, settle_coin: str | None = None):
        if self.fail_open_orders:
            raise RuntimeError("open orders unavailable")
        if symbol:
            return [row for row in self.open_orders if str(row.get("symbol") or "") == symbol]
        return self.open_orders

    def place_order(self, **params):
        self.orders.append(params)
        return {"orderId": "rest-order-1"}

    def get_trade_history(self, *, symbol: str | None = None, order_link_id: str | None = None, limit: int = 50):
        if self.fail_trade_history:
            raise RuntimeError("history unavailable")
        if not self.confirm_fills:
            return []
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


class BlockingPrivateStream(FakePrivateStream):
    def subscribe_positions(self, callback):
        time.sleep(10)


class BlockingPublicStream(FakePublicStream):
    def subscribe_tickers(self, symbols, callback):
        time.sleep(10)


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
    assert "AAAUSDT" not in engine.state.submitted_symbols


def test_ws_risk_live_open_exit_order_blocks_duplicate_tracked_exit(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient(
        open_orders=[
            {
                "symbol": "AAAUSDT",
                "orderLinkId": "agc-ex-existing",
                "orderStatus": "New",
                "reduceOnly": True,
            }
        ]
    )
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
    payload = engine.write_report(reason="heartbeat")

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert private_client.orders == []
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "open"
    assert engine.state.live_exit_order_symbols == {"AAAUSDT"}
    assert payload["cycle"]["bybit_live_exit_open_orders"] == 1


def test_ws_risk_manual_reduce_only_order_does_not_block_emergency_exit(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient(
        open_orders=[
            {
                "symbol": "AAAUSDT",
                "orderLinkId": "manual-reduce",
                "orderStatus": "New",
                "reduceOnly": True,
            }
        ]
    )
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
    assert len(private_client.orders) == 1
    assert private_client.orders[0]["reduceOnly"] is True
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"


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


def test_ws_risk_run_does_not_hang_when_private_stream_subscription_blocks(tmp_path: Path) -> None:
    private_client = FakePrivateClient(confirm_fills=False)
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
            max_runtime_seconds=0.05,
            stream_start_timeout_seconds=0.01,
            exit_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=BlockingPrivateStream(),
        public_stream=FakePublicStream(),
    )

    started = time.monotonic()
    payload = engine.run()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert payload["cycle"]["reason"] == "max_runtime"
    assert "private websocket subscriptions timed out" in payload["cycle"]["position_report_error"]


def test_ws_risk_public_ticker_subscription_timeout_does_not_block_bootstrap(tmp_path: Path) -> None:
    private_client = FakePrivateClient(confirm_fills=False)
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
            stream_start_timeout_seconds=0.01,
            exit_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=BlockingPublicStream(),
    )

    started = time.monotonic()
    engine.bootstrap()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert "AAAUSDT" not in engine.state.subscribed_symbols
    assert any("public ticker subscription AAAUSDT timed out" in error for error in engine.state.errors)
    engine.public_stream = FakePublicStream()
    engine.subscribe_tickers({"AAAUSDT"})
    assert "AAAUSDT" in engine.state.subscribed_symbols


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
            exit_untracked_positions=False,
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
    assert engine.state.orders[0]["status"] == "filled"
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"


def test_ws_risk_rest_fallback_order_closes_from_execution_stream(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient(confirm_fills=False)
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
    link = str(engine.state.orders[0]["order_link_id"])
    engine.on_execution_message(
        {"data": [{"symbol": "AAAUSDT", "orderLinkId": link, "execQty": "1", "execPrice": "113", "execValue": "113"}]}
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert engine.state.orders[0]["status"] == "filled"
    assert engine.state.exits[0]["submit_mode"] == "submitted"
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"


def test_ws_risk_order_stream_fill_closes_trade_when_execution_lags(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient(confirm_fills=False)
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
    link = str(engine.state.orders[0]["order_link_id"])
    engine.on_order_message(
        {
            "data": [
                {
                    "symbol": "AAAUSDT",
                    "orderLinkId": link,
                    "orderStatus": "Filled",
                    "cumExecQty": "1",
                    "avgPrice": "113",
                }
            ]
        }
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert engine.state.orders[0]["status"] == "filled"
    assert engine.state.exits[0]["submit_mode"] == "submitted"
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert stored_orders.filter(pl.col("order_link_id") == link).select("status").item() == "filled"
    assert "AAAUSDT" not in engine.state.submitted_symbols


def test_ws_risk_bootstrap_loads_pending_exit_order_after_restart(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-ex-pending",
                    "ts_ms": 9_999_999_999_000,
                    "trade_id": "t1",
                    "symbol": "AAAUSDT",
                    "side": "Buy",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": True,
                    "submit_mode": "submitted",
                    "status": "submitted_unconfirmed",
                    "exit_reason": "stop_loss",
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )
    private_client = FakePrivateClient(confirm_fills=False)
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
    assert "AAAUSDT" in engine.state.submitted_symbols
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})
    engine.on_execution_message(
        {
            "data": [
                {
                    "symbol": "AAAUSDT",
                    "orderLinkId": "agc-ex-pending",
                    "execQty": "1",
                    "execPrice": "113",
                    "execValue": "113",
                }
            ]
        }
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert private_client.orders == []
    assert engine.state.exits[0]["submit_mode"] == "submitted"
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert stored_orders.filter(pl.col("order_link_id") == "agc-ex-pending").select("status").item() == "filled"


def test_ws_risk_rejected_pending_exit_unblocks_retry_after_restart(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-ex-pending",
                    "ts_ms": 9_999_999_999_000,
                    "trade_id": "t1",
                    "symbol": "AAAUSDT",
                    "side": "Buy",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": True,
                    "submit_mode": "submitted",
                    "status": "submitted_unconfirmed",
                    "exit_reason": "stop_loss",
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )
    private_client = FakePrivateClient(confirm_fills=False)
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
    engine.on_order_message(
        {
            "data": [
                {
                    "symbol": "AAAUSDT",
                    "orderLinkId": "agc-ex-pending",
                    "orderStatus": "Rejected",
                    "rejectReason": "insufficient margin",
                }
            ]
        }
    )
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert stored_orders.filter(pl.col("order_link_id") == "agc-ex-pending").select("status").item() == "rejected"
    assert len(private_client.orders) == 1
    assert private_client.orders[0]["orderLinkId"] != "agc-ex-pending"
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_flattens_untracked_position_on_bootstrap(tmp_path: Path) -> None:
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

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert private_client.orders[0]["reduceOnly"] is True
    assert private_client.orders[0]["side"] == "Buy"
    assert stored_orders.select("exit_reason").item() == "untracked_position"
    assert stored_orders.select("status").item() == "filled"
    assert "AAAUSDT" not in engine.state.positions_by_symbol


def test_ws_risk_pending_entry_position_is_not_flattened_before_entry_reconcile(tmp_path: Path) -> None:
    _write_pending_entry_order(tmp_path, status="submitted_unconfirmed", ts_ms=9_999_999_999_000)
    private_client = FakePrivateClient(confirm_fills=False)
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
    payload = engine.write_report(reason="heartbeat")

    assert private_client.orders == []
    assert engine.state.pending_entry_symbols == {"AAAUSDT"}
    assert payload["cycle"]["pending_entry_positions"] == 1
    assert payload["cycle"]["untracked_positions"] == 0


def test_ws_risk_reconciles_pending_entry_fill_before_untracked_guard(tmp_path: Path) -> None:
    _write_pending_entry_order(tmp_path, status="submitted_unconfirmed", ts_ms=9_999_999_999_000)
    private_client = FakePrivateClient(confirm_fills=True)
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

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    trade = stored.filter(pl.col("trade_id") == "t-entry").to_dicts()[0]
    assert private_client.orders == []
    assert trade["status"] == "open"
    assert trade["symbol"] == "AAAUSDT"
    assert trade["qty"] == "1"
    assert stored_orders.filter(pl.col("order_link_id") == "agc-en-pending").select("status").item() == "filled"
    assert engine.state.open_trades.height == 1
    assert engine.state.pending_entry_symbols == set()


def test_ws_risk_stale_pending_entry_no_longer_blocks_untracked_flatten(tmp_path: Path) -> None:
    _write_pending_entry_order(tmp_path, status="submitted_unconfirmed", ts_ms=1)
    private_client = FakePrivateClient(confirm_fills=False)
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

    assert engine.state.pending_entry_symbols == set()
    assert private_client.orders[0]["reduceOnly"] is True
    assert private_client.orders[0]["side"] == "Buy"


def test_ws_risk_untracked_exit_blocks_duplicate_until_fill(tmp_path: Path) -> None:
    private_client = FakePrivateClient(confirm_fills=False)
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
    engine.on_position_message({"data": private_client.positions[0]})

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert len(private_client.orders) == 1
    assert stored_orders.select("status").item() == "submitted_unconfirmed"
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_untracked_exit_history_error_stays_pending(tmp_path: Path) -> None:
    private_client = FakePrivateClient(fail_trade_history=True)
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
    engine.on_position_message({"data": private_client.positions[0]})

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    order = stored_orders.to_dicts()[0]
    assert len(private_client.orders) == 1
    assert order["status"] == "submitted_unconfirmed"
    assert order["submit_mode"] == "submitted"
    assert order["order_id"] == "rest-order-1"
    assert "fill confirmation failed" in order["error"]
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_bootstrap_loads_pending_untracked_exit_after_restart(tmp_path: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-ux-pending",
                    "ts_ms": 9_999_999_999_000,
                    "trade_id": "",
                    "symbol": "AAAUSDT",
                    "side": "Buy",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": True,
                    "order_id": "rest-order-existing",
                    "submit_mode": "submitted",
                    "avg_price": 100.0,
                    "notional_usdt": 0.0,
                    "status": "submitted_unconfirmed",
                    "exit_reason": "untracked_position",
                    "target_qty": "1",
                    "filled_qty": "",
                    "error": "fill confirmation failed: history unavailable",
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )
    private_client = FakePrivateClient(confirm_fills=False)
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

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert private_client.orders == []
    assert stored_orders.height == 1
    assert len(engine.state.orders) == 1
    assert engine.state.orders[0]["order_link_id"] == "agc-ux-pending"
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_live_open_untracked_exit_blocks_duplicate_after_restart(tmp_path: Path) -> None:
    private_client = FakePrivateClient(
        confirm_fills=False,
        open_orders=[
            {
                "symbol": "AAAUSDT",
                "orderLinkId": "agc-ux-existing",
                "orderStatus": "New",
                "reduceOnly": True,
            }
        ],
    )
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
    payload = engine.write_report(reason="heartbeat")

    assert private_client.orders == []
    assert read_dataset(tmp_path, "event_demo_orders").is_empty()
    assert engine.state.live_exit_order_symbols == {"AAAUSDT"}
    assert payload["cycle"]["bybit_live_exit_open_orders"] == 1


def test_ws_risk_untracked_exit_retries_after_pending_guard(tmp_path: Path) -> None:
    private_client = FakePrivateClient(confirm_fills=False)
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
            pending_exit_guard_seconds=1.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    engine.state.submitted_symbol_ts_ms["AAAUSDT"] -= 2_000
    engine.exit_untracked_positions()

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert len(private_client.orders) == 2
    assert stored_orders.height == 2
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_untracked_reconcile_history_error_keeps_pending(tmp_path: Path) -> None:
    private_client = FakePrivateClient(confirm_fills=False, fail_trade_history=True)
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
    engine.reconcile_untracked_exit_orders()

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    order = stored_orders.to_dicts()[0]
    assert order["status"] == "submitted_unconfirmed"
    assert "fill reconciliation failed" in order["error"]
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_untracked_reconcile_flattens_when_position_missing_even_if_history_fails(tmp_path: Path) -> None:
    private_client = FakePrivateClient(confirm_fills=False, fail_trade_history=True)
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
    private_client.positions = []
    engine.rest_reconcile()

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert stored_orders.select("status").item() == "filled"
    assert float(stored_orders.select("filled_qty").item()) == 1.0
    assert "AAAUSDT" not in engine.state.submitted_symbols


def test_ws_risk_reconciles_untracked_exit_when_position_is_flat(tmp_path: Path) -> None:
    private_client = FakePrivateClient(confirm_fills=False)
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
    private_client.positions = []
    engine.rest_reconcile()

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert stored_orders.select("status").item() == "filled"
    assert float(stored_orders.select("filled_qty").item()) == 1.0
    assert "AAAUSDT" not in engine.state.submitted_symbols


def test_ws_risk_telegram_material_events_are_deduped(tmp_path: Path, monkeypatch) -> None:
    sent: list[str] = []

    def fake_send(text: str, *, enabled: bool) -> bool:
        sent.append(text)
        return enabled

    monkeypatch.setattr("aggression_carry.event_demo.send_telegram_message", fake_send)
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(telegram=True, heartbeat_seconds=0.0),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )
    engine.state.orders.append(
        {
            "order_link_id": "agc-ux-AAA-1",
            "symbol": "AAAUSDT",
            "side": "Buy",
            "status": "filled",
            "submit_mode": "submitted",
            "exit_reason": "untracked_position",
        }
    )

    first = engine.write_report(reason="untracked_exit_submitted")
    second = engine.write_report(reason="untracked_exit_submitted")
    heartbeat = engine.write_report(reason="heartbeat")

    assert first["cycle"]["telegram_sent"] is True
    assert second["cycle"]["telegram_sent"] is False
    assert second["cycle"]["telegram_error"] == "duplicate_material_event"
    assert heartbeat["cycle"]["telegram_error"] == "quiet_no_material_event"
    assert len(sent) == 1


def test_ws_risk_pending_fill_notification_is_deduped_across_heartbeats(tmp_path: Path, monkeypatch) -> None:
    sent: list[str] = []

    def fake_send(text: str, *, enabled: bool) -> bool:
        sent.append(text)
        return enabled

    monkeypatch.setattr("aggression_carry.event_demo.send_telegram_message", fake_send)
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(telegram=True, heartbeat_seconds=0.0),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )
    engine.state.pending_fill_reconciliations.append(
        {
            "trade_id": "t-entry",
            "symbol": "AAAUSDT",
            "status": "open",
            "entry_order_link_id": "agc-en-pending",
        }
    )

    first = engine.write_report(reason="startup")
    second = engine.write_report(reason="heartbeat")

    assert first["cycle"]["pending_order_fills_reconciled"] == 1
    assert first["cycle"]["pending_entry_fills_reconciled"] == 1
    assert first["cycle"]["telegram_sent"] is True
    assert second["cycle"]["telegram_sent"] is False
    assert second["cycle"]["telegram_error"] == "duplicate_material_event"
    assert len(sent) == 1


def test_ws_risk_telegram_dedupe_survives_restart(tmp_path: Path, monkeypatch) -> None:
    sent: list[str] = []

    def fake_send(text: str, *, enabled: bool) -> bool:
        sent.append(text)
        return enabled

    monkeypatch.setattr("aggression_carry.event_demo.send_telegram_message", fake_send)
    first_engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(telegram=True, heartbeat_seconds=0.0),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )
    first_engine.state.errors.append("position snapshot failed")
    first = first_engine.write_report(reason="heartbeat")

    second_engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(telegram=True, heartbeat_seconds=0.0),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )
    second_engine.state.errors.append("position snapshot failed")
    second = second_engine.write_report(reason="heartbeat")

    dedupe_path = tmp_path / "reports" / "event-risk-ws" / "telegram_dedupe_keys.json"
    assert first["cycle"]["telegram_sent"] is True
    assert second["cycle"]["telegram_sent"] is False
    assert second["cycle"]["telegram_error"] == "duplicate_material_event"
    assert dedupe_path.exists()
    assert len(_read_telegram_dedupe_keys(dedupe_path.parent)) == 1
    assert len(sent) == 1


def test_ws_risk_stop_repair_dedupe_ignores_synthetic_order_link(tmp_path: Path, monkeypatch) -> None:
    sent: list[str] = []

    def fake_send(text: str, *, enabled: bool) -> bool:
        sent.append(text)
        return enabled

    monkeypatch.setattr("aggression_carry.event_demo.send_telegram_message", fake_send)

    def write_repair(order_link_id: str, *, stop_price: float = 112.0) -> dict[str, object]:
        engine = EventWebSocketRiskEngine(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            risk_config=EventWebSocketRiskConfig(telegram=True, heartbeat_seconds=0.0),
            private_client=FakePrivateClient(),
            private_stream=FakePrivateStream(),
            public_stream=FakePublicStream(),
        )
        engine.state.repairs.append(
            {
                "order_link_id": order_link_id,
                "symbol": "AAAUSDT",
                "status": "stop_repaired",
                "submit_mode": "submitted",
                "stop_price": stop_price,
                "take_profit_price": 80.0,
            }
        )
        return engine.write_report(reason="heartbeat")

    first = write_repair("agc-st-AAA-1")
    duplicate = write_repair("agc-st-AAA-2")
    changed_target = write_repair("agc-st-AAA-3", stop_price=113.0)

    assert first["cycle"]["telegram_sent"] is True
    assert duplicate["cycle"]["telegram_sent"] is False
    assert duplicate["cycle"]["telegram_error"] == "duplicate_material_event"
    assert changed_target["cycle"]["telegram_sent"] is True
    assert len(sent) == 2


def test_ws_risk_startup_report_keeps_timestamped_audit_copy(tmp_path: Path) -> None:
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(heartbeat_seconds=0.0),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    payload = engine.write_report(reason="startup")

    latest_path = tmp_path / "reports" / "event-risk-ws" / "latest_event_ws_risk_cycle.md"
    history_path = Path(payload["history_report_path"])
    assert payload["report_path"] == str(latest_path)
    assert latest_path.exists()
    assert (tmp_path / "reports" / "event-risk-ws" / "latest_event_ws_risk_cycle.json").exists()
    assert history_path.exists()
    assert history_path.name.startswith("event_ws_risk_cycle_ws-risk-")
    assert history_path.with_suffix(".json").exists()


def test_ws_risk_quiet_heartbeat_only_updates_latest_report(tmp_path: Path) -> None:
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(heartbeat_seconds=0.0),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    payload = engine.write_report(reason="heartbeat")

    report_dir = tmp_path / "reports" / "event-risk-ws"
    assert "history_report_path" not in payload
    assert (report_dir / "latest_event_ws_risk_cycle.md").exists()
    assert list(report_dir.glob("event_ws_risk_cycle_*.json")) == []


def test_ws_risk_material_heartbeat_keeps_timestamped_audit_copy(tmp_path: Path) -> None:
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(heartbeat_seconds=0.0),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )
    engine.state.errors.append("position snapshot failed")

    payload = engine.write_report(reason="heartbeat")

    history_path = Path(payload["history_report_path"])
    assert history_path.exists()
    assert history_path.with_suffix(".json").exists()


def test_ws_risk_position_stream_zero_closes_missing_ledger_position(tmp_path: Path) -> None:
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
    engine.mark_submitted_symbol("AAAUSDT")
    engine.on_position_message({"data": {"symbol": "AAAUSDT", "side": "Sell", "size": "0", "markPrice": "113"}})

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_reason").item() == "bybit_position_missing"
    assert "AAAUSDT" not in engine.state.submitted_symbols
    assert engine.state.reconciliations[0]["trade_id"] == "t1"


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


def _write_pending_entry_order(root: Path, *, status: str, ts_ms: int) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "agc-en-pending",
                    "ts_ms": ts_ms,
                    "trade_id": "t-entry",
                    "symbol": "AAAUSDT",
                    "side": "Sell",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": False,
                    "order_id": "order-entry",
                    "submit_mode": "submitted",
                    "avg_price": 100.0,
                    "notional_usdt": 100.0,
                    "target_notional_pct_equity": 0.2,
                    "entry_leverage": 2.0,
                    "initial_margin_usdt": 50.0,
                    "status": status,
                    "trade_side": "short",
                    "signal_ts_ms": 1_700_000_000_000,
                    "equity_usdt": 10_000.0,
                    "tick_size": 0.1,
                    "qty_step": 0.1,
                    "stop_price": 112.0,
                    "take_profit_price": 80.0,
                    "target_qty": "1",
                    "filled_qty": "",
                }
            ]
        ),
        root,
        "event_demo_orders",
        partition_by=(),
    )
