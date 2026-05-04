from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import blake2b
from pathlib import Path
from typing import Any

import polars as pl

from .bybit import BybitMarketData, BybitPrivateClient
from .config import ResearchConfig
from .storage import dataset_path, read_dataset, write_dataset


@dataclass(frozen=True, slots=True)
class DemoProbeConfig:
    symbol: str = "XRPUSDT"
    side: str = "Sell"
    notional: float = 5.0
    max_notional: float = 10.0
    price_offset_bps: float = 500.0
    place_order: bool = False
    cancel_order: bool = True
    confirmed: bool = False
    account_type: str = "UNIFIED"


@dataclass(frozen=True, slots=True)
class DemoSyncConfig:
    max_order_notional: float = 10.0
    max_new_orders: int = 5
    max_total_new_notional: float = 50.0
    price_offset_bps: float = 2.0
    cancel_stale_minutes: int = 5
    submit_orders: bool = False
    confirmed: bool = False
    allow_market_exit: bool = True
    account_type: str = "UNIFIED"


def run_bybit_demo_sync(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    sync_config: DemoSyncConfig,
    now: datetime | None = None,
    market_client: Any | None = None,
    execution_client: Any | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> dict[str, Any]:
    if sync_config.max_order_notional <= 0.0:
        raise ValueError("max_order_notional must be positive")
    if sync_config.max_total_new_notional <= 0.0:
        raise ValueError("max_total_new_notional must be positive")
    if sync_config.submit_orders and not sync_config.confirmed:
        raise RuntimeError("Refusing demo sync order submission without --i-understand-demo-sync")

    now_dt = _as_utc(now or datetime.now(tz=UTC))
    now_ms = int(now_dt.timestamp() * 1000)
    trades = read_dataset(data_root, "forward_paper_trades")
    existing = read_dataset(data_root, "demo_execution_orders")
    market = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
    executor = _demo_executor(config, sync_config, execution_client, api_key, api_secret)
    instruments = {str(row.get("symbol", "")).upper(): row for row in market.get_instruments_info()}

    reconciled = reconcile_demo_orders(existing, now=now_dt, execution_client=executor) if executor is not None else existing
    reconciled = cancel_stale_demo_orders(reconciled, now=now_dt, sync_config=sync_config, execution_client=executor)
    new_orders = build_demo_sync_orders(
        trades,
        existing_orders=reconciled,
        instruments=instruments,
        sync_config=sync_config,
        now_ms=now_ms,
        market_client=market,
        execution_client=executor,
    )
    ledger = _merge_order_frames(reconciled, new_orders)
    summary = summarize_demo_sync_orders(ledger)
    payload = {
        "now": now_dt.isoformat(),
        "rows": {
            "paper_trades": trades.height,
            "existing_orders": existing.height,
            "new_orders": new_orders.height,
            "ledger_orders": ledger.height,
        },
        "summary": summary,
        "config": asdict(sync_config),
        "new_orders": new_orders.to_dicts() if not new_orders.is_empty() else [],
    }
    _write_demo_sync_outputs(data_root, ledger, payload=payload)
    return payload


def build_demo_sync_orders(
    trades: pl.DataFrame,
    *,
    existing_orders: pl.DataFrame,
    instruments: dict[str, dict[str, Any]],
    sync_config: DemoSyncConfig,
    now_ms: int,
    market_client: Any,
    execution_client: Any | None,
) -> pl.DataFrame:
    if trades.is_empty() or sync_config.max_new_orders <= 0:
        return pl.DataFrame()
    rows: list[dict[str, Any]] = []
    new_notional = 0.0
    sorted_trades = trades.sort(["entry_ts_ms", "symbol"]) if "entry_ts_ms" in trades.columns else trades
    for trade in sorted_trades.to_dicts():
        trade_id = str(trade.get("trade_id") or "")
        symbol = str(trade.get("symbol") or "").upper()
        if not trade_id or not symbol:
            continue
        if symbol not in instruments:
            rows.append(_skip_row(trade, "entry", now_ms, "instrument_missing"))
            continue
        if trade.get("status") == "open" and not _has_action(existing_orders, trade_id, "entry"):
            if len(rows) >= sync_config.max_new_orders:
                break
            try:
                row = _entry_order_row(
                    trade,
                    instrument=instruments[symbol],
                    sync_config=sync_config,
                    now_ms=now_ms,
                    market_client=market_client,
                    execution_client=execution_client,
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol should not block the batch
                row = _skip_row(trade, "entry", now_ms, f"build_failed: {exc}")
            estimated_notional = float(row.get("estimated_notional") or 0.0)
            if new_notional + estimated_notional > sync_config.max_total_new_notional:
                row = _skip_row(trade, "entry", now_ms, "max_total_new_notional_exceeded")
            rows.append(row)
            new_notional += estimated_notional if row.get("status") != "skipped" else 0.0
        elif trade.get("status") == "closed" and _has_action(existing_orders, trade_id, "entry") and not _has_action(
            existing_orders, trade_id, "exit"
        ):
            if len(rows) >= sync_config.max_new_orders:
                break
            try:
                row = _exit_order_row(
                    trade,
                    existing_orders=existing_orders,
                    sync_config=sync_config,
                    now_ms=now_ms,
                    execution_client=execution_client,
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol should not block the batch
                row = _skip_row(trade, "exit", now_ms, f"build_failed: {exc}")
            estimated_notional = float(row.get("estimated_notional") or 0.0)
            if new_notional + estimated_notional > sync_config.max_total_new_notional:
                row = _skip_row(trade, "exit", now_ms, "max_total_new_notional_exceeded")
            rows.append(row)
            new_notional += estimated_notional if row.get("status") != "skipped" else 0.0
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def reconcile_demo_orders(
    orders: pl.DataFrame,
    *,
    now: datetime,
    execution_client: Any,
) -> pl.DataFrame:
    if orders.is_empty():
        return orders
    symbols = sorted({str(symbol).upper() for symbol in orders["symbol"].drop_nulls().to_list() if str(symbol)})
    open_by_symbol: dict[str, list[dict[str, Any]]] = {}
    positions_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        try:
            open_by_symbol[symbol] = execution_client.get_open_orders(symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            open_by_symbol[symbol] = [{"__error__": str(exc)}]
        try:
            positions_by_symbol[symbol] = execution_client.get_positions(symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            positions_by_symbol[symbol] = [{"__error__": str(exc)}]
    rows = []
    for row in orders.to_dicts():
        updated = dict(row)
        symbol = str(row.get("symbol") or "").upper()
        order_link_id = str(row.get("order_link_id") or "")
        open_orders = open_by_symbol.get(symbol, [])
        positions = positions_by_symbol.get(symbol, [])
        open_error = next((item["__error__"] for item in open_orders if "__error__" in item), None)
        position_error = next((item["__error__"] for item in positions if "__error__" in item), None)
        open_match = next((item for item in open_orders if str(item.get("orderLinkId") or "") == order_link_id), None)
        position = _symbol_position(positions, symbol)
        updated["reconcile_ts_ms"] = int(now.timestamp() * 1000)
        updated["reconcile_time"] = now.isoformat()
        updated["open_order_seen"] = bool(open_match)
        updated["open_order_status"] = str(open_match.get("orderStatus") or "") if open_match else ""
        updated["position_side"] = position["side"]
        updated["position_size"] = position["size"]
        updated["position_value"] = position["value"]
        updated["reconcile_error"] = open_error or position_error
        if updated.get("status") in {"placed", "cancel_requested"} and not open_match and not position["size"]:
            updated["reconciled_status"] = "not_open_no_position"
        elif updated.get("status") in {"placed", "cancel_requested"} and not open_match and position["size"]:
            updated["reconciled_status"] = "position_detected"
        elif open_match:
            updated["reconciled_status"] = "open_order_seen"
        else:
            updated["reconciled_status"] = updated.get("reconciled_status") or ""
        rows.append(updated)
    return pl.DataFrame(rows, infer_schema_length=None)


def cancel_stale_demo_orders(
    orders: pl.DataFrame,
    *,
    now: datetime,
    sync_config: DemoSyncConfig,
    execution_client: Any | None,
) -> pl.DataFrame:
    if orders.is_empty() or execution_client is None or sync_config.cancel_stale_minutes < 0:
        return orders
    now_ms = int(now.timestamp() * 1000)
    rows = []
    for row in orders.to_dicts():
        updated = dict(row)
        status = str(updated.get("status") or "")
        if (
            status == "placed"
            and bool(updated.get("open_order_seen"))
            and str(updated.get("action")) == "entry"
            and now_ms - int(updated.get("created_ts_ms") or now_ms) >= max(sync_config.cancel_stale_minutes, 0) * 60_000
        ):
            try:
                result = execution_client.cancel_order(
                    symbol=str(updated["symbol"]),
                    order_link_id=str(updated["order_link_id"]),
                )
                updated["status"] = "cancel_requested"
                updated["cancel_result"] = json.dumps(result, sort_keys=True)
                updated["cancel_ts_ms"] = now_ms
                updated["cancel_time"] = now.isoformat()
            except Exception as exc:  # noqa: BLE001
                updated["status"] = "cancel_failed"
                updated["error"] = str(exc)
        rows.append(updated)
    return pl.DataFrame(rows, infer_schema_length=None)


def summarize_demo_sync_orders(orders: pl.DataFrame) -> dict[str, Any]:
    if orders.is_empty():
        return {
            "orders": 0,
            "placed": 0,
            "dry_run": 0,
            "skipped": 0,
            "cancel_requested": 0,
            "open_order_seen": 0,
            "estimated_notional": 0.0,
        }
    return {
        "orders": orders.height,
        "placed": _count_status(orders, "placed"),
        "dry_run": _count_status(orders, "dry_run"),
        "skipped": _count_status(orders, "skipped"),
        "cancel_requested": _count_status(orders, "cancel_requested"),
        "place_failed": _count_status(orders, "place_failed"),
        "open_order_seen": int(orders["open_order_seen"].sum()) if "open_order_seen" in orders.columns else 0,
        "estimated_notional": float(orders["estimated_notional"].sum()) if "estimated_notional" in orders.columns else 0.0,
    }


def format_demo_sync_report(payload: dict[str, Any], orders: pl.DataFrame) -> str:
    summary = payload.get("summary", {})
    lines = [
        "# Bybit Demo Execution Sync",
        "",
        f"Now: {payload.get('now')}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Paper trades | {payload.get('rows', {}).get('paper_trades', 0)} |",
        f"| New orders | {payload.get('rows', {}).get('new_orders', 0)} |",
        f"| Ledger orders | {summary.get('orders', 0)} |",
        f"| Placed | {summary.get('placed', 0)} |",
        f"| Dry run | {summary.get('dry_run', 0)} |",
        f"| Skipped | {summary.get('skipped', 0)} |",
        f"| Cancel requested | {summary.get('cancel_requested', 0)} |",
        f"| Open order seen | {summary.get('open_order_seen', 0)} |",
        f"| Estimated notional | {summary.get('estimated_notional', 0.0):.2f} |",
        "",
        "## Recent Orders",
        "",
        "| Time | Action | Symbol | Side | Status | Qty | Price | Notional | Paper Trade | Reconciled | Error |",
        "|---|---|---|---|---|---:|---:|---:|---|---|---|",
    ]
    if not orders.is_empty():
        for row in orders.sort("created_ts_ms", descending=True).head(50).to_dicts():
            lines.append(
                f"| {row.get('created_time', '')} | {row.get('action', '')} | {row.get('symbol', '')} | "
                f"{row.get('side', '')} | {row.get('status', '')} | {row.get('qty', '')} | "
                f"{row.get('price', '')} | {float(row.get('estimated_notional') or 0.0):.2f} | "
                f"{row.get('paper_trade_id', '')} | {row.get('reconciled_status', '')} | "
                f"{str(row.get('error') or row.get('reconcile_error') or '')[:120]} |"
            )
    lines.append("")
    return "\n".join(lines)


def run_bybit_demo_probe(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    probe_config: DemoProbeConfig,
    now: datetime | None = None,
    market_client: Any | None = None,
    execution_client: Any | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> dict[str, Any]:
    symbol = probe_config.symbol.strip().upper()
    side = _side(probe_config.side)
    if probe_config.notional <= 0.0:
        raise ValueError("demo probe notional must be positive")
    if probe_config.max_notional <= 0.0:
        raise ValueError("demo probe max_notional must be positive")
    if probe_config.notional > probe_config.max_notional:
        raise ValueError(
            f"demo probe notional {probe_config.notional} exceeds max_notional {probe_config.max_notional}"
        )
    if probe_config.place_order and not probe_config.confirmed:
        raise RuntimeError("Refusing to place a demo order without --i-understand-demo-order")

    now_dt = _as_utc(now or datetime.now(tz=UTC))
    market = market_client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
    instruments = market.get_instruments_info()
    instrument = _find_instrument(instruments, symbol)
    orderbook = market.get_orderbook(symbol, limit=1)
    quote = _top_of_book(orderbook)
    order = _build_probe_order(
        symbol=symbol,
        side=side,
        notional=probe_config.notional,
        max_notional=probe_config.max_notional,
        price_offset_bps=probe_config.price_offset_bps,
        quote=quote,
        instrument=instrument,
        timestamp_ms=int(now_dt.timestamp() * 1000),
    )

    wallet: dict[str, Any] | None = None
    place_result: dict[str, Any] | None = None
    cancel_result: dict[str, Any] | None = None
    status = "dry_run"
    if probe_config.place_order:
        key = api_key or os.environ.get("BYBIT_DEMO_API_KEY")
        secret = api_secret or os.environ.get("BYBIT_DEMO_API_SECRET")
        executor = execution_client or BybitPrivateClient(
            category=config.exchange.category,
            testnet=config.exchange.testnet,
            demo=True,
            api_key=key,
            api_secret=secret,
        )
        wallet = executor.get_wallet_balance(account_type=probe_config.account_type, coin=config.exchange.settle_coin)
        place_result = executor.place_order(**order["request"])
        status = "placed"
        if probe_config.cancel_order:
            cancel_result = executor.cancel_order(symbol=symbol, order_link_id=order["request"]["orderLinkId"])
            status = "placed_cancel_requested"

    payload = {
        "status": status,
        "now": now_dt.isoformat(),
        "config": asdict(probe_config),
        "symbol": symbol,
        "side": side,
        "quote": quote,
        "instrument": _instrument_report(instrument),
        "order": order,
        "wallet": wallet,
        "place_result": place_result,
        "cancel_result": cancel_result,
    }
    _write_demo_probe_report(data_root, payload)
    return payload


def format_demo_probe_report(payload: dict[str, Any]) -> str:
    request = payload.get("order", {}).get("request", {})
    quote = payload.get("quote", {})
    lines = [
        "# Bybit Demo Probe",
        "",
        f"Status: `{payload.get('status')}`",
        f"Now: {payload.get('now')}",
        "",
        "## Order",
        "",
        "| Field | Value |",
        "|---|---:|",
        f"| Symbol | {payload.get('symbol')} |",
        f"| Side | {payload.get('side')} |",
        f"| Price | {request.get('price')} |",
        f"| Qty | {request.get('qty')} |",
        f"| Time in force | {request.get('timeInForce')} |",
        f"| Order link ID | {request.get('orderLinkId')} |",
        f"| Bid | {quote.get('bid')} |",
        f"| Ask | {quote.get('ask')} |",
        "",
        "This probe intentionally uses a far-from-touch post-only order and cancels it by default. It is only an auth/order-path check, not a strategy fill test.",
        "",
    ]
    return "\n".join(lines)


def _build_probe_order(
    *,
    symbol: str,
    side: str,
    notional: float,
    max_notional: float,
    price_offset_bps: float,
    quote: dict[str, float],
    instrument: dict[str, Any],
    timestamp_ms: int,
) -> dict[str, Any]:
    tick_size = _decimal_filter(instrument, "priceFilter", "tickSize", "0.0001")
    qty_step = _decimal_filter(instrument, "lotSizeFilter", "qtyStep", "0.001")
    min_qty = _decimal_filter(instrument, "lotSizeFilter", "minOrderQty", "0")
    min_notional = _decimal_filter(instrument, "lotSizeFilter", "minNotionalValue", "0")
    offset = Decimal(str(price_offset_bps)) / Decimal("10000")
    bid = Decimal(str(quote["bid"]))
    ask = Decimal(str(quote["ask"]))
    if side == "Sell":
        raw_price = ask * (Decimal("1") + offset)
        price = _round_to_step(raw_price, tick_size, ROUND_CEILING)
    else:
        raw_price = bid * (Decimal("1") - offset)
        price = _round_to_step(raw_price, tick_size, ROUND_FLOOR)
    if price <= 0:
        raise ValueError(f"computed non-positive demo probe price for {symbol}: {price}")

    qty = _capped_order_qty(
        symbol=symbol,
        notional=Decimal(str(notional)),
        max_notional=Decimal(str(max_notional)),
        price=price,
        qty_step=qty_step,
        min_qty=min_qty,
        min_notional=min_notional,
        cap_name="max_notional",
    )
    estimated_notional = qty * price
    if estimated_notional > Decimal(str(max_notional)):
        raise ValueError(
            f"minimum rounded demo order for {symbol} is {_decimal_text(estimated_notional)} USDT, "
            f"above max_notional {max_notional}. Use a lower-priced symbol or raise --max-notional intentionally."
        )

    order_link_id = _order_link_id(symbol, timestamp_ms)
    request = {
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "qty": _decimal_text(qty),
        "price": _decimal_text(price),
        "timeInForce": "PostOnly",
        "positionIdx": 0,
        "reduceOnly": False,
        "orderLinkId": order_link_id,
    }
    return {
        "request": request,
        "estimated_notional": float(estimated_notional),
        "price_offset_bps": price_offset_bps,
        "tick_size": _decimal_text(tick_size),
        "qty_step": _decimal_text(qty_step),
        "min_qty": _decimal_text(min_qty),
        "min_notional": _decimal_text(min_notional),
    }


def _entry_order_row(
    trade: dict[str, Any],
    *,
    instrument: dict[str, Any],
    sync_config: DemoSyncConfig,
    now_ms: int,
    market_client: Any,
    execution_client: Any | None,
) -> dict[str, Any]:
    symbol = str(trade["symbol"]).upper()
    side = "Sell" if str(trade.get("side", "short")).lower() == "short" else "Buy"
    notional = min(_paper_notional(trade), sync_config.max_order_notional)
    if notional <= 0.0:
        return _skip_row(trade, "entry", now_ms, "non_positive_notional")
    quote = _top_of_book(market_client.get_orderbook(symbol, limit=1))
    order = _build_limit_order(
        symbol=symbol,
        side=side,
        notional=notional,
        max_notional=sync_config.max_order_notional,
        price_offset_bps=sync_config.price_offset_bps,
        quote=quote,
        instrument=instrument,
        order_link_id=_sync_order_link_id(str(trade["trade_id"]), "entry"),
        reduce_only=False,
    )
    return _submit_or_dry_order_row(
        trade,
        action="entry",
        order=order,
        status_if_dry="dry_run",
        now_ms=now_ms,
        execution_client=execution_client,
    )


def _exit_order_row(
    trade: dict[str, Any],
    *,
    existing_orders: pl.DataFrame,
    sync_config: DemoSyncConfig,
    now_ms: int,
    execution_client: Any | None,
) -> dict[str, Any]:
    symbol = str(trade["symbol"]).upper()
    position = _symbol_position(execution_client.get_positions(symbol=symbol) if execution_client is not None else [], symbol)
    if position["size"] <= 0.0:
        return _skip_row(trade, "exit", now_ms, "no_demo_position_detected")
    if not sync_config.allow_market_exit:
        return _skip_row(trade, "exit", now_ms, "market_exit_disabled")
    entry = _entry_order_for_trade(existing_orders, str(trade["trade_id"]))
    entry_qty = Decimal(str(entry.get("qty") or "0")) if entry else Decimal("0")
    qty = min(Decimal(str(position["size"])), entry_qty) if entry_qty > 0 else Decimal(str(position["size"]))
    if qty <= 0:
        return _skip_row(trade, "exit", now_ms, "non_positive_exit_qty")
    side = "Buy" if str(position["side"]).lower() == "sell" else "Sell"
    order_link_id = _sync_order_link_id(str(trade["trade_id"]), "exit")
    request = {
        "symbol": symbol,
        "side": side,
        "orderType": "Market" if sync_config.allow_market_exit else "Limit",
        "qty": _decimal_text(qty),
        "timeInForce": "IOC" if sync_config.allow_market_exit else "PostOnly",
        "positionIdx": 0,
        "reduceOnly": True,
        "orderLinkId": order_link_id,
    }
    order = {
        "request": request,
        "estimated_notional": min(float(position["value"]), sync_config.max_order_notional),
        "max_order_notional": sync_config.max_order_notional,
        "price_offset_bps": 0.0,
    }
    return _submit_or_dry_order_row(
        trade,
        action="exit",
        order=order,
        status_if_dry="dry_run",
        now_ms=now_ms,
        execution_client=execution_client,
    )


def _build_limit_order(
    *,
    symbol: str,
    side: str,
    notional: float,
    max_notional: float,
    price_offset_bps: float,
    quote: dict[str, float],
    instrument: dict[str, Any],
    order_link_id: str,
    reduce_only: bool,
) -> dict[str, Any]:
    tick_size = _decimal_filter(instrument, "priceFilter", "tickSize", "0.0001")
    qty_step = _decimal_filter(instrument, "lotSizeFilter", "qtyStep", "0.001")
    min_qty = _decimal_filter(instrument, "lotSizeFilter", "minOrderQty", "0")
    min_notional = _decimal_filter(instrument, "lotSizeFilter", "minNotionalValue", "0")
    offset = Decimal(str(price_offset_bps)) / Decimal("10000")
    bid = Decimal(str(quote["bid"]))
    ask = Decimal(str(quote["ask"]))
    if side == "Sell":
        price = _round_to_step(ask * (Decimal("1") + offset), tick_size, ROUND_CEILING)
    else:
        price = _round_to_step(bid * (Decimal("1") - offset), tick_size, ROUND_FLOOR)
    if price <= 0:
        raise ValueError(f"computed non-positive demo sync price for {symbol}: {price}")
    qty = _capped_order_qty(
        symbol=symbol,
        notional=Decimal(str(notional)),
        max_notional=Decimal(str(max_notional)),
        price=price,
        qty_step=qty_step,
        min_qty=min_qty,
        min_notional=min_notional,
        cap_name="max_order_notional",
    )
    estimated_notional = qty * price
    if estimated_notional > Decimal(str(max_notional)):
        raise ValueError(
            f"minimum rounded demo order for {symbol} is {_decimal_text(estimated_notional)} USDT, "
            f"above max_order_notional {max_notional}"
        )
    request = {
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "qty": _decimal_text(qty),
        "price": _decimal_text(price),
        "timeInForce": "PostOnly",
        "positionIdx": 0,
        "reduceOnly": reduce_only,
        "orderLinkId": order_link_id,
    }
    return {
        "request": request,
        "estimated_notional": float(estimated_notional),
        "max_order_notional": float(max_notional),
        "price_offset_bps": price_offset_bps,
        "tick_size": _decimal_text(tick_size),
        "qty_step": _decimal_text(qty_step),
        "min_qty": _decimal_text(min_qty),
        "min_notional": _decimal_text(min_notional),
    }


def _submit_or_dry_order_row(
    trade: dict[str, Any],
    *,
    action: str,
    order: dict[str, Any],
    status_if_dry: str,
    now_ms: int,
    execution_client: Any | None,
) -> dict[str, Any]:
    request = dict(order["request"])
    status = status_if_dry
    place_result: dict[str, Any] | None = None
    error: str | None = None
    if execution_client is not None:
        try:
            place_result = execution_client.place_order(**request)
            status = "placed"
        except Exception as exc:  # noqa: BLE001
            status = "place_failed"
            error = str(exc)
    return {
        "order_link_id": request["orderLinkId"],
        "order_id": str(place_result.get("orderId") or "") if place_result else "",
        "paper_trade_id": str(trade.get("trade_id") or ""),
        "basket_id": str(trade.get("basket_id") or ""),
        "date": str(trade.get("date") or _dt_from_ms(now_ms).date().isoformat()),
        "action": action,
        "status": status,
        "symbol": request["symbol"],
        "side": request["side"],
        "order_type": request["orderType"],
        "time_in_force": request.get("timeInForce", ""),
        "qty": request["qty"],
        "price": request.get("price", ""),
        "reduce_only": bool(request.get("reduceOnly")),
        "estimated_notional": float(order.get("estimated_notional") or 0.0),
        "max_order_notional": float(order.get("max_order_notional") or 0.0),
        "created_ts_ms": now_ms,
        "created_time": _dt_from_ms(now_ms).isoformat(),
        "paper_status": str(trade.get("status") or ""),
        "paper_entry_price": float(trade.get("entry_price") or 0.0),
        "paper_mark_price": float(trade.get("mark_price") or 0.0),
        "paper_exit_price": float(trade.get("exit_price") or 0.0) if trade.get("exit_price") is not None else None,
        "paper_exit_reason": str(trade.get("exit_reason") or ""),
        "request": json.dumps(request, sort_keys=True),
        "place_result": json.dumps(place_result, sort_keys=True) if place_result else "",
        "cancel_result": "",
        "cancel_ts_ms": None,
        "cancel_time": None,
        "open_order_seen": False,
        "open_order_status": "",
        "position_side": "",
        "position_size": 0.0,
        "position_value": 0.0,
        "reconciled_status": "",
        "reconcile_ts_ms": None,
        "reconcile_time": None,
        "reconcile_error": None,
        "error": error,
    }


def _write_demo_sync_outputs(
    data_root: str | Path,
    ledger: pl.DataFrame,
    *,
    payload: dict[str, Any],
) -> None:
    output_dir = Path(data_root) / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "bybit_demo_sync_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "bybit_demo_sync_report.md").write_text(format_demo_sync_report(payload, ledger), encoding="utf-8")
    if not ledger.is_empty():
        ledger.write_csv(output_dir / "bybit_demo_execution_orders.csv")
    path = dataset_path(data_root, "demo_execution_orders")
    if path.exists():
        shutil.rmtree(path)
    write_dataset(ledger, data_root, "demo_execution_orders", partition_by=("date", "symbol"), append=False)


def _demo_executor(
    config: ResearchConfig,
    sync_config: DemoSyncConfig | DemoProbeConfig,
    execution_client: Any | None,
    api_key: str | None,
    api_secret: str | None,
) -> Any | None:
    submit = getattr(sync_config, "submit_orders", False) or getattr(sync_config, "place_order", False)
    if not submit:
        return None
    if execution_client is not None:
        return execution_client
    key = api_key or os.environ.get("BYBIT_DEMO_API_KEY")
    secret = api_secret or os.environ.get("BYBIT_DEMO_API_SECRET")
    return BybitPrivateClient(
        category=config.exchange.category,
        testnet=config.exchange.testnet,
        demo=True,
        api_key=key,
        api_secret=secret,
    )


def _skip_row(trade: dict[str, Any], action: str, now_ms: int, reason: str) -> dict[str, Any]:
    order_link_id = _sync_order_link_id(str(trade.get("trade_id") or "missing"), action)
    return {
        "order_link_id": order_link_id,
        "order_id": "",
        "paper_trade_id": str(trade.get("trade_id") or ""),
        "basket_id": str(trade.get("basket_id") or ""),
        "date": str(trade.get("date") or _dt_from_ms(now_ms).date().isoformat()),
        "action": action,
        "status": "skipped",
        "symbol": str(trade.get("symbol") or ""),
        "side": "",
        "order_type": "",
        "time_in_force": "",
        "qty": "",
        "price": "",
        "reduce_only": False,
        "estimated_notional": 0.0,
        "max_order_notional": 0.0,
        "created_ts_ms": now_ms,
        "created_time": _dt_from_ms(now_ms).isoformat(),
        "paper_status": str(trade.get("status") or ""),
        "paper_entry_price": float(trade.get("entry_price") or 0.0),
        "paper_mark_price": float(trade.get("mark_price") or 0.0),
        "paper_exit_price": float(trade.get("exit_price") or 0.0) if trade.get("exit_price") is not None else None,
        "paper_exit_reason": str(trade.get("exit_reason") or ""),
        "request": "",
        "place_result": "",
        "cancel_result": "",
        "cancel_ts_ms": None,
        "cancel_time": None,
        "open_order_seen": False,
        "open_order_status": "",
        "position_side": "",
        "position_size": 0.0,
        "position_value": 0.0,
        "reconciled_status": "",
        "reconcile_ts_ms": None,
        "reconcile_time": None,
        "reconcile_error": None,
        "error": reason,
    }


def _has_action(orders: pl.DataFrame, trade_id: str, action: str) -> bool:
    return (
        not orders.is_empty()
        and {"paper_trade_id", "action"}.issubset(set(orders.columns))
        and orders.filter((pl.col("paper_trade_id") == trade_id) & (pl.col("action") == action)).height > 0
    )


def _entry_order_for_trade(orders: pl.DataFrame, trade_id: str) -> dict[str, Any] | None:
    if orders.is_empty() or {"paper_trade_id", "action"}.difference(set(orders.columns)):
        return None
    rows = orders.filter((pl.col("paper_trade_id") == trade_id) & (pl.col("action") == "entry")).to_dicts()
    return rows[-1] if rows else None


def _merge_order_frames(left: pl.DataFrame, right: pl.DataFrame) -> pl.DataFrame:
    frames = [frame for frame in (left, right) if not frame.is_empty()]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed").unique(subset=["order_link_id"], keep="last").sort(
        ["created_ts_ms", "symbol"]
    )


def _paper_notional(trade: dict[str, Any]) -> float:
    for key in ("actual_notional", "target_notional"):
        value = trade.get(key)
        if value is not None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0.0:
                return parsed
    return 0.0


def _symbol_position(positions: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    for row in positions:
        if "__error__" in row:
            continue
        if str(row.get("symbol") or "").upper() != symbol.upper():
            continue
        size = _float(row.get("size"))
        if size <= 0.0:
            continue
        return {
            "side": str(row.get("side") or ""),
            "size": size,
            "value": _float(row.get("positionValue", row.get("position_value"))),
        }
    return {"side": "", "size": 0.0, "value": 0.0}


def _sync_order_link_id(trade_id: str, action: str) -> str:
    digest = blake2b(f"{trade_id}:{action}".encode("utf-8"), digest_size=8).hexdigest()
    return f"agc{action[:1]}{digest}"[:36]


def _count_status(orders: pl.DataFrame, status: str) -> int:
    return 0 if orders.is_empty() or "status" not in orders.columns else int((orders["status"] == status).sum())


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _dt_from_ms(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)


def _write_demo_probe_report(data_root: str | Path, payload: dict[str, Any]) -> None:
    output_dir = Path(data_root) / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "bybit_demo_probe_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "bybit_demo_probe_report.md").write_text(format_demo_probe_report(payload), encoding="utf-8")


def _find_instrument(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    for row in rows:
        if str(row.get("symbol", "")).upper() == symbol:
            return row
    raise ValueError(f"symbol not found in Bybit instruments: {symbol}")


def _top_of_book(orderbook: dict[str, Any]) -> dict[str, float]:
    bids = orderbook.get("b") or orderbook.get("bids") or []
    asks = orderbook.get("a") or orderbook.get("asks") or []
    if not bids or not asks:
        raise ValueError(f"missing top of book: {orderbook}")
    bid = float(bids[0][0])
    ask = float(asks[0][0])
    if bid <= 0.0 or ask <= 0.0 or ask < bid:
        raise ValueError(f"invalid top of book bid={bid} ask={ask}")
    return {"bid": bid, "ask": ask, "spread_bps": (ask / bid - 1.0) * 10_000.0}


def _decimal_filter(instrument: dict[str, Any], section: str, key: str, default: str) -> Decimal:
    payload = instrument.get(section) or {}
    return Decimal(str(payload.get(key, default)))


def _round_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def _capped_order_qty(
    *,
    symbol: str,
    notional: Decimal,
    max_notional: Decimal,
    price: Decimal,
    qty_step: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    cap_name: str,
) -> Decimal:
    max_qty = _round_to_step(max_notional / price, qty_step, ROUND_FLOOR)
    target_qty = _round_to_step(min(notional, max_notional) / price, qty_step, ROUND_FLOOR)
    min_notional_qty = (min_notional / price) if min_notional > 0 else Decimal("0")
    min_required_qty = _round_to_step(max(min_qty, min_notional_qty), qty_step, ROUND_CEILING)
    qty = max(target_qty, min_required_qty)
    if qty <= 0:
        raise ValueError(f"computed non-positive demo qty for {symbol}: {qty}")
    if qty > max_qty or qty * price > max_notional:
        minimum = min_required_qty * price
        raise ValueError(
            f"minimum rounded demo order for {symbol} is {_decimal_text(minimum)} USDT, "
            f"above {cap_name} {max_notional}"
        )
    return qty


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _order_link_id(symbol: str, timestamp_ms: int) -> str:
    suffix = "".join(char for char in symbol.upper() if char.isalnum())[:10]
    return f"agcdemo{timestamp_ms % 100000000000:011d}{suffix}"[:36]


def _side(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"sell", "short"}:
        return "Sell"
    if normalized in {"buy", "long"}:
        return "Buy"
    raise ValueError("side must be Buy/Sell or long/short")


def _instrument_report(instrument: dict[str, Any]) -> dict[str, Any]:
    lot = instrument.get("lotSizeFilter") or {}
    price = instrument.get("priceFilter") or {}
    return {
        "symbol": instrument.get("symbol"),
        "status": instrument.get("status"),
        "tick_size": price.get("tickSize"),
        "qty_step": lot.get("qtyStep"),
        "min_order_qty": lot.get("minOrderQty"),
        "min_notional": lot.get("minNotionalValue"),
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
