#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import traceback
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any

from aggression_carry.bybit import BybitMarketData, BybitPrivateClient, BybitWebSocketTradeClient
from aggression_carry.event_demo import (
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    _execute_entries,
    _execution_summary,
    _float,
    _submit_reduce_only_exit,
    _wallet_equity_usdt,
)


def main() -> int:
    if os.environ.get("CONFIRM_DEMO_ORDERS") != "1":
        raise RuntimeError("Set CONFIRM_DEMO_ORDERS=1 to run the demo lifecycle proof")
    api_key = os.environ.get("BYBIT_DEMO_API_KEY")
    api_secret = os.environ.get("BYBIT_DEMO_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("Set BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET")

    symbol = os.environ.get("PROOF_SYMBOL", "BTCUSDT").upper()
    target_pct = float(os.environ.get("PROOF_TARGET_PCT", "0.20"))
    started = datetime.now(UTC)
    report_dir = Path(os.environ.get("PROOF_REPORT_DIR", "data/bybit-demo-event/reports/system-audit"))
    report_dir.mkdir(parents=True, exist_ok=True)

    market = BybitMarketData(category="linear", testnet=False)
    private = BybitPrivateClient(category="linear", testnet=False, demo=True, api_key=api_key, api_secret=api_secret)
    contract = _contract_for(market, symbol)
    reference_price = _ticker_price(market, symbol)
    equity = _wallet_equity_usdt(private, demo=EventDemoCycleConfig())

    pre_positions = _nonzero_positions(private)
    pre_open_orders = _open_orders(private)
    if pre_positions or pre_open_orders:
        raise RuntimeError(f"Refusing proof test with existing exposure: positions={pre_positions} open_orders={pre_open_orders}")

    report: dict[str, Any] = {
        "started_at": started.isoformat(),
        "symbol": symbol,
        "target_pct_equity": target_pct,
        "equity_usdt": equity,
        "reference_price": reference_price,
        "contract": contract,
        "pre_positions": pre_positions,
        "pre_open_orders": pre_open_orders,
        "strategy_sized_short": {},
        "websocket_order_entry_probe": {},
        "cleanup": {},
        "errors": [],
    }

    try:
        _run_strategy_sized_short_proof(
            report,
            market=market,
            private=private,
            symbol=symbol,
            contract=contract,
            reference_price=reference_price,
            equity=equity,
            target_pct=target_pct,
        )
        if os.environ.get("PROOF_WS", "1") == "1":
            _run_ws_order_entry_probe(report, market=market, private=private, symbol=symbol, contract=contract)
    except Exception as exc:  # noqa: BLE001 - report and cleanup are the point of this tool
        report["errors"].append(str(exc))
        report["traceback_tail"] = traceback.format_exc()[-2000:]
    finally:
        report["cleanup"] = _cleanup_flat(private, symbol)
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["post_positions_all"] = _nonzero_positions(private)
        report["post_open_orders_all"] = _open_orders(private)

    path = report_dir / f"prove_unproven_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    latest = report_dir / "latest_prove_unproven.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    latest.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    summary = {
        "report": str(path),
        "errors": report["errors"],
        "entry_notional": report.get("strategy_sized_short", {}).get("entry", {}).get("actual_notional_usdt"),
        "entry_pct_equity": report.get("strategy_sized_short", {}).get("entry", {}).get("actual_pct_equity"),
        "native_stop_loss": report.get("strategy_sized_short", {}).get("entry", {}).get("native_stop_loss"),
        "native_take_profit": report.get("strategy_sized_short", {}).get("entry", {}).get("native_take_profit"),
        "entry_history_seen": report.get("strategy_sized_short", {}).get("entry", {}).get("history_seen"),
        "exit_history_seen": report.get("strategy_sized_short", {}).get("exit", {}).get("history_seen"),
        "post_positions": len(report.get("post_positions_all", [])),
        "post_open_orders": len(report.get("post_open_orders_all", [])),
        "ws_conclusion": report.get("websocket_order_entry_probe", {}).get("conclusion"),
        "ws_error": str(report.get("websocket_order_entry_probe", {}).get("error", ""))[:220],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if report["errors"] or report["post_positions_all"] or report["post_open_orders_all"] else 0


def _run_strategy_sized_short_proof(
    report: dict[str, Any],
    *,
    market: BybitMarketData,
    private: BybitPrivateClient,
    symbol: str,
    contract: dict[str, Any],
    reference_price: float,
    equity: float,
    target_pct: float,
) -> None:
    candidate = {
        "trade_id": f"audit-{symbol}-{_now_ms()}",
        "symbol": symbol,
        "side": "short",
        "signal_ts_ms": _now_ms() - 60_000,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.25,
        "scenario_id": "audit-forced-liqmig-short-lifecycle",
        "event_type": "liquidity_migration",
        "threshold": 0.4,
        "side_hypothesis": "reversal",
        "cost_multiplier": 3.0,
        "tradable_membership_flag": True,
    }
    demo_config = EventDemoCycleConfig(
        submit_orders=True,
        confirm_demo_orders=True,
        entry_leverage=2.0,
        order_fill_confirm_seconds=3.0,
        order_fill_poll_interval_seconds=0.25,
    )

    entry_started = time.perf_counter()
    entry_rows, order_rows = _execute_entries(
        [candidate],
        trading_client=private,
        demo=demo_config,
        equity_usdt=equity,
        order_notional_pct_equity=target_pct,
        price_by_symbol={symbol: reference_price},
        contract_by_symbol={symbol: contract},
        now_ms=_now_ms(),
    )
    entry_elapsed_ms = round((time.perf_counter() - entry_started) * 1000.0, 3)
    entry_order = order_rows[0] if order_rows else {}
    entry_row = entry_rows[0] if entry_rows else {}
    entry_link = str(entry_order.get("order_link_id") or entry_row.get("entry_order_link_id") or "")
    entry_immediate = _execution_summary(private.get_trade_history(symbol=symbol, order_link_id=entry_link, limit=50)) if entry_link else {}
    entry_seen = _wait_exec(private, symbol, entry_link, timeout_s=20.0) if entry_link else {"seen": False}

    time.sleep(1.0)
    positions_after_entry = _nonzero_positions(private, symbol)
    open_orders_after_entry = _open_orders(private, symbol)
    position = positions_after_entry[0] if positions_after_entry else {}
    qty = str(entry_order.get("qty") or entry_row.get("qty") or position.get("size") or "")
    notional = _float(entry_order.get("notional_usdt") or entry_row.get("notional_usdt"))
    report["strategy_sized_short"]["entry"] = {
        "elapsed_ms": entry_elapsed_ms,
        "entry_rows": entry_rows,
        "order_rows": order_rows,
        "immediate_exec_summary": entry_immediate,
        "history_seen": entry_seen,
        "positions_after_entry": positions_after_entry,
        "open_orders_after_entry": open_orders_after_entry,
        "native_stop_loss": position.get("stopLoss"),
        "native_take_profit": position.get("takeProfit"),
        "actual_notional_usdt": notional,
        "actual_pct_equity": notional / equity if equity > 0.0 else 0.0,
    }
    if not positions_after_entry:
        raise RuntimeError("Entry did not leave a live position to test exit lifecycle")
    if _float(position.get("stopLoss")) <= 0.0 or _float(position.get("takeProfit")) <= 0.0:
        raise RuntimeError(f"Native stop/take-profit missing after entry: {position}")

    exit_started = time.perf_counter()
    exit_result = _submit_reduce_only_exit(
        symbol=symbol,
        bybit_side="Buy",
        qty=qty,
        trading_client=private,
        risk=EventRiskCycleConfig(submit_orders=True, confirm_demo_orders=True, exit_order_mode="market"),
        now_ms=_now_ms(),
        reference_price=_ticker_price(market, symbol),
        tick_size=float(contract["tick_size"]),
    )
    exit_elapsed_ms = round((time.perf_counter() - exit_started) * 1000.0, 3)
    exit_link = str(exit_result.get("order_link_id") or "")
    exit_immediate = _execution_summary(private.get_trade_history(symbol=symbol, order_link_id=exit_link, limit=50)) if exit_link else {}
    exit_seen = _wait_exec(private, symbol, exit_link, timeout_s=20.0) if exit_link else {"seen": False}
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if not _nonzero_positions(private, symbol):
            break
        time.sleep(0.5)
    report["strategy_sized_short"]["exit"] = {
        "elapsed_ms": exit_elapsed_ms,
        "exit_result": exit_result,
        "immediate_exec_summary": exit_immediate,
        "history_seen": exit_seen,
        "positions_after_exit": _nonzero_positions(private, symbol),
        "open_orders_after_exit": _open_orders(private, symbol),
    }


def _run_ws_order_entry_probe(
    report: dict[str, Any],
    *,
    market: BybitMarketData,
    private: BybitPrivateClient,
    symbol: str,
    contract: dict[str, Any],
) -> None:
    messages: list[Any] = []
    link = f"agc-wsprove-{_now_ms()}"[:36]
    try:
        client = BybitWebSocketTradeClient(
            category="linear",
            testnet=False,
            demo=True,
            api_key=os.environ["BYBIT_DEMO_API_KEY"],
            api_secret=os.environ["BYBIT_DEMO_API_SECRET"],
        )
        try:
            reference = Decimal(str(_ticker_price(market, symbol)))
            tick = Decimal(str(contract["tick_size"]))
            price = (reference * Decimal("0.5") / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
            qty = _qty_for_min_notional(contract, price)

            def callback(message: Any) -> None:
                messages.append(message)

            client.place_order(
                callback,
                symbol=symbol,
                side="Buy",
                orderType="Limit",
                qty=qty,
                price=_decimal_text(price),
                timeInForce="PostOnly",
                orderLinkId=link,
                reduceOnly=False,
            )
            start = time.monotonic()
            while time.monotonic() - start < 6.0 and not messages:
                time.sleep(0.1)
            ws_open = [row for row in _open_orders(private, symbol) if str(row.get("orderLinkId") or "") == link]
            if ws_open:
                private.cancel_order(symbol=symbol, order_link_id=link)
            report["websocket_order_entry_probe"] = {
                "attempted": True,
                "order_link_id": link,
                "messages": messages,
                "open_order_seen": ws_open,
                "conclusion": "accepted_or_pending" if ws_open or messages else "no_ack_within_timeout",
            }
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - unavailable is the expected Bybit demo result today
        report["websocket_order_entry_probe"] = {
            "attempted": True,
            "order_link_id": link,
            "error": str(exc),
            "traceback_tail": traceback.format_exc()[-1200:],
            "conclusion": "unavailable_or_rejected",
        }


def _contract_for(market: BybitMarketData, symbol: str) -> dict[str, Any]:
    for row in market.get_instruments_info():
        if str(row.get("symbol", "")).upper() != symbol:
            continue
        lot = row.get("lotSizeFilter") or {}
        price_filter = row.get("priceFilter") or {}
        return {
            "min_order_qty": _float(lot.get("minOrderQty") or lot.get("qtyStep") or "0.001"),
            "qty_step": _float(lot.get("qtyStep") or "0.001"),
            "min_notional_value": _float(lot.get("minNotionalValue") or "0"),
            "tick_size": _float(price_filter.get("tickSize") or "0.1"),
        }
    raise RuntimeError(f"Instrument not found: {symbol}")


def _ticker_price(market: BybitMarketData, symbol: str) -> float:
    for row in market.get_tickers():
        if str(row.get("symbol", "")).upper() == symbol:
            for key in ("markPrice", "lastPrice", "indexPrice"):
                value = _float(row.get(key))
                if value > 0.0:
                    return value
    raise RuntimeError(f"Ticker not found: {symbol}")


def _nonzero_positions(private: BybitPrivateClient, symbol: str | None = None) -> list[dict[str, Any]]:
    rows = private.get_positions(symbol=symbol) if symbol else private.get_positions(settle_coin="USDT")
    return [row for row in rows if _float(row.get("size")) > 0.0]


def _open_orders(private: BybitPrivateClient, symbol: str | None = None) -> list[dict[str, Any]]:
    return private.get_open_orders(symbol=symbol) if symbol else private.get_open_orders(settle_coin="USDT")


def _wait_exec(private: BybitPrivateClient, symbol: str, link: str, *, timeout_s: float) -> dict[str, Any]:
    start = time.perf_counter()
    polls = 0
    last_summary: dict[str, Any] = {}
    while True:
        polls += 1
        rows = private.get_trade_history(symbol=symbol, order_link_id=link, limit=50)
        summary = _execution_summary(rows)
        last_summary = dict(summary)
        if _float(summary.get("qty")) > 0.0:
            return {"seen": True, "latency_ms": round((time.perf_counter() - start) * 1000.0, 3), "polls": polls, "summary": summary}
        if time.perf_counter() - start >= timeout_s:
            return {"seen": False, "latency_ms": round((time.perf_counter() - start) * 1000.0, 3), "polls": polls, "summary": last_summary}
        time.sleep(0.25)


def _cleanup_flat(private: BybitPrivateClient, symbol: str) -> dict[str, Any]:
    cleanup: dict[str, Any] = {"cancel_all_error": "", "flatten_orders": []}
    try:
        private.cancel_all_orders(symbol=symbol)
    except Exception as exc:  # noqa: BLE001 - still try to flatten position
        cleanup["cancel_all_error"] = str(exc)[:500]
    time.sleep(0.5)
    for position in _nonzero_positions(private, symbol):
        size = str(position.get("size") or "")
        side_text = str(position.get("side") or "").lower()
        close_side = "Sell" if side_text in {"buy", "long"} else "Buy"
        link = f"agc-clean-{_now_ms()}"[:36]
        try:
            result = private.place_order(
                symbol=symbol,
                side=close_side,
                orderType="Market",
                qty=size,
                orderLinkId=link,
                reduceOnly=True,
            )
            cleanup["flatten_orders"].append({"link": link, "result": result})
        except Exception as exc:  # noqa: BLE001
            cleanup["flatten_orders"].append({"link": link, "error": str(exc)[:500]})
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if not _nonzero_positions(private, symbol):
            break
        time.sleep(0.5)
    cleanup["final_positions"] = _nonzero_positions(private, symbol)
    cleanup["final_open_orders"] = _open_orders(private, symbol)
    return cleanup


def _qty_for_min_notional(contract: dict[str, Any], price: Decimal) -> str:
    min_qty = Decimal(str(contract["min_order_qty"]))
    step = Decimal(str(contract["qty_step"]))
    min_notional = Decimal(str(contract["min_notional_value"]))
    required = min_qty
    if min_notional > 0 and price > 0:
        required = max(required, min_notional / price)
    units = (required / step).to_integral_value(rounding=ROUND_CEILING)
    return _decimal_text(units * step)


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _now_ms() -> int:
    return int(time.time() * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
