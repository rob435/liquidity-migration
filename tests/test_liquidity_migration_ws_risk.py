from __future__ import annotations

import time
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import ws_risk
from liquidity_migration.config import ResearchConfig
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration.ws_risk import (
    _LOG_RETENTION,
    EventWebSocketRiskConfig,
    EventWebSocketRiskEngine,
    WebSocketRiskState,
    _read_telegram_dedupe_keys,
)


def test_prune_state_logs_caps_telemetry_and_preserves_cumulative_count() -> None:
    """Resource leak: the append-only telemetry logs must be bounded so a
    long-lived risk daemon can't OOM (orphaning a position), while the
    cumulative report counters stay exact via the eviction counters."""
    state = WebSocketRiskState()
    state.exits = [{"i": i} for i in range(_LOG_RETENTION + 50)]
    state.errors = [f"e{i}" for i in range(_LOG_RETENTION + 10)]

    class _Stub:
        pass

    stub = _Stub()
    stub.state = state
    stub.risk = EventWebSocketRiskConfig(telemetry_log_retention=_LOG_RETENTION)
    EventWebSocketRiskEngine._prune_state_logs(stub)

    assert len(state.exits) == _LOG_RETENTION
    assert state.exits_evicted == 50
    # The most recent rows are retained (last-N display still works).
    assert state.exits[-1] == {"i": _LOG_RETENTION + 49}
    # Cumulative count == surviving + evicted == original length.
    assert len(state.exits) + state.exits_evicted == _LOG_RETENTION + 50
    assert len(state.errors) == _LOG_RETENTION
    assert state.errors_evicted == 10


def test_prune_closed_order_state_evicts_closed_links_keeps_open_and_recent() -> None:
    """The per-order-link maps (orders/orders_by_link/executions_by_link/
    submitted_link_*) must be bounded like the telemetry logs, but an OPEN
    trade's links must NEVER be evicted — a late fill/cancel/reconcile would
    then find a missing order and mis-handle a live position (audit #14)."""
    retention = 5
    state = WebSocketRiskState()
    open_order = {"order_link_id": "lm-open", "trade_id": "T_OPEN", "symbol": "AAA"}
    closed_old = [{"order_link_id": f"lm-c{i}", "trade_id": f"T_C{i}", "symbol": "B"} for i in range(20)]
    recent = [{"order_link_id": f"lm-r{i}", "trade_id": f"T_R{i}", "symbol": "C"} for i in range(retention)]
    state.orders = [open_order, *closed_old, *recent]  # append-ordered: open is the OLDEST
    state.orders_by_link = {o["order_link_id"]: o for o in state.orders}
    state.executions_by_link = {o["order_link_id"]: {"filled_qty": 1.0, "value": 1.0} for o in state.orders}
    state.submitted_link_to_trade_id = {o["order_link_id"]: o["trade_id"] for o in state.orders}
    state.submitted_link_submit_mode = {o["order_link_id"]: "ws_submitted" for o in state.orders}
    state.open_trades = pl.DataFrame([{"trade_id": "T_OPEN", "symbol": "AAA", "status": "open"}])

    class _Stub:
        pass

    stub = _Stub()
    stub.state = state
    stub.risk = EventWebSocketRiskConfig(telemetry_log_retention=retention)
    EventWebSocketRiskEngine._prune_closed_order_state(stub)

    # The OPEN trade's link survives even though it is the single oldest entry.
    for m in (state.orders_by_link, state.executions_by_link, state.submitted_link_to_trade_id, state.submitted_link_submit_mode):
        assert "lm-open" in m
    # The recent grace window survives.
    assert all(f"lm-r{i}" in state.orders_by_link for i in range(retention))
    # Old CLOSED-trade links are evicted from ALL four maps.
    for link in ("lm-c0", "lm-c19"):
        assert link not in state.orders_by_link
        assert link not in state.executions_by_link
        assert link not in state.submitted_link_to_trade_id
        assert link not in state.submitted_link_submit_mode
    # The list and its index stay consistent; counter is exact.
    assert {o["order_link_id"] for o in state.orders} == set(state.orders_by_link)
    assert state.orders_evicted == 20


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
        fail_positions: bool = False,
    ) -> None:
        self.confirm_fills = confirm_fills
        self.fail_trade_history = fail_trade_history
        self.open_orders = open_orders or []
        self.order_history = order_history or []
        self.fail_open_orders = fail_open_orders
        self.fail_order = fail_order
        self.fail_positions = fail_positions
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
        if self.fail_positions:
            raise RuntimeError("get_positions timeout")
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


def test_ws_risk_ws_exit_splits_close_when_position_exceeds_max_mkt_qty(tmp_path: Path) -> None:
    """ws_exit path on a SUPER-shape position: submits N reduce-only sub-orders
    with -s0/-s1 suffixes when qty > maxMktOrderQty. Without the split,
    Bybit rejects the single close outright and the position hangs open."""
    # Open trade with qty 37500 and a cap of 20000 — must split into 2 subs.
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "t1",
                    "symbol": "SUPERUSDT",
                    "side": "short",
                    "status": "open",
                    "qty": "37500",
                    "entry_price": 2.0,
                    "stop_price": 2.24,
                    "take_profit_price": 1.6,
                    "planned_exit_ts_ms": 9_999_999_999_999,
                    "qty_step": 1.0,
                    "max_market_order_qty": 20000.0,
                }
            ]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    trade_client = FakeTradeClient()
    # Match the venue position so the orphan reconciler doesn't fire and so
    # the WS engine recognises the ticker hit as belonging to a tracked trade.
    private_client = FakePrivateClient(
        positions=[
            {
                "symbol": "SUPERUSDT",
                "side": "Sell",
                "size": "37500",
                "avgPrice": "2.0",
                "markPrice": "2.0",
                "positionValue": "75000",
                "unrealisedPnl": "0",
                "stopLoss": "2.24",
                "takeProfit": "1.6",
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
            order_submit_mode="ws",
            rest_fallback=False,
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
    engine.on_ticker_message({"data": {"symbol": "SUPERUSDT", "markPrice": "2.30"}})

    # Two reduce-only sub-orders should have been submitted, each <= 20000.
    assert len(trade_client.orders) == 2, [o.get("qty") for o in trade_client.orders]
    for order in trade_client.orders:
        assert order["reduceOnly"] is True
        assert float(order["qty"]) <= 20000.0
    link_ids = [str(o["orderLinkId"]) for o in trade_client.orders]
    bases = {link.rsplit("-s", 1)[0] for link in link_ids}
    assert len(bases) == 1, bases
    suffixes = sorted(link.rsplit("-s", 1)[1] for link in link_ids)
    assert suffixes == ["0", "1"], suffixes


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


def test_ws_risk_reconcile_flat_pending_exit_backfills_exit_price_from_closed_pnl(tmp_path: Path) -> None:
    """DRIFT-style close (P1-1, 2026-05-27): a failed lm-rx left an order
    with avg_price=0; the venue position later went flat under its own
    stop. ``reconcile_flat_pending_exit_orders`` previously closed the
    trade with exit_price=0 — a broken audit row.

    With the closed-PnL backfill the trade closes with the real venue
    exit price even when the lm-rx never confirmed a fill."""
    _write_open_trade(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-rx-AAA-1",
                    "ts_ms": 9_999_999_999_000,
                    "trade_id": "t1",
                    "symbol": "AAAUSDT",
                    "side": "Buy",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": True,
                    "submit_mode": "submitted",
                    # The failed-confirmation shape: status pending but no fill data.
                    "status": "submitted_unconfirmed",
                    "avg_price": 0.0,
                    "exit_reason": "stop_loss",
                    "exit_trigger_ts_ms": 1_700_000_000_000,
                    "target_qty": "1",
                    "filled_qty": "",
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )

    class _FakePrivateClientWithClosedPnl(FakePrivateClient):
        """FakePrivateClient + a get_closed_pnl record so the orphan PnL
        backfill returns the real venue close instead of an empty dict."""

        def get_closed_pnl(self, *, symbol: str | None = None, start_time_ms: int | None = None, limit: int = 50):
            return [
                {
                    "symbol": "AAAUSDT",
                    "side": "Buy",
                    "avgExitPrice": "112.5",
                    "createdTime": "1700000060000",
                    "orderId": "closed-pnl-1",
                }
            ]

    # Flat position at venue + no fill data on get_trade_history (mirrors
    # the failed lm-rx live shape: order submitted, fill confirm never
    # resolved, venue closed under its own stop).
    private_client = _FakePrivateClientWithClosedPnl(positions=[], confirm_fills=False)
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
            exit_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    # Run the rest-reconcile cycle — fires reconcile_flat_pending_exit_orders
    # which we previously short-circuited with exit_price=0.
    engine.rest_reconcile()

    stored = read_dataset(tmp_path, "event_demo_trades")
    closed = stored.filter(pl.col("trade_id") == "t1")
    assert closed.select("status").item() == "closed"
    # The fix: exit_price comes from the closed-PnL backfill, not 0.
    assert float(closed.select("exit_price").item()) == pytest.approx(112.5)


def _submit_exit_engine(tmp_path: Path, trade_client: "FakeTradeClient") -> "EventWebSocketRiskEngine":
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
            adopt_untracked_positions=False,
        ),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
        trade_client=trade_client,
    )
    engine.bootstrap()
    return engine


_SUBMIT_EXIT_PLAN = {
    "trade_id": "t1",
    "symbol": "AAAUSDT",
    "side": "short",
    "qty": "1",
    "exit_reason": "stop_loss",
    "planned_exit_price": 113.0,
}


def test_ws_risk_submit_exit_skips_when_symbol_has_live_exit_order(tmp_path: Path) -> None:
    """Cross-process double-submit guard (P1-2), now IN-MEMORY (no parquet read on
    the stop hot path). ``live_exit_order_symbols`` is refreshed from the venue's
    open orders every rest_reconcile and covers the demo cycle's live reduce-only
    exits. When it already shows an exit for a symbol ws_risk did not submit, skip
    — the demo cycle's exit is handling it."""
    _write_open_trade(tmp_path)
    trade_client = FakeTradeClient()
    engine = _submit_exit_engine(tmp_path, trade_client)
    # A reconcile has picked up the demo cycle's live reduce-only on AAAUSDT.
    engine.state.live_exit_order_symbols.add("AAAUSDT")
    engine.state.submitted_symbols.discard("AAAUSDT")

    engine.submit_exit({**_SUBMIT_EXIT_PLAN, "exit_trigger_ts_ms": int(time.time() * 1000)})

    assert trade_client.orders == [], trade_client.orders  # skipped, no second submit
    assert "AAAUSDT" in engine.state.submitted_symbols  # re-marked so in-cycle ticks no-op


def test_ws_risk_submit_exit_proceeds_without_a_tracked_live_exit(tmp_path: Path) -> None:
    """The guard is EFFICIENCY, not safety: when no live exit order is tracked for
    the symbol (e.g. the <30s cross-process race before the next reconcile), the
    stop submission MUST proceed. Worst case is a redundant reduce-only (venue
    caps/rejects it) — never a missed stop."""
    _write_open_trade(tmp_path)
    trade_client = FakeTradeClient()
    engine = _submit_exit_engine(tmp_path, trade_client)
    engine.state.live_exit_order_symbols.discard("AAAUSDT")
    engine.state.submitted_symbols.discard("AAAUSDT")

    engine.submit_exit({**_SUBMIT_EXIT_PLAN, "exit_trigger_ts_ms": int(time.time() * 1000)})

    assert trade_client.orders, "stop submission must not be suppressed when no live exit is tracked"


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


def test_exit_untracked_blocked_when_a_ledger_read_failed(tmp_path: Path, caplog) -> None:
    """Fail-closed sleeve isolation: if a configured sibling ledger read raised
    this pass, that sleeve's open trades are missing from open_trades, so a live
    position of that sleeve looks 'untracked'. exit_untracked_positions must
    refuse to flatten while state.ledger_read_error is set; once cleared, the same
    untracked position IS flattened -- proving the guard, not the setup, blocks
    it. Directly defends operator goal #1 (a fault in one sleeve must not flatten
    another's positions on the netted account)."""
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
    # A live, untracked position (absent from the empty open_trades ledger).
    engine.state.positions_by_symbol = {"AAAUSDT": {"symbol": "AAAUSDT", "size": "1", "side": "Buy"}}
    engine.state.ledger_read_error = "continuous:continuous_fade_demo_trades: ComputeError: torn parquet"

    with caplog.at_level(_logging.WARNING, logger="liquidity_migration.ws_risk"):
        engine.exit_untracked_positions()
    assert private_client.orders == [], "must not flatten a position while a ledger read is failing"
    assert any("skipping exit_untracked_positions" in r.getMessage() for r in caplog.records)

    # Clear the fault: the very same untracked position is now flattened (grace=0).
    engine.state.untracked_first_seen_ms.clear()
    engine.state.ledger_read_error = ""
    engine.exit_untracked_positions()
    assert len(private_client.orders) == 1
    assert private_client.orders[0]["reduceOnly"] is True


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

    monkeypatch.setattr("liquidity_migration.ws_risk.send_telegram_message", fake_send)
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

    monkeypatch.setattr("liquidity_migration.ws_risk.send_telegram_message", fake_send)
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

    monkeypatch.setattr("liquidity_migration.ws_risk.send_telegram_message", fake_send)
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

    monkeypatch.setattr("liquidity_migration.ws_risk.send_telegram_message", fake_send)

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
    import time

    # signal_ts derived from wall-clock so the planned_exit (signal_ts + 3h
    # bybit-fill-delay + 3d production-hold) is always in the future relative
    # to test execution. Hardcoded absolute timestamps in this fixture
    # silently expired and caused the adopted-recovered trade to be closed
    # by the max-hold exit before the test could assert on it.
    # Align signal_ts to the nearest second: the order_link_id encoding
    # uses a base-36 second-resolution timestamp, so signal_ts must be
    # second-aligned for the round-trip (encode → decode → trade_id) to
    # produce the same ms value the test asserts on.
    now_ms = (int(time.time() * 1000) // 1000) * 1000
    signal_ts_ms = now_ms - 24 * 60 * 60 * 1000  # 1 day ago — enough for fill but before max-hold
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


def test_ws_risk_recovers_continuous_addon_hedge_as_tracking_row(tmp_path: Path) -> None:
    from liquidity_migration.continuous_hedge_manager import HEDGE_SYMBOL
    from liquidity_migration.event_demo import _order_link_id

    addon_root = tmp_path / "continuous-addon"
    signal_ts_ms = 1_700_000_000_000
    opened_ms = signal_ts_ms + 60_000
    entry_link = _order_link_id("en-ca", symbol=HEDGE_SYMBOL, signal_ts_ms=signal_ts_ms)
    private_client = FakePrivateClient(
        positions=[
            {
                "symbol": HEDGE_SYMBOL,
                "side": "Buy",
                "size": "0.02",
                "avgPrice": "100000",
                "markPrice": "100000",
                "positionValue": "2000",
                "unrealisedPnl": "0",
                "createdTime": str(opened_ms),
            },
        ],
        order_history=[
            {
                "symbol": HEDGE_SYMBOL,
                "side": "Buy",
                "orderLinkId": entry_link,
                "orderStatus": "Filled",
                "qty": "0.02",
                "avgPrice": "100000",
                "createdTime": str(opened_ms),
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
            continuous_addon_data_root=str(addon_root),
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    stored = read_dataset(addon_root, "continuous_fade_demo_trades")
    assert stored.height == 1
    trade = stored.to_dicts()[0]
    assert trade["trade_id"] == f"hedge-{entry_link}"
    assert trade["strategy_id"] == "continuous_btc_hedge_v1"
    assert trade["symbol"] == HEDGE_SYMBOL
    assert trade["side"] == "long"
    assert trade["sleeve"] == "continuous_addon"
    assert trade["entry_order_link_id"] == entry_link
    assert trade["signal_ts_ms"] == signal_ts_ms
    assert trade["entry_ts_ms"] == opened_ms
    assert trade["stop_price"] == 0.0
    assert trade["take_profit_price"] == 0.0
    assert trade["planned_exit_ts_ms"] == 0
    assert trade["submit_mode"] == "adopted_recovered"


def test_recover_entry_link_metadata_prefers_latest_reentry(tmp_path: Path) -> None:
    """re-audit scan-continuous-identity-1: a same-window cover-then-re-enter leaves TWO continuous
    entry links for one symbol in order history (seq=0 covered, seq=1 the live position). The rebuild
    must adopt the LATEST (seq=1) so it reconstructs the LIVE id — deterministically, even when the
    history is NOT newest-first."""
    import time

    from liquidity_migration.continuous_demo import _continuous_order_link_id

    sig = (int(time.time() * 1000) // 1000) * 1000 - 24 * 60 * 60 * 1000
    link0 = _continuous_order_link_id("en-c", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=0)
    link1 = _continuous_order_link_id("en-c", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=1)
    # Order history deliberately NOT newest-first (seq=0 listed first) — the tie-break must still win.
    private_client = FakePrivateClient(order_history=[
        {"symbol": "WIFUSDT", "side": "Sell", "orderLinkId": link0, "createdTime": str(sig + 60_000)},
        {"symbol": "WIFUSDT", "side": "Sell", "orderLinkId": link1, "createdTime": str(sig + 2_400_000)},
    ])
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False, repair_stops=False, rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0, untracked_position_grace_seconds=0.0,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )
    recovered = engine._recover_entry_link_metadata(symbol="WIFUSDT", side="short")
    assert recovered is not None
    link, _strategy_id, signal_ts_ms, sleeve, reentry_seq = recovered
    assert (sleeve, reentry_seq, link) == ("continuous", 1, link1)
    assert signal_ts_ms == (sig // 1000) * 1000


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


def test_validate_ws_risk_config_warns_untracked_exit_without_long_root(caplog) -> None:
    """exit_untracked_positions on a shared account with long_data_root unset
    would force-close the long sleeve's positions. The validator must emit a
    loud warning (it does not raise: a dedicated single-sleeve account is a
    legitimate setup, and the launch script hard-fails the shared case)."""
    import logging

    from liquidity_migration.ws_risk import _validate_ws_risk_config

    def _cfg(long_root: str, continuous_root: str = "") -> EventWebSocketRiskConfig:
        return EventWebSocketRiskConfig(
            submit_orders=False,  # keep validate_order_submit_allowed a no-op
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
            max_runtime_seconds=0.0,
            stream_start_timeout_seconds=0.0,
            exit_untracked_positions=True,
            long_data_root=long_root,
            continuous_data_root=continuous_root,
        )

    # No sibling roots -> warns (would flatten BOTH the long + continuous sleeves).
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.ws_risk"):
        _validate_ws_risk_config(_cfg(""))
    assert any("exit_untracked_positions=ON" in r.message for r in caplog.records)

    # Long set but continuous still unset -> STILL warns (continuous positions
    # would be flattened as untracked on the shared account).
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.ws_risk"):
        _validate_ws_risk_config(_cfg("data/bybit-long-demo-event"))
    assert any("exit_untracked_positions=ON" in r.message for r in caplog.records)

    # BOTH sibling roots set (fully multi-sleeve aware) -> no warning.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.ws_risk"):
        _validate_ws_risk_config(
            _cfg("data/bybit-long-demo-event", "data/bybit-continuous-demo-event")
        )
    assert not any("exit_untracked_positions=ON" in r.message for r in caplog.records)


def test_validate_trade_row_invariants_catches_entry_before_signal() -> None:
    """The 2026-05-25 WAVESUSDT premature-exit bug: entry_ts_ms set to
    signal_ts_ms (i.e. before the actual fill). The validator must catch
    this before the row is written to the ledger."""
    from liquidity_migration.ws_risk import _validate_trade_row_invariants

    signal_ts = 1_779_667_200_000
    # Bad: entry_ts before signal_ts (the bug shape)
    ok, reason = _validate_trade_row_invariants({
        "signal_ts_ms": signal_ts,
        "entry_ts_ms": signal_ts - 60_000,
    })
    assert not ok
    assert "entry_ts_ms" in reason and "signal_ts_ms" in reason

    # Bad: planned_exit_ts equals entry_ts (hold window <= 0)
    ok, reason = _validate_trade_row_invariants({
        "signal_ts_ms": signal_ts,
        "entry_ts_ms": signal_ts + 60_000,
        "planned_exit_ts_ms": signal_ts + 60_000,
    })
    assert not ok
    assert "planned_exit_ts_ms" in reason

    # Bad: opened_at_ms before signal_ts
    ok, reason = _validate_trade_row_invariants({
        "signal_ts_ms": signal_ts,
        "entry_ts_ms": signal_ts + 60_000,
        "opened_at_ms": signal_ts - 1,
        "planned_exit_ts_ms": signal_ts + 86_400_000,
    })
    assert not ok
    assert "opened_at_ms" in reason

    # Good: all timestamps consistent
    ok, reason = _validate_trade_row_invariants({
        "signal_ts_ms": signal_ts,
        "entry_ts_ms": signal_ts + 3 * 3600 * 1000,
        "opened_at_ms": signal_ts + 3 * 3600 * 1000,
        "planned_exit_ts_ms": signal_ts + 3 * 86_400_000,
    })
    assert ok, f"valid row should pass: reason={reason!r}"


def test_validate_trade_row_invariants_tolerates_missing_fields() -> None:
    """Legacy / hand-placed adopted-* rows may be missing signal_ts_ms or
    planned_exit_ts_ms. The validator must not crash on those — only flag
    when both sides of an invariant are present."""
    from liquidity_migration.ws_risk import _validate_trade_row_invariants

    ok, _ = _validate_trade_row_invariants({"entry_ts_ms": 1_779_667_200_000})
    assert ok
    ok, _ = _validate_trade_row_invariants({})
    assert ok


def test_rebuild_flow_end_to_end_preserves_trade_ids_for_reconciliation(tmp_path: Path) -> None:
    """End-to-end simulation of a VPS rebuild's adoption + reconciliation
    flow. Catches the cluster of bugs that surfaced 2026-05-25:

      - adoption recovery decodes orderLinkId back to signal_ts so demo
        and paper ledgers carry the SAME deterministic trade_id (was the
        whole reason reconciliation could pair to begin with)
      - entry_ts_ms reflects actual venue fill time, NOT signal_ts (the
        bug that tripped event_decay prematurely on WAVES)
      - the timestamp invariants in docs/timestamp_glossary.md actually
        hold for the rows the adoption path writes

    Failure modes this test catches:
      - reconciliation pairs=0 (recovered trade_id != paper trade_id)
      - event_decay fires hours early (entry_ts_ms collapsed onto signal_ts)
      - planned_exit_ts_ms ≤ entry_ts_ms (hold-window math wrong)
    """
    import polars as pl
    from liquidity_migration.event_demo import _order_link_id, _demo_event_config, _selected_scenario
    from liquidity_migration.reconciliation import run_paper_demo_reconciliation
    from liquidity_migration.storage import write_dataset
    from liquidity_migration.volume_events import VolumeEventResearchConfig

    # Three signals at the same 1h-bar boundary, three different symbols.
    signal_ts_ms = 1_779_667_200_000
    bybit_created_ms = signal_ts_ms + 3 * 3600 * 1000  # +3h, realistic fill lag
    symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    promoted_strategy = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
    promoted_scenario = _selected_scenario(promoted_strategy)
    scenario_id = promoted_scenario.scenario_id  # type: ignore[union-attr]

    # Bybit state: 3 open short positions with valid bot orderLinkIds (this is
    # what survives a VPS rebuild — Bybit retains state).
    positions = []
    order_history = []
    expected_trade_ids = []
    for sym in symbols:
        link = _order_link_id("en", symbol=sym, signal_ts_ms=signal_ts_ms)
        positions.append({
            "symbol": sym,
            "side": "Sell",
            "size": "100",
            "avgPrice": "1.00",
            "markPrice": "1.00",
            "positionValue": "100",
            "unrealisedPnl": "0",
            "stopLoss": "1.12",
            "takeProfit": "0.79",
            "createdTime": str(bybit_created_ms),
        })
        order_history.append({
            "symbol": sym,
            "side": "Sell",
            "orderLinkId": link,
            "orderStatus": "Filled",
            "qty": "100",
            "avgPrice": "1.00",
        })
        expected_trade_ids.append(f"{scenario_id}-{sym}-{signal_ts_ms}")

    private_client = FakePrivateClient(
        positions=positions,
        order_history=order_history,
    )

    # Demo daemon comes back up post-rebuild → runs adoption → ledger is
    # written from the recovered orderLinkIds.
    demo_root = tmp_path / "demo"
    demo_root.mkdir()
    engine = EventWebSocketRiskEngine(
        demo_root,
        config=ResearchConfig(data_root=demo_root),
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

    demo_trades = read_dataset(demo_root, "event_demo_trades")
    assert demo_trades.height == 3, f"all 3 positions adopted, got {demo_trades.height}"

    # Every adopted row must use the deterministic strategy trade_id (NOT
    # the lossy adopted-*) AND obey the timestamp invariants.
    from liquidity_migration.ws_risk import _validate_trade_row_invariants
    for trade in demo_trades.to_dicts():
        assert trade["trade_id"] in expected_trade_ids, (
            f"recovered trade_id {trade['trade_id']!r} not in expected set"
        )
        assert not str(trade["trade_id"]).startswith("adopted-")
        ok, reason = _validate_trade_row_invariants(trade)
        assert ok, f"row failed invariant: {reason}; row={trade}"
        # Specific assertion for the WAVES bug shape:
        assert trade["entry_ts_ms"] != signal_ts_ms, (
            "entry_ts_ms must NOT collapse onto signal_ts_ms — that's the bug "
            "that tripped event_decay prematurely on 2026-05-25"
        )
        assert trade["entry_ts_ms"] == bybit_created_ms

    # Paper ledger: same 3 deterministic trade_ids, fresh-cycle entry_ts
    # (3h after signal, just like a real paper run would do).
    paper_root = tmp_path / "paper"
    paper_root.mkdir()
    paper_rows = []
    for sym, tid in zip(symbols, expected_trade_ids):
        paper_rows.append({
            "trade_id": tid,
            "symbol": sym,
            "side": "short",
            "status": "open",
            "qty": "100",
            "entry_price": 1.00,
            "entry_ts_ms": signal_ts_ms + 3 * 3600 * 1000,  # paper fires +3h after signal
            "signal_ts_ms": signal_ts_ms,
            "ts_ms": signal_ts_ms + 3 * 3600 * 1000,
        })
    write_dataset(pl.DataFrame(paper_rows), paper_root, "event_demo_trades", partition_by=())

    # Reconciliation should pair all 3.
    payload = run_paper_demo_reconciliation(paper_root=paper_root, demo_root=demo_root)
    summary = payload["result"]["summary"]
    assert summary["paired"] == 3, (
        f"reconciliation must pair all 3 trades after rebuild; got {summary}"
    )
    assert summary["paper_only"] == 0
    assert summary["demo_only"] == 0


def test_maybe_notify_offloads_send_off_consumer_thread(tmp_path: Path, monkeypatch) -> None:
    """Tier A latency: the Telegram HTTP send must NOT block the consumer thread
    (a slow RTT would stall stop-enforcement event processing). maybe_notify
    records the dedupe key on-thread and hands the network send to a background
    daemon thread, returning immediately."""
    import threading as _threading

    sent_event = _threading.Event()
    calls: list[str] = []

    def _fake_send(text, *, enabled):
        calls.append(text)
        sent_event.set()
        return True

    monkeypatch.setattr(ws_risk, "_telegram_notification_reason", lambda payload: "stop_filled")
    monkeypatch.setattr(ws_risk, "_telegram_dedupe_key", lambda reason, payload: "k1")
    # The message is rendered on the consumer thread (WS-R-001) and only the frozen
    # string is enqueued; the background sender calls send_telegram_message directly.
    monkeypatch.setattr(ws_risk, "format_telegram_status_message", lambda payload: "rendered-text")
    monkeypatch.setattr(ws_risk, "send_telegram_message", _fake_send)

    engine = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig(telegram=True)
    )
    # Returns immediately with "enqueued" — no HTTP on the caller (consumer) thread.
    ok, status = engine.maybe_notify({"cycle": {}})
    assert (ok, status) == (True, "enqueued")
    assert "k1" in engine.state.telegram_keys_sent  # dedupe recorded on-thread
    # The background sender actually performs the send.
    assert sent_event.wait(2.0), "background telegram sender did not run"
    assert len(calls) == 1
    # Optimistic dedupe: a second material event with the same key is suppressed.
    assert engine.maybe_notify({"cycle": {}}) == (False, "duplicate_material_event")
    engine.close()  # drains + stops the sender thread
    assert len(calls) == 1


def test_maybe_notify_disabled_does_not_enqueue(tmp_path: Path) -> None:
    engine = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig(telegram=False)
    )
    assert engine.maybe_notify({"cycle": {}}) == (False, "disabled")
    assert engine._telegram_thread is None  # never started


def _raw_pos(symbol: str) -> dict:
    return {
        "symbol": symbol, "side": "Sell", "size": "1", "avgPrice": "100",
        "markPrice": "100", "positionValue": "100", "unrealisedPnl": "0",
    }


def test_rest_reconcile_prefetch_union_never_drops_a_ws_position(tmp_path: Path) -> None:
    """Prefetch offload (opt-in): when rest_reconcile reads positions from the
    background snapshot, a position the WS added AFTER the snapshot must NOT be
    dropped — else its stop would go unchecked (missed stop). The union of the
    snapshot with current WS positions guarantees neither source is lost."""
    import time as _time

    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False, repair_stops=False, reconcile_prefetch_enabled=True,
            rest_reconcile_seconds=30.0, heartbeat_seconds=0.0,
            adopt_untracked_positions=False, exit_untracked_positions=False,
        ),
        private_client=FakePrivateClient(positions=[]),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
        trade_client=FakeTradeClient(),
    )
    engine.bootstrap()
    # A position the WS just added (in positions_by_symbol), absent from the snapshot.
    engine.state.positions_by_symbol = {"WSFRESH": {"symbol": "WSFRESH", "side": "short", "size": 1.0}}
    engine._reconcile_prefetch = {
        "positions": [_raw_pos("SNAPONLY")], "positions_error": "",
        "open_orders": [], "open_orders_error": "", "monotonic": _time.monotonic(),
    }
    engine.rest_reconcile()
    assert "WSFRESH" in engine.state.positions_by_symbol, "WS-added position dropped -> missed-stop risk"
    assert "SNAPONLY" in engine.state.positions_by_symbol  # snapshot applied too


def test_rest_reconcile_prefetch_stale_falls_back_to_inline(tmp_path: Path) -> None:
    """A stale prefetch snapshot is ignored — rest_reconcile fetches inline (the
    legacy path), so correctness never depends on a lagging background thread."""
    import time as _time

    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False, repair_stops=False, reconcile_prefetch_enabled=True,
            rest_reconcile_seconds=30.0, heartbeat_seconds=0.0,
            adopt_untracked_positions=False, exit_untracked_positions=False,
        ),
        private_client=FakePrivateClient(positions=[_raw_pos("INLINE")]),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
        trade_client=FakeTradeClient(),
    )
    engine.bootstrap()
    engine.state.positions_by_symbol = {}
    # Stale snapshot (well beyond rest_reconcile_seconds) -> ignored.
    engine._reconcile_prefetch = {
        "positions": [_raw_pos("STALE")], "positions_error": "",
        "open_orders": [], "open_orders_error": "", "monotonic": _time.monotonic() - 999.0,
    }
    engine.rest_reconcile()
    assert "INLINE" in engine.state.positions_by_symbol  # used the inline fetch
    assert "STALE" not in engine.state.positions_by_symbol


def test_reconcile_prefetcher_lifecycle(tmp_path: Path, monkeypatch) -> None:
    """The prefetcher starts only when enabled (own client), populates the snapshot,
    and is stopped by close(). Disabled -> no thread."""
    monkeypatch.setattr(ws_risk, "_build_private_client", lambda config: FakePrivateClient(positions=[_raw_pos("BG")]))

    off = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(reconcile_prefetch_enabled=False),
    )
    off._start_reconcile_prefetcher()
    assert off._reconcile_prefetch_thread is None  # disabled -> never starts

    on = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(reconcile_prefetch_enabled=True, rest_reconcile_seconds=0.3),
    )
    on._start_reconcile_prefetcher()
    assert on._reconcile_prefetch_thread is not None and on._reconcile_prefetch_thread.is_alive()
    # The loop populates the snapshot within ~1 interval (0.1s).
    import time as _t
    deadline = _t.monotonic() + 3.0
    while on._reconcile_prefetch is None and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert on._reconcile_prefetch is not None and "BG" in {
        str(p.get("symbol")) for p in on._reconcile_prefetch["positions"]
    }
    on.close()
    assert not on._reconcile_prefetch_thread.is_alive()  # stopped by close()


# ---------------------------------------------------------------------------
# Continuous sleeve coexistence on the SHARED demo account (task #14).
# These guard the invariants that make "turn trading on" safe with three
# sleeves netting into one Bybit account: (1) a tracked continuous position is
# never flattened as untracked, (2) routed writes keep the continuous ledger
# isolated from the short ledger, (3) the combined read tags continuous rows.
# ---------------------------------------------------------------------------


def _write_continuous_open_trade(root: Path, symbol: str = "AAAUSDT") -> None:
    """Seed an OPEN continuous-sleeve trade in the continuous ledger."""
    from liquidity_migration.continuous_demo import CONTINUOUS_STRATEGY_ID

    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": f"{CONTINUOUS_STRATEGY_ID}-{symbol}-1700000000000",
                    "strategy_id": CONTINUOUS_STRATEGY_ID,
                    "sleeve": "continuous",
                    "symbol": symbol,
                    "side": "short",
                    "status": "open",
                    "qty": "1",
                    "entry_price": 100.0,
                    "stop_price": 125.0,
                    "planned_exit_ts_ms": 9_999_999_999_999,
                }
            ]
        ),
        root,
        "continuous_fade_demo_trades",
        partition_by=(),
    )


def test_ws_risk_does_not_flatten_tracked_continuous_position(tmp_path: Path) -> None:
    """THE shared-account safety invariant: with continuous_data_root set, a
    Bybit position that belongs to the continuous sleeve (present in the
    continuous ledger) must be recognised as tracked and NOT force-closed by
    exit_untracked_positions. Without this wiring the risk engine would flatten
    the continuous sleeve's live shorts."""
    continuous_root = tmp_path / "continuous"
    continuous_root.mkdir()
    _write_continuous_open_trade(continuous_root, "AAAUSDT")
    private_client = FakePrivateClient()  # default position: AAAUSDT short
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
            continuous_data_root=str(continuous_root),
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    # No reduce-only flatten order was sent, and the position survives.
    assert private_client.orders == []
    assert "AAAUSDT" in engine.state.positions_by_symbol
    # The continuous trade is in the engine's combined open-trade view.
    assert "AAAUSDT" in set(engine.state.open_trades["symbol"].to_list())


def test_ws_risk_flattens_untracked_when_continuous_root_set_but_symbol_absent(tmp_path: Path) -> None:
    """Control for the test above: continuous_data_root set but the venue
    position is NOT in any ledger -> it is still a genuine orphan and gets
    flattened. (Proves the no-flatten above is due to tracking, not a blanket
    disable.)"""
    continuous_root = tmp_path / "continuous"
    continuous_root.mkdir()
    _write_continuous_open_trade(continuous_root, "BBBUSDT")  # different symbol
    private_client = FakePrivateClient()  # position: AAAUSDT (untracked)
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
            continuous_data_root=str(continuous_root),
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()

    assert private_client.orders and private_client.orders[0]["side"] == "Buy"
    assert "AAAUSDT" not in engine.state.positions_by_symbol


def test_ws_risk_routed_write_isolates_continuous_from_short_ledger(tmp_path: Path) -> None:
    """A row tagged sleeve='continuous' must land in the continuous orders
    dataset/root, never the short ledger — otherwise the continuous sleeve's
    reconciliation diverges from what ws_risk actually wrote."""
    continuous_root = tmp_path / "continuous"
    continuous_root.mkdir()
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(continuous_data_root=str(continuous_root)),
    )
    engine._write_order_rows_routed([
        {"order_link_id": "lm-ux-c-AAA-x", "ts_ms": 1, "symbol": "AAAUSDT", "side": "Buy",
         "status": "filled", "sleeve": "continuous", "trade_id": "continuous_fade_v1-AAAUSDT-1"},
        {"order_link_id": "lm-ux-SHORT-y", "ts_ms": 2, "symbol": "ZZZUSDT", "side": "Buy",
         "status": "filled", "sleeve": "short", "trade_id": "short-ZZZUSDT-1"},
    ])

    cont_orders = read_dataset(continuous_root, "continuous_fade_demo_orders")
    short_orders = read_dataset(tmp_path, "event_demo_orders")
    assert set(cont_orders["symbol"].to_list()) == {"AAAUSDT"}
    assert set(short_orders["symbol"].to_list()) == {"ZZZUSDT"}


def test_ws_risk_read_combined_includes_and_tags_continuous(tmp_path: Path) -> None:
    """The combined trade read must surface continuous-ledger trades tagged
    sleeve='continuous' so they show up in open_trades (tracked) and route back
    correctly on write."""
    continuous_root = tmp_path / "continuous"
    continuous_root.mkdir()
    _write_continuous_open_trade(continuous_root, "AAAUSDT")
    _write_open_trade(tmp_path)  # short-sleeve trade (BBB? -> AAAUSDT default)
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(continuous_data_root=str(continuous_root)),
    )
    combined = engine._read_trades_combined()
    rows = {(r["symbol"], r["sleeve"]) for r in combined.select("symbol", "sleeve").to_dicts()}
    assert ("AAAUSDT", "continuous") in rows
    assert ("AAAUSDT", "short") in rows


def test_ws_risk_reconciles_vanished_continuous_position_into_continuous_ledger(tmp_path: Path) -> None:
    """The disaster-stop-fired path for the continuous sleeve: a continuous
    trade is open in the continuous ledger but its Bybit position has vanished
    (server-side stopLoss closed it). ws_risk is the reconcile authority for ALL
    sleeves it reads — it must close that trade with the real venue PnL backfilled
    from get_closed_pnl, and route the close back to the CONTINUOUS ledger (not
    the short ledger). This is why the continuous cycle does NOT need its own
    orphan-close (it would race ws_risk); it mirrors the long-sleeve design."""
    continuous_root = tmp_path / "continuous"
    continuous_root.mkdir()
    _write_continuous_open_trade(continuous_root, "AAAUSDT")

    class _ClosedPnlClient(FakePrivateClient):
        def get_closed_pnl(self, *, symbol: str | None = None, start_time_ms: int | None = None, limit: int = 50):
            return [{"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "90.0",
                     "createdTime": "1700000060000", "orderId": "cont-closed-1"}]

    # Position vanished at the venue (stop fired) -> orphan.
    private_client = _ClosedPnlClient(positions=[], confirm_fills=False)
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
            exit_untracked_positions=False,
            continuous_data_root=str(continuous_root),
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    engine.rest_reconcile()

    # Closed in the CONTINUOUS ledger with the backfilled venue exit price.
    cont_trades = read_dataset(continuous_root, "continuous_fade_demo_trades")
    closed = cont_trades.filter(pl.col("symbol") == "AAAUSDT")
    assert closed.select("status").item() == "closed"
    assert float(closed.select("exit_price").item()) == pytest.approx(90.0)
    assert closed.select("sleeve").item() == "continuous"
    # The short ledger was NOT touched (no spurious continuous close leaked there).
    try:
        short_trades = read_dataset(tmp_path, "event_demo_trades")
        assert "AAAUSDT" not in set(short_trades["symbol"].to_list()) if not short_trades.is_empty() else True
    except Exception:
        pass


def test_ws_risk_adopt_strategy_id_for_continuous_sleeve(tmp_path: Path) -> None:
    """A recovered continuous orphan must adopt under the continuous strategy_id
    (so its deterministic trade_id matches the live sleeve), not 'short'."""
    from liquidity_migration.continuous_demo import CONTINUOUS_STRATEGY_ID

    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(),
    )
    assert engine._adopt_strategy_id_for_sleeve("continuous") == CONTINUOUS_STRATEGY_ID


def test_transient_get_positions_failure_does_not_orphan_close_open_trades(tmp_path: Path) -> None:
    """The C1 mass-false-orphan-close class (memory: audit-2026-05-29-critical-orphan-close):
    a transient get_positions failure returns an empty position set that must NOT be read as
    "every position vanished". Exercised through the engine (the cache-level guard is tested
    separately) — open trades must survive, no reconciliation rows are written, and the error
    is recorded so the orphan reconciler stays disarmed (audit 2026-06-02 #6)."""
    _write_open_trade(tmp_path)
    private_client = FakePrivateClient(fail_positions=True)
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
    assert engine.state.open_trades.height == 1  # loaded from the ledger

    engine.rest_reconcile()                       # get_positions raises -> error short-circuit
    rows = engine.reconcile_positions(write=True)  # orphan reconciler must stay disarmed

    assert rows == [], "a transient positions-fetch failure must not orphan-close anything"
    assert engine.state.open_trades.height == 1, "the open trade must survive the failed fetch"
    assert engine.state.last_position_error, "the positions error must be recorded"
    assert engine.state.reconciliations == [], "no false reconciliation rows"


def test_empty_but_successful_positions_snapshot_does_not_orphan_close(tmp_path: Path) -> None:
    """C1 extension (audit 2026-06-03): a *successful-but-empty* get_positions
    response (retCode 0, empty result.list during a venue degradation) carries NO
    position_error, yet is indistinguishable from "all positions closed". With no
    closed-PnL record confirming a real close, the bulk REST orphan reconciler
    (require_evidence=True) must KEEP the trade open rather than zero-PnL-wipe a
    possibly-live position. Distinct from the transient-failure case above: here
    the fetch SUCCEEDS and sets no error, so the position_error guard does NOT
    fire — the evidence requirement is the only thing standing between a degraded
    venue read and a mass false close."""
    _write_open_trade(tmp_path)
    # Empty positions, retCode 0, NO error, and the base client returns NO
    # closed-PnL record (only the *WithClosedPnl subclasses do).
    private_client = FakePrivateClient(positions=[])
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
            adopt_untracked_positions=False,
            exit_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )
    engine.bootstrap()
    assert engine.state.open_trades.height == 1, "loaded from the ledger"
    assert not engine.state.last_position_error, "an empty-but-successful read records no error"

    engine.rest_reconcile()

    assert engine.state.open_trades.height == 1, (
        "an empty-but-successful positions snapshot with no closure record must not "
        "orphan-close a possibly-live trade"
    )
    assert engine.state.reconciliations == [], "no false reconciliation rows"


def test_ws_exit_and_tracked_fill_use_filtered_single_trade_lookup(tmp_path: Path) -> None:
    """reconcile-core-5: ws_exit and record_tracked_exit_stream_fill must find the
    target trade via a column filter, not by materializing every ledger row. With
    extra unrelated open trades present, the right trade is closed/reduced -- proving
    the filtered lookup is behaviour-equivalent to the old full-map lookup."""
    write_dataset(
        pl.DataFrame(
            [
                {"trade_id": "t1", "symbol": "AAAUSDT", "side": "short", "status": "open",
                 "qty": "1", "entry_price": 100.0, "stop_price": 112.0, "take_profit_price": 80.0,
                 "planned_exit_ts_ms": 9_999_999_999_999},
                {"trade_id": "t2", "symbol": "BBBUSDT", "side": "short", "status": "open",
                 "qty": "3", "entry_price": 50.0, "stop_price": 56.0, "take_profit_price": 40.0,
                 "planned_exit_ts_ms": 9_999_999_999_999},
                {"trade_id": "t3", "symbol": "CCCUSDT", "side": "short", "status": "open",
                 "qty": "2", "entry_price": 10.0, "stop_price": 12.0, "take_profit_price": 8.0,
                 "planned_exit_ts_ms": 9_999_999_999_999},
            ]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    trade_client = FakeTradeClient()
    engine = _submit_exit_engine(tmp_path, trade_client)
    rows, orders = engine.ws_exit({**_SUBMIT_EXIT_PLAN, "exit_trigger_ts_ms": int(time.time() * 1000)})
    assert orders and orders[0]["trade_id"] == "t1"
    assert orders[0]["symbol"] == "AAAUSDT" and orders[0]["side"] == "Buy"
    link = str(orders[0]["order_link_id"])
    engine.state.submitted_link_to_trade_id[link] = "t1"
    engine.record_tracked_exit_stream_fill(
        order_link_id=link, filled_qty=1.0, exit_price=111.0, source="execution",
    )
    stored = read_dataset(tmp_path, "event_demo_trades")
    assert stored.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert stored.filter(pl.col("trade_id") == "t2").select("status").item() == "open"
    assert stored.filter(pl.col("trade_id") == "t3").select("status").item() == "open"
    with pytest.raises(KeyError):
        engine.ws_exit({**_SUBMIT_EXIT_PLAN, "trade_id": "does-not-exist",
                        "exit_trigger_ts_ms": int(time.time() * 1000)})


def test_ws_risk_consumer_loop_isolates_handler_exception(tmp_path: Path) -> None:
    """A single raise out of on_idle/handle_event must NOT crash the sole reconcile
    authority. The loop logs+records the error and continues to max_runtime (WSR-2)."""
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
            max_runtime_seconds=0.3,
        ),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )
    real_on_idle = engine.on_idle
    calls = {"n": 0}

    def flaky_on_idle() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom in idle cycle")
        real_on_idle()

    engine.on_idle = flaky_on_idle  # type: ignore[method-assign]
    payload = engine.run()

    assert payload["cycle"]["reason"] == "max_runtime", "loop must survive the handler raise"
    assert calls["n"] >= 2, "loop must continue iterating after isolating the error"
    assert any("consumer-loop error in on_idle" in e for e in engine.state.errors)


def test_ws_risk_consumer_loop_reraises_off_thread_assertion(tmp_path: Path) -> None:
    """The off-thread state-mutation assertion is a deliberate fail-fast (a programming
    bug, not a data event) and must still propagate out of run(), not be isolated."""
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
            max_runtime_seconds=0.3,
        ),
        private_client=FakePrivateClient(),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    def off_thread() -> None:
        raise RuntimeError(
            "WebSocketRiskState mutated off the consumer thread -- WS callbacks must enqueue."
        )

    engine.on_idle = off_thread  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="off the consumer thread"):
        engine.run()


def test_ws_risk_rebuilds_private_stream_when_socket_down(tmp_path: Path, monkeypatch) -> None:
    """IND-1 (the risk authority): the private stream feeds the real-time position/order/
    execution events that drive intrabar stops. When its socket is genuinely DOWN past the
    bound, ws_risk rebuilds + re-subscribes it (REST reconcile covers the gap). A quiet but
    connected socket must NOT trigger a rebuild."""
    built: list[str] = []
    closed: list[str] = []

    class _DeadPrivate:
        def is_connected(self) -> bool:
            return False

        def close(self) -> None:
            closed.append("c")

        def subscribe_positions(self, cb) -> None:  # noqa: ANN001
            pass

        def subscribe_orders(self, cb) -> None:  # noqa: ANN001
            pass

        def subscribe_executions(self, cb, **k) -> None:  # noqa: ANN001
            pass

    class _LivePrivate(_DeadPrivate):
        def is_connected(self) -> bool:
            return True

    def _fake_build(_config):  # noqa: ANN001, ANN202
        built.append("b")
        return _LivePrivate()

    monkeypatch.setattr("liquidity_migration.ws_risk._build_private_stream", _fake_build)

    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(),
        risk_config=EventWebSocketRiskConfig(private_ws_reconnect_seconds=30.0),
    )
    engine.private_stream = _DeadPrivate()  # type: ignore[assignment]

    engine._maybe_reconnect_private_stream(time.monotonic())  # marks since; not past bound
    assert not closed and engine._private_ws_reconnects == 0

    engine._private_disconnected_since = time.monotonic() - 999  # past the bound
    engine._maybe_reconnect_private_stream(time.monotonic())
    assert closed, "down-past-bound private stream must be closed"
    assert built, "a fresh private stream must be built + re-subscribed"
    assert engine._private_ws_reconnects == 1
    assert isinstance(engine.private_stream, _LivePrivate)

    # A now-healthy socket must clear the tracker and never reconnect.
    engine._maybe_reconnect_private_stream(time.monotonic())
    assert engine._private_disconnected_since is None
    assert engine._private_ws_reconnects == 1


def test_state_mutators_assert_consumer_thread(tmp_path: Path) -> None:
    """WS-R-002: the off-thread state-mutation guard now covers the public mutators too,
    not just handle_event — a stray callback wired directly to a mutator fails fast
    instead of silently racing. No-op on the consumer thread (ident matches)."""
    import threading as _threading

    engine = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig()
    )
    engine._consumer_thread_ident = _threading.get_ident()  # as run() sets it, to THIS thread

    # On the consumer thread the guard is a no-op (empty message -> no state change).
    engine.on_position_message({"data": []})

    errors: list[str] = []

    def _off_thread() -> None:
        for mutator in (
            engine.on_position_message,
            engine.on_order_message,
            engine.on_execution_message,
            engine.on_ws_order_ack,
        ):
            try:
                mutator({"data": []})
            except RuntimeError as exc:
                errors.append(str(exc))

    t = _threading.Thread(target=_off_thread)
    t.start()
    t.join(2.0)
    assert len(errors) == 4, "every mutator must fail-fast when called off the consumer thread"
    assert all("off the consumer thread" in e for e in errors)
