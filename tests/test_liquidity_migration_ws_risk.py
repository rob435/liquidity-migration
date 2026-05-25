from __future__ import annotations

import time
from pathlib import Path

import polars as pl

from liquidity_migration import ws_risk
from liquidity_migration.config import ResearchConfig
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration.ws_risk import (
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
        positions: list[dict[str, object]] | None = None,
        open_orders: list[dict[str, object]] | None = None,
        order_history: list[dict[str, object]] | None = None,
        fail_open_orders: bool = False,
        fail_order: bool = False,
    ) -> None:
        self.confirm_fills = confirm_fills
        self.fail_trade_history = fail_trade_history
        self.open_orders = open_orders or []
        self.order_history = order_history or []
        self.fail_open_orders = fail_open_orders
        self.fail_order = fail_order
        self.positions = positions if positions is not None else [
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
        self.stop_updates: list[dict[str, object]] = []

    def get_positions(self, *, settle_coin: str | None = None):
        return self.positions

    def get_open_orders(self, *, symbol: str | None = None, settle_coin: str | None = None):
        if self.fail_open_orders:
            raise RuntimeError("open orders unavailable")
        if symbol:
            return [row for row in self.open_orders if str(row.get("symbol") or "") == symbol]
        return self.open_orders

    def place_order(self, **params):
        if self.fail_order:
            raise RuntimeError("rest order rejected")
        self.orders.append(params)
        return {"orderId": "rest-order-1"}

    def set_trading_stop(self, **params):
        self.stop_updates.append(params)
        return {}

    def get_trade_history(self, *, symbol: str | None = None, order_link_id: str | None = None, limit: int = 50):
        if self.fail_trade_history:
            raise RuntimeError("history unavailable")
        if not self.confirm_fills:
            return []
        return [{"orderLinkId": order_link_id, "execQty": "1", "execPrice": "113", "execValue": "113", "execFee": "0.01"}]

    def get_order_history(self, *, symbol: str | None = None, order_link_id: str | None = None, limit: int = 50):
        rows = list(self.order_history)
        if symbol:
            rows = [row for row in rows if str(row.get("symbol") or "") == symbol]
        return rows[:limit]


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
            untracked_position_grace_seconds=0.0,
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


def test_ws_risk_bootstrap_exits_crossed_stop_before_stop_repair(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient(
        positions=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "avgPrice": "100",
                "markPrice": "113",
                "positionValue": "113",
                "unrealisedPnl": "-13",
                "stopLoss": "",
                "takeProfit": "80",
            }
        ]
    )
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=True,
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert len(private_client.orders) == 1
    assert private_client.orders[0]["reduceOnly"] is True
    assert private_client.stop_updates == []
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_reason").item() == "stop_loss"


def test_ws_risk_skips_stop_repair_when_exit_order_pending(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-ex-pending",
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
                    "exit_trigger_ts_ms": 1_234_567_890,
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )
    private_client = FakePrivateClient(
        confirm_fills=False,
        positions=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "avgPrice": "100",
                "markPrice": "100",
                "positionValue": "100",
                "unrealisedPnl": "0",
                "stopLoss": "",
                "takeProfit": "80",
            }
        ],
    )
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=True,
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    assert private_client.orders == []
    assert private_client.stop_updates == []
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_live_open_exit_order_blocks_duplicate_tracked_exit(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient(
        open_orders=[
            {
                "symbol": "AAAUSDT",
                "orderLinkId": "lm-ex-existing",
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
            untracked_position_grace_seconds=0.0,
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
            untracked_position_grace_seconds=0.0,
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
            untracked_position_grace_seconds=0.0,
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
            untracked_position_grace_seconds=0.0,
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
            untracked_position_grace_seconds=0.0,
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
            untracked_position_grace_seconds=0.0,
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
            untracked_position_grace_seconds=0.0,
        ),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
        trade_client=trade_client,
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})
    link = str(engine.state.orders[0]["order_link_id"])
    trigger_ts_ms = int(engine.state.orders[0]["exit_trigger_ts_ms"])
    engine.on_execution_message(
        {"data": [{"symbol": "AAAUSDT", "orderLinkId": link, "execQty": "1", "execPrice": "113", "execValue": "113"}]}
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert trade_client.orders[0]["reduceOnly"] is True
    assert engine.state.orders[0]["submit_mode"] == "ws_submitted"
    assert engine.state.orders[0]["status"] == "filled"
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_reason").item() == "stop_loss"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_trigger_ts_ms").item() == trigger_ts_ms


def test_ws_risk_execution_stream_partial_fill_reduces_trade_qty(tmp_path: Path) -> None:
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
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})
    link = str(engine.state.orders[0]["order_link_id"])
    trigger_ts_ms = int(engine.state.orders[0]["exit_trigger_ts_ms"])
    engine.on_execution_message(
        {"data": [{"symbol": "AAAUSDT", "orderLinkId": link, "execQty": "0.4", "execPrice": "113", "execValue": "45.2"}]}
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_order = read_dataset(tmp_path, "event_demo_orders").filter(pl.col("order_link_id") == link).to_dicts()[0]
    trade = stored.filter(pl.col("trade_id") == "t1").to_dicts()[0]
    payload = engine.write_report(reason="heartbeat")
    assert trade["status"] == "open"
    assert trade["qty"] == "0.6"
    assert trade["partial_exit_reason"] == "stop_loss"
    assert trade["partial_exit_qty"] == "0.4"
    assert trade["partial_exit_trigger_ts_ms"] == trigger_ts_ms
    assert stored_order["status"] == "partial"
    assert stored_order["filled_qty"] == "0.4"
    assert "AAAUSDT" in engine.state.submitted_symbols
    assert payload["cycle"]["pending_exit_fills_reconciled"] == 1
    assert payload["cycle"]["pending_entry_fills_reconciled"] == 0

    engine.on_execution_message(
        {"data": [{"symbol": "AAAUSDT", "orderLinkId": link, "execQty": "0.6", "execPrice": "113", "execValue": "67.8"}]}
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_order = read_dataset(tmp_path, "event_demo_orders").filter(pl.col("order_link_id") == link).to_dicts()[0]
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert stored_order["status"] == "filled"
    assert stored_order["filled_qty"] == "1"
    assert "AAAUSDT" not in engine.state.submitted_symbols


def test_ws_then_rest_falls_back_after_failed_ws_order_ack(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient()
    trade_client = FakeTradeClient()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="ws_then_rest",
            rest_fallback=True,
            exit_untracked_positions=False,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
        trade_client=trade_client,
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})
    ws_link = str(engine.state.orders[0]["order_link_id"])
    trigger_ts_ms = int(engine.state.orders[0]["exit_trigger_ts_ms"])
    engine.on_ws_order_ack({"retCode": 10001, "retMsg": "demo ws rejected", "_lm_order_link_id": ws_link})

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert trade_client.orders[0]["reduceOnly"] is True
    assert len(private_client.orders) == 1
    assert private_client.orders[0]["reduceOnly"] is True
    assert stored_orders.filter(pl.col("order_link_id") == ws_link).select("status").item() == "rejected"
    assert stored_orders.filter(pl.col("order_link_id") != ws_link).select("status").item() == "filled"
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_reason").item() == "stop_loss"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_trigger_ts_ms").item() == trigger_ts_ms
    assert "AAAUSDT" not in engine.state.submitted_symbols
    assert any("websocket order ack failed" in error for error in engine.state.errors)


def test_ws_ack_rest_fallback_failure_keeps_trade_open_with_context(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient(fail_order=True)
    trade_client = FakeTradeClient()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="ws_then_rest",
            rest_fallback=True,
            exit_untracked_positions=False,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
        trade_client=trade_client,
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})
    ws_link = str(engine.state.orders[0]["order_link_id"])
    trigger_ts_ms = int(engine.state.orders[0]["exit_trigger_ts_ms"])
    engine.on_ws_order_ack({"retCode": 10001, "retMsg": "demo ws rejected", "_lm_order_link_id": ws_link})

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    failed = stored_orders.filter(pl.col("order_link_id") != ws_link).to_dicts()[0]
    assert private_client.orders == []
    assert stored_orders.filter(pl.col("order_link_id") == ws_link).select("status").item() == "rejected"
    assert failed["status"] == "failed"
    assert failed["trade_id"] == "t1"
    assert failed["exit_reason"] == "stop_loss"
    assert failed["exit_trigger_ts_ms"] == trigger_ts_ms
    assert failed["target_qty"] == "1"
    assert "rest order rejected" in failed["error"]
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "open"
    assert "AAAUSDT" not in engine.state.submitted_symbols


def test_ws_order_ack_failure_without_rest_marks_order_rejected(tmp_path: Path) -> None:
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
            untracked_position_grace_seconds=0.0,
        ),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
        trade_client=trade_client,
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})
    ws_link = str(engine.state.orders[0]["order_link_id"])
    engine.on_ws_order_ack({"retCode": 10001, "retMsg": "demo ws rejected", "_lm_order_link_id": ws_link})

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert stored_orders.filter(pl.col("order_link_id") == ws_link).select("status").item() == "rejected"
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "open"
    assert "AAAUSDT" not in engine.state.submitted_symbols


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
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})
    link = str(engine.state.orders[0]["order_link_id"])
    trigger_ts_ms = int(engine.state.orders[0]["exit_trigger_ts_ms"])
    engine.on_execution_message(
        {"data": [{"symbol": "AAAUSDT", "orderLinkId": link, "execQty": "1", "execPrice": "113", "execValue": "113"}]}
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert engine.state.orders[0]["status"] == "filled"
    assert engine.state.exits[0]["submit_mode"] == "submitted"
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_reason").item() == "stop_loss"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_trigger_ts_ms").item() == trigger_ts_ms


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
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})
    link = str(engine.state.orders[0]["order_link_id"])
    trigger_ts_ms = int(engine.state.orders[0]["exit_trigger_ts_ms"])
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
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_reason").item() == "stop_loss"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_trigger_ts_ms").item() == trigger_ts_ms
    assert stored_orders.filter(pl.col("order_link_id") == link).select("status").item() == "filled"
    assert "AAAUSDT" not in engine.state.submitted_symbols


def test_ws_risk_order_stream_partial_fill_reduces_trade_qty(tmp_path: Path) -> None:
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
            untracked_position_grace_seconds=0.0,
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
                    "orderStatus": "PartiallyFilled",
                    "cumExecQty": "0.4",
                    "avgPrice": "113",
                }
            ]
        }
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_order = read_dataset(tmp_path, "event_demo_orders").filter(pl.col("order_link_id") == link).to_dicts()[0]
    trade = stored.filter(pl.col("trade_id") == "t1").to_dicts()[0]
    assert trade["status"] == "open"
    assert trade["qty"] == "0.6"
    assert trade["partial_exit_reason"] == "stop_loss"
    assert trade["partial_exit_qty"] == "0.4"
    assert stored_order["status"] == "partial"
    assert stored_order["filled_qty"] == "0.4"
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_bootstrap_loads_pending_exit_order_after_restart(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-ex-pending",
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
                    "exit_trigger_ts_ms": 1_234_567_890,
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
            untracked_position_grace_seconds=0.0,
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
                    "orderLinkId": "lm-ex-pending",
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
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_reason").item() == "stop_loss"
    assert stored.filter(pl.col("trade_id") == "t1").select("exit_trigger_ts_ms").item() == 1_234_567_890
    assert stored_orders.filter(pl.col("order_link_id") == "lm-ex-pending").select("status").item() == "filled"


def test_ws_risk_rejected_pending_exit_unblocks_retry_after_restart(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-ex-pending",
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
            untracked_position_grace_seconds=0.0,
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
                    "orderLinkId": "lm-ex-pending",
                    "orderStatus": "Rejected",
                    "rejectReason": "insufficient margin",
                }
            ]
        }
    )
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert stored_orders.filter(pl.col("order_link_id") == "lm-ex-pending").select("status").item() == "rejected"
    assert len(private_client.orders) == 1
    assert private_client.orders[0]["orderLinkId"] != "lm-ex-pending"
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_logs_untracked_close_to_logger(tmp_path: Path, caplog) -> None:
    """The risk engine had no journal output for 12+ hours pre-fix because it
    relied only on parquet reports. Every untracked_position close must now
    emit a WARNING log line carrying symbol, side, status, grace_seconds, and
    error so systemd journalctl tells the operator immediately when positions
    are being flattened.
    """
    import logging as _logging
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
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    with caplog.at_level(_logging.WARNING, logger="liquidity_migration.ws_risk"):
        engine.bootstrap()

    close_records = [
        record for record in caplog.records
        if record.name == "liquidity_migration.ws_risk" and "untracked_position close" in record.getMessage()
    ]
    assert close_records, "expected an untracked_position close log line"
    msg = close_records[0].getMessage()
    assert "symbol=AAAUSDT" in msg
    assert "side=Buy" in msg
    assert "grace_seconds=0.0" in msg


def test_ws_risk_untracked_grace_period_defers_close_then_fires(tmp_path: Path) -> None:
    """A freshly opened Bybit position appears via WS before the demo engine
    writes the trade/order rows. With grace_seconds=90, the first
    exit_untracked_positions call must only stamp first_seen and NOT submit a
    close; once the grace window elapses, the next call submits the close.
    This is the defense-in-depth half of the close-on-open fix.
    """
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
            untracked_position_grace_seconds=90.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    assert private_client.orders == [], "first sighting must defer the close"
    assert engine.state.untracked_first_seen_ms.get("AAAUSDT") is not None

    # A second call within the grace window must still defer.
    engine.exit_untracked_positions()
    assert private_client.orders == []

    # Backdate first_seen by more than the grace and re-run — close must fire.
    engine.state.untracked_first_seen_ms["AAAUSDT"] -= 91_000
    engine.exit_untracked_positions()
    assert len(private_client.orders) == 1
    assert private_client.orders[0]["reduceOnly"] is True
    assert private_client.orders[0]["side"] == "Buy"

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert stored_orders.select("exit_reason").item() == "untracked_position"


def test_ws_risk_untracked_grace_cleared_when_symbol_becomes_tracked(tmp_path: Path) -> None:
    """If the demo engine's pending entry row lands in parquet during the grace
    window, the next reconcile populates pending_entry_symbols and the grace
    timer must be dropped (so a future, unrelated untracked re-sighting starts
    fresh)."""
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
            untracked_position_grace_seconds=90.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    assert "AAAUSDT" in engine.state.untracked_first_seen_ms

    engine.state.pending_entry_symbols.add("AAAUSDT")
    engine.exit_untracked_positions()
    assert "AAAUSDT" not in engine.state.untracked_first_seen_ms
    assert private_client.orders == []


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
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
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
            untracked_position_grace_seconds=0.0,
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
            untracked_position_grace_seconds=0.0,
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
    assert stored_orders.filter(pl.col("order_link_id") == "lm-en-pending").select("status").item() == "filled"
    assert engine.state.open_trades.height == 1
    assert engine.state.pending_entry_symbols == set()


def test_ws_risk_reconciles_stale_pending_entry_when_position_live(tmp_path: Path) -> None:
    _write_pending_entry_order(tmp_path, status="submitted_unconfirmed", ts_ms=1)
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
            untracked_position_grace_seconds=0.0,
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
    assert stored_orders.filter(pl.col("order_link_id") == "lm-en-pending").select("status").item() == "filled"
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
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    assert engine.state.pending_entry_symbols == set()
    assert private_client.orders[0]["reduceOnly"] is True
    assert private_client.orders[0]["side"] == "Buy"


def test_ws_risk_terminalizes_stale_pending_entry_when_exchange_flat(tmp_path: Path) -> None:
    _write_pending_entry_order(tmp_path, status="submitted_unconfirmed", ts_ms=1)
    private_client = FakePrivateClient(confirm_fills=False, positions=[])
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
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert private_client.orders == []
    assert engine.state.pending_entry_symbols == set()
    assert stored_orders.filter(pl.col("order_link_id") == "lm-en-pending").select("status").item() == "expired_unconfirmed"


def test_ws_risk_keeps_stale_pending_entry_when_live_order_exists(tmp_path: Path) -> None:
    _write_pending_entry_order(tmp_path, status="submitted_unconfirmed", ts_ms=1)
    private_client = FakePrivateClient(
        confirm_fills=False,
        positions=[],
        open_orders=[
            {
                "symbol": "AAAUSDT",
                "orderLinkId": "lm-en-pending",
                "orderStatus": "New",
                "reduceOnly": False,
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
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert private_client.orders == []
    assert stored_orders.filter(pl.col("order_link_id") == "lm-en-pending").select("status").item() == "submitted_unconfirmed"


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
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
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
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
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


def test_ws_risk_untracked_execution_partial_keeps_duplicate_guard(tmp_path: Path) -> None:
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
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    link = str(engine.state.orders[0]["order_link_id"])
    engine.on_execution_message(
        {"data": [{"symbol": "AAAUSDT", "orderLinkId": link, "execQty": "0.4", "execPrice": "113", "execValue": "45.2"}]}
    )

    stored_order = read_dataset(tmp_path, "event_demo_orders").filter(pl.col("order_link_id") == link).to_dicts()[0]
    assert stored_order["status"] == "partial"
    assert stored_order["filled_qty"] == "0.4"
    assert "AAAUSDT" in engine.state.submitted_symbols
    assert "AAAUSDT" in engine.state.positions_by_symbol


def test_ws_risk_bootstrap_loads_pending_untracked_exit_after_restart(tmp_path: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-ux-pending",
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
            untracked_position_grace_seconds=0.0,
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
    assert engine.state.orders[0]["order_link_id"] == "lm-ux-pending"
    assert "AAAUSDT" in engine.state.submitted_symbols


def test_ws_risk_live_open_untracked_exit_blocks_duplicate_after_restart(tmp_path: Path) -> None:
    private_client = FakePrivateClient(
        confirm_fills=False,
        open_orders=[
            {
                "symbol": "AAAUSDT",
                "orderLinkId": "lm-ux-existing",
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
            untracked_position_grace_seconds=0.0,
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


def test_ws_risk_stale_untracked_exit_is_filled_when_exchange_is_flat(tmp_path: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-ux-stale",
                    "ts_ms": 1,
                    "trade_id": "",
                    "symbol": "AAAUSDT",
                    "side": "Buy",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": True,
                    "submit_mode": "submitted",
                    "status": "submitted_unconfirmed",
                    "exit_reason": "untracked_position",
                    "target_qty": "1",
                    "filled_qty": "",
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )
    private_client = FakePrivateClient(confirm_fills=False)
    private_client.positions = []
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="rest",
            pending_exit_guard_seconds=1.0,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    order = stored_orders.filter(pl.col("order_link_id") == "lm-ux-stale").to_dicts()[0]
    assert private_client.orders == []
    assert order["status"] == "filled"
    assert order["filled_qty"] == "1"
    assert "filled inferred from flat Bybit position" in order["error"]


def test_ws_risk_stale_exit_stays_pending_when_open_order_snapshot_fails(tmp_path: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-ux-stale",
                    "ts_ms": 1,
                    "trade_id": "",
                    "symbol": "AAAUSDT",
                    "side": "Buy",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": True,
                    "submit_mode": "submitted",
                    "status": "submitted_unconfirmed",
                    "exit_reason": "untracked_position",
                    "target_qty": "1",
                    "filled_qty": "",
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )
    private_client = FakePrivateClient(confirm_fills=False, fail_open_orders=True)
    private_client.positions = []
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="rest",
            pending_exit_guard_seconds=1.0,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    order = stored_orders.filter(pl.col("order_link_id") == "lm-ux-stale").to_dicts()[0]
    assert order["status"] == "submitted_unconfirmed"
    assert "open orders unavailable" in "; ".join(engine.state.errors)


def test_ws_risk_stale_tracked_exit_closes_trade_when_exchange_is_flat(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-ex-stale",
                    "ts_ms": 1,
                    "trade_id": "t1",
                    "symbol": "AAAUSDT",
                    "side": "Buy",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": True,
                    "submit_mode": "submitted",
                    "status": "submitted_unconfirmed",
                    "exit_reason": "stop_loss",
                    "exit_trigger_ts_ms": 1_234_567_890,
                    "target_qty": "1",
                    "filled_qty": "",
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )
    private_client = FakePrivateClient(confirm_fills=False)
    private_client.positions = []
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="rest",
            pending_exit_guard_seconds=1.0,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored = read_dataset(tmp_path, "event_demo_trades")
    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    trade = stored.filter(pl.col("trade_id") == "t1").to_dicts()[0]
    order = stored_orders.filter(pl.col("order_link_id") == "lm-ex-stale").to_dicts()[0]
    assert private_client.orders == []
    assert order["status"] == "filled"
    assert trade["status"] == "closed"
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_trigger_ts_ms"] == 1_234_567_890


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
            untracked_position_grace_seconds=0.0,
            pending_exit_guard_seconds=1.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
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
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
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
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
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
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
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

    monkeypatch.setattr("liquidity_migration.event_demo.send_telegram_message", fake_send)
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
            "order_link_id": "lm-ux-AAA-1",
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

    monkeypatch.setattr("liquidity_migration.event_demo.send_telegram_message", fake_send)
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
            "entry_order_link_id": "lm-en-pending",
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

    monkeypatch.setattr("liquidity_migration.event_demo.send_telegram_message", fake_send)
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

    monkeypatch.setattr("liquidity_migration.event_demo.send_telegram_message", fake_send)

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

    first = write_repair("lm-st-AAA-1")
    duplicate = write_repair("lm-st-AAA-2")
    changed_target = write_repair("lm-st-AAA-3", stop_price=113.0)

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
            untracked_position_grace_seconds=0.0,
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
            untracked_position_grace_seconds=0.0,
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


def test_ws_risk_adopts_untracked_position_on_bootstrap(tmp_path: Path) -> None:
    private_client = FakePrivateClient()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False,
            repair_stops=False,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert stored.height == 1
    trade = stored.to_dicts()[0]
    assert trade["symbol"] == "AAAUSDT"
    assert trade["side"] == "short"
    assert trade["status"] == "open"
    assert trade["submit_mode"] == "adopted"
    assert trade["strategy_id"] == "adopted"
    assert trade["stop_price"] == 112.0
    assert trade["take_profit_price"] == 79.0
    assert trade["planned_exit_ts_ms"] > trade["entry_ts_ms"]
    assert str(trade["trade_id"]).startswith("adopted-AAAUSDT-")
    assert "AAAUSDT" not in engine.state.submitted_symbols


def test_ws_risk_recovers_strategy_trade_id_from_bot_order_link(tmp_path: Path) -> None:
    """When an adopted position's symbol has a Bybit order history row whose
    orderLinkId matches our bot-generated `lm-en-{base}-{ts36}` entry pattern,
    ws_risk must decode the signal_ts and rebuild the deterministic strategy
    trade_id verbatim — NOT the lossy `adopted-{symbol}-{opened_ms}` form.
    This is what makes paper/demo reconciliation pair-able across a VPS
    rebuild: same orderLinkId on Bybit's side → same signal_ts → same
    trade_id on both ledgers."""
    from liquidity_migration.event_demo import _order_link_id, _demo_event_config, _selected_scenario
    from liquidity_migration.volume_events import VolumeEventResearchConfig

    signal_ts_ms = 1_779_667_200_000
    entry_link = _order_link_id("en", symbol="AAAUSDT", signal_ts_ms=signal_ts_ms)
    promoted_strategy = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
    promoted_scenario = _selected_scenario(promoted_strategy)
    expected_trade_id = f"{promoted_scenario.scenario_id}-AAAUSDT-{signal_ts_ms}"

    # Bybit createdTime is typically 1-6h AFTER signal_ts (the cycle waits
    # for the feature pipeline before submitting). Use a realistic offset so
    # the test pins entry_ts_ms != signal_ts_ms — that gap was the bug.
    bybit_created_ms = signal_ts_ms + 3 * 60 * 60 * 1000  # +3h
    private_client = FakePrivateClient(
        positions=[
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
                "createdTime": str(bybit_created_ms),
            },
        ],
        order_history=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",  # short entry: bot sold to open
                "orderLinkId": entry_link,
                "orderStatus": "Filled",
                "qty": "1",
                "avgPrice": "100",
            },
        ],
    )
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False,
            repair_stops=False,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored = read_dataset(tmp_path, "event_demo_trades")
    assert stored.height == 1
    trade = stored.to_dicts()[0]
    # The recovered trade_id matches the deterministic one the paper sleeve
    # would have written for the same signal — so reconciliation now pairs.
    assert trade["trade_id"] == expected_trade_id, (
        f"expected recovered trade_id {expected_trade_id!r}, got {trade['trade_id']!r}"
    )
    assert trade["strategy_id"] == promoted_scenario.scenario_id  # type: ignore[union-attr]
    # entry_ts_ms must reflect the actual venue fill time, NOT signal_ts.
    # The cycle's exit logic computes planned_exit_ts_ms + event_decay rank
    # checks from entry_ts_ms — putting signal_ts there (1-6h before fill)
    # makes the position look older and trips exits prematurely. Observed
    # live 2026-05-25: WAVESUSDT got event_decay on demo while paper still
    # held the same position because paper's entry_ts was correct.
    assert trade["entry_ts_ms"] == bybit_created_ms, (
        f"entry_ts_ms must be Bybit createdTime ({bybit_created_ms}), "
        f"got {trade['entry_ts_ms']} — and signal_ts ({signal_ts_ms}) is what we DON'T want"
    )
    assert trade["entry_ts_ms"] != signal_ts_ms, "entry_ts_ms must not collapse to signal_ts"
    assert trade["signal_ts_ms"] == signal_ts_ms
    assert trade["opened_at_ms"] == bybit_created_ms
    assert trade["entry_order_link_id"] == entry_link
    assert trade["submit_mode"] == "adopted_recovered"
    assert not str(trade["trade_id"]).startswith("adopted-"), (
        "recovered trades must not use the lossy adopted-* prefix"
    )


def test_ws_risk_falls_back_to_adopted_when_order_history_has_no_bot_link(tmp_path: Path) -> None:
    """Hand-placed positions (or positions older than the order history
    window) lack a bot-formatted orderLinkId — recovery must NOT synthesize
    a fake signal_ts in that case. Falls back to the legacy adopted-*
    trade_id so the operator can still manage the position."""
    private_client = FakePrivateClient(
        order_history=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "orderLinkId": "manually-placed-order-xyz",  # not a bot pattern
                "orderStatus": "Filled",
            },
        ],
    )
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False,
            repair_stops=False,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored = read_dataset(tmp_path, "event_demo_trades")
    trade = stored.to_dicts()[0]
    assert str(trade["trade_id"]).startswith("adopted-AAAUSDT-")
    assert trade["submit_mode"] == "adopted"
    assert trade["strategy_id"] == "adopted"


def test_ws_risk_adopted_position_exits_on_stop(tmp_path: Path) -> None:
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
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "AAAUSDT", "markPrice": "113"}})

    stored = read_dataset(tmp_path, "event_demo_trades")
    adopted = stored.filter(pl.col("symbol") == "AAAUSDT").to_dicts()[0]
    assert adopted["status"] == "closed"
    assert adopted["exit_reason"] == "stop_loss"
    assert private_client.orders and private_client.orders[0]["reduceOnly"] is True


def test_ws_risk_does_not_adopt_already_tracked_position(tmp_path: Path) -> None:
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False,
            repair_stops=False,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored = read_dataset(tmp_path, "event_demo_trades")
    trade_ids = [str(tid) for tid in stored["trade_id"].to_list()]
    assert trade_ids == ["t1"]
    assert not any(tid.startswith("adopted-") for tid in trade_ids)


def test_ws_risk_adoption_respects_grace_period(tmp_path: Path) -> None:
    private_client = FakePrivateClient()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False,
            repair_stops=False,
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=300.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    assert read_dataset(tmp_path, "event_demo_trades").is_empty()
    assert "AAAUSDT" in engine.state.untracked_first_seen_ms


def test_ws_risk_adopt_config_rejects_negative_pct(tmp_path: Path) -> None:
    raised = ""
    try:
        EventWebSocketRiskEngine(
            tmp_path,
            config=ResearchConfig(data_root=tmp_path),
            risk_config=EventWebSocketRiskConfig(adopt_stop_loss_pct=-0.05),
        )
    except ValueError as exc:
        raised = str(exc)
    assert "non-negative" in raised


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
                    "order_link_id": "lm-en-pending",
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
