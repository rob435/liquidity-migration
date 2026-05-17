from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from .bybit import BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient
from .config import ResearchConfig
from .event_demo import (
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    PENDING_ORDER_GUARD_MS,
    PENDING_ORDER_STATUSES,
    _active_position_by_symbol,
    _bool,
    _build_private_client,
    _column_values,
    _empty_trades,
    _execution_summary,
    _execute_risk_exits,
    _execute_stop_repairs,
    _float,
    _open_trades,
    _order_params,
    _price_lookup_from_positions,
    _risk_order_link_id,
    _risk_reconcile_missing_positions,
    _reconcile_pending_order_fills,
    _live_open_order_symbols,
    _safe_open_orders,
    _safe_raw_positions,
    _terminalize_stale_pending_entry_orders,
    _maybe_notify,
    _telegram_notification_reason,
    _upsert_rows,
    _write_order_rows,
    _write_trade_rows,
    build_ledger_position_pnl_snapshot,
    build_position_pnl_snapshot,
    format_event_risk_cycle_report,
    plan_risk_exits,
    plan_stop_repairs,
    summarize_position_pnl,
)
from .storage import exclusive_file_lock, read_dataset, write_dataset


@dataclass(frozen=True, slots=True)
class EventWebSocketRiskConfig:
    submit_orders: bool = False
    confirm_demo_orders: bool = False
    telegram: bool = False
    account_type: str = "UNIFIED"
    settle_coin: str = "USDT"
    data_name: str = "event-risk-ws"
    repair_stops: bool = True
    order_submit_mode: str = "ws_then_rest"
    rest_fallback: bool = True
    rest_reconcile_seconds: float = 30.0
    heartbeat_seconds: float = 10.0
    max_runtime_seconds: float = 0.0
    stale_ws_seconds: float = 15.0
    stream_start_timeout_seconds: float = 3.0
    fast_execution_stream: bool = False
    stop_tolerance_bps: float = 1.0
    pending_exit_guard_seconds: float = 120.0
    exit_untracked_positions: bool = True


@dataclass(slots=True)
class WebSocketRiskState:
    all_trades: pl.DataFrame = field(default_factory=pl.DataFrame)
    open_trades: pl.DataFrame = field(default_factory=_empty_trades)
    positions_by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    price_by_symbol: dict[str, float] = field(default_factory=dict)
    pending_entry_symbols: set[str] = field(default_factory=set)
    submitted_symbols: set[str] = field(default_factory=set)
    live_entry_order_symbols: set[str] = field(default_factory=set)
    live_exit_order_symbols: set[str] = field(default_factory=set)
    submitted_symbol_ts_ms: dict[str, int] = field(default_factory=dict)
    submitted_link_to_trade_id: dict[str, str] = field(default_factory=dict)
    submitted_link_submit_mode: dict[str, str] = field(default_factory=dict)
    executions_by_link: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    subscribed_symbols: set[str] = field(default_factory=set)
    last_ws_event_monotonic: float = field(default_factory=time.monotonic)
    last_stale_reconcile_monotonic: float = 0.0
    last_report_monotonic: float = 0.0
    last_reconcile_monotonic: float = 0.0
    exits: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    repairs: list[dict[str, Any]] = field(default_factory=list)
    reconciliations: list[dict[str, Any]] = field(default_factory=list)
    pending_fill_reconciliations: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ws_order_unavailable: str = ""
    telegram_keys_sent: set[str] = field(default_factory=set)


class EventWebSocketRiskEngine:
    def __init__(
        self,
        data_root: str | Path,
        *,
        config: ResearchConfig,
        risk_config: EventWebSocketRiskConfig | None = None,
        private_client: Any | None = None,
        private_stream: Any | None = None,
        public_stream: Any | None = None,
        trade_client: Any | None = None,
    ) -> None:
        self.root = Path(data_root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.risk = risk_config or EventWebSocketRiskConfig()
        _validate_ws_risk_config(self.risk)
        self.private_client = private_client
        self.private_stream = private_stream
        self.public_stream = public_stream
        self.trade_client = trade_client
        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.state = WebSocketRiskState()
        self.report_dir = self.root / "reports" / self.risk.data_name
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.state.telegram_keys_sent = set(_read_telegram_dedupe_keys(self.report_dir))

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        self.bootstrap()
        self.write_report(reason="startup")
        while True:
            if self.risk.max_runtime_seconds > 0 and time.monotonic() - started >= self.risk.max_runtime_seconds:
                return self.write_report(reason="max_runtime")
            timeout = max(min(self.risk.heartbeat_seconds, self.risk.rest_reconcile_seconds, 1.0), 0.05)
            try:
                event_type, message = self.events.get(timeout=timeout)
            except queue.Empty:
                self.on_idle()
                continue
            self.handle_event(event_type, message)

    def bootstrap(self) -> None:
        self.private_client = self.private_client or _build_private_client(self.config)
        self.state.all_trades = read_dataset(self.root, "event_demo_trades")
        self.state.open_trades = _open_trades(self.state.all_trades)
        orders = read_dataset(self.root, "event_demo_orders")
        raw_positions, error = _safe_raw_positions(self.private_client, settle_coin=self.risk.settle_coin)
        if error:
            self.state.errors.append(error)
        self.state.positions_by_symbol = _active_position_by_symbol(raw_positions)
        self.state.price_by_symbol.update(_price_lookup_from_positions(self.state.positions_by_symbol))
        open_orders_ok = self.refresh_live_exit_order_symbols()
        self.reconcile_pending_order_fills(orders)
        orders = read_dataset(self.root, "event_demo_orders")
        self.load_pending_entry_orders(orders)
        self.load_pending_exit_orders(orders)
        if not error and open_orders_ok:
            self.reconcile_flat_pending_exit_orders(orders)
            orders = read_dataset(self.root, "event_demo_orders")
            self.terminalize_stale_pending_entry_orders(orders)
        self.reconcile_positions(write=True)
        self.evaluate_symbols(set(self.state.positions_by_symbol))
        self.repair_exchange_stops()
        self.exit_untracked_positions()
        self.start_streams()
        self.state.last_reconcile_monotonic = time.monotonic()

    def start_streams(self) -> None:
        if self.private_stream is None:
            stream, error = _call_with_timeout(
                "private websocket stream construction",
                lambda: _build_private_stream(self.config),
                timeout_seconds=self.risk.stream_start_timeout_seconds,
            )
            if error:
                self.state.errors.append(error)
            else:
                self.private_stream = stream
        if self.private_stream is not None:
            _, error = _call_with_timeout(
                "private websocket subscriptions",
                self._subscribe_private_stream,
                timeout_seconds=self.risk.stream_start_timeout_seconds,
            )
            if error:
                self.state.errors.append(error)
        if self.public_stream is None:
            stream, error = _call_with_timeout(
                "public ticker websocket stream construction",
                lambda: BybitPublicTickerStream(
                    category=self.config.exchange.category,
                    testnet=self.config.exchange.testnet,
                    demo=False,
                ),
                timeout_seconds=self.risk.stream_start_timeout_seconds,
            )
            if error:
                self.state.errors.append(error)
            else:
                self.public_stream = stream
        self.subscribe_tickers(set(self.state.positions_by_symbol) | set(_column_values(self.state.open_trades, "symbol")))
        if self.risk.order_submit_mode in {"ws", "ws_then_rest"} and self.trade_client is None:
            if self.risk.order_submit_mode == "ws_then_rest":
                self.state.ws_order_unavailable = _DEMO_WS_TRADE_UNAVAILABLE
                return
            client, error = _call_with_timeout(
                "websocket trade client construction",
                lambda: _build_ws_trade_client(self.config),
                timeout_seconds=self.risk.stream_start_timeout_seconds,
            )
            if error:
                self.state.ws_order_unavailable = error[:500]
                if self.risk.order_submit_mode == "ws" and self.risk.submit_orders:
                    raise RuntimeError(error)
            else:
                self.trade_client = client

    def _subscribe_private_stream(self) -> None:
        assert self.private_stream is not None
        self.private_stream.subscribe_positions(lambda message: self.events.put(("position", message)))
        self.private_stream.subscribe_orders(lambda message: self.events.put(("order", message)))
        self.private_stream.subscribe_executions(
            lambda message: self.events.put(("execution", message)),
            fast=self.risk.fast_execution_stream,
        )

    def subscribe_tickers(self, symbols: set[str]) -> None:
        missing = sorted(symbol for symbol in symbols if symbol and symbol not in self.state.subscribed_symbols)
        if not missing or self.public_stream is None:
            return
        _, error = _call_with_timeout(
            f"public ticker subscription {','.join(missing[:8])}",
            lambda: self.public_stream.subscribe_tickers(missing, lambda message: self.events.put(("ticker", message))),
            timeout_seconds=self.risk.stream_start_timeout_seconds,
        )
        if error:
            self.state.errors.append(error)
            return
        self.state.subscribed_symbols.update(missing)

    def handle_event(self, event_type: str, message: dict[str, Any]) -> None:
        self.state.last_ws_event_monotonic = time.monotonic()
        if event_type == "position":
            self.on_position_message(message)
        elif event_type == "ticker":
            self.on_ticker_message(message)
        elif event_type == "execution":
            self.on_execution_message(message)
        elif event_type == "order":
            self.on_order_message(message)
        elif event_type == "ws_order_ack":
            self.on_ws_order_ack(message)
        self.on_idle()

    def on_position_message(self, message: dict[str, Any]) -> None:
        changed_symbols: set[str] = set()
        for row in _message_rows(message):
            symbol = str(row.get("symbol", ""))
            if not symbol:
                continue
            changed_symbols.add(symbol)
            if _float(row.get("size")) > 0.0:
                self.state.positions_by_symbol[symbol] = row
                price = _position_price(row)
                if price > 0.0:
                    self.state.price_by_symbol[symbol] = price
            else:
                self.state.positions_by_symbol.pop(symbol, None)
        self.subscribe_tickers(changed_symbols)
        reconcile_rows = self.reconcile_positions(write=True)
        if reconcile_rows:
            self.write_report(reason="position_stream_reconcile")
        self.exit_untracked_positions()
        self.evaluate_symbols(changed_symbols)

    def on_ticker_message(self, message: dict[str, Any]) -> None:
        changed_symbols: set[str] = set()
        for row in _message_rows(message):
            symbol = str(row.get("symbol", ""))
            price = _first_price(row, ("markPrice", "lastPrice", "indexPrice"))
            if symbol and price > 0.0:
                self.state.price_by_symbol[symbol] = price
                changed_symbols.add(symbol)
        self.evaluate_symbols(changed_symbols)

    def on_order_message(self, message: dict[str, Any]) -> None:
        updates: list[dict[str, Any]] = []
        for row in _message_rows(message):
            link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
            if not link:
                continue
            status = str(row.get("orderStatus") or row.get("order_status") or "").lower()
            if status in {"rejected", "cancelled", "canceled", "deactivated"}:
                updates.extend(self.mark_order_terminal_from_order_update(order_link_id=link, status=status, row=row))
            elif status == "filled":
                filled_qty = _float(row.get("cumExecQty") or row.get("qty")) or self.order_target_qty(link)
                avg_price = _float(row.get("avgPrice") or row.get("price")) or self.order_avg_price(link)
                updates.extend(
                    self.mark_order_filled_from_execution(
                        order_link_id=link,
                        filled_qty=filled_qty,
                        exit_price=avg_price,
                    )
                )
                self.close_trade_from_order_update(order_link_id=link, filled_qty=filled_qty, exit_price=avg_price)
                for order in updates:
                    self.clear_submitted_symbol(str(order.get("symbol", "")))
        if updates:
            _write_order_rows(self.root, pl.DataFrame(updates, infer_schema_length=None))

    def on_ws_order_ack(self, message: dict[str, Any]) -> None:
        ret_code = _int(message.get("retCode"))
        if ret_code == 0:
            return
        ret_msg = str(message.get("retMsg") or message.get("ret_msg") or message)[:500]
        self.state.errors.append(f"websocket order ack failed: {ret_msg}")
        link = _ack_order_link(message)
        order = self.order_row(link) if link else {}
        if not order:
            self.write_report(reason="ws_order_ack_failed")
            return
        was_pending = str(order.get("status", "")) in PENDING_ORDER_STATUSES
        updates = self.mark_order_terminal_from_order_update(
            order_link_id=link,
            status="rejected",
            row={"symbol": order.get("symbol", ""), "rejectReason": ret_msg},
        )
        if updates:
            _write_order_rows(self.root, pl.DataFrame(updates, infer_schema_length=None))
        if (
            was_pending
            and self.risk.submit_orders
            and self.risk.rest_fallback
            and self.risk.order_submit_mode == "ws_then_rest"
        ):
            exit_plan = self.exit_plan_from_order(order)
            if exit_plan is not None:
                rows, orders = self.rest_exit([exit_plan], submit_orders=True)
                self.record_exit_submission_result(str(exit_plan.get("symbol", "")), rows, orders)
                self.write_report(reason="ws_order_ack_rest_fallback")
                return
        self.write_report(reason="ws_order_ack_failed")

    def on_execution_message(self, message: dict[str, Any]) -> None:
        for row in _message_rows(message):
            link = str(row.get("orderLinkId") or row.get("order_link_id") or "")
            if not link:
                continue
            self.state.executions_by_link.setdefault(link, []).append(row)
            self.close_trade_from_execution(link)
            if link not in self.state.submitted_link_to_trade_id:
                filled_qty = sum(_float(item.get("execQty")) for item in self.state.executions_by_link.get(link, []))
                value = sum(
                    _float(item.get("execValue")) or _float(item.get("execQty")) * _float(item.get("execPrice"))
                    for item in self.state.executions_by_link.get(link, [])
                )
                exit_price = value / filled_qty if filled_qty > 0.0 else 0.0
                order_updates = self.mark_order_filled_from_execution(
                    order_link_id=link,
                    filled_qty=filled_qty,
                    exit_price=exit_price,
                )
                for order in order_updates:
                    self.clear_submitted_symbol(str(order.get("symbol", "")))
                if order_updates:
                    _write_order_rows(self.root, pl.DataFrame(order_updates, infer_schema_length=None))

    def close_trade_from_execution(self, order_link_id: str) -> None:
        trade_id = self.state.submitted_link_to_trade_id.get(order_link_id, "")
        if not trade_id or self.state.all_trades.is_empty():
            return
        trades = {str(row["trade_id"]): row for row in self.state.all_trades.to_dicts()}
        trade = dict(trades.get(trade_id, {}))
        if not trade or str(trade.get("status")) == "closed":
            return
        target_qty = _float(trade.get("qty"))
        executions = self.state.executions_by_link.get(order_link_id, [])
        filled_qty = sum(_float(row.get("execQty")) for row in executions)
        if target_qty <= 0.0 or filled_qty + max(target_qty * 1e-8, 1e-12) < target_qty:
            return
        value = sum(_float(row.get("execValue")) or _float(row.get("execQty")) * _float(row.get("execPrice")) for row in executions)
        exit_price = value / filled_qty if filled_qty > 0.0 else _float(trade.get("exit_price"))
        now_ms = _now_ms()
        order = self.order_row(order_link_id)
        trade.update(
            {
                "status": "closed",
                "exit_ts_ms": now_ms,
                "exit_trigger_ts_ms": _int(order.get("exit_trigger_ts_ms")) or now_ms,
                "exit_price": exit_price,
                "exit_reason": str(order.get("exit_reason") or trade.get("exit_reason") or "execution_confirmed"),
                "exit_order_link_id": order_link_id,
                "submit_mode": self.state.submitted_link_submit_mode.get(order_link_id, "execution_confirmed"),
                "closed_at_ms": now_ms,
                "updated_at_ms": now_ms,
            }
        )
        self.state.exits.append(trade)
        self.clear_submitted_symbol(str(trade.get("symbol", "")))
        self.state.positions_by_symbol.pop(str(trade.get("symbol", "")), None)
        self.state.all_trades = _upsert_rows(self.state.all_trades, [trade], key="trade_id")
        self.state.open_trades = _open_trades(self.state.all_trades)
        _write_trade_rows(self.root, pl.DataFrame([trade], infer_schema_length=None))
        order_updates = self.mark_order_filled_from_execution(
            order_link_id=order_link_id,
            filled_qty=filled_qty,
            exit_price=exit_price,
        )
        if order_updates:
            _write_order_rows(self.root, pl.DataFrame(order_updates, infer_schema_length=None))
        self.write_report(reason="ws_execution_fill")

    def close_trade_from_order_update(self, *, order_link_id: str, filled_qty: float, exit_price: float) -> None:
        trade_id = self.state.submitted_link_to_trade_id.get(order_link_id, "")
        if not trade_id or self.state.all_trades.is_empty():
            return
        trades = {str(row["trade_id"]): row for row in self.state.all_trades.to_dicts()}
        trade = dict(trades.get(trade_id, {}))
        if not trade or str(trade.get("status")) == "closed":
            return
        target_qty = _float(trade.get("qty"))
        if target_qty <= 0.0 or filled_qty + max(target_qty * 1e-8, 1e-12) < target_qty:
            return
        now_ms = _now_ms()
        order = self.order_row(order_link_id)
        trade.update(
            {
                "status": "closed",
                "exit_ts_ms": now_ms,
                "exit_trigger_ts_ms": _int(order.get("exit_trigger_ts_ms")) or now_ms,
                "exit_price": exit_price if exit_price > 0.0 else _float(trade.get("exit_price")),
                "exit_reason": str(order.get("exit_reason") or trade.get("exit_reason") or "order_stream_filled"),
                "exit_order_link_id": order_link_id,
                "submit_mode": self.state.submitted_link_submit_mode.get(order_link_id, "order_stream_filled"),
                "closed_at_ms": now_ms,
                "updated_at_ms": now_ms,
            }
        )
        self.state.exits.append(trade)
        self.clear_submitted_symbol(str(trade.get("symbol", "")))
        self.state.positions_by_symbol.pop(str(trade.get("symbol", "")), None)
        self.state.all_trades = _upsert_rows(self.state.all_trades, [trade], key="trade_id")
        self.state.open_trades = _open_trades(self.state.all_trades)
        _write_trade_rows(self.root, pl.DataFrame([trade], infer_schema_length=None))
        self.write_report(reason="ws_order_fill")

    def mark_order_filled_from_execution(self, *, order_link_id: str, filled_qty: float, exit_price: float) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        for order in self.state.orders:
            if str(order.get("order_link_id") or "") != order_link_id:
                continue
            order["status"] = "filled"
            order["filled_qty"] = str(filled_qty)
            order["avg_price"] = exit_price
            order["notional_usdt"] = abs(exit_price * filled_qty) if exit_price > 0.0 else 0.0
            updates.append(order)
        return updates

    def order_target_qty(self, order_link_id: str) -> float:
        for order in self.state.orders:
            if str(order.get("order_link_id") or "") == order_link_id:
                return _float(order.get("target_qty") or order.get("qty"))
        return 0.0

    def order_avg_price(self, order_link_id: str) -> float:
        for order in self.state.orders:
            if str(order.get("order_link_id") or "") == order_link_id:
                return _float(order.get("avg_price"))
        return 0.0

    def order_row(self, order_link_id: str) -> dict[str, Any]:
        for order in self.state.orders:
            if str(order.get("order_link_id") or "") == order_link_id:
                return order
        return {}

    def mark_order_terminal_from_order_update(
        self,
        *,
        order_link_id: str,
        status: str,
        row: dict[str, Any],
    ) -> list[dict[str, Any]]:
        normalized_status = "cancelled" if status in {"cancelled", "canceled", "deactivated"} else "rejected"
        updates: list[dict[str, Any]] = []
        for order in self.state.orders:
            if str(order.get("order_link_id") or "") != order_link_id:
                continue
            symbol = str(row.get("symbol") or order.get("symbol") or "")
            order["status"] = normalized_status
            order["error"] = str(row.get("rejectReason") or row.get("cancelType") or row.get("orderStatus") or "")[:500]
            self.clear_submitted_symbol(symbol)
            updates.append(order)
        return updates

    def evaluate_symbols(self, symbols: set[str]) -> None:
        if self.state.open_trades.is_empty() or not symbols:
            return
        self.expire_stale_submitted_symbols()
        trades = self.state.open_trades.filter(pl.col("symbol").is_in(sorted(symbols)))
        if trades.is_empty():
            return
        exits = plan_risk_exits(
            trades,
            position_by_symbol=self.state.positions_by_symbol,
            price_by_symbol=self.state.price_by_symbol,
            now_ms=_now_ms(),
        )
        for exit_plan in exits:
            symbol = str(exit_plan.get("symbol", ""))
            if symbol and not self.exit_submission_active(symbol):
                self.submit_exit(exit_plan)

    def submit_exit(self, exit_plan: dict[str, Any]) -> None:
        symbol = str(exit_plan["symbol"])
        if not self.risk.submit_orders:
            rows, orders = self.rest_exit([exit_plan], submit_orders=False)
        elif self.trade_client is not None and self.risk.order_submit_mode in {"ws", "ws_then_rest"}:
            try:
                rows, orders = self.ws_exit(exit_plan)
            except Exception as exc:  # noqa: BLE001 - REST fallback is the explicit last resort
                self.state.errors.append(str(exc)[:500])
                if not self.risk.rest_fallback:
                    raise
                rows, orders = self.rest_exit([exit_plan], submit_orders=True)
        elif self.risk.rest_fallback:
            rows, orders = self.rest_exit([exit_plan], submit_orders=True)
        else:
            raise RuntimeError("No available risk exit order path")
        self.record_exit_submission_result(symbol, rows, orders)
        self.write_report(reason="exit_submitted")

    def record_exit_submission_result(
        self,
        symbol: str,
        rows: list[dict[str, Any]],
        orders: list[dict[str, Any]],
    ) -> None:
        if rows:
            self.state.all_trades = _upsert_rows(self.state.all_trades, rows, key="trade_id")
            self.state.open_trades = _open_trades(self.state.all_trades)
            _write_trade_rows(self.root, pl.DataFrame(rows, infer_schema_length=None))
            self.state.exits.extend(rows)
            for row in rows:
                if str(row.get("status", "")) == "closed":
                    self.state.positions_by_symbol.pop(str(row.get("symbol", "")), None)
        if orders:
            for order in orders:
                link = str(order.get("order_link_id") or "")
                trade_id = str(order.get("trade_id") or "")
                if link and trade_id:
                    self.state.submitted_link_to_trade_id[link] = trade_id
                    self.state.submitted_link_submit_mode[link] = str(order.get("submit_mode") or "submitted")
            _write_order_rows(self.root, pl.DataFrame(orders, infer_schema_length=None))
            self.state.orders.extend(orders)
        open_symbols = set(_column_values(self.state.open_trades, "symbol"))
        has_pending_order = any(str(order.get("status", "")) in PENDING_ORDER_STATUSES for order in orders)
        if symbol in open_symbols and has_pending_order:
            self.mark_submitted_symbol(symbol)
        else:
            self.clear_submitted_symbol(symbol)

    def exit_plan_from_order(self, order: dict[str, Any]) -> dict[str, Any] | None:
        trade_id = str(order.get("trade_id") or "")
        symbol = str(order.get("symbol") or "")
        if not trade_id or not symbol or self.state.open_trades.is_empty():
            return None
        trade_lookup = {str(row["trade_id"]): row for row in self.state.open_trades.to_dicts()}
        trade = trade_lookup.get(trade_id)
        if not trade:
            return None
        bybit_side = str(order.get("side") or "")
        side = str(trade.get("side") or ("short" if bybit_side == "Buy" else "long" if bybit_side == "Sell" else ""))
        return {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "qty": str(order.get("target_qty") or order.get("qty") or trade.get("qty") or ""),
            "exit_reason": str(order.get("exit_reason") or "ws_order_ack_failed"),
            "exit_trigger_ts_ms": _int(order.get("exit_trigger_ts_ms")) or _now_ms(),
            "planned_exit_price": self.state.price_by_symbol.get(symbol, _float(order.get("avg_price"))),
        }

    def ws_exit(self, exit_plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        trade_lookup = {str(row["trade_id"]): row for row in self.state.all_trades.to_dicts()}
        trade = dict(trade_lookup[str(exit_plan["trade_id"])])
        side = str(exit_plan.get("side") or trade.get("side") or "short")
        bybit_side = "Buy" if side == "short" else "Sell"
        symbol = str(exit_plan["symbol"])
        qty = str(exit_plan.get("qty") or trade.get("qty"))
        link = _risk_order_link_id("wx", symbol=symbol, ts_ms=_now_ms(), attempt=0)
        order_params = _order_params(
            symbol=symbol,
            side=bybit_side,
            qty=qty,
            order_type="Market",
            order_link_id=link,
            reduce_only=True,
        )
        def enqueue_ack(message: dict[str, Any]) -> None:
            payload = dict(message) if isinstance(message, dict) else {"message": message}
            payload["_agc_order_link_id"] = link
            self.events.put(("ws_order_ack", payload))

        self.trade_client.place_order(enqueue_ack, **order_params)
        self.state.submitted_link_to_trade_id[link] = str(trade["trade_id"])
        self.state.submitted_link_submit_mode[link] = "ws_submitted"
        order_row = {
            "order_link_id": link,
            "ts_ms": _now_ms(),
            "trade_id": str(trade["trade_id"]),
            "symbol": symbol,
            "side": bybit_side,
            "order_type": "Market",
            "qty": qty,
            "reduce_only": True,
            "order_id": "",
            "submit_mode": "ws_submitted",
            "avg_price": 0.0,
            "notional_usdt": 0.0,
            "status": "submitted_unconfirmed",
            "exit_reason": str(exit_plan["exit_reason"]),
            "exit_trigger_ts_ms": int(exit_plan["exit_trigger_ts_ms"]),
            "target_qty": qty,
            "filled_qty": "",
        }
        return [], [order_row]

    def rest_exit(self, exits: list[dict[str, Any]], *, submit_orders: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rest_risk = EventRiskCycleConfig(
            submit_orders=submit_orders,
            confirm_demo_orders=self.risk.confirm_demo_orders,
            telegram=False,
            repair_stops=False,
            exit_order_mode="market",
            settle_coin=self.risk.settle_coin,
        )
        return _execute_risk_exits(
            exits,
            self.state.all_trades,
            trading_client=self.private_client,
            risk=rest_risk,
            now_ms=_now_ms(),
            price_by_symbol=self.state.price_by_symbol,
            tick_size_by_symbol={},
        )

    def repair_exchange_stops(self) -> None:
        if not self.risk.repair_stops:
            return
        repairs = plan_stop_repairs(
            self.state.open_trades,
            position_by_symbol=self.state.positions_by_symbol,
            skip_symbols=self.state.submitted_symbols | self.state.live_exit_order_symbols,
            tolerance_bps=self.risk.stop_tolerance_bps,
        )
        if not repairs:
            return
        rows = _execute_stop_repairs(
            repairs,
            trading_client=self.private_client,
            risk=EventRiskCycleConfig(
                submit_orders=self.risk.submit_orders,
                confirm_demo_orders=self.risk.confirm_demo_orders,
                repair_stops=True,
                settle_coin=self.risk.settle_coin,
            ),
            now_ms=_now_ms(),
        )
        if rows:
            _write_order_rows(self.root, pl.DataFrame(rows, infer_schema_length=None))
            self.state.repairs.extend(rows)

    def reconcile_positions(self, *, write: bool) -> list[dict[str, Any]]:
        reconciled, rows = _risk_reconcile_missing_positions(
            self.state.open_trades,
            position_by_symbol=self.state.positions_by_symbol,
            now_ms=_now_ms(),
            enabled=self.risk.submit_orders and self.private_client is not None,
        )
        self.state.open_trades = reconciled
        if rows:
            self.state.all_trades = _upsert_rows(self.state.all_trades, rows, key="trade_id")
            self.state.reconciliations.extend(rows)
            for row in rows:
                self.clear_submitted_symbol(str(row.get("symbol", "")))
            if write:
                _write_trade_rows(self.root, pl.DataFrame(rows, infer_schema_length=None))
        return rows

    def rest_reconcile(self) -> None:
        raw_positions, error = _safe_raw_positions(self.private_client, settle_coin=self.risk.settle_coin)
        if error:
            self.state.errors.append(error)
            return
        self.state.positions_by_symbol = _active_position_by_symbol(raw_positions)
        self.state.price_by_symbol.update(_price_lookup_from_positions(self.state.positions_by_symbol))
        self.state.all_trades = read_dataset(self.root, "event_demo_trades")
        self.state.open_trades = _open_trades(self.state.all_trades)
        orders = read_dataset(self.root, "event_demo_orders")
        open_orders_ok = self.refresh_live_exit_order_symbols()
        self.reconcile_pending_order_fills(orders)
        orders = read_dataset(self.root, "event_demo_orders")
        self.load_pending_entry_orders(orders)
        self.load_pending_exit_orders(orders)
        if open_orders_ok:
            self.reconcile_flat_pending_exit_orders(orders)
            orders = read_dataset(self.root, "event_demo_orders")
            self.terminalize_stale_pending_entry_orders(orders)
        self.reconcile_positions(write=True)
        self.evaluate_symbols(set(self.state.positions_by_symbol))
        self.repair_exchange_stops()
        self.reconcile_untracked_exit_orders()
        self.exit_untracked_positions()
        self.subscribe_tickers(set(self.state.positions_by_symbol) | set(_column_values(self.state.open_trades, "symbol")))
        self.state.last_reconcile_monotonic = time.monotonic()

    def exit_untracked_positions(self) -> None:
        if not self.risk.exit_untracked_positions:
            return
        self.expire_stale_submitted_symbols()
        open_symbols = set(_column_values(self.state.open_trades, "symbol"))
        now_ms = _now_ms()
        rows: list[dict[str, Any]] = []
        for position in list(self.state.positions_by_symbol.values()):
            symbol = str(position.get("symbol", ""))
            qty = str(position.get("size") or "")
            if (
                not symbol
                or symbol in open_symbols
                or symbol in self.state.pending_entry_symbols
                or self.exit_submission_active(symbol)
                or _float(qty) <= 0.0
            ):
                continue
            side_text = str(position.get("side") or "").lower()
            close_side = "Sell" if side_text in {"buy", "long"} else "Buy"
            attempt = sum(
                1
                for order in self.state.orders
                if str(order.get("symbol", "")) == symbol and str(order.get("exit_reason", "")) == "untracked_position"
            )
            link = _risk_order_link_id("ux", symbol=symbol, ts_ms=now_ms, attempt=attempt)
            order_result: dict[str, Any] = {}
            exec_summary: dict[str, Any] = {}
            submit_mode = "dry_run"
            status = "planned"
            error = ""
            if self.risk.submit_orders:
                if not self.risk.rest_fallback:
                    submit_mode = "error"
                    status = "failed"
                    error = "untracked position exit requires REST fallback in Bybit demo mode"
                else:
                    try:
                        assert self.private_client is not None
                        order_result = self.private_client.place_order(
                            **_order_params(
                                symbol=symbol,
                                side=close_side,
                                qty=qty,
                                order_type="Market",
                                order_link_id=link,
                                reduce_only=True,
                            )
                        )
                        submit_mode = "submitted"
                    except Exception as exc:  # noqa: BLE001 - untracked positions must be surfaced and retried
                        submit_mode = "error"
                        status = "failed"
                        error = str(exc)[:500]
                        self.state.errors.append(error)
                    if submit_mode == "submitted":
                        try:
                            exec_summary = _execution_summary(
                                self.private_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50)
                            )
                        except Exception as exc:  # noqa: BLE001 - accepted reduce-only order remains pending for reconciliation
                            status = "submitted_unconfirmed"
                            error = f"fill confirmation failed: {exc}"[:500]
                            self.mark_submitted_symbol(symbol, now_ms=now_ms)
                        else:
                            filled_qty = _float(exec_summary.get("qty"))
                            target_qty = _float(qty)
                            if target_qty > 0.0 and filled_qty + max(target_qty * 1e-8, 1e-12) >= target_qty:
                                status = "filled"
                                self.state.positions_by_symbol.pop(symbol, None)
                            elif filled_qty > 0.0:
                                status = "partial"
                                self.mark_submitted_symbol(symbol, now_ms=now_ms)
                            else:
                                status = "submitted_unconfirmed"
                                self.mark_submitted_symbol(symbol, now_ms=now_ms)
            filled_qty = _float(exec_summary.get("qty")) if exec_summary else 0.0
            avg_price = _float(exec_summary.get("avg_price")) or _position_price(position)
            rows.append(
                {
                    "order_link_id": link,
                    "ts_ms": now_ms,
                    "trade_id": "",
                    "symbol": symbol,
                    "side": close_side,
                    "order_type": "Market",
                    "qty": qty,
                    "reduce_only": True,
                    "order_id": order_result.get("orderId", ""),
                    "submit_mode": submit_mode,
                    "avg_price": avg_price,
                    "notional_usdt": abs(avg_price * filled_qty) if avg_price > 0.0 else 0.0,
                    "status": status,
                    "exit_reason": "untracked_position",
                    "target_qty": qty,
                    "filled_qty": str(filled_qty) if filled_qty > 0.0 else "",
                    "error": error,
                }
            )
        if not rows:
            return
        _write_order_rows(self.root, pl.DataFrame(rows, infer_schema_length=None))
        self.state.orders.extend(rows)
        self.write_report(reason="untracked_exit_submitted")

    def reconcile_untracked_exit_orders(self) -> None:
        if self.private_client is None:
            return
        active_symbols = set(self.state.positions_by_symbol)
        updates: list[dict[str, Any]] = []
        for order in self.state.orders:
            if str(order.get("exit_reason", "")) != "untracked_position":
                continue
            if str(order.get("status", "")) not in PENDING_ORDER_STATUSES:
                continue
            symbol = str(order.get("symbol", ""))
            link = str(order.get("order_link_id", ""))
            target_qty = _float(order.get("target_qty") or order.get("qty"))
            position_flat = symbol and symbol not in active_symbols
            try:
                summary = _execution_summary(self.private_client.get_trade_history(symbol=symbol, order_link_id=link, limit=50))
            except Exception as exc:  # noqa: BLE001 - keep pending guard active and retry
                if position_flat:
                    summary = {"qty": "", "avg_price": 0.0, "fee": 0.0, "executions": 0}
                else:
                    order["error"] = f"fill reconciliation failed: {exc}"[:500]
                    order["updated_at_ms"] = _now_ms()
                    updates.append(dict(order))
                    self.mark_submitted_symbol(symbol)
                    continue
            filled_qty = _float(summary.get("qty"))
            avg_price = _float(summary.get("avg_price")) or _float(order.get("avg_price"))
            if filled_qty <= 0.0 and position_flat:
                filled_qty = target_qty
            if filled_qty <= 0.0:
                continue
            full = target_qty > 0.0 and filled_qty + max(target_qty * 1e-8, 1e-12) >= target_qty
            order["status"] = "filled" if full or position_flat else "partial"
            order["filled_qty"] = str(filled_qty)
            order["avg_price"] = avg_price
            order["notional_usdt"] = abs(avg_price * filled_qty) if avg_price > 0.0 else 0.0
            updates.append(dict(order))
            if order["status"] == "filled":
                self.clear_submitted_symbol(symbol)
            else:
                self.mark_submitted_symbol(symbol)
        if updates:
            _write_order_rows(self.root, pl.DataFrame(updates, infer_schema_length=None))

    def reconcile_flat_pending_exit_orders(self, orders: pl.DataFrame) -> None:
        if orders.is_empty():
            return
        active_symbols = set(self.state.positions_by_symbol)
        trade_lookup = {str(row["trade_id"]): row for row in self.state.open_trades.to_dicts()}
        now_ms = _now_ms()
        order_updates: list[dict[str, Any]] = []
        trade_updates: list[dict[str, Any]] = []
        for order in orders.to_dicts():
            if not _bool(order.get("reduce_only")):
                continue
            if str(order.get("status", "")) not in PENDING_ORDER_STATUSES:
                continue
            if not str(order.get("exit_reason", "")):
                continue
            symbol = str(order.get("symbol") or "")
            link = str(order.get("order_link_id") or "")
            if not symbol or not link:
                continue
            if symbol in active_symbols or symbol in self.state.live_exit_order_symbols:
                continue
            target_qty = str(order.get("target_qty") or order.get("qty") or "")
            filled_qty = target_qty if _float(target_qty) > 0.0 else str(order.get("filled_qty") or "")
            avg_price = _float(order.get("avg_price"))
            filled_qty_float = _float(filled_qty)
            order_update = dict(order)
            order_update.update(
                {
                    "status": "filled",
                    "filled_qty": filled_qty,
                    "notional_usdt": abs(avg_price * filled_qty_float) if avg_price > 0.0 else _float(order.get("notional_usdt")),
                    "updated_at_ms": now_ms,
                }
            )
            if not str(order_update.get("error") or ""):
                order_update["error"] = "filled inferred from flat Bybit position"
            order_updates.append(order_update)
            self.clear_submitted_symbol(symbol)
            loaded = False
            for state_order in self.state.orders:
                if str(state_order.get("order_link_id") or "") == link:
                    state_order.update(order_update)
                    loaded = True
            if not loaded:
                self.state.orders.append(order_update)

            trade_id = str(order.get("trade_id") or "")
            trade = dict(trade_lookup.get(trade_id, {}))
            if not trade:
                continue
            trade.update(
                {
                    "status": "closed",
                    "exit_ts_ms": now_ms,
                    "exit_trigger_ts_ms": _int(order.get("exit_trigger_ts_ms")) or now_ms,
                    "exit_price": avg_price,
                    "exit_reason": str(order.get("exit_reason") or "pending_exit_position_flat"),
                    "exit_order_link_id": link,
                    "exit_order_id": order.get("order_id", ""),
                    "submit_mode": str(order.get("submit_mode") or "position_flat_reconciled"),
                    "closed_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
            )
            trade_updates.append(trade)
        if order_updates:
            _write_order_rows(self.root, pl.DataFrame(order_updates, infer_schema_length=None))
        if trade_updates:
            self.state.all_trades = _upsert_rows(self.state.all_trades, trade_updates, key="trade_id")
            self.state.open_trades = _open_trades(self.state.all_trades)
            self.state.pending_fill_reconciliations.extend(trade_updates)
            _write_trade_rows(self.root, pl.DataFrame(trade_updates, infer_schema_length=None))

    def refresh_live_exit_order_symbols(self) -> bool:
        open_orders, error = _safe_open_orders(self.private_client, settle_coin=self.risk.settle_coin)
        if error:
            self.state.errors.append(error)
            return False
        self.state.live_entry_order_symbols = _live_open_order_symbols(open_orders, reduce_only=False)
        self.state.live_exit_order_symbols = _live_open_order_symbols(open_orders, reduce_only=True)
        return True

    def exit_submission_active(self, symbol: str) -> bool:
        return symbol in self.state.submitted_symbols or symbol in self.state.live_exit_order_symbols

    def reconcile_pending_order_fills(self, orders: pl.DataFrame) -> None:
        if orders.is_empty() or self.private_client is None:
            return
        trade_rows, order_rows = _reconcile_pending_order_fills(
            orders,
            self.state.all_trades,
            trading_client=self.private_client,
            demo=EventDemoCycleConfig(
                submit_orders=self.risk.submit_orders,
                confirm_demo_orders=self.risk.confirm_demo_orders,
            ),
            now_ms=_now_ms(),
            live_position_symbols=set(self.state.positions_by_symbol),
            live_open_order_symbols=self.state.live_entry_order_symbols | self.state.live_exit_order_symbols,
        )
        if trade_rows:
            self.state.all_trades = _upsert_rows(self.state.all_trades, trade_rows, key="trade_id")
            self.state.open_trades = _open_trades(self.state.all_trades)
            self.state.pending_fill_reconciliations.extend(trade_rows)
            _write_trade_rows(self.root, pl.DataFrame(trade_rows, infer_schema_length=None))
        if order_rows:
            for update in order_rows:
                link = str(update.get("order_link_id") or "")
                for order in self.state.orders:
                    if str(order.get("order_link_id") or "") == link:
                        order.update(update)
            _write_order_rows(self.root, pl.DataFrame(order_rows, infer_schema_length=None))

    def terminalize_stale_pending_entry_orders(self, orders: pl.DataFrame) -> None:
        if orders.is_empty():
            return
        order_rows = _terminalize_stale_pending_entry_orders(
            orders,
            live_position_symbols=set(self.state.positions_by_symbol),
            live_open_entry_order_symbols=self.state.live_entry_order_symbols,
            now_ms=_now_ms(),
        )
        if not order_rows:
            return
        for update in order_rows:
            symbol = str(update.get("symbol") or "")
            if symbol:
                self.state.pending_entry_symbols.discard(symbol)
            link = str(update.get("order_link_id") or "")
            for order in self.state.orders:
                if str(order.get("order_link_id") or "") == link:
                    order.update(update)
        _write_order_rows(self.root, pl.DataFrame(order_rows, infer_schema_length=None))

    def on_idle(self) -> None:
        now = time.monotonic()
        self.reconcile_stale_websocket(now)
        if self.risk.rest_reconcile_seconds > 0 and now - self.state.last_reconcile_monotonic >= self.risk.rest_reconcile_seconds:
            self.rest_reconcile()
        if self.risk.heartbeat_seconds > 0 and now - self.state.last_report_monotonic >= self.risk.heartbeat_seconds:
            self.write_report(reason="heartbeat")

    def reconcile_stale_websocket(self, now: float) -> None:
        if self.risk.stale_ws_seconds <= 0.0 or not self.risk.rest_fallback:
            return
        has_active_work = bool(self.state.subscribed_symbols or self.state.positions_by_symbol) or not self.state.open_trades.is_empty()
        if not has_active_work:
            return
        ws_age = now - self.state.last_ws_event_monotonic
        if ws_age < self.risk.stale_ws_seconds:
            return
        if now - self.state.last_stale_reconcile_monotonic < self.risk.stale_ws_seconds:
            return
        self.state.errors.append(f"websocket stale for {ws_age:.1f}s; forced REST reconcile")
        self.rest_reconcile()
        self.state.last_stale_reconcile_monotonic = now

    def load_pending_exit_orders(self, orders: pl.DataFrame) -> None:
        if orders.is_empty():
            return
        open_trade_ids = set(_column_values(self.state.open_trades, "trade_id"))
        loaded_order_links = {str(order.get("order_link_id") or "") for order in self.state.orders}
        now_ms = _now_ms()
        max_age_ms = max(self.risk.pending_exit_guard_seconds, 0.0) * 1000.0
        for row in orders.to_dicts():
            link = str(row.get("order_link_id") or "")
            trade_id = str(row.get("trade_id") or "")
            symbol = str(row.get("symbol") or "")
            exit_reason = str(row.get("exit_reason", ""))
            is_untracked_exit = exit_reason == "untracked_position"
            if not link or not symbol:
                continue
            if trade_id:
                if trade_id not in open_trade_ids:
                    continue
            elif not is_untracked_exit:
                continue
            if not _bool(row.get("reduce_only")) or not exit_reason:
                continue
            if str(row.get("status", "")) not in PENDING_ORDER_STATUSES:
                continue
            ts_ms = int(row.get("ts_ms") or 0)
            if ts_ms > 0 and max_age_ms > 0 and now_ms - ts_ms > max_age_ms:
                continue
            if trade_id:
                self.state.submitted_link_to_trade_id[link] = trade_id
            self.state.submitted_link_submit_mode[link] = str(row.get("submit_mode") or "submitted")
            self.mark_submitted_symbol(symbol, now_ms=ts_ms or now_ms)
            if link not in loaded_order_links:
                self.state.orders.append(dict(row))
                loaded_order_links.add(link)

    def load_pending_entry_orders(self, orders: pl.DataFrame) -> None:
        self.state.pending_entry_symbols.clear()
        if orders.is_empty():
            return
        open_symbols = set(_column_values(self.state.open_trades, "symbol"))
        now_ms = _now_ms()
        for row in orders.to_dicts():
            symbol = str(row.get("symbol") or "")
            link = str(row.get("order_link_id") or "")
            trade_id = str(row.get("trade_id") or "")
            if not symbol or not link or not trade_id or symbol in open_symbols:
                continue
            if _bool(row.get("reduce_only")):
                continue
            if str(row.get("status", "")) not in PENDING_ORDER_STATUSES:
                continue
            ts_ms = _int(row.get("ts_ms"))
            if ts_ms > 0 and now_ms - ts_ms > PENDING_ORDER_GUARD_MS:
                continue
            self.state.pending_entry_symbols.add(symbol)

    def mark_submitted_symbol(self, symbol: str, *, now_ms: int | None = None) -> None:
        if not symbol:
            return
        self.state.submitted_symbols.add(symbol)
        self.state.submitted_symbol_ts_ms[symbol] = now_ms if now_ms is not None else _now_ms()

    def clear_submitted_symbol(self, symbol: str) -> None:
        if not symbol:
            return
        self.state.submitted_symbols.discard(symbol)
        self.state.submitted_symbol_ts_ms.pop(symbol, None)

    def expire_stale_submitted_symbols(self) -> None:
        max_age_ms = max(self.risk.pending_exit_guard_seconds, 0.0) * 1000.0
        if max_age_ms <= 0.0:
            return
        now_ms = _now_ms()
        for symbol, ts_ms in list(self.state.submitted_symbol_ts_ms.items()):
            if ts_ms > 0 and now_ms - ts_ms > max_age_ms:
                self.clear_submitted_symbol(symbol)

    def write_report(self, *, reason: str) -> dict[str, Any]:
        now_ms = _now_ms()
        position_snapshot = build_position_pnl_snapshot(list(self.state.positions_by_symbol.values()))
        bybit_summary = summarize_position_pnl(position_snapshot)
        ledger_positions = build_ledger_position_pnl_snapshot(self.state.open_trades, self.state.price_by_symbol)
        ledger_summary = summarize_position_pnl(ledger_positions)
        open_symbols = set(_column_values(self.state.open_trades, "symbol"))
        pending_entry_fills = sum(1 for row in self.state.pending_fill_reconciliations if str(row.get("status", "")) == "open")
        pending_exit_fills = sum(1 for row in self.state.pending_fill_reconciliations if str(row.get("status", "")) == "closed")
        pending_entry_positions = [
            row
            for row in position_snapshot
            if str(row.get("symbol", "")) and str(row.get("symbol", "")) in self.state.pending_entry_symbols
            and str(row.get("symbol", "")) not in open_symbols
        ]
        untracked_positions = [
            row
            for row in position_snapshot
            if str(row.get("symbol", ""))
            and str(row.get("symbol", "")) not in open_symbols
            and str(row.get("symbol", "")) not in self.state.pending_entry_symbols
        ]
        cycle = {
            "cycle_id": f"ws-risk-{now_ms}",
            "ts_ms": now_ms,
            "mode": "ws_risk_submit" if self.risk.submit_orders else "ws_risk_dry_run",
            "reason": reason,
            "symbols": len(open_symbols),
            "entry_candidates": 0,
            "entries_executed": 0,
            "exit_candidates": len(self.state.orders),
            "exits_executed": len(self.state.exits),
            "stop_repairs": len(self.state.repairs),
            "pending_entry_positions": len(pending_entry_positions),
            "pending_fills_reconciled": len(self.state.pending_fill_reconciliations),
            "pending_order_fills_reconciled": len(self.state.pending_fill_reconciliations),
            "pending_entry_fills_reconciled": pending_entry_fills,
            "pending_exit_fills_reconciled": pending_exit_fills,
            "untracked_exits_submitted": sum(1 for row in self.state.orders if str(row.get("exit_reason", "")) == "untracked_position"),
            "bybit_live_exit_open_orders": len(self.state.live_exit_order_symbols),
            "open_trades_before": self.state.open_trades.height,
            "open_trades_after": self.state.open_trades.height,
            "equity_usdt": 0.0,
            "bybit_positions": bybit_summary["positions"],
            "bybit_position_value_usdt": bybit_summary["position_value_usdt"],
            "bybit_unrealized_pnl_usdt": bybit_summary["unrealized_pnl_usdt"],
            "bybit_position_pnl_pct": bybit_summary["pnl_pct"],
            "ledger_positions": ledger_summary["positions"],
            "ledger_position_value_usdt": ledger_summary["position_value_usdt"],
            "ledger_unrealized_pnl_usdt": ledger_summary["unrealized_pnl_usdt"],
            "ledger_position_pnl_pct": ledger_summary["pnl_pct"],
            "position_report_error": "; ".join(self.state.errors[-3:]),
            "untracked_positions": len(untracked_positions),
            "ws_order_unavailable": self.state.ws_order_unavailable,
            "telegram_sent": False,
            "telegram_error": "",
        }
        payload = {
            "cycle": cycle,
            "risk_config": asdict(self.risk),
            "exits": self.state.exits[-20:],
            "exit_orders": self.state.orders[-20:],
            "stop_repairs": self.state.repairs[-20:],
            "reconciliations": self.state.reconciliations[-20:],
            "pending_fill_reconciliations": self.state.pending_fill_reconciliations[-20:],
            "pending_entry_positions": pending_entry_positions,
            "untracked_positions": untracked_positions,
            "bybit_positions": position_snapshot,
            "bybit_position_summary": bybit_summary,
            "ledger_positions": ledger_positions,
            "ledger_position_summary": ledger_summary,
            "report_dir": str(self.report_dir),
        }
        telegram_sent, telegram_error = self.maybe_notify(payload)
        cycle["telegram_sent"] = telegram_sent
        cycle["telegram_error"] = telegram_error
        payload["cycle"] = cycle
        latest_json_path = self.report_dir / "latest_event_ws_risk_cycle.json"
        latest_md_path = self.report_dir / "latest_event_ws_risk_cycle.md"
        payload["report_path"] = str(latest_md_path)
        if _persist_ws_risk_history(payload):
            history_json_path = self.report_dir / f"event_ws_risk_cycle_{cycle['cycle_id']}.json"
            history_md_path = self.report_dir / f"event_ws_risk_cycle_{cycle['cycle_id']}.md"
            payload["history_report_path"] = str(history_md_path)
            history_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            history_md_path.write_text(format_event_risk_cycle_report(payload), encoding="utf-8")
        write_dataset(pl.DataFrame([cycle]), self.root, "event_demo_cycles", partition_by=())
        latest_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        latest_md_path.write_text(format_event_risk_cycle_report(payload), encoding="utf-8")
        self.state.last_report_monotonic = time.monotonic()
        return payload

    def maybe_notify(self, payload: dict[str, Any]) -> tuple[bool, str]:
        if not self.risk.telegram:
            return False, "disabled"
        reason = _telegram_notification_reason(payload)
        if not reason:
            return False, "quiet_no_material_event"
        key = _telegram_dedupe_key(reason, payload)
        if key in self.state.telegram_keys_sent:
            return False, "duplicate_material_event"
        sent, error = _maybe_notify(payload, enabled=True)
        if sent:
            self.state.telegram_keys_sent.add(key)
            _write_telegram_dedupe_keys(self.report_dir, self.state.telegram_keys_sent)
        return sent, error

    def close(self) -> None:
        for client in (self.private_stream, self.public_stream, self.trade_client):
            close = getattr(client, "close", None)
            if callable(close):
                close()


def run_event_ws_risk(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    risk_config: EventWebSocketRiskConfig | None = None,
    private_client: Any | None = None,
    private_stream: Any | None = None,
    public_stream: Any | None = None,
    trade_client: Any | None = None,
) -> dict[str, Any]:
    root = Path(data_root).expanduser()
    with exclusive_file_lock(root / ".locks" / "event_ws_risk_cycle.lock", stale_seconds=0, poll_seconds=0.05):
        engine = EventWebSocketRiskEngine(
            root,
            config=config,
            risk_config=risk_config,
            private_client=private_client,
            private_stream=private_stream,
            public_stream=public_stream,
            trade_client=trade_client,
        )
        try:
            return engine.run()
        finally:
            engine.close()


def _build_private_stream(config: ResearchConfig) -> BybitPrivateWebSocketStream:
    api_key = os.environ.get("BYBIT_DEMO_API_KEY")
    api_secret = os.environ.get("BYBIT_DEMO_API_SECRET")
    return BybitPrivateWebSocketStream(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
    )


def _build_ws_trade_client(config: ResearchConfig) -> BybitWebSocketTradeClient:
    api_key = os.environ.get("BYBIT_DEMO_API_KEY")
    api_secret = os.environ.get("BYBIT_DEMO_API_SECRET")
    return BybitWebSocketTradeClient(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
    )


def _persist_ws_risk_history(payload: dict[str, Any]) -> bool:
    reason = str(payload.get("cycle", {}).get("reason") or "")
    return reason != "heartbeat" or bool(_telegram_notification_reason(payload))


def _validate_ws_risk_config(config: EventWebSocketRiskConfig) -> None:
    if config.submit_orders and not config.confirm_demo_orders:
        raise RuntimeError("Refusing to submit demo websocket risk orders without --confirm-demo-orders")
    if config.order_submit_mode not in {"ws", "ws_then_rest", "rest"}:
        raise ValueError("order_submit_mode must be ws, ws_then_rest, or rest")
    if config.order_submit_mode == "ws" and config.rest_fallback:
        raise ValueError("pure ws order mode must set rest_fallback=False")
    if config.rest_reconcile_seconds < 0.0 or config.heartbeat_seconds < 0.0:
        raise ValueError("heartbeat and reconcile intervals must be non-negative")
    if config.max_runtime_seconds < 0.0:
        raise ValueError("max_runtime_seconds must be non-negative")
    if config.stream_start_timeout_seconds < 0.0:
        raise ValueError("stream_start_timeout_seconds must be non-negative")
    if config.pending_exit_guard_seconds < 0.0:
        raise ValueError("pending_exit_guard_seconds must be non-negative")
    if config.exit_untracked_positions and config.order_submit_mode == "ws" and not config.rest_fallback:
        raise ValueError("exit_untracked_positions requires REST fallback in Bybit demo mode")


_DEMO_WS_TRADE_UNAVAILABLE = (
    "Bybit demo WebSocket Trade order entry is unavailable; using REST fallback for demo reduce-only exits."
)
TELEGRAM_DEDUPE_RETENTION_SECONDS = 24 * 60 * 60


def _message_rows(message: dict[str, Any]) -> list[dict[str, Any]]:
    data = message.get("data", message)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _ack_order_link(message: dict[str, Any]) -> str:
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    return str(
        message.get("_agc_order_link_id")
        or message.get("orderLinkId")
        or message.get("order_link_id")
        or data.get("orderLinkId")
        or data.get("order_link_id")
        or ""
    )


def _position_price(row: dict[str, Any]) -> float:
    return _first_price(row, ("markPrice", "mark_price", "lastPrice", "indexPrice", "avgPrice"))


def _first_price(row: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _float(row.get(key))
        if value > 0.0:
            return value
    return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _telegram_dedupe_key(reason: str, payload: dict[str, Any]) -> str:
    cycle = payload.get("cycle", {})
    order_links = sorted(
        str(row.get("order_link_id") or "")
        for row in payload.get("exit_orders", [])
        if str(row.get("order_link_id") or "")
    ) + sorted(
        str(row.get("entry_order_link_id") or row.get("exit_order_link_id") or row.get("order_link_id") or "")
        for row in payload.get("pending_fill_reconciliations", [])
        if str(row.get("entry_order_link_id") or row.get("exit_order_link_id") or row.get("order_link_id") or "")
    )
    symbols = sorted(
        str(row.get("symbol") or "")
        for row in payload.get("untracked_positions", []) + payload.get("bybit_positions", [])
        if str(row.get("symbol") or "")
    )
    repairs = sorted(
        "|".join(
            [
                str(row.get("symbol") or ""),
                f"{_float(row.get('stop_price')):.12g}",
                f"{_float(row.get('take_profit_price')):.12g}",
                str(row.get("status") or ""),
                str(row.get("submit_mode") or ""),
                str(row.get("error") or "")[:160],
            ]
        )
        for row in payload.get("stop_repairs", [])
        if str(row.get("symbol") or "")
    )
    error = str(cycle.get("position_report_error") or "")[:160]
    return "|".join(
        [
            reason,
            ",".join(order_links[-8:]),
            ",".join(repairs[-8:]),
            ",".join(symbols),
            error,
        ]
    )


def _telegram_dedupe_path(report_dir: Path) -> Path:
    return report_dir / "telegram_dedupe_keys.json"


def _read_telegram_dedupe_keys(report_dir: Path, *, now: float | None = None) -> set[str]:
    current = time.time() if now is None else now
    payload = _read_telegram_dedupe_key_payload(report_dir)
    return {
        key
        for key, sent_at in payload.items()
        if current - sent_at <= TELEGRAM_DEDUPE_RETENTION_SECONDS
    }


def _write_telegram_dedupe_keys(report_dir: Path, keys: set[str], *, now: float | None = None) -> None:
    current = time.time() if now is None else now
    existing = _read_telegram_dedupe_key_payload(report_dir)
    output = {
        key: float(existing.get(key, current))
        for key in sorted(keys)
        if current - float(existing.get(key, current)) <= TELEGRAM_DEDUPE_RETENTION_SECONDS
    }
    path = _telegram_dedupe_path(report_dir)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp_path.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _read_telegram_dedupe_key_payload(report_dir: Path) -> dict[str, float]:
    path = _telegram_dedupe_path(report_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if isinstance(payload, list):
        timestamp = time.time()
        return {str(item): timestamp for item in payload if item}
    if not isinstance(payload, dict):
        return {}
    output: dict[str, float] = {}
    for key, value in payload.items():
        try:
            output[str(key)] = float(value)
        except (TypeError, ValueError):
            output[str(key)] = time.time()
    return output


def _call_with_timeout(label: str, func: Any, *, timeout_seconds: float) -> tuple[Any, str]:
    timeout = max(float(timeout_seconds), 0.0)
    if timeout <= 0.0:
        try:
            return func(), ""
        except Exception as exc:  # noqa: BLE001 - caller surfaces third-party transport failures
            return None, f"{label} failed: {exc}"[:500]
    result_queue: queue.Queue[tuple[Any, str]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put((func(), ""))
        except Exception as exc:  # noqa: BLE001 - caller surfaces third-party transport failures
            result_queue.put((None, f"{label} failed: {exc}"[:500]))

    thread = threading.Thread(target=worker, name=f"agc-{_thread_name(label)}", daemon=True)
    thread.start()
    try:
        return result_queue.get(timeout=timeout)
    except queue.Empty:
        return None, f"{label} timed out after {timeout:.2f}s; REST reconciliation remains active"


def _thread_name(label: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in label.lower())[:48]


def _now_ms() -> int:
    return int(time.time() * 1000)
