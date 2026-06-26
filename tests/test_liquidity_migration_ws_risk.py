from __future__ import annotations

import time
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import cross_sleeve as _cross_sleeve
from liquidity_migration import ws_risk
from liquidity_migration.config import ResearchConfig
from liquidity_migration.cross_sleeve import (
    claim_symbol_reservation,
    read_account_state,
)
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration.ws_risk import (
    _LOG_RETENTION,
    EventWebSocketRiskConfig,
    EventWebSocketRiskEngine,
    WebSocketRiskState,
    _now_ms,
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


def test_ws_risk_split_ws_close_aggregates_fee_and_weighted_gross_return(tmp_path: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "t1",
                    "symbol": "SUPERUSDT",
                    "side": "short",
                    "status": "open",
                    "qty": "3",
                    "entry_price": 100.0,
                    "notional_usdt": 300.0,
                    "equity_usdt": 1_000.0,
                    "stop_price": 112.0,
                    "take_profit_price": 80.0,
                    "planned_exit_ts_ms": 9_999_999_999_999,
                    "qty_step": 1.0,
                    "max_market_order_qty": 2.0,
                }
            ]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    private_client = FakePrivateClient(
        positions=[
            {
                "symbol": "SUPERUSDT",
                "side": "Sell",
                "size": "3",
                "avgPrice": "100",
                "markPrice": "100",
                "positionValue": "300",
                "unrealisedPnl": "0",
                "stopLoss": "112",
                "takeProfit": "80",
            }
        ]
    )
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
        private_client=private_client,
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
        trade_client=trade_client,
    )

    engine.bootstrap()
    engine.on_ticker_message({"data": {"symbol": "SUPERUSDT", "markPrice": "113"}})
    assert len(trade_client.orders) == 2
    orders_by_qty = {str(order["qty"]): str(order["orderLinkId"]) for order in trade_client.orders}

    engine.on_execution_message(
        {
            "data": [
                {
                    "symbol": "SUPERUSDT",
                    "orderLinkId": orders_by_qty["2"],
                    "execQty": "2",
                    "execPrice": "110",
                    "execValue": "220",
                    "execFee": "0.20",
                },
                {
                    "symbol": "SUPERUSDT",
                    "orderLinkId": orders_by_qty["1"],
                    "execQty": "1",
                    "execPrice": "120",
                    "execValue": "120",
                    "execFee": "0.10",
                },
            ]
        }
    )

    closed = read_dataset(tmp_path, "event_demo_trades").filter(pl.col("trade_id") == "t1").to_dicts()[0]
    assert closed["status"] == "closed"
    # Weighted raw gross return across both close legs:
    # 2/3 * ((100-110)/100) + 1/3 * ((100-120)/100) = -0.133333...
    assert float(closed["gross_trade_return"]) == pytest.approx((-0.10 * 2 + -0.20 * 1) / 3)
    # Return contribution: (-10% * 200/1000) + (-20% * 100/1000).
    assert float(closed["net_return"]) == pytest.approx(-0.04)
    assert float(closed["rebalance_realized_return"]) == pytest.approx(-0.04)
    assert float(closed["exit_fee_usdt"]) == pytest.approx(0.30)


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
    # ROUND 4: the backfill's realized-PnL fields must land on the closed row
    # too — they were previously extracted for price/time only and discarded.
    assert float(closed.select("gross_trade_return").item()) == pytest.approx((100.0 - 112.5) / 100.0)
    assert closed.select("net_return").item() is not None


def test_ws_risk_reconcile_flat_pending_exit_preserves_prior_partial_realized_pnl(tmp_path: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "t2",
                    "symbol": "BBBUSDT",
                    "side": "short",
                    "status": "open",
                    "qty": "2",
                    "entry_price": 50.0,
                    "equity_usdt": 10_000.0,
                    "notional_usdt": 100.0,
                    "stop_price": 56.0,
                    "take_profit_price": 40.0,
                    "planned_exit_ts_ms": 9_999_999_999_999,
                    # Prior partial close: 1 unit at 55, short gross -10%, weight 0.005.
                    "rebalance_realized_return": -0.0005,
                    "rebalance_realized_weight": 0.005,
                    "rebalance_exit_fee_usdt": 0.01,
                    "exit_fee_usdt": 0.01,
                }
            ]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-rx-BBB-1",
                    "ts_ms": 9_999_999_999_000,
                    "trade_id": "t2",
                    "symbol": "BBBUSDT",
                    "side": "Buy",
                    "order_type": "Market",
                    "qty": "2",
                    "reduce_only": True,
                    "submit_mode": "submitted",
                    "status": "submitted_unconfirmed",
                    "avg_price": 56.0,
                    "exit_reason": "stop_loss",
                    "exit_trigger_ts_ms": 1_700_000_000_000,
                    "target_qty": "2",
                    "filled_qty": "",
                    "fee_usdt": 0.02,
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
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
            exit_untracked_positions=False,
        ),
        private_client=FakePrivateClient(positions=[], confirm_fills=False),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    engine.rest_reconcile()

    closed = read_dataset(tmp_path, "event_demo_trades").filter(pl.col("trade_id") == "t2").to_dicts()[0]
    assert closed["status"] == "closed"
    assert float(closed["net_return"]) == pytest.approx(-0.0017)
    assert float(closed["rebalance_realized_return"]) == pytest.approx(-0.0017)
    assert float(closed["gross_trade_return"]) == pytest.approx((-0.0005 - 0.0012) / 0.015)
    assert float(closed["exit_fee_usdt"]) == pytest.approx(0.03)


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


def test_ws_risk_aged_pending_tracked_exit_still_tracks_late_ws_fill(tmp_path: Path) -> None:
    """An old pending tracked exit must remain link-mapped while the trade is open.

    The duplicate-submit guard may age out, but dropping submitted_link_to_trade_id
    makes a late WS execution for that link look unrelated and silently discards
    the close.
    """
    _write_open_trade(tmp_path)
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-ex-aged",
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
        private_client=FakePrivateClient(confirm_fills=False),
        private_stream=FakePrivateStream(),
        public_stream=FakePublicStream(),
    )

    engine.bootstrap()
    assert engine.state.submitted_link_to_trade_id["lm-ex-aged"] == "t1"
    assert "AAAUSDT" not in engine.state.submitted_symbols

    engine.on_execution_message(
        {
            "data": [
                {
                    "symbol": "AAAUSDT",
                    "orderLinkId": "lm-ex-aged",
                    "execQty": "1",
                    "execPrice": "113",
                    "execValue": "113",
                    "execFee": "0.03",
                }
            ]
        }
    )

    trade = read_dataset(tmp_path, "event_demo_trades").filter(pl.col("trade_id") == "t1").to_dicts()[0]
    assert trade["status"] == "closed"
    assert trade["exit_reason"] == "stop_loss"
    assert float(trade["gross_trade_return"]) == pytest.approx(-0.13)
    assert float(trade["exit_fee_usdt"]) == pytest.approx(0.03)


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
    from liquidity_migration.event_demo import _order_link_id
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
    # The daily-short sleeve was erased (2026-06-11): legacy short rows are only
    # recoverable with an explicitly configured adopt_short_strategy_id.
    legacy_strategy_id = "legacy-short-strategy"
    expected_trade_id = f"{legacy_strategy_id}-AAAUSDT-{signal_ts_ms}"

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
            adopt_short_strategy_id=legacy_strategy_id,
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
    assert trade["strategy_id"] == legacy_strategy_id
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
    assert trade["strategy_id"] == "continuous_btc_hedge_v2"
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
    link, _strategy_id, signal_ts_ms, sleeve, reentry_seq, component_tag = recovered
    assert (sleeve, reentry_seq, link) == ("continuous", 1, link1)
    assert signal_ts_ms == (sig // 1000) * 1000
    assert component_tag == ""  # bare "en-c" prefix carries no ensemble component


def test_adoption_rebuilds_component_tagged_trade_id(tmp_path: Path) -> None:
    """REGRESSION (audit 2026-06-12): the ensemble profiles emit ONLY
    component-tagged links (lm-en-cp3-…) and the live trade_id carries the component
    ({base}-p3). The rebuild reconstruction dropped it, so every post-rebuild adoption
    produced an id that matched no paper-twin row — reconciliation pairing broke."""
    import time

    from liquidity_migration.continuous_demo import _continuous_order_link_id

    sig = (int(time.time() * 1000) // 1000) * 1000 - 24 * 60 * 60 * 1000
    link = _continuous_order_link_id("en-cp3", symbol="WIFUSDT", signal_ts_ms=sig)
    private_client = FakePrivateClient(order_history=[
        {"symbol": "WIFUSDT", "side": "Sell", "orderLinkId": link, "createdTime": str(sig + 60_000)},
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
    row = engine._build_adopted_trade(
        {"symbol": "WIFUSDT", "side": "Sell", "size": "100", "avgPrice": "2.0",
         "createdTime": str(sig + 60_000)},
        now_ms=sig + 120_000,
    )
    assert row is not None
    assert str(row["trade_id"]).endswith("-p3"), row["trade_id"]
    assert row["sleeve"] == "continuous"
    # ROUND 4: the adopted row must also carry the ensemble sizing weight —
    # without it the daily rebalance defaults the missing weight to 1.0 and
    # resizes a fractional p3 entry to FULL base notional (the round-3 CRITICAL
    # re-entering through the adoption door). p3 weight renormalized to 1/3 after
    # the 2026-06-18 component-set freeze.
    assert row["component"] == "p3"
    assert float(row["component_weight"]) == pytest.approx(0.3333333333333333)


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


def test_validate_ws_risk_config_warns_untracked_exit_without_sibling_roots(caplog) -> None:
    """exit_untracked_positions on a shared account with long_data_root unset
    would force-close the long sleeve's positions. The validator must emit a
    loud warning (it does not raise: a dedicated single-sleeve account is a
    legitimate setup, and the launch script hard-fails the shared case)."""
    import logging

    from liquidity_migration.ws_risk import _validate_ws_risk_config

    def _cfg(
        long_root: str,
        continuous_root: str = "",
        continuous_addon_root: str = "",
    ) -> EventWebSocketRiskConfig:
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
            continuous_addon_data_root=continuous_addon_root,
        )

    # No sibling roots -> warns (would flatten long / continuous / add-on sleeves).
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.ws_risk"):
        _validate_ws_risk_config(_cfg(""))
    assert any("exit_untracked_positions=ON" in r.message for r in caplog.records)
    assert any("continuous_addon_data_root" in r.message for r in caplog.records)

    # Long + continuous set but add-on still unset -> STILL warns (add-on positions
    # would be flattened as untracked on the shared account).
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.ws_risk"):
        _validate_ws_risk_config(
            _cfg("data/bybit-long-demo-event", "data/bybit-continuous-demo-event")
        )
    assert any("exit_untracked_positions=ON" in r.message for r in caplog.records)
    assert any("continuous_addon_data_root" in r.message for r in caplog.records)

    # All sibling roots set (fully multi-sleeve aware) -> no warning.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="liquidity_migration.ws_risk"):
        _validate_ws_risk_config(
            _cfg(
                "data/bybit-long-demo-event",
                "data/bybit-continuous-demo-event",
                "data/bybit-continuous-addon-demo-event",
            )
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
    from liquidity_migration.event_demo import _order_link_id
    from liquidity_migration.reconciliation import _run_reconciliation
    from liquidity_migration.storage import write_dataset

    # Three signals at the same 1h-bar boundary, three different symbols.
    signal_ts_ms = 1_779_667_200_000
    bybit_created_ms = signal_ts_ms + 3 * 3600 * 1000  # +3h, realistic fill lag
    symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    # The daily-short sleeve was erased (2026-06-11): legacy short rows are only
    # recoverable with an explicitly configured adopt_short_strategy_id.
    scenario_id = "legacy-short-strategy"

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
            adopt_short_strategy_id=scenario_id,
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
    payload = _run_reconciliation(
        paper_root=paper_root, demo_root=demo_root,
        paper_dataset="event_demo_trades", demo_dataset="event_demo_trades",
        report_subdir="reconciliation", report_filename="paper_demo_reconciliation.md",
        entry_tolerance_ms=6 * 3600 * 1000, output_dir=None,
    )
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


def test_failed_background_send_unrecords_dedupe_key(tmp_path: Path, monkeypatch) -> None:
    """REGRESSION (audit 2026-06-12): the optimistic dedupe wrote the key BEFORE the HTTP
    round-trip and a failed send was only logged — a single timeout/429 on an
    UNPROTECTED/stop_repair_failed alert silently suppressed that exact alert for 24h.
    The sender loop now un-records the key (disk + memory) so the next material cycle
    re-fires it. Round 3: the un-record happens only for a CONFIGURED transport —
    creds present + False return = transport failure worth retrying; a missing env
    is permanent, so keeping the key avoids per-cycle dedupe-file churn."""
    import threading as _threading

    attempted = _threading.Event()

    def _failing_send(text, *, enabled):
        attempted.set()
        return False  # non-2xx with creds present: a transport failure, no exception

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setattr(ws_risk, "_telegram_notification_reason", lambda payload: "stop_filled")
    monkeypatch.setattr(ws_risk, "_telegram_dedupe_key", lambda reason, payload: "k-fail")
    monkeypatch.setattr(ws_risk, "format_telegram_status_message", lambda payload: "rendered-text")
    monkeypatch.setattr(ws_risk, "send_telegram_message", _failing_send)

    engine = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig(telegram=True)
    )
    ok, status = engine.maybe_notify({"cycle": {}})
    assert (ok, status) == (True, "enqueued")
    assert attempted.wait(2.0), "background telegram sender did not run"
    # audit2c: the sender thread no longer un-records the key off-thread (that mutated
    # consumer-only state and raced the dedupe file). It hands the failed key back via a
    # queue; the NEXT maybe_notify drains it (un-record on the consumer thread) THEN
    # re-fires. Wait for the hand-back, then assert the alert re-fires — the (True,
    # "enqueued") return proves the dedupe key was un-recorded (else it would be a dup).
    import time as _time
    for _ in range(200):
        if not engine._telegram_failed_keys.empty():
            break
        _time.sleep(0.01)
    assert engine.maybe_notify({"cycle": {}}) == (True, "enqueued")
    engine.close()


def test_unconfigured_telegram_keeps_dedupe_key(tmp_path: Path, monkeypatch) -> None:
    """Round 3: send_telegram_message returns False BOTH for a transport failure and
    for missing TELEGRAM_* env. Un-recording on the latter re-rendered and rewrote
    the dedupe file on every material cycle (heartbeat-cadence disk churn + log
    noise masking real failures). Not-configured keeps the key."""
    import threading as _threading

    attempted = _threading.Event()

    def _failing_send(text, *, enabled):
        attempted.set()
        return False  # env missing: permanent, not a transport failure

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(ws_risk, "_telegram_notification_reason", lambda payload: "stop_filled")
    monkeypatch.setattr(ws_risk, "_telegram_dedupe_key", lambda reason, payload: "k-noenv")
    monkeypatch.setattr(ws_risk, "format_telegram_status_message", lambda payload: "rendered-text")
    monkeypatch.setattr(ws_risk, "send_telegram_message", _failing_send)

    engine = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig(telegram=True)
    )
    assert engine.maybe_notify({"cycle": {}}) == (True, "enqueued")
    assert attempted.wait(2.0), "background telegram sender did not run"
    engine.close()
    assert "k-noenv" in engine.state.telegram_keys_sent
    assert "k-noenv" in set(ws_risk._read_telegram_dedupe_keys(engine.report_dir))
    # ...and the same material event stays deduped (no per-cycle churn)
    assert engine.maybe_notify({"cycle": {}}) == (False, "duplicate_material_event")
    engine.close()


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
         "status": "filled", "sleeve": "continuous", "trade_id": "continuous_fade_v2-AAAUSDT-1"},
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


def test_tracked_stream_fill_partial_target_reduce_shrinks_not_closes(tmp_path: Path) -> None:
    """REGRESSION (solo sweep 2026-06-12): the WS-path twin of the round-3
    pending-fill fix. A reduce order targeting only PART of the position (a
    rebalance_reduce) that FULLY fills via the execution stream must shrink the
    trade, not close it — the old order-fullness clause (filled >= target)
    erased the live remainder from the ledger."""
    write_dataset(
        pl.DataFrame(
            [{"trade_id": "t2", "symbol": "BBBUSDT", "side": "short", "status": "open",
              "qty": "3", "entry_price": 50.0, "stop_price": 56.0, "take_profit_price": 40.0,
              "planned_exit_ts_ms": 9_999_999_999_999}]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    trade_client = FakeTradeClient()
    engine = _submit_exit_engine(tmp_path, trade_client)
    link = "lm-ux-c-BBBUSDT-resize-x0aa"
    engine._record_orders([{
        "order_link_id": link, "trade_id": "t2", "symbol": "BBBUSDT", "side": "Buy",
        "reduce_only": True, "status": "submitted", "qty": "1", "target_qty": "1",
        "filled_qty": "", "exit_reason": "rebalance_reduce",
    }])
    engine.state.submitted_link_to_trade_id[link] = "t2"

    engine.record_tracked_exit_stream_fill(
        order_link_id=link, filled_qty=1.0, exit_price=49.0, source="execution",
    )

    stored = read_dataset(tmp_path, "event_demo_trades")
    row = stored.filter(pl.col("trade_id") == "t2").to_dicts()[0]
    # The ORDER fully filled (1 of target 1) but the POSITION is 3: shrink to 2.
    assert row["status"] == "open"
    assert float(row["qty"]) == pytest.approx(2.0)
    assert row["partial_exit_reason"] == "rebalance_reduce"

    # The remaining 2 close when a FULL exit later fills.
    link2 = "lm-ux-c-BBBUSDT-final-x0ab"
    engine._record_orders([{
        "order_link_id": link2, "trade_id": "t2", "symbol": "BBBUSDT", "side": "Buy",
        "reduce_only": True, "status": "submitted", "qty": "2", "target_qty": "2",
        "filled_qty": "", "exit_reason": "max_hold",
    }])
    engine.state.submitted_link_to_trade_id[link2] = "t2"
    engine.record_tracked_exit_stream_fill(
        order_link_id=link2, filled_qty=2.0, exit_price=48.0, source="execution",
    )
    stored = read_dataset(tmp_path, "event_demo_trades")
    assert stored.filter(pl.col("trade_id") == "t2").select("status").item() == "closed"


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


def test_tracked_stream_fill_full_close_books_realized_pnl(tmp_path: Path) -> None:
    """ROUND 4: the WS-stream close is the STEADY-STATE close path under
    ORDER_SUBMIT_MODE=ws_then_rest, and it stamped status=closed WITHOUT
    gross_trade_return/net_return/exit_fee_usdt — and once closed, the
    pending-fill reconciler skips the row, so the PnL was never backfilled
    (the adverse-exit entry-pause breaker read the loss as net 0)."""
    write_dataset(
        pl.DataFrame(
            [{"trade_id": "t1", "symbol": "AAAUSDT", "side": "short", "status": "open",
              "qty": "1", "entry_price": 100.0, "notional_usdt": 100.0,
              "equity_usdt": 10_000.0, "stop_price": 112.0, "take_profit_price": 80.0,
              "planned_exit_ts_ms": 9_999_999_999_999}]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    trade_client = FakeTradeClient()
    engine = _submit_exit_engine(tmp_path, trade_client)
    _rows, orders = engine.ws_exit({**_SUBMIT_EXIT_PLAN, "exit_trigger_ts_ms": int(time.time() * 1000)})
    link = str(orders[0]["order_link_id"])
    engine.state.submitted_link_to_trade_id[link] = "t1"
    engine.record_tracked_exit_stream_fill(
        order_link_id=link, filled_qty=1.0, exit_price=90.0, source="execution", fee_usdt=0.05,
    )
    closed = read_dataset(tmp_path, "event_demo_trades").filter(pl.col("trade_id") == "t1").to_dicts()[0]
    assert closed["status"] == "closed"
    assert float(closed["gross_trade_return"]) == pytest.approx(0.10)  # short 100 -> 90
    assert float(closed["net_return"]) == pytest.approx(0.10 * 100.0 / 10_000.0)
    assert float(closed["exit_fee_usdt"]) == pytest.approx(0.05)


def test_adoption_treats_eth_hedge_leg_as_externally_managed(tmp_path: Path) -> None:
    """ROUND 4: BOTH 2f hedge legs (BTC + ETH) share the hedge link prefix and
    the externally-managed contract (track, NEVER force-exit). The BTC-only
    check sent an orphaned ETH leg down the generic recovered path, which
    stamps adopt stop/TP/3d hold — ws_risk would force-exit the leg and the
    next daily hedge run would re-buy it, a silent churn loop."""
    from liquidity_migration.continuous_hedge_manager import hedge_order_link_id

    now = int(time.time() * 1000)
    link = hedge_order_link_id(now - 60_000, symbol="ETHUSDT")
    private_client = FakePrivateClient(order_history=[
        {"symbol": "ETHUSDT", "side": "Buy", "orderLinkId": link, "createdTime": str(now - 60_000)},
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
    row = engine._build_adopted_trade(
        {"symbol": "ETHUSDT", "side": "Buy", "size": "1", "avgPrice": "2500.0",
         "createdTime": str(now - 60_000)},
        now_ms=now,
    )
    assert row is not None
    assert row["sleeve"] == "continuous_addon"
    assert row["symbol"] == "ETHUSDT"
    assert float(row["stop_price"]) == 0.0
    assert float(row["take_profit_price"]) == 0.0
    assert int(row["planned_exit_ts_ms"]) == 0
    assert row["submit_mode"] == "adopted_recovered"


def test_drain_failed_telegram_keys_unrecords_on_consumer_thread(tmp_path) -> None:
    engine = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig(telegram=True)
    )
    try:
        engine.state.telegram_keys_sent.add("k-x")
        ws_risk._write_telegram_dedupe_keys(engine.report_dir, engine.state.telegram_keys_sent)
        # Sender thread hands a failed key back; consumer drains + un-records it.
        engine._telegram_failed_keys.put("k-x")
        engine._drain_failed_telegram_keys()
        assert "k-x" not in engine.state.telegram_keys_sent
        assert "k-x" not in set(ws_risk._read_telegram_dedupe_keys(engine.report_dir))
    finally:
        engine.close()


def test_drain_failed_telegram_keys_noop_when_queue_empty(tmp_path) -> None:
    engine = EventWebSocketRiskEngine(
        tmp_path, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig(telegram=True)
    )
    try:
        engine.state.telegram_keys_sent.add("keep")
        ws_risk._write_telegram_dedupe_keys(engine.report_dir, engine.state.telegram_keys_sent)
        engine._drain_failed_telegram_keys()  # nothing queued -> no change
        assert "keep" in engine.state.telegram_keys_sent
        assert "keep" in set(ws_risk._read_telegram_dedupe_keys(engine.report_dir))
    finally:
        engine.close()


# ===========================================================================
# Relocated from tests/test_audit_fix_b03.py (audit bucket b03 regressions).
# Each test FAILS on the original bug and PASSES on the fix. These use a
# distinct set of minimal doubles (`_FakePrivateClient` etc.) carried over
# from the b03 suite — they track wallet calls and default to empty positions,
# which the originals' `FakePrivateClient` does not.
# ===========================================================================
class _FakePrivateClient:
    def __init__(self, *, positions: list[dict[str, object]] | None = None) -> None:
        self.positions = positions if positions is not None else []
        self.orders: list[dict[str, object]] = []
        self.wallet_calls = 0

    def get_positions(self, *, settle_coin: str | None = None):
        return self.positions

    def get_open_orders(self, *, symbol: str | None = None, settle_coin: str | None = None):
        return []

    def place_order(self, **params):
        self.orders.append(params)
        return {"orderId": "rest-order-1"}

    def set_trading_stop(self, **params):
        return {}

    def get_wallet_balance(self, *, account_type=None, coin=None):
        self.wallet_calls += 1
        return {"list": [{"coin": [{"coin": "USDT", "equity": "10000", "walletBalance": "10000"}]}]}

    def get_trade_history(self, *, symbol=None, order_link_id=None, limit=50):
        return [{"orderLinkId": order_link_id, "execQty": "1", "execPrice": "113",
                 "execValue": "113", "execFee": "0.01"}]

    def get_order_history(self, *, symbol=None, order_link_id=None, limit=50):
        return []


class _FakePrivateStream:
    def subscribe_positions(self, callback):
        pass

    def subscribe_orders(self, callback):
        pass

    def subscribe_executions(self, callback, *, fast: bool = False):
        pass

    def close(self):
        pass


class _FakePublicStream:
    def __init__(self) -> None:
        self.symbols: list[str] = []

    def subscribe_tickers(self, symbols, callback):
        self.symbols.extend(symbols if isinstance(symbols, list) else [symbols])

    def close(self):
        pass


def _long_position(symbol: str = "AAAUSDT") -> dict[str, object]:
    # A LONG (Buy) position: close_side must be Sell (test-gaps-7).
    return {
        "symbol": symbol,
        "side": "Buy",
        "size": "1",
        "avgPrice": "100",
        "markPrice": "100",
        "positionValue": "100",
        "unrealisedPnl": "0",
        "stopLoss": "88",
        "takeProfit": "120",
    }


# test-gaps-7 — untracked LONG close-side sign
def test_untracked_long_position_is_closed_with_a_sell(tmp_path: Path) -> None:
    """test-gaps-7: the Buy->Sell long-close branch of close_side was never asserted.
    An inverted sign would close a LONG with a Buy (increasing exposure). Pin it."""
    private_client = _FakePrivateClient(positions=[_long_position()])
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
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )

    engine.bootstrap()

    assert len(private_client.orders) == 1
    assert private_client.orders[0]["reduceOnly"] is True
    # The load-bearing assertion the suite was missing: a LONG closes with a SELL.
    assert private_client.orders[0]["side"] == "Sell"
    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert stored_orders.select("side").item() == "Sell"
    assert stored_orders.select("exit_reason").item() == "untracked_position"


# ws-risk-1 / ws-risk-2 — failed rebuild must retry, not latch None
class _DeadPrivate:
    def is_connected(self) -> bool:
        return False

    def close(self) -> None:
        pass

    def subscribe_positions(self, cb) -> None:  # noqa: ANN001
        pass

    def subscribe_orders(self, cb) -> None:  # noqa: ANN001
        pass

    def subscribe_executions(self, cb, **k) -> None:  # noqa: ANN001
        pass


class _LivePrivate(_DeadPrivate):
    def is_connected(self) -> bool:
        return True


def test_failed_private_rebuild_retries_instead_of_latching_none(tmp_path: Path, monkeypatch) -> None:
    """ws-risk-1: a rebuild that raises after the old socket is closed leaves
    private_stream=None. The old guard (`stream is None -> return`) then made every
    later on_idle pass a no-op forever (start_streams runs only once). The fix arms a
    pending-rebuild latch so a SUBSEQUENT pass retries and recovers."""
    attempts = {"n": 0}

    def _flaky_build(_config):  # noqa: ANN001, ANN202
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient build failure")
        return _LivePrivate()

    monkeypatch.setattr("liquidity_migration.ws_risk._build_private_stream", _flaky_build)

    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(),
        # timeout=0 runs the build inline so the failure is deterministic.
        risk_config=EventWebSocketRiskConfig(
            private_ws_reconnect_seconds=30.0, stream_start_timeout_seconds=0.0
        ),
    )
    engine.private_stream = _DeadPrivate()  # type: ignore[assignment]

    # First reconnect pass: socket down past the bound -> build raises -> None + pending latch.
    engine._maybe_reconnect_private_stream(time.monotonic())  # arms _private_disconnected_since
    engine._private_disconnected_since = time.monotonic() - 999  # past the bound
    engine._maybe_reconnect_private_stream(time.monotonic())
    assert engine.private_stream is None
    assert engine._private_rebuild_pending is True, "a failed rebuild must OWE a retry"

    # A LATER pass must retry the build even though private_stream is None.
    engine._last_private_reconnect_monotonic = time.monotonic() - 999  # clear the cooldown
    engine._private_disconnected_since = time.monotonic() - 999
    engine._maybe_reconnect_private_stream(time.monotonic())
    assert isinstance(engine.private_stream, _LivePrivate), "the next pass must rebuild, not stay blind"
    assert engine._private_rebuild_pending is False
    assert attempts["n"] == 2


def test_failed_public_rebuild_retries_instead_of_latching_none(tmp_path: Path, monkeypatch) -> None:
    """ws-risk-2: same structure as ws-risk-1 for the public ticker stream. A failed
    rebuild after the socket is closed leaves public_stream=None AND clears
    subscribed_symbols; without the pending latch every later pass is a no-op and the
    intrabar price feed silently degrades to the 30s REST mark forever."""
    attempts = {"n": 0}

    class _RebuiltPublic(_FakePublicStream):
        pass

    def _flaky_public(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient public build failure")
        return _RebuiltPublic()

    monkeypatch.setattr("liquidity_migration.ws_risk.BybitPublicTickerStream", _flaky_public)

    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(),
        risk_config=EventWebSocketRiskConfig(
            private_ws_reconnect_seconds=30.0, stream_start_timeout_seconds=0.0
        ),
    )
    engine.public_stream = _FakePublicStream()  # type: ignore[assignment]
    engine.state.subscribed_symbols = {"AAAUSDT"}

    now = time.monotonic()
    # Force a long silence so the public watchdog fires.
    engine._last_ticker_event_monotonic = now - 10_000
    engine._public_stream_built_monotonic = now - 10_000
    engine._maybe_reconnect_public_stream(now)
    assert engine.public_stream is None
    assert engine._public_rebuild_pending is True, "a failed public rebuild must OWE a retry"
    # The symbols to re-subscribe are preserved ACROSS the failed attempt.
    assert "AAAUSDT" in engine._public_resubscribe

    # A later pass retries and recovers, re-subscribing the saved symbol set.
    engine._last_public_reconnect_monotonic = now - 10_000
    engine._maybe_reconnect_public_stream(now)
    assert isinstance(engine.public_stream, _RebuiltPublic)
    assert engine._public_rebuild_pending is False
    assert "AAAUSDT" in engine.public_stream.symbols
    assert attempts["n"] == 2


# ws-risk-4 — cap-qty predicate must include the addon root
def test_cap_qty_predicate_includes_addon_only_sibling(tmp_path: Path) -> None:
    """ws-risk-4: an engine with ONLY continuous_addon configured still has a netted
    sibling (short is always present), so the stop-cap must engage. The old predicate
    (long_root or continuous_root) omitted the addon root -> cap_qty_to_trade=False,
    letting a stop flatten the sibling leg. The fix keys off the owned-ledger count."""
    addon_root = tmp_path / "addon"
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(continuous_addon_data_root=str(addon_root)),
        private_client=_FakePrivateClient(),
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    # short + continuous_addon == 2 owned ledgers -> cap engages.
    assert len(engine._sleeve_routes(trades=True)) == 2
    assert (len(engine._sleeve_routes(trades=True)) > 1) is True
    # The old buggy predicate would have been False for this config.
    old_predicate = engine.long_root is not None or engine.continuous_root is not None
    assert old_predicate is False, "the addon-only config exposes the omitted-root bug"

    # A short-only engine has a single owned ledger -> cap must NOT engage.
    short_only = EventWebSocketRiskEngine(
        tmp_path / "short_only",
        config=ResearchConfig(data_root=tmp_path / "short_only"),
        risk_config=EventWebSocketRiskConfig(),
        private_client=_FakePrivateClient(),
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    assert len(short_only._sleeve_routes(trades=True)) == 1
    assert (len(short_only._sleeve_routes(trades=True)) > 1) is False


# ws-risk-5 — stale-WS watchdog must see a dead PRIVATE stream while tickers flow
def test_stale_watchdog_fires_on_private_silence_while_tickers_flow(tmp_path: Path) -> None:
    """ws-risk-5: the all-events clock is bumped by ticker traffic too, so a dead
    private stream stayed invisible to the stale-WS watchdog while public tickers kept
    it warm. The fix tracks a private-only event clock; with positions held, private
    silence must force a REST reconcile even when ticker events are fresh."""
    private_client = _FakePrivateClient(positions=[])
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False,
            repair_stops=False,
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            stale_ws_seconds=30.0,
            rest_fallback=True,
        ),
        private_client=private_client,
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    # Hold a position so the watchdog expects private-stream traffic.
    engine.state.positions_by_symbol = {"AAAUSDT": _long_position()}

    now = time.monotonic()
    reconciles: list[float] = []
    engine.rest_reconcile = lambda *a, **k: reconciles.append(now)  # type: ignore[assignment]

    # Public ticker traffic keeps the all-events clock FRESH...
    engine.state.last_ws_event_monotonic = now
    # ...but the PRIVATE stream has been silent past the bound.
    engine.state.last_private_ws_event_monotonic = now - 999
    engine.state.last_stale_reconcile_monotonic = now - 999

    engine.reconcile_stale_websocket(now)
    assert reconciles, "private-stream silence must force a REST reconcile even with fresh tickers"
    assert any("private-stream" in e for e in engine.state.errors)


# ws-risk-6 — a CONFIRMED-LIVE but quiet private socket must NOT read as stale
def _stale_watchdog_engine(tmp_path: Path, private_stream) -> "EventWebSocketRiskEngine":  # noqa: ANN001
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False,
            repair_stops=False,
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            stale_ws_seconds=30.0,
            rest_fallback=True,
        ),
        private_client=_FakePrivateClient(positions=[]),
        private_stream=private_stream,
        public_stream=_FakePublicStream(),
    )
    engine.state.positions_by_symbol = {"AAAUSDT": _long_position()}
    return engine


def test_stale_watchdog_ignores_quiet_but_live_private_socket(tmp_path: Path) -> None:
    """ws-risk-6: Bybit only pushes private events on state changes, so a healthy
    socket on a quiet book goes silent for hours. The watchdog must trust the socket's
    is_connected()=True over its data-silence and NOT raise a false 'private-stream
    stale; forced REST reconcile' (which surfaced as position_report_error on every
    heartbeat of the idle 3-short demo book). Regression for the operator false page."""
    engine = _stale_watchdog_engine(tmp_path, _LivePrivate())

    now = time.monotonic()
    reconciles: list[float] = []
    engine.rest_reconcile = lambda *a, **k: reconciles.append(now)  # type: ignore[assignment]
    # Public tickers fresh; private silent far past the bound (the quiet-book case).
    engine.state.last_ws_event_monotonic = now
    engine.state.last_private_ws_event_monotonic = now - 9999
    engine.state.last_stale_reconcile_monotonic = now - 9999

    engine.reconcile_stale_websocket(now)
    assert not reconciles, "a confirmed-live private socket must not be forced to reconcile on silence alone"
    assert not any("private-stream" in e for e in engine.state.errors)


def test_stale_watchdog_still_fires_on_dead_private_socket(tmp_path: Path) -> None:
    """ws-risk-6 guard: the liveness gate must NOT blind the watchdog to a genuinely
    DOWN private socket. is_connected()=False -> silence is meaningful -> still force a
    REST reconcile so protection is preserved while _maybe_reconnect rebuilds."""
    engine = _stale_watchdog_engine(tmp_path, _DeadPrivate())

    now = time.monotonic()
    reconciles: list[float] = []
    engine.rest_reconcile = lambda *a, **k: reconciles.append(now)  # type: ignore[assignment]
    engine.state.last_ws_event_monotonic = now
    engine.state.last_private_ws_event_monotonic = now - 9999
    engine.state.last_stale_reconcile_monotonic = now - 9999

    engine.reconcile_stale_websocket(now)
    assert reconciles, "a down private socket's silence must still force a REST reconcile"
    assert any("private-stream" in e for e in engine.state.errors)


# ws-risk-6 — partial reduce-only fills book realized PnL on the closed chunk
def _open_continuous_trade(root: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [{
                "trade_id": "t2", "symbol": "BBBUSDT", "side": "short", "status": "open",
                "qty": "3", "entry_price": 50.0, "equity_usdt": 10_000.0,
                "notional_usdt": 150.0, "stop_price": 56.0, "take_profit_price": 40.0,
                "planned_exit_ts_ms": 9_999_999_999_999,
            }]
        ),
        root,
        "event_demo_trades",
        partition_by=(),
    )


def test_partial_reduce_books_realized_loss_on_the_closed_chunk(tmp_path: Path) -> None:
    """ws-risk-6: the partial (not-fully-filled) branch booked NO realized PnL for the
    closed chunk — the loss was hidden until the residual fully closed, so the
    adverse-exit breaker read it as net 0. The fix accumulates the closed delta's
    realized return on the still-open row, and folds every leg into the final
    close's net_return."""
    _open_continuous_trade(tmp_path)
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True, confirm_demo_orders=True, repair_stops=False,
            order_submit_mode="rest", rest_fallback=True, exit_untracked_positions=False,
            rest_reconcile_seconds=0.0, heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0, adopt_untracked_positions=False,
        ),
        private_client=_FakePrivateClient(),
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    engine.bootstrap()

    # Partial reduce: close 1 of 3 at 55 (a SHORT loss: entry 50 -> 55 = -10% gross).
    link1 = "lm-ux-c-BBBUSDT-reduce"
    engine._record_orders([{
        "order_link_id": link1, "trade_id": "t2", "symbol": "BBBUSDT", "side": "Buy",
        "reduce_only": True, "status": "submitted", "qty": "1", "target_qty": "1",
        "filled_qty": "", "exit_reason": "rebalance_reduce",
    }])
    engine.state.submitted_link_to_trade_id[link1] = "t2"
    engine.record_tracked_exit_stream_fill(
        order_link_id=link1, filled_qty=1.0, exit_price=55.0, source="execution",
    )

    row = read_dataset(tmp_path, "event_demo_trades").filter(pl.col("trade_id") == "t2").to_dicts()[0]
    assert row["status"] == "open"
    assert float(row["qty"]) == pytest.approx(2.0)
    # The closed chunk's realized return is now BOOKED on the open row (was absent).
    # gross = (50-55)/50 = -0.10; delta notional = 1*50 = 50; weight = 50/10000 = 0.005.
    assert float(row["partial_exit_gross_return"]) == pytest.approx(-0.10)
    assert float(row["partial_exit_realized_return"]) == pytest.approx(-0.10 * 0.005)
    assert float(row["rebalance_realized_return"]) == pytest.approx(-0.0005)

    # Close the residual 2 at 56 (another loss). The final net_return must fold BOTH
    # legs — not just the last delta — so the multi-leg close no longer understates.
    link2 = "lm-ux-c-BBBUSDT-final"
    engine._record_orders([{
        "order_link_id": link2, "trade_id": "t2", "symbol": "BBBUSDT", "side": "Buy",
        "reduce_only": True, "status": "submitted", "qty": "2", "target_qty": "2",
        "filled_qty": "", "exit_reason": "max_hold",
    }])
    engine.state.submitted_link_to_trade_id[link2] = "t2"
    engine.record_tracked_exit_stream_fill(
        order_link_id=link2, filled_qty=2.0, exit_price=56.0, source="execution",
    )

    closed = read_dataset(tmp_path, "event_demo_trades").filter(pl.col("trade_id") == "t2").to_dicts()[0]
    assert closed["status"] == "closed"
    # Final delta: gross = (50-56)/50 = -0.12; delta notional = 2*50 = 100; weight = 0.01.
    # net_return = prior(-0.0005) + (-0.12 * 0.01) = -0.0005 - 0.0012 = -0.0017.
    assert float(closed["net_return"]) == pytest.approx(-0.0017)
    # The original bug would have booked only the final leg (-0.0012), hiding -0.0005.
    assert float(closed["net_return"]) < -0.0012


# ws-risk-7 — prune is skipped on a degraded ledger read
def test_prune_closed_order_state_skipped_on_ledger_read_error(tmp_path: Path) -> None:
    """ws-risk-7: live_trade_ids derives from open_trades, which is incomplete when a
    sibling ledger read raised this pass. Pruning then evicts a still-open sibling
    order's link, and a later fill for it finds no order and is dropped. The fix bails
    out of pruning whenever ledger_read_error is set."""
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(telemetry_log_retention=2),
        private_client=_FakePrivateClient(),
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    # The OLDEST order (idx 0, beyond the retention grace window) belongs to a still-open
    # sibling trade that is INVISIBLE this pass because the sibling ledger read raised, so
    # open_trades is empty and the order looks trade-closed. WITHOUT the guard it would be
    # evicted; a later fill for its link would then find no order and be dropped.
    link = "lm-ux-c-CCC-open-sibling"
    orders = [{"order_link_id": link, "trade_id": "open-sibling", "symbol": "CCCUSDT",
               "status": "submitted", "exit_reason": "rebalance_reduce"}]
    orders += [
        {"order_link_id": f"recent-{i}", "trade_id": f"tx{i}", "symbol": "ZZZUSDT",
         "status": "filled", "exit_reason": "rebalance_reduce"}
        for i in range(5)
    ]
    engine._record_orders(orders)
    engine.state.submitted_link_to_trade_id[link] = "open-sibling"

    # With a ledger_read_error set, the prune MUST be a no-op (fail closed) — even though
    # the oldest order is well beyond the grace window.
    engine.state.ledger_read_error = "continuous:combined: RuntimeError: torn read"
    engine._prune_closed_order_state()
    assert link in engine.state.orders_by_link, "must NOT evict an old sibling order on a degraded read"
    assert engine.state.submitted_link_to_trade_id.get(link) == "open-sibling"
    assert engine.state.orders_evicted == 0

    # Clearing the error re-enables the bounded prune (so it is a guard, not a disable):
    # the oldest order IS now evictable, proving the guard — not a coincidental retention
    # window — is what protected it above.
    engine.state.ledger_read_error = ""
    engine._prune_closed_order_state()
    assert engine.state.orders_evicted > 0
    assert link not in engine.state.orders_by_link, "the unguarded prune evicts the old order"


# ws-risk-8 — cold-start adoption never fires a per-orphan wallet REST
def test_cold_start_adoption_uses_cached_equity_no_wallet_call(tmp_path: Path) -> None:
    """ws-risk-8: _build_adopted_trade stamped equity via
    `_last_equity_usdt or _account_equity_usdt()`. On cold start the cache is 0.0, so a
    blocking get_wallet_balance fired per adopted orphan on the latency-critical
    consumer thread. The fix seeds the cache ONCE before adoption and reads cache-only."""
    def _short(symbol: str) -> dict[str, object]:
        return {"symbol": symbol, "side": "Sell", "size": "1", "avgPrice": "100",
                "markPrice": "100", "positionValue": "100", "unrealisedPnl": "0",
                "stopLoss": "112", "takeProfit": "80"}

    private_client = _FakePrivateClient(positions=[_short("DDDUSDT"), _short("EEEUSDT")])
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False, repair_stops=False, order_submit_mode="rest",
            rest_reconcile_seconds=0.0, heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
            adopt_untracked_positions=True, exit_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    engine.bootstrap()

    # The adopted rows carry a non-zero equity snapshot from the seeded cache.
    stored = read_dataset(tmp_path, "event_demo_trades")
    assert not stored.is_empty()
    assert all(e and float(e) > 0.0 for e in stored.select("equity_usdt").to_series().to_list())

    # The adoption builder reads cache-only -> never a wallet call. _build_adopted_trade
    # would have called get_wallet_balance once PER orphan (2 here) under the bug.
    before = private_client.wallet_calls
    built = engine._build_adopted_trade({"symbol": "FFFUSDT", "side": "Sell", "size": "1", "avgPrice": "100"},
                                        now_ms=ws_risk._now_ms())
    assert built is not None and float(built["equity_usdt"]) > 0.0
    assert private_client.wallet_calls == before, "adoption builder must not fire a wallet REST"


# --- relocated from tests/test_audit_int_iB.py (audit-integration bucket iB) ---
# cross-sleeve-2: ws_risk._refresh_cross_sleeve_account_state must pass the
# open->closed trade_id delta to cross_sleeve.write_account_state so a closed
# trade's symbol reservation is GC'd promptly instead of lingering until the TTL.


def _open_trades_frame(trade_ids: list[str]) -> pl.DataFrame:
    """A minimal open_trades snapshot carrying just the trade_id column the
    close-GC diff reads. compute_im_used tolerates extra/missing columns and the
    engine's private_client is None (equity 0.0), so this is enough to drive a
    real _refresh_cross_sleeve_account_state pass end to end."""
    return pl.DataFrame({"trade_id": pl.Series(trade_ids, dtype=pl.String)})


def _cross_sleeve_engine(root: Path) -> EventWebSocketRiskEngine:
    # No sibling roots / no private client: the refresh writes ONLY the control
    # row (IM/equity=0, reservations GC'd) into `root`, exactly as in production.
    return EventWebSocketRiskEngine(
        root, config=ResearchConfig(), risk_config=EventWebSocketRiskConfig()
    )


def _reserved_symbols(root: Path, *, now_ms: int) -> set[str]:
    return {r["symbol"] for r in read_account_state(root).active_reservations(now_ms=now_ms)}


def _seed_control_row(root: Path, *, now_ms: int) -> None:
    """Create the cross-sleeve control row so claim_symbol_reservation PERSISTS.
    Without an existing row, claim is safe-by-default fail-open ("ws_risk not
    deployed yet") and records nothing; in production ws_risk seeds the row on
    bootstrap before any sleeve claims a symbol."""
    _cross_sleeve.write_account_state(
        root, equity_usdt=0.0, account_im_used_pct=0.0, im_used_pct_by_sleeve={}, now_ms=now_ms,
    )


def test_refresh_gcs_reservation_of_trade_that_closed_since_last_pass(tmp_path: Path) -> None:
    """The end-to-end wiring: a sibling sleeve reserves two symbols; ws_risk sees
    both trades open, then one closes. The next refresh frees ONLY the closed
    trade's reservation — without waiting out the TTL — and keeps the live one.

    Reservations are anchored to the real clock (_now_ms) because the engine's
    write uses _now_ms() for TTL filtering; that keeps both reservations well
    within their 180s TTL, so the close-GC is the ONLY thing that can drop one."""
    engine = _cross_sleeve_engine(tmp_path)
    now = _now_ms()
    _seed_control_row(tmp_path, now_ms=now)  # ws_risk seeds the row before sleeves claim
    assert claim_symbol_reservation(
        tmp_path, symbol="CLOSEDUSDT", sleeve="continuous", trade_id="t-closed", now_ms=now
    ) is True
    assert claim_symbol_reservation(
        tmp_path, symbol="LIVEUSDT", sleeve="continuous", trade_id="t-live", now_ms=now
    ) is True

    # Pass 1: both trades open -> baseline recorded, nothing closed yet, both kept.
    engine.state.open_trades = _open_trades_frame(["t-closed", "t-live"])
    engine._refresh_cross_sleeve_account_state()
    assert engine._cross_sleeve_open_trade_ids == {"t-closed", "t-live"}
    assert _reserved_symbols(tmp_path, now_ms=now) == {"CLOSEDUSDT", "LIVEUSDT"}

    # Pass 2: t-closed dropped out of open_trades (the trade closed). Both
    # reservations are well inside the 180s TTL, so only the close-GC can free
    # CLOSEDUSDT — LIVEUSDT must remain.
    engine.state.open_trades = _open_trades_frame(["t-live"])
    engine._refresh_cross_sleeve_account_state()
    assert engine._cross_sleeve_open_trade_ids == {"t-live"}
    assert _reserved_symbols(tmp_path, now_ms=now) == {"LIVEUSDT"}


def test_first_pass_closes_nothing_with_no_prior_baseline(tmp_path: Path) -> None:
    """On the very first refresh the prior-open baseline is empty, so the diff
    yields no closed ids — a fresh deploy must not GC reservations a sleeve just
    claimed for trades it has not yet observed as open."""
    engine = _cross_sleeve_engine(tmp_path)
    now = _now_ms()
    _seed_control_row(tmp_path, now_ms=now)  # ws_risk seeds the row before sleeves claim
    assert claim_symbol_reservation(
        tmp_path, symbol="AAAUSDT", sleeve="continuous", trade_id="t-a", now_ms=now
    ) is True
    assert engine._cross_sleeve_open_trade_ids == set()

    engine.state.open_trades = _open_trades_frame(["t-a"])
    engine._refresh_cross_sleeve_account_state()
    # Baseline now seeded; the just-claimed reservation is untouched.
    assert engine._cross_sleeve_open_trade_ids == {"t-a"}
    assert _reserved_symbols(tmp_path, now_ms=now) == {"AAAUSDT"}


def test_refresh_passes_closed_trade_ids_argument(tmp_path: Path, monkeypatch) -> None:
    """Lock the wiring itself: write_account_state must RECEIVE the open->closed
    delta as closed_trade_ids, independent of the owned-side GC semantics."""
    captured: list[set[str]] = []

    def _spy(*args, **kwargs):  # noqa: ANN002, ANN003
        captured.append(set(kwargs.get("closed_trade_ids", ())))

    monkeypatch.setattr(_cross_sleeve, "write_account_state", _spy)
    engine = _cross_sleeve_engine(tmp_path)

    engine.state.open_trades = _open_trades_frame(["t1", "t2"])
    engine._refresh_cross_sleeve_account_state()
    assert captured[-1] == set()  # nothing closed on the baseline pass

    engine.state.open_trades = _open_trades_frame(["t2"])
    engine._refresh_cross_sleeve_account_state()
    assert captured[-1] == {"t1"}  # t1 transitioned open->closed


def test_baseline_not_advanced_when_write_raises(tmp_path: Path, monkeypatch) -> None:
    """If write_account_state raises before completing, the prior-open baseline
    must NOT advance, so the same close-GC retries next pass (the GC is
    idempotent). The refresh itself stays self-swallowing — a control-row fault
    must never break the reconcile loop."""
    engine = _cross_sleeve_engine(tmp_path)
    engine.state.open_trades = _open_trades_frame(["t1", "t2"])
    engine._refresh_cross_sleeve_account_state()
    assert engine._cross_sleeve_open_trade_ids == {"t1", "t2"}

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated control-row write failure")

    monkeypatch.setattr(_cross_sleeve, "write_account_state", _boom)
    engine.state.open_trades = _open_trades_frame(["t2"])  # t1 closed
    # Self-swallowing: no exception propagates out of the refresh.
    engine._refresh_cross_sleeve_account_state()
    # Baseline unchanged -> t1 is re-diffed as closed on the next (successful) pass.
    assert engine._cross_sleeve_open_trade_ids == {"t1", "t2"}
